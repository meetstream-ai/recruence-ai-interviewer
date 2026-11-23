"""Email: the candidate's invitation and the hiring team's "report ready" note.

Plain SMTP (smtplib, run in a thread). Not configured (no SMTP_HOST or SMTP_FROM): nothing
is sent, but the invitation can still be previewed and copied into the recruiter's own email.

The invitation is the written notice before the call: that an AI conducts the interview, that
it is recorded and transcribed, that a person reviews it, and how to ask for a human
interviewer or an accommodation instead.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, Optional

from app.config import Settings
from app.models import EvaluationReport, Interview

logger = logging.getLogger(__name__)

RETRY_DELAYS = (30.0, 120.0)  # seconds before the second and third try of a background email
REC_WORDS = {"strong_yes": "Strong yes", "yes": "Yes", "no": "No", "strong_no": "Strong no", "insufficient_evidence": "Insufficient evidence"}


@dataclass(frozen=True)
class Email:
    to: list[str]
    subject: str
    body: str
    reply_to: str = ""
    attachments: tuple[tuple[str, str, bytes], ...] = ()  # (file name, MIME type, content), e.g. a calendar file


class MailError(RuntimeError):
    pass


def invitation(interview: Interview, settings: Settings, join_link: str, when_text: str, booking_link: str = "",
               expires_text: str = "", company: str = "") -> Email:
    """A scheduled interview's invitation. join_link is the candidate's interview link (app/gateway.py), never the
    meeting's own: it shows the meeting after a one-time code to this address."""
    s = interview.setup
    first = s.candidate_name.split()[0] if s.candidate_name.split() else "there"
    when = f"on {when_text}" if when_text else "at the agreed time"
    contact = settings.recruiter_contact or "by replying to this email"
    where = (
        f"Please choose a time for your interview, a video call, here:\n\n{booking_link}\n\nOnce you've chosen, we'll email you your interview link."
        + (f" Please choose by {expires_text}; the link stops working after that." if expires_text else "")
        if booking_link else (f"Your interview is {when}, a video call. Join it from your interview link:\n\n{join_link}\n\n"
                              "To make sure it's you, the page emails you a one-time code first, and it opens shortly before the time.")
    )
    if interview.options.mode == "analyst":
        notes = f"""- A member of the hiring team interviews you. It takes about {s.duration_minutes} minutes.
- A note-taking assistant joins the call as "{settings.bot_name}". It doesn't speak; it transcribes the call so the team can review it, and an AI summarises the transcript. A person makes the decision.
- If you'd rather the call wasn't transcribed, or need an accommodation, contact us {contact}. Choosing this does not count against you.

Tips: join from a quiet place, ideally with headphones."""
    else:
        notes = f"""{format_note(interview)}- The interview is conducted by an AI interviewer. It asks {questions_asked(interview)} questions about your experience and may ask short follow-ups. It takes about {s.duration_minutes} minutes.
- The call is recorded and transcribed. A person on the hiring team reviews the interview; the AI does not make the decision.
- At the start, the AI interviewer will ask whether you agree to continue. You can say no, or ask to stop at any time.
- If you would prefer to be interviewed by a person, or need an accommodation, contact us {contact}. Choosing this does not count against you.

Tips: join from a quiet place and use headphones, so the AI's voice doesn't echo into your microphone. The AI interviewer joins as "{settings.bot_name}"."""
    body = f"""Hello {first},

Thank you for your interest in the {s.role_title} role. {where}

Please note before you join:
{notes}

Best regards,
{f"The {company} hiring team" if company else "The hiring team"}
"""
    return Email(to=[s.candidate_email] if s.candidate_email else [],
                 subject=f"Your interview for the {s.role_title} role" + (f" at {company}" if company else ""),
                 body=body, reply_to=settings.mail_reply_to)


def questions_asked(interview: Interview) -> int:
    """How many planned questions the interview asks: its plan's (edited or not), else the number set up."""
    return len(interview.plan.questions) if interview.plan and interview.plan.questions else interview.setup.question_count


def format_note(interview: Interview) -> str:
    """What a video or proctored interview asks of the candidate, as a first bullet (empty for voice)."""
    o = interview.options
    if o.format == "video":
        return "- Please join with your camera on: the video is recorded with the call.\n"
    if o.format == "proctored":
        needs = [n for n, on in (("keep your camera on", o.require_camera), ("share your entire screen when asked", o.require_screen_share)) if on]
        if needs:
            return (f"- This is a proctored interview. Please join from a computer and be ready to {' and '.join(needs)}. "
                    "The times your screen is shared are logged for the hiring team; what's on it isn't analysed.\n")
    return ""


