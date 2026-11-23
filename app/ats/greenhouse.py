"""Greenhouse: web hooks in, Harvest API notes out.

From Greenhouse's documentation (developers.greenhouse.io); not verified against a live account here:
- Web hooks (Configure → Dev Center → Web Hooks) POST {"action": ..., "payload": {"application": {...}}}, with the
  candidate inside (names, email_addresses, phone_numbers, attachments with signed resume URLs) and the job(s).
  Each delivery carries `Signature: sha256 <hex>`, HMAC-SHA256 of the body with the web hook's secret key.
  A "ping" is sent when the hook is created.
- Harvest API: HTTP Basic with the API key as user name. Notes: POST /v1/candidates/{id}/activity_feed/notes with
  an On-Behalf-Of user id. Greenhouse is moving Harvest to v3 (OAuth); the base URL is a setting for that reason.

Recruence acts on "new_candidate_application" (or, with an "only at stage" setting, on "candidate_stage_change" into that
stage, e.g. "AI Interview").
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import httpx

from app.ats.base import Adapter, AdapterError, InboundApplication, InboundEvent, Result, SignatureError, http_error, result_text
from app.compare import same

DEFAULT_API = "https://harvest.greenhouse.io/v1"


def _auth(secrets: dict[str, str]) -> dict[str, str]:
    key = secrets.get("api_key", "")
    if not key:
        raise AdapterError("Set the Harvest API key", retry=False)
    return {"Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode()}


class GreenhouseAdapter(Adapter):
    provider = "greenhouse"
    label = "Greenhouse"
    capabilities = {
        "inbound": "Web hooks: new candidate application, candidate stage change (signed: Signature header, HMAC-SHA256)",
        "candidate details": "In the web hook (name, email, phone, answers, resume link)",
        "resume": "Downloaded from the web hook's attachment link (signed, short-lived)",
        "screening decision": "Use 'Trust the ATS's screening' with a stage hook: reaching the stage means eligible",
        "results back": "A note on the candidate (Harvest API), as the user you choose",
        "status changes back": "Not automated: moving the application needs Greenhouse stage ids; the note says the outcome",
    }
    secret_fields = ("webhook_secret", "api_key")
    setting_fields = ("on_behalf_of_user_id", "stage_name", "api_base")

    def verify(self, headers: dict[str, str], body: bytes, secrets: dict[str, str]) -> None:
        secret = secrets.get("webhook_secret", "")
        given = headers.get("signature", "")
        if not secret or not given.startswith("sha256 "):
            raise SignatureError("The web hook isn't signed (Signature: sha256 …)")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not same(given.split(" ", 1)[1].strip(), expected):
            raise SignatureError()

    def parse(self, payload: Any, settings: dict) -> InboundEvent:
        action = str((payload or {}).get("action", ""))
        app = ((payload or {}).get("payload") or {}).get("application") or {}
        app_id = str(app.get("id") or "")
        event_id = f"{action}:{app_id}:{(app.get('current_stage') or {}).get('id', '')}"
        if action == "ping":
            return InboundEvent(event_id=f"ping:{payload.get('payload', {}).get('web_hook_id', '')}", kind="ping", ignored="Greenhouse ping")
        wanted_stage = (settings.get("stage_name") or "").strip().lower()
        if action == "candidate_stage_change":
            stage = str((app.get("current_stage") or {}).get("name", "")).strip().lower()
            if not wanted_stage or stage != wanted_stage:
                return InboundEvent(event_id=event_id, kind=action, ignored=f"stage {stage or '?'} isn't the one Recruence acts on")
        elif action == "new_candidate_application":
            if wanted_stage:
                return InboundEvent(event_id=event_id, kind=action, ignored="waiting for the stage change instead")
        else:
            return InboundEvent(event_id=event_id, kind=action or "unknown", ignored=f"Recruence doesn't act on {action or 'this event'}")
        if not app_id:
            raise AdapterError("The web hook has no application id", retry=False)
        cand = app.get("candidate") or {}
        emails = [e.get("value", "") for e in cand.get("email_addresses") or [] if isinstance(e, dict)]
        phones = [p.get("value", "") for p in cand.get("phone_numbers") or [] if isinstance(p, dict)]
        sites = [w.get("value", "") for w in cand.get("website_addresses") or [] if isinstance(w, dict)]
        resume = next((a for a in cand.get("attachments") or [] if isinstance(a, dict) and a.get("type") == "resume"), {})
        job = (app.get("jobs") or [{}])[0] or {}
        answers = [(str(a.get("question", ""))[:300], str(a.get("answer", "") or "")[:4000])
                   for a in app.get("answers") or [] if isinstance(a, dict) and a.get("question")]
        return InboundEvent(event_id=event_id, kind=action, applications=[InboundApplication(
            external_application_id=app_id, external_candidate_id=str(cand.get("id") or ""), external_job_id=str(job.get("id") or ""),
            job_title=str(job.get("name") or ""), full_name=" ".join(x for x in (cand.get("first_name"), cand.get("last_name")) if x),
            email=emails[0] if emails else "", phone=phones[0] if phones else "", links=", ".join(sites),
            current_role=" at ".join(x for x in (cand.get("title"), cand.get("company")) if x),
            resume_url=str(resume.get("url") or ""), resume_filename=str(resume.get("filename") or ""), answers=answers,
            ats_screening=True if action == "candidate_stage_change" else None)])

    async def push_result(self, client: httpx.AsyncClient, ref: dict, result: Result, secrets: dict[str, str], settings: dict) -> str:
        user = str(settings.get("on_behalf_of_user_id") or "").strip()
        if not user:
            raise AdapterError("Set the Greenhouse user id notes are posted as", retry=False)
        cid = ref.get("candidate_id")
        if not cid:
            raise AdapterError("No Greenhouse candidate id for this application", retry=False)
        base = (settings.get("api_base") or DEFAULT_API).rstrip("/")
        try:
            r = await client.post(f"{base}/candidates/{cid}/activity_feed/notes",
                                  headers={**_auth(secrets), "On-Behalf-Of": user, "Content-Type": "application/json"},
                                  json={"user_id": int(user) if user.isdigit() else user, "body": result_text(result), "visibility": "admin_only"})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise http_error("Greenhouse", e) from e
        return "note added to the Greenhouse candidate"

    async def test(self, client: httpx.AsyncClient, secrets: dict[str, str], settings: dict) -> str:
        base = (settings.get("api_base") or DEFAULT_API).rstrip("/")
        if not secrets.get("webhook_secret"):
            raise AdapterError("Set the web hook's secret key (Greenhouse shows it when you create the web hook)", retry=False)
        try:
            r = await client.get(f"{base}/users", params={"per_page": 1}, headers=_auth(secrets))
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise http_error("Greenhouse", e) from e
        return "Connected to the Harvest API; the API key works."
