"""Which database the app uses, and changing it from Settings without surprises.

Order of precedence:
1. DATABASE_URL in .env (an infrastructure choice; the app shows it but can't override it).
2. The active saved connection: DATA_DIR/database.json holds named connection profiles ("Company", "Dev"…),
   each URL encrypted with the settings key, and which one is active.
3. The local SQLite file in DATA_DIR.

Two ways to change it:
- Switch database (live, `/api/connection/*` in app/server.py): connect to another saved or new database now.
  Everyone is signed out; the new database's own accounts and workspaces apply. Nothing is copied, merged or
  deleted in either database.
- Settings → Database → Replace (below): copy this install's data into an empty database, used from the next start.

A saved connection never takes effect while the app runs: it is used from the next start, so the app never
switches databases under a running interview. Saving tests the connection first, refuses to replace an existing
saved connection unless asked, and either copies this install's data into an empty database or, if the target
already holds Recruence data, requires the admin to confirm they want to use that data instead. The schema is created
and upgraded on the next start (app/store.py migrations), against whichever database is in use.

Connection details never leave the server: the API returns only "PostgreSQL at host:port/db as user".
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Optional

import sqlalchemy as sa

from app.models import utcnow
from app.secret_box import SecretBox, SecretError
from app.store import POSTGRES_CONNECT_ARGS, Store, database_owner, database_url, describe, installations, metadata, schema_version

FILE = "database.json"
TIMEOUT_SECONDS = 8
BATCH = 500


class DatabaseConfigError(ValueError):
    pass


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE


LOCAL = "local"  # the built-in profile: the SQLite file in DATA_DIR


def _load(data_dir: Path) -> dict:
    """{"active": profile id, "profiles": [{id, name, url (encrypted), saved_at, saved_by}]}. Reads the older
    single-connection file ({"url": …}) as one profile."""
    path = _path(data_dir)
    if not path.exists():
        return {"active": LOCAL, "profiles": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise DatabaseConfigError(f"The saved database connections in {path} can't be read: {e}") from e
    if "profiles" not in data:
        return {"active": "saved", "profiles": [{"id": "saved", "name": "Saved database", "url": data["url"],
                                                  "saved_at": data.get("saved_at", ""), "saved_by": data.get("saved_by", "")}]}
    return data


def _write(data_dir: Path, data: dict) -> None:
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def profiles(data_dir: Path) -> list[dict]:
    """The saved connections for display: names and ids only, never the URL."""
    data = _load(data_dir)
    return [{"id": p["id"], "name": p["name"], "saved_at": p.get("saved_at", ""), "active": p["id"] == data["active"]}
            for p in data["profiles"]]


def active_profile(data_dir: Path) -> str:
    return _load(data_dir)["active"]


def profile_url(data_dir: Path, box: SecretBox, profile_id: str) -> str:
    for prof in _load(data_dir)["profiles"]:
        if prof["id"] == profile_id:
            try:
                return box.decrypt(prof["url"])
            except SecretError as e:
                raise DatabaseConfigError(f"The saved connection “{prof['name']}” can't be read: {e}") from e
    raise DatabaseConfigError("There's no saved connection with that id")


INSTALL_FILE = "installation.json"


def installation_id(data_dir: Path) -> str:
    """This installation's own random id (made once, kept in its data folder). A database records the installations
    that use it, and which one set it up (app/store.py database_owner)."""
    import os
    import secrets as _secrets
    given = os.environ.get("INSTALLATION_ID", "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{32}", given):
        return given  # a host without a persistent data folder (docs/CLOUD.md)
    path = Path(data_dir) / INSTALL_FILE
    try:
        found = json.loads(path.read_text(encoding="utf-8")).get("id", "")
        if re.fullmatch(r"[0-9a-f]{32}", found or ""):
            return found
    except (OSError, ValueError):
        pass
    new = _secrets.token_hex(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": new, "created_at": utcnow().isoformat()}), encoding="utf-8")
    return new


def installation_label(desktop: bool) -> str:
    import socket
    return f"{'Desktop app' if desktop else 'Server'} on {socket.gethostname()[:60] or 'an unnamed computer'}"


def connection_key(url: str) -> str:
    """What two connection strings must share to reach the same database without any credentials: backend, host
    (lower case), port (5432 when left out), database name and user. Parameters, passwords and spelling of the
    scheme don't count. A database's own id (app/store.py database_owner) decides when it can be read."""
    u = sa.make_url(normalise(url))
    return f"postgresql://{(u.username or '').strip()}@{(u.host or '').lower().rstrip('.')}:{u.port or 5432}/{u.database or ''}"


def save_profile(data_dir: Path, box: SecretBox, name: str, url: str, by: str = "", activate: bool = True,
                 database_id: str = "") -> str:
    """Add (or update) a saved connection. Returns its id. A database already saved (the same database id, or the
    same connection_key) is updated, not saved twice; otherwise a connection of the same name is replaced."""
    import secrets as _secrets
    name = " ".join(name.split())[:80] or "Database"
    data = _load(data_dir)
    key = connection_key(url) if str(url).startswith(("postgres://", "postgresql")) else ""
    prof = next((p for p in data["profiles"] if (database_id and p.get("database_id") == database_id)
                 or (key and p.get("key") == key)), None)
    if prof is None:
        prof = next((p for p in data["profiles"] if p["name"].lower() == name.lower()), None)
    if prof is None:
        prof = {"id": _secrets.token_hex(4), "name": name}
        data["profiles"].append(prof)
    prof.update(url=box.encrypt(url), saved_at=utcnow().isoformat(), saved_by=by, key=key)
    if database_id:
        prof["database_id"] = database_id
    if activate:
        data["active"] = prof["id"]
    _write(data_dir, data)
    return prof["id"]


def set_active(data_dir: Path, profile_id: str) -> None:
    data = _load(data_dir)
    if profile_id != LOCAL and not any(p["id"] == profile_id for p in data["profiles"]):
        raise DatabaseConfigError("There's no saved connection with that id")
    data["active"] = profile_id
    _write(data_dir, data)


def remove_profile(data_dir: Path, profile_id: str) -> None:
    data = _load(data_dir)
    if data["active"] == profile_id:
        raise DatabaseConfigError("This connection is in use. Connect to another database before removing it.")
    data["profiles"] = [p for p in data["profiles"] if p["id"] != profile_id]
    _write(data_dir, data)


def saved_url(data_dir: Path, box: SecretBox) -> Optional[str]:
    """The active saved connection's URL, or None when the local database is active."""
    data = _load(data_dir)
    if data["active"] == LOCAL:
        return None
    return profile_url(data_dir, box, data["active"])


