"""Google Meet, Zoom and Microsoft Teams as MeetStream supports them.

MeetStream's create_bot is platform-agnostic: it detects the platform from the
link. What differs per platform is which links work, how long the bot may wait in
the lobby, what has to be set up first, and how well speakers are attributed.
Source: MeetStream platform matrix (docs.meetstream.ai/navigation), 2026-10-28.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


class UnsupportedMeetingLink(ValueError):
    pass


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    max_waiting_room_seconds: int
    setup: str
    speaker_attribution: str
    live_tested: bool = False
    # Post the AI-interview notice in the meeting chat on join. Off on Zoom: live 2026-10-31 (call 13) one
    # send_message call showed up as the same notice repeated over and over in the Zoom chat.
    chat_notice: bool = True
    # The agent can't hear or speak until the host allows recording (Zoom asks the host when the bot joins). The
    # start watchdog is timed from that permission: live 2026-11-13 it came 2 m 20 s after joining (R-13).
    waits_for_recording_permission: bool = False


GOOGLE_MEET = Platform(
    key="google_meet",
    name="Google Meet",
    max_waiting_room_seconds=600,
    setup="None. Start the meeting before launching: the bot fails at once if the host hasn't started it.",
    speaker_attribution="Approximate: the active speaker when a phrase was spoken.",
    live_tested=True,
)
ZOOM = Platform(
    key="zoom",
    name="Zoom",
    max_waiting_room_seconds=1200,
    setup=(
        "A Zoom Meeting SDK app (Client ID and Secret) configured in MeetStream. Meetings outside that app's "
        "account need a ZAK or OBF token, which this app doesn't support yet. The bot asks the host for recording permission."
    ),
    speaker_attribution="Exact: each speaker's audio is transcribed separately.",
    live_tested=True,
    chat_notice=False,
    waits_for_recording_permission=True,
)
TEAMS = Platform(
    key="teams",
    name="Microsoft Teams",
    max_waiting_room_seconds=1800,
    setup="None for anonymous guest join.",
    speaker_attribution="Approximate: the dominant speaker when a phrase was spoken.",
    live_tested=True,
)
PLATFORMS = {p.key: p for p in (GOOGLE_MEET, ZOOM, TEAMS)}


def detect_platform(link: str) -> Platform:
    """Return the platform for a meeting link, or raise UnsupportedMeetingLink with the reason."""
    link = (link or "").strip()
    parsed = urlparse(link)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if not host:
        raise UnsupportedMeetingLink("Paste the full meeting link, starting with https://")
    if parsed.scheme != "https":
        raise UnsupportedMeetingLink("Meeting links must use https://")

    if host == "meet.google.com":
        if path.startswith("/lookup/"):
            raise UnsupportedMeetingLink("Google Meet /lookup/ links aren't supported by MeetStream; use the meeting's own link")
        if path.strip("/") == "":
            raise UnsupportedMeetingLink("This Google Meet link has no meeting code")
        return GOOGLE_MEET
    if host == "g.co":
        raise UnsupportedMeetingLink("g.co/meet short links aren't supported by MeetStream; use the meet.google.com link")

    if host == "zoom.us" or host.endswith(".zoom.us"):
        for prefix, what in (
            ("/my/", "personal meeting room"),
            ("/w/", "webinar"),
            ("/s/", "/s/ start"),
            ("/wc/join/", "web-client"),
        ):
            if path.startswith(prefix):
                raise UnsupportedMeetingLink(f"Zoom {what} links aren't supported by MeetStream; use the meeting's /j/ join link")
        return ZOOM
    if host.endswith("zoomgov.com"):
        raise UnsupportedMeetingLink("ZoomGov meetings aren't supported by MeetStream")

    if host in ("teams.microsoft.com", "teams.live.com"):
        if path.strip("/") == "":
            raise UnsupportedMeetingLink("This Teams link has no meeting path; copy the full join link")
        return TEAMS
    if host == "teams.cloud.microsoft" or host.endswith(".teams.cloud.microsoft"):
        raise UnsupportedMeetingLink("teams.cloud.microsoft links aren't supported by MeetStream; use a teams.microsoft.com join link")

    raise UnsupportedMeetingLink("Only Google Meet, Zoom and Microsoft Teams links are supported")
