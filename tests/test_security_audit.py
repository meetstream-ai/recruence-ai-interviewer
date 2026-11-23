"""Regression tests for the security audit. Each one failed before its fix."""

import asyncio
import io
import logging
import zipfile
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import app.server as server_module
from app.llm import OfflineAI
from app.logsafe import RedactTokens, redact
from app.models import CriterionResult, ScreeningOutput
from app.resume import ResumeError, extract_resume
from app.server import create_app
from app.store import Store
from tests.fakes import FakeMeetStream
from tests.test_api import wait_for
from tests.test_recruiting import RESUME, ScreeningAI, apply, published_role

REMOTE = ("203.0.113.7", 40000)  # a visitor on the internet, not through a local proxy
OWNER = {"name": "Olive Owner", "email": "olive@example.com", "password": "owner-password-1", "workspace_name": "Acme"}


def client(settings, ai=None, mail=None, peer=None):
    app = create_app(settings=settings, store=Store(settings.database), ai=ai or ScreeningAI(),
                     meetstream_client_factory=FakeMeetStream, run_loops=False, mail_send=mail)
    return app, (TestClient(app, client=peer) if peer else TestClient(app))


def test_a_faked_forwarded_address_does_not_lift_the_application_limit(settings):
    app, c = client(settings)
    with c:
        rid, _, token = published_role(c)
        outsider = TestClient(app, client=REMOTE)
        codes = [outsider.post(f"/api/public/apply/{token}", data={"full_name": f"P {i}", "email": f"p{i}@example.org", "consent": "yes"},
                               headers={"CF-Connecting-IP": f"198.51.100.{i}"}).status_code for i in range(12)]
        assert codes[:10] == [201] * 10 and codes[10:] == [429, 429]  # one remote address, whatever header it sends


def test_one_role_takes_a_bounded_number_of_applications_an_hour(settings, monkeypatch):
    monkeypatch.setattr(server_module, "ROLE_APPLICATIONS_PER_HOUR", 3)
    app, c = client(settings)
    with c:
        rid, _, token = published_role(c)
        codes = [c.post(f"/api/public/apply/{token}", data={"full_name": f"P {i}", "email": f"p{i}@example.org", "consent": "yes"},
                        headers={"CF-Connecting-IP": f"198.51.100.{i}"}).status_code for i in range(4)]
        assert codes == [201, 201, 201, 429]  # many addresses (through the tunnel), one role: capped


def test_link_and_webhook_tokens_never_reach_the_logs():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "POST", "/api/public/interview/l51Mrok65RYk8IbKTQ2XtIEwHJVygZhC/start", "1.1", 200), None)
    RedactTokens().filter(record)
    assert "l51Mrok65RYk8IbKTQ2XtIEwHJVygZhC" not in record.getMessage() and "/api/public/interview/***/start" in record.getMessage()
    assert redact("/webhooks/meetstream/AbCdEfGh1234567890xyz/events") == "/webhooks/meetstream/***/events"
    assert redact("/webhooks/mia/AbCdEfGh1234567890xyz/next_question") == "/webhooks/mia/***/next_question"
    assert redact("/api/roles/abc123def456") == "/api/roles/abc123def456"  # ordinary ids stay readable


def test_every_route_with_a_secret_in_its_path_is_hidden_in_logs(settings):
    """R-04/R-05 (12 November, Railway): the careers key, the short candidate link and the ATS webhook token were logged
    in full. Every route whose path carries a token or key is checked, so a route added later can't slip past."""
    app = create_app(settings=settings, store=Store(settings.database), ai=ScreeningAI(),
                     meetstream_client_factory=lambda: FakeMeetStream(), run_loops=False)
    secret = "aLghpkjbl61h_OokJsefa99zLyRdgsW2"
    paths = sorted({r.path for r in app.routes if "{token}" in getattr(r, "path", "") or "{key}" in getattr(r, "path", "")}
                   - {"/api/config/{key}"})  # a setting's name, not a secret
    assert {"/api/public/careers/{key}", "/i/{token}", "/webhooks/ats/{token}"} <= set(paths)
    for path in paths:
        line = path.replace("{token}", secret).replace("{key}", secret).replace("{kind}", "events").replace("{tool}", "next_question")
        assert secret not in redact(line), path


