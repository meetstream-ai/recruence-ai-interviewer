"""People, sign-in sessions and workspaces, kept in the connected database.

- Accounts: email (any provider, Gmail included) and a password, hashed with scrypt from the standard library.
  An account exists only in the database it was created in: connecting another database means signing in with
  an account from that one.
- Sessions: the browser holds a random value in an HttpOnly cookie; the database keeps only its SHA-256, the
  user and the workspace that browser is in. They expire after SESSION_DAYS and end at sign-out, a password
  change, or a database switch (the new database simply doesn't have them).
- Workspaces: independent recruiting spaces in one database. Members are owner, admin or recruiter. Joining
  takes a code from the owner AND their approval: a code only files a request.
- The first account is the installation's admin (AI providers, MeetStream, email, database) and the owner of
  the first workspace, which also takes over any records from before workspaces existed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import timedelta
from typing import Optional

import sqlalchemy as sa

from app.models import utcnow
from app.store import Store, auth_sessions, database_owner, memberships, password_resets, users, workspaces
from app.compare import same

SESSION_DAYS = 14
RESET_MINUTES = 60
MIN_PASSWORD = 8
WORKSPACE_ROLES = ("owner", "admin", "recruiter")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# scrypt cost: about 50 ms and 16 MB per check on a laptop.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


class AccountError(ValueError):
    """Something the person can fix (shown to them as is)."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        check = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(check, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


# Compared against when the email is unknown, so a wrong email takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> str:
    return utcnow().isoformat()


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Use at least {MIN_PASSWORD} characters for the password")
    if len(password) > 200:
        raise AccountError("That password is too long")


def normalise_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL.match(email) or len(email) > 320:
        raise AccountError("Enter a valid email address")
    return email


def new_join_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))  # ~60 bits
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def _clean_code(code: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9]", "", code).upper()
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}" if len(raw) == 12 else ""


def public_user(row: dict) -> dict:
    return {"id": row["id"], "email": row["email"], "name": row["name"], "is_admin": bool(row["is_admin"])}


# Workspaces one account may own: every account can create them, this only stops runaway creation.
MAX_OWNED_WORKSPACES = 25


class _NotFirst(Exception):
    """Rolls the owner claim back: the database already had accounts."""


