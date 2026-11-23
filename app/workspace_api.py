"""HTTP routes for accounts, workspaces and the database connection (app/accounts.py, app/dbconfig.py).

Kept apart from app/server.py, which wires them in with `register`.

Three separate things:
- Signing in: an account in the connected database (email and password).
- Switching workspace: another workspace in the same database, without signing out.
- Switching database: connect another database (a saved connection or a new one). Everyone is signed out
  first; afterwards only that database's accounts and workspaces exist. Who may do it: an installation admin
  (who is then signed out and handed a short-lived connection pass), the first-run setup, or anyone holding a
  one-time code printed in the server log (for when nobody can sign in). Never while an interview is running.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import dbconfig
from app.accounts import AccountError, Accounts, WORKSPACE_ROLES, public_user
from app.mailer import Email
from app.security import ACCOUNT_COOKIE, WORKSPACE_COOKIE, User, is_direct_local, role_for_workspace
from app.store import CURRENT_WORKSPACE, Store, auth_sessions, describe
from app.compare import same

logger = logging.getLogger(__name__)

CONNECT_COOKIE = "mia_connect"
PUBLIC_DB_PROBLEM = ("The saved database couldn't be reached when Recruence started, so the local database is in use. "
                     "An administrator can reconnect it.")
TICKET_SECONDS = 30 * 60
LOGIN_WINDOW, LOGIN_TRIES = 15 * 60, 10


class RegisterBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)
    workspace_name: str = Field(default="", max_length=120)
    # From another installation of Recruence (not the one that set this database up): a workspace's joining code.
    join_code: str = Field(default="", max_length=40)


class LoginBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


class ResetRequestBody(BaseModel):
    email: str = Field(max_length=320)


class ResetBody(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class ProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class PasswordBody(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(min_length=1, max_length=200)


class WorkspaceBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class WorkspaceUpdateBody(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    notify_emails: Optional[list[str]] = Field(default=None, max_length=20)
    candidate_notices: Optional[bool] = None
    # What candidates see (the interview page and emails): the company, never the product.
    company_name: Optional[str] = Field(default=None, max_length=120)
    support_contact: Optional[str] = Field(default=None, max_length=200)
    # Interview links: "email_code" (a code to the application's address before the interview opens) or "none".
    gateway_verification: Optional[Literal["email_code", "none"]] = None
    # How the company's careers website is set up, for the instructions in Settings → Recruitment sources:
    # "recruence": the site lists the jobs set up here; "own": it has its own job pages and only forwards applications.
    careers_site: Optional[Literal["recruence", "own"]] = None
    # A site with its own job pages, when an application names a job title Recruence has no published role for:
    # "manual": it's refused until someone adds the role; "draft": a draft role is created and the application waits.
    careers_new_jobs: Optional[Literal["manual", "draft"]] = None
    # A site with its own job pages, when an application names a job title Recruence has no published role for:
    # "manual": it's refused until someone adds the role; "draft": a draft role is created and the application waits.
    careers_new_jobs: Optional[Literal["manual", "draft"]] = None


class SwitchBody(BaseModel):
    workspace_id: str = Field(max_length=40)


class JoinBody(BaseModel):
    code: str = Field(max_length=40)


class CodeBody(BaseModel):
    days: Optional[int] = Field(default=7, ge=1, le=365)  # None: doesn't expire


class DecisionBody(BaseModel):
    approve: bool
    role: str = "recruiter"


class RoleBody(BaseModel):
    role: str


class TransferBody(BaseModel):
    user_id: str = Field(max_length=40)


class AccountRulesBody(BaseModel):
    open_registration: Optional[bool] = None
    anyone_can_create_workspaces: Optional[bool] = None  # ignored: every account can create workspaces (kept for older clients)


class AdminBody(BaseModel):
    is_admin: bool


class UnlockBody(BaseModel):
    code: str = Field(max_length=40)


class ConnectBody(BaseModel):
    profile_id: Optional[str] = Field(default=None, max_length=40)  # a saved connection, or "local"
    url: Optional[str] = Field(default=None, max_length=2000)  # or a new one
    name: str = Field(default="", max_length=80)  # saved under this name
    save: bool = True
    use_existing: bool = False  # the target already holds Recruence data: confirm using it as it is


class TestBody(BaseModel):
    url: str = Field(max_length=2000)


class ConnectionGate:
    """Who may change the database without signing in: short-lived passes, and a one-time code from the log."""

    def __init__(self) -> None:
        self._tickets: dict[str, float] = {}
        self._code: Optional[str] = None
        self._code_until = 0.0
        self._failures = 0
        self._last_code: Optional[float] = None  # none yet (monotonic time starts near 0 on a freshly booted machine)

    def ticket(self) -> str:
        now = time.monotonic()
        self._tickets = {k: v for k, v in self._tickets.items() if v > now}
        t = secrets.token_urlsafe(24)
        self._tickets[t] = now + TICKET_SECONDS
        return t

    def valid(self, token: str) -> bool:
        return bool(token) and self._tickets.get(token, 0) > time.monotonic()

    def drop(self, token: str) -> None:
        self._tickets.pop(token, None)

    def request_code(self) -> bool:
        now = time.monotonic()
        if self._last_code is not None and now - self._last_code < 60:
            return False
        raw = secrets.token_hex(4).upper()
        self._code, self._code_until, self._failures, self._last_code = f"{raw[:4]}-{raw[4:]}", now + 15 * 60, 0, now
        # In the log only: whoever reads it runs the server.
        logger.warning("Database connection code (valid 15 minutes): %s", self._code)
        return True

    def unlock(self, code: str) -> Optional[str]:
        given = code.strip().upper().replace(" ", "")
        if len(given) == 8:
            given = f"{given[:4]}-{given[4:]}"
        if not self._code or time.monotonic() > self._code_until:
            return None
        if not same(given, self._code):
            self._failures += 1
            if self._failures >= 10:
                self._code = None
            return None
        self._code = None
        return self.ticket()


@dataclass
class Deps:
    store: Store
    accounts: Accounts
    settings: Any
    mailer: Any
    setup: Any  # app/setup.py SetupState
    box: Any
    gate: ConnectionGate
    db_state: dict
    switch_database: Callable[[Any, str], None]  # (target, source) -> None; raises HTTPException
    accounts_changed: Callable[[], None]
    background: Callable[[Any, str], Any]
    sharing_problem: Callable[[], Optional[str]] = lambda: None  # why several people can't share this database
    team_mode: Callable[[], bool] = lambda: False
    install_id: str = ""  # this installation (app/dbconfig.py installation_id)


def _secure(request: Request) -> bool:
    from app.security import served_over_https
    return served_over_https(request)


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(ACCOUNT_COOKIE, token, httponly=True, samesite="strict", secure=_secure(request), max_age=14 * 24 * 3600)


def client_of(request: Request) -> str:
    """The caller's address. A forwarded one is believed only from a proxy or tunnel on this machine."""
    from app.security import REMOTE, remote_client
    if REMOTE["on"]:
        return remote_client(request)  # the cloud backend: its trusted proxy names the visitor
    peer = request.client.host if request.client else "?"
    if peer in ("127.0.0.1", "::1", "localhost", "testclient"):
        forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        return forwarded or peer
    return peer


