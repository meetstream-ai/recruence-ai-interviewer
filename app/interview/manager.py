"""Session lifecycle around the runner: launch, persistence, scheduling, retention, recovery.

Every runner event is written to the store before it is published, so a finished
session's transcript, notes and report survive a restart. Live runner state is in
memory only: a session that was live when the server stopped is marked failed on
the next start, and its bot and agent config are cleaned up at MeetStream.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from app.config import Settings
from app.interview.events import EventBus
from app.interview.runner import InterviewRunner, ReportSink
from app.llm.base import InterviewAI
from app.meeting.meetstream import MeetStreamBot, erase_bot_data
from app.meeting.platforms import UnsupportedMeetingLink, detect_platform
from app.models import AnswerNote, Utterance, utcnow
from app.planning import PlanError, context_for
from app.store import Store

logger = logging.getLogger(__name__)

TERMINAL = {"completed", "failed"}
FINISHED_TOKEN_TTL_SECONDS = 6 * 3600  # post-call processing callbacks can arrive well after the call


class LaunchError(ValueError):
    pass


class RoomBusy(LaunchError):
    """The meeting room is in use by another interview (or one is starting in it)."""


ROOM_WAIT_MINUTES = 20  # how long a booked interview waits for its room before it's marked failed


async def public_host_resolves(url: str, timeout_s: float = 5.0) -> bool:
    """Whether the public URL's hostname resolves (a stopped quick tunnel's hostname no longer does)."""
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, 443), timeout_s)
    except (OSError, asyncio.TimeoutError):
        return False
    return True


@dataclass
class LiveSession:
    id: str
    interview_id: str
    runner: InterviewRunner
    bus: EventBus
    webhook_token: str


class SessionManager:
    def __init__(self, settings: Settings, store: Store, ai: InterviewAI, client_factory: Optional[Callable[[], Any]] = None,
                 sink: Optional[ReportSink] = None, install_id: str = "",
                 reaches_this_app: Optional[Callable[[], Awaitable[Optional[bool | str]]]] = None):
        self.settings = settings
        # On a database several installations share: which one this is, and whether the Public URL leads to this
        # process (None: couldn't tell). MeetStream calls back on the Public URL, so an interview started here while it
        # leads to another installation would talk to the wrong app.
        self.install_id = install_id
        self._reaches_this_app = reaches_this_app
        self.store = store
        self.sink = sink or store  # where finished reports go: the store, or the store plus the export
        self.ai = ai
        self._client_factory = client_factory
        self._client: Any = None
        self.live: dict[str, LiveSession] = {}
        # Interviews (and their rooms) between the start of launch() and the live session existing. launch() awaits
        # in between, so without this two quick starts could both pass the "already live" check: two bots, billed twice.
        self._launching: set[str] = set()
        self._launching_rooms: set[str] = set()
        self._waiting_for_room: set[str] = set()  # booked interviews whose room was still in use at their time
        self._by_token: dict[str, LiveSession] = {}
        # MeetStream keeps calling back after a session ends (bot.leaving, bot.stopped, post-call events).
        # Their tokens are remembered for a while so those calls are acknowledged instead of 404ed.
        self._finished_tokens: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._loops: list[asyncio.Task] = []

    # ---------- MeetStream client ----------

    def reset_client(self) -> None:
        """The next call builds a client with the current key (after the key changes in Settings)."""
        if not self._client_factory:
            self._client = None

    def client(self) -> Any:
        if self._client is None:
            if self._client_factory:
                self._client = self._client_factory()
            else:
                import meetstream

                self._client = meetstream.AsyncMeetStream(api_key=self.settings.meetstream_api_key)
        return self._client

    async def client_for(self, api_key: str, call: Callable[[Any], Awaitable[Any]]) -> Any:
        """`call` with a client for a key that isn't saved yet (Settings → Check), closed afterwards."""
        if self._client_factory:  # tests: the fake client stands for any key
            return await call(self._client_factory())
        import meetstream

        client = meetstream.AsyncMeetStream(api_key=api_key)
        try:
            return await call(client)
        finally:
            await client.aclose()

    # ---------- lookups ----------

    def by_token(self, token: str) -> Optional[LiveSession]:
        return self._by_token.get(token)

    def recently_finished(self, token: str) -> bool:
        now = time.monotonic()
        for t, at in list(self._finished_tokens.items()):
            if now - at > FINISHED_TOKEN_TTL_SECONDS:
                del self._finished_tokens[t]
        return token in self._finished_tokens

    def live_for_interview(self, interview_id: str) -> Optional[LiveSession]:
        return next((s for s in self.live.values() if s.interview_id == interview_id), None)

    # ---------- launch ----------

    async def launch(self, interview_id: str) -> str:
        interview = self.store.get_interview(interview_id)
        if interview is None:
            raise LaunchError("Interview not found")
        if self.live_for_interview(interview_id) or interview_id in self._launching:
            raise LaunchError("This interview already has a live session")
        room = _room(interview.meeting_link)
        # One interview per meeting room at a time, however it starts (the candidate's link, the recruiter, or the
        # scheduler at a booked time): two AI interviewers in one call would talk over each other.
        if room and room in self._launching_rooms:
            raise RoomBusy("Another interview is starting in this meeting room right now")
        if room and any(_room(live.runner.bot.meeting_link) == room for live in self.live.values()):
            raise RoomBusy("Another interview is under way in this meeting room. Wait until it ends, or give this "
                           "candidate their own meeting on their interview page")
        # Claimed before the first await (asyncio runs this synchronously up to there), released whatever happens.
        self._launching.add(interview_id)
        if room:
            self._launching_rooms.add(room)
        try:
            return await self._launch(interview)
        finally:
            self._launching.discard(interview_id)
            self._launching_rooms.discard(room)

    async def _launch(self, interview) -> str:
        interview_id = interview.id
        if interview.demo:
            raise LaunchError("This is demo data: demo interviews can't run in a real meeting")
        if interview.plan is None or interview.plan_approved_at is None:
            raise LaunchError("Approve the interview plan first")
        if interview.consent_attested_at is None:
            raise LaunchError("Confirm that the candidate was told about the AI interview, recording and transcription")
        ready, why = self.settings.meetstream_ready
        if not ready:
            raise LaunchError(why)
        if self.settings.check_public_url and not await public_host_resolves(self.settings.public_base_url):
            # Live 2026-11-01: with a stopped quick tunnel in PUBLIC_BASE_URL, MeetStream answered the agent
            # setup with a bare "500 Internal server error" (its tool URLs must resolve). Say what's wrong instead.
            raise LaunchError(
                f"PUBLIC_BASE_URL ({self.settings.public_base_url}) doesn't resolve, so MeetStream can't reach this app. "
                "If it's a quick tunnel, start it, put its new address in PUBLIC_BASE_URL and restart."
            )
        reach = await self._reaches_this_app() if self.settings.check_public_url and self._reaches_this_app is not None else None
        if isinstance(reach, str):  # it leads somewhere else; the message says where
            raise LaunchError(f"MeetStream couldn't reach this interview through the Public URL. {reach}")
        if reach is False:
            raise LaunchError(
                f"The Public URL ({self.settings.public_base_url}) leads to another Recruence, not this one, so MeetStream's "
                "calls for this interview would go there. Start it from the installation the address leads to, or point "
                "the address (your tunnel) at this one.")
        try:
            platform = detect_platform(interview.meeting_link)
            context = context_for(interview)
        except (UnsupportedMeetingLink, PlanError) as e:
            raise LaunchError(str(e)) from None

        session_id = uuid.uuid4().hex[:12]
        token = secrets.token_urlsafe(24)
        bus = EventBus()
        bot = MeetStreamBot(
            self.client(),
            meeting_link=interview.meeting_link,
            platform=platform,
            bot_name=self.settings.bot_name,
            public_base_url=self.settings.public_base_url,
            webhook_token=token,
            session_id=session_id,
            interview_id=interview_id,
            retention_hours=self.settings.meetstream_retention_hours,
            waiting_room_timeout=self.settings.waiting_room_timeout,
        )
        runner = InterviewRunner(
            session_id,
            context,
            self.client(),
            self.ai,
            self.settings,
            bot=bot,
            webhook_token=token,
            emit=lambda event: self._on_event(session_id, interview_id, event),
            sink=self.sink,
            goodbye_grace_s=self.settings.goodbye_grace_seconds,
            end_after_done_s=self.settings.end_after_done_seconds,
            agent_recovery_s=self.settings.agent_recovery_seconds,
        )
        live = LiveSession(session_id, interview_id, runner, bus, token)
        self.store.create_session(session_id, interview_id, platform.name, self.install_id)
        self.store.save_interview(
            interview.model_copy(
                update={"status": "in_progress", "latest_session_id": session_id, "platform": platform.name, "last_error": ""}
            )
        )
        self.store.audit("session_launched", interview_id, session_id, f"platform={platform.key}")
        self.live[session_id] = live
        self._by_token[token] = live
        self.run_background(runner.start(), f"start {session_id}")
        return session_id

    async def end(self, session_id: str, reason: str = "ended_by_recruiter") -> bool:
        live = self.live.get(session_id)
        if live is None:
            return False
        self.store.audit("session_end_requested", live.interview_id, session_id, reason)
        self.run_background(live.runner.end(reason), f"end {session_id}")
        return True

    # ---------- persistence of runner events ----------

    def _on_event(self, session_id: str, interview_id: str, event: dict) -> None:
        kind = event.get("type")
        try:
            if kind == "utterance":
                self.store.add_utterance(session_id, Utterance.model_validate(event["utterance"]))
            elif kind == "note":
                self.store.upsert_note(session_id, AnswerNote.model_validate(event["note"]))
            elif kind == "ids":
                self.store.update_session(session_id, **{k: v for k, v in event.items() if k in ("bot_id", "agent_config_id")})
            elif kind == "consent":
                self.store.audit("candidate_consented", interview_id, session_id)
            elif kind == "alert":
                # Something in the call the hiring team should look at (app/interview/runner.py): a flag on the interview.
                from app import gateway
                interview = self.store.get_interview(interview_id)
                if interview is not None:
                    flagged = gateway.flag(interview, event["kind"], event.get("message", ""))
                    if flagged is not interview:
                        self.store.save_interview(flagged)
                self.store.audit("interview_alert", interview_id, session_id, event["kind"])
            elif kind == "candidate_lines_removed":
                removed = self.store.delete_utterances(session_id, "candidate")
                self.store.audit("candidate_lines_deleted", interview_id, session_id, f"no consent; {removed} lines")
            elif kind == "erased":
                self.store.audit("recording_erased", interview_id, session_id, f"no consent; {event.get('outcome')}")
            elif kind == "state":
                self.store.update_session(session_id, state=event["state"])
                if event["state"] in TERMINAL:
                    self._session_finished(session_id, interview_id, event["state"])
        except Exception:
            logger.exception("[%s] could not persist %s event", session_id, kind)
        live = self.live.get(session_id)
        if live:
            live.bus.publish(event)
            if kind == "state" and event["state"] in TERMINAL:
                self._by_token.pop(live.webhook_token, None)
                self._finished_tokens[live.webhook_token] = time.monotonic()
                self.live.pop(session_id, None)

    def _session_finished(self, session_id: str, interview_id: str, state: str) -> None:
        live = self.live.get(session_id)
        reason = live.runner.end_reason if live else None
        self.store.update_session(session_id, ended_at=utcnow(), end_reason=reason or state)
        interview = self.store.get_interview(interview_id)
        if interview and interview.latest_session_id == session_id:
            last_error = ""
            if state == "failed" and live:
                errors = [e["message"] for e in live.bus.history if e.get("type") == "error"]
                last_error = errors[-1] if errors else "The session failed"
            self.store.save_interview(interview.model_copy(update={"status": state, "last_error": last_error}))
        self.store.audit(f"session_{state}", interview_id, session_id, reason or "")

    # ---------- background work ----------

    def run_background(self, coro, label: str) -> asyncio.Task:
        # A fresh context: a task started while handling a recruiter's request must not carry their
        # name (CURRENT_ACTOR) into audit entries for things the app does on its own later.
        task = asyncio.create_task(coro, context=contextvars.Context())
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("background task %s failed", label, exc_info=t.exception())

        task.add_done_callback(done)
        return task

    async def start_loops(self) -> None:
        await self.recover()
        await self.purge_expired()
        self._loops = [asyncio.create_task(self._scheduler_loop()), asyncio.create_task(self._retention_loop())]

    async def aclose(self) -> None:
        for t in self._loops:
            t.cancel()
        for live in list(self.live.values()):
            try:
                await asyncio.wait_for(live.runner.bot.leave(), timeout=5)
            except Exception:
                logger.warning("[%s] could not remove the bot on shutdown", live.id)
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()

    async def _scheduler_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.scheduler_interval_seconds)
            try:
                await self.launch_due()
            except Exception:
                logger.exception("scheduler tick failed")

    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await self.purge_expired()
            except Exception:
                logger.exception("retention purge failed")

    async def launch_due(self, now: Optional[datetime] = None) -> list[str]:
        now = now or utcnow()
        launched = []
        if not self.settings.meetstream_ready[0]:
            return launched  # an installation that can't run interviews leaves booked ones to one that can
        for interview in self.store.due_interviews(now):
            # On a shared database, one installation starts it: the first to claim it.
            if not self.store.claim_launch(interview.id, self.install_id or "this"):
                continue
            try:
                launched.append(await self.launch(interview.id))
                self._waiting_for_room.discard(interview.id)
                self.store.release_launch(interview.id)
            except RoomBusy as e:
                # The room is still in use (the interview before ran over): try again on the next tick, for a while.
                late = (now - _utc(interview.scheduled_at)).total_seconds() / 60
                self.store.release_launch(interview.id)
                if late < ROOM_WAIT_MINUTES:
                    if interview.id not in self._waiting_for_room:
                        self._waiting_for_room.add(interview.id)
                        self.store.audit("scheduled_launch_waiting", interview.id, detail=str(e))
                    continue
                self._waiting_for_room.discard(interview.id)
                logger.warning("scheduled interview %s could not start: %s", interview.id, e)
                self.store.save_interview(interview.model_copy(update={"status": "failed", "last_error": f"Scheduled start failed: {e}"}))
                self.store.audit("scheduled_launch_failed", interview.id, detail=str(e))
            except LaunchError as e:
                logger.warning("scheduled interview %s could not start: %s", interview.id, e)
                self.store.save_interview(interview.model_copy(update={"status": "failed", "last_error": f"Scheduled start failed: {e}"}))
                self.store.audit("scheduled_launch_failed", interview.id, detail=str(e))
                self.store.release_launch(interview.id)
        return launched

    def room_busy(self, meeting_link: str) -> bool:
        """A live session is using this meeting room (a role's shared interview room), or one is starting in it."""
        link = _room(meeting_link)
        return link in self._launching_rooms or any(_room(live.runner.bot.meeting_link) == link for live in self.live.values())

    async def purge_expired(self, now: Optional[datetime] = None) -> list[str]:
        for application_id in self.store.expired_applications(self.settings.data_retention_days, now):
            self.store.delete_application(application_id)
            self.store.audit("application_deleted", detail="retention period ended")
        purged = []
        for interview_id in self.store.expired_interviews(self.settings.data_retention_days, now):
            await self.delete_interview(interview_id, reason="retention period ended")
            purged.append(interview_id)
        return purged

    async def delete_interview(self, interview_id: str, reason: str = "deleted by recruiter") -> dict:
        if self.live_for_interview(interview_id):
            raise LaunchError("End the live session before deleting this interview")
        bot_ids = self.store.delete_interview(interview_id)
        self.store.delete_applications_for_interview(interview_id)  # the application holds the same personal data
        outcomes = {}
        if bot_ids and self.settings.meetstream_api_key:
            for bot_id in bot_ids:
                outcomes[bot_id] = await erase_bot_data(self.client(), bot_id)
        self.store.audit(
            "interview_deleted", interview_id, detail=f"{reason}; MeetStream: " + (", ".join(outcomes.values()) or "no bots")
        )
        return {"deleted": True, "meetstream": outcomes}

    async def recover(self) -> None:
        """Mark sessions that were live when the server stopped as failed, and clean up at MeetStream."""
        # Only this installation's sessions: on a shared database, another installation may be running its own.
        for s in self.store.unfinished_sessions(self.install_id or None):
            self.store.update_session(s["id"], state="failed", ended_at=utcnow(), end_reason="server_restarted")
            interview = self.store.get_interview(s["interview_id"])
            if interview and interview.latest_session_id == s["id"]:
                self.store.save_interview(
                    interview.model_copy(update={"status": "failed", "last_error": "The server restarted during the session"})
                )
            self.store.audit("session_failed", s["interview_id"], s["id"], "server_restarted")
            if not self.settings.meetstream_api_key:
                continue
            client = self.client()
            if s.get("bot_id"):
                try:
                    await client.bots.remove(s["bot_id"])
                except Exception as e:
                    logger.info("recovery: remove_bot %s: %s", s["bot_id"], e)
            if s.get("agent_config_id"):
                try:
                    await client.mia.delete(s["agent_config_id"])
                except Exception as e:
                    logger.info("recovery: delete agent config %s: %s", s["agent_config_id"], e)


def _room(meeting_link: Optional[str]) -> str:
    return (meeting_link or "").strip().rstrip("/").lower()


room_key = _room  # the same meeting room, however its link was written


def _utc(dt: datetime) -> datetime:
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