def save(data_dir: Path, box: SecretBox, url: str, by: str = "") -> None:
    """Settings → Database → Replace: save as the active connection, used from the next start."""
    save_profile(data_dir, box, "Saved database", url, by)


def forget(data_dir: Path) -> bool:
    """Back to the local database (the saved connection is removed)."""
    data = _load(data_dir)
    if data["active"] == LOCAL:
        return False
    data["profiles"] = [p for p in data["profiles"] if p["id"] != data["active"]]
    data["active"] = LOCAL
    _write(data_dir, data)
    return True


def effective(env_url: str, local: Path, data_dir: Path, box: SecretBox) -> tuple[str | Path, str]:
    """(database to use, where that choice came from: "env", "saved" or "local")."""
    if env_url:
        return env_url, "env"
    url = saved_url(data_dir, box)
    if url:
        return url, "saved"
    return local, "local"


def normalise(url: str) -> str:
    """A PostgreSQL URL for SQLAlchemy with psycopg 3. Raises DatabaseConfigError for anything else."""
    url = database_url(url.strip()) if "://" in url else url.strip()
    try:
        parsed = sa.make_url(url)
    except sa.exc.ArgumentError as e:
        raise DatabaseConfigError("That isn't a database URL. Use postgresql://user:password@host:5432/database") from e
    if parsed.get_backend_name() != "postgresql":
        raise DatabaseConfigError("Only PostgreSQL can be connected from here (postgresql://…); the local option is SQLite")
    if not parsed.host or not parsed.database:
        raise DatabaseConfigError("The URL needs a host and a database name")
    return parsed.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _scrub(message: str, url: str) -> str:
    """A driver error without the password (some drivers echo the connection string)."""
    pw = sa.make_url(url).password
    if pw:
        message = message.replace(str(pw), "***")
    return re.sub(r"://[^@\s]+@", "://***@", message)[:300]


# An empty database is the normal start: Recruence creates its tables in it. A database with Recruence's tables is an existing
# installation, used as it is. Neither is ever dropped, truncated or overwritten (Store only creates what's missing
# and adds columns).
MIA_MARKERS = {"schema_version", "interviews", "sessions"}


def classify(tables: set[str], rows: dict[str, int], version: Optional[int] = None) -> dict:
    """What a connected database holds, for Recruence: "empty" and "no_mia" are ready for a new setup, "mia" is an existing
    installation, "conflict" has tables with Recruence's names that aren't Recruence's."""
    ours = {t.name for t in metadata.sorted_tables} & tables
    others = tables - {t.name for t in metadata.sorted_tables}
    if MIA_MARKERS <= tables:
        counts = ", ".join(f"{n} {k}" for k, n in rows.items() if n) or "no records yet"
        return {"state": "mia", "ready": True, "summary": (
            f"Existing Recruence installation found ({counts}{f'; schema version {version}' if version else ''}). "
            "Its data is kept as it is: nothing is dropped, cleared or overwritten, and missing tables or columns are added.")}
    if ours:
        return {"state": "conflict", "ready": False, "summary": (
            f"This database has tables named like Recruence's ({', '.join(sorted(ours)[:5])}) that aren't from a Recruence installation. "
            "Recruence won't touch them: use an empty database (or a separate one for Recruence).")}
    if others:
        return {"state": "no_mia", "ready": True, "summary": (
            f"Ready for a new Recruence setup. The database has {len(others)} other table(s), which Recruence leaves untouched; "
            "it creates its own tables when you confirm setup.")}
    return {"state": "empty", "ready": True, "summary": "Empty database: ready for a new Recruence setup. Recruence creates its tables when you confirm setup."}


