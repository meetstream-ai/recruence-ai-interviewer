"""Signed report export: the generic ATS hook.

When REPORT_WEBHOOK_URL is set, each saved report and each recruiter decision is
POSTed there as JSON, so an ATS integration (or Zapier, Make, a small script) can
pick it up. Every delivery is signed with HMAC-SHA256 over "<timestamp>.<body>"
using REPORT_WEBHOOK_SECRET:

    X-MIA-Event:     report.completed | decision.recorded
    X-MIA-Delivery:  unique id (the same on retries)
    X-MIA-Timestamp: unix seconds
    X-MIA-Signature: sha256=<hex>

Delivery runs in the background, is retried, and never affects saving the
report. Outcomes go to the audit log, without content. The transcript is left
out unless REPORT_WEBHOOK_TRANSCRIPT is on; without consent only the outcome
is sent.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

from app.models import EvaluationReport, Interview, utcnow
from app.store import Store

logger = logging.getLogger(__name__)

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def signature(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def config_problem(url: str, secret: str) -> Optional[str]:
    """Why the export can't be enabled, or None. Plain http is allowed only to this machine."""
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        return "REPORT_WEBHOOK_URL is not a valid URL"
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        return "REPORT_WEBHOOK_URL must use https (plain http only to localhost)"
    if len(secret) < 16:
        return "REPORT_WEBHOOK_SECRET must be set (at least 16 characters) to sign deliveries"
    return None


def report_payload(report: EvaluationReport, include_transcript: bool) -> dict[str, Any]:
    if not report.consent_given:
        # Nothing about the interview's content: only that it ended without consent.
        return {
            "session_id": report.session_id, "interview_id": report.interview_id,
            "role_title": report.role_title, "candidate_name": report.candidate_name,
            "consent_given": False, "end_reason": report.end_reason, "evaluated": False,
        }
    # Without the transcript, key moments are just the evidence quotes again with line numbers that point nowhere.
    data = report.model_dump(mode="json", exclude=None if include_transcript else {"transcript", "key_moments"})
    data["evaluated"] = report.evaluation is not None
    return data


def decision_payload(interview: Interview) -> dict[str, Any]:
    return {
        "interview_id": interview.id,
        "role_title": interview.setup.role_title,
        "candidate_name": interview.setup.candidate_name,
        "decision": interview.decision,
        "note": interview.decision_note,
        "decided_at": interview.decided_at.isoformat() if interview.decided_at else None,
    }


class ReportExporter:
    def __init__(
        self,
        url: str,
        secret: str,
        store: Store,
        *,
        include_transcript: bool = False,
        attempts: int = 3,
        backoff_s: float = 5.0,
        client_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(timeout=15.0),
    ):
        self.url, self._secret, self._store = url, secret, store
        self.include_transcript = include_transcript
        self._attempts, self._backoff_s = attempts, backoff_s
        self._client_factory = client_factory
        self._tasks: set[asyncio.Task] = set()
        self.problem = config_problem(url, secret) if url else None
        if self.problem:
            logger.warning("Report export disabled: %s", self.problem)

    @property
    def enabled(self) -> bool:
        return bool(self.url) and self.problem is None

    def report_saved(self, report: EvaluationReport) -> None:
        self._schedule("report.completed", report_payload(report, self.include_transcript), report.interview_id, report.session_id)

    def decision_recorded(self, interview: Interview) -> None:
        self._schedule("decision.recorded", decision_payload(interview), interview.id, None)

    def _schedule(self, event: str, data: dict, interview_id: str, session_id: Optional[str]) -> None:
        if not self.enabled:
            return
        # A fresh context, so the delivery's audit entry doesn't carry the name of whoever triggered it.
        task = asyncio.get_running_loop().create_task(self.deliver(event, data, interview_id, session_id), context=contextvars.Context())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def deliver(self, event: str, data: dict, interview_id: str, session_id: Optional[str]) -> bool:
        delivery = uuid.uuid4().hex
        body = json.dumps({"event": event, "delivery_id": delivery, "sent_at": utcnow().isoformat(), "data": data},
                          separators=(",", ":"), ensure_ascii=False).encode()
        problem = ""
        async with self._client_factory() as client:
            for attempt in range(1, self._attempts + 1):
                ts = str(int(time.time()))
                headers = {"Content-Type": "application/json", "X-MIA-Event": event, "X-MIA-Delivery": delivery,
                           "X-MIA-Timestamp": ts, "X-MIA-Signature": signature(self._secret, ts, body)}
                try:
                    resp = await client.post(self.url, content=body, headers=headers)
                    if 200 <= resp.status_code < 300:
                        self._store.audit("report_exported", interview_id, session_id, f"{event}; HTTP {resp.status_code}")
                        return True
                    problem = f"HTTP {resp.status_code}"
                    if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                        break  # the receiver rejected it; retrying won't help
                except httpx.HTTPError as e:
                    problem = type(e).__name__
                if attempt < self._attempts:
                    await asyncio.sleep(self._backoff_s * attempt)
        logger.warning("Report export of %s for %s failed: %s", event, interview_id, problem)
        self._store.audit("report_export_failed", interview_id, session_id, f"{event}; {problem}")
        return False

    async def aclose(self, timeout: float = 10.0) -> None:
        """Give deliveries in flight a moment to finish on shutdown."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)


class ExportingSink:
    """ReportSink that saves to the store, then exports in the background."""

    def __init__(self, store: Store, exporter: ReportExporter):
        self._store, self._exporter = store, exporter

    def push_report(self, report: EvaluationReport) -> str:
        saved = self._store.push_report(report)
        self._exporter.report_saved(report)
        return saved
