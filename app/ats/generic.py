"""Generic: Recruence's own signed JSON, both ways. For Workday (through its integration tools or middleware), Zapier, Make,
or any ATS with outgoing webhooks, and for scripts. docs/ATS_INTEGRATIONS.md has the format.

In: POST /webhooks/ats/<token> with
    X-MIA-Timestamp: unix seconds   X-MIA-Signature: sha256=<hex of HMAC-SHA256(secret, "<timestamp>.<body>")>
and a body like {"event_id", "job": {...}, "candidate": {...}, "application": {...}, "resume": {...}}. Other shapes
work with a field mapping (Recruence field -> dotted path in the payload).
Out: the same signing, POSTed to the integration's callback URL: {"event": "interview.result", "application": {...}, "result": {...}}.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx

from app.ats.base import Adapter, AdapterError, InboundApplication, InboundEvent, Result, SignatureError, http_error
from app.compare import same

MAX_SKEW_SECONDS = 300
DEFAULT_FIELDS = {
    "event_id": "event_id", "application_id": "application.id", "candidate_id": "candidate.id", "job_id": "job.id",
    "job_title": "job.title", "job_description": "job.description", "full_name": "candidate.name", "email": "candidate.email",
    "phone": "candidate.phone", "location": "candidate.location", "current_role": "candidate.current_role", "links": "candidate.links",
    "resume_text": "resume.text", "resume_url": "resume.url", "resume_filename": "resume.filename", "answers": "application.answers",
    "url": "application.url", "screening": "application.screening",
}


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def get_path(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data


class GenericAdapter(Adapter):
    provider = "generic"
    label = "Generic (signed JSON: Workday via middleware, Zapier, scripts)"
    verified = True  # B-9, 12 November 2026: signed webhooks in, results out over public HTTPS
    capabilities = {
        "inbound": "Webhook with Recruence's JSON (or your shape, with a field mapping), signed with HMAC-SHA256",
        "candidate details": "In the webhook",
        "resume": "Text in the webhook, or a URL Recruence downloads",
        "screening decision": "Optional application.screening: eligible / not_eligible",
        "results back": "Signed POST to your callback URL",
    }
    secret_fields = ("webhook_secret",)
    setting_fields = ("callback_url", "field_map")

    def verify(self, headers: dict[str, str], body: bytes, secrets: dict[str, str]) -> None:
        secret = secrets.get("webhook_secret", "")
        ts, given = headers.get("x-mia-timestamp", ""), headers.get("x-mia-signature", "")
        if not secret or not ts or not given:
            raise SignatureError("The webhook isn't signed (X-MIA-Timestamp and X-MIA-Signature)")
        try:
            if abs(time.time() - int(ts)) > MAX_SKEW_SECONDS:
                raise SignatureError("The webhook's timestamp is too old (replayed?)")
        except ValueError:
            raise SignatureError("X-MIA-Timestamp isn't a number") from None
        if not same(given, sign(secret, ts, body)):
            raise SignatureError()

    def parse(self, payload: Any, settings: dict) -> InboundEvent:
        fields = {**DEFAULT_FIELDS, **(settings.get("field_map") or {})}
        get = lambda key: get_path(payload, fields[key])  # noqa: E731
        app_id = str(get("application_id") or "")
        if not app_id:
            raise AdapterError("The webhook has no application id (application.id, or your field mapping's)", retry=False)
        answers = []
        for a in get("answers") or []:
            if isinstance(a, dict) and a.get("question"):
                answers.append((str(a["question"])[:300], str(a.get("answer", ""))[:4000]))
        screening = str(get("screening") or "").lower()
        inbound = InboundApplication(
            external_application_id=app_id, external_candidate_id=str(get("candidate_id") or ""), external_job_id=str(get("job_id") or ""),
            job_title=str(get("job_title") or ""), job_description=str(get("job_description") or ""),
            full_name=str(get("full_name") or ""), email=str(get("email") or ""), phone=str(get("phone") or ""),
            location=str(get("location") or ""), current_role=str(get("current_role") or ""), links=str(get("links") or ""),
            resume_text=str(get("resume_text") or ""), resume_url=str(get("resume_url") or ""), resume_filename=str(get("resume_filename") or ""),
            answers=answers, url=str(get("url") or ""),
            ats_screening=True if screening == "eligible" else False if screening == "not_eligible" else None)
        return InboundEvent(event_id=str(get("event_id") or f"application:{app_id}"), kind="application", applications=[inbound])

    async def push_result(self, client: httpx.AsyncClient, ref: dict, result: Result, secrets: dict[str, str], settings: dict) -> str:
        url = settings.get("callback_url", "")
        if not url:
            raise AdapterError("No callback URL is set for results", retry=False)
        body = json.dumps({"event": "interview.result", "delivery_id": uuid.uuid4().hex, "application": ref,
                           "result": result.__dict__}, separators=(",", ":"), ensure_ascii=False).encode()
        ts = str(int(time.time()))
        try:
            r = await client.post(url, content=body, headers={"Content-Type": "application/json", "X-MIA-Event": "interview.result",
                                                             "X-MIA-Timestamp": ts, "X-MIA-Signature": sign(secrets.get("webhook_secret", ""), ts, body)})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise http_error("The callback URL", e) from e
        return f"posted to the callback URL (HTTP {r.status_code})"

    async def test(self, client: httpx.AsyncClient, secrets: dict[str, str], settings: dict) -> str:
        if not secrets.get("webhook_secret") or len(secrets["webhook_secret"]) < 16:
            raise AdapterError("Set a webhook secret of at least 16 characters", retry=False)
        if not settings.get("callback_url"):
            return "Ready to receive webhooks. No callback URL, so results aren't sent back."
        body = json.dumps({"event": "test", "delivery_id": uuid.uuid4().hex}).encode()
        ts = str(int(time.time()))
        try:
            r = await client.post(settings["callback_url"], content=body, headers={
                "Content-Type": "application/json", "X-MIA-Event": "test", "X-MIA-Timestamp": ts, "X-MIA-Signature": sign(secrets["webhook_secret"], ts, body)})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise http_error("The callback URL", e) from e
        return f"Ready. The callback URL answered the test (HTTP {r.status_code})."
