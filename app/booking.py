"""Candidate self-scheduling: the candidate picks their interview time from the recruiter's offered slots.

The recruiter offers a meeting link and a list of times; the candidate opens a private link
(/book/<token>, reachable on the public address like the webhooks), reads the AI-interview
notice, picks a time and confirms they've read the notice. That books the interview exactly as
a recruiter's "start at" would: status scheduled, and the app's scheduler sends the bot then.

The meeting link is one the recruiter reuses for every offered time (e.g. a recurring Zoom meeting's /j/ link
or a Meet link they keep). Nothing creates meetings or calendar events in anyone's calendar.
What the candidate sees: their first name, the role, length, times and the notice. Never the
resume, job description, email address or anything about other candidates.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional

from app.config import Settings
from app.mailer import Email, invitation, questions_asked
from app.models import Booking, Interview, utcnow
from app.compare import same

MAX_SLOTS = 30
MIN_NOTICE = timedelta(minutes=10)  # a time must be at least this far ahead to be offered or booked
CHANGE_CUTOFF = timedelta(minutes=30)  # the candidate can change or cancel until this long before the start
CLOSED_STATUSES = ("in_progress", "completed")


class BookingError(ValueError):
    """A request the candidate or recruiter can fix; the message says how."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def new_token() -> str:
    return secrets.token_urlsafe(24)


