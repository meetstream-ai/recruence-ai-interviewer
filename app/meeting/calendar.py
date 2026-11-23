"""Upcoming meetings from the calendar connected in MeetStream, to pick an interview's link and time.

Only reads events (`GET /calendar/events`). MeetStream's own calendar auto-join is not used:
each interview needs its agent config and webhook token created right before the bot joins,
so the bot is still started by this app's scheduler at the chosen time.

Observed 2026-10-31 (Google Calendar): one page holds 50 events in start order, from about a day
ago to four weeks ahead, each with `start_time`, `end_time`, `meeting_url`, `is_deleted` and the
provider's event under `raw` (title in `raw.summary`). Only the first page is used.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.meeting.platforms import UnsupportedMeetingLink, detect_platform

# A meeting that started a few minutes ago can still be joined.
STARTED_GRACE = timedelta(minutes=10)
# MeetStream's copy of the calendar not changed for this long: say so (R-10). On 13 November its copy had stopped at
# 26 October; a "resync" still answered 200 with that copy, and new meetings never arrived.
STALE_AFTER = timedelta(days=2)
MAX_TITLE = 200


def _when(value: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def upcoming_meetings(data: Any, now: datetime) -> list[dict]:
    """Events that haven't ended long ago and have a Meet, Zoom or Teams link this app accepts, soonest first."""
    out = []
    results = data.get("results") if isinstance(data, dict) else None
    for event in results if isinstance(results, list) else []:
        if not isinstance(event, dict) or event.get("is_deleted"):
            continue
        start = _when(event.get("start_time"))
        link = str(event.get("meeting_url") or "").strip()
        if start is None or start < now - STARTED_GRACE or not link:
            continue
        try:
            platform = detect_platform(link)
        except UnsupportedMeetingLink:
            continue
        end = _when(event.get("end_time"))
        raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
        out.append({
            "id": str(event.get("id") or ""),
            "title": " ".join(str(raw.get("summary") or "").split())[:MAX_TITLE] or "(no title)",
            "start": start.isoformat(),
            "end": end.isoformat() if end else None,
            "meeting_link": link,
            "platform": platform.name,
        })
    return sorted(out, key=lambda e: _when(e["start"]))


PROVIDERS = {"google": "Google", "outlook": "Outlook", "microsoft": "Outlook"}
MAX_PROBLEM = 300


def sync_problems(data: Any) -> list[dict]:
    """The connected calendars MeetStream couldn't read this time, from `partial_errors` (R-14).

    Seen 14 November: `{"google::<account>": "Failed to fetch calendar events: Failed to refresh token: 400 ..."}`
    while every resync still answered 200 with a weeks-old copy. Each problem names the provider and account (shown to
    the installation's admins only, never logged) and says what to do. A sign-in that can't be refreshed (an expired or
    revoked Google grant, for example a Google OAuth app left in Testing mode, whose grants end after 7 days) needs
    the calendar to be reconnected in MeetStream."""
    errors = data.get("partial_errors") if isinstance(data, dict) else None
    if not isinstance(errors, dict):
        return []
    out = []
    for key, message in errors.items():
        provider_key, _, account = str(key).partition("::")
        provider = PROVIDERS.get(provider_key.strip().lower(), provider_key.strip().title() or "Calendar")
        text = " ".join(str(message or "").split())[:MAX_PROBLEM]
        token = any(w in text.lower() for w in ("refresh token", "invalid_grant", "token has been expired", "revoked", "unauthorized"))
        out.append({
            "provider": provider, "account": account.strip()[:200], "message": text, "reconnect": token,
            "advice": (f"MeetStream can no longer use its sign-in to this {provider} calendar. Reconnect it in MeetStream "
                       "(Calendar → Disconnect, then Connect), then resync." if token
                       else f"MeetStream couldn't read this {provider} calendar. Check it in MeetStream's Calendar page, then resync."),
        })
    return out


def staleness(data: Any, now: datetime) -> Optional[dict]:
    """When MeetStream last stored a change to any event of the connected calendar, if that's over STALE_AFTER ago.

    MeetStream answers a resync from its own copy; when its connection to the calendar provider has stopped working,
    that copy just stops changing. A calendar nobody touched for days looks the same, so the dashboard's wording is
    "hasn't changed since", with what to do if meetings were added since."""
    results = data.get("results") if isinstance(data, dict) else None
    newest = None
    for event in results if isinstance(results, list) else []:
        if isinstance(event, dict):
            for field in ("updated_at", "created_at"):
                when = _when(event.get(field)) if event.get(field) else None
                if when and (newest is None or when > newest):
                    newest = when
    if newest is None or now - newest <= STALE_AFTER:
        return None
    return {"since": newest.isoformat(), "days": (now - newest).days}
