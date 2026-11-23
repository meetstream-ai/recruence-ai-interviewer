"""The Settings page: configuration without secrets, and admin-only checks."""

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from app.llm import OfflineAI
from app.security import permitted
from app.server import create_app
from app.store import Store
from tests.fakes import FakeMeetStream


def client(settings, fake=None, send=None):
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), run_loops=False,
                     meetstream_client_factory=lambda: fake or FakeMeetStream(), mail_send=send)
    return TestClient(app)


def test_settings_show_configuration_but_never_secrets(settings):
    secret_settings = replace(settings, app_access_token="", meetstream_api_key="ms-super-secret", gemini_api_key="AIza-secret",
                              smtp_host="smtp.example.com", smtp_from="hiring@example.com", smtp_password="pw-secret",
                              report_webhook_url="https://ats.example.com/hook", report_webhook_secret="0123456789abcdef-secret")
    with client(secret_settings) as c:
        sections = c.get("/api/settings").json()["sections"]
    text = json.dumps(sections)
    for secret in ("ms-super-secret", "AIza-secret", "pw-secret", "0123456789abcdef-secret"):
        assert secret not in text
    flat = {item[0]: item[1] for s in sections for item in s["items"]}
    assert flat["API key"] == "set" and flat["Anthropic / Gemini / OpenAI key"] == "not set / set / not set"
    assert flat["Sending"].startswith("on, via smtp.example.com") and flat["Export"] == "on, to ats.example.com"
    assert flat["Public address"] == settings.public_base_url
    assert all(len(item) == 3 for s in sections for item in s["items"])  # label, value, the .env variable(s)


def test_checks(settings, monkeypatch):
    from app import server as server_module

    async def resolves(url):
        return url == "https://tunnel.example.com"

    monkeypatch.setattr(server_module, "public_host_resolves", resolves)
    sent = []
    with client(replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com"),
                send=lambda s, e: sent.append(e)) as c:
        import httpx
        # The public address leads back to this app.
        monkeypatch.setattr(server_module, "public_transport", httpx.ASGITransport(app=c.app))
        r = c.post("/api/settings/check/public_url").json()
        assert r["ok"] is True and r["result"] == "this_app" and r["message"] == "https://tunnel.example.com reaches this app"
        # Resolves, but another Recruence answers.
        monkeypatch.setattr(server_module, "public_transport", httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True, "instance": "not-us"})))
        r = c.post("/api/settings/check/public_url").json()
        assert r["ok"] is False and r["result"] == "other_recruence"
        assert c.post("/api/settings/check/meetstream").json() == {"ok": True, "message": "MeetStream accepted the API key."}
        assert c.post("/api/settings/check/email", json={"to": "me@example.com"}).json()["ok"] is True
        assert sent[0].to == ["me@example.com"]
        assert c.post("/api/settings/check/email", json={"to": "nope"}).status_code == 422
        assert c.post("/api/settings/check/teleport").status_code == 404
    with client(replace(settings, public_base_url="https://gone.trycloudflare.com")) as c:
        r = c.post("/api/settings/check/public_url").json()
        assert r["ok"] is False and "start the tunnel" in r["message"]
        assert c.post("/api/settings/check/email", json={"to": "me@example.com"}).json()["ok"] is False  # email not set up


def test_checks_are_admin_only():
    assert not permitted("recruiter", "POST", "/api/settings/check/email") and not permitted("viewer", "POST", "/api/settings/check/meetstream")
    assert permitted("viewer", "GET", "/api/settings") and permitted("admin", "POST", "/api/settings/check/email")