def register(app: FastAPI, d: Deps) -> None:
    accounts, store = d.accounts, d.store
    failures: dict[str, list[float]] = {}

    def limited(key: str, limit: int, window: float) -> bool:
        """Count one attempt for `key`; True when there were already `limit` in the last `window` seconds."""
        now = time.monotonic()
        recent = [t for t in failures.get(key, []) if now - t < window]
        failures[key] = recent + [now]
        if len(failures) > 5000:  # counters with nothing recent are dropped: memory can't grow without end
            for k in [k for k, hits in failures.items() if not hits or now - hits[-1] >= max(window, LOGIN_WINDOW)]:
                failures.pop(k, None)
        return len(recent) >= limit

    def current(request: Request) -> Optional[User]:
        return getattr(request.state, "user", None)

    def account(request: Request) -> User:
        user = current(request)
        if user is None or user.via != "account":
            raise HTTPException(409, "Sign in with an account for this")
        return user

    def workspace_manager(request: Request) -> User:
        user = account(request)
        if not user.workspace_id or user.workspace_role not in ("owner", "admin"):
            raise HTTPException(403, "Only the workspace's owner and admins can do this")
        return user

    def owner(request: Request) -> User:
        user = account(request)
        if user.workspace_role != "owner":
            raise HTTPException(403, "Only the workspace owner can do this")
        return user

    def may_bootstrap(request: Request) -> bool:
        """Creating the first account of a database: during setup, with a connection pass, or on the server itself."""
        return current(request) is not None or d.gate.valid(request.cookies.get(CONNECT_COOKIE, ""))

    def sign_in(response: Response, request: Request, user: dict) -> dict:
        if user.get("is_admin") and d.install_id:
            accounts.adopt_install(user["id"], d.install_id)  # an older database: its admin's installation owns it
        accounts.ensure_personal_workspace(user)
        ws = accounts.default_workspace(user)
        set_session_cookie(response, request, accounts.start_session(user["id"], ws))
        response.delete_cookie(CONNECT_COOKIE)
        return {"signed_in": True, "user": public_user(user), "workspace_id": ws}

    def ws_view(row: dict, role: str, manage: bool) -> dict:
        settings = accounts.settings_of(row["id"])
        out = {"id": row["id"], "name": row["name"], "description": row["description"], "created_at": row["created_at"], "role": role,
               "notify_emails": settings.get("notify_emails", []), "candidate_notices": bool(settings.get("candidate_notices", False)),
               "company_name": settings.get("company_name", ""), "support_contact": settings.get("support_contact", ""),
               "gateway_verification": settings.get("gateway_verification", "email_code"),
               "careers_site": settings.get("careers_site", ""),
               "careers_new_jobs": settings.get("careers_new_jobs", "manual"),
               "careers_new_jobs": settings.get("careers_new_jobs", "manual"),
               "sharing_problem": d.sharing_problem()}
        if manage:
            key = settings.get("careers_key", "")
            out["careers_path"] = f"/api/public/careers/{key}" if key else ""
            out["careers_url"] = f"{d.settings.public_base_url.rstrip('/')}/api/public/careers/{key}" if key and d.settings.public_base_url else ""
            out["join_code"] = row["join_code"]
            out["join_code_expires_at"] = row["join_code_expires_at"]
            out["members"] = accounts.members(row["id"])
        return out

    # ---------- accounts ----------

    def open_by_default() -> bool:
        """Whether people may create accounts without a joining code until an admin decides. Not on a cloud backend
        (REMOTE_DASHBOARD): anyone on the internet could otherwise register and use the installation's AI and MeetStream
        keys. An admin can still switch it on in Settings → Access → Accounts."""
        return not d.settings.remote_dashboard

    @app.post("/api/auth/register")
    def register_account(body: RegisterBody, request: Request, response: Response) -> dict:
        if limited(f"register:{client_of(request)}", 10, 3600):
            raise HTTPException(429, "Too many new accounts from this address. Try again later.")
        user, first = None, accounts.count() == 0
        if first:
            if not may_bootstrap(request):
                raise HTTPException(403, "This database has no accounts yet. Create the first one from the server itself, during setup, "
                                         "or after connecting the database.")
            try:
                # The database's owner: claimed atomically, so a second registration at the same moment isn't one too.
                user = accounts.create_owner(body.email, body.name, body.password, d.install_id)
            except AccountError as e:
                raise HTTPException(422, str(e)) from None
            first = user is not None
        joining = None
        if not first:
            owner = accounts.owner()
            # An installation that didn't set this database up (another computer pointed at it): only with a joining
            # code from one of its workspaces, whatever the registration setting. Knowing the connection string
            # isn't an invitation. A database from before owners were recorded is treated the same until its admin
            # signs in (which records their installation) or switches open registration on explicitly.
            open_setting = (store.get_state("accounts") or {}).get("open_registration")
            owner_install = owner.get("owner_install_id")
            foreign = owner_install != d.install_id if owner_install else open_setting is not True
            if foreign or body.join_code.strip():
                try:
                    joining = accounts.workspace_for_code(body.join_code)
                except AccountError as e:
                    raise HTTPException(403 if not body.join_code.strip() else 422,
                                        "This database belongs to another Recruence installation. Ask one of its workspace owners for a joining code."
                                        if not body.join_code.strip() else str(e)) from None
            elif not (store.get_state("accounts") or {}).get("open_registration", open_by_default()):
                raise HTTPException(403, "New accounts are switched off here. Ask an administrator.")
            if d.sharing_problem():
                raise HTTPException(409, "This installation uses a local database for one person, so it can't have more accounts. "
                                         + d.sharing_problem())
        try:
            if user is None:
                user = accounts.create_user(body.email, body.name, body.password, is_admin=False)
                if joining is not None:
                    accounts.request_join(body.join_code, user["id"])  # the workspace owner approves it
            # Every account starts with a workspace of its own, named as asked (or after the person).
            name = body.workspace_name.strip() or f"{body.name.split()[0]}'s workspace"
            ws = accounts.create_workspace(name, user["id"])
            accounts.update_workspace(ws["id"], settings={"notify_emails": [user["email"]]})
        except AccountError as e:
            raise HTTPException(422, str(e)) from None
        d.accounts_changed()
        if first and not d.setup.completed:
            # The account and its workspace are already in the database: a data folder that can't be written (a root-
            # owned volume on a cloud host, R-07) mustn't turn that into an error the person can't retry past. The
            # progress stays in memory; with PostgreSQL nothing else depends on the file.
            try:
                d.setup.update(access="account", admin_name=user["name"], admin_email=user["email"])
            except OSError as e:
                logger.warning("The first account was created, but setup progress couldn't be saved in %s (%s). "
                               "Check that DATA_DIR is writable by the app's user.", d.setup.path.parent, type(e).__name__)
        store.audit("account_created", detail="first account: database owner and installation admin" if first
                    else "with a joining code" if joining is not None else "")
        return {**sign_in(response, request, user), **({"join_requested": joining["name"]} if joining is not None else {})}

    @app.post("/api/auth/account-login")
    def account_login(body: LoginBody, request: Request, response: Response) -> dict:
        key = body.email.strip().lower()
        ip_key = f"login-ip:{client_of(request)}"
        now = time.monotonic()
        recent = [t for t in failures.get(key, []) if now - t < LOGIN_WINDOW]
        from_here = [t for t in failures.get(ip_key, []) if now - t < LOGIN_WINDOW]
        # Per account (guessing one password) and per address (trying one password on many accounts).
        if len(recent) >= LOGIN_TRIES or len(from_here) >= 3 * LOGIN_TRIES:
            raise HTTPException(429, "Too many attempts. Wait a few minutes, or reset your password.")
        user = accounts.authenticate(body.email, body.password)
        if user is None:
            failures[key] = recent + [now]
            failures[ip_key] = from_here + [now]
            raise HTTPException(401, "Wrong email or password")
        failures.pop(key, None)
        return sign_in(response, request, user)

    @app.post("/api/auth/reset-request")
    async def reset_request(body: ResetRequestBody, request: Request) -> dict:
        """Always the same answer, so it can't tell who has an account. At most 3 emails an hour to one address and
        10 requests an hour from one client, so it can't be used to flood an inbox."""
        flood = limited(f"reset-ip:{client_of(request)}", 10, 3600) | limited(f"reset:{body.email.strip().lower()}", 3, 3600)
        if d.mailer.enabled and not flood:
            started = accounts.start_reset(body.email)
            if started:
                user, token = started
                base = (d.settings.dashboard_url or str(request.base_url)).rstrip("/")
                email = Email(to=[user["email"]], subject="Reset your Recruence password",
                              body=f"Hello {user['name'].split()[0]},\n\nTo choose a new password, open this link within the next hour:\n\n"
                                   f"{base}/#/reset/{token}\n\nIf you didn't ask for this, ignore this email; your password stays as it is.\n")
                d.background(d.mailer.send_retrying(email), "password reset email")
        return {"sent": True, "email_enabled": d.mailer.enabled}

    @app.post("/api/auth/reset")
    def reset_password(body: ResetBody, request: Request, response: Response) -> dict:
        try:
            user = accounts.finish_reset(body.token, body.password)
        except AccountError as e:
            raise HTTPException(422, str(e)) from None
        store.audit("password_reset")
        return sign_in(response, request, user)

    @app.get("/api/me")
    def me(request: Request) -> dict:
        user = current(request)
        if user is None:
            raise HTTPException(401, "Sign in to continue")
        if user.via != "account":
            return {"user": {"name": user.name, "via": user.via, "is_admin": user.install_admin}, "workspace_id": user.workspace_id,
                    "workspaces": [{"id": w["id"], "name": w["name"], "role": "admin", "status": "approved"} for w in _all_workspaces()]}
        row = accounts.user(user.user_id)
        return {"user": {**public_user(row), "via": "account"}, "workspace_id": user.workspace_id, "workspace_role": user.workspace_role,
                "workspaces": accounts.workspaces_for(user.user_id)}

    def _all_workspaces() -> list[dict]:
        from app.store import workspaces
        import sqlalchemy as sa
        return store._rows(sa.select(workspaces.c.id, workspaces.c.name).order_by(workspaces.c.created_at))

    @app.put("/api/me")
    def update_me(body: ProfileBody, request: Request) -> dict:
        user = account(request)
        try:
            accounts.update_profile(user.user_id, body.name)
        except AccountError as e:
            raise HTTPException(422, str(e)) from None
        return {"saved": True}

    @app.post("/api/me/password")
    def change_password(body: PasswordBody, request: Request, response: Response) -> dict:
        user = account(request)
        row = accounts.user(user.user_id)
        if accounts.authenticate(row["email"], body.current) is None:
            raise HTTPException(422, "Your current password isn't right")
        try:
            accounts.set_password(user.user_id, body.new)
        except AccountError as e:
            raise HTTPException(422, str(e)) from None
        store.audit("password_changed")
        return sign_in(response, request, row)  # other browsers are signed out; this one stays in

    # ---------- workspaces ----------

    @app.post("/api/workspaces", status_code=201)
    def create_workspace(body: WorkspaceBody, request: Request, response: Response) -> dict:
        user = account(request)  # any signed-in account: creating workspaces isn't an administrator's privilege
        try:
            ws = accounts.create_workspace(body.name, user.user_id, body.description)
        except AccountError as e:
            raise HTTPException(422, str(e)) from None
        accounts.update_workspace(ws["id"], settings={"notify_emails": [user.email]})
        token = request.cookies.get(ACCOUNT_COOKIE, "")
        accounts.set_session_workspace(token, user.user_id, ws["id"])
        store.audit("workspace_created", detail=ws["name"])
        return ws_view(accounts.workspace(ws["id"]), "owner", True)

    @app.post("/api/workspaces/switch")
    def switch_workspace(body: SwitchBody, request: Request, response: Response) -> dict:
        user = current(request)
        if user is None:
            raise HTTPException(401, "Sign in to continue")
        if user.via == "account":
            if accounts.approved_role(body.workspace_id, user.user_id) is None:
                raise HTTPException(403, "You're not an approved member of that workspace")
            accounts.set_session_workspace(request.cookies.get(ACCOUNT_COOKIE, ""), user.user_id, body.workspace_id)
        else:
            if user.role != "admin":
                raise HTTPException(403, "Only administrators can switch workspaces here")
            if accounts.workspace(body.workspace_id) is None:
                raise HTTPException(404, "Workspace not found")
            response.set_cookie(WORKSPACE_COOKIE, body.workspace_id, httponly=True, samesite="strict", secure=_secure(request))
        return {"workspace_id": body.workspace_id}

    @app.post("/api/workspaces/join")
    def join_workspace(body: JoinBody, request: Request) -> dict:
        user = account(request)
        key = f"join:{user.user_id}"
        now = time.monotonic()
        recent = [t for t in failures.get(key, []) if now - t < 3600]
        if len(recent) >= 10:
            raise HTTPException(429, "Too many wrong codes. Try again in an hour.")
        try:
            ws = accounts.request_join(body.code, user.user_id)
        except AccountError as e:
            failures[key] = recent + [now]
            raise HTTPException(422, str(e)) from None
        token = CURRENT_WORKSPACE.set(ws["id"])
        try:
            store.audit("membership_requested", detail=user.email)
        finally:
            CURRENT_WORKSPACE.reset(token)
        return {"requested": True, "workspace": {"id": ws["id"], "name": ws["name"]}, "status": "pending"}

    @app.get("/api/workspaces/current")
    def current_workspace(request: Request) -> dict:
        user = current(request)
        if user is None or not user.workspace_id:
            raise HTTPException(404, "You're not in a workspace")
        row = accounts.workspace(user.workspace_id)
        if row is None:
            raise HTTPException(404, "Workspace not found")
        role = user.workspace_role if user.via == "account" else user.role  # token, local and proxy users: their own role
        return ws_view(row, role, role in ("owner", "admin"))

    @app.put("/api/workspaces/current")
    def update_workspace(body: WorkspaceUpdateBody, request: Request) -> dict:
        user = workspace_manager(request)
        settings = accounts.settings_of(user.workspace_id)
        if body.notify_emails is not None:
            clean = []
            for e in body.notify_emails:
                e = e.strip().lower()
                if e and ("@" not in e or len(e) > 320):
                    raise HTTPException(422, f"Not an email address: {e}")
                if e:
                    clean.append(e)
            settings["notify_emails"] = clean
        if body.candidate_notices is not None:
            settings["candidate_notices"] = body.candidate_notices
        if body.company_name is not None:
            settings["company_name"] = " ".join(body.company_name.split())
        if body.support_contact is not None:
            settings["support_contact"] = body.support_contact.strip()
        if body.gateway_verification is not None:
            settings["gateway_verification"] = body.gateway_verification
        if body.careers_site is not None:
            settings["careers_site"] = body.careers_site
        if body.careers_new_jobs is not None:
            settings["careers_new_jobs"] = body.careers_new_jobs
        if body.careers_new_jobs is not None:
            settings["careers_new_jobs"] = body.careers_new_jobs
        try:
            row = accounts.update_workspace(user.workspace_id, body.name, body.description, settings)
        except AccountError as e:
            raise HTTPException(422, str(e)) from None
        store.audit("workspace_updated", detail="interview links without a code" if body.gateway_verification == "none" else "")
        return ws_view(row, user.workspace_role, True)

    @app.post("/api/workspaces/current/careers-key")
    def new_careers_key(request: Request) -> dict:
        """The address a careers website's server posts every application to (/api/public/careers/<key>). A new one
        replaces the old, which stops working."""
        user = workspace_manager(request)
        settings = accounts.settings_of(user.workspace_id)
        settings["careers_key"] = secrets.token_urlsafe(24)
        row = accounts.update_workspace(user.workspace_id, None, None, settings)
        store.audit("careers_address_created")
        return ws_view(row, user.workspace_role, True)

    @app.delete("/api/workspaces/current/careers-key")
    def revoke_careers_key(request: Request) -> dict:
        user = workspace_manager(request)
        settings = accounts.settings_of(user.workspace_id)
        settings.pop("careers_key", None)
        row = accounts.update_workspace(user.workspace_id, None, None, settings)
        store.audit("careers_address_revoked")
        return ws_view(row, user.workspace_role, True)

    @app.post("/api/workspaces/current/code")
    def new_code(body: CodeBody, request: Request) -> dict:
        user = workspace_manager(request)
        if d.sharing_problem():
            raise HTTPException(409, d.sharing_problem())
        row = accounts.set_join_code(user.workspace_id, body.days)
        store.audit("join_code_created", detail=f"expires {row['join_code_expires_at'] or 'never'}")
        return ws_view(row, user.workspace_role, True)

    @app.delete("/api/workspaces/current/code")
    def revoke_code(request: Request) -> dict:
        user = workspace_manager(request)
        accounts.revoke_join_code(user.workspace_id)
        store.audit("join_code_revoked")
        return ws_view(accounts.workspace(user.workspace_id), user.workspace_role, True)

    @app.post("/api/workspaces/current/members/{user_id}/decision")
    def decide(user_id: str, body: DecisionBody, request: Request) -> dict:
        user = workspace_manager(request)
        if body.approve and d.sharing_problem():
            raise HTTPException(409, d.sharing_problem())
        if body.role == "admin" and user.workspace_role != "owner":
            raise HTTPException(403, "Only the owner can make someone an admin")
        try:
            accounts.decide(user.workspace_id, user_id, body.approve, user.user_id, body.role)
        except AccountError as e:
            raise HTTPException(409, str(e)) from None
        store.audit("membership_approved" if body.approve else "membership_rejected", detail=user_id)
        return ws_view(accounts.workspace(user.workspace_id), user.workspace_role, True)

    @app.put("/api/workspaces/current/members/{user_id}")
    def set_role(user_id: str, body: RoleBody, request: Request) -> dict:
        user = owner(request)
        try:
            accounts.set_role(user.workspace_id, user_id, body.role)
        except AccountError as e:
            raise HTTPException(409, str(e)) from None
        store.audit("member_role_changed", detail=f"{user_id}: {body.role}")
        return ws_view(accounts.workspace(user.workspace_id), "owner", True)

    @app.delete("/api/workspaces/current/members/{user_id}")
    def remove_member(user_id: str, request: Request) -> dict:
        user = workspace_manager(request)
        target = accounts.membership(user.workspace_id, user_id)
        if target and target["role"] == "admin" and target["status"] == "approved" and user.workspace_role != "owner":
            raise HTTPException(403, "Only the owner can remove an admin")
        try:
            accounts.remove_member(user.workspace_id, user_id)
        except AccountError as e:
            raise HTTPException(409, str(e)) from None
        store.audit("member_removed", detail=user_id)
        return ws_view(accounts.workspace(user.workspace_id), user.workspace_role, True)

    @app.post("/api/workspaces/current/transfer")
    def transfer(body: TransferBody, request: Request) -> dict:
        user = owner(request)
        try:
            accounts.transfer_owner(user.workspace_id, user.user_id, body.user_id)
        except AccountError as e:
            raise HTTPException(409, str(e)) from None
        store.audit("ownership_transferred", detail=body.user_id)
        return {"transferred": True}

    @app.post("/api/workspaces/current/leave")
    def leave(request: Request) -> dict:
        user = account(request)
        if not user.workspace_id:
            raise HTTPException(404, "You're not in a workspace")
        try:
            accounts.remove_member(user.workspace_id, user.user_id)
        except AccountError as e:
            raise HTTPException(409, str(e)) from None
        accounts.set_session_workspace(request.cookies.get(ACCOUNT_COOKIE, ""), user.user_id, None)
        return {"left": True}

    # ---------- accounts in this database (installation admins) ----------

    def installation_admin(request: Request) -> User:
        user = current(request)
        if user is None or not user.install_admin:
            raise HTTPException(403, "Only the installation's admins can do this")
        return user

    def account_rules() -> dict:
        return {"open_registration": (store.get_state("accounts") or {}).get("open_registration", open_by_default())}

    @app.get("/api/accounts")
    def list_accounts(request: Request) -> dict:
        installation_admin(request)
        return {"users": accounts.all_users(), **account_rules()}

    @app.put("/api/accounts/rules")
    def set_account_rules(body: AccountRulesBody, request: Request) -> dict:
        installation_admin(request)
        if body.open_registration is not None:
            store.set_state("accounts", {**(store.get_state("accounts") or {}), "open_registration": body.open_registration})
        store.audit("account_rules_changed", detail=", ".join(f"{k}={v}" for k, v in body.model_dump(exclude_none=True).items()))
        return account_rules()

    @app.post("/api/accounts/{user_id}/temporary-password")
    def temporary_password(user_id: str, request: Request) -> dict:
        admin = installation_admin(request)
        if admin.via == "account" and admin.user_id == user_id:
            raise HTTPException(409, "Change your own password in Your account")
        try:
            password = accounts.temporary_password(user_id)
        except AccountError as e:
            raise HTTPException(404, str(e)) from None
        store.audit("temporary_password_set", detail=user_id)
        return {"password": password}  # shown once; the person changes it in Your account

    @app.put("/api/accounts/{user_id}/admin")
    def set_installation_admin(user_id: str, body: AdminBody, request: Request) -> dict:
        installation_admin(request)
        if accounts.user(user_id) is None:
            raise HTTPException(404, "There's no such account")
        try:
            accounts.set_admin(user_id, body.is_admin)
        except AccountError as e:
            raise HTTPException(409, str(e)) from None
        store.audit("installation_admin_changed", detail=f"{user_id}: {body.is_admin}")
        return {"users": accounts.all_users()}

    # ---------- database connection ----------

    def connection_allowed(request: Request) -> bool:
        # Someone at the computer running Recruence already controls its data folder: asking them for a code from the
        # server's log only got in the way. From anywhere else it still takes an admin or that code.
        if is_direct_local(request):
            return True
        user = current(request)
        if user is not None and user.install_admin and (user.via != "local" or accounts.count() == 0):
            return True
        return d.gate.valid(request.cookies.get(CONNECT_COOKIE, ""))

    def need_connection(request: Request) -> None:
        if not connection_allowed(request):
            raise HTTPException(403, "Sign in as an administrator, or enter the connection code from the server log")

    @app.get("/api/connection/status")
    def connection_status(request: Request) -> dict:
        allowed = connection_allowed(request)
        problem = d.db_state.get("problem", "")
        base = {"backend": store.backend, "env_locked": d.db_state["source"] == "env", "allowed": allowed,
                # Which server it is stays private until the visitor may manage the connection.
                "problem": problem if allowed else (PUBLIC_DB_PROBLEM if problem else ""),
                "has_accounts": accounts.count() > 0}
        if not allowed:
            return base  # where the database is stays private until the visitor may manage it
        source = d.db_state["source"]
        # On the fallback (the saved database was down at start) the local database is what's in use.
        active = None if source == "env" else dbconfig.LOCAL if source == "fallback" else dbconfig.active_profile(d.settings.data_dir)
        return {**base, "current": describe(store.target), "source": d.db_state["source"], "active_profile": active,
                "profiles": [{"id": dbconfig.LOCAL, "name": "Local database (SQLite)", "active": active == dbconfig.LOCAL}]
                + [{**p, "active": p["id"] == active} for p in dbconfig.profiles(d.settings.data_dir)]}

    @app.post("/api/connection/request-code")
    def request_code() -> dict:
        return {"logged": d.gate.request_code()}

    @app.post("/api/connection/unlock")
    def connection_unlock(body: UnlockBody, request: Request, response: Response) -> dict:
        if any(h in request.headers for h in ("x-forwarded-for", "forwarded", "cf-connecting-ip", "x-real-ip")):
            raise HTTPException(403, "Manage the database on the server's own network, not through the public address.")
        ticket = d.gate.unlock(body.code)
        if ticket is None:
            raise HTTPException(401, "That isn't the current connection code. Ask for one; it's printed in the server log.")
        response.set_cookie(CONNECT_COOKIE, ticket, httponly=True, samesite="strict", secure=_secure(request), max_age=TICKET_SECONDS)
        return {"unlocked": True}

    @app.post("/api/connection/switch-start")
    def switch_start(request: Request, response: Response) -> dict:
        """Switch database, step 1: an admin is signed out and gets a pass for the connection screen."""
        user = current(request)
        if user is None or not user.install_admin:
            raise HTTPException(403, "Only an installation admin can switch the database")
        if d.db_state["source"] == "env":
            raise HTTPException(409, "DATABASE_URL in the environment decides the database. Remove it there to switch here.")
        token = request.cookies.get(ACCOUNT_COOKIE, "")
        if token:
            accounts.end_session(token)
        response.delete_cookie(ACCOUNT_COOKIE)
        response.set_cookie(CONNECT_COOKIE, d.gate.ticket(), httponly=True, samesite="strict", secure=_secure(request), max_age=TICKET_SECONDS)
        store.audit("database_switch_started")
        return {"ok": True}

    @app.post("/api/connection/test")
    async def connection_test(body: TestBody, request: Request) -> dict:
        need_connection(request)
        return await asyncio.to_thread(dbconfig.test, body.url)

    @app.post("/api/connection/connect")
    async def connect(body: ConnectBody, request: Request, response: Response) -> dict:
        need_connection(request)
        if d.db_state["source"] == "env":
            raise HTTPException(409, "DATABASE_URL in the environment decides the database. Remove it there to switch here.")
        data_dir = d.settings.data_dir
        if body.profile_id == dbconfig.LOCAL:
            if d.team_mode() and not d.settings.allow_shared_sqlite:
                raise HTTPException(409, "This is a team installation: its members share a hosted PostgreSQL database, "
                                         "so the local one (for one person) can't be connected here.")
            target, label, profile = d.settings.db_path, "Local database (SQLite)", dbconfig.LOCAL
        else:
            try:
                url = dbconfig.profile_url(data_dir, d.box, body.profile_id) if body.profile_id else dbconfig.normalise(body.url or "")
            except dbconfig.DatabaseConfigError as e:
                raise HTTPException(422, str(e)) from None
            result = await asyncio.to_thread(dbconfig.test, url)
            if not result["ok"]:
                raise HTTPException(422, result["message"])
            if result["has_mia_tables"] and not body.use_existing and not body.profile_id:
                raise HTTPException(409, f"{result.get('summary') or 'That database already holds Recruence data.'} Confirm to use this existing installation.")
            target, label, profile = url, result["database"], body.profile_id
        # The same database, however its connection string is written: its own id (when it has one) decides.
        current_id = store.database_identity().get("database_id", "")
        if describe(target) == describe(store.target) or (profile != dbconfig.LOCAL and current_id and result.get("database_id") == current_id):
            raise HTTPException(409, "That's the database already in use")
        d.switch_database(target, "local" if profile == dbconfig.LOCAL else "saved")
        if profile is None:
            name = body.name.strip() or label
            if body.save:
                dbconfig.save_profile(data_dir, d.box, name, target, by="connection screen", database_id=store.database_identity().get("database_id", ""))
        else:
            dbconfig.set_active(data_dir, profile)
        # The pass stays valid for creating the first account if this database has none.
        response.delete_cookie(ACCOUNT_COOKIE)
        return {"connected": describe(target), "has_accounts": accounts.count() > 0}

    @app.delete("/api/connection/profiles/{profile_id}")
    def remove_profile(profile_id: str, request: Request) -> dict:
        need_connection(request)
        try:
            dbconfig.remove_profile(d.settings.data_dir, profile_id)
        except dbconfig.DatabaseConfigError as e:
            raise HTTPException(409, str(e)) from None
        return {"removed": True}


