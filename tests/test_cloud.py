"""The cloud backend (docs/CLOUD.md): Recruence behind an HTTPS proxy with REMOTE_DASHBOARD on, used by the desktop app
in cloud mode, with no desktop app or recruiter's computer involved in anything candidates or MeetStream do.

Every request here arrives the way it does on a cloud host: through the proxy, with X-Forwarded-For and
X-Forwarded-Proto. MeetStream and the careers site are fakes; nothing calls a paid API."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.models import utcnow
from app.security import configure_remote
from app.server import create_app, remote_problems
from app.store import Store
from tests.fakes import FakeMeetStream, event, sentence
from tests.test_api import wait_for
from tests.test_recruiting import CRITERIA, ScreeningAI
from tests.test_resume import make_pdf
from tests.test_tenant_isolation import OWNER

MEET = "https://meet.google.com/abc-defg-hij"
RECRUITER = {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "https"}  # the recruiter's desktop app, via the proxy
CAREERS = {"X-Forwarded-For": "203.0.113.20", "X-Forwarded-Proto": "https"}  # a careers site's server
MEETSTREAM = {"X-Forwarded-For": "34.230.88.153", "X-Forwarded-Proto": "https"}  # MeetStream's callbacks


@pytest.fixture(autouse=True)
def _remote_off_afterwards():
    yield
    configure_remote(False)  # the switch is per process: leave it off for the other tests


def cloud(settings):
    return replace(settings, remote_dashboard=True, settings_encryption_key=Fernet.generate_key().decode())


def make(s, fake=None):
    app = create_app(settings=s, store=Store(s.database), ai=ScreeningAI(), meetstream_client_factory=lambda: fake or FakeMeetStream(),
                     run_loops=False)
    return app


def owner_session(app):
    """The database already has its owner (created on the server itself), as the user's Neon database does."""
    with TestClient(app) as local:
        assert local.post("/api/auth/register", json=OWNER).status_code == 200


def signed_in(app):
    c = TestClient(app, base_url="https://testserver", headers=RECRUITER)
    r = c.post("/api/auth/account-login", json={"email": OWNER["email"], "password": OWNER["password"]})
    assert r.status_code == 200, r.text
    return c, r


def publish(c, title):
    rid = c.post("/api/roles", json={"role_title": title, "job_description": f"{title}: build things."}).json()["id"]
    c.post(f"/api/roles/{rid}/plan/generate")
    assert c.post(f"/api/roles/{rid}/plan/approve").status_code == 200
    r = c.put(f"/api/roles/{rid}/hiring", json={"meeting_link": MEET, "published": True, "criteria": CRITERIA, "resume_review": "manual"})
    assert r.status_code == 200, r.text
    return rid


# ---------- the backend refuses to run as a cloud backend without what that needs ----------

def test_a_cloud_backend_needs_postgres_its_key_and_https(settings):
    s = replace(settings, remote_dashboard=True, database_url="", settings_encryption_key="", public_base_url="http://x.example.com")
    problems = " | ".join(remote_problems(s))
    assert "DATABASE_URL" in problems and "SETTINGS_ENCRYPTION_KEY" in problems and "https" in problems
    with pytest.raises(RuntimeError, match="REMOTE_DASHBOARD is on"):
        create_app(settings=s, ai=ScreeningAI(), run_loops=False)  # no store handed over: it would open SQLite
    ok = replace(settings, remote_dashboard=True, database_url="postgresql://u:p@db.example.com/neondb",
                 settings_encryption_key=Fernet.generate_key().decode())
    assert remote_problems(ok) == []


# ---------- 2. recruiter authentication through the proxy ----------

