"""External ATS candidates into Recruence's one pipeline, and results back out.

In (POST /webhooks/ats/<token>):
  verify the provider's signature -> parse -> skip a delivery already processed -> fill in details the webhook
  lacks -> for each candidate: the role (mapped from the ATS's job, or created as a draft), then the application:
  the same one if this ATS application was seen before, else an existing application of that email for the role
  (linked, not duplicated), else a new one with source "ats" and the ATS's ids -> screening by Recruence
  (process_application, as for every application) or the ATS's own decision -> invitation by Recruence's invite code.
Out (after an interview's report, or a recruiter's decision, for an application that came from an ATS):
  the result in the provider's terms, in the background, retried, logged; the application's sync status follows.

Nothing here screens, invites or interviews by itself: it hands candidates to the same functions the application
page uses (`Pipeline`). A provider that fails only marks its own events and applications as failed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import ValidationError

from app.ats import ADAPTERS
from app.ats.base import Adapter, AdapterError, InboundApplication, Result, SignatureError, resolves_publicly, url_problem
from app.models import (Application, ApplicationAnswer, ApplicationInput, EvaluationReport, ExternalRef, Interview, RoleSetup,
                        Screening, SyncState, utcnow)
from app.resume import MAX_BYTES, ResumeError, extract_resume
from app.secret_box import SecretBox, SecretError
from app.store import CURRENT_WORKSPACE, Store

logger = logging.getLogger(__name__)

PUSH_ATTEMPTS = 3
RETRY_LIMIT = 8  # failed results are tried again hourly, up to this many attempts in all
SCREENING_MODES = ("mia", "trust_ats")
# What a result is called when it's sent back; the status mapping gives each one the ATS's own label.
RESULT_STAGES = ("passed", "not_passed", "incomplete", "evaluation_failed", "advance", "hold", "reject")


def score_of(report: Optional[EvaluationReport]) -> Optional[int]:
    """The interview's score out of 100, as the role's ranking computes it (app/roles.py)."""
    ev = report.evaluation if report else None
    scores = [s.score for s in ev.competency_scores if s.score is not None] if ev else []
    return round(sum(scores) / len(scores) * 20) if scores else None


class IngestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Pipeline:
    """Recruence's own steps, from app/server.py: the same ones every application goes through."""

    screen: Callable[[str], None]  # schedule process_application(application_id)
    invite: Callable[[Any, Application], Awaitable[None]]  # invite_application(role, application)
    background: Callable[[Any, str], Any]
    report_url: Callable[[str], str]  # session id -> link to the report in the dashboard
    passing_score: Callable[[Interview], int]  # the role's pass mark, out of 100


def public_view(row: dict, base_url: str) -> dict:
    """An integration for the dashboard: settings, which credentials are set (never their values), the webhook URL."""
    adapter = ADAPTERS.get(row["provider"])
    data = row["data"]
    return {
        "id": row["id"], "provider": row["provider"], "provider_label": adapter.label if adapter else row["provider"],
        "name": data.get("name", ""), "enabled": bool(row["enabled"]), "settings": data.get("settings", {}),
        "secrets": {k: bool(data.get("secrets", {}).get(k)) for k in (adapter.secret_fields if adapter else ())},
        "capabilities": adapter.capabilities if adapter else {},
        "verified": bool(adapter and adapter.verified),
        "status_note": "" if adapter and adapter.verified else Adapter.UNVERIFIED_NOTE,
        "webhook_url": f"{base_url.rstrip('/')}/webhooks/ats/{row['inbound_token']}" if base_url else "",
        "webhook_path": f"/webhooks/ats/{row['inbound_token']}",
        "health": data.get("health", {}), "created_at": row["created_at"],
    }