def account_user_resolver(accounts: Accounts) -> Callable[[Request], Optional[User]]:
    def resolve(request: Request) -> Optional[User]:
        token = request.cookies.get(ACCOUNT_COOKIE, "")
        found = accounts.session(token) if token else None
        if found is None:
            return None
        row, ws = found
        ws_role = accounts.approved_role(ws, row["id"]) if ws else None
        if ws and ws_role is None:  # removed from it since: out of the workspace at once
            ws = None
        role = role_for_workspace(ws_role) if ws_role else ("admin" if row["is_admin"] else "recruiter")
        return User(name=row["name"], role=role, via="account", user_id=row["id"], email=row["email"],
                    install_admin=bool(row["is_admin"]), workspace_id=ws, workspace_role=ws_role or "")
    return resolve


def workspace_resolver(accounts: Accounts, store: Store) -> Callable[[Request, User], Optional[str]]:
    """Token, proxy and localhost sign-ins: the workspace chosen in this browser, else the first one."""
    import sqlalchemy as sa
    from app.store import workspaces

    def resolve(request: Request, user: User) -> Optional[str]:
        # Choosing a workspace is for the installation's admins (access token, this computer, proxy admins): an SSO
        # proxy's recruiters and viewers have no memberships, so they stay in the default workspace.
        chosen = request.cookies.get(WORKSPACE_COOKIE, "") or request.headers.get("x-mia-workspace", "")
        if chosen and user.role == "admin" and accounts.workspace(chosen):
            return chosen
        rows = store._rows(sa.select(workspaces.c.id).order_by(workspaces.c.created_at).limit(1))
        return rows[0]["id"] if rows else None
    return resolve