class Accounts:
    def __init__(self, store: Store):
        self.store = store

    def _rows(self, stmt) -> list[dict]:
        return self.store._rows(stmt)

    def _run(self, stmt) -> int:
        return self.store._run(stmt)

    # ---------- users ----------

    def count(self) -> int:
        return self._rows(sa.select(sa.func.count().label("n")).select_from(users))[0]["n"]

    def user(self, user_id: str) -> Optional[dict]:
        rows = self._rows(sa.select(users).where(users.c.id == user_id))
        return rows[0] if rows else None

    def user_by_email(self, email: str) -> Optional[dict]:
        rows = self._rows(sa.select(users).where(users.c.email == email.strip().lower()))
        return rows[0] if rows else None

    def create_owner(self, email: str, name: str, password: str, install_id: str) -> Optional[dict]:
        """The database's first account, or None if another one claimed the database first. The claim (the owner row
        from no owner to this account) and the account are one transaction: two registrations at the same moment
        can't both become the owner, and a failed one leaves no claim behind."""
        row = self._new_user_row(email, name, password, is_admin=True)
        try:
            with self.store._engine.begin() as c:
                claimed = c.execute(database_owner.update().where(database_owner.c.id == 1, database_owner.c.owner_user_id.is_(None))
                                    .values(owner_user_id=row["id"], owner_install_id=install_id, claimed_at=row["created_at"])).rowcount
                if claimed != 1:
                    return None
                if c.execute(sa.select(sa.func.count()).select_from(users)).scalar():
                    raise _NotFirst()  # accounts made before ownership was recorded: not this one's database to claim
                c.execute(users.insert().values(**row))
        except _NotFirst:
            return None
        except sa.exc.IntegrityError:
            raise AccountError("An account with this email already exists in this database. Sign in instead.") from None
        return row

    def owner(self) -> dict:
        return self.store.database_identity()

    def in_someone_elses_workspace(self, user_id: str) -> bool:
        """Approved into a workspace by someone else (not only the account's own personal workspace)."""
        return bool(self._rows(sa.select(memberships.c.user_id).where(
            memberships.c.user_id == user_id, memberships.c.status == "approved", memberships.c.decided_by != user_id).limit(1)))

    def without_workspace(self) -> list[dict]:
        """Accounts in no workspace besides their own (no membership someone else approved, and none waiting): for
        an admin to review. Nothing is changed."""
        has = sa.select(memberships.c.user_id).where(sa.or_(
            sa.and_(memberships.c.status == "approved", memberships.c.decided_by != memberships.c.user_id),
            memberships.c.status == "pending"))
        rows = self._rows(sa.select(users.c.id, users.c.name, users.c.email, users.c.is_admin, users.c.disabled, users.c.created_at)
                          .where(users.c.id.not_in(has)).order_by(users.c.created_at))
        return rows

    def adopt_install(self, user_id: str, install_id: str) -> None:
        """A database recorded before installations were: the first admin to sign in records their installation as
        the owner's (once; never replaces one already recorded)."""
        self._run(database_owner.update().where(database_owner.c.id == 1, database_owner.c.owner_install_id.is_(None))
                  .values(owner_install_id=install_id, owner_user_id=sa.func.coalesce(database_owner.c.owner_user_id, user_id)))

    def create_user(self, email: str, name: str, password: str, is_admin: bool = False) -> dict:
        row = self._new_user_row(email, name, password, is_admin)
        try:
            self._run(users.insert().values(**row))
        except sa.exc.IntegrityError:
            raise AccountError("An account with this email already exists in this database. Sign in instead.") from None
        return row

    def _new_user_row(self, email: str, name: str, password: str, is_admin: bool) -> dict:
        email = normalise_email(email)
        name = " ".join(name.split())[:200]
        if not name:
            raise AccountError("Enter your name")
        check_password(password)
        now = _now()
        return {"id": uuid.uuid4().hex[:12], "email": email, "name": name, "password_hash": hash_password(password),
                "is_admin": is_admin, "disabled": False, "last_workspace_id": None, "created_at": now, "password_changed_at": now}

    def authenticate(self, email: str, password: str) -> Optional[dict]:
        row = self.user_by_email(email)
        ok = verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
        return row if row and ok and not row["disabled"] else None

    def set_password(self, user_id: str, password: str) -> None:
        check_password(password)
        self._run(users.update().where(users.c.id == user_id).values(password_hash=hash_password(password), password_changed_at=_now()))
        self._run(auth_sessions.delete().where(auth_sessions.c.user_id == user_id))  # every browser signs in again

    def update_profile(self, user_id: str, name: str) -> None:
        name = " ".join(name.split())[:200]
        if not name:
            raise AccountError("Enter your name")
        self._run(users.update().where(users.c.id == user_id).values(name=name))

    def set_admin(self, user_id: str, is_admin: bool) -> None:
        if not is_admin:
            admins = self._rows(sa.select(users.c.id).where(users.c.is_admin.is_(True), users.c.disabled.is_(False)))
            if [r["id"] for r in admins] == [user_id]:
                raise AccountError("This is the only administrator. Make someone else an administrator first.")
        self._run(users.update().where(users.c.id == user_id).values(is_admin=is_admin))

    def all_users(self) -> list[dict]:
        return [public_user(r) | {"created_at": r["created_at"]} for r in self._rows(sa.select(users).order_by(users.c.created_at))]

    def temporary_password(self, user_id: str) -> str:
        """A new password an administrator hands over (shown once); every browser of that person is signed out."""
        if self.user(user_id) is None:
            raise AccountError("There's no such account")
        password = "-".join(secrets.token_urlsafe(4) for _ in range(3))
        self.set_password(user_id, password)
        return password

    # ---------- sessions ----------

    def start_session(self, user_id: str, workspace_id: Optional[str]) -> str:
        token = secrets.token_urlsafe(32)
        now = utcnow()
        self._run(auth_sessions.insert().values(token_hash=_digest(token), user_id=user_id, workspace_id=workspace_id,
                                                created_at=now.isoformat(), expires_at=(now + timedelta(days=SESSION_DAYS)).isoformat()))
        return token

    def session(self, token: str) -> Optional[tuple[dict, Optional[str]]]:
        """(user, the browser's workspace) for a cookie value, or None if unknown, expired or the user is disabled."""
        if not token:
            return None
        rows = self._rows(sa.select(auth_sessions).where(auth_sessions.c.token_hash == _digest(token)))
        if not rows:
            return None
        s = rows[0]
        if s["expires_at"] <= _now():
            self._run(auth_sessions.delete().where(auth_sessions.c.token_hash == s["token_hash"]))
            return None
        user = self.user(s["user_id"])
        if user is None or user["disabled"]:
            return None
        return user, s["workspace_id"]

    def set_session_workspace(self, token: str, user_id: str, workspace_id: Optional[str]) -> None:
        self._run(auth_sessions.update().where(auth_sessions.c.token_hash == _digest(token)).values(workspace_id=workspace_id))
        self._run(users.update().where(users.c.id == user_id).values(last_workspace_id=workspace_id))  # restored at next sign-in

    def end_session(self, token: str) -> None:
        self._run(auth_sessions.delete().where(auth_sessions.c.token_hash == _digest(token)))

    # ---------- password reset ----------

    def start_reset(self, email: str) -> Optional[tuple[dict, str]]:
        try:
            row = self.user_by_email(normalise_email(email))
        except AccountError:
            return None
        if row is None or row["disabled"]:
            return None
        token = secrets.token_urlsafe(32)
        self._run(password_resets.delete().where(password_resets.c.user_id == row["id"]))
        self._run(password_resets.insert().values(token_hash=_digest(token), user_id=row["id"],
                                                  expires_at=(utcnow() + timedelta(minutes=RESET_MINUTES)).isoformat()))
        return row, token

    def finish_reset(self, token: str, password: str) -> dict:
        rows = self._rows(sa.select(password_resets).where(password_resets.c.token_hash == _digest(token)))
        if not rows or rows[0]["expires_at"] <= _now():
            raise AccountError("This reset link has expired or was already used. Ask for a new one.")
        check_password(password)
        self._run(password_resets.delete().where(password_resets.c.token_hash == rows[0]["token_hash"]))
        self.set_password(rows[0]["user_id"], password)
        return self.user(rows[0]["user_id"])

    # ---------- workspaces ----------

    def workspace(self, workspace_id: str) -> Optional[dict]:
        rows = self._rows(sa.select(workspaces).where(workspaces.c.id == workspace_id))
        return rows[0] if rows else None

    def workspace_for_careers_key(self, key: str) -> Optional[str]:
        """The workspace whose careers website posts applications with this key (its settings' careers_key), or None."""
        if not (24 <= len(key or "") <= 64):
            return None
        for row in self._rows(sa.select(workspaces.c.id, workspaces.c.settings)):
            try:
                saved = json.loads(row["settings"] or "{}").get("careers_key", "")
            except ValueError:
                continue
            if same(key, saved):
                return row["id"]
        return None

    def workspace_count(self) -> int:
        return self._rows(sa.select(sa.func.count().label("n")).select_from(workspaces))[0]["n"]

    def owned_count(self, user_id: str) -> int:
        return self._rows(sa.select(sa.func.count().label("n")).select_from(workspaces).where(workspaces.c.owner_id == user_id))[0]["n"]

    def ensure_personal_workspace(self, user: dict) -> Optional[str]:
        """Every account has a workspace of its own: made at registration, or at the next sign-in for an account
        from before (nothing else of theirs changes). Returns its id when one was made."""
        if self.owned_count(user["id"]):
            return None
        first = (user.get("name") or "").split()
        ws = self.create_workspace(f"{first[0]}'s workspace" if first else "My workspace", user["id"])
        self.update_workspace(ws["id"], settings={"notify_emails": [user["email"]]})
        return ws["id"]

    def create_workspace(self, name: str, owner_id: str, description: str = "") -> dict:
        name = " ".join(name.split())[:120]
        if not name:
            raise AccountError("Give the workspace a name")
        if self.owned_count(owner_id) >= MAX_OWNED_WORKSPACES:
            raise AccountError(f"You already own {MAX_OWNED_WORKSPACES} workspaces. Delete or hand one over before creating another.")
        first = self.workspace_count() == 0
        now = _now()
        row = {"id": uuid.uuid4().hex[:12], "name": name, "description": description.strip()[:500], "owner_id": owner_id,
               "created_at": now, "join_code": None, "join_code_expires_at": None, "settings": "{}"}
        self._run(workspaces.insert().values(**row))
        self._run(memberships.insert().values(workspace_id=row["id"], user_id=owner_id, role="owner", status="approved",
                                              requested_at=now, decided_at=now, decided_by=owner_id))
        if first:
            self.store.claim_unassigned(row["id"])  # everything made before workspaces existed is the first one's
        return row

    def update_workspace(self, workspace_id: str, name: Optional[str] = None, description: Optional[str] = None,
                         settings: Optional[dict] = None) -> dict:
        values: dict = {}
        if name is not None:
            if not " ".join(name.split()):
                raise AccountError("Give the workspace a name")
            values["name"] = " ".join(name.split())[:120]
        if description is not None:
            values["description"] = description.strip()[:500]
        if settings is not None:
            values["settings"] = json.dumps(settings)
        if values:
            self._run(workspaces.update().where(workspaces.c.id == workspace_id).values(**values))
        return self.workspace(workspace_id)

    def delete_workspace(self, workspace_id: str) -> None:
        self._run(workspaces.delete().where(workspaces.c.id == workspace_id))

    def settings_of(self, workspace_id: Optional[str]) -> dict:
        w = self.workspace(workspace_id) if workspace_id else None
        try:
            return json.loads(w["settings"]) if w else {}
        except ValueError:
            return {}

    def membership(self, workspace_id: str, user_id: str) -> Optional[dict]:
        rows = self._rows(sa.select(memberships).where(memberships.c.workspace_id == workspace_id, memberships.c.user_id == user_id))
        return rows[0] if rows else None

    def approved_role(self, workspace_id: Optional[str], user_id: str) -> Optional[str]:
        m = self.membership(workspace_id, user_id) if workspace_id else None
        return m["role"] if m and m["status"] == "approved" else None

    def workspaces_for(self, user_id: str) -> list[dict]:
        """Every workspace this person is in or asked to join, with their role and the request's state."""
        stmt = (sa.select(workspaces.c.id, workspaces.c.name, workspaces.c.description, memberships.c.role, memberships.c.status,
                          memberships.c.requested_at)
                .select_from(memberships.join(workspaces, memberships.c.workspace_id == workspaces.c.id))
                .where(memberships.c.user_id == user_id).order_by(workspaces.c.name))
        return self._rows(stmt)

    def default_workspace(self, user: dict) -> Optional[str]:
        """Where a sign-in lands: the last workspace used, if still allowed, else the first approved one."""
        approved = [w["id"] for w in self.workspaces_for(user["id"]) if w["status"] == "approved"]
        if user.get("last_workspace_id") in approved:
            return user["last_workspace_id"]
        return approved[0] if approved else None

    def members(self, workspace_id: str) -> list[dict]:
        stmt = (sa.select(users.c.id, users.c.email, users.c.name, memberships.c.role, memberships.c.status, memberships.c.requested_at,
                          memberships.c.decided_at)
                .select_from(memberships.join(users, memberships.c.user_id == users.c.id))
                .where(memberships.c.workspace_id == workspace_id).order_by(memberships.c.requested_at))
        return self._rows(stmt)

    # ---------- joining ----------

    def set_join_code(self, workspace_id: str, days: Optional[int]) -> dict:
        expires = (utcnow() + timedelta(days=days)).isoformat() if days else None
        self._run(workspaces.update().where(workspaces.c.id == workspace_id).values(join_code=new_join_code(), join_code_expires_at=expires))
        return self.workspace(workspace_id)

    def revoke_join_code(self, workspace_id: str) -> None:
        self._run(workspaces.update().where(workspaces.c.id == workspace_id).values(join_code=None, join_code_expires_at=None))

    def workspace_for_code(self, code: str) -> dict:
        """The workspace a joining code opens. Raises AccountError for a wrong or expired code."""
        clean = _clean_code(code)
        rows = self._rows(sa.select(workspaces).where(workspaces.c.join_code == clean)) if clean else []
        if not rows:
            raise AccountError("That joining code isn't valid. Check it with the workspace owner.")
        w = rows[0]
        if w["join_code_expires_at"] and w["join_code_expires_at"] <= _now():
            raise AccountError("That joining code has expired. Ask the workspace owner for a new one.")
        return w

    def request_join(self, code: str, user_id: str) -> dict:
        """File a membership request with a joining code. Raises AccountError for a wrong or expired code."""
        w = self.workspace_for_code(code)
        existing = self.membership(w["id"], user_id)
        if existing and existing["status"] in ("approved", "pending"):
            raise AccountError("You're already a member of this workspace" if existing["status"] == "approved"
                               else "Your request to join this workspace is waiting for the owner's approval")
        now = _now()
        if existing:  # asked before and was turned down: a new request
            self._run(memberships.update().where(memberships.c.workspace_id == w["id"], memberships.c.user_id == user_id)
                      .values(status="pending", role="recruiter", requested_at=now, decided_at=None, decided_by=None))
        else:
            self._run(memberships.insert().values(workspace_id=w["id"], user_id=user_id, role="recruiter", status="pending", requested_at=now))
        return w

    def decide(self, workspace_id: str, user_id: str, approve: bool, by: str, role: str = "recruiter") -> None:
        m = self.membership(workspace_id, user_id)
        if m is None or m["status"] != "pending":
            raise AccountError("There's no pending request from this person")
        if role not in ("admin", "recruiter"):
            raise AccountError("Choose admin or recruiter")
        self._run(memberships.update().where(memberships.c.workspace_id == workspace_id, memberships.c.user_id == user_id)
                  .values(status="approved" if approve else "rejected", role=role, decided_at=_now(), decided_by=by))

    def set_role(self, workspace_id: str, user_id: str, role: str) -> None:
        m = self.membership(workspace_id, user_id)
        if m is None or m["status"] != "approved":
            raise AccountError("This person isn't a member")
        if m["role"] == "owner":
            raise AccountError("Transfer ownership to change the owner's role")
        if role not in ("admin", "recruiter"):
            raise AccountError("Choose admin or recruiter")
        self._run(memberships.update().where(memberships.c.workspace_id == workspace_id, memberships.c.user_id == user_id).values(role=role))

    def remove_member(self, workspace_id: str, user_id: str) -> None:
        m = self.membership(workspace_id, user_id)
        if m is None:
            raise AccountError("This person isn't a member")
        if m["role"] == "owner" and m["status"] == "approved":
            raise AccountError("The owner can't be removed; transfer ownership first")
        self._run(memberships.delete().where(memberships.c.workspace_id == workspace_id, memberships.c.user_id == user_id))
        self._run(auth_sessions.update().where(auth_sessions.c.user_id == user_id, auth_sessions.c.workspace_id == workspace_id)
                  .values(workspace_id=None))  # their open browsers leave the workspace at once

    def transfer_owner(self, workspace_id: str, from_user: str, to_user: str) -> None:
        if self.approved_role(workspace_id, to_user) is None:
            raise AccountError("The new owner must be an approved member")
        with self.store._engine.begin() as c:
            c.execute(memberships.update().where(memberships.c.workspace_id == workspace_id, memberships.c.user_id == from_user).values(role="admin"))
            c.execute(memberships.update().where(memberships.c.workspace_id == workspace_id, memberships.c.user_id == to_user).values(role="owner"))
            c.execute(workspaces.update().where(workspaces.c.id == workspace_id).values(owner_id=to_user))
