"""The database connection chosen from Settings: precedence, encryption at rest, validation, never switching live."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import dbconfig
from app.llm import OfflineAI
from app.secret_box import load_box
from app.security import permitted
from app.server import create_app
from tests.conftest import TEST_DATABASE_URL

PG = "postgresql://mia:s3cret-pw@db.example.com:5432/mia"


def test_precedence_env_then_saved_then_local(tmp_path):
    box = load_box(tmp_path)
    local = tmp_path / "interviewer.db"
    assert dbconfig.effective("", local, tmp_path, box) == (local, "local")
    dbconfig.save(tmp_path, box, PG)
    assert dbconfig.effective("", local, tmp_path, box) == (PG, "saved")
    assert dbconfig.effective("postgresql://env/x", local, tmp_path, box) == ("postgresql://env/x", "env")
    assert "s3cret-pw" not in (tmp_path / dbconfig.FILE).read_text()  # encrypted at rest
    assert dbconfig.forget(tmp_path) and not dbconfig.forget(tmp_path)


def test_only_postgresql_urls_with_a_host_and_database():
    for bad in ("mysql://u:p@h/db", "not a url", "postgresql:///nodb"):
        with pytest.raises(dbconfig.DatabaseConfigError):
            dbconfig.normalise(bad)
    assert dbconfig.normalise("postgres://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"


def test_a_failed_connection_never_echoes_the_password():
    r = dbconfig.test("postgresql://mia:s3cret-pw@127.0.0.1:1/mia")
    assert r["ok"] is False and "s3cret-pw" not in r["message"]


def test_copy_moves_every_table_and_refuses_a_full_target(tmp_path, store, setup):
    store.create_interview(setup)
    store.save_setting("bot_name", '"X"', secret=False)
    source = tmp_path / "a.db"
    counts = dbconfig.copy_all(store_path(store, tmp_path, source), tmp_path / "b.db")
    assert counts["interviews"] == 1 and counts["app_settings"] == 1 and "applications" in counts
    with pytest.raises(dbconfig.DatabaseConfigError, match="already holds data"):
        dbconfig.copy_all(source, tmp_path / "b.db")


def store_path(store, tmp_path, path):
    """The fixture's store copied into a SQLite file (the fixture may be PostgreSQL)."""
    if store.backend == "sqlite" and str(store._engine.url.database) not in ("", ":memory:"):
        return store._engine.url.database
    dbconfig.copy_all(store._engine.url.render_as_string(hide_password=False), path)
    return path


def test_settings_api_never_switches_live_and_is_admin_only(settings, tmp_path):
    s = replace(settings, data_dir=tmp_path)
    app = create_app(settings=s, ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        view = c.get("/api/database").json()
        assert view["source"] in ("local", "env") and view["restart_required"] is False
        assert c.post("/api/database/test", json={"url": "mysql://x/y"}).json()["ok"] is False
        assert c.put("/api/database", json={"url": "postgresql://mia:pw@127.0.0.1:1/mia"}).status_code in (409, 422)
        assert "pw@" not in str(c.get("/api/database").json())
        # A saved connection is used from the next start only, and replacing it needs confirming.
        box = load_box(tmp_path)
        dbconfig.save(tmp_path, box, PG)
        if view["source"] != "env":
            after = c.get("/api/database").json()
            assert after["restart_required"] is True and after["saved"] == "PostgreSQL at db.example.com:5432/mia as mia"
            assert c.put("/api/database", json={"url": PG}).status_code == 409  # replace not confirmed
            assert c.delete("/api/database").json()["saved"] is None
    for role in ("viewer", "recruiter"):
        assert permitted(role, "GET", "/api/database")
        assert not permitted(role, "POST", "/api/database/test") and not permitted(role, "PUT", "/api/database")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="needs a real PostgreSQL (TEST_DATABASE_URL)")
def test_checking_a_real_postgresql_succeeds():
    # Mocked elsewhere; this runs the real reads and the create-table check on one connection.
    r = dbconfig.test(TEST_DATABASE_URL)
    assert r["ok"] is True, r["message"]
    assert r["message"].startswith("Connected to") and r["state"] in ("empty", "no_mia", "mia")
    left = dbconfig.sa.inspect(dbconfig.sa.create_engine(TEST_DATABASE_URL)).get_table_names()
    assert not [t for t in left if t.startswith("mia_permission_check")]  # the check is rolled back, never left behind


