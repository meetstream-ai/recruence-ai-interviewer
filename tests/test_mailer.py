"""Email: the candidate's invitation and the hiring team's report-ready note, with a fake SMTP sender."""

import smtplib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.llm import OfflineAI
from app.mailer import Mailer, MailError, NotifyingSink, invitation, report_ready
from app.models import CompetencyScore, EvaluationReport, LLMEvaluation, utcnow
from app.server import create_app
from app.store import Store
from tests.fakes import MEET_LINK

SMTP = {"smtp_host": "smtp.example.com", "smtp_from": "hiring@example.com", "recruiter_contact": "at talent@example.com",
        "mail_reply_to": "talent@example.com", "notify_emails": ("lead@example.com", "hr@example.com"), "dashboard_url": "https://mia.example.com"}


def sender():
    sent = []
    return sent, lambda settings, email: sent.append(email)


def report(interview_id="i1", evaluated=True):
    now = utcnow()
    ev = LLMEvaluation(recommendation="yes", summary="s", confidence="medium", competency_scores=[], strengths=[],
                       areas_for_improvement=[], suggested_next_round_focus=[]) if evaluated else None
    return EvaluationReport(session_id="s1", interview_id=interview_id, role_title="Backend Engineer", candidate_name="Priya Raman",
                            platform="Google Meet", started_at=now, ended_at=now, duration_seconds=600, questions_planned=3,
                            questions_asked=3, follow_ups_asked=1, end_reason="completed", consent_given=True, generated_by="t",
                            evaluation=ev, evaluation_error=None if evaluated else "No candidate speech", notes=[], transcript=[])


def test_invitation_gives_notice_and_a_human_alternative(settings, ready_interview):
    i = ready_interview.model_copy(update={"setup": ready_interview.setup.model_copy(update={"candidate_email": "priya@example.com"})})
    email = invitation(i, replace(settings, **SMTP), MEET_LINK, "Mon 3 Nov 2026, 10:00 IST")
    assert email.to == ["priya@example.com"] and email.reply_to == "talent@example.com"
    assert email.subject == "Your interview for the Backend Engineer role"
    for must in (MEET_LINK, "on Mon 3 Nov 2026, 10:00 IST", "conducted by an AI interviewer", "recorded and transcribed",
                 "A person on the hiring team reviews", "You can say no", "interviewed by a person", "at talent@example.com",
                 "does not count against you", '"AI Interviewer"', "headphones", "Hello Priya"):
        assert must in email.body, must
    plain = invitation(ready_interview, settings, MEET_LINK, "")
    assert plain.to == [] and "at the agreed time" in plain.body and "by replying to this email" in plain.body


def test_report_ready_note(settings):
    s = replace(settings, **SMTP)
    email = report_ready(report(), s)
    assert email.to == ["lead@example.com", "hr@example.com"]
    assert "AI recommendation: Yes (confidence medium)" in email.body and "https://mia.example.com/#/s/s1" in email.body
    assert "Not evaluated: No candidate speech" in report_ready(report(evaluated=False), settings).body
    assert "session s1 in the dashboard" in report_ready(report(), settings).body  # no DASHBOARD_URL


async def test_mailer_sends_or_explains(settings):
    with pytest.raises(MailError, match="SMTP_HOST"):
        await Mailer(settings).send(report_ready(report(), settings))
    sent, send = sender()
    m = Mailer(replace(settings, **SMTP), send=send)
    await m.send(report_ready(report(), m.settings))
    assert [e.subject for e in sent] == ["Interview report ready: Priya Raman, Backend Engineer"]

    def broken(settings, email):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
    with pytest.raises(MailError, match=r"didn't accept the username and password \(535 bad credentials\)"):
        await Mailer(m.settings, send=broken).send(report_ready(report(), m.settings))


def client(settings, send=None):
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), run_loops=False, mail_send=send)
    return app, TestClient(app)


def test_invitation_preview_and_send(settings, ready_interview):
    # Without email set up: preview works (to copy into your own email), sending explains why it can't.
    app, c = client(settings)
    store = app.state.store
    with c:
        iid = ready_interview.id
        store.save_interview(store.get_interview(iid).model_copy(update={"setup": ready_interview.setup.model_copy(update={"candidate_email": "priya@example.com"})}))
        p = c.post(f"/api/interviews/{iid}/invite/preview", json={"meeting_link": MEET_LINK, "when_text": "Mon 10:00"}).json()
        assert p["to"] == ["priya@example.com"] and p["can_send"] is False and MEET_LINK not in p["body"] and "/i/" in p["body"]
        assert store.get_interview(iid).invitation is None  # a preview makes no link
        assert c.post(f"/api/interviews/{iid}/invite", json={"meeting_link": MEET_LINK}).status_code == 409
        assert c.post(f"/api/interviews/{iid}/invite/preview", json={"meeting_link": "https://example.com/x"}).status_code == 422

    sent, send = sender()
    app, c = client(replace(settings, **SMTP), send)
    with c:
        r = c.post(f"/api/interviews/{iid}/invite", json={"when_text": "Mon 10:00"})  # falls back to the interview's own link
        assert r.status_code == 200 and r.json()["to"] == ["priya@example.com"]
        assert sent[0].to == ["priya@example.com"] and ready_interview.meeting_link not in sent[0].body and "/i/" in sent[0].body
        [entry] = [e for e in app.state.store.audit_log(20, iid) if e["action"] == "invitation_sent"]
        assert entry["detail"] == ""  # no address or content in the audit log


