"""Persistence: interviews, sessions, transcripts, notes, reports, an audit log, and the people and workspaces.

SQLite by default (one file under DATA_DIR, nothing to run), or PostgreSQL through DATABASE_URL.
SQLAlchemy Core keeps the SQL the same on both. Existing databases are upgraded on start by the
numbered steps in `MIGRATIONS`; a new database is created at the latest version.

Candidate data lives in `interviews` (resume, JD), `utterances`, `notes` and `reports`.
`audit_events` never stores candidate content, only actions and ids.

Workspaces: roles, interviews, applications and audit entries carry a `workspace_id`. While a request runs
for a workspace (`CURRENT_WORKSPACE`, set by the access guard), every read and write here is limited to it,
so one workspace's records can't be read or changed from another, whatever the endpoint. Outside a request
(the scheduler, webhooks, candidate pages behind their own tokens) nothing is limited. Rows from before
workspaces existed have no workspace and belong to the first workspace created (`claim_unassigned`).
"""

from __future__ import annotations

import json
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app import lifecycle
from app.models import (
    Application,
    ApplicationInput,
    AnswerNote,
    EvaluationReport,
    Interview,
    REPORT_DERIVED_FIELDS,
    InterviewSetup,
    Role,
    RoleSetup,
    Utterance,
    utcnow,
)