def test(url: str) -> dict:
    """Connect, check the server and permissions, and say what the database holds (classify). Never raises.
    Read-only: the permission check creates a table inside a transaction that is rolled back."""
    try:
        url = normalise(url)
    except DatabaseConfigError as e:
        return {"ok": False, "message": str(e)}
    try:
        engine = sa.create_engine(url, connect_args={**POSTGRES_CONNECT_ARGS, "connect_timeout": TIMEOUT_SECONDS}, pool_pre_ping=True)
    except ModuleNotFoundError:
        return {"ok": False, "message": "The PostgreSQL driver isn't installed: pip install -r requirements-postgres.txt"}
    try:
        with engine.connect() as c:
            server_version = c.execute(sa.text("SHOW server_version")).scalar()
            tables = set(sa.inspect(c).get_table_names())
            rows = {}
            for name in ("roles", "interviews", "applications"):
                if name in tables:
                    rows[name] = c.execute(sa.text(f'SELECT count(*) FROM "{name}"')).scalar()
            version = None
            if "schema_version" in tables:
                version = c.execute(sa.text("SELECT max(version) FROM schema_version")).scalar()
            database_id, owned = "", False
            if "database_owner" in tables:
                row = c.execute(sa.text("SELECT database_id, owner_user_id FROM database_owner WHERE id = 1")).first()
                if row:
                    database_id, owned = row[0] or "", bool(row[1])
            c.rollback()  # end the read transaction the queries above started, so the check below gets its own
            # Can the app create its tables? Postgres DDL is transactional: the table is rolled back, never dropped,
            # and its random name can't be an existing table.
            check = c.begin()
            try:
                c.execute(sa.text(f"CREATE TABLE mia_permission_check_{secrets.token_hex(6)} (id integer)"))
            finally:
                check.rollback()
    except sa.exc.OperationalError as e:
        message = f"Couldn't connect: {_scrub(str(e.orig), url)}"
        host = sa.make_url(url).host or ""
        if host.startswith("db.") and host.endswith(".supabase.co") and "resolve" in message:
            # Supabase's direct connection has only an IPv6 address; many networks are IPv4-only.
            message += (" Supabase's direct connection works over IPv6 only. Use the Session pooler string instead: "
                        "Supabase → Project → Connect → Session pooler (host …pooler.supabase.com, user postgres.<project-ref>).")
        return {"ok": False, "message": message}
    except sa.exc.ProgrammingError as e:
        return {"ok": False, "message": f"Connected, but this user can't create tables: {_scrub(str(e.orig), url)}"}
    except Exception as e:  # noqa: BLE001 - any other driver error means the connection can't be used
        return {"ok": False, "message": f"Couldn't use the database: {type(e).__name__}: {_scrub(str(e), url)}"}
    finally:
        engine.dispose()
    found = classify(tables, rows, version)
    if not found["ready"]:
        return {"ok": False, "message": f"Connected to {describe(url)}, but: {found['summary']}", "state": found["state"]}
    return {
        "ok": True,
        "message": f"Connected to {describe(url)} (PostgreSQL {server_version}).",
        "database": describe(url),
        "state": found["state"],  # empty | no_mia | mia
        "summary": found["summary"],
        "has_mia_tables": found["state"] == "mia",
        "has_data": any(rows.values()),
        "rows": rows,
        "schema_version": version,
        "database_id": database_id,  # the database's own id (no secret): equal ids are one database
        "owned": owned,  # already belongs to an account: its own accounts sign in; others need a joining code
    }


def copy_all(source: str | Path, target: str | Path) -> dict[str, int]:
    """Copy every table from source into target (which must hold no data); both are brought to the current schema."""
    src, dst = Store(source), Store(target)
    # The target keeps its own identity (it's another database) and its own list of installations; ownership moves
    # with the accounts (below).
    tables = [t for t in metadata.sorted_tables if t not in (schema_version, database_owner, installations)]  # parents first
    try:
        with dst._engine.connect() as c:
            if any(c.execute(sa.select(sa.func.count()).select_from(t)).scalar() for t in tables):
                raise DatabaseConfigError(f"{describe(target)} already holds data: copy into an empty database")
        counts = {}
        with src._engine.connect() as s, dst._engine.begin() as d:
            for table in tables:
                rows = [dict(r._mapping) for r in s.execute(sa.select(table))]
                for i in range(0, len(rows), BATCH):
                    d.execute(table.insert(), rows[i:i + BATCH])
                counts[table.name] = len(rows)
            owner = src.database_identity()
            d.execute(database_owner.update().where(database_owner.c.id == 1).values(
                owner_user_id=owner.get("owner_user_id"), owner_install_id=owner.get("owner_install_id"), claimed_at=owner.get("claimed_at")))
            if dst.backend == "postgresql" and counts.get("audit_events"):
                # Ids were copied as they were; move the sequence past them so new entries don't collide.
                d.execute(sa.text("SELECT setval(pg_get_serial_sequence('audit_events', 'id'), (SELECT MAX(id) FROM audit_events))"))
        return counts
    finally:
        src.close()
        dst.close()