def cancellation(interview: Interview, settings: Settings) -> Email:
    """Tells the candidate the interview won't go ahead. No reason is given; the team follows up themselves."""
    s = interview.setup
    first = s.candidate_name.split()[0] if s.candidate_name.split() else "there"
    contact = settings.recruiter_contact or "by replying to this email"
    body = f"""Hello {first},

Your interview for the {s.role_title} role has been cancelled, and any link we sent you for it no longer works. We're sorry for any inconvenience.

If you have questions, contact us {contact}.

Best regards,
The hiring team
"""
    return Email(to=[s.candidate_email] if s.candidate_email else [], subject=f"Your interview for the {s.role_title} role is cancelled",
                 body=body, reply_to=settings.mail_reply_to)


def report_ready(report: EvaluationReport, settings: Settings) -> Email:
    ev = report.evaluation
    outcome = (f"AI recommendation: {REC_WORDS.get(ev.recommendation, ev.recommendation)} (confidence {ev.confidence})"
               if ev else f"Not evaluated: {report.evaluation_error or 'see the report'}")
    link = f"{settings.dashboard_url.rstrip('/')}/#/s/{report.session_id}" if settings.dashboard_url else f"session {report.session_id} in the dashboard"
    scores = [c.score for c in ev.competency_scores if c.score is not None] if ev else []
    missing = ev.unassessed if ev else []
    score = f"Score: {round(round(sum(scores) / len(scores), 2) * 20)}/100{' from the assessed competencies only' if missing else ''}\n" if scores else ""
    if missing:
        score += f"Not assessed: {', '.join(missing)}. There's no pass or fail until every competency is assessed.\n"
    body = f"""The interview with {report.candidate_name} for {report.role_title} has finished.

{outcome}
{score}Questions asked: {report.questions_asked} of {report.questions_planned}. Ended: {report.end_reason.replace('_', ' ')}.

Open the report: {link}

The AI's output is advisory. Please review the report and the transcript before deciding.
"""
    return Email(to=list(settings.notify_emails), subject=f"Interview report ready: {report.candidate_name}, {report.role_title}", body=body)


def _is_gmail(host: str) -> bool:
    return (host or "").strip().lower() in ("smtp.gmail.com", "smtp.googlemail.com")


def smtp_password(settings: Settings) -> str:
    """The password as the server expects it. Gmail App Passwords are shown in groups ("abcd efgh ijkl mnop") and are
    often pasted that way; they never contain spaces, so for Gmail every space is removed. Elsewhere only the ends."""
    password = (settings.smtp_password or "").strip()
    return "".join(password.split()) if _is_gmail(settings.smtp_host) else password


def _login(server: smtplib.SMTP, username: str, password: str) -> None:
    """One login attempt with one mechanism. smtplib's login() tries each mechanism in turn, and Gmail closes the
    connection after the first refusal, so its 535 "Username and Password not accepted" used to surface as
    "Connection unexpectedly closed". This keeps the server's real answer."""
    server.ehlo_or_helo_if_needed()
    if not server.has_extn("auth"):
        raise smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server.")
    offered = server.esmtp_features["auth"].upper().split()
    mechanism = next((m for m in ("PLAIN", "LOGIN") if m in offered), None)
    if mechanism is None:
        raise smtplib.SMTPException(f"The server offers no login method this app supports ({' '.join(offered) or 'none'})")
    server.user, server.password = username, password
    server.auth(mechanism, getattr(server, f"auth_{mechanism.lower()}"), initial_response_ok=True)


def explain(settings: Settings, error: BaseException) -> str:
    """A send failure in words a person can act on. Never contains the password."""
    host, port = settings.smtp_host, settings.smtp_port
    gmail = _is_gmail(host)
    if isinstance(error, smtplib.SMTPAuthenticationError):
        answer = error.smtp_error.decode(errors="replace") if isinstance(error.smtp_error, bytes) else str(error.smtp_error)
        answer = " ".join(answer.split())[:160]
        hint = (" For Gmail: the username is the full Gmail address, and the password must be a 16-character App Password "
                "(Google Account → Security → 2-Step Verification → App passwords), not the account's normal password. "
                "A revoked App Password stops working too." if gmail else " Check the SMTP username and password.")
        return f"{host} didn't accept the username and password ({error.smtp_code} {answer}).{hint}"
    if isinstance(error, ssl.SSLError) and "WRONG_VERSION_NUMBER" in str(error):
        return (f"Port {port} doesn't start with SSL/TLS: turn off \"Use SSL/TLS from the start\" and use STARTTLS "
                f"(port 587{' for Gmail' if gmail else ''}).")
    if isinstance(error, smtplib.SMTPServerDisconnected):
        if port == 465 and not settings.smtp_ssl:
            return f"{host} closed the connection: port 465 expects SSL/TLS from the start. Turn it on, or use port 587 with STARTTLS."
        return (f"{host}:{port} closed the connection. Check the port and encryption: 587 with STARTTLS, or 465 with "
                "\"SSL/TLS from the start\" (not both).")
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return f"{host} only allows a login over an encrypted connection: turn on STARTTLS (port 587)."
    if isinstance(error, (TimeoutError, ConnectionError, OSError)) and not isinstance(error, smtplib.SMTPException):
        return (f"Couldn't connect to {host}:{port} ({type(error).__name__}: {error}). Check the server name and port; "
                "a firewall or network that blocks outgoing mail ports gives this too.")
    return f"Couldn't send the email: {type(error).__name__}: {error}"


