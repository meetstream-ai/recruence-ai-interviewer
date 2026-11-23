"""Tenant isolation across the whole dashboard API: a second account in its own workspace can't read, change or act on
anything of another workspace, through any route that takes a record id, and sees none of it in any list.

The routes come from the app itself, so a route added later is covered without editing this test (a route with a new
kind of id fails the test until it's classified below)."""

import re
from dataclasses import replace

from fastapi.testclient import TestClient

from app.llm import OfflineAI
from app.server import create_app
from app.store import Store
from tests.test_api import TUNNEL
from tests.test_roles import evaluated

SECRET = "Zebra Quantum Platform Lead"  # A's role title: must never appear in B's responses
OWNER = {"name": "Olive Owner", "email": "olive@example.com", "password": "owner-password-1", "workspace_name": "Acme"}
OTHER = {"name": "Bea Other", "email": "bea@example.com", "password": "other-password-1", "workspace_name": "Bea Co"}
ZOOM = "https://zoom.us/j/1234567890"
# Ids that aren't workspace records (installation settings, saved connections, settings checks): not this test's subject.
NOT_RECORDS = {"profile_id", "key", "check"}


def workspace_a(c, app):
    c.post("/api/auth/register", json=OWNER)
    role = c.post("/api/roles", json={"role_title": SECRET, "job_description": "Lead a zebra platform."}).json()["id"]
    plan = c.post(f"/api/roles/{role}/plan/generate").json()["role"]["plan"]
    c.post(f"/api/roles/{role}/plan/approve")
    hiring = c.put(f"/api/roles/{role}/hiring", json={"meeting_link": ZOOM, "published": True}).json()
    interview = c.post(f"/api/roles/{role}/candidates", json={"candidate_name": "Ana Silva", "candidate_email": "ana@example.com"}).json()["id"]
    c.post(f"/api/public/apply/{hiring['careers']['job_key']}", headers=TUNNEL,
           data={"full_name": "Lee Park", "email": "lee@example.com", "consent": "yes"}, files={"resume": ("cv.txt", b"Lee Park. Python.", "text/plain")})
    store = app.state.store
    application = store.all_applications()[0].id
    evaluated(store, interview, "Ana Silva", [4, 3], "meets", [x["id"] for x in plan["competencies"]])
    session = store.get_interview(interview).latest_session_id
    integration = c.post("/api/integrations", json={"provider": "generic", "name": "A's ATS", "secrets": {"webhook_secret": "a" * 24}}).json()["id"]
    member = c.get("/api/workspaces/current").json()["members"][0]["id"]
    return {"role_id": role, "interview_id": interview, "application_id": application, "session_id": session,
            "integration_id": integration, "user_id": member}


def test_another_workspace_can_reach_nothing(settings):
    s = replace(settings)
    app = create_app(settings=s, store=Store(s.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as a:
        ids = workspace_a(a, app)
        b = TestClient(app, client=("172.17.0.1", 50000))  # another browser, another account
        r = b.post("/api/auth/register", json=OTHER)
        assert r.status_code == 200 and r.json()["workspace_id"]
        assert b.get("/api/workspaces/current").json()["name"] == "Bea Co"

        checked, leaks, statuses = 0, [], []
        for route in app.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/api/") or path.startswith("/api/public/") or "{" not in path:
                continue
            names = set(re.findall(r"{(\w+)}", path))
            if names & NOT_RECORDS:
                continue
            assert names <= set(ids), f"classify the id in {path}"
            url = path.format(**ids)
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                checked += 1
                if path.endswith("/events"):  # a live event stream: only its status is read
                    with b.stream(method, url) as resp:
                        status, text = resp.status_code, ""
                else:
                    resp = b.request(method, url, json={})
                    status, text = resp.status_code, resp.text
                statuses.append(status)
                if status < 400 or SECRET in text or "ana@example.com" in text or "lee@example.com" in text:
                    leaks.append(f"{method} {path} -> {status}")
        assert checked >= 45 and leaks == [], leaks
        # Refused because the record isn't B's (404), not because B wasn't signed in (401).
        assert 401 not in statuses and statuses.count(404) >= 0.6 * len(statuses), sorted(set(statuses))

        # Lists and overviews: B sees only B.
        for url in ("/api/roles", "/api/interviews", "/api/candidates", "/api/integrations", "/api/insights/roles",
                    "/api/activity", "/api/audit", "/api/me", "/api/workspaces/current"):
            resp = b.get(url)
            assert resp.status_code in (200, 403), url
            text = resp.text
            assert SECRET not in text and "ana@example.com" not in text and "lee@example.com" not in text, url
            for value in ids.values():
                if value and len(value) >= 8:
                    assert value not in text, (url, value)
        # And A still has everything.
        assert [r["role_title"] for r in a.get("/api/roles").json()] == [SECRET]
        assert a.get(f"/api/interviews/{ids['interview_id']}").status_code == 200
