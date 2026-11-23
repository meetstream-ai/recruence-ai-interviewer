"""Accounts, workspaces and switching databases: sign-in, isolation between workspaces, joining with approval,
roles, and connecting another database without anything leaking across."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import dbconfig
from app.accounts import Accounts, hash_password, verify_password
from app.llm import OfflineAI
from app.server import create_app
from app.store import Store

DOCKER = ("172.17.0.1", 50000)
ROLE = {"role_title": "Backend Engineer", "job_description": "Python APIs."}
OWNER = {"name": "Olive Owner", "email": "Olive@Example.com", "password": "owner-password-1", "workspace_name": "Acme Hiring"}


def make(settings, store=None):
    app = create_app(settings=settings, store=store or Store(settings.database), ai=OfflineAI(), run_loops=False)
    return app, TestClient(app)


def remote(app):
    return TestClient(app, client=DOCKER)


def signup(c, name, email, password="member-password-1"):
    r = c.post("/api/auth/register", json={"name": name, "email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def test_passwords_are_hashed_with_a_salt():
    h = hash_password("s3cret-passw0rd")
    assert h.startswith("scrypt$") and "s3cret" not in h and h != hash_password("s3cret-passw0rd")
    assert verify_password("s3cret-passw0rd", h) and not verify_password("wrong", h) and not verify_password("x", "garbage")


def test_first_account_owns_the_first_workspace_and_then_everyone_signs_in(settings):
    app, c = make(replace(settings))
    with c:
        real = c.post("/api/roles", json=ROLE).json()["id"]  # made before accounts existed
        first = c.post("/api/auth/register", json=OWNER).json()
        assert first["user"]["is_admin"] is True and first["user"]["email"] == "olive@example.com"
        ws = c.get("/api/workspaces/current").json()
        assert ws["name"] == "Acme Hiring" and ws["role"] == "owner"
        assert [r["id"] for r in c.get("/api/roles").json()] == [real]  # older records belong to the first workspace
        # From now on even this machine signs in.
        anon = TestClient(app)
        assert anon.get("/api/roles").status_code == 401
        assert anon.post("/api/auth/account-login", json={"email": "olive@example.com", "password": "nope"}).status_code == 401
        assert anon.post("/api/auth/account-login", json={"email": "OLIVE@example.com", "password": OWNER["password"]}).status_code == 200
        assert anon.get("/api/roles").status_code == 200
        anon.post("/api/auth/logout")
        assert anon.get("/api/roles").status_code == 401
        # Someone else can only bootstrap an empty database, never take over this one.
        later = signup(remote(app), "Max Member", "max@example.com")
        assert later["user"]["is_admin"] is False and later["workspace_id"] not in (None, ws["id"])  # a workspace of their own


def test_too_many_wrong_passwords_are_refused_for_a_while(settings):
    app, c = make(replace(settings))
    with c:
        c.post("/api/auth/register", json=OWNER)
        other = TestClient(app)
        for _ in range(10):
            other.post("/api/auth/account-login", json={"email": "olive@example.com", "password": "wrong-one"})
        assert other.post("/api/auth/account-login", json={"email": "olive@example.com", "password": OWNER["password"]}).status_code == 429


def test_changing_the_password_signs_other_browsers_out(settings):
    app, c = make(replace(settings))
    with c:
        c.post("/api/auth/register", json=OWNER)
        phone = TestClient(app)
        phone.post("/api/auth/account-login", json={"email": "olive@example.com", "password": OWNER["password"]})
        assert c.post("/api/me/password", json={"current": "wrong", "new": "brand-new-pass"}).status_code == 422
        assert c.post("/api/me/password", json={"current": OWNER["password"], "new": "brand-new-pass"}).status_code == 200
        assert phone.get("/api/roles").status_code == 401 and c.get("/api/roles").status_code == 200


def test_joining_takes_a_code_and_the_owners_approval(settings):
    app, owner = make(replace(settings))
    with owner:
        owner.post("/api/auth/register", json=OWNER)
        rid = owner.post("/api/roles", json=ROLE).json()["id"]
        member = remote(app)
        signup(member, "Max Member", "max@example.com")
        assert member.post("/api/workspaces/join", json={"code": "AAAA-BBBB-CCCC"}).status_code == 422
        code = owner.post("/api/workspaces/current/code", json={"days": 7}).json()["join_code"]
        r = member.post("/api/workspaces/join", json={"code": code.lower().replace("-", " ")}).json()
        assert r["status"] == "pending"
        assert member.post("/api/workspaces/join", json={"code": code}).status_code == 422  # one request at a time
        # Pending: nothing of the workspace is visible; what they create lands in their own workspace.
        assert member.get("/api/roles").json() == [] and member.get(f"/api/roles/{rid}").status_code == 404
        theirs = member.post("/api/roles", json=ROLE).json()["id"]
        assert [r["id"] for r in owner.get("/api/roles").json()] == [rid] and owner.get(f"/api/roles/{theirs}").status_code == 404
        mine = {w["name"]: w["status"] for w in member.get("/api/me").json()["workspaces"]}
        assert mine == {"Acme Hiring": "pending", "Max's workspace": "approved"}
        uid = next(m["id"] for m in owner.get("/api/workspaces/current").json()["members"] if m["email"] == "max@example.com")
        # They can't approve themselves into the team's workspace (their requests only act on their own one).
        assert member.post(f"/api/workspaces/current/members/{uid}/decision", json={"approve": True}).status_code in (403, 409)
        assert {w["name"]: w["status"] for w in member.get("/api/me").json()["workspaces"]}["Acme Hiring"] == "pending"
        owner.post(f"/api/workspaces/current/members/{uid}/decision", json={"approve": True, "role": "recruiter"})
        ws_id = owner.get("/api/workspaces/current").json()["id"]
        assert member.post("/api/workspaces/switch", json={"workspace_id": ws_id}).status_code == 200
        assert [r["id"] for r in member.get("/api/roles").json()] == [rid]
        # A recruiter can work with roles but not delete them or change installation settings.
        assert member.delete(f"/api/roles/{rid}").status_code == 403
        assert member.put("/api/config", json={"changes": {"app_name": "X"}}).status_code == 403
        assert member.post("/api/workspaces/current/code", json={"days": 1}).status_code == 403
        # Removed: out at once, in every open browser.
        owner.delete(f"/api/workspaces/current/members/{uid}")
        assert member.get("/api/roles").json() == [] and member.get(f"/api/roles/{rid}").status_code == 404
        # A revoked code no longer works.
        owner.delete("/api/workspaces/current/code")
        assert member.post("/api/workspaces/join", json={"code": code}).status_code == 422


def test_a_rejected_request_grants_nothing(settings):
    app, owner = make(replace(settings))
    with owner:
        owner.post("/api/auth/register", json=OWNER)
        member = remote(app)
        signup(member, "Rita Rejected", "rita@example.com")
        code = owner.post("/api/workspaces/current/code", json={"days": None}).json()["join_code"]
        member.post("/api/workspaces/join", json={"code": code})
        uid = next(m["id"] for m in owner.get("/api/workspaces/current").json()["members"] if m["status"] == "pending")
        owner.post(f"/api/workspaces/current/members/{uid}/decision", json={"approve": False})
        ws_id = owner.get("/api/workspaces/current").json()["id"]
        assert member.post("/api/workspaces/switch", json={"workspace_id": ws_id}).status_code == 403
        assert {w["name"]: w["status"] for w in member.get("/api/me").json()["workspaces"]} == {"Acme Hiring": "rejected", "Rita's workspace": "approved"}


def test_workspaces_never_see_each_others_records(settings):
    app, c = make(replace(settings))
    with c:
        c.post("/api/auth/register", json=OWNER)
        a = c.get("/api/workspaces/current").json()["id"]
        role_a = c.post("/api/roles", json=ROLE).json()["id"]
        cand = c.post(f"/api/roles/{role_a}/plan/generate").json()
        c.post(f"/api/roles/{role_a}/plan/approve")
        iid = c.post(f"/api/roles/{role_a}/candidates", json={"candidate_name": "Ana Silva"}).json()["id"]
        b = c.post("/api/workspaces", json={"name": "Second company"}).json()["id"]  # creating one switches to it
        assert c.get("/api/roles").json() == [] and c.get("/api/interviews").json() == []
        assert c.get(f"/api/roles/{role_a}").status_code == 404 and c.get(f"/api/interviews/{iid}").status_code == 404
        assert c.put(f"/api/roles/{role_a}/hiring", json={"passing_score": 80}).status_code == 404
        assert c.delete(f"/api/interviews/{iid}").status_code == 404
        role_b = c.post("/api/roles", json={**ROLE, "role_title": "Designer"}).json()["id"]
        log = c.get("/api/audit").json()
        assert log and all(e["workspace_id"] == b for e in log)  # the audit log is per workspace too
        c.post("/api/workspaces/switch", json={"workspace_id": a})
        assert [r["id"] for r in c.get("/api/roles").json()] == [role_a] and c.get(f"/api/interviews/{iid}").status_code == 200
        assert c.get(f"/api/roles/{role_b}").status_code == 404
        assert cand  # the plan was generated in workspace A
        # Not a member: can't switch into someone else's workspace.
        outsider = remote(app)
        signup(outsider, "Otto Outsider", "otto@example.com")
        assert outsider.post("/api/workspaces/switch", json={"workspace_id": a}).status_code == 403


def test_every_account_creates_workspaces(settings):
    """Workspaces are a standard capability: every account has its own and can create more (up to a limit)."""
    from app import accounts as accounts_module
    app, owner = make(replace(settings))
    with owner:
        owner.post("/api/auth/register", json=OWNER)
        member = remote(app)
        signup(member, "Max Member", "max@example.com")
        made = member.post("/api/workspaces", json={"name": "Side project"})
        assert made.status_code == 201 and made.json()["role"] == "owner"
        assert {w["name"] for w in member.get("/api/me").json()["workspaces"]} == {"Max's workspace", "Side project"}
        assert "Side project" not in {w["name"] for w in owner.get("/api/me").json()["workspaces"]}
        # The old "anyone can create" rule is gone (an older client sending it changes nothing).
        assert owner.put("/api/accounts/rules", json={"anyone_can_create_workspaces": False}).json() == {"open_registration": True}
        assert member.post("/api/workspaces", json={"name": "Another"}).status_code == 201
        accounts_module.MAX_OWNED_WORKSPACES, old = 3, accounts_module.MAX_OWNED_WORKSPACES
        try:
            assert member.post("/api/workspaces", json={"name": "One too many"}).status_code == 422
        finally:
            accounts_module.MAX_OWNED_WORKSPACES = old


def test_switching_databases_signs_everyone_out_and_shares_nothing(settings, tmp_path, monkeypatch):
    if settings.database_url:
        pytest.skip("the switch target here is a SQLite file")
    second = tmp_path / "second.db"
    monkeypatch.setattr(dbconfig, "normalise", lambda url: url)
    monkeypatch.setattr(dbconfig, "test", lambda url: {"ok": True, "message": "ok", "database": "Second database",
                                                        "has_mia_tables": False, "has_data": False, "rows": {}})
    s = replace(settings)
    app, c = make(s)
    with c:
        c.post("/api/auth/register", json=OWNER)
        rid = c.post("/api/roles", json=ROLE).json()["id"]
        other_browser = remote(app)
        other_browser.post("/api/auth/account-login", json={"email": "olive@example.com", "password": OWNER["password"]})
        stranger = remote(app)
        assert stranger.post("/api/connection/connect", json={"url": f"sqlite:///{second.as_posix()}"}).status_code == 403
        # Switch: signed out, with a pass for the connection screen.
        assert c.post("/api/connection/switch-start").status_code == 200
        assert c.get("/api/roles").status_code == 401
        r = c.post("/api/connection/connect", json={"url": f"sqlite:///{second.as_posix()}", "name": "Second"})
        assert r.status_code == 200 and r.json()["has_accounts"] is False
        assert other_browser.get("/api/roles").status_code in (401, 403)  # every session of the old database ended
        # The old account doesn't exist here; the pass allows creating this database's first account.
        assert c.post("/api/auth/account-login", json={"email": "olive@example.com", "password": OWNER["password"]}).status_code == 401
        c.post("/api/auth/register", json={"name": "Sam Second", "email": "sam@example.com", "password": "second-pass-1"})
        assert c.get("/api/roles").json() == [] and c.get(f"/api/roles/{rid}").status_code == 404
        profiles = c.get("/api/connection/status").json()["profiles"]
        assert {p["name"] for p in profiles} == {"Local database (SQLite)", "Second"}
        assert "second.db" not in str(profiles)  # connection details never leave the server
        # Back to the first database: its data and accounts are as they were.
        c.post("/api/connection/switch-start")
        assert c.post("/api/connection/connect", json={"profile_id": "local"}).status_code == 200
        assert c.post("/api/auth/account-login", json={"email": "olive@example.com", "password": OWNER["password"]}).status_code == 200
        assert [r["id"] for r in c.get("/api/roles").json()] == [rid]
    assert Accounts(Store(second)).count() == 1 and Accounts(Store(s.db_path)).count() == 1


def test_the_connection_code_from_the_log_lets_someone_reconnect(settings, caplog):
    app, c = make(replace(settings))
    with c:
        c.post("/api/auth/register", json=OWNER)
        anon = remote(app)
        assert anon.get("/api/connection/status").json().get("current") is None  # where the database is stays private
        assert anon.post("/api/connection/request-code").json() == {"logged": True}
        code = caplog.text.split("valid 15 minutes): ")[-1].split()[0]
        assert anon.post("/api/connection/unlock", json={"code": "0000-0000"}).status_code == 401
        assert anon.post("/api/connection/unlock", json={"code": code}).status_code == 200
        assert anon.get("/api/connection/status").json()["allowed"] is True
        assert anon.get("/api/roles").status_code == 401  # the pass is for the connection screen only


def test_demo_data_lives_in_one_workspace(settings):
    app, c = make(replace(settings))
    with c:
        c.post("/api/auth/register", json=OWNER)
        assert c.post("/api/demo").json()["installed"] is True
        assert len(c.get("/api/interviews").json()) == 12
        c.post("/api/workspaces", json={"name": "Clean workspace"})
        assert c.get("/api/interviews").json() == [] and c.get("/api/demo").json()["installed"] is False
        assert c.post("/api/demo").status_code == 409  # one copy per database


def test_admins_manage_accounts_and_registration(settings):
    app, owner = make(replace(settings))
    with owner:
        owner.post("/api/auth/register", json=OWNER)
        member = remote(app)
        uid = signup(member, "Max Member", "max@example.com")["user"]["id"]
        assert member.get("/api/accounts").status_code == 403  # not an installation admin
        assert member.post(f"/api/accounts/{uid}/temporary-password").status_code == 403
        users = owner.get("/api/accounts").json()
        assert {u["email"] for u in users["users"]} == {"olive@example.com", "max@example.com"} and users["open_registration"] is True
        # A temporary password: shown once, the old one stops working, every browser of theirs is signed out.
        temp = owner.post(f"/api/accounts/{uid}/temporary-password").json()["password"]
        assert member.get("/api/me").status_code == 401
        assert member.post("/api/auth/account-login", json={"email": "max@example.com", "password": "member-password-1"}).status_code == 401
        assert member.post("/api/auth/account-login", json={"email": "max@example.com", "password": temp}).status_code == 200
        me = owner.get("/api/me").json()["user"]["id"]
        assert owner.post(f"/api/accounts/{me}/temporary-password").status_code == 409  # your own: change it yourself
        # The last installation admin can't be removed.
        assert owner.put(f"/api/accounts/{me}/admin", json={"is_admin": False}).status_code == 409
        owner.put(f"/api/accounts/{uid}/admin", json={"is_admin": True})
        assert owner.put(f"/api/accounts/{me}/admin", json={"is_admin": False}).status_code == 200
        # Registration off: nobody new can create an account.
        assert member.put("/api/accounts/rules", json={"open_registration": False}).json() == {"open_registration": False}
        r = remote(app).post("/api/auth/register", json={"name": "Late Larry", "email": "larry@example.com", "password": "larry-pass-123"})
        assert r.status_code == 403


def test_workspace_settings_stay_in_their_workspace(settings):
    """Report-ready recipients and candidate notices are per workspace; the installation's settings (/api/config)
    are one set for everyone and only installation admins see them."""
    app, c = make(replace(settings))
    with c:
        c.post("/api/auth/register", json=OWNER)
        a = c.get("/api/workspaces/current").json()
        c.put("/api/workspaces/current", json={"notify_emails": ["hiring-a@example.com"], "candidate_notices": True})
        c.post("/api/workspaces", json={"name": "Second company"})  # creating one switches to it
        b = c.get("/api/workspaces/current").json()
        assert b["id"] != a["id"]
        assert "hiring-a@example.com" not in str(b) and b["candidate_notices"] is False
        c.post("/api/workspaces/switch", json={"workspace_id": a["id"]})
        again = c.get("/api/workspaces/current").json()
        assert again["notify_emails"] == ["hiring-a@example.com"] and again["candidate_notices"] is True
        member = remote(app)
        signup(member, "Max Member", "max@example.com")
        assert member.get("/api/config").status_code == 403  # installation settings: installation admins only


def test_on_the_server_itself_the_database_can_be_changed_without_a_code(settings):
    """Signed out, on the computer running Recruence: the connection screen opens with no code from the log."""
    app, c = make(replace(settings))
    with c:
        c.post("/api/auth/register", json=OWNER)
        c.post("/api/auth/logout")
        here = TestClient(app)  # a direct local request, as from the desktop app or a browser on the server
        assert here.get("/api/connection/status").json()["allowed"] is True
        assert remote(app).get("/api/connection/status").json()["allowed"] is False  # elsewhere it still takes a code
        assert here.post("/api/connection/test", json={"url": "postgresql://u:p@127.0.0.1:1/x"}).status_code == 200
        tunnelled = TestClient(app).get("/api/connection/status", headers={"X-Forwarded-For": "34.230.88.153"})
        assert tunnelled.status_code == 403 or tunnelled.json()["allowed"] is False  # a public address is never "here"


def test_the_first_connection_code_is_given_even_just_after_the_machine_starts(monkeypatch, caplog):
    """Regression: monotonic time counts from boot, so a 60-second throttle that started at 0 refused the very first
    code on a machine up for under a minute (seen on a fresh CI runner)."""
    from app import workspace_api
    from app.workspace_api import ConnectionGate
    clock = {"now": 5.0}
    monkeypatch.setattr(workspace_api.time, "monotonic", lambda: clock["now"])
    gate = ConnectionGate()
    assert gate.request_code() is True
    assert gate.request_code() is False  # a second one within a minute is still refused
    clock["now"] += 61
    assert gate.request_code() is True