def recruiter_in(app, owner):
    code = owner.post("/api/workspaces/current/code", json={"days": 1}).json()["join_code"]
    member = TestClient(app, client=("172.17.0.1", 50000))
    member.post("/api/auth/register", json={"name": "Rex Recruiter", "email": "rex@example.com", "password": "recruiter-pass-1"})
    member.post("/api/workspaces/join", json={"code": code})
    uid = next(m["id"] for m in owner.get("/api/workspaces/current").json()["members"] if m["status"] == "pending")
    owner.post(f"/api/workspaces/current/members/{uid}/decision", json={"approve": True})
    member.post("/api/workspaces/switch", json={"workspace_id": owner.get("/api/workspaces/current").json()["id"]})
    return member


def test_recruiters_dont_see_installation_details_or_its_calendar(settings):
    app, owner = client(settings, ai=OfflineAI())
    with owner:
        owner.post("/api/auth/register", json=OWNER)
        member = recruiter_in(app, owner)
        assert member.get("/api/roles").status_code == 200  # their workspace works
        for path in ("/api/config", "/api/settings", "/api/database", "/api/calendar/status", "/api/calendar/events"):
            assert member.get(path).status_code == 403, path
        assert owner.get("/api/config").status_code == 200


def test_proxy_recruiters_cant_pick_another_workspace(settings):
    s = replace(settings, proxy_auth_secret="proxy-secret-value-123", proxy_default_role="recruiter")
    app, c = client(s, ai=OfflineAI())
    with c:
        c.post("/api/auth/register", json=OWNER)
        first = c.get("/api/workspaces/current").json()["id"]
        second = c.post("/api/workspaces", json={"name": "Other company"}).json()["id"]
        c.post("/api/roles", json={"role_title": "Secret role", "job_description": "Only in the other company"})
        proxied = TestClient(app)
        h = {"X-MIA-Proxy-Secret": "proxy-secret-value-123", "X-Forwarded-Email": "sam@example.com", "X-MIA-Workspace": second}
        roles = proxied.get("/api/roles", headers=h).json()
        assert all(r["role_title"] != "Secret role" for r in roles)  # stays in the default workspace
        assert proxied.post("/api/workspaces/switch", json={"workspace_id": second}, headers=h).status_code == 403
        assert first