def test_database_check_and_password_kept_out(settings):
    with client(settings) as c:
        r = c.post("/api/settings/check/database").json()
        assert r["ok"] is True and "answered (schema version 8)" in r["message"]
        flat = {item[0]: item[1] for s in c.get("/api/settings").json()["sections"] for item in s["items"]}
    assert flat["Database"].startswith(("SQLite, ", "PostgreSQL at "))
    shown = __import__("app.settings_view", fromlist=["overview"]).overview(
        replace(settings, database_url="postgresql://mia:pg-secret@db:5432/mia"), "x", False, False, False)
    assert "pg-secret" not in json.dumps(shown) and "PostgreSQL at db:5432/mia as mia" in json.dumps(shown)
    assert not permitted("recruiter", "POST", "/api/settings/check/database")


def test_a_check_tests_what_was_typed_without_saving_it(settings, monkeypatch):
    """Check the API key / Send a test email: the form's unsaved values are what's tested; nothing is saved, and only a
    check of the saved values counts for setup."""
    import meetstream as sdk

    seen = []

    class Client:
        def __init__(self, api_key=None, **_):
            seen.append(api_key)
            self.mia = self

        async def list(self):
            if seen[-1] != "ms_live_typed_key":
                raise sdk.MeetStreamError("401 invalid key")
            return []

        async def aclose(self):
            pass

    sent = []
    s = replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com", smtp_password="saved-pass")
    with client(s, send=lambda used, e: sent.append((used, e))) as c:
        app = c.app
        monkeypatch.setattr(app.state.manager, "_client_factory", None)
        monkeypatch.setattr(sdk, "AsyncMeetStream", Client)
        r = c.post("/api/settings/check/meetstream", json={"values": {"meetstream_api_key": "ms_live_typed_key"}}).json()
        assert r["ok"] is True and r["unsaved"] is True and "Save to keep it" in r["message"]
        assert seen == ["ms_live_typed_key"]  # the backend got the typed key
        assert s.meetstream_api_key != "ms_live_typed_key"  # and didn't save it
        assert (c.get("/api/setup/status").json()["steps"]["meetstream"].get("check") or {}).get("ok") is not True
        bad = c.post("/api/settings/check/meetstream", json={"values": {"meetstream_api_key": "wrong"}}).json()
        assert bad["ok"] is False and "refused" in bad["message"]
        # The public URL: an unsaved value is validated like a save, and reported, not a 422.
        r = c.post("/api/settings/check/public_url", json={"values": {"public_base_url": "http://localhost:8000"}}).json()
        assert r["ok"] is False and "Public application URL" in r["message"]
        # Email: the typed port and server are used; an empty password field means the saved password.
        r = c.post("/api/settings/check/email", json={"to": "me@example.com", "values": {
            "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_password": "", "smtp_starttls": True, "smtp_ssl": False}}).json()
        used, email = sent[-1]
        assert r["ok"] is True and r["unsaved"] is True and used.smtp_host == "smtp.gmail.com" and used.smtp_password == "saved-pass"
        assert s.smtp_host == "smtp.example.com" and email.to == ["me@example.com"]
        r = c.post("/api/settings/check/email", json={"to": "me@example.com", "values": {"smtp_port": "abc"}}).json()
        assert r["ok"] is False and "SMTP port" in r["message"]
        # The saved values: recorded for setup.
        assert c.post("/api/settings/check/email", json={"to": "me@example.com"}).json()["ok"] is True
        assert c.get("/api/setup/status").json()["steps"]["email"]["check"]["ok"] is True


# ---------- The Public URL check: reachable, Recruence and this app are told apart ----------

QUICK_TUNNEL = "https://courses-compound-scheme-qualified.trycloudflare.com"


