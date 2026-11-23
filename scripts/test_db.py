"""Check a database for Recruence, safely: `npm run test:db -- neon` (or supabase, sqlite, or any env variable name).

Development only. Connection strings come from environment variables, read from `.env.test.local` (git-ignored)
or `.env`: MIA_TEST_NEON_DATABASE_URL, MIA_TEST_SUPABASE_DATABASE_URL, or any other name you pass. They are never
printed: only the host, database and user are shown.

What it does, in order:
1. Connects and reports the server version, SSL, encoding, extensions and permissions.
2. Looks at the schema: empty, Recruence's (which version), or someone else's tables (left alone).
3. Only with --init: creates Recruence's tables or upgrades them. Recruence's steps only add tables, columns and indexes;
   nothing is dropped, truncated, renamed or rewritten. Without --init nothing is changed.
4. A create / read / update / delete test with temporary records in a private test workspace (never visible in
   any real workspace), then removes exactly those records.

Exit code 0 when everything asked for passed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from app import dbconfig  # noqa: E402
from app.models import RoleSetup  # noqa: E402
from app.store import CURRENT_WORKSPACE, SCHEMA_VERSION, Store, describe, metadata, roles, schema_version  # noqa: E402

TARGETS = {"neon": "MIA_TEST_NEON_DATABASE_URL", "supabase": "MIA_TEST_SUPABASE_DATABASE_URL"}
TEST_WORKSPACE = "mia-db-test"  # no such workspace exists; records here are invisible everywhere else


def env_value(name: str) -> str:
    for file in (ROOT / ".env.test.local", ROOT / ".env"):
        if file.exists():
            value = dotenv_values(file).get(name)
            if value:
                return value.strip()
    return os.environ.get(name, "").strip()


def step(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{f': {detail}' if detail else ''}")
    return ok


def info(label: str, detail: str) -> None:
    print(f"  [info] {label}: {detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="neon, supabase, sqlite, or the name of an environment variable holding a URL")
    ap.add_argument("--init", action="store_true", help="create or upgrade Recruence's tables (additive only)")
    ap.add_argument("--no-crud", action="store_true", help="skip the temporary-record test")
    args = ap.parse_args()

    if args.target == "sqlite":
        url, label = str(ROOT / "data" / "db-test.sqlite"), "SQLite (data/db-test.sqlite)"
    else:
        var = TARGETS.get(args.target.lower(), args.target)
        url = env_value(var)
        if not url:
            print(f"{var} isn't set. Add it to .env.test.local (git-ignored):\n  {var}=postgresql://user:password@host/db?sslmode=require")
            return 2
        try:
            url = dbconfig.normalise(url)
        except dbconfig.DatabaseConfigError as e:
            print(f"{var}: {e}")
            return 2
        label = describe(url)
    print(f"\nDatabase: {label}")
    ok = True
    started = time.monotonic()

    # 1. Connection and server
    if args.target != "sqlite":
        result = dbconfig.test(url)
        if not step("connect", result["ok"], result["message"]):
            return 1
        engine = sa.create_engine(url, connect_args={"connect_timeout": 10})
        try:
            with engine.connect() as c:
                info("server", c.execute(sa.text("SELECT version()")).scalar().split(",")[0])
                info("server_version_num", str(c.execute(sa.text("SHOW server_version_num")).scalar()))
                info("database / user", f"{c.execute(sa.text('SELECT current_database()')).scalar()} / {c.execute(sa.text('SELECT current_user')).scalar()}")
                info("encoding", c.execute(sa.text("SHOW server_encoding")).scalar())
                # Asked of this connection itself: behind a pooler (Neon, Supabase) the server's own view
                # (pg_stat_ssl) describes the pooler's connection, not ours.
                ssl = bool(c.connection.dbapi_connection.pgconn.ssl_in_use)
                ok &= step("SSL", ssl, "this connection is encrypted" if ssl else "not encrypted: add ?sslmode=require to the URL")
                exts = sorted(r[0] for r in c.execute(sa.text("SELECT extname FROM pg_extension")))
                info("extensions", ", ".join(exts) or "none")
                info("needed extensions", "none (Recruence uses plain tables, text and integers)")
                ok &= step("PostgreSQL 12 or newer", int(c.execute(sa.text("SHOW server_version_num")).scalar()) >= 120000)
                schema = c.execute(sa.text("SELECT current_schema()")).scalar()
                info("schema", f"{schema} (Recruence uses only this one; it never creates another)")
                tables = set(sa.inspect(c).get_table_names())
        finally:
            engine.dispose()
    else:
        tables = set(sa.inspect(sa.create_engine(f"sqlite:///{url}")).get_table_names()) if Path(url).exists() else set()

    # 2. Schema
    ours = {t.name for t in metadata.sorted_tables}
    present, others = tables & ours, tables - ours
    version = None
    if "schema_version" in tables:
        engine = sa.create_engine(url if "://" in url else f"sqlite:///{url}")
        with engine.connect() as c:
            version = c.execute(sa.select(sa.func.max(schema_version.c.version))).scalar()
        engine.dispose()
    if others:
        info("other tables", f"{len(others)} not Recruence's: left untouched")
    if not present:
        info("Recruence schema", "none yet (empty for Recruence)")
    else:
        info("Recruence schema", f"version {version} of {SCHEMA_VERSION}; {len(present)} of {len(ours)} tables")
    needs_change = not present or version != SCHEMA_VERSION or present != ours
    if needs_change and not args.init:
        print(f"  [skip] Recruence's tables are {'missing' if not present else 'not up to date'}. Run again with --init to create or "
              "upgrade them (adds tables, columns and indexes only).")
        print(f"\nDone in {time.monotonic() - started:.1f} s: {'checks passed' if ok else 'some checks failed'}; nothing was changed.")
        return 0 if ok else 1

    # 3. Initialise or upgrade (additive), through the same code the app uses
    try:
        store = Store(url)
    except Exception as e:  # noqa: BLE001
        step("open Recruence's schema", False, dbconfig._scrub(f"{type(e).__name__}: {e}", url) if "://" in url else str(e))
        return 1
    after = store.ping()
    with store._engine.connect() as c:
        now_tables = set(sa.inspect(c).get_table_names())
    ok &= step("Recruence schema", after == SCHEMA_VERSION and ours <= now_tables,
               f"version {after}; {'created' if not present else 'upgraded' if version != after else 'already current'}; all {len(ours)} tables present")

    # 4. Temporary records, in a private test workspace, removed afterwards
    if not args.no_crud:
        token = CURRENT_WORKSPACE.set(TEST_WORKSPACE)
        tag = f"Recruence DB test {uuid.uuid4().hex[:8]}"
        state_key = f"mia_db_test_{uuid.uuid4().hex[:8]}"
        role_id = None
        try:
            role = store.create_role(RoleSetup(role_title=tag, job_description="Temporary record from scripts/test_db.py"))
            role_id = role.id
            ok &= step("create", store.get_role(role_id) is not None)
            saved = store.save_role(role.model_copy(update={"setup": role.setup.model_copy(update={"level": "Updated"})}))
            ok &= step("update", store.get_role(role_id).setup.level == "Updated" and saved.id == role_id)
            ok &= step("read (list)", [r.id for r in store.list_roles()] == [role_id], "only the test record is visible in the test workspace")
            store.set_state(state_key, {"ok": True})
            ok &= step("key-value write and read", store.get_state(state_key) == {"ok": True})
        finally:
            if role_id:
                store.delete_role(role_id)
            store._run(sa.text("DELETE FROM app_state WHERE key = :k").bindparams(k=state_key))
            CURRENT_WORKSPACE.reset(token)
        with store._engine.connect() as c:
            left = c.execute(sa.select(sa.func.count()).select_from(roles).where(roles.c.workspace_id == TEST_WORKSPACE)).scalar()
        ok &= step("delete and clean-up", left == 0 and store.get_state(state_key) is None, "only the records this test made were removed")
    store.close()
    print(f"\nDone in {time.monotonic() - started:.1f} s: {'all checks passed' if ok else 'some checks failed'}.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