def test_recruiters_sign_in_to_the_cloud_dashboard_through_the_proxy(settings):
    app = make(cloud(settings))
    owner_session(app)
    with TestClient(app, base_url="https://testserver", headers=RECRUITER) as anon:
        assert anon.get("/").status_code == 200 and anon.get("/static/app.js").status_code == 200  # the desktop app's page loads
        r = anon.get("/api/roles")
        assert r.status_code == 401 and r.json()["detail"] == "Sign in to continue"  # not "only webhooks": the sign-in page shows
        assert anon.post("/api/auth/account-login", json={"email": OWNER["email"], "password": "wrong-password"}).status_code == 401
    c, r = signed_in(app)
    with c:
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=strict" in cookie.lower()  # https behind the proxy
        assert c.get("/api/roles").status_code == 200 and c.get("/api/auth/status").json()
        # The setup wizard and the open "this computer" mode are never reachable through the proxy.
        assert c.post("/api/setup/unlock", json={"code": "ABCD-1234"}).status_code in (403, 404, 409)

    # Sign-in limits count the visitor's own address, not the proxy's: one address guessing doesn't lock out the rest.
    with TestClient(app, headers={"X-Forwarded-For": "192.0.2.66", "X-Forwarded-Proto": "https"}) as guesser:
        codes = [guesser.post("/api/auth/account-login", json={"email": f"x{i}@example.com", "password": "nope-nope-1"}).status_code
                 for i in range(40)]
        assert 429 in codes
    c2, _ = signed_in(app)  # 198.51.100.7, unaffected
    c2.close()


def test_plain_http_forwarded_by_the_proxy_never_reaches_the_app(settings):
    """R-04: a proxy that forwards http instead of redirecting it (a Cloudflare quick tunnel, some load balancers)."""
    app = make(cloud(settings))
    owner_session(app)
    plain = {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "http"}
    with TestClient(app, headers=plain, follow_redirects=False) as c:
        r = c.get("/api/roles?x=1", headers={"Host": "evil.example.net"})  # the redirect never comes from the Host header
        assert r.status_code == 308 and r.headers["location"] == "https://tunnel.example.com/api/roles?x=1"
        assert r.headers["x-frame-options"] == "DENY" and "content-security-policy" in r.headers
        assert c.head("/").status_code == 308
        # The right password over http: refused before it's checked, and no session cookie is ever set.
        r = c.post("/api/auth/account-login", json={"email": OWNER["email"], "password": OWNER["password"]})
        assert r.status_code == 403 and "https://tunnel.example.com" in r.json()["detail"] and "set-cookie" not in r.headers
        assert c.post("/api/public/apply/x", data={"name": "X"}).status_code == 403  # candidates' data too
    # The same request over https works; requests without the header (the health check, the server itself) are untouched.
    c, r = signed_in(app)
    c.close()
    with TestClient(app) as direct:
        assert direct.get("/api/health/ready").status_code == 200


def test_the_first_account_works_even_if_the_data_folder_cant_be_written(settings, monkeypatch, caplog):
    """R-07 (Railway): a root-owned /data volume. The account was saved, then the request failed with 500, and a retry
    was refused because accounts now existed. Now the sign-up completes and the problem is logged."""
    from app.setup import SetupState

    def read_only(self):
        raise PermissionError(13, "Permission denied", str(self.path.with_suffix(".tmp")))
    monkeypatch.setattr(SetupState, "save", read_only)
    app = make(cloud(settings))
    with TestClient(app) as local, caplog.at_level("WARNING"):  # on the server itself, as on a fresh cloud database
        r = local.post("/api/auth/register", json=OWNER)
        assert r.status_code == 200, r.text
        assert r.json()["signed_in"] and r.json()["user"]["is_admin"]
        assert "setup progress couldn't be saved" in caplog.text and "PermissionError" in caplog.text
    c, _ = signed_in(app)  # through the proxy, as the owner afterwards
    c.close()


