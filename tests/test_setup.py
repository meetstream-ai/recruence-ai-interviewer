"""First-run setup: detection, the setup code, each step, resuming, finishing, and keeping secrets out.

All provider, MeetStream and email checks here are mocked; nothing calls a paid API.
"""

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from app import dbconfig, setup as first_run
from app.llm import OfflineAI
from app.secret_box import load_box
from app.server import create_app
from app.store import Store

DOCKER = ("172.17.0.1", 50000)  # how a browser reaches a container through its port mapping: not loopback


def make(settings, client=None, ai=None, store=None):
    app = create_app(settings=settings, store=store or Store(settings.database), ai=ai or OfflineAI(), run_loops=True)
    return app, TestClient(app, client=client) if client else TestClient(app)


class FakeAI(OfflineAI):
    is_ai = True
    name = "gemini:test-model"


def test_first_launch_needs_setup_and_existing_installs_dont(settings):
    _, c = make(replace(settings))
    with c:
        s = c.get("/api/setup/status").json()
        assert s["completed"] is False and s["steps"]["access"]["local_request"] is True
    # An install that already holds roles or interviews is set up already (an upgrade never starts the wizard).
    s2 = replace(settings, data_dir=settings.data_dir / "other")
    store = Store(s2.database)
    store.create_role(__import__("app.models", fromlist=["RoleSetup"]).RoleSetup(role_title="R", job_description="J"))
    _, c2 = make(s2, store=store)
    with c2:
        assert c2.get("/api/setup/status").json()["completed"] is True


def test_preconfigured_deployments_skip_the_wizard(settings):
    _, c = make(replace(settings, app_access_token="env-token"), ai=FakeAI())
    with c:
        assert c.get("/api/setup/status", headers={"Authorization": "Bearer env-token"}).json()["completed"] is True