def booking_url(settings: Settings, token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/book/{token}"


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def clean_slots(slots: list[datetime], now: Optional[datetime] = None) -> list[datetime]:
    """Offered times, in UTC, sorted, without duplicates. Raises BookingError if none is usable."""
    now = now or utcnow()
    out = sorted({utc(s).replace(microsecond=0) for s in slots})
    if not out:
        raise BookingError("Offer at least one time", 422)
    if len(out) > MAX_SLOTS:
        raise BookingError(f"Offer at most {MAX_SLOTS} times", 422)
    if out[0] < now + MIN_NOTICE:
        raise BookingError("Every offered time must be at least 10 minutes from now", 422)
    return out


def clean_expiry(expires_at: Optional[datetime], slots: list[datetime], now: Optional[datetime] = None) -> datetime:
    """When the link stops accepting bookings: the recruiter's deadline, or the last offered time.

    A deadline after the last offered time would change nothing, so it is brought back to it.
    """
    now = now or utcnow()
    last = max(utc(s) for s in slots)
    if expires_at is None:
        return last
    expires_at = utc(expires_at).replace(microsecond=0)
    if expires_at < now + MIN_NOTICE:
        raise BookingError("The link must stay open for at least 10 minutes", 422)
    return min(expires_at, last)


def deadline(b: Booking) -> datetime:
    return utc(b.expires_at) if b.expires_at else max(utc(s) for s in b.slots)


def expired(interview: Interview, now: Optional[datetime] = None) -> bool:
    """An open link (nothing booked yet) whose time to book has run out."""
    b = interview.booking
    return bool(b and not b.booked_at and (now or utcnow()) >= deadline(b))


def is_live(interview: Interview, token: str) -> bool:
    """The interview still has this booking and it hasn't run yet."""
    return bool(interview.booking and same(token, interview.booking.token))


def booked(interview: Interview) -> bool:
    return bool(interview.booking and interview.booking.booked_at and interview.status == "scheduled" and interview.scheduled_at)


def can_change(interview: Interview, now: Optional[datetime] = None) -> bool:
    return booked(interview) and interview.scheduled_at - (now or utcnow()) > CHANGE_CUTOFF


ROOM_BUFFER = timedelta(minutes=10)  # between two interviews in one room: a late bot, an answer that runs over


def clashes(start: datetime, minutes: int, busy: list[tuple[datetime, datetime]]) -> bool:
    """Whether an interview of `minutes` from `start` overlaps any of the busy spans (each already with its buffer)."""
    start = utc(start)
    end = start + timedelta(minutes=minutes) + ROOM_BUFFER
    return any(s < end and start < e for s, e in busy)


def public_view(interview: Interview, settings: Settings, now: Optional[datetime] = None,
                busy: Optional[list[tuple[datetime, datetime]]] = None) -> dict:
    """What the booking page shows. Only what the candidate needs; no resume, JD or email. `busy`: times other
    interviews are booked in the same meeting room; those slots aren't offered (the candidate's own booking stays)."""
    now = now or utcnow()
    s = interview.setup
    b: Booking = interview.booking
    state = ("closed" if interview.status in CLOSED_STATUSES or interview.cancelled_at else "booked" if booked(interview)
             else "expired" if expired(interview, now) else "open")
    mine = utc(interview.scheduled_at) if state == "booked" and interview.scheduled_at else None
    offered = [t for t in b.slots if utc(t) >= now + MIN_NOTICE
               and (utc(t) == mine or not clashes(t, s.duration_minutes, busy or []))] if state in ("open", "booked") else []
    return {
        "state": state,
        "first_name": s.candidate_name.split()[0] if s.candidate_name.split() else "",
        "role_title": s.role_title,
        "duration_minutes": s.duration_minutes,
        "question_count": questions_asked(interview),
        "platform": b.platform,
        "bot_name": settings.bot_name,
        "contact": settings.recruiter_contact or "",
        "slots": [utc(t).isoformat() for t in offered],
        "booked_at": utc(interview.scheduled_at).isoformat() if state == "booked" else None,
        # Never the meeting's own link: once booked, the candidate joins through their interview link (app/gateway.py),
        # which emails them a code first. The server adds it (interview_link).
        "meeting_link": None,
        "can_change": state == "booked" and can_change(interview, now),
        "expires_at": deadline(b).isoformat() if state == "open" else None,
        "mode": interview.options.mode,  # "analyst": a person interviews; the bot only transcribes
        "format": interview.options.format,
        "require_camera": interview.options.require_camera,
        "require_screen_share": interview.options.require_screen_share,
    }


def book(interview: Interview, token: str, slot: datetime, acknowledged: bool, now: Optional[datetime] = None,
         busy: Optional[list[tuple[datetime, datetime]]] = None) -> Interview:
    """The interview with the candidate's chosen time booked. Raises BookingError. `busy`: as in public_view."""
    now = now or utcnow()
    if not acknowledged:
        raise BookingError("Confirm that you have read how the AI interview works", 422)
    if not is_live(interview, token):
        raise BookingError("This booking link isn't valid any more", 404)
    if interview.status in CLOSED_STATUSES:
        raise BookingError("This interview has already taken place or is under way")
    if interview.cancelled_at:
        raise BookingError("This interview was cancelled; please contact the hiring team")
    if expired(interview, now):
        raise BookingError("This booking link has expired; please contact the hiring team", 410)
    if interview.plan_approved_at is None:
        raise BookingError("This interview isn't ready to book yet; please contact the hiring team")
    if booked(interview) and not can_change(interview, now):
        raise BookingError("It's too close to your interview to change the time; please contact the hiring team")
    if interview.status == "scheduled" and not booked(interview):
        raise BookingError("The hiring team has already set a time for this interview")
    slot = utc(slot).replace(microsecond=0)
    if slot not in {utc(t) for t in interview.booking.slots}:
        raise BookingError("That time isn't one of the times offered", 422)
    if slot < now + MIN_NOTICE:
        raise BookingError("That time is too soon; please choose a later one", 422)
    if clashes(slot, interview.setup.duration_minutes, busy or []):
        raise BookingError("That time was just taken by another interview. Please choose another one.", 409)
    return interview.model_copy(update={
        "meeting_link": interview.booking.meeting_link,
        "platform": interview.booking.platform,
        "scheduled_at": slot,
        "status": "scheduled",
        # The candidate confirmed the written notice themselves; the AI still asks for consent at the start.
        "consent_attested_at": now,
        "booking": interview.booking.model_copy(update={"booked_at": now}),
    })


def cancel(interview: Interview, token: str, now: Optional[datetime] = None) -> Interview:
    if not is_live(interview, token):
        raise BookingError("This booking link isn't valid any more", 404)
    if not booked(interview):
        raise BookingError("There is no booked time to cancel")
    if not can_change(interview, now):
        raise BookingError("It's too close to your interview to cancel here; please contact the hiring team")
    return interview.model_copy(update={
        "status": "ready", "scheduled_at": None, "booking": interview.booking.model_copy(update={"booked_at": None}),
    })


def when_text(dt: datetime, zone: str = "UTC") -> str:
    """A time for an email, in the configured time zone (Settings > General), with its abbreviation."""
    try:
        local = utc(dt).astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        local = utc(dt)
    return local.strftime("%a %d %b %Y, %H:%M ") + (local.tzname() or "UTC")


def confirmation(interview: Interview, settings: Settings, join_url: str, company: str = "") -> Email:
    """The candidate's confirmation: the invitation's notice, with the booked time and their interview link."""
    email = invitation(interview, settings, join_url, when_text(interview.scheduled_at, settings.time_zone), company=company)
    return Email(to=email.to, subject=f"Confirmed: your interview for the {interview.setup.role_title} role" + (f" at {company}" if company else ""),
                 body=email.body, reply_to=email.reply_to,
                 attachments=(("interview.ics", "text/calendar", ics(interview, settings, join_url).encode("utf-8")),))


def team_note(interview: Interview, settings: Settings, cancelled: bool = False) -> Email:
    s = interview.setup
    what = "cancelled their booked time" if cancelled else f"booked {when_text(interview.scheduled_at, settings.time_zone)}"
    return Email(to=list(settings.notify_emails), subject=f"{s.candidate_name} {what} ({s.role_title})",
                 body=f"{s.candidate_name} {what} for the {s.role_title} interview through the self-scheduling link.\n")


def ics(interview: Interview, settings: Settings, join_url: str = "") -> str:
    """A calendar file for the booked time, with the candidate's interview link (never the meeting's own)."""
    start = utc(interview.scheduled_at)
    end = start + timedelta(minutes=interview.setup.duration_minutes)
    fmt = "%Y%m%dT%H%M%SZ"

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Interview scheduling//EN", "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{interview.id}-{start.strftime(fmt)}@interview",
        f"DTSTAMP:{utcnow().strftime(fmt)}", f"DTSTART:{start.strftime(fmt)}", f"DTEND:{end.strftime(fmt)}",
        f"SUMMARY:{esc(f'Interview: {interview.setup.role_title}')}",
        f"DESCRIPTION:{esc(f'AI interview, about {interview.setup.duration_minutes} minutes. Join from your interview link: {join_url}')}",
        f"LOCATION:{esc(join_url)}", f"URL:{join_url}",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"
