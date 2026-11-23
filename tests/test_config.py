"""Settings saved from the dashboard: encrypted secrets, live vs restart changes, validation, access token, provider tests."""

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.llm import OfflineAI
from app.llm.probe import test_provider as probe
from app.security import permitted
from app.server import create_app
from app.store import Store, app_settings


def client(settings):
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    return app, TestClient(app)


def fields(view):
    return {f["key"]: f for s in view["sections"] for f in s["fields"]}


def test_secrets_are_encrypted_at_rest_and_never_returned(settings):
    app, c = client(replace(settings))
    with c:
        r = c.put("/api/config", json={"changes": {"gemini_api_key": "AIza-very-secret", "smtp_password": "pw-secret"}})
        assert r.status_code == 200 and r.json()["applied"] == ["gemini_api_key", "smtp_password"]
        text = json.dumps(c.get("/api/config").json()) + json.dumps(c.get("/api/audit").json())
        assert "AIza-very-secret" not in text and "pw-secret" not in text
        assert fields(c.get("/api/config").json())["gemini_api_key"] == fields(r.json())["gemini_api_key"]
        assert fields(r.json())["gemini_api_key"]["set"] is True and "value" not in fields(r.json())["gemini_api_key"]
        rows = {x["key"]: x for x in app.state.store._rows(__import__("sqlalchemy").select(app_settings))}
        assert rows["gemini_api_key"]["secret"] and "AIza-very-secret" not in rows["gemini_api_key"]["value"]


def test_live_changes_apply_now_and_survive_a_restart(settings):
    fresh = replace(settings)
    _, c = client(replace(settings))
    with c:
        r = c.put("/api/config", json={"changes": {"bot_name": "Hiring Assistant", "data_retention_days": 14, "time_zone": "Asia/Kolkata"}}).json()
        assert sorted(r["applied"]) == ["bot_name", "data_retention_days", "time_zone"] and r["restart_required"] == []
        assert c.get("/api/health").json()["meetstream"]["bot_name"] == "Hiring Assistant"
    _, c2 = client(fresh)  # a new start, same database
    with c2:
        f = fields(c2.get("/api/config").json())
        assert f["bot_name"]["value"] == "Hiring Assistant" and f["bot_name"]["source"] == "saved" and f["data_retention_days"]["value"] == 14


def test_the_product_name_saved_before_the_rename_shows_as_recruence(settings):
    from app.config import current_app_name
    assert current_app_name("MIA Interviewer") == "Recruence" and current_app_name("") == "Recruence"
    assert current_app_name("Acme Hiring") == "Acme Hiring"
    store = Store(settings.database)
    store.save_setting("app_name", json.dumps("MIA Interviewer"), secret=False)  # saved by an earlier version
    _, c = client(replace(settings))
    with c:
        assert c.get("/api/health").json()["app_name"] == "Recruence"
    assert json.loads(next(r for r in store.saved_settings() if r["key"] == "app_name")["value"]) == "MIA Interviewer"  # row untouched
    _, c = client(replace(settings))
    with c:
        c.put("/api/config", json={"changes": {"app_name": "Acme Hiring"}})
        assert c.get("/api/health").json()["app_name"] == "Acme Hiring"  # a name of one's own is kept


def test_restart_only_settings_wait_for_a_restart(settings):
    s = replace(settings)
    _, c = client(s)
    with c:
        r = c.put("/api/config", json={"changes": {"notify_emails": "a@example.com, b@example.com"}}).json()
        assert r["restart_required"] == ["notify_emails"] and r["applied"] == []
        assert s.notify_emails == () and fields(r)["notify_emails"]["restart_pending"] is True
    s2 = replace(settings)
    _, c2 = client(s2)
    with c2:
        assert s2.notify_emails == ("a@example.com", "b@example.com")


@pytest.mark.parametrize("changes, bad", [
    ({"public_base_url": "http://plain.example.com"}, "public_base_url"),
    ({"default_duration_minutes": 500}, "default_duration_minutes"),
    ({"time_zone": "Mars/Olympus"}, "time_zone"),
    ({"mia_tts_provider": "acme-voices"}, "mia_tts_provider"),
    ({"app_access_token": "x"}, "app_access_token"),  # managed with regenerate / revoke only
    ({"chat_disclosure": "yes"}, "chat_disclosure"),
])
def test_invalid_changes_are_refused_and_nothing_is_saved(settings, changes, bad):
    s = replace(settings)
    _, c = client(s)
    with c:
        r = c.put("/api/config", json={"changes": {"bot_name": "Changed", **changes}})
        assert r.status_code == 422 and bad in r.json()["detail"]["errors"]
        assert s.bot_name == settings.bot_name  # all or nothing


def test_reset_goes_back_to_env(settings):
    s = replace(settings, bot_name="From env")
    _, c = client(s)
    with c:
        c.put("/api/config", json={"changes": {"bot_name": "Saved name"}})
        assert s.bot_name == "Saved name"
        r = c.delete("/api/config/bot_name").json()
        assert s.bot_name == "From env" and fields(r)["bot_name"]["source"] != "saved"


