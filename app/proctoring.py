"""Proctored interviews: what the app can honestly know about camera and screen sharing.

The candidate's interview room stays their Meet / Zoom / Teams call. What MeetStream offers
(docs.meetstream.ai, read 2026-11-02; none of it seen in a live call yet):

- `video_separate_streams: true` records each webcam and each screen share as its own file.
  After the call, `GET /bots/{id}/get_recording_streams` lists them with `type` ("webcam" or
  "screenshare"), the participant, a start time and a duration (per-participant-video guide).
  That is the one documented, structured source for when a screen was shared, and it is read
  here after the call.
- Meet and Teams name `screenshare_on` / `screenshare_off` and `webcam_on` / `webcam_off`
  participant events, but not their payload or how they are delivered. If one reaches the
  bot's callback during the call, it is logged as reported; nothing depends on it.
- No API reports a participant's camera state; a camera that is off is a black tile in the
  recording. The shared screen's content is never analysed.

So an event's `source` always says how it is known, and the record lists what couldn't be
checked. A gap in screen sharing is a technical fact, never a finding about the candidate.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from app.models import ProctoringEvent

LIVE_ACTIONS = {
    "screenshare_on": ("screen_share_started", "MeetStream reported that screen sharing started"),
    "screenshare_off": ("screen_share_stopped", "MeetStream reported that screen sharing stopped"),
    "webcam_on": ("camera_on", "MeetStream reported that the camera was turned on"),
    "webcam_off": ("camera_off", "MeetStream reported that the camera was turned off"),
}
# A gap shorter than this between two recorded screen-share segments is a recording seam, not an interruption.
MIN_GAP_SECONDS = 3.0
# A share whose stream starts this close to the recording's start was already on when the bot arrived (live, 2026-11-02:
# a screen shared before the bot joined was recorded from the bot's admission, for the whole call).
AT_START_SECONDS = 5.0


def _when(value: Any) -> Optional[datetime]:
    """A timestamp in whatever form MeetStream sent it: ISO text, or epoch seconds / milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _seconds(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"\d+(\.\d+)?", value.strip()):
        return float(value)
    return None


def _participant_name(item: dict) -> str:
    p = item.get("participant")
    if isinstance(p, dict):
        return str(p.get("name") or p.get("full_name") or p.get("displayName") or "").strip()
    for key in ("participant_name", "speakerName", "displayName", "fullName", "name"):
        if item.get(key):
            return str(item[key]).strip()
    return ""


def live_event(payload: dict, received_at: datetime) -> Optional[ProctoringEvent]:
    """A screen-share or webcam event from the bot's callback, or None if the payload isn't one."""
    event = str(payload.get("bot_event") or payload.get("event") or "")
    action = str(payload.get("action") or event.removeprefix("participant_events.")).strip().lower()
    if action not in LIVE_ACTIONS:
        return None
    kind, text = LIVE_ACTIONS[action]
    who = _participant_name(payload)
    return ProctoringEvent(at=_when(payload.get("timestamp")) or received_at, kind=kind, source="meetstream_live",
                           text=f"{text}{f' ({who})' if who else ''}.", participant=who)


def recording_events(streams: Any, started_at: datetime, is_candidate: Callable[[str], bool],
                     is_bot: Callable[[str], bool] = lambda name: False) -> tuple[list[ProctoringEvent], float, int, list[str]]:
    """Screen-share segments from get_recording_streams: the events, total shared seconds, interruptions, notes.

    Only the candidate's shares count (by name; anyone else's are listed but not totalled). Times come from
    `start_timestamp.absolute` when present, otherwise `relative` seconds from the start of the recording.
    A camera stream from anyone but the candidate and the bot is listed as another participant: the stream list
    is the only documented record of who was in the call, and it shows only people with a camera on.
    """
    notes: list[str] = []
    items = streams if isinstance(streams, list) else (streams.get("streams") or streams.get("data") or []) if isinstance(streams, dict) else []
    segments: list[tuple[datetime, float, str, bool]] = []
    webcams = 0
    others: dict[str, Optional[datetime]] = {}  # name -> when their camera stream started
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").lower()
        if kind == "webcam":
            webcams += 1
            who = _participant_name(item)
            if who and not is_candidate(who) and not is_bot(who) and who not in others:
                start = item.get("start_timestamp") or {}
                at = _when(start.get("absolute")) if isinstance(start, dict) else _when(start)
                if at is None and isinstance(start, dict) and _seconds(start.get("relative")) is not None:
                    at = started_at + timedelta(seconds=_seconds(start["relative"]))
                others[who] = at
            continue
        if kind != "screenshare":
            continue
        start = item.get("start_timestamp") or {}
        at = _when(start.get("absolute")) if isinstance(start, dict) else _when(start)
        if at is None and isinstance(start, dict) and _seconds(start.get("relative")) is not None:
            at = started_at + timedelta(seconds=_seconds(start["relative"]))
        duration = _seconds(item.get("duration"))
        if at is None or duration is None:
            notes.append("A screen-share recording had no usable start time or duration.")
            continue
        who = _participant_name(item)
        segments.append((at, duration, who, is_candidate(who) if who else True))
    segments.sort(key=lambda s: s[0])

    events: list[ProctoringEvent] = []
    total, interruptions = 0.0, 0
    last_end: Optional[datetime] = None
    for at, duration, who, theirs in segments:
        end = at + timedelta(seconds=duration)
        label = f" ({who})" if who else ""
        if not theirs:
            events.append(ProctoringEvent(at=at, kind="other_screen_share", source="meetstream_recording", participant=who,
                                          duration_seconds=duration, text=f"Someone other than the candidate shared a screen{label}."))
            continue
        if last_end is not None and (at - last_end).total_seconds() >= MIN_GAP_SECONDS:
            gap = (at - last_end).total_seconds()
            interruptions += 1
            events.append(ProctoringEvent(at=last_end, kind="screen_share_interrupted", source="meetstream_recording", participant=who,
                                          duration_seconds=gap, text=f"Screen sharing stopped for {_span(gap)}, then resumed."))
        already = last_end is None and abs((at - started_at).total_seconds()) <= AT_START_SECONDS
        events.append(ProctoringEvent(at=at, kind="screen_share_started" if last_end is None else "screen_share_resumed",
                                      source="meetstream_recording", participant=who, duration_seconds=duration,
                                      text=(f"The screen was already being shared when the recording started; recorded for {_span(duration)}{label}."
                                            if already else f"Screen shared for {_span(duration)}{label}.")))
        total += duration
        last_end = end
    if last_end is not None:
        events.append(ProctoringEvent(at=last_end, kind="screen_share_stopped", source="meetstream_recording",
                                      text="The screen-share recording ends here: sharing stopped, or the bot left the call."))
    for who, at in others.items():
        events.append(ProctoringEvent(at=at or started_at, kind="other_participant", source="meetstream_recording", participant=who,
                                      text=f"Someone other than the candidate was in the call with their camera on ({who})."))
    if not segments:
        notes.append("MeetStream's recording has no screen share.")
    if webcams == 0:
        notes.append("MeetStream's recording has no separate camera stream for anyone.")
    return events, total, interruptions, notes


def _span(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    m, s = divmod(seconds, 60)
    return f"{m} min {s} s" if s else f"{m} min"


def ordered(events: Iterable[ProctoringEvent]) -> list[ProctoringEvent]:
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(events, key=lambda e: e.at or far)