def test_the_cloud_address_wins_over_a_public_url_saved_by_the_desktop(settings, caplog):
    """R-08 (B-7 on Neon): the database still held the desktop's tunnel as its Public URL, and the cloud backend used
    it: candidate links, MeetStream callbacks and the https redirect went to the PC's dead tunnel."""
    import json
    s = cloud(settings)  # PUBLIC_BASE_URL https://tunnel.example.com: the backend's own address
    store = Store(s.database)
    store.save_setting("public_base_url", json.dumps("https://old-pc-tunnel.trycloudflare.com"), secret=False)
    with caplog.at_level("WARNING"):
        app = create_app(settings=s, store=store, ai=ScreeningAI(), meetstream_client_factory=lambda: FakeMeetStream(),
                         run_loops=False)
    assert "Ignoring the public_base_url saved in the database" in caplog.text
    owner_session(app)
    plain = {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "http"}
    with TestClient(app, headers=plain, follow_redirects=False) as c:
        assert c.get("/api/roles").headers["location"] == "https://tunnel.example.com/api/roles"
    c, _ = signed_in(app)
    with c:
        field = next(f for sec in c.get("/api/config").json()["sections"] for f in sec["fields"] if f["key"] == "public_base_url")
        assert field["source"] == "server" and field["value"] == "https://tunnel.example.com"
        r = c.put("/api/config", json={"changes": {"public_base_url": "https://elsewhere.example.com"}})
        assert r.status_code == 422 and "set on the server" in r.json()["detail"]["errors"]["public_base_url"]
        assert c.get("/api/health").status_code == 200


def test_a_desktop_install_keeps_its_saved_public_url(settings):
    import json
    store = Store(settings.database)
    store.save_setting("public_base_url", json.dumps("https://saved-tunnel.example.com"), secret=False)
    app = create_app(settings=settings, store=store, ai=ScreeningAI(), meetstream_client_factory=lambda: FakeMeetStream(),
                     run_loops=False)
    with TestClient(app) as local:
        local.post("/api/auth/register", json=OWNER)
        field = next(f for sec in local.get("/api/config").json()["sections"] for f in sec["fields"] if f["key"] == "public_base_url")
        assert field["source"] == "saved" and field["value"] == "https://saved-tunnel.example.com"


def test_settings_saved_with_another_key_are_named_in_the_log(settings, caplog):
    """R-09: a SETTINGS_ENCRYPTION_KEY that isn't the one the secrets were saved with dropped them silently in the log."""
    other = Fernet(Fernet.generate_key())
    store = Store(settings.database)
    store.save_setting("gemini_api_key", other.encrypt(b"AIza-not-a-real-key-123").decode(), secret=True)
    with caplog.at_level("WARNING"):
        create_app(settings=cloud(settings), store=store, ai=ScreeningAI(), meetstream_client_factory=lambda: FakeMeetStream(),
                   run_loops=False)
    assert "Saved settings couldn't be read and were skipped: gemini_api_key" in caplog.text
    assert "SETTINGS_ENCRYPTION_KEY" in caplog.text and "AIza-not-a-real-key" not in caplog.text


def test_a_desktop_install_ignores_the_forwarded_scheme(settings):
    app = make(settings)  # not remote: its public URL serves webhooks only, as before
    with TestClient(app, headers={"X-Forwarded-For": "34.230.88.153", "X-Forwarded-Proto": "http"}, follow_redirects=False) as c:
        assert c.get("/api/health").status_code == 200


def test_without_remote_mode_the_dashboard_stays_off_the_public_address(settings):
    app = make(settings)
    owner_session(app)
    with TestClient(app, base_url="https://testserver", headers=RECRUITER) as c:
        r = c.post("/api/auth/account-login", json={"email": OWNER["email"], "password": OWNER["password"]})
        assert c.get("/api/roles").status_code == 403  # as before: through a proxy only webhooks and candidate pages
        assert r.status_code in (200, 403)


