"""Shared use needs a hosted database: a team installation, joining codes and more than one account need PostgreSQL;
the local SQLite file is for one person. Enforced on the server, whatever the UI shows."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.llm import OfflineAI
from app.server import create_app
from app.store import Store

DOCKER = ("172.17.0.1", 50000)
OWNER = {"name": "Olive Owner", "email": "olive@example.com", "password": "owner-password-1"}


@pytest.fixture
def solo(settings):
    if settings.database_url:
        pytest.skip("about the local SQLite database")
    return replace(settings, allow_shared_sqlite=False)


class FakeAI(OfflineAI):
    is_ai = True
    name = "gemini:test-model"


def test_a_team_install_cant_use_the_local_database(solo):
    app = create_app(settings=solo, store=Store(solo.database), ai=FakeAI(), run_loops=False)
    with TestClient(app) as c:
        c.put("/api/setup/state", json={"mode": "team"})
        r = c.post("/api/setup/database", json={"choice": "current"})
        assert r.status_code == 409 and "hosted PostgreSQL" in r.json()["detail"]
        assert c.get("/api/setup/status").json()["steps"]["database"]["status"] == "incomplete"
        c.post("/api/auth/register", json=OWNER)
        done = c.post("/api/setup/complete")
        assert done.status_code == 422 and "hosted PostgreSQL" in done.json()["detail"]
        # Switching back to the local database later is refused too.
        assert c.post("/api/connection/connect", json={"profile_id": "local"}).status_code == 409


def test_a_personal_install_on_sqlite_is_one_person(solo):
    app = create_app(settings=solo, store=Store(solo.database), ai=FakeAI(), run_loops=False)
    with TestClient(app) as c:
        c.put("/api/setup/state", json={"mode": "personal"})
        assert c.post("/api/setup/database", json={"choice": "current"}).status_code == 200
        c.post("/api/auth/register", json=OWNER)
        assert c.post("/api/setup/complete").json()["completed"] is True  # fine for one person
        ws = c.get("/api/workspaces/current").json()
        assert "hosted PostgreSQL" in ws["sharing_problem"]
        code = c.post("/api/workspaces/current/code", json={"days": 1})
        assert code.status_code == 409 and "hosted PostgreSQL" in code.json()["detail"]
        other = TestClient(app, client=DOCKER).post("/api/auth/register", json={"name": "Max", "email": "max@example.com",
                                                                                  "password": "member-password-1"})
        assert other.status_code == 409  # no second account on a one-person database
        assert c.post("/api/roles", json={"role_title": "R", "job_description": "J"}).status_code == 201  # recruiting works
