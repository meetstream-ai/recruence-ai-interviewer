"""A database belongs to the installation and account that set it up (app/store.py database_owner): the first account
claims it atomically; other installations' accounts need a joining code; the same database is recognised however its
connection string is written; existing databases are adopted without changing their data."""

import threading
from dataclasses import replace

import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import dbconfig
from app.accounts import Accounts
from app.llm import OfflineAI
from app.server import create_app
from app.store import Store, database_owner, users

OWNER = {"name": "Olive Owner", "email": "olive@example.com", "password": "owner-password-1", "workspace_name": "Acme"}
DOCKER = ("172.17.0.1", 50000)


def install(settings, tmp_path, name, db):
    """One Recruence installation (its own data folder, so its own installation id) on the database file `db`."""
    s = replace(settings, data_dir=tmp_path / name)
    app = create_app(settings=s, store=Store(db), ai=OfflineAI(), run_loops=False)
    return app, TestClient(app)


def test_the_first_account_owns_the_database_and_its_installation_is_recorded(settings, tmp_path):
    db = tmp_path / "shared.db"
    app, c = install(settings, tmp_path, "a", db)
    with c:
        assert c.post("/api/auth/register", json=OWNER).status_code == 200
        ident = app.state.store.database_identity()
        olive = Accounts(app.state.store).user_by_email("olive@example.com")
        assert ident["owner_user_id"] == olive["id"] and ident["owner_install_id"] == dbconfig.installation_id(tmp_path / "a")
        o = c.get("/api/database/ownership").json()
        assert o["owner"] == {"name": "Olive Owner", "email": "olive@example.com"} and o["owner_installation_is_this_one"] is True
        assert [i["this_one"] for i in o["installations"]] == [True]
        assert "password" not in str(o).lower() and "sqlite:///" not in str(o)


def test_two_registrations_at_once_cant_both_own_an_empty_database(tmp_path):
    db = tmp_path / "race.db"
    Store(db).close()
    stores = [Store(db) for _ in range(6)]
    barrier = threading.Barrier(len(stores))
    winners = []

    def claim(n):
        barrier.wait()
        row = Accounts(stores[n]).create_owner(f"u{n}@example.com", f"User {n}", "a-strong-password-1", f"install-{n}")
        if row is not None:
            winners.append(row["id"])

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(len(stores))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check = Store(db)
    with check._engine.connect() as c:
        admins = c.execute(sa.select(sa.func.count()).select_from(users).where(users.c.is_admin.is_(True))).scalar()
        total = c.execute(sa.select(sa.func.count()).select_from(users)).scalar()
        owner = c.execute(sa.select(database_owner.c.owner_user_id)).scalar()
    assert len(winners) == 1 and admins == 1 and total == 1 and owner == winners[0]  # the losers left nothing behind
    for s in [*stores, check]:
        s.close()


def test_another_installation_needs_a_joining_code_and_sees_nothing_without_approval(settings, tmp_path):
    db = tmp_path / "victim.db"
    app_a, a = install(settings, tmp_path, "a", db)
    with a:
        a.post("/api/auth/register", json=OWNER)
        rid = a.post("/api/roles", json={"role_title": "Backend Engineer", "job_description": "Python."}).json()["id"]
        code = a.post("/api/workspaces/current/code", json={"days": 7}).json()["join_code"]
    # Someone else's computer, pointed at the same database with its connection string.
    app_b, b = install(settings, tmp_path, "b", db)
    with b:
        r = b.post("/api/auth/register", json={"name": "Mallory", "email": "mallory@example.com", "password": "mallory-password-1"})
        assert r.status_code == 403 and "joining code" in r.json()["detail"]
        assert b.post("/api/auth/register", json={"name": "Mallory", "email": "mallory@example.com", "password": "mallory-password-1",
                                                  "join_code": "AAAA-BBBB-CCCC"}).status_code == 422
        assert Accounts(app_b.state.store).user_by_email("mallory@example.com") is None
        # Becoming owner isn't possible either: the database is claimed.
        assert app_b.state.store.database_identity()["owner_install_id"] == dbconfig.installation_id(tmp_path / "a")
        # With a real code: an account and a request the owner decides on; until then nothing of the workspace.
        r = b.post("/api/auth/register", json={"name": "Max Member", "email": "max@example.com", "password": "member-password-1", "join_code": code})
        assert r.status_code == 200 and r.json()["join_requested"] == "Acme"
        assert b.get("/api/workspaces/current").json()["name"] == "Max's workspace"  # their own, empty, until approved
        assert b.get("/api/roles").json() == [] and b.get(f"/api/roles/{rid}").status_code == 404
        assert b.post("/api/setup/existing-account").status_code in (403, 409)  # can't finish setup on this database yet
        assert b.get("/api/database/ownership").status_code == 403  # installation admins only


