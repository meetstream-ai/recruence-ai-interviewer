"""One MeetStream bot in one meeting: join, lifecycle, live transcript, leave, erase.

Documented contracts this relies on (docs.meetstream.ai, 2026-10-28):
- create_bot with `agent_config_id` attaches an MIA agent; MeetStream fills the audio
  bridge fields itself, so we never send `socket_connection_url` / `live_audio_required`.
- `callback_url` gets lifecycle events; branch on `bot_event`; exactly one ending event.
- `live_transcription_required.webhook_url` + a streaming provider in `sentence` mode
  POSTs one finished phrase per request (`utterance`, `speakerName`, `timestamp`).
- Both kinds of delivery are unsigned and not retried. Each session gets a random
  path token, and every payload's `bot_id` must match this bot.
UNVERIFIED: that live transcription is delivered while an MIA agent is attached.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import meetstream

from app.meeting.platforms import Platform

logger = logging.getLogger(__name__)

LineHandler = Callable[[str, str, bool], Awaitable[None]]  # speaker, text, is_bot
EndedHandler = Callable[[str], Awaitable[None]]
AgentErrorHandler = Callable[[str], Awaitable[None]]
ParticipantEventHandler = Callable[[dict], Awaitable[None]]

# MIA reports problems in the agent (e.g. its LLM provider at capacity) as these callback events.
AGENT_ERROR_EVENTS = {"agent_error", "bot.error"}

IN_MEETING_EVENTS = {"bot.inmeeting", "bot.recording"}
# Zoom asks the host to allow recording; seen live 2026-11-13 as recording_permission_allowed, then recording.
RECORDING_ALLOWED_EVENTS = {"bot.recording_permission_allowed", "bot.recording"}
# Zoom: the host refused recording (documented by MeetStream in 2026; bot.stopped follows). Said at once, not after the wait.
RECORDING_DENIED_EVENTS = {"bot.recording_permission_denied"}
ENDING_EVENTS = {"bot.stopped", "bot.kicked", "bot.denied", "bot.notallowed", "bot.failed"}
IN_MEETING_STATUSES = {"inmeeting", "recording"}
ENDED_STATUSES = {"stopped", "denied", "notallowed", "error", "failed"}


class MeetingError(RuntimeError):
    pass


def _norm(status: Any) -> str:
    return str(status or "").replace("_", "").replace(" ", "").lower()


class MeetStreamBot:
    def __init__(
        self,
        client: Any,  # meetstream.AsyncMeetStream or a test double
        *,
        meeting_link: str,
        platform: Platform,
        bot_name: str,
        public_base_url: str,
        webhook_token: str,
        session_id: str,
        interview_id: str,
        retention_hours: int = 24,
        waiting_room_timeout: int = 600,
        poll_interval_s: float = 5.0,
    ):
        self._client = client
        self.meeting_link = meeting_link.strip()
        self.platform = platform
        self.bot_name = bot_name
        self._base = public_base_url.rstrip("/")
        self._token = webhook_token
        self._session_id = session_id
        self._interview_id = interview_id
        self._retention_hours = retention_hours
        self.waiting_room_timeout = max(60, min(waiting_room_timeout, platform.max_waiting_room_seconds))
        self._poll_interval_s = poll_interval_s

        self.bot_id: Optional[str] = None
        self.status: str = ""
        self.end_reason: Optional[str] = None
        self._in_meeting = asyncio.Event()
        self.recording_allowed = asyncio.Event()  # the host allowed recording (Zoom asks; elsewhere it's implied)
        self.recording_denied = asyncio.Event()  # the host refused it (Zoom)
        self._ended = asyncio.Event()
        self._leaving = False
        self._seen: set[tuple] = set()
        self._on_line: Optional[LineHandler] = None
        self._on_ended: Optional[EndedHandler] = None
        self._on_agent_error: Optional[AgentErrorHandler] = None
        self.on_participant_event: Optional[ParticipantEventHandler] = None

    def bind(self, on_line: LineHandler, on_ended: EndedHandler, on_agent_error: Optional[AgentErrorHandler] = None) -> None:
        self._on_line = on_line
        self._on_ended = on_ended
        self._on_agent_error = on_agent_error

    @property
    def in_meeting(self) -> bool:
        return self._in_meeting.is_set() and not self._ended.is_set()

    # ---------- payload ----------

    @property
    def events_url(self) -> str:
        return f"{self._base}/webhooks/meetstream/{self._token}/events"

    @property
    def transcript_url(self) -> str:
        return f"{self._base}/webhooks/meetstream/{self._token}/transcript"

    def create_payload(self, agent_config_id: Optional[str], record_video: bool = False, separate_streams: bool = False) -> dict:
        """No agent_config_id: a silent bot that only transcribes (Meeting Analyst mode).

        separate_streams (proctored interviews): MeetStream also records each webcam and each screen share as its
        own file (`video_separate_streams`, per-participant-video guide), which is how the app later learns when a
        screen was shared. Documented, not yet seen in a live call.
        """
        payload = {
            "meeting_link": self.meeting_link,
            "bot_name": self.bot_name,
            "video_required": record_video,
            "callback_url": self.events_url,
            "live_transcription_required": {"webhook_url": self.transcript_url},
            "recording_config": {
                "transcript": {"provider": {"meetstream_streaming": {"transcription_mode": "sentence"}}},
                "retention": {"type": "timed", "hours": self._retention_hours},
                **({"video_layout": "speaker_view"} if record_video else {}),
            },
            "automatic_leave": {
                "waiting_room_timeout": self.waiting_room_timeout,
                "everyone_left_timeout": 120,
                # Candidates pause to think; the 100 s default would end the call mid-interview.
                "voice_inactivity_timeout": 600,
            },
            "custom_attributes": {"session_id": self._session_id, "interview_id": self._interview_id, "app": "recruence"},
        }
        if agent_config_id:
            payload["agent_config_id"] = agent_config_id
        if separate_streams:
            payload["video_required"] = True
            payload["video_separate_streams"] = True
        return payload

    # ---------- lifecycle ----------

    async def join(self, agent_config_id: Optional[str], record_video: bool = False, separate_streams: bool = False) -> None:
        """Create the bot and wait until it is admitted. Raises MeetingError on failure."""
        try:
            payload = self.create_payload(agent_config_id, record_video, separate_streams)
            created = await self._client.bots.create(payload, idempotency_key=self._session_id)
        except meetstream.MeetStreamError as e:
            raise MeetingError(f"MeetStream could not create the bot: {e}") from e
        self.bot_id = created.get("bot_id") if isinstance(created, dict) else None
        if not self.bot_id:
            raise MeetingError(f"MeetStream returned no bot_id: {created!r}")
        logger.info("[%s] bot %s created for %s", self._session_id, self.bot_id, self.platform.name)

        # The lobby timeout is enforced by MeetStream; we wait a little longer to hear about it.
        deadline = time.monotonic() + self.waiting_room_timeout + 60
        while not self._in_meeting.is_set():
            if self._ended.is_set():
                raise MeetingError(f"The bot did not join: {self.end_reason}")
            if time.monotonic() > deadline:
                await self.leave()
                raise MeetingError("The bot was not admitted to the meeting in time")
            await self._poll_status()
            try:
                await asyncio.wait_for(self._wait_for_change(), timeout=self._poll_interval_s)
            except asyncio.TimeoutError:
                pass
        if self._ended.is_set():
            raise MeetingError(f"The bot left the meeting: {self.end_reason}")
        logger.info("[%s] bot %s is in the meeting", self._session_id, self.bot_id)

    async def leave(self) -> None:
        self._leaving = True
        if self.bot_id and not self._ended.is_set():
            try:
                await self._client.bots.remove(self.bot_id)
            except meetstream.NotReadyError:
                pass  # 202 Accepted: the bot is leaving
            except meetstream.MeetStreamError as e:
                logger.warning("[%s] remove_bot failed (it may already be gone): %s", self._session_id, e)
        self._mark_ended(self.end_reason or "removed by the app")

    # ---------- webhooks ----------

    def owns(self, payload: dict) -> bool:
        return bool(self.bot_id) and payload.get("bot_id") == self.bot_id

    async def handle_event(self, payload: dict) -> bool:
        if not self.owns(payload):
            return False
        event = str(payload.get("bot_event") or payload.get("event") or "")
        self.status = str(payload.get("bot_status") or self.status)
        logger.info("[%s] MeetStream %s (%s): %s", self._session_id, event, self.status, payload.get("message"))
        if event in RECORDING_ALLOWED_EVENTS:
            self.recording_allowed.set()
        elif event in RECORDING_DENIED_EVENTS:
            self.recording_denied.set()
        if event in IN_MEETING_EVENTS:
            self._in_meeting.set()
        elif event in ENDING_EVENTS:
            await self._ended_by_platform(f"{event}: {payload.get('message') or self.status}")
        elif event in AGENT_ERROR_EVENTS and self._on_agent_error:
            await self._on_agent_error(str(payload.get("message") or event))
        elif self.on_participant_event and (event.startswith("participant_events.") or payload.get("action")):
            # Screen-share and webcam on/off are named for Meet and Teams; how (and whether) they reach this
            # callback is undocumented, so app/proctoring.py reads them defensively.
            await self.on_participant_event(payload)
        return True

    async def handle_transcript(self, payload: dict) -> bool:
        if not self.owns(payload):
            return False
        text = str(payload.get("utterance") or payload.get("transcript") or "").strip()
        if not text:
            return False
        key = (payload.get("timestamp"), text)
        if key in self._seen:
            return False
        self._seen.add(key)
        speaker = str(payload.get("speakerName") or "Unknown speaker").strip()
        is_bot = speaker.casefold() == self.bot_name.casefold()
        # Speaker and size only: candidate words stay out of the log.
        logger.info("[%s] live transcript: speaker=%r bot=%s words=%d", self._session_id, speaker, is_bot, len(text.split()))
        if self._on_line:
            await self._on_line(speaker, text, is_bot)
        return True

    # ---------- internals ----------

    async def _wait_for_change(self) -> None:
        waiters = [asyncio.create_task(self._in_meeting.wait()), asyncio.create_task(self._ended.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()

    async def _poll_status(self) -> None:
        """Backup for lost callbacks (they are never retried)."""
        try:
            result = await self._client.bots.status(self.bot_id)
        except meetstream.MeetStreamError as e:
            logger.debug("[%s] status poll failed: %s", self._session_id, e)
            return
        if not isinstance(result, dict):
            return
        status = _norm(result.get("status") or result.get("bot_status"))
        if status in IN_MEETING_STATUSES:
            self._in_meeting.set()
            if status == "recording":
                self.recording_allowed.set()
        elif status in ENDED_STATUSES:
            await self._ended_by_platform(f"status {status}")

    def _mark_ended(self, reason: str) -> None:
        if not self._ended.is_set():
            self.end_reason = reason
            self._ended.set()

    async def _ended_by_platform(self, reason: str) -> None:
        if self._ended.is_set():
            return
        was_in_meeting = self._in_meeting.is_set()
        self._mark_ended(reason)
        if was_in_meeting and not self._leaving and self._on_ended:
            await self._on_ended(reason)


async def erase_bot_data(client: Any, bot_id: str) -> str:
    """Ask MeetStream to delete a bot's recordings and transcripts. Returns an outcome label."""
    try:
        await client.bots.delete_data(bot_id)
        return "erased"
    except meetstream.NotReadyError:
        return "erase accepted"
    except meetstream.NotFoundError:
        return "not found (already deleted)"
    except meetstream.MeetStreamError as e:
        logger.warning("delete_data for bot %s failed: %s", bot_id, e)
        return f"failed: {e}"
