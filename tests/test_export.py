"""Signed report export (the generic ATS hook): config checks, payloads, signing, retries, wiring."""

import json
from dataclasses import replace

import httpx
from fastapi.testclient import TestClient

from app.export import ExportingSink, ReportExporter, config_problem, report_payload, signature
from app.llm import OfflineAI
from app.models import AnswerNote, EvaluationReport, Utterance, utcnow
from app.server import create_app
from app.store import Store

URL = "https://ats-bridge.example.com/mia"
SECRET = "0123456789abcdef-secret"


def report(store, interview, consent=True):
    now = utcnow()
    r = EvaluationReport(
        session_id="s1", interview_id=interview.id, role_title="Backend Engineer", candidate_name="Priya Raman",
        platform="Google Meet", started_at=now, ended_at=now, duration_seconds=600, questions_planned=3,
        questions_asked=3, follow_ups_asked=1, end_reason="completed" if consent else "consent_declined",
        consent_given=consent, generated_by="test", notes=[AnswerNote(question_id="q1", note="Concrete.")],
        transcript=[Utterance(speaker="Priya Raman", role="candidate", text="I led a migration.")],
    )
    if store.get_session("s1") is None:
        store.create_session("s1", interview.id, "Google Meet")
    return r


def recorder(responses):
    """httpx client factory answering with the given status codes (an exception instance raises), recording requests."""
    seen, queue = [], list(responses)

    def handler(request):
        seen.append(request)
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return httpx.Response(nxt)

    return seen, lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_config_problems():
    assert config_problem(URL, SECRET) is None
    assert config_problem("http://localhost:9000/hook", SECRET) is None
    assert "https" in config_problem("http://ats.example.com/hook", SECRET)
    assert "valid URL" in config_problem("ats.example.com", SECRET)
    assert "REPORT_WEBHOOK_SECRET" in config_problem(URL, "short")


def test_payloads_leave_out_what_they_should(store, ready_interview):
    consented = report_payload(report(store, ready_interview), include_transcript=False)
    assert "transcript" not in consented and consented["evaluated"] is False and consented["notes"][0]["note"] == "Concrete."
    assert "key_moments" not in consented and consented["talk"]["candidate_words"] == 4  # moments point into the transcript
    full = report_payload(report(store, ready_interview), include_transcript=True)
    assert full["transcript"][0]["text"] == "I led a migration." and "key_moments" in full
    declined = report_payload(report(store, ready_interview, consent=False), include_transcript=True)
    assert declined == {"session_id": "s1", "interview_id": ready_interview.id, "role_title": "Backend Engineer",
                        "candidate_name": "Priya Raman", "consent_given": False, "end_reason": "consent_declined", "evaluated": False}


async def test_delivery_is_signed_and_audited(store, ready_interview):
    seen, factory = recorder([200])
    exporter = ReportExporter(URL, SECRET, store, client_factory=factory, backoff_s=0)
    assert await exporter.deliver("report.completed", {"k": "v"}, ready_interview.id, "s1")
    [req] = seen
    body = req.content
    assert req.headers["X-MIA-Signature"] == signature(SECRET, req.headers["X-MIA-Timestamp"], body)
    envelope = json.loads(body)
    assert envelope["event"] == req.headers["X-MIA-Event"] == "report.completed" and envelope["data"] == {"k": "v"}
    assert envelope["delivery_id"] == req.headers["X-MIA-Delivery"]
    [entry] = store.audit_log(5, ready_interview.id)
    assert entry["action"] == "report_exported" and "HTTP 200" in entry["detail"]


async def test_retries_then_gives_up_without_content_in_the_audit(store, ready_interview):
    seen, factory = recorder([503, httpx.ConnectError("down"), 200])
    exporter = ReportExporter(URL, SECRET, store, client_factory=factory, backoff_s=0)
    assert await exporter.deliver("report.completed", {"k": "v"}, ready_interview.id, "s1")
    assert len(seen) == 3 and len({r.headers["X-MIA-Delivery"] for r in seen}) == 1  # same delivery id on retries

    seen, factory = recorder([400])
    exporter = ReportExporter(URL, SECRET, store, client_factory=factory, backoff_s=0)
    assert not await exporter.deliver("decision.recorded", {"note": "private"}, ready_interview.id, None)
    assert len(seen) == 1  # a 4xx rejection isn't retried
    entry = store.audit_log(1, ready_interview.id)[0]
    assert entry["action"] == "report_export_failed" and entry["detail"] == "decision.recorded; HTTP 400"


async def test_exporting_sink_saves_then_sends(store, ready_interview):
    seen, factory = recorder([200])
    exporter = ReportExporter(URL, SECRET, store, client_factory=factory, backoff_s=0)
    r = report(store, ready_interview)
    ExportingSink(store, exporter).push_report(r)
    assert store.get_report("s1") is not None  # saved before anything is sent
    await exporter.aclose()
    assert json.loads(seen[0].content)["data"]["session_id"] == "s1"


def test_disabled_without_valid_config(store):
    assert not ReportExporter("", "", store).enabled
    bad = ReportExporter("http://ats.example.com/hook", SECRET, store)
    assert not bad.enabled and "https" in bad.problem


def test_decision_is_exported(settings, store, ready_interview):
    app = create_app(settings=replace(settings, report_webhook_url=URL, report_webhook_secret=SECRET),
                     store=store, ai=OfflineAI(), run_loops=False)
    seen, factory = recorder([200])
    app.state.exporter._client_factory = factory
    store.save_interview(ready_interview.model_copy(update={"status": "completed"}))
    with TestClient(app) as c:
        assert c.get("/api/health").json()["report_export"] == {"enabled": True, "problem": ""}
        assert c.post(f"/api/interviews/{ready_interview.id}/decision", json={"decision": "advance", "note": "Next round"}).status_code == 200
    data = json.loads(seen[0].content)["data"]
    assert seen[0].headers["X-MIA-Event"] == "decision.recorded"
    assert data["decision"] == "advance" and data["note"] == "Next round" and data["candidate_name"] == "Priya Raman"
