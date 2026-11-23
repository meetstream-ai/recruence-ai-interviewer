"""Connection tests for the Settings page: can this provider be reached with this key, and which models does it offer?

Each test lists the provider's models and checks that the chosen model is among them. For Gemini it then sends each
model Recruence will use one tiny request (a few tokens): listing models works even when the key's daily quota is used
up, and on the free tier (about 20 requests per model a day) that quota ends an interview mid-call (live calls
16 and 18, and 2026-11-11). Keys travel only in request headers and are never logged.
"""

from __future__ import annotations

from typing import Optional

import httpx

TIMEOUT = 10.0


class ProbeResult(dict):
    pass


def _result(ok: bool, message: str, models: Optional[list[str]] = None, reason: str = "") -> dict:
    """reason: "" when ok, else missing_key, invalid_key, model_unavailable, quota, unreachable or error."""
    return {"ok": ok, "message": message, "models": models or [], "reason": "" if ok else reason or "error"}


async def list_models(provider: str, api_key: str = "", base_url: str = "", client: Optional[httpx.AsyncClient] = None) -> list[str]:
    """Model ids the provider offers. Raises httpx.HTTPStatusError / httpx.HTTPError on failure."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        if provider == "gemini":
            r = await client.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 1000},
                                 headers={"x-goog-api-key": api_key})
            r.raise_for_status()
            return [m["name"].removeprefix("models/") for m in r.json().get("models", [])]
        if provider == "claude":
            r = await client.get("https://api.anthropic.com/v1/models", params={"limit": 1000},
                                 headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        if provider in ("openai", "xai"):
            if provider == "xai":
                from app.config import XAI_BASE_URL
                base_url = XAI_BASE_URL
            r = await client.get(f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        if provider == "ollama":
            r = await client.get(f"{base_url.rstrip('/')}/api/tags")
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        raise ValueError(f"No connection test for {provider!r}")
    finally:
        if own:
            await client.aclose()


async def gemini_quota(model: str, api_key: str, client: httpx.AsyncClient) -> Optional[str]:
    """None if the model answers a tiny request, else why not ("quota" when the key's limit for it is used up)."""
    r = await client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                          json={"contents": [{"role": "user", "parts": [{"text": "Reply with OK."}]}],
                                "generationConfig": {"maxOutputTokens": 5, "temperature": 0}})
    if r.status_code == 429:
        free = "free_tier" in r.text or "FreeTier" in r.text
        return "quota_free" if free else "quota"
    if r.status_code >= 400:
        return f"status {r.status_code}"
    return None


async def test_provider(provider: str, api_key: str = "", model: str = "", base_url: str = "",
                        client: Optional[httpx.AsyncClient] = None, also_models: tuple[str, ...] = ()) -> dict:
    """also_models: other models Recruence uses with this key (the interview's conversation model), checked too."""
    if provider in ("gemini", "claude", "xai") and not api_key:
        return _result(False, "Add the API key first.", reason="missing_key")
    try:
        models = await list_models(provider, api_key, base_url, client)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        # Gemini answers a bad key with 400 and API_KEY_INVALID (seen 2026-11-02), not 401/403.
        if code in (401, 403) or (code == 400 and ("API_KEY_INVALID" in e.response.text or "API key not valid" in e.response.text)):
            return _result(False, f"The provider rejected the key ({code}).", reason="invalid_key")
        return _result(False, f"The provider answered {code}.")
    except httpx.HTTPError as e:
        where = base_url or provider
        return _result(False, f"Couldn't reach {where}: {type(e).__name__}.", reason="unreachable")
    except ValueError as e:
        return _result(False, str(e))
    if model and model not in models:
        return _result(False, f"Connected, but the model \"{model}\" isn't available here ({_count(models, 'other model')} {'is' if len(models) == 1 else 'are'}).", models,
                       reason="model_unavailable")
    if provider == "gemini" and api_key:
        wanted = list(dict.fromkeys(m for m in (model, *also_models) if m and m in models))
        own = client is None
        http = client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            for m in wanted:
                try:
                    problem = await gemini_quota(m, api_key, http)
                except httpx.HTTPError as e:
                    return _result(False, f"Connected, but {m} didn't answer a test request ({type(e).__name__}).", models, reason="unreachable")
                if problem in ("quota", "quota_free"):
                    tier = (" This key is on Gemini's free tier, about 20 requests per model a day; an interview needs close to 20."
                            " Use a key with billing enabled for interviews.") if problem == "quota_free" else ""
                    return _result(False, f"Connected, but the daily quota for {m} is used up (429), so screening, plans and "
                                          f"interviews that use it will fail until it resets.{tier}", models, reason="quota")
                if problem:
                    return _result(False, f"Connected, but {m} refused a test request ({problem}).", models)
        finally:
            if own:
                await http.aclose()
        if wanted:
            return _result(True, f"Connected. {_count(models, 'model')} available; {', '.join(wanted)} answered a test request.", models)
    return _result(True, f"Connected. {_count(models, 'model')} available" + (f", including {model}." if model else "."), models)


def _count(items: list, noun: str) -> str:
    return f"{len(items)} {noun}{'' if len(items) == 1 else 's'}"