def careers_site():
    """A careers website like MeetStream Careers (FastAPI on port 9000): a homepage and /api/jobs, nothing of Recruence."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    site = FastAPI()
    site.get("/", response_class=HTMLResponse)(lambda: "<!doctype html><html><body>Careers</body></html>")
    site.get("/api/jobs")(lambda: {"available": False})
    return site


def public_check(settings, monkeypatch, transport, url=QUICK_TUNNEL):
    from app import server as server_module

    async def resolves(u):
        return True
    monkeypatch.setattr(server_module, "public_host_resolves", resolves)
    monkeypatch.setattr(server_module, "public_transport", transport)
    with client(replace(settings, public_base_url=url)) as c:
        return c.post("/api/settings/check/public_url").json()


def test_a_quick_tunnel_to_the_careers_site_is_reachable_but_not_this_app(settings, monkeypatch):
    """Regression: the Public URL was a Cloudflare Quick Tunnel to the careers site on the same computer. The site
    answers / and /api/jobs with 200, but /webhooks/health with 404; the check said only "HTTPStatusError"."""
    import asyncio

    import httpx
    site = httpx.ASGITransport(app=careers_site())

    async def through_tunnel():  # what the tunnel serves: the careers site works
        async with httpx.AsyncClient(transport=site, base_url=QUICK_TUNNEL) as h:
            return (await h.get("/")).status_code, (await h.get("/api/jobs")).json()
    assert asyncio.run(through_tunnel()) == (200, {"available": False})

    r = public_check(settings, monkeypatch, site)
    assert r["ok"] is False and r["result"] == "not_recruence" and r["status"] == 404
    assert "is reachable, but it isn't this app" in r["message"] and "HTTP 404" in r["message"]
    assert '{"detail":"Not Found"}' in r["message"]  # the real body, not just the exception's name
    assert "careers site" in r["message"] and "http://127.0.0.1:" in r["message"]  # where the tunnel should lead
    assert "HTTPStatusError" not in r["message"]


def test_a_200_page_that_isnt_recruence_is_not_proof(settings, monkeypatch):
    """A site that answers every path with 200 (a single-page app, a parking page) isn't this app."""
    import httpx
    html = httpx.MockTransport(lambda req: httpx.Response(200, html="<html>Welcome</html>"))
    r = public_check(settings, monkeypatch, html)
    assert r["ok"] is False and r["result"] == "not_recruence" and "text/html page" in r["message"]
    lookalike = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))  # healthy, but no instance id
    assert public_check(settings, monkeypatch, lookalike)["result"] == "not_recruence"


def test_redirects_dead_origins_and_no_connection_are_told_apart(settings, monkeypatch):
    import httpx
    seen = []

    def redirect(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"location": "https://elsewhere.example.com/webhooks/health"})
    r = public_check(settings, monkeypatch, httpx.MockTransport(redirect))
    assert r["result"] == "redirect" and "elsewhere.example.com" in r["message"]
    assert seen == [f"{QUICK_TUNNEL}/webhooks/health"]  # asked once, the redirect not followed

    # Cloudflare's own answer when the tunnel's local service isn't running.
    r = public_check(settings, monkeypatch, httpx.MockTransport(lambda req: httpx.Response(530, text="error code: 1033")))
    assert r["result"] == "no_origin" and "HTTP 530" in r["message"] and "nothing answers behind it" in r["message"]

    def refuse(req):
        raise httpx.ConnectError("connection refused", request=req)
    r = public_check(settings, monkeypatch, httpx.MockTransport(refuse))
    assert r["result"] == "unreachable" and "ConnectError: connection refused" in r["message"]

    def slow(req):
        raise httpx.ReadTimeout("timed out", request=req)
    assert "didn't answer within 10 seconds" in public_check(settings, monkeypatch, httpx.MockTransport(slow))["message"]


def test_an_interview_isnt_started_through_an_address_that_leads_to_the_careers_site(settings, monkeypatch):
    """The launch guard uses the same verdict: MeetStream's callbacks would go to the careers site."""
    import asyncio

    import httpx
    from app import server as server_module
    monkeypatch.setattr(server_module, "public_transport", httpx.ASGITransport(app=careers_site()))
    app = create_app(settings=replace(settings, public_base_url=QUICK_TUNNEL, check_public_url=True), store=Store(settings.database),
                     ai=OfflineAI(), run_loops=False, meetstream_client_factory=FakeMeetStream)
    answer = asyncio.run(app.state.manager._reaches_this_app())
    assert isinstance(answer, str) and "HTTP 404" in answer and "isn't this app" in answer