def test_switching_the_ai_provider_takes_effect_without_restart(settings):
    _, c = client(replace(settings))
    with c:
        assert c.get("/api/health").json()["llm"]["is_ai"] is False
        c.put("/api/config", json={"changes": {"gemini_api_key": "test-key", "llm_provider": "gemini", "gemini_model": "gemini-3.6-flash"}})
        assert c.get("/api/health").json()["llm"] == {"name": "gemini:gemini-3.6-flash", "is_ai": True}
        c.put("/api/config", json={"changes": {"llm_provider": "ollama", "ollama_model": "llama3.2"}})
        assert c.get("/api/health").json()["llm"]["name"].endswith("llama3.2")


def test_only_admins_change_settings():
    assert permitted("viewer", "GET", "/api/config")
    for role in ("viewer", "recruiter"):
        assert not permitted(role, "PUT", "/api/config")
        assert not permitted(role, "POST", "/api/config/access-token/regenerate")
    assert permitted("admin", "PUT", "/api/config")


def test_access_token_regenerate_and_revoke(settings):
    s = replace(settings, app_access_token="old-token")
    _, c = client(s)
    with c:
        auth = {"Authorization": "Bearer old-token"}
        new = c.post("/api/config/access-token/regenerate", headers=auth).json()["token"]
        assert new != "old-token" and len(new) >= 24
        c.cookies.clear()
        assert c.get("/api/interviews", headers=auth).status_code == 401
        assert c.get("/api/interviews", headers={"Authorization": f"Bearer {new}"}).status_code == 200
        assert c.post("/api/config/access-token/revoke", headers={"Authorization": f"Bearer {new}"}).json() == {"revoked": True}
        assert s.app_access_token == "" and c.get("/api/interviews").status_code == 200  # localhost only now


async def test_provider_connection_tests():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "generativelanguage.googleapis.com":
            if request.headers.get("x-goog-api-key") == "malformed":  # what Gemini really answers a bad key with
                return httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}})
            if request.headers.get("x-goog-api-key") != "good":
                return httpx.Response(403, json={})
            return httpx.Response(200, json={"models": [{"name": "models/gemini-3.6-flash"}]})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3.2"}, {"name": "qwen3"}]})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ok = await probe("gemini", "good", "gemini-3.6-flash", client=http)
        assert ok["ok"] and ok["models"] == ["gemini-3.6-flash"]
        assert "rejected the key" in (await probe("gemini", "bad", client=http))["message"]
        bad = await probe("gemini", "malformed", client=http)
        assert bad["reason"] == "invalid_key" and "rejected the key (400)" in bad["message"]
        assert (await probe("claude", ""))["reason"] == "missing_key"
        missing = await probe("gemini", "good", "gemini-9", client=http)
        assert not missing["ok"] and "isn't available" in missing["message"]
        ollama = await probe("ollama", model="qwen3", base_url="http://localhost:11434", client=http)
        assert ollama["ok"] and ollama["models"] == ["llama3.2", "qwen3"]
        assert (await probe("claude", ""))["message"] == "Add the API key first."


def test_the_public_url_must_be_reachable_from_the_internet(settings):
    from app.config import public_url_problem
    from app.recruiting import apply_api_url, interview_url

    for bad in ("http://interviews.example.com", "https://localhost:8765", "https://127.0.0.1", "https://192.168.1.20",
                "https://10.0.0.5:8443", "https://mia.local", "https://interviews.example.com/mia", "https://x.example.com?a=1"):
        assert public_url_problem(bad), bad
    for good in ("https://interviews.example.com", "https://abc-def.trycloudflare.com", "https://interviews.example.com/"):
        assert public_url_problem(good) is None, good
    _, c = client(settings)
    with c:
        r = c.put("/api/config", json={"changes": {"public_base_url": "https://localhost:8765"}})
        assert r.status_code == 422 and "can't reach" in str(r.json())
        assert c.put("/api/config", json={"changes": {"public_base_url": "https://interviews.example.com/"}}).status_code == 200
    s = replace(settings, public_base_url="https://interviews.example.com")
    assert apply_api_url(s, "tok") == "https://interviews.example.com/api/public/apply/tok" and interview_url(s, "t2").endswith(".com/i/t2")
    assert replace(settings, public_base_url="https://localhost").meetstream_ready[0] is False


async def test_the_gemini_test_catches_a_used_up_daily_quota():
    """Listing models works even when the key's daily quota is gone (2026-11-11: an interview stopped mid-call on 429)."""
    spent = {"gemini-3.6-flash"}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "models/gemini-3.6-flash"}, {"name": "models/gemini-3.5-flash"}]})
        model = request.url.path.split("/models/")[1].split(":")[0]
        calls.append(model)
        if model in spent:
            return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                       "message": "Quota exceeded for metric: generate_content_free_tier_requests"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        out = await probe("gemini", "good", "gemini-3.6-flash", client=http)
        assert not out["ok"] and out["reason"] == "quota" and "free tier" in out["message"] and "billing" in out["message"]
        # The app's model is fine but the interview's conversation model has run out: still a failure, naming that model.
        voice = await probe("gemini", "good", "gemini-3.5-flash", client=http, also_models=("gemini-3.6-flash",))
        assert not voice["ok"] and "gemini-3.6-flash" in voice["message"]
        spent.clear()
        ok = await probe("gemini", "good", "gemini-3.5-flash", client=http, also_models=("gemini-3.6-flash",))
        assert ok["ok"] and "answered a test request" in ok["message"]
        assert calls.count("gemini-3.5-flash") >= 1 and "gemini-3.6-flash" in calls
