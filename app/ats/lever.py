"""Lever: webhooks in (thin: ids only), the Lever API for details and for notes out.

From Lever's documentation (hire.lever.co/developer/documentation); not verified against a live account here:
- Webhooks (Settings → Integrations and API → Webhooks) POST {"triggeredAt": ms, "event": ..., "token", "signature",
  "data": {"opportunityId", "postingId", ...}}. The signature is HMAC-SHA256 of token + triggeredAt with the
  webhook's signature token.
- The API (HTTP Basic, the API key as user name): GET /opportunities/{id}, GET /postings/{id},
  GET /opportunities/{id}/resumes and .../download, POST /opportunities/{id}/notes?perform_as=<user id>.

Recruence acts on "applicationCreated" (or, with an "only at stage" setting, on "candidateStageChange" into that stage id).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import httpx

from app.ats.base import Adapter, AdapterError, InboundApplication, InboundEvent, Result, SignatureError, http_error, result_text
from app.compare import same

DEFAULT_API = "https://api.lever.co/v1"
MAX_RESUME_BYTES = 5 * 1024 * 1024


def _auth(secrets: dict[str, str]) -> dict[str, str]:
    key = secrets.get("api_key", "")
    if not key:
        raise AdapterError("Set the Lever API key", retry=False)
    return {"Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode()}


class LeverAdapter(Adapter):
    provider = "lever"
    label = "Lever"
    capabilities = {
        "inbound": "Webhooks: application created, candidate stage change (signed: HMAC-SHA256 of token + triggeredAt)",
        "candidate details": "Fetched from the Lever API (the webhook has ids only)",
        "resume": "Downloaded through the Lever API",
        "screening decision": "Use 'Trust the ATS's screening' with a stage hook: reaching the stage means eligible",
        "results back": "A note on the opportunity (Lever API), as the user you choose",
        "status changes back": "Not automated; the note says the outcome",
    }
    secret_fields = ("webhook_secret", "api_key")
    setting_fields = ("perform_as_user_id", "stage_id", "api_base")

    def verify(self, headers: dict[str, str], body: bytes, secrets: dict[str, str]) -> None:
        import json
        secret = secrets.get("webhook_secret", "")
        try:
            payload = json.loads(body)
        except ValueError:
            raise SignatureError("The webhook body isn't JSON") from None
        token, triggered, given = str(payload.get("token", "")), str(payload.get("triggeredAt", "")), str(payload.get("signature", ""))
        if not secret or not token or not given:
            raise SignatureError("The webhook isn't signed (token, triggeredAt, signature)")
        expected = hmac.new(secret.encode(), (token + triggered).encode(), hashlib.sha256).hexdigest()
        if not same(given, expected):
            raise SignatureError()

    def parse(self, payload: Any, settings: dict) -> InboundEvent:
        event = str((payload or {}).get("event", ""))
        data = (payload or {}).get("data") or {}
        opp = str(data.get("opportunityId") or "")
        event_id = f"{event}:{opp}:{payload.get('triggeredAt', '')}"
        wanted = (settings.get("stage_id") or "").strip()
        if event == "candidateStageChange":
            if not wanted or str(data.get("toStageId", "")) != wanted:
                return InboundEvent(event_id=event_id, kind=event, ignored="not the stage Recruence acts on")
        elif event == "applicationCreated":
            if wanted:
                return InboundEvent(event_id=event_id, kind=event, ignored="waiting for the stage change instead")
        else:
            return InboundEvent(event_id=event_id, kind=event or "unknown", ignored=f"Recruence doesn't act on {event or 'this event'}")
        if not opp:
            raise AdapterError("The webhook has no opportunityId", retry=False)
        return InboundEvent(event_id=event_id, kind=event, applications=[InboundApplication(
            external_application_id=str(data.get("applicationId") or opp), external_candidate_id=opp,
            external_job_id=str(data.get("postingId") or ""), ats_screening=True if event == "candidateStageChange" else None)])

    async def complete(self, event: InboundEvent, client: httpx.AsyncClient, secrets: dict[str, str], settings: dict) -> InboundEvent:
        base = (settings.get("api_base") or DEFAULT_API).rstrip("/")
        auth = _auth(secrets)
        try:
            for a in event.applications:
                r = await client.get(f"{base}/opportunities/{a.external_candidate_id}", headers=auth)
                r.raise_for_status()
                o = r.json().get("data") or {}
                a.full_name = str(o.get("name") or "")
                a.email = (o.get("emails") or [""])[0]
                a.phone = ((o.get("phones") or [{}])[0] or {}).get("value", "")
                a.location = str(o.get("location") or "")
                a.current_role = str(o.get("headline") or "")
                a.links = ", ".join(o.get("links") or [])
                a.url = str((o.get("urls") or {}).get("show") or "")
                if a.external_job_id:
                    p = await client.get(f"{base}/postings/{a.external_job_id}", headers=auth)
                    if p.status_code == 200:
                        post = p.json().get("data") or {}
                        a.job_title = str(post.get("text") or "")
                        a.job_description = str((post.get("content") or {}).get("description") or "")
                rs = await client.get(f"{base}/opportunities/{a.external_candidate_id}/resumes", headers=auth)
                if rs.status_code == 200 and (items := rs.json().get("data") or []):
                    first = items[0]
                    d = await client.get(f"{base}/opportunities/{a.external_candidate_id}/resumes/{first.get('id')}/download", headers=auth,
                                           follow_redirects=True)  # Lever may redirect to its file storage
                    if d.status_code == 200 and len(d.content) <= MAX_RESUME_BYTES:
                        a.resume_bytes = d.content
                        a.resume_filename = str((first.get("file") or {}).get("name") or "resume.pdf")
        except httpx.HTTPError as e:
            raise http_error("Lever", e) from e
        return event

    async def push_result(self, client: httpx.AsyncClient, ref: dict, result: Result, secrets: dict[str, str], settings: dict) -> str:
        user = str(settings.get("perform_as_user_id") or "").strip()
        if not user:
            raise AdapterError("Set the Lever user id notes are posted as", retry=False)
        opp = ref.get("candidate_id")
        if not opp:
            raise AdapterError("No Lever opportunity id for this application", retry=False)
        base = (settings.get("api_base") or DEFAULT_API).rstrip("/")
        try:
            r = await client.post(f"{base}/opportunities/{opp}/notes", params={"perform_as": user}, headers=_auth(secrets),
                                  json={"value": result_text(result)})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise http_error("Lever", e) from e
        return "note added to the Lever opportunity"

    async def test(self, client: httpx.AsyncClient, secrets: dict[str, str], settings: dict) -> str:
        base = (settings.get("api_base") or DEFAULT_API).rstrip("/")
        if not secrets.get("webhook_secret"):
            raise AdapterError("Set the webhook's signature token (Lever shows it next to the webhook)", retry=False)
        try:
            r = await client.get(f"{base}/users", params={"limit": 1}, headers=_auth(secrets))
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise http_error("Lever", e) from e
        return "Connected to the Lever API; the API key works."