def _smtp_send(settings: Settings, email: Email) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, ", ".join(email.to), email.subject
    if email.reply_to:
        msg["Reply-To"] = email.reply_to
    msg.set_content(email.body)
    for name, mime, data in email.attachments:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    if settings.smtp_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    with server:
        if settings.smtp_starttls and not settings.smtp_ssl:
            server.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            _login(server, settings.smtp_username.strip(), smtp_password(settings))
        server.send_message(msg)


class Mailer:
    def __init__(self, settings: Settings, send: Optional[Callable[[Settings, Email], None]] = None):
        self.settings = settings
        self._send = send or _smtp_send
        self._tasks: set[asyncio.Task] = set()
        self.retry_delays: tuple[float, ...] = RETRY_DELAYS

    @property
    def enabled(self) -> bool:
        return bool(self.settings.smtp_host and self.settings.smtp_from)

    async def send(self, email: Email) -> None:
        await self.send_with(self.settings, email)

    async def send_with(self, settings: Settings, email: Email) -> None:
        """send() with the given settings: the saved ones, or a form's values being tested before they're saved."""
        if not (settings.smtp_host and settings.smtp_from):
            raise MailError("Email isn't set up: set SMTP_HOST and SMTP_FROM")
        if not email.to:
            raise MailError("No recipient")
        try:
            await asyncio.to_thread(self._send, settings, email)
        except (OSError, smtplib.SMTPException) as e:
            raise MailError(explain(settings, e)) from e

    async def send_retrying(self, email: Email) -> int:
        """send(), tried again after each of retry_delays seconds while it fails. Returns the attempts used; raises the last MailError."""
        for attempt, delay in enumerate((0, *self.retry_delays), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self.send(email)
                return attempt
            except MailError as e:
                if not self.enabled or attempt > len(self.retry_delays):
                    raise
                logger.warning("email attempt %d failed, retrying: %s", attempt, e)
        raise AssertionError("unreachable")

    def in_background(self, coro) -> None:
        # A fresh context, so audit entries from the send don't carry the name of whoever triggered it.
        task = asyncio.get_running_loop().create_task(coro, context=contextvars.Context())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self, timeout: float = 10.0) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)


class NotifyingSink:
    """ReportSink that saves through `inner`, then emails the hiring team (and, where the workspace wants it, a short
    note to the candidate) in the background."""

    def __init__(self, inner, mailer: Mailer, store, recipients: Optional[Callable[[EvaluationReport], list[str]]] = None,
                 candidate_note: Optional[Callable[[EvaluationReport], Optional[Email]]] = None):
        """`recipients(report)`: who else hears about it (the interview's workspace's notification list).
        `candidate_note(report)`: the candidate's "we received your interview" email, or None."""
        self._inner, self._mailer, self._store, self._recipients = inner, mailer, store, recipients
        self._candidate_note = candidate_note

    def push_report(self, report: EvaluationReport) -> str:
        saved = self._inner.push_report(report)
        self._mailer.in_background(self._notify(report))
        return saved

    async def _notify(self, report: EvaluationReport) -> None:
        if not self._mailer.enabled:
            return
        email = report_ready(report, self._mailer.settings)
        extra = self._recipients(report) if self._recipients else []
        email = Email(to=list(dict.fromkeys([*email.to, *extra])), subject=email.subject, body=email.body)
        if email.to:
            try:
                await self._mailer.send_retrying(email)
                self._store.audit("report_notification_sent", report.interview_id, report.session_id)
            except MailError as e:
                logger.warning("report notification for %s failed: %s", report.session_id, e)
                self._store.audit("report_notification_failed", report.interview_id, report.session_id, str(e)[:200])
        note = self._candidate_note(report) if self._candidate_note else None
        if note is not None and note.to:
            try:
                await self._mailer.send_retrying(note)
                self._store.audit("candidate_notice_sent", report.interview_id, report.session_id)
            except MailError as e:
                logger.warning("candidate notice for %s failed: %s", report.session_id, e)


def interview_received(interview: Interview, settings: Settings) -> Email:
    """For the candidate, when the workspace sends it: their interview arrived. Nothing about how it went."""
    s = interview.setup
    first = s.candidate_name.split()[0] if s.candidate_name.split() else "there"
    contact = settings.recruiter_contact or "by replying to this email"
    return Email(to=[s.candidate_email], subject=f"We received your interview for the {s.role_title} role",
                 reply_to=settings.mail_reply_to,
                 body=f"Hello {first},\n\nThank you for taking the interview for the {s.role_title} role. We've received it, "
                      f"and a person on the hiring team will review it and let you know about the next step.\n\n"
                      f"If you have questions, contact us {contact}.\n\nBest regards,\nThe hiring team\n")