def test_a_remote_browser_needs_the_setup_code(settings, caplog):
    app, c = make(replace(settings), client=DOCKER)
    with c:
        anon = c.get("/api/setup/status").json()
        assert anon == {"completed": False, "signed_in": False, "code_required": True, "sign_in_required": False}  # nothing about the config
        assert c.get("/api/config").status_code == 403
        code = app.state.setup.code
        assert code and code in caplog.text  # printed in the server log
        assert c.post("/api/setup/unlock", json={"code": "0000-0000"}).status_code == 401
        assert c.post("/api/setup/unlock", json={"code": code}, headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403
        assert c.post("/api/setup/unlock", json={"code": code.replace("-", "").lower()}).json() == {"unlocked": True}
        assert c.get("/api/config").status_code == 200  # the setup session is an admin until setup is done
        assert c.get("/api/setup/status").json()["steps"]["access"]["local_request"] is False


def test_the_whole_wizard_resumes_and_finishes(settings):
    s = replace(settings)
    app, c = make(s, client=DOCKER, ai=FakeAI())
    with c:
        c.post("/api/setup/unlock", json={"code": app.state.setup.code})
        assert c.put("/api/setup/state", json={"mode": "team", "admin_name": "Dana", "admin_email": "dana@example.com"}).status_code == 200
        assert c.post("/api/setup/access", json={"method": "local_only"}).status_code == 409  # not on the server itself
        r = c.post("/api/setup/access", json={"method": "token"}).json()
        token = r["token"]
        assert len(token) >= 24 and r["steps"]["access"]["status"] == "done"
        assert token not in json.dumps(c.get("/api/setup/status").json()) and token not in json.dumps(c.get("/api/config").json())
        c.put("/api/setup/state", json={"skip": "meetstream"})
        c.put("/api/setup/state", json={"skip": "email"})

    # Restart: progress is still there (it's a file in DATA_DIR), and the token signs in.
    app2, c2 = make(replace(settings), client=DOCKER, ai=FakeAI())
    with c2:
        auth = {"Authorization": f"Bearer {token}"}
        st = c2.get("/api/setup/status", headers=auth).json()
        assert st["completed"] is False and st["mode"] == "team" and st["admin_name"] == "Dana"
        assert st["steps"]["meetstream"]["status"] == "skipped" and st["steps"]["ai"]["status"] == "done"
        done = c2.post("/api/setup/complete", headers=auth).json()
        assert done == {"completed": True, "restart_required": False}
        assert c2.post("/api/setup/complete", headers=auth).status_code == 409  # idempotent: once only
        assert c2.post("/api/setup/unlock", json={"code": "anything"}).status_code == 409


def test_setup_reopens_when_the_database_no_longer_holds_its_access_token(settings, tmp_path):
    app, c = make(replace(settings), client=DOCKER, ai=FakeAI())
    with c:
        c.post("/api/setup/unlock", json={"code": app.state.setup.code})
        c.put("/api/setup/state", json={"mode": "team"})
        c.post("/api/setup/access", json={"method": "token"})
        assert c.post("/api/setup/complete").json()["completed"] is True
    # The same DATA_DIR on an empty database (DATABASE_URL changed): the token, a setting in the old database, is gone.
    app2, c2 = make(replace(settings), client=DOCKER, ai=FakeAI(), store=Store(tmp_path / "other.db"))
    with c2:
        assert c2.get("/api/setup/status").json() == {"completed": False, "signed_in": False, "code_required": True, "sign_in_required": False}
        c2.post("/api/setup/unlock", json={"code": app2.state.setup.code})
        st = c2.get("/api/setup/status").json()
        assert st["mode"] == "team" and st["steps"]["access"]["status"] != "done"  # the rest of the progress is kept
    # Back on the original database (DATABASE_URL removed again): the token is there, so setup is finished again.
    _, c3 = make(replace(settings), client=DOCKER, ai=FakeAI())
    with c3:
        assert c3.get("/api/setup/status").json()["completed"] is True
    # And it keeps watching: switching to the empty database again reopens setup again.
    _, c4 = make(replace(settings), client=DOCKER, ai=FakeAI(), store=Store(tmp_path / "other.db"))
    with c4:
        assert c4.get("/api/setup/status").json()["completed"] is False


def test_finishing_needs_access_and_an_ai_choice(settings):
    _, c = make(replace(settings))
    with c:
        c.put("/api/setup/state", json={"mode": "personal"})
        r = c.post("/api/setup/complete")
        assert r.status_code == 422 and "Access" in r.json()["detail"] and "AI provider" in r.json()["detail"]
        c.post("/api/setup/access", json={"method": "local_only"})  # the test client counts as this machine
        c.put("/api/setup/state", json={"ai_offline": True})
        assert c.post("/api/setup/complete").json()["completed"] is True


def test_provider_test_outcomes_are_recorded_without_keys(settings, monkeypatch):
    import app.server as server_module

    async def rejected(provider, api_key, model, base, **_):
        return {"ok": False, "message": "The provider rejected the key (401).", "models": [], "reason": "invalid_key"}
    monkeypatch.setattr(server_module, "test_provider", rejected)
    app, c = make(replace(settings))
    with c:
        r = c.post("/api/config/test-ai", json={"provider": "gemini", "api_key": "AIza-wrong-key", "model": "m"}).json()
        assert r["reason"] == "invalid_key"
        st = c.get("/api/setup/status").json()["steps"]["ai"]
        assert st["status"] == "failed" and st["check"]["reason"] == "invalid_key"
    assert "AIza-wrong-key" not in (settings.data_dir / first_run.FILE).read_text()


def test_saving_other_ai_settings_clears_an_old_test_result(settings, monkeypatch):
    import app.server as server_module

    async def missing_model(provider, api_key, model, base, **_):
        return {"ok": False, "message": "Connected, but the model isn't available here.", "models": [], "reason": "model_unavailable"}
    monkeypatch.setattr(server_module, "test_provider", missing_model)
    _, c = make(replace(settings))
    with c:
        c.post("/api/config/test-ai", json={"provider": "ollama", "model": "not-installed:1b"})
        assert c.get("/api/setup/status").json()["steps"]["ai"]["check"]["ok"] is False
        assert c.put("/api/config", json={"changes": {"llm_provider": "ollama", "ollama_model": "qwen2.5:3b"}}).status_code == 200
        assert c.get("/api/setup/status").json()["steps"]["ai"]["check"] is None  # the failure was about another model


def test_the_setup_database_is_connected_before_the_first_account(settings, monkeypatch, tmp_path):
    other = tmp_path / "hosted.db"  # stands in for a PostgreSQL server: same code path after the connection test
    full = tmp_path / "full.db"
    Store(full).create_role(__import__("app.models", fromlist=["RoleSetup"]).RoleSetup(role_title="Theirs", job_description="J"))

    def fake_test(url):
        has = "full" in url
        return {"ok": True, "message": "Connected", "database": "PostgreSQL at db.example.com:5432/mia as mia", "has_data": has,
                "has_mia_tables": has, "rows": {"roles": 1} if has else {}}
    monkeypatch.setattr(dbconfig, "test", fake_test)
    monkeypatch.setattr(dbconfig, "normalise", lambda url: url.split("#")[0])
    s = replace(settings)
    app, c = make(s)
    with c:
        c.put("/api/setup/state", json={"mode": "team"})
        refused = c.post("/api/setup/database", json={"choice": "postgresql", "url": f"sqlite:///{full.as_posix()}#pw-full"})
        assert refused.status_code == 409 and "already holds Recruence data" in refused.json()["detail"]  # never taken over silently
        ok = c.post("/api/setup/database", json={"choice": "postgresql", "url": f"sqlite:///{other.as_posix()}#pw-secret"}).json()
        assert ok["has_accounts"] is False and "pw-secret" not in json.dumps(ok)
        assert "hosted.db" in str(app.state.store.target)  # connected now, not at Finish
        assert "pw-secret" not in (s.data_dir / first_run.FILE).read_text()
        # The owner's account goes into the connected database.
        r = c.post("/api/auth/register", json={"name": "Dana Owner", "email": "dana@example.com", "password": "correct horse"})
        assert r.status_code == 200 and r.json()["workspace_id"]
        c.put("/api/setup/state", json={"ai_offline": True})
        assert c.post("/api/setup/complete").json() == {"completed": True, "restart_required": False}
    from app.accounts import Accounts
    assert Accounts(Store(other)).count() == 1 and Accounts(Store(settings.db_path)).count() == 0
    assert dbconfig.saved_url(s.data_dir, load_box(s.data_dir)).startswith("sqlite:///")  # used again at the next start


def test_the_setup_code_stops_working_after_repeated_guesses(settings):
    app, c = make(replace(settings), client=DOCKER)
    with c:
        code = app.state.setup.code
        for _ in range(first_run.MAX_FAILURES):
            c.post("/api/setup/unlock", json={"code": "0000-0000"})
        assert c.post("/api/setup/unlock", json={"code": code}).status_code == 401  # even the right code, until a restart


def test_signing_in_with_an_existing_account_finishes_setup(settings, monkeypatch, tmp_path):
    from app.accounts import Accounts
    from app.models import RoleSetup
    from app.store import CURRENT_WORKSPACE

    theirs = tmp_path / "company.db"
    company = Store(theirs)
    acc = Accounts(company)
    owner = acc.create_user("dana@company.example", "Dana", "company-pass-1", is_admin=True)
    ws = acc.create_workspace("Company", owner["id"])
    token = CURRENT_WORKSPACE.set(ws["id"])
    company.create_role(RoleSetup(role_title="Existing role", job_description="Already there"))
    CURRENT_WORKSPACE.reset(token)
    company.close()

    def fake_test(url):
        return {"ok": True, "message": "Connected", "database": "Company database", "has_data": True, "has_mia_tables": True, "rows": {"roles": 1}}
    monkeypatch.setattr(dbconfig, "test", fake_test)
    monkeypatch.setattr(dbconfig, "normalise", lambda url: url)
    app, c = make(replace(settings), client=DOCKER)
    with c:
        c.post("/api/setup/unlock", json={"code": app.state.setup.code})
        assert c.post("/api/setup/existing-account").status_code == 409  # not signed in with an account yet
        r = c.post("/api/setup/database", json={"choice": "postgresql", "url": f"sqlite:///{theirs.as_posix()}", "data": "use_existing"}).json()
        assert r["has_accounts"] is True
        assert c.post("/api/auth/account-login", json={"email": "dana@company.example", "password": "wrong"}).status_code == 401
        assert c.post("/api/auth/account-login", json={"email": "dana@company.example", "password": "company-pass-1"}).status_code == 200
        assert c.post("/api/setup/existing-account").json() == {"completed": True}
        assert c.get("/api/setup/status").json()["completed"] is True
        assert [r["role_title"] for r in c.get("/api/roles").json()] == ["Existing role"]  # its data, as it was
        assert c.post("/api/setup/existing-account").status_code == 409  # only during setup


def test_an_empty_database_is_set_up_and_an_existing_installation_is_kept(settings, monkeypatch, tmp_path):
    """"I already have an account" with an empty database: nothing is created by the check; confirming setup creates
    Recruence's tables there. An existing installation is used as it is: its records survive the connection untouched."""
    import sqlalchemy as sa
    from app.models import RoleSetup
    from app.store import SCHEMA_VERSION, metadata

    fresh = tmp_path / "fresh.db"
    sa.create_engine(f"sqlite:///{fresh.as_posix()}").dispose()  # an empty database file, as a new Neon/Supabase one is
    existing = tmp_path / "existing.db"
    Store(existing).create_role(RoleSetup(role_title="Kept", job_description="J"))

    def fake_test(url):
        tables = set(sa.inspect(sa.create_engine(url)).get_table_names())
        return {"ok": True, "message": "Connected", "database": "PostgreSQL at db.example.com:5432/mia",
                **dbconfig.classify(tables, {}), "has_mia_tables": "schema_version" in tables, "has_data": False, "rows": {}}
    monkeypatch.setattr(dbconfig, "test", fake_test)
    monkeypatch.setattr(dbconfig, "normalise", lambda url: url)
    app, c = make(replace(settings))
    with c:
        checked = c.post("/api/database/test", json={"url": f"sqlite:///{fresh.as_posix()}"}).json()
        assert checked["state"] == "empty" and checked["ready"] and "ready for a new Recruence setup" in checked["summary"]
        assert sa.inspect(sa.create_engine(f"sqlite:///{fresh.as_posix()}")).get_table_names() == []  # the check creates nothing
        r = c.post("/api/setup/database", json={"choice": "postgresql", "url": f"sqlite:///{fresh.as_posix()}", "data": "copy"})
        assert r.status_code == 200 and r.json()["database_state"] == "empty" and r.json()["has_accounts"] is False
        assert set(sa.inspect(sa.create_engine(f"sqlite:///{fresh.as_posix()}")).get_table_names()) == {t.name for t in metadata.sorted_tables}
        assert Store(fresh).ping() == SCHEMA_VERSION

        seen = c.post("/api/database/test", json={"url": f"sqlite:///{existing.as_posix()}"}).json()
        assert seen["state"] == "mia" and "Existing Recruence installation" in seen["summary"]
        refused = c.post("/api/setup/database", json={"choice": "postgresql", "url": f"sqlite:///{existing.as_posix()}"})
        assert refused.status_code == 409 and "Existing Recruence installation" in refused.json()["detail"]  # confirmed, never silently
        kept = c.post("/api/setup/database", json={"choice": "postgresql", "url": f"sqlite:///{existing.as_posix()}", "data": "use_existing"})
        assert kept.status_code == 200 and kept.json()["database_state"] == "mia"
    assert [r.setup.role_title for r in Store(existing).list_roles()] == ["Kept"]  # its records, as they were


def test_a_database_with_accounts_asks_for_sign_in_not_the_server_code(settings, monkeypatch, tmp_path):
    """The desktop app (a request from this computer) connects, during setup, a database that already has accounts
    (another install's Neon or Supabase): setup continues after signing in with one of them. The code from the server
    log, which a desktop user can't see, is only for a browser elsewhere before any account exists."""
    from app.accounts import Accounts

    theirs = tmp_path / "hosted.db"
    company = Store(theirs)
    Accounts(company).create_user("dana@company.example", "Dana", "company-pass-1", is_admin=True)
    company.close()
    monkeypatch.setattr(dbconfig, "test", lambda url: {"ok": True, "message": "Connected", "database": "Hosted database", "state": "mia",
                                                      "summary": "Existing Recruence installation found.", "has_data": False, "has_mia_tables": True, "rows": {}})
    monkeypatch.setattr(dbconfig, "normalise", lambda url: url)
    app, c = make(replace(settings))  # this computer: no setup code needed
    with c:
        first = c.get("/api/setup/status").json()
        assert first["signed_in"] is True and first["code_required"] is False
        c.post("/api/setup/database", json={"choice": "postgresql", "url": f"sqlite:///{theirs.as_posix()}", "data": "use_existing"})
        after = c.get("/api/setup/status").json()  # accounts exist now: this computer is no longer let in by itself
        assert after["signed_in"] is False and after["code_required"] is False and after["sign_in_required"] is True
        assert c.post("/api/auth/account-login", json={"email": "dana@company.example", "password": "company-pass-1"}).status_code == 200
        resumed = c.get("/api/setup/status").json()
        assert resumed["signed_in"] is True and resumed["completed"] is False and resumed["steps"]
    # A browser on another machine, before any account exists: the server's setup code is still required.
    app2, remote = make(replace(settings, data_dir=tmp_path / "server"), client=DOCKER)
    with remote:
        s = remote.get("/api/setup/status").json()
        assert s["code_required"] is True and s["sign_in_required"] is False and s["signed_in"] is False


def test_the_careers_website_step_asks_how_the_site_is_set_up(settings):
    app, c = make(replace(settings), ai=FakeAI())  # this computer: the desktop app's own setup
    with c:
        st = c.get("/api/setup/status").json()
        assert st["order"][-2:] == ["careers", "review"] and "careers" in st["optional"]
        c.post("/api/auth/register", json={"name": "Dana", "email": "dana@example.com", "password": "a-long-test-password-1"})
        st = c.get("/api/setup/status").json()
        assert st["steps"]["careers"] == {"status": "incomplete", "site": ""}
        assert c.put("/api/setup/state", json={"skip": "careers"}).status_code == 200
        assert c.get("/api/setup/status").json()["steps"]["careers"]["status"] == "skipped"
        # Answering it (the same setting as Settings → Recruitment sources) completes the step.
        c.put("/api/workspaces/current", json={"careers_site": "own"})
        assert c.get("/api/setup/status").json()["steps"]["careers"] == {"status": "done", "site": "own"}