def test_other_websites_cant_drive_the_dashboard_from_a_browser(settings):
    app, c = client(replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com", smtp_password="saved-secret"))
    attacker = {"smtp_host": "evil.example.net"}
    with c:
        cross = {"Sec-Fetch-Site": "cross-site"}
        refused = c.post("/api/settings/check/email", json={"to": "a@example.net", "values": attacker}, headers=cross)
        assert refused.status_code == 403 and "other websites" in refused.json()["detail"]
        assert c.post("/api/roles", json={"role_title": "Planted", "job_description": "x"}, headers=cross).status_code == 403
        assert c.post("/api/roles", json={"role_title": "Port", "job_description": "x"}, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
        assert c.get("/api/roles", headers=cross).status_code == 200  # reading is unaffected, and needs no protection here
        assert c.post("/api/roles", json={"role_title": "Mine", "job_description": "x"}, headers={"Sec-Fetch-Site": "same-origin"}).status_code != 403
        assert c.post("/api/roles", json={"role_title": "Script", "job_description": "x"}).status_code != 403  # not a browser: no header
        # Public addresses are meant for other sites and servers: a careers site's form still posts.
        _, _, token = published_role(c)
        public = c.post(f"/api/public/apply/{token}", data={"full_name": "P", "email": "p@example.org", "consent": "yes"}, headers=cross)
        assert public.status_code == 201


def test_proxy_users_only_see_what_their_role_manages(settings):
    s = replace(settings, proxy_auth_secret="proxy-secret-value-123", proxy_default_role="viewer", proxy_admin_groups=("admins",))
    app, c = client(s, ai=OfflineAI())
    with c:
        c.post("/api/auth/register", json=OWNER)
        c.post("/api/workspaces/current/careers-key")
        proxied = TestClient(app)
        h = {"X-MIA-Proxy-Secret": "proxy-secret-value-123", "X-Forwarded-Email": "sam@example.com"}
        seen = proxied.get("/api/workspaces/current", headers=h).json()
        assert seen["role"] == "viewer"
        assert not {"careers_path", "careers_url", "join_code", "members"} & set(seen)
        admin = proxied.get("/api/workspaces/current", headers={**h, "X-Forwarded-Groups": "admins"}).json()
        assert admin["role"] == "admin" and "careers_path" in admin


class SlowScreeningAI(ScreeningAI):
    async def screen(self, system, user):
        await asyncio.sleep(0.6)
        return ScreeningOutput(results=[CriterionResult(criterion_id=k, verdict="met", evidence=q, reasoning="r")
                                        for k, q in (("e1", "B.Tech in Computer Science"), ("e2", "Five years of Python services"))],
                               summary="Fits.")


def test_inviting_during_screening_doesnt_invite_twice(settings):
    sent = []
    s = replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com")
    app, c = client(s, ai=SlowScreeningAI(), mail=lambda _, e: sent.append(e))
    store = app.state.store
    with c:
        rid, _, token = published_role(c, auto_invite=True)
        assert apply(c, token).status_code == 201
        a = store.applications_for_role(rid)[0]
        assert a.status == "screening"
        c.post(f"/api/applications/{a.id}/decision", json={"action": "invite"})  # while the AI is still screening
        wait_for(lambda: store.get_application(a.id).screening is not None)
        import time
        time.sleep(0.3)
        assert len(store.interviews_for_role(rid)) == 1
        assert len([e for e in sent if "/i/" in e.body]) == 1


def test_a_docx_zip_bomb_is_refused_before_unpacking():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", b"\0" * (60 * 1024 * 1024))  # ~60 MB that compresses to ~60 KB
    data = buf.getvalue()
    assert len(data) < 1024 * 1024
    with pytest.raises(ResumeError, match="unusually large"):
        extract_resume("cv.docx", data)
    assert extract_resume("cv.txt", RESUME).text.startswith("Priya")


def test_password_reset_emails_cant_flood_an_inbox(settings):
    sent = []
    s = replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com")
    app, c = client(s, ai=OfflineAI(), mail=lambda _, e: sent.append(e))
    with c:
        c.post("/api/auth/register", json=OWNER)
        for _ in range(6):
            assert c.post("/api/auth/reset-request", json={"email": "olive@example.com"}).json()["sent"] is True  # same answer
        wait_for(lambda: len(sent) >= 3)
        import time
        time.sleep(0.3)
        assert len(sent) == 3


def test_one_address_cant_spray_passwords_across_accounts(settings):
    app, c = client(settings, ai=OfflineAI())
    with c:
        c.post("/api/auth/register", json=OWNER)
        sprayer = TestClient(app, client=REMOTE)
        codes = [sprayer.post("/api/auth/account-login", json={"email": f"user{i}@example.com", "password": "Summer2026!"}).status_code
                 for i in range(31)]
        assert codes[:30] == [401] * 30 and codes[30] == 429
        assert TestClient(app, client=("198.51.100.9", 1)).post(
            "/api/auth/account-login", json={"email": "olive@example.com", "password": OWNER["password"]}).status_code == 200


def test_visitors_dont_learn_which_database_server_is_down(settings):
    from app import dbconfig
    from app.secret_box import load_box

    if settings.database_url:
        pytest.skip("needs the local SQLite default")
    dbconfig.save_profile(settings.data_dir, load_box(settings.data_dir), "Company", "postgresql+psycopg://u:pw@10.20.30.40:1/hr")
    app = create_app(settings=replace(settings), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        c.post("/api/auth/register", json=OWNER)  # now there's an account: the local open door is closed
        visitor = TestClient(app, client=REMOTE)
        for body in (visitor.get("/api/auth/status").json()["database_problem"], visitor.get("/api/connection/status").json()["problem"]):
            assert "couldn't be reached" in body and "10.20.30.40" not in body
        assert "10.20.30.40" in c.get("/api/auth/status").json()["database_problem"]  # the admin sees which one


def test_only_listed_websites_can_use_the_application_api_from_their_pages(settings):
    s = replace(settings, public_cors_origins=("http://127.0.0.1:8300",))
    app, c = client(s)
    with c:
        rid, _, token = published_role(c)
        site = {"Origin": "http://127.0.0.1:8300"}
        r = c.get(f"/api/public/apply/{token}", headers=site)
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://127.0.0.1:8300"
        pre = c.options(f"/api/public/apply/{token}", headers={**site, "Access-Control-Request-Method": "POST"})
        assert pre.status_code == 204 and "POST" in pre.headers["access-control-allow-methods"]
        assert "access-control-allow-credentials" not in pre.headers  # never with cookies
        other = c.get(f"/api/public/apply/{token}", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-origin" not in other.headers
        assert "access-control-allow-origin" not in c.get("/api/roles", headers=site).headers  # nothing else is opened
        bad = c.put("/api/config", json={"changes": {"public_cors_origins": "https://ok.example.com/path"}})
        assert bad.status_code == 422


def test_open_localhost_mode_checks_the_host_name(settings):
    """Audit 2026-11-10 (M3): before any account exists, a request from this computer is the administrator. A page whose
    name was rebound to 127.0.0.1 reaches the port too, with its own name in Host: that isn't this computer's app."""
    from fastapi.testclient import TestClient

    from app.llm import OfflineAI
    from app.server import create_app
    from app.store import Store
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        assert c.get("/api/interviews").status_code == 200
        for host in ("localhost:8765", "127.0.0.1:8765", "[::1]:8765"):
            assert c.get("/api/interviews", headers={"host": host}).status_code == 200, host
        for host in ("evil.example", "evil.example:8765", "127.0.0.1.evil.example"):
            assert c.get("/api/interviews", headers={"host": host}).status_code in (401, 403), host
            assert c.post("/api/roles", json={"role_title": "X", "job_description": "Y"}, headers={"host": host}).status_code in (401, 403)


def test_a_resume_is_read_off_the_event_loop_with_a_time_limit(settings, monkeypatch):
    """Audit 2026-11-10 (M4): parsing ran on the server's loop, so a slow file stalled everything else. It now runs in
    a worker thread, and one that takes too long is refused with a reason instead of holding the request."""
    import threading
    import time

    from fastapi.testclient import TestClient

    from app import server as server_module
    from app.llm import OfflineAI
    from app.server import create_app
    from app.store import Store
    seen = {}

    def slow(name, data):
        seen["thread"] = threading.current_thread().name
        time.sleep(1.0)
        raise AssertionError("read too late")
    monkeypatch.setattr(server_module, "extract_resume", slow)
    monkeypatch.setattr(server_module, "RESUME_SECONDS", 0.2)
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        started = time.monotonic()
        r = c.post("/api/resume/extract", files={"file": ("cv.pdf", b"%PDF-1.4", "application/pdf")})
        assert r.status_code == 422 and "took too long" in r.json()["detail"] and time.monotonic() - started < 0.9
    assert seen["thread"] != "MainThread"


def test_pages_send_browser_security_headers(settings):
    """Audit 2026-11-10 (M5): no Content-Security-Policy, framing or sniffing protection."""
    from fastapi.testclient import TestClient

    from app.llm import OfflineAI
    from app.server import create_app
    from app.store import Store
    from tests.test_api import TUNNEL
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        for r in (c.get("/"), c.get("/static/app.js"), c.get("/i/" + "x" * 32, headers=TUNNEL), c.get("/api/health")):
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and "script-src 'self'" in r.headers["content-security-policy"]
            assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        assert c.get("/i/" + "x" * 32, headers=TUNNEL).headers["referrer-policy"] == "no-referrer"  # the page's own, kept
        assert c.get("/").headers["referrer-policy"] == "same-origin"


def test_odd_characters_in_links_and_credentials_are_refused_not_errors(settings):
    """Audit 2026-11-10 (L1): an accented character in a link, key or token gave a 500 (compare_digest refuses
    non-ASCII text). They're now plain refusals."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app.compare import same
    from app.llm import OfflineAI
    from app.server import create_app
    from app.store import Store
    from tests.test_api import TUNNEL
    from tests.test_recruiting import published_role
    assert same("café", "café") and not same("café", "cafe") and not same("", "") and not same(None, "x")
    app = create_app(settings=replace(settings, app_access_token="token-value"), store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/api/auth/login", json={"token": "token-value"})
        published_role(c)
        for path in ("/api/public/apply/" + "é" * 20, "/api/public/careers/" + "é" * 30, "/api/public/booking/" + "é" * 30):
            assert c.get(path, headers=TUNNEL).status_code == 404, path
        anon = TestClient(app, raise_server_exceptions=False)
        assert anon.get("/api/interviews", headers={"authorization": "Bearer café".encode("latin-1")}).status_code == 401
        assert anon.post("/api/auth/login", json={"token": "café"}).status_code == 401


def test_the_access_token_sign_in_has_an_attempt_limit(settings):
    """Audit 2026-11-10 (L2): wrong access tokens could be tried without limit."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from app.llm import OfflineAI
    from app.server import create_app
    from app.store import Store
    app = create_app(settings=replace(settings, app_access_token="the-right-token"), store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        assert [c.post("/api/auth/login", json={"token": f"wrong-{n}"}).status_code for n in range(10)] == [401] * 10
        assert c.post("/api/auth/login", json={"token": "wrong-again"}).status_code == 429
        assert c.post("/api/auth/login", json={"token": "the-right-token"}).status_code == 429  # waits out the window too


def test_a_public_upload_must_say_its_size(settings):
    """Audit 2026-11-10 (L5): a chunked upload without Content-Length was spooled to disk with no limit."""
    from fastapi.testclient import TestClient

    from app.llm import OfflineAI
    from app.server import create_app
    from app.store import Store
    from tests.test_api import TUNNEL
    from tests.test_recruiting import published_role
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        _, _, key = published_role(c)

        def chunks():
            yield b"--x\r\nContent-Disposition: form-data; name=\"full_name\"\r\n\r\nPriya\r\n--x--\r\n"
        r = c.post(f"/api/public/apply/{key}", content=chunks(), headers={**TUNNEL, "content-type": "multipart/form-data; boundary=x"})
        assert r.status_code == 411


def test_parallel_code_guesses_cant_exceed_the_tries(settings):
    """Audit 2026-11-10 (L6): tries were read, then counted, so guesses sent at once could use more than five."""
    from app import gateway
    from app.store import Store
    store = Store(settings.database)
    gateway.send_code(store, "t" * 32)
    link = gateway.token_hash("t" * 32)
    taken = [store.take_gateway_attempt(link, gateway.MAX_TRIES) for _ in range(8)]
    assert taken == [True] * gateway.MAX_TRIES + [False] * (8 - gateway.MAX_TRIES)
    assert gateway.check_code(store, "t" * 32, "000000") is False  # no tries left, whatever the code