def test_the_access_token_works_through_the_proxy_too(settings):
    app = make(replace(cloud(settings), app_access_token="t" * 40))
    with TestClient(app, headers={**RECRUITER, "Authorization": "Bearer " + "t" * 40}) as c:
        assert c.get("/api/roles").status_code == 200
    with TestClient(app, headers={**RECRUITER, "Authorization": "Bearer wrong"}) as c:
        assert c.get("/api/roles").status_code == 401


# ---------- 3-7: jobs, the careers site, applications with a PDF, routing, interviews ----------

def test_jobs_applications_and_interviews_on_the_cloud_backend(settings):
    app = make(cloud(settings))
    store = app.state.store
    owner_session(app)
    c, _ = signed_in(app)
    with c:
        ai_role = publish(c, "AI Engineer")  # 3. job creation and publishing, from the desktop app via the proxy
        backend_role = publish(c, "Backend Engineer")
        careers = c.post("/api/workspaces/current/careers-key").json()
        assert careers["careers_url"].startswith("https://tunnel.example.com/api/public/careers/")  # the stable public address
        path = careers["careers_path"]

        site = TestClient(app, headers=CAREERS)
        jobs = site.get(path).json()["jobs"]  # 4. the careers site reads the published jobs
        assert sorted(j["title"] for j in jobs) == ["AI Engineer", "Backend Engineer"] and all(j["open"] for j in jobs)

        # 5. an application with a PDF resume, 6. routed to the job it names (by title here, by ref for a listing site).
        pdf = make_pdf(["Lin Wei", "Python and LLM APIs, 3 years", "Built a RAG assistant with evaluation"])
        r = site.post(path, data={"role": "AI Engineer", "name": "Lin Wei", "email": "lin@example.com"},
                      files={"resume": ("lin-wei.pdf", pdf, "application/pdf")})
        assert r.status_code == 201 and r.json()["role_title"] == "AI Engineer", r.text
        ref = next(j["ref"] for j in jobs if j["title"] == "Backend Engineer")
        r = site.post(path, data={"job_ref": ref, "name": "Sam Lee", "email": "sam@example.com"},
                      files={"resume": ("sam.pdf", make_pdf(["Sam Lee", "Go and PostgreSQL"]), "application/pdf")})
        assert r.status_code == 201, r.text
        apps = wait_for(lambda: (x := store.all_applications()) and all(a.screening for a in x) and x)
        by_email = {a.candidate.email: a for a in apps}
        assert by_email["lin@example.com"].role_id == ai_role and by_email["sam@example.com"].role_id == backend_role
        assert "RAG assistant" in by_email["lin@example.com"].resume_text  # the PDF was read
        assert site.post(path, data={"role": "Chief Astronaut", "name": "X", "email": "x@example.com"}).status_code == 404
        listed = c.get(f"/api/roles/{ai_role}/pipeline")  # the recruiter sees it under the right role, in the desktop app
        assert listed.status_code == 200 and "Lin Wei" in listed.text and "Sam Lee" not in listed.text

        # 7. interview creation and scheduling.
        iid = c.post(f"/api/roles/{ai_role}/candidates", json={"candidate_name": "Lin Wei", "candidate_email": "lin@example.com"}).json()["id"]
        when = (utcnow() + timedelta(days=2)).replace(microsecond=0)
        r = c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET, "consent_attested": True, "scheduled_at": when.isoformat()})
        assert r.status_code == 200, r.text
        assert c.get(f"/api/interviews/{iid}").json()["interview"]["status"] == "scheduled"
        site.close()


# ---------- 8-9: webhooks and a whole interview with no desktop app and the recruiter's PC off ----------