def test_invitation_needs_an_address_and_a_link(settings, store, setup):
    i = store.create_interview(setup)
    sent, send = sender()
    _, c = client(replace(settings, **SMTP), send)
    with c:
        assert c.post(f"/api/interviews/{i.id}/invite/preview", json={}).status_code == 422  # no meeting link anywhere
        assert c.post(f"/api/interviews/{i.id}/invite", json={"meeting_link": MEET_LINK}).status_code == 422  # no candidate email
    assert sent == []


async def test_report_ready_note_after_a_report_is_saved(settings, store, ready_interview):
    sent, send = sender()
    mailer = Mailer(replace(settings, **SMTP), send=send)
    store.create_session("s1", ready_interview.id, "Google Meet")
    NotifyingSink(store, mailer, store).push_report(report(ready_interview.id))
    assert store.get_report("s1") is not None  # saved first
    await mailer.aclose()
    assert [e.to for e in sent] == [["lead@example.com", "hr@example.com"]]
    assert "report_notification_sent" in [e["action"] for e in store.audit_log(10, ready_interview.id)]


class GmailLike:
    """An SMTP server that, like Gmail, answers a bad login with 535 and then hangs up."""

    def __init__(self, host, port, timeout=None, **_):
        self.esmtp_features, self.tried, self.closed, self.sent = {"auth": "LOGIN PLAIN XOAUTH2"}, [], False, []
        self.user = self.password = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, name):
        return name in self.esmtp_features

    def auth_plain(self, challenge=None):
        return f"\0{self.user}\0{self.password}"

    def auth_login(self, challenge=None):
        return self.user

    def auth(self, mechanism, authobject, initial_response_ok=True):
        if self.closed:
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        self.tried.append((mechanism, self.password))
        if self.password != "abcdefghijklmnop":
            self.closed = True
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted. BadCredentials")
        return 235, b"ok"

    def send_message(self, msg):
        self.sent.append(msg)


async def test_a_refused_gmail_login_says_so_and_spaces_in_an_app_password_are_ignored(settings, monkeypatch):
    servers = []
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: servers.append(GmailLike(*a, **k)) or servers[-1])
    gmail = replace(settings, smtp_host="smtp.gmail.com", smtp_port=587, smtp_from="me@gmail.com", smtp_username="me@gmail.com")
    m = Mailer(gmail)
    email = report_ready(report(), replace(gmail, notify_emails=("lead@example.com",)))
    with pytest.raises(MailError) as e:
        await m.send_with(replace(gmail, smtp_password="wrong-password"), email)
    text = str(e.value)
    assert "didn't accept the username and password (535" in text and "App Password" in text  # not "Connection unexpectedly closed"
    assert "wrong-password" not in text and servers[-1].tried == [("PLAIN", "wrong-password")]  # one attempt, one mechanism
    await m.send_with(replace(gmail, smtp_password=" abcd efgh ijkl mnop "), email)  # pasted as Google shows it
    assert servers[-1].tried == [("PLAIN", "abcdefghijklmnop")] and len(servers[-1].sent) == 1


def test_wrong_port_or_encryption_is_explained(settings):
    import ssl

    from app.mailer import explain
    s = replace(settings, smtp_host="smtp.gmail.com", smtp_port=465, smtp_ssl=False)
    assert "port 465 expects SSL/TLS from the start" in explain(s, smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
    s = replace(settings, smtp_host="smtp.gmail.com", smtp_port=587, smtp_ssl=True)
    assert "turn off \"Use SSL/TLS from the start\"" in explain(s, ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"))
    assert "turn on STARTTLS" in explain(s, smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server."))
    assert "Couldn't connect to smtp.gmail.com:587" in explain(s, ConnectionRefusedError(10061, "refused"))


def test_the_report_note_says_when_a_score_is_partial(settings):
    s = replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com")
    partial = report()
    scores = [CompetencyScore(competency_id="a", competency_name="APIs", score=5, rationale="r", evidence=["x"]),
              CompetencyScore(competency_id="b", competency_name="Databases", score=None, rationale="r", evidence=[])]
    partial = partial.model_copy(update={"evaluation": partial.evaluation.model_copy(update={"competency_scores": scores})})
    body = report_ready(partial, s).body
    assert "Score: 100/100 from the assessed competencies only" in body and "Not assessed: Databases" in body
    assert "assessed" not in report_ready(report(), s).body