# Timestamps are ISO 8601 text in UTC, as in the original SQLite schema: they sort and compare as text.
metadata = sa.MetaData()
roles = sa.Table(
    "roles", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Column("data", sa.Text, nullable=False),
    sa.Column("workspace_id", sa.Text),
    sa.Index("ix_roles_workspace", "workspace_id"),
)
interviews = sa.Table(
    "interviews", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("scheduled_at", sa.Text),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Column("role_id", sa.Text),  # copied from the JSON so role queries need no JSON functions
    sa.Column("data", sa.Text, nullable=False),
    sa.Column("workspace_id", sa.Text),
    sa.Index("ix_interviews_role_id", "role_id"),
    sa.Index("ix_interviews_workspace", "workspace_id"),
)
sessions = sa.Table(
    "sessions", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("interview_id", sa.Text, sa.ForeignKey("interviews.id", ondelete="CASCADE"), nullable=False),
    sa.Column("platform", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("bot_id", sa.Text),
    sa.Column("agent_config_id", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("ended_at", sa.Text),
    sa.Column("end_reason", sa.Text),
    # The installation running it (app/dbconfig.py installation_id): on a database several installations share, each
    # cleans up only its own sessions when it restarts. Empty for sessions from before this was recorded.
    sa.Column("install_id", sa.Text),
    sa.Index("idx_sessions_interview", "interview_id"),
)
# A booked interview's start, claimed by one installation: on a shared database every installation's scheduler sees
# the same booked interviews, and only the one that claims it first starts it.
launch_claims = sa.Table(
    "launch_claims", metadata,
    sa.Column("interview_id", sa.Text, primary_key=True),
    sa.Column("install_id", sa.Text, nullable=False),
    sa.Column("claimed_at", sa.Text, nullable=False),
)
utterances = sa.Table(
    "utterances", metadata,
    sa.Column("session_id", sa.Text, sa.ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("seq", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("ts", sa.Text, nullable=False),
    sa.Column("speaker", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("question_id", sa.Text),
)
notes = sa.Table(
    "notes", metadata,
    sa.Column("session_id", sa.Text, sa.ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("question_id", sa.Text, primary_key=True),
    sa.Column("note", sa.Text, nullable=False),
    sa.Column("follow_ups", sa.Integer, nullable=False),
    sa.Column("position", sa.Integer, nullable=False, server_default="0"),  # the order notes were first written
)
reports = sa.Table(
    "reports", metadata,
    sa.Column("session_id", sa.Text, sa.ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("recommendation", sa.Text),  # copied from the JSON for the dashboard list
    sa.Column("data", sa.Text, nullable=False),
)
audit_events = sa.Table(
    "audit_events", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("interview_id", sa.Text),
    sa.Column("session_id", sa.Text),
    sa.Column("detail", sa.Text, nullable=False, server_default=""),
    sa.Column("actor", sa.Text, nullable=False, server_default=""),
    sa.Column("workspace_id", sa.Text),
    sqlite_autoincrement=True,
)
# The public booking address's token -> its interview (the booking itself lives in the interview's JSON).
bookings = sa.Table(
    "bookings", metadata,
    sa.Column("token", sa.Text, primary_key=True),
    sa.Column("interview_id", sa.Text, sa.ForeignKey("interviews.id", ondelete="CASCADE"), nullable=False),
    sa.Index("ix_bookings_interview", "interview_id"),
)
# A candidate's own interview link token -> its interview (the invitation itself lives in the interview's JSON).
invite_tokens = sa.Table(
    "invite_tokens", metadata,
    sa.Column("token", sa.Text, primary_key=True),
    sa.Column("interview_id", sa.Text, sa.ForeignKey("interviews.id", ondelete="CASCADE"), nullable=False),
    sa.Index("ix_invite_tokens_interview", "interview_id"),
)
# Candidate interview links, by SHA-256 of the token (the token itself is never stored in clear; app/gateway.py).
invitation_links = sa.Table(
    "invitation_links", metadata,
    sa.Column("token_hash", sa.Text, primary_key=True),
    sa.Column("interview_id", sa.Text, sa.ForeignKey("interviews.id", ondelete="CASCADE"), nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Index("ix_invitation_links_interview", "interview_id"),
)
# Where an interview takes place (app/meetings.py): one per interview, made by hand or (later) through a provider.
# The interview's meeting_link is what the bot joins; this is its record: who set it, when, and the provider's id.
meetings = sa.Table(
    "meetings", metadata,
    sa.Column("interview_id", sa.Text, sa.ForeignKey("interviews.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("workspace_id", sa.Text),
    sa.Column("mode", sa.Text, nullable=False),  # manual | provider
    sa.Column("provider", sa.Text, nullable=False),  # manual, or google / microsoft / zoom later
    sa.Column("platform", sa.Text, nullable=False),
    sa.Column("join_url_enc", sa.Text, nullable=False),  # encrypted (app/secret_box.py)
    sa.Column("external_id", sa.Text, nullable=False, server_default=""),
    sa.Column("starts_at", sa.Text),
    sa.Column("ends_at", sa.Text),
    sa.Column("status", sa.Text, nullable=False, server_default="scheduled"),  # scheduled | cancelled | ended
    sa.Column("created_by", sa.Text, nullable=False, server_default=""),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
)
# The one-time code a candidate is emailed before their interview opens: only its hash, for a few minutes.
gateway_codes = sa.Table(
    "gateway_codes", metadata,
    sa.Column("token_hash", sa.Text, primary_key=True),
    sa.Column("code_hash", sa.Text, nullable=False),
    sa.Column("expires_at", sa.Text, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("sends", sa.Integer, nullable=False, server_default="1"),
    sa.Column("first_sent_at", sa.Text, nullable=False),
)
# The device that verified a link: one active per link. The cookie's value is never stored, only its hash.
gateway_sessions = sa.Table(
    "gateway_sessions", metadata,
    sa.Column("session_hash", sa.Text, primary_key=True),
    sa.Column("token_hash", sa.Text, nullable=False),
    sa.Column("interview_id", sa.Text, sa.ForeignKey("interviews.id", ondelete="CASCADE"), nullable=False),
    sa.Column("verified_at", sa.Text, nullable=False),
    sa.Column("last_seen", sa.Text, nullable=False),
    sa.Column("ended_at", sa.Text),
    sa.Index("ix_gateway_sessions_token", "token_hash"),
)
# Applications through a role's public link. email is lower-cased and copied out of the JSON to refuse duplicates.
applications = sa.Table(
    "applications", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("role_id", sa.Text, sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("interview_id", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Column("data", sa.Text, nullable=False),
    sa.Column("workspace_id", sa.Text),
    # "<integration id>:<the ATS's application id>" for applications from an external ATS: a retried webhook
    # finds the same application instead of making another.
    sa.Column("external_key", sa.Text),
    sa.UniqueConstraint("role_id", "email", name="uq_applications_role_email"),
    sa.Index("ix_applications_role", "role_id"),
    sa.Index("ix_applications_external", "external_key"),
)
# External ATS connections (app/ats): one per provider account and workspace. Credentials are encrypted in `data`.
integrations = sa.Table(
    "integrations", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("workspace_id", sa.Text),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("inbound_token", sa.Text, nullable=False, unique=True),  # the secret part of its webhook address
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("data", sa.Text, nullable=False),  # JSON: name, settings, encrypted credentials, health
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
)
# What each integration did: webhooks received, results sent, with failures and retries. No candidate content.
integration_events = sa.Table(
    "integration_events", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("integration_id", sa.Text, sa.ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False),
    sa.Column("ts", sa.Text, nullable=False),
    sa.Column("direction", sa.Text, nullable=False),  # in, out
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),  # ok, failed, pending, duplicate, ignored
    sa.Column("dedupe_key", sa.Text, unique=True),  # the provider's event id, or the result being sent
    sa.Column("application_id", sa.Text),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
    sa.Column("message", sa.Text, nullable=False, server_default=""),
    sa.Column("payload", sa.Text),  # an outbound result waiting to be retried (JSON)
    sa.Index("ix_integration_events_integration", "integration_id"),
    sqlite_autoincrement=True,
)
# Small app-wide state the app keeps for itself, as JSON per key (e.g. the last calendar sync).
app_state = sa.Table(
    "app_state", metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("value", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
)
# Settings changed from the Settings page, layered over .env. Secrets are stored encrypted (app/secret_box.py).
app_settings = sa.Table(
    "app_settings", metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("value", sa.Text, nullable=False),  # JSON, or Fernet ciphertext when secret
    sa.Column("secret", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Column("updated_by", sa.Text, nullable=False, server_default=""),
)
# People who sign in (app/accounts.py). Accounts live in the connected database: another database has its own.
users = sa.Table(
    "users", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("email", sa.Text, nullable=False, unique=True),  # lower-cased
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("is_admin", sa.Boolean, nullable=False, server_default=sa.false()),  # runs this installation
    sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("last_workspace_id", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("password_changed_at", sa.Text, nullable=False),
)
# Who this database belongs to: one row (id 1), made when the schema is prepared. database_id names the database
# itself, whatever connection string reaches it (a pooler or direct host, other parameters). The first account claims
# it atomically (owner_user_id from NULL, in the same transaction as creating that account), with the installation
# it was made from: accounts from other installations then need a joining code from this database's admins.
# The applicant's original resume file (audit M1: only its text used to be kept). Removed with its application.
resume_files = sa.Table(
    "resume_files", metadata,
    sa.Column("application_id", sa.Text, sa.ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("workspace_id", sa.Text),
    sa.Column("filename", sa.Text, nullable=False),
    sa.Column("content_type", sa.Text, nullable=False),
    sa.Column("size", sa.Integer, nullable=False),
    sa.Column("data", sa.LargeBinary, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
)
database_owner = sa.Table(
    "database_owner", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),  # always 1: one owner per database
    sa.Column("database_id", sa.Text, nullable=False, unique=True),
    sa.Column("owner_user_id", sa.Text),
    sa.Column("owner_install_id", sa.Text),
    sa.Column("claimed_at", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
)
# Every Recruence installation that has used this database (app/dbconfig.py installation_id), for its admins to review.
installations = sa.Table(
    "installations", metadata,
    sa.Column("install_id", sa.Text, primary_key=True),
    sa.Column("label", sa.Text, nullable=False, server_default=""),
    sa.Column("first_seen", sa.Text, nullable=False),
    sa.Column("last_seen", sa.Text, nullable=False),
)
# Signed-in browsers: only a SHA-256 of the cookie's random value is kept.
auth_sessions = sa.Table(
    "auth_sessions", metadata,
    sa.Column("token_hash", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("workspace_id", sa.Text),  # the workspace this browser is in
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("expires_at", sa.Text, nullable=False),
    sa.Index("ix_auth_sessions_user", "user_id"),
)
password_resets = sa.Table(
    "password_resets", metadata,
    sa.Column("token_hash", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("expires_at", sa.Text, nullable=False),
)
workspaces = sa.Table(
    "workspaces", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, server_default=""),
    sa.Column("owner_id", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("join_code", sa.Text, unique=True),  # None: joining by code is off
    sa.Column("join_code_expires_at", sa.Text),
    sa.Column("settings", sa.Text, nullable=False, server_default="{}"),  # JSON
)
memberships = sa.Table(
    "memberships", metadata,
    sa.Column("workspace_id", sa.Text, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("role", sa.Text, nullable=False),  # owner, admin, recruiter
    sa.Column("status", sa.Text, nullable=False),  # pending, approved, rejected
    sa.Column("requested_at", sa.Text, nullable=False),
    sa.Column("decided_at", sa.Text),
    sa.Column("decided_by", sa.Text),
    sa.Index("ix_memberships_user", "user_id"),
)
schema_version = sa.Table("schema_version", metadata, sa.Column("version", sa.Integer, nullable=False))

TERMINAL_SESSION_STATES = ("completed", "failed")


class DuplicateApplication(ValueError):
    pass
# Who is acting, for the audit log: set per request by the access guard (app/security.py). Empty for the app itself.
CURRENT_ACTOR: ContextVar[str] = ContextVar("current_actor", default="")
# The workspace a request runs in, set by the access guard. None: not limited (the app itself, candidate pages).
# "" (no workspace): a signed-in person who belongs to none sees nothing.
CURRENT_WORKSPACE: ContextVar[Optional[str]] = ContextVar("current_workspace", default=None)
WORKSPACE_TABLES = ("roles", "interviews", "applications", "audit_events")


# PostgreSQL connections, for every provider (Neon, Supabase, Amazon RDS / Aurora, your own):
# - connect_timeout: a server that is down fails within seconds rather than hanging the app (or its start).
# - prepare_threshold None: no server-side prepared statements. psycopg prepares a query after 5 runs, which breaks
#   behind poolers in transaction mode (PgBouncer, Supabase port 6543: "prepared statement already exists") and pins
#   every session behind Amazon RDS Proxy. The queries here are small; the cost is negligible.
POSTGRES_CONNECT_ARGS = {"connect_timeout": 10, "prepare_threshold": None}


class NoWorkspace(PermissionError):
    """A write that needs a workspace, from someone who isn't in one."""


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def database_url(target: Path | str) -> str:
    """A SQLAlchemy URL for DATABASE_URL or a SQLite file path. postgres:// and postgresql:// use psycopg 3."""
    text = str(target)
    if "://" not in text:
        return "sqlite://" if text == ":memory:" else f"sqlite:///{Path(text).as_posix()}"
    for prefix in ("postgres://", "postgresql://"):
        if text.startswith(prefix):
            return "postgresql+psycopg://" + text[len(prefix):]
    return text


def describe(target: Path | str) -> str:
    """Which database, for the Settings page: never the password."""
    url = sa.make_url(database_url(target))
    if url.get_backend_name() == "sqlite":
        return f"SQLite, {url.database or 'in memory'}"
    where = f"{url.host or 'localhost'}{f':{url.port}' if url.port else ''}/{url.database or ''}"
    return f"PostgreSQL at {where}" + (f" as {url.username}" if url.username else "")


# ---------- migrations: upgrade an existing database, one numbered step at a time ----------


def _columns(c: Connection, table: str) -> set[str]:
    insp = sa.inspect(c)
    return {col["name"] for col in insp.get_columns(table)} if insp.has_table(table) else set()


def _copy_from_json(c: Connection, table: sa.Table, key: str, column: str, pick: Callable[[dict], Any]) -> None:
    for row in c.execute(sa.select(table.c[key], table.c.data)).all():
        value = pick(json.loads(row.data))
        if value is not None:
            c.execute(table.update().where(table.c[key] == row[0]).values({column: value}))


def _migrate_1(c: Connection) -> None:
    """From the first SQLite schema: audit actor, role and recommendation columns, note order."""
    if (cols := _columns(c, "audit_events")) and "actor" not in cols:
        c.execute(sa.text("ALTER TABLE audit_events ADD COLUMN actor TEXT NOT NULL DEFAULT ''"))
    if (cols := _columns(c, "interviews")) and "role_id" not in cols:
        c.execute(sa.text("ALTER TABLE interviews ADD COLUMN role_id TEXT"))
        _copy_from_json(c, interviews, "id", "role_id", lambda d: d.get("role_id"))
        c.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_interviews_role_id ON interviews (role_id)"))
    if (cols := _columns(c, "reports")) and "recommendation" not in cols:
        c.execute(sa.text("ALTER TABLE reports ADD COLUMN recommendation TEXT"))
        _copy_from_json(c, reports, "session_id", "recommendation", lambda d: (d.get("evaluation") or {}).get("recommendation"))
    if (cols := _columns(c, "notes")) and "position" not in cols:
        c.execute(sa.text("ALTER TABLE notes ADD COLUMN position INTEGER NOT NULL DEFAULT 0"))
        if c.dialect.name == "sqlite":  # databases before version 1 were always SQLite
            c.execute(sa.text("UPDATE notes SET position = rowid"))


def _migrate_2(c: Connection) -> None:
    """Workspaces: a workspace column on everything a workspace owns. Existing rows get none until claimed."""
    for table in WORKSPACE_TABLES:
        if (cols := _columns(c, table)) and "workspace_id" not in cols:
            c.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT"))
    for table, index in (("roles", "ix_roles_workspace"), ("interviews", "ix_interviews_workspace")):
        if _columns(c, table):
            c.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {index} ON {table} (workspace_id)"))


def _migrate_3(c: Connection) -> None:
    """External ATS integrations: the application's external key (the integration tables are new)."""
    if (cols := _columns(c, "applications")) and "external_key" not in cols:
        c.execute(sa.text("ALTER TABLE applications ADD COLUMN external_key TEXT"))
        c.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_applications_external ON applications (external_key)"))


def _migrate_4(c: Connection) -> None:
    """Database ownership: the database_owner and installations tables are new (created below); nothing existing
    changes. The owner row itself is made in Store._prepare, for new and existing databases alike."""


def _migrate_5(c: Connection) -> None:
    """Interview access gateway: invitation_links, gateway_codes and gateway_sessions are new (created below). Older
    plain-text links (invite_tokens) are moved to hashes by the app at start (app/gateway.py secure_old_links)."""


def _migrate_6(c: Connection) -> None:
    """Meetings: the meetings table is new (created below); interviews keep their meeting_link as before."""


def _migrate_7(c: Connection) -> None:
    """Shared databases: sessions record their installation; launch_claims is new (created below)."""
    if (cols := _columns(c, "sessions")) and "install_id" not in cols:
        c.execute(sa.text("ALTER TABLE sessions ADD COLUMN install_id TEXT"))


def _migrate_8(c: Connection) -> None:
    """Original resume files: the resume_files table is new (created below); older applications keep their text only."""


MIGRATIONS: list[tuple[int, Callable[[Connection], None]]] = [(1, _migrate_1), (2, _migrate_2), (3, _migrate_3), (4, _migrate_4),
                                                              (5, _migrate_5), (6, _migrate_6), (7, _migrate_7), (8, _migrate_8)]
SCHEMA_VERSION = MIGRATIONS[-1][0]


class Store:
    def __init__(self, target: Path | str):
        """`target`: a SQLite file path, ":memory:", or a database URL (DATABASE_URL)."""
        # Read-then-write steps (the next transcript line number) run one at a time in this process.
        self._lock = threading.RLock()
        self._engine, self.backend, self._insert = self._open(target)
        self._single = self._one_statement_engine(self._engine, self.backend)
        self.target = target

    @staticmethod
    def _one_statement_engine(engine: sa.Engine, backend: str) -> sa.Engine:
        """For one-statement reads and writes (_rows, _run). On PostgreSQL they run in autocommit: one round trip
        instead of three (BEGIN, the statement, COMMIT), which is most of the wait on a hosted database far away
        (Neon, Supabase). A single statement is atomic either way; multi-statement steps keep their transactions."""
        return engine.execution_options(isolation_level="AUTOCOMMIT") if backend == "postgresql" else engine

    @staticmethod
    def _open(target: Path | str) -> tuple[sa.Engine, str, Any]:
        url = sa.make_url(database_url(target))
        backend = url.get_backend_name()
        if backend == "sqlite":
            if url.database and url.database != ":memory:":
                Path(url.database).parent.mkdir(parents=True, exist_ok=True)
                engine = sa.create_engine(url, connect_args={"check_same_thread": False})
            else:
                engine = sa.create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
            event.listen(engine, "connect", Store._sqlite_pragmas)
        elif backend == "postgresql":
            # No ping before each use (another round trip): connections are renewed after 4 minutes, before a hosted
            # database closes idle ones (Neon suspends after 5), and a dropped one is retried once (_once).
            engine = sa.create_engine(url, pool_pre_ping=False, pool_recycle=240, connect_args=POSTGRES_CONNECT_ARGS)
        else:
            raise ValueError(f"Unsupported database {backend!r}: use SQLite (the default) or PostgreSQL")
        try:
            Store._prepare(engine)
        except Exception:
            engine.dispose()
            raise
        return engine, backend, pg_insert if backend == "postgresql" else sqlite_insert

    def reconnect(self, target: Path | str) -> None:
        """Use another database from now on, in place, so everything holding this store follows. The new one is
        opened and brought to the current schema (additive steps only) before anything switches; the old pool is
        closed afterwards. Callers make sure no interview is running (app/server.py)."""
        engine, backend, insert = self._open(target)
        with self._lock:
            old = self._engine
            self._engine, self.backend, self._insert, self.target = engine, backend, insert, target
            self._single = self._one_statement_engine(engine, backend)
        old.dispose()

    @staticmethod
    def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute("PRAGMA journal_mode = WAL")  # ignored for in-memory databases
        cur.close()

    @staticmethod
    def _prepare(engine: sa.Engine) -> None:
        with engine.begin() as c:
            insp = sa.inspect(c)
            if insp.has_table("schema_version"):
                version = c.execute(sa.select(sa.func.max(schema_version.c.version))).scalar() or 0
                if version > SCHEMA_VERSION:
                    raise ValueError(f"This database was set up by a newer version of Recruence (schema {version}, this one knows "
                                     f"{SCHEMA_VERSION}); update the app before using it")
            else:
                # No version table: a new database, or one created before versioning (always SQLite).
                version = 0 if any(insp.has_table(t) for t in ("interviews", "audit_events", "roles")) else SCHEMA_VERSION
            for number, step in MIGRATIONS:
                if number > version:
                    step(c)
            metadata.create_all(c)  # tables (and their indexes) the database doesn't have yet
            Store._ensure_owner_row(c)
            if c.execute(sa.select(sa.func.max(schema_version.c.version))).scalar() != SCHEMA_VERSION:
                c.execute(schema_version.delete())
                c.execute(schema_version.insert().values(version=SCHEMA_VERSION))

    @staticmethod
    def _ensure_owner_row(c: Connection) -> None:
        """The database's identity row, made once. A database that already has accounts (from before ownership was
        recorded) is owned by its earliest administrator: recorded, nothing else changes."""
        if c.execute(sa.select(database_owner.c.id).where(database_owner.c.id == 1)).first() is not None:
            return
        owner = c.execute(sa.select(users.c.id).where(users.c.is_admin.is_(True)).order_by(users.c.created_at).limit(1)).scalar()
        now = utcnow().isoformat()
        try:
            with c.begin_nested():
                c.execute(database_owner.insert().values(id=1, database_id=uuid.uuid4().hex, owner_user_id=owner,
                                                         claimed_at=now if owner else None, created_at=now))
        except sa.exc.IntegrityError:
            pass  # another app instance prepared it at the same moment: theirs stands

    def close(self) -> None:
        self._engine.dispose()

    # ---------- database identity and ownership ----------

    def database_identity(self) -> dict:
        rows = self._rows(sa.select(database_owner).where(database_owner.c.id == 1))
        return rows[0] if rows else {}

    def record_installation(self, install_id: str, label: str = "") -> None:
        """This installation uses this database (inserted once, then its last-seen time moves on)."""
        now = utcnow().isoformat()
        stmt = self._insert(installations).values(install_id=install_id, label=label[:120], first_seen=now, last_seen=now)
        self._run(stmt.on_conflict_do_update(index_elements=["install_id"], set_={"last_seen": now, "label": stmt.excluded.label}))

    def installations(self) -> list[dict]:
        return self._rows(sa.select(installations).order_by(installations.c.first_seen))

    def has_data(self) -> bool:
        """Anything a person made here: roles, interviews or saved settings (first-run detection, app/setup.py)."""
        def real(table) -> sa.Select:  # demo records (app/demo.py) don't make an install "existing"
            return sa.select(sa.func.count()).select_from(table).where(
                sa.not_(sa.and_(table.c.id.like("de%"), table.c.data.like('%"demo":true%'))))
        with self._engine.connect() as c:
            return bool(c.execute(real(roles)).scalar() or c.execute(real(interviews)).scalar()
                        or c.execute(sa.select(sa.func.count()).select_from(app_settings)).scalar())

    def ping(self) -> int:
        """The schema version, after a round trip to the database."""
        with self._engine.connect() as c:
            return c.execute(sa.select(sa.func.max(schema_version.c.version))).scalar() or 0

    # ---------- workspace scoping ----------

    @staticmethod
    def _scope(stmt: Any, table: sa.Table) -> Any:
        """Limit a statement to the request's workspace, if it runs in one."""
        ws = CURRENT_WORKSPACE.get()
        return stmt if ws is None else stmt.where(table.c.workspace_id == ws)

    @staticmethod
    def _workspace_for_write() -> Optional[str]:
        ws = CURRENT_WORKSPACE.get()
        if ws == "":
            raise NoWorkspace("Join or create a workspace first")
        return ws

    def role_workspace(self, role_id: str) -> Optional[str]:
        with self._engine.connect() as c:
            return c.execute(sa.select(roles.c.workspace_id).where(roles.c.id == role_id)).scalar()

    def interview_workspace(self, interview_id: str) -> Optional[str]:
        with self._engine.connect() as c:
            return c.execute(sa.select(interviews.c.workspace_id).where(interviews.c.id == interview_id)).scalar()

    def _session_visible(self, session_id: str) -> bool:
        if CURRENT_WORKSPACE.get() is None:
            return True
        stmt = self._scope(sa.select(sa.func.count()).select_from(sessions.join(interviews, sessions.c.interview_id == interviews.c.id))
                           .where(sessions.c.id == session_id), interviews)
        with self._engine.connect() as c:
            return bool(c.execute(stmt).scalar())

    def claim_unassigned(self, workspace_id: str) -> None:
        """Records from before workspaces (no workspace yet) join this one: the first workspace in the database."""
        with self._engine.begin() as c:
            for table in (roles, interviews, applications, audit_events):
                c.execute(table.update().where(table.c.workspace_id.is_(None)).values(workspace_id=workspace_id))

    def _once(self, work: Callable[[sa.Connection], Any]) -> Any:
        """One statement on a pooled connection; if the database had dropped that connection (idle, restarted),
        once more on a fresh one."""
        try:
            with self._single.connect() as c:
                return work(c)
        except sa.exc.DBAPIError as e:
            if not e.connection_invalidated:
                raise
            with self._single.connect() as c:
                return work(c)

    def _rows(self, stmt: Any) -> list[dict]:
        if self.backend != "postgresql":
            with self._engine.begin() as c:
                return [dict(r._mapping) for r in c.execute(stmt)]
        return self._once(lambda c: [dict(r._mapping) for r in c.execute(stmt)])

    def _run(self, stmt: Any) -> int:
        if self.backend != "postgresql":
            with self._engine.begin() as c:
                return c.execute(stmt).rowcount
        return self._once(lambda c: c.execute(stmt).rowcount)

    # ---------- roles ----------

    def create_role(self, setup: RoleSetup) -> Role:
        now = utcnow()
        role = Role(id=uuid.uuid4().hex[:12], created_at=now, updated_at=now, setup=setup)
        self._run(roles.insert().values(id=role.id, updated_at=_iso(now), data=role.model_dump_json(), workspace_id=self._workspace_for_write()))
        return role

    def save_role(self, role: Role) -> Role:
        role = role.model_copy(update={"updated_at": utcnow()})
        self._run(self._scope(roles.update().where(roles.c.id == role.id), roles).values(updated_at=_iso(role.updated_at), data=role.model_dump_json()))
        return role

    def rewrite_role(self, role: Role) -> None:
        """Rewrite a role's stored data as it is, keeping its updated_at (one-time data fixes at start, not edits)."""
        self._run(roles.update().where(roles.c.id == role.id).values(data=role.model_dump_json()))

    def get_role(self, role_id: str) -> Optional[Role]:
        rows = self._rows(self._scope(sa.select(roles.c.data).where(roles.c.id == role_id), roles))
        return Role.model_validate_json(rows[0]["data"]) if rows else None

    def list_roles(self) -> list[Role]:
        return [Role.model_validate_json(r["data"]) for r in self._rows(self._scope(sa.select(roles.c.data).order_by(roles.c.updated_at.desc()), roles))]

    def delete_role(self, role_id: str) -> None:
        self._run(self._scope(roles.delete().where(roles.c.id == role_id), roles))

    def role_candidate_counts(self) -> dict[str, int]:
        stmt = (
            sa.select(interviews.c.role_id, sa.func.count().label("n"))
            .where(interviews.c.role_id.is_not(None))
            .group_by(interviews.c.role_id)
        )
        return {r["role_id"]: r["n"] for r in self._rows(self._scope(stmt, interviews))}

    def interviews_for_role(self, role_id: str) -> list[Interview]:
        """The role's candidate interviews, oldest first (the order candidates were added)."""
        rows = self._rows(self._scope(sa.select(interviews.c.data).where(interviews.c.role_id == role_id), interviews))
        return sorted((Interview.model_validate_json(r["data"]) for r in rows), key=lambda i: i.created_at)

    # ---------- interviews ----------

    def create_interview(self, setup: InterviewSetup, **fields: Any) -> Interview:
        now = utcnow()
        interview = Interview(id=uuid.uuid4().hex[:12], created_at=now, updated_at=now, setup=setup, **fields)
        # A role's candidate is in the role's workspace (also for applications through the public link).
        ws = self.role_workspace(interview.role_id) if interview.role_id else self._workspace_for_write()
        self._run(interviews.insert().values(id=interview.id, workspace_id=ws, **self._interview_columns(interview)))
        return interview

    def save_interview(self, interview: Interview) -> Interview:
        interview = interview.model_copy(update={"updated_at": utcnow()})
        self._run(self._scope(interviews.update().where(interviews.c.id == interview.id), interviews).values(**self._interview_columns(interview)))
        return interview

    @staticmethod
    def _interview_columns(i: Interview) -> dict:
        return {"status": i.status, "scheduled_at": _iso(i.scheduled_at), "updated_at": _iso(i.updated_at),
                "role_id": i.role_id, "data": i.model_dump_json()}

    def get_interview(self, interview_id: str) -> Optional[Interview]:
        rows = self._rows(self._scope(sa.select(interviews.c.data).where(interviews.c.id == interview_id), interviews))
        return Interview.model_validate_json(rows[0]["data"]) if rows else None

    def list_interviews(
        self,
        q: str = "",
        status: Optional[str] = None,
        platform: Optional[str] = None,
        recommendation: Optional[str] = None,
        decision: Optional[str] = None,
        stage: Optional[str] = None,
        state: Optional[str] = None,
        assigned: Optional[str] = None,
        role_id: Optional[str] = None,
    ) -> list[dict]:
        """Summaries for the dashboard, newest first, with the latest report's recommendation.

        Filters: q matches candidate name or role (any case); "none" for recommendation or decision
        means not evaluated / not decided yet. Filtering is done here rather than in SQL because the
        fields live in each interview's JSON, and a self-hosted install holds at most a few thousand.
        """
        needle = " ".join(q.lower().split())
        wanted_rec = None if recommendation == "none" else recommendation
        wanted_decision = None if decision == "none" else decision
        recs = {r["session_id"]: r["recommendation"] for r in self._rows(sa.select(reports.c.session_id, reports.c.recommendation))}
        now = utcnow()
        out = []
        for row in self._rows(self._scope(sa.select(interviews.c.data).order_by(interviews.c.updated_at.desc()), interviews)):
            i = Interview.model_validate_json(row["data"])
            if (
                (needle and needle not in " ".join(f"{i.setup.candidate_name} {i.setup.role_title}".lower().split()))
                or (status is not None and i.status != status)
                or (platform is not None and i.platform != platform)
                or (decision is not None and i.decision != wanted_decision)
                or (assigned is not None and i.assigned != ("" if assigned == "unknown" else assigned))
                or (role_id is not None and i.role_id != role_id)
            ):
                continue
            rec = recs.get(i.latest_session_id) if i.latest_session_id else None
            if recommendation is not None and rec != wanted_rec:
                continue
            where = lifecycle.stage(i, rec is not None, now)
            if stage is not None and where != stage:
                continue
            board = lifecycle.state(i, where, now)
            if state is not None and board != state:
                continue
            out.append(
                {
                    "id": i.id,
                    "role_title": i.setup.role_title,
                    "candidate_name": i.setup.candidate_name,
                    "interview_type": i.setup.interview_type,
                    "status": i.status,
                    "stage": where,
                    "platform": i.platform,
                    "scheduled_at": _iso(i.scheduled_at),
                    "updated_at": _iso(i.updated_at),
                    "recommendation": rec,
                    "decision": i.decision,
                    "latest_session_id": i.latest_session_id,
                    "role_id": i.role_id,
                    "demo": i.demo,
                    "state": board,
                    "assigned": i.assigned,
                    "application_id": i.application_id,
                    "meeting_link": i.meeting_link,
                    "booking_pending": bool(i.booking and not i.booking.booked_at),
                    "invitation_undelivered": bool(i.invitation and i.invitation.undelivered),
                }
            )
        return out

    def scheduled_interviews(self) -> list[Interview]:
        """Every interview booked for a time, in any workspace: a meeting room can be shared, so its bookings are checked
        across the installation."""
        stmt = sa.select(interviews.c.data).where(interviews.c.status == "scheduled", interviews.c.scheduled_at.is_not(None))
        return [Interview.model_validate_json(r["data"]) for r in self._rows(stmt)]

    def due_interviews(self, now: datetime) -> list[Interview]:
        stmt = sa.select(interviews.c.data).where(
            interviews.c.status == "scheduled", interviews.c.scheduled_at.is_not(None), interviews.c.scheduled_at <= now.isoformat()
        )
        due = [Interview.model_validate_json(r["data"]) for r in self._rows(stmt)]
        return [i for i in due if not i.demo]  # demo interviews never run (app/demo.py)

    def delete_interview(self, interview_id: str) -> list[str]:
        """Delete an interview and everything under it. Returns the MeetStream bot ids it used."""
        if self.get_interview(interview_id) is None:
            return []
        rows = self._rows(sa.select(sessions.c.bot_id).where(sessions.c.interview_id == interview_id))
        self._run(self._scope(interviews.delete().where(interviews.c.id == interview_id), interviews))
        return [r["bot_id"] for r in rows if r["bot_id"]]

    def expired_interviews(self, days: int, now: Optional[datetime] = None) -> list[str]:
        """Finished interviews not updated for `days` days."""
        if days <= 0:
            return []
        cutoff = ((now or utcnow()) - timedelta(days=days)).isoformat()
        stmt = sa.select(interviews.c.id).where(interviews.c.status.in_(("completed", "failed")), interviews.c.updated_at < cutoff)
        return [r["id"] for r in self._rows(stmt)]

    # ---------- app state ----------

    def get_state(self, key: str) -> Optional[dict]:
        rows = self._rows(sa.select(app_state.c.value).where(app_state.c.key == key))
        return json.loads(rows[0]["value"]) if rows else None

    def set_state(self, key: str, value: dict) -> None:
        stmt = self._insert(app_state).values(key=key, value=json.dumps(value), updated_at=utcnow().isoformat())
        self._run(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at}))

    # ---------- saved settings ----------

    def saved_settings(self) -> list[dict]:
        return self._rows(sa.select(app_settings).order_by(app_settings.c.key))

    def save_setting(self, key: str, value: str, secret: bool) -> None:
        stmt = self._insert(app_settings).values(key=key, value=value, secret=secret, updated_at=utcnow().isoformat(), updated_by=CURRENT_ACTOR.get())
        self._run(stmt.on_conflict_do_update(index_elements=["key"], set_={
            "value": stmt.excluded.value, "secret": stmt.excluded.secret, "updated_at": stmt.excluded.updated_at, "updated_by": stmt.excluded.updated_by,
        }))

    def delete_setting(self, key: str) -> None:
        self._run(app_settings.delete().where(app_settings.c.key == key))

    # ---------- self-scheduling links ----------

    def set_booking_token(self, interview_id: str, token: Optional[str]) -> None:
        """The interview's one booking token (replacing any earlier one); None withdraws it."""
        with self._engine.begin() as c:
            c.execute(bookings.delete().where(bookings.c.interview_id == interview_id))
            if token:
                c.execute(bookings.insert().values(token=token, interview_id=interview_id))

    def set_invite_token(self, interview_id: str, token: Optional[str]) -> None:
        """The interview's one invitation link (replacing any earlier one, which stops working); None withdraws it.
        Only the token's hash is kept; its sessions and codes go with the old link."""
        from app.gateway import token_hash
        now = utcnow().isoformat()
        with self._engine.begin() as c:
            old = [r[0] for r in c.execute(sa.select(invitation_links.c.token_hash).where(invitation_links.c.interview_id == interview_id))]
            if old:
                c.execute(gateway_codes.delete().where(gateway_codes.c.token_hash.in_(old)))
            c.execute(gateway_sessions.delete().where(gateway_sessions.c.interview_id == interview_id))
            c.execute(invitation_links.delete().where(invitation_links.c.interview_id == interview_id))
            c.execute(invite_tokens.delete().where(invite_tokens.c.interview_id == interview_id))  # an older plain-text link
            if token:
                c.execute(invitation_links.insert().values(token_hash=token_hash(token), interview_id=interview_id, created_at=now))

    def interview_for_invite(self, token: str) -> Optional[Interview]:
        from app.gateway import token_hash
        rows = self._rows(sa.select(invitation_links.c.interview_id).where(invitation_links.c.token_hash == token_hash(token)))
        if not rows:  # a link from before hashed links that the app hasn't moved yet
            rows = self._rows(sa.select(invite_tokens.c.interview_id).where(invite_tokens.c.token == token))
        return self.get_interview(rows[0]["interview_id"]) if rows else None

    def put_meeting(self, interview_id: str, **row: Any) -> None:
        now = utcnow().isoformat()
        values = {"interview_id": interview_id, "workspace_id": self.interview_workspace(interview_id), "updated_at": now, **row}
        stmt = self._insert(meetings).values(created_at=now, **values)
        self._run(stmt.on_conflict_do_update(index_elements=["interview_id"], set_={k: stmt.excluded[k] for k in values if k != "interview_id"}))

    def meeting_for(self, interview_id: str) -> Optional[dict]:
        rows = self._rows(sa.select(meetings).where(meetings.c.interview_id == interview_id))
        return rows[0] if rows else None

    def delete_meeting(self, interview_id: str) -> None:
        self._run(meetings.delete().where(meetings.c.interview_id == interview_id))

    def plain_invite_links(self) -> list[dict]:
        """Links still stored in clear (from before hashed links), to move: app/gateway.py secure_old_links."""
        return self._rows(sa.select(invite_tokens.c.token, invite_tokens.c.interview_id))

    # ---------- interview access gateway: codes and sessions (app/gateway.py) ----------

    def gateway_code(self, token_hash: str) -> Optional[dict]:
        rows = self._rows(sa.select(gateway_codes).where(gateway_codes.c.token_hash == token_hash))
        return rows[0] if rows else None

    def put_gateway_code(self, token_hash: str, code_hash: str, expires_at: str, sends: int, first_sent_at: str) -> None:
        with self._engine.begin() as c:
            c.execute(gateway_codes.delete().where(gateway_codes.c.token_hash == token_hash))
            c.execute(gateway_codes.insert().values(token_hash=token_hash, code_hash=code_hash, expires_at=expires_at, attempts=0,
                                                    sends=sends, first_sent_at=first_sent_at))

    def take_gateway_attempt(self, token_hash: str, limit: int) -> bool:
        """Use one of the code's tries, in one statement: True if one was left. Parallel guesses can't exceed the limit."""
        return self._run(gateway_codes.update().where(gateway_codes.c.token_hash == token_hash, gateway_codes.c.attempts < limit)
                         .values(attempts=gateway_codes.c.attempts + 1)) == 1

    def count_gateway_attempt(self, token_hash: str) -> None:
        self._run(gateway_codes.update().where(gateway_codes.c.token_hash == token_hash).values(attempts=gateway_codes.c.attempts + 1))

    def drop_gateway_code(self, token_hash: str) -> None:
        self._run(gateway_codes.delete().where(gateway_codes.c.token_hash == token_hash))

    def active_gateway_sessions(self, token_hash: str) -> list[dict]:
        return self._rows(sa.select(gateway_sessions).where(gateway_sessions.c.token_hash == token_hash, gateway_sessions.c.ended_at.is_(None)))

    def open_gateway_session(self, session_hash: str, token_hash: str, interview_id: str) -> bool:
        """The link's verified device, unless another one is active already (one at a time; atomic). Returns whether
        this one is now the link's."""
        now = utcnow().isoformat()
        with self._lock, self._engine.begin() as c:
            active = c.execute(sa.select(gateway_sessions.c.session_hash).where(gateway_sessions.c.token_hash == token_hash,
                                                                                 gateway_sessions.c.ended_at.is_(None))).first()
            if active is not None and active[0] != session_hash:
                return False
            if active is None:
                c.execute(gateway_sessions.insert().values(session_hash=session_hash, token_hash=token_hash, interview_id=interview_id,
                                                           verified_at=now, last_seen=now))
            return True

    def touch_gateway_session(self, session_hash: str) -> None:
        self._run(gateway_sessions.update().where(gateway_sessions.c.session_hash == session_hash).values(last_seen=utcnow().isoformat()))

    def end_gateway_sessions(self, token_hash: str) -> int:
        return self._run(gateway_sessions.update().where(gateway_sessions.c.token_hash == token_hash, gateway_sessions.c.ended_at.is_(None))
                         .values(ended_at=utcnow().isoformat()))

    # ---------- applications ----------

    def create_application(self, role_id: str, candidate: ApplicationInput, **fields: Any) -> Application:
        """Raises DuplicateApplication if this email already applied for the role."""
        now = utcnow()
        app_ = Application(id=uuid.uuid4().hex[:12], role_id=role_id, created_at=now, updated_at=now, candidate=candidate, **fields)
        try:
            self._run(applications.insert().values(
                id=app_.id, role_id=role_id, workspace_id=self.role_workspace(role_id), email=candidate.email.strip().lower(), status=app_.status,
                external_key=f"{app_.external.integration_id}:{app_.external.application_id}" if app_.external and app_.external.application_id else None,
                interview_id=app_.interview_id, created_at=_iso(now), updated_at=_iso(now), data=app_.model_dump_json()))
        except sa.exc.IntegrityError as e:
            raise DuplicateApplication("You have already applied for this role") from e
        return app_

    def save_application(self, application: Application) -> Application:
        application = application.model_copy(update={"updated_at": utcnow()})
        self._run(self._scope(applications.update().where(applications.c.id == application.id), applications).values(
            status=application.status, interview_id=application.interview_id,
            updated_at=_iso(application.updated_at), data=application.model_dump_json()))
        return application

    def get_application(self, application_id: str) -> Optional[Application]:
        rows = self._rows(self._scope(sa.select(applications.c.data).where(applications.c.id == application_id), applications))
        return Application.model_validate_json(rows[0]["data"]) if rows else None

    def save_resume_file(self, application_id: str, filename: str, content_type: str, data: bytes) -> None:
        with self._engine.begin() as c:
            ws = c.execute(sa.select(applications.c.workspace_id).where(applications.c.id == application_id)).scalar()
            c.execute(resume_files.delete().where(resume_files.c.application_id == application_id))
            c.execute(resume_files.insert().values(application_id=application_id, workspace_id=ws, filename=filename[:200],
                                                   content_type=content_type, size=len(data), data=data, created_at=_iso(utcnow())))

    def resume_file(self, application_id: str) -> Optional[dict]:
        rows = self._rows(self._scope(sa.select(resume_files.c.filename, resume_files.c.content_type, resume_files.c.size, resume_files.c.data)
                                      .where(resume_files.c.application_id == application_id), resume_files))
        return rows[0] if rows else None

    def has_resume_file(self, application_id: str) -> bool:
        return bool(self._rows(self._scope(sa.select(resume_files.c.size).where(resume_files.c.application_id == application_id), resume_files)))

    def applications_for_role(self, role_id: str) -> list[Application]:
        stmt = self._scope(sa.select(applications.c.data).where(applications.c.role_id == role_id).order_by(applications.c.created_at.desc()), applications)
        return [Application.model_validate_json(r["data"]) for r in self._rows(stmt)]

    def application_counts(self) -> dict[str, dict[str, int]]:
        """role_id -> {status: count}, for the dashboard."""
        stmt = sa.select(applications.c.role_id, applications.c.status, sa.func.count().label("n")).group_by(
            applications.c.role_id, applications.c.status)
        out: dict[str, dict[str, int]] = {}
        for r in self._rows(self._scope(stmt, applications)):
            out.setdefault(r["role_id"], {})[r["status"]] = r["n"]
        return out

    def application_by_external(self, integration_id: str, external_application_id: str) -> Optional[Application]:
        rows = self._rows(sa.select(applications.c.data).where(applications.c.external_key == f"{integration_id}:{external_application_id}"))
        return Application.model_validate_json(rows[0]["data"]) if rows else None

    def application_by_email(self, role_id: str, email: str) -> Optional[Application]:
        rows = self._rows(sa.select(applications.c.data).where(applications.c.role_id == role_id, applications.c.email == email.strip().lower()))
        return Application.model_validate_json(rows[0]["data"]) if rows else None

    def set_external_key(self, application_id: str, key: Optional[str]) -> None:
        self._run(applications.update().where(applications.c.id == application_id).values(external_key=key))

    def all_applications(self) -> list[Application]:
        """Every application in the request's workspace, newest first (candidate search)."""
        stmt = self._scope(sa.select(applications.c.data).order_by(applications.c.created_at.desc()), applications)
        return [Application.model_validate_json(r["data"]) for r in self._rows(stmt)]

    # ---------- external ATS integrations ----------

    def create_integration(self, provider: str, data: dict) -> dict:
        import secrets as _secrets
        now = utcnow().isoformat()
        row = {"id": uuid.uuid4().hex[:12], "workspace_id": self._workspace_for_write(), "provider": provider,
               "inbound_token": _secrets.token_urlsafe(24), "enabled": True, "data": json.dumps(data), "created_at": now, "updated_at": now}
        self._run(integrations.insert().values(**row))
        return self._integration_row(row)

    @staticmethod
    def _integration_row(row: dict) -> dict:
        return {**{k: v for k, v in row.items() if k != "data"}, "data": json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]}

    def integrations(self) -> list[dict]:
        return [self._integration_row(r) for r in self._rows(self._scope(sa.select(integrations).order_by(integrations.c.created_at), integrations))]

    def integration(self, integration_id: str) -> Optional[dict]:
        rows = self._rows(self._scope(sa.select(integrations).where(integrations.c.id == integration_id), integrations))
        return self._integration_row(rows[0]) if rows else None

    def integration_by_token(self, token: str) -> Optional[dict]:
        """For the public webhook: not limited to a workspace (the token identifies it)."""
        rows = self._rows(sa.select(integrations).where(integrations.c.inbound_token == token))
        return self._integration_row(rows[0]) if rows else None

    def update_integration(self, integration_id: str, data: Optional[dict] = None, enabled: Optional[bool] = None) -> None:
        values: dict = {"updated_at": utcnow().isoformat()}
        if data is not None:
            values["data"] = json.dumps(data)
        if enabled is not None:
            values["enabled"] = enabled
        self._run(integrations.update().where(integrations.c.id == integration_id).values(**values))

    def delete_integration(self, integration_id: str) -> None:
        self._run(self._scope(integrations.delete().where(integrations.c.id == integration_id), integrations))

    def integration_event(self, integration_id: str, direction: str, kind: str, status: str, message: str = "",
                          application_id: Optional[str] = None, dedupe_key: Optional[str] = None, payload: Optional[dict] = None) -> Optional[int]:
        """Record what happened. With a dedupe_key already recorded: nothing is written and None is returned."""
        try:
            with self._engine.begin() as c:
                result = c.execute(integration_events.insert().values(
                    integration_id=integration_id, ts=utcnow().isoformat(), direction=direction, kind=kind, status=status,
                    message=message[:500], application_id=application_id, dedupe_key=dedupe_key,
                    payload=json.dumps(payload) if payload is not None else None))
                return result.inserted_primary_key[0]
        except sa.exc.IntegrityError:
            return None

    def update_integration_event(self, event_id: int, status: str, message: str = "", attempts: Optional[int] = None,
                                 keep_payload: bool = False) -> None:
        values: dict = {"status": status, "message": message[:500], "ts": utcnow().isoformat()}
        if attempts is not None:
            values["attempts"] = attempts
        if not keep_payload:
            values["payload"] = None
        self._run(integration_events.update().where(integration_events.c.id == event_id).values(**values))

    def integration_events(self, integration_id: str, limit: int = 100) -> list[dict]:
        stmt = (sa.select(integration_events.c.id, integration_events.c.ts, integration_events.c.direction, integration_events.c.kind,
                          integration_events.c.status, integration_events.c.application_id, integration_events.c.attempts,
                          integration_events.c.message)
                .where(integration_events.c.integration_id == integration_id).order_by(integration_events.c.id.desc()).limit(limit))
        return self._rows(stmt)

    def integration_event_seen(self, dedupe_key: str) -> bool:
        return bool(self._rows(sa.select(integration_events.c.id).where(integration_events.c.dedupe_key == dedupe_key)))

    def failed_outbound_events(self, max_attempts: int) -> list[dict]:
        stmt = sa.select(integration_events).where(integration_events.c.direction == "out", integration_events.c.status == "failed",
                                                   integration_events.c.payload.is_not(None), integration_events.c.attempts < max_attempts)
        return self._rows(stmt)

    def delete_applications_for_interview(self, interview_id: str) -> int:
        return self._run(self._scope(applications.delete().where(applications.c.interview_id == interview_id), applications))

    def delete_application(self, application_id: str) -> None:
        self._run(self._scope(applications.delete().where(applications.c.id == application_id), applications))

    def expired_applications(self, days: int, now: Optional[datetime] = None) -> list[str]:
        """Applications that never became an interview, not updated for `days` days (their interviews follow retention)."""
        if days <= 0:
            return []
        cutoff = ((now or utcnow()) - timedelta(days=days)).isoformat()
        stmt = sa.select(applications.c.id).where(applications.c.interview_id.is_(None), applications.c.updated_at < cutoff)
        return [r["id"] for r in self._rows(stmt)]

    def interview_for_booking(self, token: str) -> Optional[Interview]:
        rows = self._rows(sa.select(bookings.c.interview_id).where(bookings.c.token == token))
        return self.get_interview(rows[0]["interview_id"]) if rows else None

    def activity(self, since: datetime) -> dict[str, list[str]]:
        """UTC timestamps of what happened since `since`: interviews created, run (launched), completed, decided.

        Timestamps rather than per-day counts, so the dashboard can group them by the viewer's own time zone.
        """
        cut = since.isoformat()
        created, decided = [], []
        for row in self._rows(self._scope(sa.select(interviews.c.data), interviews)):
            i = Interview.model_validate_json(row["data"])
            if i.created_at.isoformat() >= cut:
                created.append(i.created_at.isoformat())
            if i.decided_at and i.decided_at.isoformat() >= cut:
                decided.append(i.decided_at.isoformat())
        joined = sessions.join(interviews, sessions.c.interview_id == interviews.c.id)
        run = [r["created_at"] for r in self._rows(self._scope(sa.select(sessions.c.created_at).select_from(joined).where(sessions.c.created_at >= cut), interviews))]
        completed = [
            r["ended_at"] for r in self._rows(self._scope(
                sa.select(sessions.c.ended_at).select_from(joined).where(sessions.c.state == "completed", sessions.c.ended_at.is_not(None), sessions.c.ended_at >= cut),
                interviews))
        ]
        return {"created": sorted(created), "run": sorted(run), "completed": sorted(completed), "decided": sorted(decided)}

    # ---------- sessions ----------

    def create_session(self, session_id: str, interview_id: str, platform: str, install_id: str = "") -> None:
        self._run(sessions.insert().values(
            id=session_id, interview_id=interview_id, platform=platform, state="created", created_at=utcnow().isoformat(),
            install_id=install_id or None,
        ))

    def claim_launch(self, interview_id: str, install_id: str, stale_after: timedelta = timedelta(minutes=10)) -> bool:
        """Claim a booked interview's start for this installation. True for the first claimant (or this one again); an
        older claim (an installation that stopped mid-start) is given up after `stale_after`."""
        now = utcnow()
        self._run(launch_claims.delete().where(launch_claims.c.interview_id == interview_id,
                                               launch_claims.c.claimed_at < (now - stale_after).isoformat()))
        try:
            self._run(launch_claims.insert().values(interview_id=interview_id, install_id=install_id, claimed_at=now.isoformat()))
            return True
        except sa.exc.IntegrityError:
            rows = self._rows(sa.select(launch_claims.c.install_id).where(launch_claims.c.interview_id == interview_id))
            return bool(rows) and rows[0]["install_id"] == install_id

    def release_launch(self, interview_id: str) -> None:
        self._run(launch_claims.delete().where(launch_claims.c.interview_id == interview_id))

    def update_session(self, session_id: str, **fields: Any) -> None:
        allowed = {"state", "bot_id", "agent_config_id", "ended_at", "end_reason"}
        sets = {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in fields.items() if k in allowed}
        if sets:
            self._run(sessions.update().where(sessions.c.id == session_id).values(**sets))

    def get_session(self, session_id: str) -> Optional[dict]:
        if not self._session_visible(session_id):
            return None
        rows = self._rows(sa.select(sessions).where(sessions.c.id == session_id))
        return rows[0] if rows else None

    def list_sessions(self, interview_id: str) -> list[dict]:
        if self.get_interview(interview_id) is None:
            return []
        return self._rows(sa.select(sessions).where(sessions.c.interview_id == interview_id).order_by(sessions.c.created_at.desc()))

    def latest_session_id(self) -> Optional[str]:
        joined = sessions.join(interviews, sessions.c.interview_id == interviews.c.id)
        rows = self._rows(self._scope(sa.select(sessions.c.id).select_from(joined).order_by(sessions.c.created_at.desc()).limit(1), interviews))
        return rows[0]["id"] if rows else None

    def unfinished_sessions(self, install_id: Optional[str] = None) -> list[dict]:
        """Sessions not finished. With install_id: only that installation's, and those with none recorded (older)."""
        stmt = sa.select(sessions).where(sessions.c.state.not_in(TERMINAL_SESSION_STATES))
        if install_id:
            stmt = stmt.where(sa.or_(sessions.c.install_id.is_(None), sessions.c.install_id == install_id))
        return self._rows(stmt)

    def add_utterance(self, session_id: str, u: Utterance) -> None:
        with self._lock, self._engine.begin() as c:
            seq = c.execute(sa.select(sa.func.coalesce(sa.func.max(utterances.c.seq), 0) + 1).where(utterances.c.session_id == session_id)).scalar()
            c.execute(utterances.insert().values(
                session_id=session_id, seq=seq, ts=u.ts.isoformat(), speaker=u.speaker, role=u.role, text=u.text, question_id=u.question_id
            ))

    def delete_utterances(self, session_id: str, role: str) -> int:
        return self._run(utterances.delete().where(utterances.c.session_id == session_id, utterances.c.role == role))

    def transcript(self, session_id: str) -> list[Utterance]:
        if not self._session_visible(session_id):
            return []
        rows = self._rows(sa.select(utterances).where(utterances.c.session_id == session_id).order_by(utterances.c.seq))
        return [
            Utterance(speaker=r["speaker"], role=r["role"], text=r["text"], ts=r["ts"], question_id=r["question_id"])
            for r in rows
        ]

    def upsert_note(self, session_id: str, n: AnswerNote) -> None:
        with self._lock, self._engine.begin() as c:
            position = c.execute(sa.select(sa.func.coalesce(sa.func.max(notes.c.position), 0) + 1).where(notes.c.session_id == session_id)).scalar()
            stmt = self._insert(notes).values(
                session_id=session_id, question_id=n.question_id, note=n.note, follow_ups=n.follow_ups, position=position
            )
            c.execute(stmt.on_conflict_do_update(
                index_elements=["session_id", "question_id"], set_={"note": stmt.excluded.note, "follow_ups": stmt.excluded.follow_ups}
            ))

    def notes(self, session_id: str) -> list[AnswerNote]:
        if not self._session_visible(session_id):
            return []
        rows = self._rows(sa.select(notes).where(notes.c.session_id == session_id).order_by(notes.c.position))
        return [AnswerNote(question_id=r["question_id"], note=r["note"], follow_ups=r["follow_ups"]) for r in rows]

    # ---------- reports (also the ReportSink) ----------

    def push_report(self, report: EvaluationReport) -> str:
        values = {
            "session_id": report.session_id,
            "created_at": utcnow().isoformat(),
            "recommendation": report.evaluation.recommendation if report.evaluation else None,
            "data": report.model_dump_json(exclude=REPORT_DERIVED_FIELDS),
        }
        stmt = self._insert(reports).values(**values)
        self._run(stmt.on_conflict_do_update(
            index_elements=["session_id"], set_={k: stmt.excluded[k] for k in ("created_at", "recommendation", "data")}
        ))
        return f"local:reports/{report.session_id}"

    def get_report(self, session_id: str) -> Optional[EvaluationReport]:
        if not self._session_visible(session_id):
            return None
        rows = self._rows(sa.select(reports.c.data).where(reports.c.session_id == session_id))
        return EvaluationReport.model_validate_json(rows[0]["data"]) if rows else None

    # ---------- audit ----------

    def audit(self, action: str, interview_id: str | None = None, session_id: str | None = None, detail: str = "") -> None:
        ws = CURRENT_WORKSPACE.get() or (self.interview_workspace(interview_id) if interview_id else None)
        self._run(audit_events.insert().values(
            ts=utcnow().isoformat(), action=action, interview_id=interview_id, session_id=session_id, detail=detail,
            actor=CURRENT_ACTOR.get(), workspace_id=ws or None,
        ))

    def audit_log(self, limit: int = 200, interview_id: str | None = None) -> list[dict]:
        stmt = self._scope(sa.select(audit_events).order_by(audit_events.c.id.desc()).limit(limit), audit_events)
        if interview_id:
            stmt = stmt.where(audit_events.c.interview_id == interview_id)
        return self._rows(stmt)