def test_an_interview_runs_on_webhooks_alone_with_no_desktop_app(settings, monkeypatch):
    monkeypatch.delenv("MIA_DESKTOP_PORT", raising=False)  # no desktop app anywhere: the backend runs on its own
    fake = FakeMeetStream()
    app = make(cloud(settings), fake)
    owner_session(app)
    # The backend stays up for the whole test, as the cloud host does. (A TestClient used as a context manager runs the
    # app's startup and shutdown: closing one would stop the server, which is not what closing the desktop app does.)
    with TestClient(app, headers=MEETSTREAM) as meetstream:
        c = TestClient(app, base_url="https://testserver", headers=RECRUITER)
        assert c.post("/api/auth/account-login", json={"email": OWNER["email"], "password": OWNER["password"]}).status_code == 200
        rid = publish(c, "AI Engineer")
        iid = c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Priya Raman"}).json()["id"]
        sid = c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET, "consent_attested": True}).json()["session_id"]
        assert c.get("/api/settings/readiness").json()["desktop"] is False
        c.close()  # the recruiter closes the app and switches the PC off: from here on only MeetStream talks to the backend

        live = app.state.manager.live[sid]
        token, runner = live.webhook_token, live.runner
        wait_for(lambda: fake.bots.created)
        assert fake.mia.created[0]["agent"]["custom_functions"][0]["url"].startswith("https://tunnel.example.com/webhooks/mia/")
        assert meetstream.post(f"/webhooks/meetstream/{token}/events", json=event("bot.inmeeting")).status_code == 200
        tool = f"/webhooks/mia/{token}"
        auth = {"Authorization": f"Bearer {runner.tool_token}", "X-Bot-ID": "bot-123"}
        call = lambda args: meetstream.post(f"{tool}/next_question", json={"name": "next_question", "bot": {"bot_id": "bot-123"}, "args": args},
                                            headers=auth).json()
        assert call({"candidate_consented": True})["question_id"] == "intro"
        meetstream.post(f"/webhooks/meetstream/{token}/transcript", json=sentence("I built a retrieval pipeline with evaluation sets."))
        previous = "intro"
        while True:
            nxt = call({"previous_question_id": previous, "answer_note": "Answered."})
            if nxt.get("status") == "done":
                break
            previous = nxt["question_id"]
        meetstream.post(f"{tool}/end_interview", json={"name": "end_interview", "bot": {"bot_id": "bot-123"}, "args": {"reason": "completed"}},
                        headers=auth)

        later = TestClient(app, base_url="https://testserver", headers=RECRUITER)  # the recruiter opens the app again later
        later.post("/api/auth/account-login", json={"email": OWNER["email"], "password": OWNER["password"]})
        snap = wait_for(lambda: (s := later.get(f"/api/sessions/{sid}").json()).get("report") and s, timeout=20)
        assert snap["report"]["consent_given"] and snap["report"]["end_reason"] == "completed"
        assert later.get(f"/api/interviews/{iid}").json()["interview"]["status"] == "completed"
        later.close()


# ---------- 10: persistence and recovery: a restarted (or replaced) backend carries on from the database ----------

def test_a_restarted_cloud_backend_carries_on_from_the_database(settings):
    s = cloud(settings)
    app = make(s)
    owner_session(app)
    c, _ = signed_in(app)
    with c:
        rid = publish(c, "AI Engineer")
        path = c.post("/api/workspaces/current/careers-key").json()["careers_path"]
        TestClient(app, headers=CAREERS).post(path, data={"role": "AI Engineer", "name": "Lin Wei", "email": "lin@example.com"},
                                              files={"resume": ("lin.pdf", make_pdf(["Lin Wei", "Python"]), "application/pdf")})
        iid = c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Priya Raman"}).json()["id"]
        when = (utcnow() + timedelta(days=1)).replace(microsecond=0).isoformat()
        c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET, "consent_attested": True, "scheduled_at": when})
    wait_for(lambda: all(a.screening for a in app.state.store.all_applications()))
    app.state.store.close()

    # A new process (a redeploy, a host restart) on the same database and key: same accounts, jobs, careers address,
    # applications and scheduled interview. Sessions survive: the cookie still works.
    again = make(s)
    with TestClient(again, base_url="https://testserver", headers=RECRUITER) as c2:
        c2.cookies.update(c.cookies)
        assert c2.get("/api/roles").status_code == 200
        assert [r["id"] for r in c2.get("/api/roles").json()] == [rid]
        assert c2.get(f"/api/interviews/{iid}").json()["interview"]["status"] == "scheduled"
        assert TestClient(again, headers=CAREERS).get(path).json()["jobs"][0]["title"] == "AI Engineer"
        assert [a.candidate.email for a in again.state.store.all_applications()] == ["lin@example.com"]