def test_an_unreachable_supabase_direct_host_points_to_the_session_pooler(monkeypatch):
    import sqlalchemy as sa

    class Refused:
        def connect(self):
            raise sa.exc.OperationalError("connect", {}, Exception("failed to resolve host 'db.abcd.supabase.co': getaddrinfo failed"))

        def dispose(self):
            pass
    monkeypatch.setattr(dbconfig.sa, "create_engine", lambda *a, **k: Refused())
    r = dbconfig.test("postgresql://postgres:pw-x@db.abcd.supabase.co:5432/postgres")
    assert r["ok"] is False and "Session pooler" in r["message"] and "pw-x" not in r["message"]


def test_a_saved_database_that_is_down_at_start_falls_back_to_the_local_one(settings):
    from app import setup as first_run

    if settings.database_url:
        pytest.skip("needs the local SQLite default")
    s = replace(settings)
    dbconfig.save_profile(s.data_dir, load_box(s.data_dir), "Company", "postgresql+psycopg://u:pw-down@127.0.0.1:1/company")
    state = first_run.SetupState(s.data_dir)
    state.update(access="account")
    state.complete()  # set up earlier, with accounts in that database
    app = create_app(settings=s, ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        status = c.get("/api/auth/status").json()
        assert "couldn't be reached" in status["database_problem"] and "pw-down" not in status["database_problem"]
        assert c.get("/api/setup/status").json()["completed"] is True  # accounts are elsewhere, not gone: no setup again
        conn = c.get("/api/connection/status").json()
        assert {p["name"]: p["active"] for p in conn["profiles"]} == {"Local database (SQLite)": True, "Company": False}
        assert "pw-down" not in str(conn)


def test_a_failing_database_url_in_the_environment_still_stops_the_app(settings):
    with pytest.raises(Exception):
        create_app(settings=replace(settings, database_url="postgresql+psycopg://u:p@127.0.0.1:1/x"), ai=OfflineAI(), run_loops=False)


def test_amazon_rds_and_aurora_urls_keep_their_ssl_settings():
    import sqlalchemy as sa

    url = ("postgresql://mia_app:rds-pw@mia-db.cluster-abc123.eu-west-1.rds.amazonaws.com:5432/mia"
           "?sslmode=verify-full&sslrootcert=C:/certs/global-bundle.pem")
    n = dbconfig.normalise(url)
    engine = sa.create_engine(n)
    args = engine.dialect.create_connect_args(engine.url)[1]
    assert args["sslmode"] == "verify-full" and args["sslrootcert"] == "C:/certs/global-bundle.pem"
    assert args["host"].endswith(".rds.amazonaws.com") and args["dbname"] == "mia"
    shown = dbconfig.describe(n)
    assert shown == "PostgreSQL at mia-db.cluster-abc123.eu-west-1.rds.amazonaws.com:5432/mia as mia_app" and "rds-pw" not in shown
    from app.store import POSTGRES_CONNECT_ARGS
    assert POSTGRES_CONNECT_ARGS["prepare_threshold"] is None  # works behind RDS Proxy and PgBouncer without pinning


def test_an_empty_database_is_ready_for_setup_and_an_installation_is_recognised():
    from app.store import metadata
    mia = {t.name for t in metadata.sorted_tables}
    empty = dbconfig.classify(set(), {})
    assert empty["state"] == "empty" and empty["ready"] and "ready for a new Recruence setup" in empty["summary"]
    other = dbconfig.classify({"orders", "customers"}, {})
    assert other["state"] == "no_mia" and other["ready"] and "2 other table(s)" in other["summary"] and "untouched" in other["summary"]
    found = dbconfig.classify(mia, {"roles": 2, "interviews": 5, "applications": 0}, 3)
    assert found["state"] == "mia" and found["ready"] and "2 roles, 5 interviews" in found["summary"] and "schema version 3" in found["summary"]
    assert "nothing is dropped" in found["summary"]
    assert dbconfig.classify({"schema_version", "interviews", "sessions"}, {})["summary"].startswith("Existing Recruence installation found (no records yet")
    clash = dbconfig.classify({"users", "orders"}, {})  # another app's "users" table: not Recruence's, and never touched
    assert clash["state"] == "conflict" and not clash["ready"] and "users" in clash["summary"]