def test_finishing_setup_with_an_existing_account_needs_an_admin_or_a_member(settings, tmp_path, monkeypatch):
    db = tmp_path / "company.db"
    company = Store(db)
    acc = Accounts(company)
    acc.create_owner("dana@company.example", "Dana", "company-pass-1", "install-company")
    stray = acc.create_user("stray@example.com", "Stray", "stray-password-1")
    company.close()
    monkeypatch.setattr(dbconfig, "test", lambda url: {"ok": True, "message": "Connected", "database": "Company", "state": "mia",
                                                      "summary": "Existing Recruence installation found.", "has_mia_tables": True, "has_data": False,
                                                      "rows": {}, "database_id": "x", "owned": True})
    monkeypatch.setattr(dbconfig, "normalise", lambda url: url)
    app, c = install(settings, tmp_path, "new", tmp_path / "local.db")
    with c:
        c.post("/api/setup/database", json={"choice": "postgresql", "url": f"sqlite:///{db.as_posix()}", "data": "use_existing"})
        c.post("/api/auth/account-login", json={"email": "stray@example.com", "password": "stray-password-1"})
        r = c.post("/api/setup/existing-account")
        assert r.status_code == 403 and "isn't an administrator or a member" in r.json()["detail"]
        assert c.get("/api/setup/status").json()["completed"] is False
        c.post("/api/auth/account-login", json={"email": "dana@company.example", "password": "company-pass-1"})
        assert c.post("/api/setup/existing-account").json() == {"completed": True}
    assert stray["is_admin"] is False


def test_an_older_database_is_adopted_without_changing_its_data(settings, tmp_path):
    """Accounts made before ownership was recorded: the earliest admin becomes the owner (nothing else changes);
    other installations need a code until an admin signs in, which records their installation as the owner's."""
    db = tmp_path / "old.db"
    s = Store(db)
    acc = Accounts(s)
    first = acc.create_user("first@example.com", "First Admin", "first-password-1", is_admin=True)
    acc.create_user("later@example.com", "Later Admin", "later-password-1", is_admin=True)
    with s._engine.begin() as c:  # as an older version left it: no owner row
        c.execute(database_owner.delete())
    s.close()
    reopened = Store(db)  # the schema step makes the row, nothing else
    ident = reopened.database_identity()
    assert ident["owner_user_id"] == first["id"] and ident["owner_install_id"] is None
    assert {u.user_by_email(e)["name"] for u in [Accounts(reopened)] for e in ("first@example.com", "later@example.com")} == {"First Admin", "Later Admin"}
    reopened.close()
    app, c = install(settings, tmp_path, "server", db)
    with c:
        r = c.post("/api/auth/register", json={"name": "Nobody", "email": "nobody@example.com", "password": "nobody-password-1"})
        assert r.status_code == 403  # not recorded yet: a code is needed
        assert c.post("/api/auth/account-login", json={"email": "first@example.com", "password": "first-password-1"}).status_code == 200
        assert app.state.store.database_identity()["owner_install_id"] == dbconfig.installation_id(tmp_path / "server")
        assert c.post("/api/auth/logout").status_code == 200
        r = c.post("/api/auth/register", json={"name": "Colleague", "email": "colleague@example.com", "password": "colleague-password-1"})
        assert r.status_code == 200  # the owner's own installation: its registration setting applies as before
    with TestClient(app) as admin:
        admin.post("/api/auth/account-login", json={"email": "first@example.com", "password": "first-password-1"})
        names = [u["email"] for u in admin.get("/api/database/ownership").json()["accounts_without_workspace"]]
        assert "colleague@example.com" in names  # listed for review, not removed


def test_the_same_database_is_recognised_however_the_string_is_written(tmp_path):
    key = dbconfig.connection_key
    assert key("postgresql://mia:pw-1@DB.Example.com/mia?sslmode=require") == key("postgres://mia:other@db.example.com:5432/mia")
    assert key("postgresql+psycopg://mia:pw@db.example.com:5432/mia") == key("postgresql://mia@db.example.com/mia")
    assert key("postgresql://mia@db.example.com/mia") != key("postgresql://mia@db.example.com/other")
    assert key("postgresql://mia@db.example.com:6543/mia") != key("postgresql://mia@db.example.com/mia")
    assert "pw" not in key("postgresql://mia:pw-secret@db.example.com/mia")  # no credentials in it
    from app.secret_box import load_box
    box = load_box(tmp_path)
    a = dbconfig.save_profile(tmp_path, box, "Company", "postgresql://mia:pw@db.example.com/mia?sslmode=require")
    b = dbconfig.save_profile(tmp_path, box, "Company again", "postgres://mia:pw@DB.example.com:5432/mia")
    assert a == b and len(dbconfig.profiles(tmp_path)) == 1  # one saved connection per database
    c = dbconfig.save_profile(tmp_path, box, "Pooler", "postgresql://mia.ref:pw@pooler.example.com:6543/mia", database_id="db-1")
    d = dbconfig.save_profile(tmp_path, box, "Direct", "postgresql://mia:pw@direct.example.com/mia", database_id="db-1")
    assert c == d  # a different address, the same database (its own id)
    e = dbconfig.save_profile(tmp_path, box, "Other", "postgresql://mia:pw@other.example.com/mia", database_id="db-2")
    assert e not in (a, c)


def test_separate_databases_stay_separate(settings, tmp_path):
    one, two = Store(tmp_path / "one.db"), Store(tmp_path / "two.db")
    assert one.database_identity()["database_id"] != two.database_identity()["database_id"]
    assert Accounts(one).create_owner("a@example.com", "A", "a-password-12", "i-1") is not None
    assert Accounts(two).create_owner("b@example.com", "B", "b-password-12", "i-2") is not None  # each its own owner
    one.close(), two.close()
