"""Where an interview stands, as one stage the recruiter can read at a glance.

The stored status (draft, ready, scheduled, in_progress, completed, failed) drives what the app
may do next and stays as it is. The stage adds what the status alone doesn't say: whether the
candidate was invited, is still to pick a time, let the link expire, whether the report is ready,
and whether the recruiter cancelled. It is worked out on every read, so it can't go stale.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from app import booking
from app.booking import utc
from app.models import Interview, utcnow

Stage = Literal[
    "draft", "ready", "invitation_sent", "pending_candidate", "scheduled", "in_progress",
    "evaluation_pending", "report_ready", "expired", "cancelled", "failed",
]

LABELS: dict[str, str] = {
    "draft": "Draft",
    "ready": "Ready",
    "invitation_sent": "Invitation sent",
    "pending_candidate": "Pending candidate",
    "scheduled": "Scheduled",
    "in_progress": "In progress",
    "evaluation_pending": "Evaluation pending",
    "report_ready": "Report ready",
    "expired": "Expired",
    "cancelled": "Cancelled",
    "failed": "Failed",
}


def stage(interview: Interview, evaluated: bool, now: Optional[datetime] = None) -> Stage:
    """evaluated: the latest session's report holds an evaluation (a report whose evaluation failed doesn't count)."""
    now = now or utcnow()
    if interview.cancelled_at:
        return "cancelled"
    if interview.status == "in_progress":
        return "in_progress"
    if interview.status == "completed":
        return "report_ready" if evaluated else "evaluation_pending"
    if interview.status == "failed":
        return "failed"
    if interview.status == "scheduled":
        return "scheduled"
    if interview.booking and not interview.booking.booked_at:
        return "expired" if booking.expired(interview, now) else "pending_candidate"
    inv = interview.invitation
    if inv and not inv.started_at:
        return "expired" if now >= utc(inv.expires_at) else "invitation_sent"
    if interview.invited_at:
        return "invitation_sent"
    return "draft" if interview.status == "draft" else "ready"


def can_cancel(interview: Interview) -> Optional[str]:
    """Why this interview can't be cancelled, or None if it can."""
    if interview.cancelled_at:
        return "The interview is already cancelled"
    if interview.status == "in_progress":
        return "The interview is under way; end it from the live page instead"
    if interview.status == "completed":
        return "The interview has already taken place"
    return None


# The Interviews page groups the stages into the states a recruiter manages (pending, scheduled, upcoming, in
# progress, completed, cancelled), plus the ones that need attention (expired, failed). Upcoming: scheduled to start
# within UPCOMING_HOURS.
State = Literal["pending", "scheduled", "upcoming", "in_progress", "completed", "cancelled", "expired", "failed"]
STATE_LABELS: dict[str, str] = {"pending": "Pending", "scheduled": "Scheduled", "upcoming": "Upcoming", "in_progress": "In progress",
                                "completed": "Completed", "cancelled": "Cancelled", "expired": "Expired", "failed": "Failed"}
UPCOMING_HOURS = 24
_STATE_OF_STAGE = {"draft": "pending", "ready": "pending", "invitation_sent": "pending", "pending_candidate": "pending",
                   "in_progress": "in_progress", "evaluation_pending": "completed", "report_ready": "completed",
                   "cancelled": "cancelled", "expired": "expired", "failed": "failed"}


def state(interview: Interview, where: str, now: Optional[datetime] = None) -> State:
    """The Interviews page's state for an interview at stage `where`."""
    if where == "scheduled":
        now = now or utcnow()
        start = utc(interview.scheduled_at) if interview.scheduled_at else None
        return "upcoming" if start and (start - now).total_seconds() <= UPCOMING_HOURS * 3600 else "scheduled"
    return _STATE_OF_STAGE[where]
