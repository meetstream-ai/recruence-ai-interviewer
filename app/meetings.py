"""Meetings: where an interview takes place, as one record however it was made .

Two ways to get a meeting, one record:
- manual (built): the recruiter creates the meeting in Google Meet, Zoom or Teams and pastes its link, for one
  candidate's interview (their own room) or for the role (a shared room, one interview at a time);
- provider (designed, not built): Recruence creates, updates and cancels the meeting through the recruiter's connected
  Google, Microsoft or Zoom account. Adding one means a MeetingProvider below; the interview pipeline doesn't change.

Creating a meeting and capturing it are separate: capture is MeetStream's bot joining the meeting, and how well that
works depends on the platform (CAPTURE). Candidates never get a meeting's link by email: they get their interview
link (app/gateway.py), which shows the meeting only to the verified device.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol

from app.meeting.platforms import UnsupportedMeetingLink, detect_platform

# What MeetStream's capture is known to do on each platform (README, from live calls). Shown next to a meeting so
# nobody expects an automatic evaluation where the transcript can't be obtained.
CAPTURE = {
    "Google Meet": ("verified", "Full interviews run end to end."),
    "Microsoft Teams": ("partly verified", "Works with consumer teams.live.com meetings; a work or school tenant may keep guests or bots out."),
    "Zoom": ("not verified", "Joins and transcribes; a full interview hasn't been verified. MeetStream needs a Zoom Meeting SDK app, "
                             "and the meeting must belong to that app's Zoom account."),
}


@dataclass(frozen=True)
class MeetingSpec:
    join_url: str = ""  # manual: the link the recruiter pasted
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    title: str = ""


@dataclass(frozen=True)
class ProviderMeeting:
    join_url: str
    platform: str  # Google Meet, Zoom, Microsoft Teams
    external_id: str = ""  # the provider's own id (provider mode)


class MeetingProvider(Protocol):
    id: str
    label: str

    def capabilities(self) -> dict: ...  # create, update, cancel, waiting_room, attendee_identity
    async def create(self, spec: MeetingSpec) -> ProviderMeeting: ...
    async def update(self, external_id: str, spec: MeetingSpec) -> ProviderMeeting: ...
    async def cancel(self, external_id: str) -> None: ...


class ManualMeetings:
    """A meeting the recruiter made: Recruence checks the link and records it; nothing is created or cancelled remotely."""

    id = "manual"
    label = "A link you create in Meet, Zoom or Teams"

    def capabilities(self) -> dict:
        return {"create": False, "update": False, "cancel": False, "waiting_room": "set it in the meeting's own settings",
                "attendee_identity": False}

    async def create(self, spec: MeetingSpec) -> ProviderMeeting:
        return self.check(spec.join_url)

    async def update(self, external_id: str, spec: MeetingSpec) -> ProviderMeeting:
        return self.check(spec.join_url)

    async def cancel(self, external_id: str) -> None:
        return None

    @staticmethod
    def check(url: str) -> ProviderMeeting:
        """Raises ValueError with the reason when the link isn't one MeetStream can join."""
        url = (url or "").strip()
        if not url:
            raise ValueError("Paste the meeting's link")
        try:
            platform = detect_platform(url)
        except UnsupportedMeetingLink as e:
            raise ValueError(str(e)) from None
        return ProviderMeeting(join_url=url, platform=platform.name)


PROVIDERS: dict[str, MeetingProvider] = {ManualMeetings.id: ManualMeetings()}


def capture_note(platform: str) -> dict:
    level, text = CAPTURE.get(platform, ("unknown", "Not a platform MeetStream lists."))
    return {"level": level, "note": text}