def test_strangers_cant_register_on_a_cloud_backend_unless_an_admin_opens_it(settings):
    app = make(cloud(settings))
    owner_session(app)  # the database's owner, created on the server itself
    stranger = {"name": "Eve Outsider", "email": "eve@example.com", "password": "outsider-password-1"}
    with TestClient(app, base_url="https://testserver", headers={"X-Forwarded-For": "192.0.2.99", "X-Forwarded-Proto": "https"}) as eve:
        r = eve.post("/api/auth/register", json=stranger)
        assert r.status_code == 403 and "switched off" in r.json()["detail"]  # nobody gets the installation's keys uninvited
    c, _ = signed_in(app)
    with c:
        assert c.get("/api/accounts").json()["open_registration"] is False
        assert c.put("/api/accounts/rules", json={"open_registration": True}).json()["open_registration"] is True  # the admin's choice
    with TestClient(app, base_url="https://testserver", headers={"X-Forwarded-For": "192.0.2.99", "X-Forwarded-Proto": "https"}) as eve:
        assert eve.post("/api/auth/register", json=stranger).status_code == 200


def test_a_local_install_keeps_open_registration(settings):
    app = make(settings)
    owner_session(app)
    with TestClient(app) as local:
        assert local.post("/api/auth/register", json={"name": "Bo", "email": "bo@example.com", "password": "bo-password-123"}).status_code == 200


def test_each_candidate_behind_the_proxy_has_their_own_limit(settings):
    """R-01: the proxy (Caddy, cloudflared, a load balancer) connects from another address on a private network, not
    127.0.0.1. Candidates must each count as themselves, not all as the proxy, and nobody may choose their own address."""
    from tests.test_recruiting import RESUME
    app = make(cloud(settings))
    owner_session(app)
    c, _ = signed_in(app)
    with c:
        rid = publish(c, "AI Engineer")
        key = c.get(f"/api/roles/{rid}/pipeline").json()["careers"]["job_key"]

    def apply(client, i, forwarded):
        return client.post(f"/api/public/apply/{key}", headers={"X-Forwarded-For": forwarded, "X-Forwarded-Proto": "https"},
                           data={"full_name": f"Candidate {i}", "email": f"c{i}@example.com", "consent": "yes"},
                           files={"resume": ("cv.txt", RESUME, "text/plain")}).status_code

    proxy = TestClient(app, client=("172.18.0.5", 41000))  # a trusted proxy on the Docker network
    # Twelve real candidates, each a different address added by the proxy: all accepted (R-01 refused the 10th on).
    assert [apply(proxy, i, f"203.0.113.{10 + i}") for i in range(12)] == [201] * 12
    # One client rotating made-up entries to the left of what the proxy added: still one address, so it's limited.
    codes = [apply(proxy, 100 + i, f"10.9.9.{i}, 198.51.100.200") for i in range(12)]
    assert codes[:10] == [201] * 10 and set(codes[10:]) == {429}
    # A public address that isn't a trusted proxy can't choose its address with headers: it counts as itself.
    direct = TestClient(app, client=("192.0.2.50", 41000))
    codes = [apply(direct, 200 + i, f"203.0.113.{100 + i}") for i in range(12)]
    assert codes[:10] == [201] * 10 and set(codes[10:]) == {429}
    proxy.close(); direct.close()