class IntegrationService:
    def __init__(self, store: Store, box: SecretBox, pipeline: Pipeline,
                 http: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(timeout=20, follow_redirects=False)):
        self.store, self.box, self.pipeline, self.http = store, box, pipeline, http
        self.backoff = 1.0  # seconds before the 2nd attempt (doubling); tests set 0

    # ---------- configuration ----------

    def secrets_of(self, row: dict) -> dict[str, str]:
        out = {}
        for k, v in (row["data"].get("secrets") or {}).items():
            try:
                out[k] = self.box.decrypt(v)
            except SecretError:
                logger.warning("integration %s: a saved credential can't be read", row["id"])
        return out

    def save(self, row: Optional[dict], provider: str, name: str, settings: dict, secrets_in: dict[str, str]) -> dict:
        """Create (row None) or update. Secrets: a value replaces, "" keeps the saved one, None removes it."""
        adapter = ADAPTERS[provider]
        data = dict(row["data"]) if row else {"health": {}}
        clean = {k: v for k, v in (settings or {}).items() if k in (*adapter.setting_fields, "screening", "role_map", "status_map")}
        if clean.get("screening", "mia") not in SCREENING_MODES:
            raise ValueError("Screening must be 'mia' or 'trust_ats'")
        data["name"] = (name or adapter.label)[:80]
        data["settings"] = {**data.get("settings", {}), **clean}
        stored = dict(data.get("secrets") or {})
        for k in adapter.secret_fields:
            if k in secrets_in:
                value = secrets_in[k]
                if value is None:
                    stored.pop(k, None)
                elif value:
                    stored[k] = self.box.encrypt(str(value))
        data["secrets"] = stored
        if row is None:
            return self.store.create_integration(provider, data)
        self.store.update_integration(row["id"], data=data)
        return self.store.integration(row["id"])

    async def test(self, row: dict) -> dict:
        adapter = ADAPTERS[row["provider"]]
        try:
            async with self.http() as client:
                message = await adapter.test(client, self.secrets_of(row), row["data"].get("settings", {}))
            self._health(row, ok=True, message=message, key="last_test")
            return {"ok": True, "message": message}
        except AdapterError as e:
            self._health(row, ok=False, message=str(e), key="last_test")
            return {"ok": False, "message": str(e)}

    def _health(self, row: dict, ok: bool, message: str, key: str) -> None:
        current = self.store.integration_by_token(row["inbound_token"]) or row
        data = dict(current["data"])
        data["health"] = {**data.get("health", {}), key: {"ok": ok, "message": message[:300], "at": utcnow().isoformat()}}
        self.store.update_integration(row["id"], data=data)

    # ---------- in ----------

    async def ingest(self, token: str, headers: dict[str, str], body: bytes) -> dict:
        row = self.store.integration_by_token(token)
        if row is None or not row["enabled"]:
            raise IngestError(404, "Unknown webhook")
        adapter = ADAPTERS.get(row["provider"])
        if adapter is None:
            raise IngestError(404, "Unknown provider")
        lower = {k.lower(): v for k, v in headers.items()}
        try:
            adapter.verify(lower, body, self.secrets_of(row))
        except SignatureError as e:
            self.store.integration_event(row["id"], "in", "webhook", "rejected", str(e))
            raise IngestError(401, str(e)) from None
        try:
            payload = json.loads(body or b"{}")
            event = adapter.parse(payload, row["data"].get("settings", {}))
        except (ValueError, AdapterError) as e:
            self.store.integration_event(row["id"], "in", "webhook", "failed", f"Couldn't read the webhook: {e}")
            raise IngestError(422, f"Couldn't read the webhook: {e}") from None
        dedupe = f"in:{row['id']}:{event.event_id}"
        if self._seen(dedupe):
            self.store.integration_event(row["id"], "in", event.kind, "duplicate", f"Already processed ({event.event_id})")
            return {"accepted": 0, "duplicate": True}
        if event.ignored:
            self.store.integration_event(row["id"], "in", event.kind, "ignored", event.ignored, dedupe_key=dedupe)
            return {"accepted": 0, "ignored": event.ignored}
        try:
            async with self.http() as client:
                event = await adapter.complete(event, client, self.secrets_of(row), row["data"].get("settings", {}))
                outcomes = [await self._take(row, a, client) for a in event.applications]
        except AdapterError as e:
            self.store.integration_event(row["id"], "in", event.kind, "failed", str(e))
            self._health(row, ok=False, message=str(e), key="last_inbound")
            # The provider retries failed deliveries; this one isn't marked as processed, so its retry counts.
            raise IngestError(502 if e.retry else 422, str(e)) from None
        accepted = sum(1 for o in outcomes if o[0])
        message = "; ".join(o[1] for o in outcomes)
        self.store.integration_event(row["id"], "in", event.kind, "ok" if accepted == len(outcomes) else "failed", message,
                                     application_id=next((o[2] for o in outcomes if o[2]), None), dedupe_key=dedupe)
        self._health(row, ok=accepted == len(outcomes), message=message, key="last_inbound")
        return {"accepted": accepted, "results": [o[1] for o in outcomes]}

    def _seen(self, dedupe: str) -> bool:
        return self.store.integration_event_seen(dedupe)

    async def _take(self, row: dict, a: InboundApplication, client: httpx.AsyncClient) -> tuple[bool, str, Optional[str]]:
        """One candidate into Recruence. Returns (taken, what happened, application id)."""
        token = CURRENT_WORKSPACE.set(row["workspace_id"])  # everything below is in the integration's workspace
        try:
            if not a.email or "@" not in a.email:
                return False, f"application {a.external_application_id}: no email address, not taken", None
            role = self._role_for(row, a)
            ref = ExternalRef(integration_id=row["id"], provider=row["provider"], candidate_id=a.external_candidate_id,
                              application_id=a.external_application_id, job_id=a.external_job_id, url=a.url)
            existing = self.store.application_by_external(row["id"], a.external_application_id) or self.store.application_by_email(role.id, a.email)
            if existing is not None:
                # Already in Recruence (sent before, or applied on the page): linked to the ATS, never screened or invited again,
                # so a recruiter's decision on it stands.
                if not existing.external:
                    self._link(existing, ref)
                return True, f"application {a.external_application_id}: already in Recruence ({existing.status}), not added again", existing.id
            candidate = ApplicationInput(full_name=(a.full_name or a.email.split("@")[0])[:200], email=a.email.strip()[:320],
                                         phone=a.phone[:40], location=a.location[:200], current_role=a.current_role[:200], links=a.links[:500])
            resume_text, resume_name, resume_error = await self._resume(a, client)
            answers = [ApplicationAnswer(question=q, answer=ans) for q, ans in a.answers][:20]
            try:
                app = self.store.create_application(role.id, candidate, source="ats", external=ref, answers=answers, resume_text=resume_text,
                                                    resume_filename=resume_name, resume_error=resume_error,
                                                    sync=SyncState(status="not_needed"))
            except Exception as e:  # noqa: BLE001 - duplicate (raced) or invalid
                return False, f"application {a.external_application_id}: couldn't be added ({type(e).__name__})", None
            self.store.audit("application_received", detail=f"{role.id}: {app.id} from {row['provider']}")
            return True, await self._screen(row, role, app, a), app.id
        except ValidationError as e:  # invalid candidate fields: their names only (the values are the candidate's)
            fields = ", ".join(sorted({str(err["loc"][0]) for err in e.errors() if err.get("loc")}))
            return False, f"application {a.external_application_id}: invalid {fields or 'fields'}, not taken", None
        finally:
            CURRENT_WORKSPACE.reset(token)

    def _link(self, app: Application, ref: ExternalRef) -> Application:
        self.store.set_external_key(app.id, f"{ref.integration_id}:{ref.application_id}")
        return self.store.save_application(app.model_copy(update={"external": ref}))

    def _role_for(self, row: dict, a: InboundApplication):
        settings = row["data"].get("settings", {})
        mapped = (settings.get("role_map") or {}).get(a.external_job_id)
        role = self.store.get_role(mapped) if mapped else None
        if role is not None:
            return role
        # An ATS job Recruence hasn't seen: a draft role from it, mapped from now on. A recruiter sets its questions and
        # interview room before anyone can be invited (the invitation says so until then).
        title = (a.job_title or f"{row['data'].get('name') or row['provider']} job {a.external_job_id or '?'}")[:200]
        role = self.store.create_role(RoleSetup(role_title=title, job_description=(a.job_description or f"Imported from {row['provider']}: {title}")[:30_000]))
        current = self.store.integration_by_token(row["inbound_token"])
        data = dict(current["data"])
        data["settings"] = {**data.get("settings", {}), "role_map": {**(data.get("settings", {}).get("role_map") or {}), a.external_job_id: role.id}}
        self.store.update_integration(row["id"], data=data)
        row["data"] = data
        self.store.audit("role_created", detail=f"{role.id}: from {row['provider']} job {a.external_job_id}")
        return role

    async def _resume(self, a: InboundApplication, client: httpx.AsyncClient) -> tuple[str, str, str]:
        data, name = a.resume_bytes, a.resume_filename or "resume"
        if not data and a.resume_url and url_problem(a.resume_url) is None:
            if not await resolves_publicly(a.resume_url):
                return a.resume_text, "", "The resume link leads to a private network address, so it wasn't downloaded"
            try:
                r = await client.get(a.resume_url)
                if r.status_code == 200 and len(r.content) <= MAX_BYTES:
                    data = r.content
                else:
                    return a.resume_text, "", f"The resume couldn't be downloaded (HTTP {r.status_code})"
            except httpx.HTTPError as e:
                return a.resume_text, "", f"The resume couldn't be downloaded ({type(e).__name__})"
        if data:
            try:
                # In a worker thread with a time limit, like an upload: a slow file mustn't stall the server.
                return (await asyncio.wait_for(asyncio.to_thread(extract_resume, name, data), 20)).text, name, ""
            except asyncio.TimeoutError:
                return a.resume_text, "", "Reading the resume file took too long"
            except ResumeError as e:
                return a.resume_text, name, str(e)
        return a.resume_text[:60_000], "", ""

    async def _screen(self, row: dict, role, app: Application, a: InboundApplication) -> str:
        mode = row["data"].get("settings", {}).get("screening", "mia")
        if mode == "trust_ats" and a.ats_screening is not None:
            ok = a.ats_screening
            screening = Screening(outcome="eligible" if ok else "not_eligible", screened_at=utcnow(), screened_by=row["provider"],
                                  reasons=[f"{'Eligible' if ok else 'Not eligible'} in {row['data'].get('name') or row['provider']} (its screening is trusted)"])
            app = self.store.save_application(app.model_copy(update={"screening": screening, "status": screening.outcome}))
            if ok and role.hiring.auto_invite:
                await self.pipeline.invite(role, app)
                return f"application {a.external_application_id}: eligible in the ATS, invitation sent by Recruence"
            return f"application {a.external_application_id}: {screening.outcome} in the ATS"
        app = self.store.save_application(app.model_copy(update={"status": "screening"}))
        self.pipeline.screen(app.id)
        return f"application {a.external_application_id}: added, screening by Recruence"

    # ---------- out ----------

    def result_ready(self, report: EvaluationReport) -> None:
        """After a report is saved: send it to the ATS the candidate came from, if any (background)."""
        self.pipeline.background(self._send_for_interview(report.interview_id, report=report), f"ats result {report.session_id}")

    def decision_recorded(self, interview: Interview) -> None:
        self.pipeline.background(self._send_for_interview(interview.id, decision=True), f"ats decision {interview.id}")

    async def _send_for_interview(self, interview_id: str, report: Optional[EvaluationReport] = None, decision: bool = False) -> None:
        interview = self.store.get_interview(interview_id)
        if interview is None or not interview.application_id:
            return
        app = self.store.get_application(interview.application_id)
        if app is None or app.external is None:
            return
        row = self.store.integration(app.external.integration_id)
        if row is None or not row["enabled"]:
            return
        report = report or (self.store.get_report(interview.latest_session_id) if interview.latest_session_id else None)
        score = score_of(report)
        if decision and interview.decision:
            stage = interview.decision
        elif report and report.evaluation and report.evaluation.unassessed:
            stage = "incomplete"  # part of the interview wasn't assessed: the score can't pass or fail anyone
        elif score is None:
            stage = "evaluation_failed"
        else:
            stage = "passed" if score >= self.pipeline.passing_score(interview) else "not_passed"
        status_map = row["data"].get("settings", {}).get("status_map") or {}
        ev = report.evaluation if report else None
        result = Result(candidate_name=interview.setup.candidate_name, role_title=interview.setup.role_title, stage=stage,
                        mapped_status=status_map.get(stage, ""), score=score,
                        recommendation=ev.recommendation if ev else "", summary=(ev.summary if ev else "")[:600],
                        report_url=self.pipeline.report_url(report.session_id) if report else "",
                        decision=(interview.decision or "") if decision else "")
        # One send per result: a report (again once it's evaluated, after a failed evaluation) or a decision.
        what = f"decision:{interview.decided_at}" if decision else f"{report.session_id if report else ''}:{'evaluated' if score is not None else 'not evaluated'}"
        key = f"out:{row['id']}:{interview.id}:{what}"
        event_id = self.store.integration_event(row["id"], "out", "decision" if decision else "result", "pending",
                                                application_id=app.id, dedupe_key=key,
                                                payload={"ref": app.external.model_dump(), "result": result.__dict__})
        if event_id is None:
            return  # this result was sent (or is being sent) already
        self._sync_state(app.id, "pending", "")
        await self._push(row, app.id, event_id, app.external.model_dump(), result, attempts_before=0)

    async def _push(self, row: dict, application_id: str, event_id: int, ref: dict, result: Result, attempts_before: int) -> bool:
        import asyncio
        adapter = ADAPTERS[row["provider"]]
        message, retry = "", True
        attempts = attempts_before
        for i in range(PUSH_ATTEMPTS):
            attempts += 1
            try:
                async with self.http() as client:
                    message = await adapter.push_result(client, ref, result, self.secrets_of(row), row["data"].get("settings", {}))
                self.store.update_integration_event(event_id, "ok", message, attempts=attempts)
                self._sync_state(application_id, "synced", message)
                return True
            except AdapterError as e:
                message, retry = str(e), e.retry
                if not retry:
                    break
                if i < PUSH_ATTEMPTS - 1:
                    await asyncio.sleep(min(self.backoff * 2 ** i, 10))
        self.store.update_integration_event(event_id, "failed", message, attempts=attempts if retry else RETRY_LIMIT, keep_payload=retry)
        self._sync_state(application_id, "failed", message)
        logger.warning("sending a result to %s failed: %s", row["provider"], message)
        return False

    def _sync_state(self, application_id: str, status: str, message: str) -> None:
        app = self.store.get_application(application_id)
        if app is not None:
            self.store.save_application(app.model_copy(update={"sync": SyncState(status=status, at=utcnow(), message=message[:300])}))

    async def retry_failed(self, integration_id: Optional[str] = None) -> int:
        """Hourly (or "Retry now"): results that failed for a reason that may pass (provider down, rate limit) are sent again."""
        sent = 0
        for ev in self.store.failed_outbound_events(RETRY_LIMIT):
            if integration_id and ev["integration_id"] != integration_id:
                continue
            row = self.store.integration(ev["integration_id"])
            if row is None or not row["enabled"] or not ev["payload"]:
                continue
            payload = json.loads(ev["payload"])
            sent += await self._push(row, ev["application_id"], ev["id"], payload["ref"], Result(**payload["result"]), ev["attempts"])
        return sent


def new_token() -> str:
    return secrets.token_urlsafe(24)


class ResultSyncSink:
    """ReportSink step: the report is saved by the inner sinks first, then sent to the ATS the candidate came from."""

    def __init__(self, inner, svc: IntegrationService):
        self._inner, self._svc = inner, svc

    def push_report(self, report: EvaluationReport) -> str:
        saved = self._inner.push_report(report)
        try:
            self._svc.result_ready(report)
        except Exception:  # noqa: BLE001 - an ATS problem never affects saving the report
            logger.exception("queueing the ATS result failed")
        return saved
