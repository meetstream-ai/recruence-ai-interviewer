"""The interview access gateway: a candidate's link (/i/<token>) opens their interview only after a one-time code
sent to the email address they applied with, on one device at a time, inside the interview's time window.

It is not a portal: one page per invitation, no account, no history. What it enforces:
- the link: 32 random bytes (secrets.token_urlsafe), stored as a SHA-256 hash for lookup and encrypted for the
  hiring team to copy (never in clear); a new link replaces the old one, which stops working with its sessions;
- the code: 6 digits, emailed to the application's address, valid CODE_MINUTES, MAX_TRIES wrong tries, at most
  MAX_SENDS sends in SEND_WINDOW; only its hash is kept;
- the session: the device that entered the right code gets a random cookie (only its hash is kept); while it is
  active no other device can verify or start; the same device can always reconnect;
- the window: explicit, else from WINDOW_BEFORE before a booked time to its end plus WINDOW_AFTER, else until the
  link expires.

Anything unusual (a second device, many wrong codes) is flagged on the interview for the hiring team. Nothing is
decided on it: IP addresses and devices are never treated as identity.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from app.models import Interview, InterviewFlag, utcnow

SESSION_COOKIE = "interview_session"
CODE_MINUTES = 10
MAX_TRIES = 5
MAX_SENDS = 3
SEND_WINDOW = timedelta(minutes=15)
WINDOW_BEFORE = timedelta(minutes=15)
WINDOW_AFTER = timedelta(minutes=30)
FLAG_AFTER_FAILURES = 3


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _code_hash(link_hash: str, code: str) -> str:
    return hashlib.sha256(f"{link_hash}:{code}".encode()).hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def new_session() -> str:
    return secrets.token_urlsafe(32)


def masked(email: str) -> str:
    """a••••@example.com: enough for the candidate to recognise the address, no more."""
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}{'•' * max(1, min(len(local) - 1, 6))}@{domain}" if local and domain else ""


def link_token(interview: Interview, box) -> str:
    """The link's token, for the hiring team (copy link, emails). "" if it can't be read."""
    inv = interview.invitation
    if inv is None:
        return ""
    if inv.token_enc:
        try:
            return box.decrypt(inv.token_enc)
        except Exception:  # noqa: BLE001 - another key: the team sends a new link
            return ""
    return inv.token


def seal(interview: Interview, token: str, box) -> Interview:
    """The interview with its link encrypted (and the clear one dropped)."""
    inv = interview.invitation.model_copy(update={"token": "", "token_enc": box.encrypt(token)})
    return interview.model_copy(update={"invitation": inv})


def window(interview: Interview, duration_minutes: Optional[int] = None) -> tuple[Optional[datetime], datetime]:
    """When the interview can be started: (opens, closes)."""
    from app.recruiting import utc
    inv = interview.invitation
    if inv.window_start or inv.window_end:
        return (utc(inv.window_start) if inv.window_start else None, utc(inv.window_end) if inv.window_end else utc(inv.expires_at))
    if interview.scheduled_at:
        at = utc(interview.scheduled_at)
        minutes = duration_minutes or interview.setup.duration_minutes
        return at - WINDOW_BEFORE, min(utc(inv.expires_at), at + timedelta(minutes=minutes) + WINDOW_AFTER)
    return None, utc(inv.expires_at)


def flag(interview: Interview, kind: str, detail: str, now: Optional[datetime] = None) -> Interview:
    """Add a flag for the hiring team, once per kind while it's unresolved."""
    if any(f.kind == kind and f.resolved_at is None for f in interview.flags):
        return interview
    return interview.model_copy(update={"flags": [*interview.flags, InterviewFlag(at=now or utcnow(), kind=kind, detail=detail[:300])]})


class GatewayError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def send_code(store, token: str, now: Optional[datetime] = None) -> str:
    """A new code for this link (the earlier one stops working). Returns it, for the email. Raises GatewayError."""
    now = now or utcnow()
    link = token_hash(token)
    current = store.gateway_code(link)
    sends = 1
    first = now
    if current:
        started = datetime.fromisoformat(current["first_sent_at"])
        if now - started < SEND_WINDOW:
            if current["sends"] >= MAX_SENDS:
                raise GatewayError(429, "Too many codes requested. Please wait a few minutes and try again.")
            sends, first = current["sends"] + 1, started
    code = new_code()
    store.put_gateway_code(link, _code_hash(link, code), (now + timedelta(minutes=CODE_MINUTES)).isoformat(), sends, first.isoformat())
    return code


def check_code(store, token: str, code: str, now: Optional[datetime] = None) -> bool:
    """Whether the code is right (and not used up or expired). A right code can't be used twice."""
    now = now or utcnow()
    link = token_hash(token)
    current = store.gateway_code(link)
    if not current or now >= datetime.fromisoformat(current["expires_at"]):
        return False
    # The try is taken before comparing, atomically: guesses sent at once can't use more than MAX_TRIES between them.
    if not store.take_gateway_attempt(link, MAX_TRIES):
        return False
    if not secrets.compare_digest(current["code_hash"], _code_hash(link, "".join(c for c in str(code) if c.isdigit())[:6])):
        return False
    store.drop_gateway_code(link)
    return True


def tries_left(store, token: str) -> int:
    current = store.gateway_code(token_hash(token))
    return max(0, MAX_TRIES - current["attempts"]) if current else 0


def too_many_failures(store, token: str) -> bool:
    """Enough wrong tries against a sent code to tell the hiring team. Typing something before any code was sent isn't one."""
    current = store.gateway_code(token_hash(token))
    return bool(current) and current["attempts"] >= FLAG_AFTER_FAILURES


def session_is_current(store, token: str, cookie: str) -> bool:
    """This browser is the link's verified device."""
    if not cookie:
        return False
    mine = token_hash(cookie)
    if any(s["session_hash"] == mine for s in store.active_gateway_sessions(token_hash(token))):
        store.touch_gateway_session(mine)
        return True
    return False


def other_device_active(store, token: str, cookie: str) -> bool:
    mine = token_hash(cookie) if cookie else ""
    return any(s["session_hash"] != mine for s in store.active_gateway_sessions(token_hash(token)))


def bind(store, token: str, interview_id: str, cookie: str) -> bool:
    """Make this browser the link's device (atomic). False if another one already is."""
    return store.open_gateway_session(token_hash(cookie), token_hash(token), interview_id)


def secure_old_links(store, box) -> int:
    """Links from before hashed storage: each moves to a hash (lookup) and an encrypted copy (the team's copy link);
    the clear one is removed from storage. The candidate's link itself doesn't change. Returns how many moved."""
    moved = 0
    for row in store.plain_invite_links():
        interview = store.get_interview(row["interview_id"])
        if interview is None or interview.invitation is None:
            continue
        token = row["token"]
        if interview.invitation.token and interview.invitation.token != token:
            continue  # not this interview's current link: left as it is
        store.save_interview(seal(interview, token, box))
        store.set_invite_token(interview.id, token)  # writes the hash, removes the clear row
        moved += 1
    return moved
