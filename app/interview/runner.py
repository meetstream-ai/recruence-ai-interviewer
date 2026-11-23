"""One live interview: an MIA agent in a meeting, driven by our plan.

States:
  created -> joining -> awaiting_consent -> questioning -> leaving -> transcribing -> evaluating -> completed
  joining -> failed            (agent config error, bot denied or never admitted)
  any live state -> leaving     (agent ends, recruiter ends, meeting ends, consent declined)

Rules enforced here, not left to the agent:
- No planned question is released before the candidate's consent is confirmed.
- Questions are served in order; repeated or out-of-date tool calls never skip one.
- The time budget ends the plan at the next transition.
- Without consent, nothing is evaluated, candidate lines are not kept, and the bot's
  MeetStream recording is erased.

Inline plan (realtime, experimental, MIA_REALTIME_INLINE_PLAN): the agent has the whole plan in its
instructions and only calls end_interview, which reports consent, progress and notes. Order and the
consent gate are then the agent's instructions, not enforced here. A session that ends without the
agent confirming consent is treated as not consented, and the plan's length plus
INLINE_OVERRUN_SECONDS is a hard stop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from difflib import SequenceMatcher
from typing import Any, Callable, Optional, Protocol

import meetstream

from app.config import Settings
from app.evaluation import evaluate_interview, squash
from app.interview.agent import VoiceNotConfigured, agent_config, chat_disclosure, first_message, proctoring_needs, setup_request
from app.llm.base import EvaluationInput, InterviewAI
from app.meeting.meetstream import MeetingError, MeetStreamBot, erase_bot_data
from app.models import AnswerNote, EvaluationReport, InterviewContext, ProctoringEvent, ProctoringRecord, Utterance, utcnow
from app.postcall import merge_postcall
from app.proctoring import live_event, ordered, recording_events
from app.compare import same

logger = logging.getLogger(__name__)

Emit = Callable[[dict], None]

GOODBYE_GRACE_SECONDS = 8.0  # let the agent finish its goodbye before the bot leaves
# Observed live (grok-4.20, 2026-10-29): after next_question returned "done" the agent said goodbye but never
# called end_interview, leaving the bot in the call. The app ends the interview itself after this long.
END_AFTER_DONE_SECONDS = 45.0
# MeetStream's "recoverable=False" isn't always final: in live call 1 an agent came back after 80 s of silence.
AGENT_RECOVERY_SECONDS = 120.0
AGENT_TROUBLE_NOTICE = "The AI interviewer has hit a technical problem. Please stay in the meeting while it tries to recover."
AGENT_FAILED_NOTICE = ("The AI interviewer couldn't recover, so this interview is ending now. This wasn't caused by you, "
                       "and the hiring team will see what happened.")
# Live B-1 2026-11-12: the agent joined and then said nothing for 5 minutes, and later kept cutting itself off on one
# question for 15, with no error from MeetStream. The app watches for that itself: no tool call or spoken line this
# long after joining means the conversation hasn't started; a question open far past its share of the time means the
# conversation is stuck. Either way the candidate is told in the chat and the hiring team gets a flag.
AGENT_START_ALERT_SECONDS = 90.0
AGENT_START_GIVE_UP_SECONDS = 300.0  # before consent nothing is kept, so a start that never comes is ended
QUESTION_STALL_MIN_SECONDS = 360.0  # and at least 1.5 times the question's share of the interview's time
AGENT_SLOW_START_NOTICE = ("The AI interviewer is taking longer than usual to start. If you haven't heard it yet, please stay in "
                           "the meeting and say \"hello\". The hiring team has been told.")
AGENT_NO_START_NOTICE = ("The AI interviewer couldn't start, so this interview is ending now. This wasn't caused by you, "
                         "and the hiring team will be in touch.")
QUESTION_STALL_NOTICE = ("If the AI interviewer keeps repeating itself or cutting off, you can say \"please move on to the next "
                         "question\". The hiring team has been told.")
# A candidate's request to stop is the agent's to act on (end_interview, reason candidate_stopped). If it hasn't after
# this long, the app ends the interview itself (live B-1: "Can you please end the interview?" wasn't acted on).
STOP_GRACE_SECONDS = 25.0
STOPPING_NOTICE = "Ending the interview as you asked. Thank you for your time."
STOP_REQUEST_MAX_WORDS = 25  # a request to stop is short; a long answer that mentions ending a call isn't one
STOP_REQUEST = re.compile(
    r"\b(?:please|can you|could you|would you|i want to|i'd like to|i would like to|let's|let us|just)\b[^.?!]{0,25}?"
    r"\b(?:end|stop|finish|terminate|quit)\s+(?:the|this)\s+(?:\w+\s+)?(?:interview|call|meeting)\b"
    r"|\b(?:end|stop)\s+(?:the|this)\s+(?:\w+\s+)?(?:interview|call|meeting)\s+now\b",
    re.IGNORECASE,
)
# Inline plan: with no next_question calls the time budget can't be applied at a transition, so the app
# ends the call this long after the planned duration.
INLINE_OVERRUN_SECONDS = 180.0
SAME_QUESTION = 0.8  # how alike two spoken lines are (0-1) to be the same question heard twice
NOTE_LINE = re.compile(r"^\s*(?P<qid>[A-Za-z]\w*)\s*(?:\((?P<n>\d+)[^)]*\))?\s*[:\-–]\s*(?P<note>.+?)\s*$")
# Echo filter: a candidate line is dropped when most of its content words were just spoken by the interviewer.
ECHO_MIN_WORDS = 4
ECHO_OVERLAP = 0.6
ECHO_LOOKBACK = 6
# Words that answer the interviewer. A line with one the interviewer's line doesn't have is the candidate replying,
# not an echo: "Yes, I agree to the interview being recorded and transcribed" repeats most of the consent question
# but starts with "yes", which the question doesn't contain. Seen live: such a consent was dropped as an echo.
REPLY_WORDS = {"yes", "yeah", "yep", "yup", "agree", "agreed", "consent", "sure", "okay", "ok", "absolutely",
               "definitely", "certainly", "fine", "no", "nope", "decline", "don't", "dont", "not"}
# The agent's spoken acknowledgements and goodbyes are not in our transcript (MeetStream's live transcript
# carries only human speech), so echoes of them can't be matched. Observed live without headphones:
# "Thank you." and "Thank you for your time." arrived under the candidate's name. A candidate saying
# only this carries no evidence, so whole lines like these are dropped.
ACK_LINES = {
    "thank you", "thanks", "thank you very much", "thank you for your time", "thanks for your time",
    "thank you for explaining that", "thanks for explaining that", "thank you for sharing that",
    "thanks for sharing that", "goodbye", "bye", "thank you goodbye",
}

CONSENT_INSTRUCTION = (
    "Before the first question the candidate must agree to the AI interview, recording and transcription. "
    "If they have clearly agreed, call next_question again with candidate_consented set to true. "
    "If they have not answered yet, ask them. If they decline, thank them, say the hiring team will be in touch "
    "about other options, say goodbye, and call end_interview with reason consent_declined."
)


STREAMS_POLL_SECONDS = 10.0  # proctored interviews: how often to ask whether the per-participant recording is ready


class ReportSink(Protocol):
    def push_report(self, report: EvaluationReport) -> str: ...


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) >= 4]


class InterviewRunner:
    def __init__(
        self,
        session_id: str,
        context: InterviewContext,
        client: Any,
        ai: InterviewAI,
        settings: Settings,
        *,
        bot: MeetStreamBot,
        webhook_token: str,
        emit: Emit,
        sink: ReportSink,
        clock: Callable[[], float] = time.monotonic,
        goodbye_grace_s: float = GOODBYE_GRACE_SECONDS,
        end_after_done_s: float = END_AFTER_DONE_SECONDS,
        agent_recovery_s: float = AGENT_RECOVERY_SECONDS,
        start_alert_s: float = AGENT_START_ALERT_SECONDS,
        start_give_up_s: float = AGENT_START_GIVE_UP_SECONDS,
        question_stall_min_s: float = QUESTION_STALL_MIN_SECONDS,
        stop_grace_s: float = STOP_GRACE_SECONDS,
    ):
        self.session_id = session_id
        self.context = context.model_copy(update={"answer_handoff": settings.mia_answer_handoff})
        self.ai = ai
        self.settings = settings
        self.bot = bot
        self._client = client
        self._emit = emit
        self._sink = sink
        self._clock = clock
        self._goodbye_grace_s = goodbye_grace_s
        self._end_after_done_s = end_after_done_s
        self._done_watch: Optional[asyncio.Task] = None
        self._time_watch: Optional[asyncio.Task] = None
        self.inline_plan = settings.inline_plan
        self.tool_token = secrets.token_urlsafe(24)
        self.tool_base_url = f"{settings.public_base_url.rstrip('/')}/webhooks/mia/{webhook_token}"
        bot.bind(self._on_line, self._on_meeting_ended, self._on_agent_error)
        # Proctored interviews: the timeline for the recruiter (app/proctoring.py says what each source can know).
        self.proctoring_events: list[ProctoringEvent] = []
        self.setup_done = False
        self._setup_requested = False
        self._share_stopped_pending = False
        if context.proctored:
            bot.on_participant_event = self._on_participant_event

        self.state = "created"
        self.consented = False
        self.transcript: list[Utterance] = []
        self.notes: dict[str, AnswerNote] = {}
        self.asked: list[str] = []
        self.q_index = -1  # -1: before the first question
        self.time_up = False
        self.agent_config_id: Optional[str] = None
        self.report: Optional[EvaluationReport] = None
        self.end_reason: Optional[str] = None
        self.started_at = utcnow()
        self._t0 = clock()
        self._ended = False
        self._agent_recovery_s = agent_recovery_s
        self._agent_watch: Optional[asyncio.Task] = None
        self._agent_giving_up = False  # past the point of no return: the watch is ending the interview
        self._agent_seen = 0.0  # when the conversation last showed signs of going on after an agent error
        self._lock = asyncio.Lock()
        self._end_task: Optional[asyncio.Task] = None
        self._start_alert_s = start_alert_s
        self._start_give_up_s = start_give_up_s
        self._question_stall_min_s = question_stall_min_s
        self._stop_grace_s = stop_grace_s
        self._agent_active = False  # a tool call or a spoken line has shown the agent is in the conversation
        self._start_watch: Optional[asyncio.Task] = None
        self._stall_watch: Optional[asyncio.Task] = None
        self._stop_watch: Optional[asyncio.Task] = None
        self._follow_up_counts: dict[str, int] = {}  # follow_up calls the app allowed, per question

    # ---------- status ----------

    @property
    def elapsed_seconds(self) -> int:
        return int(self._clock() - self._t0)

    @property
    def follow_ups(self) -> int:
        return sum(n.follow_ups for n in self.notes.values())

    def snapshot(self) -> dict:
        n = len(self.context.questions)
        current = self.context.questions[self.q_index] if 0 <= self.q_index < n else None
        return {
            "session_id": self.session_id,
            "state": self.state,
            "consented": self.consented,
            "question_number": max(0, min(self.q_index + 1, n)),
            "question_total": n,
            "current_question_id": current.id if current else None,
            "follow_ups": self.follow_ups,
            "elapsed_seconds": self.elapsed_seconds,
            "platform": self.bot.platform.name,
            "bot_status": self.bot.status,
            "mode": self.context.mode,
        }

    # ---------- lifecycle ----------

    @property
    def analyst(self) -> bool:
        return self.context.mode == "analyst"

    async def start(self) -> None:
        self.voice_label = self.settings.mia_label  # what this call uses; later changes apply to later calls
        self._set_state("joining")
        if self.analyst:
            await self._join()  # Meeting Analyst: a bot without a voice agent, so it never speaks
            return
        try:
            config = agent_config(self.settings, self.context, self.session_id, self.tool_base_url, self.tool_token)
        except VoiceNotConfigured as e:
            self._fail(f"The AI interviewer isn't set up: {e}")
            return
        try:
            created = await self._client.mia.create(config)
        except meetstream.ServerError as e:
            # Live 2026-11-01: a bare 500 here meant MeetStream couldn't resolve this app's tool URL (a stopped tunnel).
            self._fail(
                f"Could not create the MIA voice agent: {e}. MeetStream answers this when it can't reach this app's "
                f"address ({self.settings.public_base_url}): check the tunnel is running and PUBLIC_BASE_URL is current."
            )
            return
        except meetstream.MeetStreamError as e:
            self._fail(
                f"Could not create the MIA voice agent: {e}. Check that the provider keys for the configured MIA "
                "models are saved under Integrations in the MeetStream dashboard."
            )
            return
        self.agent_config_id = created.get("agent_config_id") if isinstance(created, dict) else None
        if not self.agent_config_id:
            self._fail(f"MeetStream returned no agent_config_id: {created!r}")
            return
        self._emit({"type": "ids", "agent_config_id": self.agent_config_id})
        await self._join()

    async def _join(self) -> None:
        join = asyncio.create_task(self.bot.join(self.agent_config_id, record_video=self.context.record_video,
                                                 separate_streams=self.context.proctored))
        reported = False
        try:
            while not join.done():  # persist the bot id as soon as MeetStream assigns it
                await asyncio.sleep(0.05)
                if self.bot.bot_id and not reported:
                    self._emit({"type": "ids", "bot_id": self.bot.bot_id})
                    reported = True
            await join
        except MeetingError as e:
            await self._delete_agent_config()
            if not self._ended:
                self._fail(f"Could not join the meeting: {e}")
            return
        finally:
            if self.bot.bot_id and not reported:
                self._emit({"type": "ids", "bot_id": self.bot.bot_id})
        if self._ended:
            return
        logger.info("[%s] bot admitted %.1fs after launch", self.session_id, self._clock() - self._t0)
        self.started_at = utcnow()
        self._t0 = self._clock()
        if self.analyst:
            # No AI asks for consent here: the recruiter confirmed it at launch, and the interviewer leads the call.
            self.consented = True
            self._record("System", "system", "Meeting Analyst: the bot listens and transcribes only; the interviewer leads the conversation. "
                         "The recruiter confirmed at launch that the candidate agreed to recording and AI analysis.")
            if self.settings.chat_disclosure and self.bot.platform.chat_notice:
                await self._post_chat_disclosure()
            self._set_state("questioning")
            return
        self._record(self.settings.bot_name, "interviewer", first_message(self.context))
        if self.context.proctored:
            needs = proctoring_needs(self.context, "their")
            self._proctoring("proctoring_enabled", "app", f"Proctored interview. The opening told the candidate they'd be asked to keep {needs}."
                             if needs else "Proctored interview, with neither camera nor screen sharing required.")
        if self.context.questions_in_chat and not self.bot.platform.chat_notice:
            self._record("System", "system", f"Questions aren't posted in the meeting chat on {self.bot.platform.name}.")
        if self.settings.chat_disclosure and self.bot.platform.chat_notice:
            await self._post_chat_disclosure()
        if self.inline_plan:
            self._record("System", "system", "Experimental inline plan: the agent reports consent and progress when it ends the interview.")
            self._time_watch = asyncio.create_task(self._end_at_time_limit())
        else:
            self._start_watch = asyncio.create_task(self._watch_agent_start())
        self._set_state("awaiting_consent")

    async def _post_chat_disclosure(self) -> None:
        text = chat_disclosure(self.context)
        try:
            await self._client.bots.send_message(self.bot.bot_id, text)
        except meetstream.MeetStreamError as e:
            logger.warning("[%s] could not post the disclosure in meeting chat: %s", self.session_id, e)
            return
        self._record("System", "system", f"Posted in the meeting chat: {text}")

    async def _tell_candidate(self, text: str) -> None:
        """For when the agent can't speak: the meeting chat is the only way left to reach the candidate."""
        if not (self.bot.in_meeting and self.bot.platform.chat_notice):
            return
        try:
            await self._client.bots.send_message(self.bot.bot_id, text)
        except meetstream.MeetStreamError as e:
            logger.warning("[%s] could not post in meeting chat: %s", self.session_id, e)
            return
        self._record("System", "system", f"Posted in the meeting chat: {text}")

    async def _post_question(self, number: int, total: int, text: str) -> None:
        message = f"Question {number} of {total}: {text}"[:2000]  # MeetStream's limit per chat message
        try:
            await self._client.bots.send_message(self.bot.bot_id, message)
        except meetstream.MeetStreamError as e:
            logger.warning("[%s] could not post question %d in meeting chat: %s", self.session_id, number, e)
            return
        self._record("System", "system", f"Posted in the meeting chat: {message}")

    async def end(self, reason: str = "ended_by_recruiter") -> None:
        async with self._lock:
            await self._finish(reason)

    # ---------- MIA tool calls ----------

    def authorised(self, authorization: Optional[str], bot_id: Optional[str]) -> bool:
        if not same(authorization, f"Bearer {self.tool_token}"):
            return False
        # MeetStream adds X-Bot-ID; once our bot is known, calls for another bot are rejected.
        return not (self.bot.bot_id and bot_id and bot_id != self.bot.bot_id)

    async def handle_tool(self, name: str, args: dict) -> dict:
        self._agent_active = True
        if self._agent_watch is not None and not self._agent_giving_up:  # a tool call proves the agent is back
            self._agent_watch.cancel()
            self._agent_watch = None
            self._record("System", "system", "The voice agent is responding again.")
        if name == "next_question":
            return await self._next_question(args)
        if name == "end_interview":
            return self._end_interview(args)
        if name == "follow_up":
            return await self._follow_up(args)
        return {"status": "error", "message": f"Unknown tool {name}"}

    async def _next_question(self, args: dict) -> dict:
        async with self._lock:
            if self._ended:
                return {"status": "done", "instruction": "The interview has ended. Say goodbye briefly."}
            if not self.consented:
                if args.get("candidate_consented") is not True:
                    logger.info("[%s] next_question before consent: consent_required", self.session_id)
                    return {"status": "consent_required", "instruction": CONSENT_INSTRUCTION}
                self.consented = True
                logger.info("[%s] consent confirmed by the agent %.1fs after admission", self.session_id, self.elapsed_seconds)
                self._record("System", "system", "The candidate agreed to the AI interview, recording and transcription.")
                self._emit({"type": "consent"})
                self._set_state("questioning")
            if self.context.proctored and proctoring_needs(self.context) and not self.setup_done:
                setup = str(args.get("proctoring_setup") or "").strip().lower()
                if setup not in ("ready", "unable"):
                    if not self._setup_requested:
                        self._setup_requested = True
                        self._proctoring("setup_requested", "app", f"The AI was told to ask: \"{setup_request(self.context)}\"")
                    return {"status": "setup_required", "instruction": (
                        f"Say exactly: \"{setup_request(self.context)}\" Then wait until the candidate says it's done and call "
                        "next_question with proctoring_setup \"ready\", or \"unable\" with a proctoring_note if they can't manage it.")}
                self.setup_done = True
                needs = proctoring_needs(self.context, "their")
                if setup == "ready":
                    self._proctoring("setup_confirmed_by_candidate", "candidate",
                                     f"The candidate said they have {needs}. This is what they said in the call, not something the app could check.")
                else:
                    note = str(args.get("proctoring_note") or "").strip()[:300]
                    self._proctoring("setup_unavailable", "candidate", f"The candidate couldn't manage to have {needs}; the interview went on."
                                     + (f" The AI's note: {note}" if note else ""))

            questions = self.context.questions
            current = questions[self.q_index] if 0 <= self.q_index < len(questions) else None
            prev_id = str(args.get("previous_question_id") or "").strip() or None
            advance = self.q_index == -1 or (current is not None and prev_id == current.id)
            if advance and current is not None:
                self._note(current.id, args)
            elif prev_id and prev_id in self.asked and prev_id != (current.id if current else None):
                self._note(prev_id, args, late=True)  # a second call for a question already closed
            if advance:
                self.q_index += 1

            if self.elapsed_seconds > self.context.duration_minutes * 60:
                self.time_up = True
            if self.q_index >= len(questions) or self.time_up:
                self.q_index = min(self.q_index, len(questions))
                if self._done_watch is None:
                    self._done_watch = asyncio.create_task(self._end_if_agent_does_not())
                return {
                    "status": "done",
                    # Live 16 November (0.13.5, Meet and Zoom): the agent reached done but never called end_interview.
                    "instruction": ("The planned questions are finished. In this same turn, without waiting for the candidate to "
                                    "reply: thank them, say goodbye in one or two sentences, then call end_interview with reason completed."),
                }
            q = questions[self.q_index]
            if advance:
                self.asked.append(q.id)
                self._record(self.settings.bot_name, "interviewer", q.text, question_id=q.id)
                if self.context.questions_in_chat and self.bot.platform.chat_notice:
                    # Posted once per question (only when the plan advances), while the agent reads it out.
                    asyncio.create_task(self._post_question(self.q_index + 1, len(questions), q.text))
                self._watch_question(self.q_index)
                self._set_state("questioning")
            result: dict = {}
            if self._share_stopped_pending:
                self._share_stopped_pending = False
                result["instruction"] = ("MeetStream reported that screen sharing stopped. Before this question, politely ask the candidate "
                                         "once to share their entire screen again, then continue whatever the answer.")
            return {
                **result,
                "status": "question",
                "question_id": q.id,
                "question_number": self.q_index + 1,
                "total_questions": len(questions),
                "question": q.text,
                "max_follow_ups": self._max_follow_ups(q),
                "look_for": q.look_for,
            }

    def _note(self, question_id: str, args: dict, late: bool = False) -> None:
        text = str(args.get("answer_note") or "").strip()
        follow_ups = args.get("follow_ups_asked")
        follow_ups = follow_ups if isinstance(follow_ups, int) and 0 <= follow_ups <= 10 else 0
        follow_ups = max(follow_ups, self._follow_up_counts.get(question_id, 0))  # the follow_up calls the app counted
        existing = self.notes.get(question_id)
        if late and (not text or (existing and existing.note)):
            return  # a late call may fill in a missing note, never overwrite one
        note = AnswerNote(question_id=question_id, note=text, follow_ups=follow_ups)
        self.notes[question_id] = note
        if text:
            self._emit({"type": "note", "note": note.model_dump()})

    def _max_follow_ups(self, q) -> int:
        return q.max_follow_ups if q.max_follow_ups is not None else self.settings.max_follow_ups_per_question

    async def _follow_up(self, args: dict) -> dict:
        """The agent asks before each follow-up; past the question's limit the answer is to move on. This is how the
        limit is enforced: the agent's own speech isn't in the live transcript, so the app can't count it as it happens."""
        async with self._lock:
            questions = self.context.questions
            current = questions[self.q_index] if self.consented and 0 <= self.q_index < len(questions) else None
            if self._ended or current is None:
                return {"status": "error", "message": "No question is open. Call next_question."}
            limit, asked = self._max_follow_ups(current), self._follow_up_counts.get(current.id, 0)
            if asked >= limit:
                logger.info("[%s] follow_up refused on %s: %d of %d asked", self.session_id, current.id, asked, limit)
                return {"status": "limit_reached", "instruction": (
                    f"You have asked the {limit} follow-up{'s' if limit != 1 else ''} allowed on this question. Don't ask "
                    f"another. Call next_question now with previous_question_id \"{current.id}\", an answer_note and "
                    f"follow_ups_asked {asked}, then ask the question it returns.")}
            self._follow_up_counts[current.id] = asked + 1
            return {"status": "ok", "follow_ups_left": limit - asked - 1}

    def _alert(self, kind: str, message: str) -> None:
        """For the hiring team: on the live session page now, and as a flag on the interview afterwards."""
        self._error(message)
        self._record("System", "system", message)
        self._emit({"type": "alert", "kind": kind, "message": message})

    def _told_candidate(self, what: str) -> str:
        """The end of an alert: whether the candidate could be told, which depends on the platform having a chat (R-12)."""
        if self.bot.platform.chat_notice:
            return f" The candidate was told {what} in the meeting chat."
        return f" There's no meeting chat on {self.bot.platform.name}, so the candidate couldn't be told."

    async def _watch_agent_start(self) -> None:
        platform = self.bot.platform
        if platform.waits_for_recording_permission and not self.bot.recording_allowed.is_set():
            # Zoom: the agent can't hear or speak until the host allows recording, so its start is timed from then (R-13).
            asked = self._clock()
            allowed = asyncio.ensure_future(self.bot.recording_allowed.wait())
            denied = asyncio.ensure_future(self.bot.recording_denied.wait())
            try:
                await asyncio.wait({allowed, denied}, timeout=self._start_give_up_s, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in (allowed, denied):
                    waiter.cancel()
            if not self.bot.recording_allowed.is_set():
                if self._agent_active or self._ended or self._end_task is not None:
                    return
                why = ("refused to let the bot record" if self.bot.recording_denied.is_set()
                       else f"didn't allow recording within {self._start_give_up_s:.0f} s of the bot joining")
                self._alert("recording_not_allowed", f"The host {why} on {platform.name}, so the AI interviewer couldn't start "
                            "and the interview was ended. Nothing the candidate said is kept: they hadn't consented yet.")
                await self.end("agent_failed")
                return
            self._record("System", "system", f"The host allowed recording {self._clock() - asked:.0f} s after the bot joined; "
                         "the AI interviewer's start is timed from now.")
        await asyncio.sleep(self._start_alert_s)
        if self._agent_active or self._ended or self._end_task is not None:
            return
        self._alert("agent_silent_start", f"The AI interviewer hasn't started the conversation {self._start_alert_s:.0f} s after "
                    "joining (no tool call, and MeetStream reported no error)." + self._told_candidate("to stay in the meeting"))
        await self._tell_candidate(AGENT_SLOW_START_NOTICE)
        await asyncio.sleep(max(0.0, self._start_give_up_s - self._start_alert_s))
        if self._agent_active or self._ended or self._end_task is not None:
            return
        self._alert("agent_silent_start", f"The AI interviewer still hadn't started {self._start_give_up_s:.0f} s after joining, "
                    "so the interview was ended. Nothing the candidate said is kept: they hadn't consented yet.")
        await self._tell_candidate(AGENT_NO_START_NOTICE)
        await self.end("agent_failed")

    def _watch_question(self, index: int) -> None:
        if self._stall_watch is not None:
            self._stall_watch.cancel()
        share = self.context.duration_minutes * 60 / max(1, len(self.context.questions))
        self._stall_watch = asyncio.create_task(self._stall_after(index, max(self._question_stall_min_s, 1.5 * share)))

    async def _stall_after(self, index: int, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self._ended or self._end_task is not None or self.q_index != index:
            return
        self._alert("question_stalled", f"Question {index + 1} has been open for {seconds / 60:.0f} minutes and the AI hasn't moved "
                    "on. It may be repeating itself or cutting off." + self._told_candidate("they can ask it to move on"))
        await self._tell_candidate(QUESTION_STALL_NOTICE)

    async def _end_if_stop_ignored(self) -> None:
        await asyncio.sleep(self._stop_grace_s)
        if self._ended or self._end_task is not None:
            return  # the agent ended it, as it should
        self._alert("candidate_asked_to_stop", "The candidate asked to stop and the AI didn't end the interview, so the app ended it.")
        await self._tell_candidate(STOPPING_NOTICE)
        await self.end("candidate_stopped")

    def _end_interview(self, args: dict) -> dict:
        reason = str(args.get("reason") or "completed").strip()[:80] or "completed"
        if self._end_task is None and not self._ended:
            logger.info("[%s] agent called end_interview (%s)", self.session_id, reason)
            if self.inline_plan:
                self._inline_report(args)
            self._end_task = asyncio.create_task(self._end_after_goodbye(reason))
        return {"status": "ok", "instruction": "Say a brief goodbye if you haven't already. The call will end shortly."}

    def _inline_report(self, args: dict) -> None:
        """Inline plan: take consent, progress and notes from the agent's end_interview call."""
        if args.get("candidate_consented") is not True:
            return
        self.consented = True
        self._record("System", "system", "The agent reported that the candidate agreed to the AI interview, recording and transcription.")
        self._emit({"type": "consent"})
        questions = self.context.questions
        last = args.get("last_question_number")
        last = last if isinstance(last, int) and not isinstance(last, bool) else 0
        asked = {q.id for q in questions[: max(0, min(last, len(questions)))]}
        plan_ids = {q.id for q in questions}
        for line in str(args.get("answer_notes") or "").splitlines():
            m = NOTE_LINE.match(line)
            if m and m["qid"] in plan_ids:
                self._note(m["qid"], {"answer_note": m["note"][:600], "follow_ups_asked": int(m["n"]) if m["n"] else 0})
                asked.add(m["qid"])  # a note means it was asked, whatever last_question_number said
        self.asked = [q.id for q in questions if q.id in asked]
        # Same meaning as after next_question: past the end once every question was asked.
        self.q_index = len(questions) if len(self.asked) == len(questions) else len(self.asked) - 1

    async def _end_at_time_limit(self) -> None:
        await asyncio.sleep(self.context.duration_minutes * 60 + INLINE_OVERRUN_SECONDS)
        if self._end_task is None and not self._ended:
            logger.warning("[%s] inline plan ran past its time limit; ending", self.session_id)
            self.time_up = True
            await self.end("time_limit")

    async def _end_if_agent_does_not(self) -> None:
        await asyncio.sleep(self._end_after_done_s)
        if self._end_task is None and not self._ended:
            logger.warning("[%s] agent didn't call end_interview %.0fs after the plan finished; ending", self.session_id, self._end_after_done_s)
            await self.end("time_limit" if self.time_up else "completed")

    async def _end_after_goodbye(self, reason: str) -> None:
        await asyncio.sleep(self._goodbye_grace_s)
        if not self.consented:
            reason = "consent_declined"
        elif self.time_up:
            reason = "time_limit"
        elif reason == "completed" and self.q_index < len(self.context.questions):
            reason = "agent_ended_early"
        await self.end(reason)

    # ---------- meeting input ----------

    async def _on_line(self, speaker: str, text: str, is_bot: bool) -> None:
        if self._ended:
            return
        if self._end_task is not None:
            # The agent has ended the interview and is saying goodbye; nothing after this is an answer.
            logger.info("[%s] ignored a transcript line after end_interview (%d words)", self.session_id, len(text.split()))
            return
        self._agent_seen = self._clock()
        if is_bot:
            self._agent_active = True
            self._record(f"{speaker} (spoken)", "interviewer", text, question_id=self._current_qid())
            return
        if self.analyst:
            # Two people talk; the candidate is whoever matches their name in the meeting.
            self._record(speaker, "candidate" if self._is_candidate(speaker) else "interviewer", text)
            return
        if self._is_echo(text):
            logger.info("[%s] dropped a likely echo of interviewer speech (%d words)", self.session_id, len(text.split()))
            return
        self._record(speaker, "candidate", text, question_id=self._current_qid())
        if self._stop_watch is None and len(text.split()) <= STOP_REQUEST_MAX_WORDS and STOP_REQUEST.search(text):
            logger.info("[%s] the candidate asked to stop; waiting %.0fs for the agent to end the interview", self.session_id, self._stop_grace_s)
            self._stop_watch = asyncio.create_task(self._end_if_stop_ignored())

    async def _on_agent_error(self, message: str) -> None:
        if self._ended:
            return
        short = " ".join(message.split())[:400]
        if "capacity" in message.lower() or "overloaded" in message.lower():
            self._error(
                "The voice agent's AI model is overloaded and may go quiet. If it stays silent, ask the candidate "
                f"to say \"could you repeat the question?\", or end and relaunch later. Provider message: {short}"
            )
        else:
            self._error(f"The voice agent reported an error: {short}")
        if "recoverable=false" in message.lower() and not self.analyst and self._end_task is None and self._agent_watch is None:
            self._agent_seen = self._clock()
            self._agent_watch = asyncio.create_task(self._end_if_agent_stays_down())
            await self._tell_candidate(AGENT_TROUBLE_NOTICE)

    async def _end_if_agent_stays_down(self) -> None:
        """The agent's speech isn't in the live transcript and it only calls a tool when it moves on, so the candidate
        still talking is the sign the conversation goes on: each line restarts the wait (see _on_line)."""
        while (wait := self._agent_seen + self._agent_recovery_s - self._clock()) > 0:
            await asyncio.sleep(wait)
        if self._ended or self._end_task is not None:
            return
        self._agent_giving_up = True  # from here a late tool call must not cancel this task: it runs the shutdown
        self._error("The voice agent didn't recover, so the interview was ended.")
        await self._tell_candidate(AGENT_FAILED_NOTICE)
        await self.end("agent_failed")

    async def _on_participant_event(self, payload: dict) -> None:
        event = live_event(payload, utcnow())
        if event is None:
            return
        self.proctoring_events.append(event)
        self._record("System", "system", event.text)
        if event.kind == "screen_share_stopped" and self.setup_done and not self._ended:
            self._share_stopped_pending = True  # passed to the AI with its next question

    def _proctoring(self, kind: str, source: str, text: str) -> None:
        self.proctoring_events.append(ProctoringEvent(at=utcnow(), kind=kind, source=source, text=text))
        self._record("System", "system", f"Proctoring: {text}")

    async def _on_meeting_ended(self, reason: str) -> None:
        logger.info("[%s] meeting ended by the platform: %s", self.session_id, reason)
        await self.end("meeting_ended")

    # ---------- closing ----------

    async def _finish(self, reason: str) -> None:
        if self._ended:
            return
        self._ended = True
        self.end_reason = reason
        for watch in (self._done_watch, self._time_watch, self._agent_watch, self._start_watch, self._stall_watch, self._stop_watch):
            if watch is not None and watch is not asyncio.current_task():
                watch.cancel()
        joined = self.state not in ("created", "joining", "failed")
        if self.state == "failed":
            return
        self._set_state("leaving")
        await self.bot.leave()
        await self._delete_agent_config()

        if not joined:
            self._fail(f"Ended before the bot joined the meeting ({reason})", already_ended=True)
            return

        evaluation, error = None, None
        transcript_source = "live"
        recorded = None
        if not self.consented:
            # Data minimisation: keep what the interviewer said, drop the candidate's words and the recording.
            self.transcript = [u for u in self.transcript if u.role != "candidate"]
            self._emit({"type": "candidate_lines_removed"})  # the stored live lines go too
            error = ("Not evaluated: the voice agent failed before the candidate consented." if reason == "agent_failed"
                     else "Not evaluated: the candidate did not consent to the AI interview.")
            if self.bot.bot_id:
                outcome = await erase_bot_data(self._client, self.bot.bot_id)
                self._emit({"type": "erased", "bot_id": self.bot.bot_id, "outcome": outcome})
        else:
            qid = self._current_qid()
            if qid and qid not in self.notes and self._follow_up_counts.get(qid):
                self._note(qid, {"follow_ups_asked": self._follow_up_counts[qid]})  # the open question's counted follow-ups
            # Analyst mode keeps the live transcript: it names each person, and there is no AI voice to recover.
            if self.context.proctored:
                recovered, recorded = await asyncio.gather(self._recording_transcript(), self._recorded_screen_shares())
            else:
                recovered, recorded = (None if self.analyst else await self._recording_transcript()), None
            if recovered:
                self.transcript, transcript_source = recovered, "recording"
                self._count_open_follow_ups()
            self._set_state("evaluating")
            evaluation, error = await evaluate_interview(
                self.ai,
                EvaluationInput(self.context, list(self.transcript), list(self.notes.values()), list(self.asked)),
            )
            if error:
                self._error(f"Evaluation failed: {error}")

        ended_at = utcnow()
        self.report = EvaluationReport(
            session_id=self.session_id,
            interview_id=self.context.interview_id,
            role_title=self.context.role_title,
            candidate_name=self.context.candidate_name,
            platform=self.bot.platform.name,
            started_at=self.started_at,
            ended_at=ended_at,
            duration_seconds=int((ended_at - self.started_at).total_seconds()),
            questions_planned=len(self.context.questions),
            questions_asked=len(self.asked),
            follow_ups_asked=self.follow_ups,
            end_reason=reason,
            consent_given=self.consented,
            generated_by=(f"{self.ai.served_name} (evaluation); Meeting Analyst (silent bot)" if self.analyst
                          else f"{self.ai.served_name} (evaluation); MIA {getattr(self, 'voice_label', self.settings.mia_label)} (voice)"),
            evaluation=evaluation,
            evaluation_error=error,
            notes=list(self.notes.values()),
            transcript=list(self.transcript),
            transcript_source=transcript_source,
            mode=self.context.mode,
            proctoring=self._proctoring_record(recorded if self.consented else None) if self.context.proctored else None,
        )
        try:
            self._sink.push_report(self.report)
        except Exception as e:  # never lose the in-memory report to a sink failure
            logger.exception("[%s] saving the report failed", self.session_id)
            self._error(f"Could not save the report: {e}")
        self._emit({"type": "report", "report": self.report.model_dump(mode="json")})
        self._set_state("completed")

    def _count_open_follow_ups(self) -> None:
        """The agent reports a question's follow-ups when it moves on, so a call that stops mid-question never reports
        that count. The recording has the agent's voice: after the question itself, each line that asks something counts."""
        qid = self._current_qid()
        if qid is None or qid in self.notes:
            return
        if self._follow_up_counts.get(qid):  # the agent asked before each one: that count, not a guess from the recording
            self._note(qid, {"follow_ups_asked": self._follow_up_counts[qid]})
            return
        lines = [u.text for u in self.transcript if u.role == "interviewer" and u.question_id == qid]
        asked: list[str] = []  # a question said again (the candidate asked to hear it) isn't a follow-up
        for text in lines:
            key = squash(text)
            if ("?" in text or not asked) and not any(SequenceMatcher(None, key, seen).ratio() >= SAME_QUESTION for seen in asked):
                asked.append(key)
        count = len(asked) - 1
        if count > 0:
            self._note(qid, {"follow_ups_asked": min(count, 10)})

    async def _recording_transcript(self) -> Optional[list[Utterance]]:
        """Transcribe the call recording and rebuild the transcript with the agent's spoken lines.

        The live transcript has only human speech; the recording has both voices but no usable
        speaker labels on Meet, so speakers are inferred (app/postcall.py). Any failure falls back
        to the live transcript.
        """
        bot_id = self.bot.bot_id
        if not (self.settings.postcall_transcript and bot_id):
            return None
        self._set_state("transcribing")
        budget = float(self.settings.postcall_timeout_seconds)
        started = self._clock()
        try:
            # The recording is processed after the bot leaves; get_audio answers 202 until it's ready.
            await asyncio.wait_for(self._client.bots.wait_for_audio(bot_id, timeout=budget, interval=10), budget)
            job = await self._client.transcripts.transcribe_bot_audio(
                bot_id, {"provider": {"meetstream": {"language": self.settings.mia_language}}}
            )
            transcript_id = job.get("transcript_id") if isinstance(job, dict) else None
            if not transcript_id:
                raise meetstream.MeetStreamError(f"no transcript_id in {job!r}")
            remaining = max(10.0, budget - (self._clock() - started))
            post = await asyncio.wait_for(
                self._client.transcripts.wait_for(transcript_id, timeout=remaining, interval=10), remaining + 5
            )
        except (meetstream.MeetStreamError, asyncio.TimeoutError) as e:
            self._error(
                f"Couldn't transcribe the recording ({e or 'timed out'}). The report uses the live transcript, "
                "which doesn't include the AI's follow-up questions."
            )
            return None
        merged = merge_postcall(
            post, self.transcript, agent_name=self.settings.bot_name,
            candidate_name=self.context.candidate_name, started_at=self.started_at,
        )
        if not merged:
            self._error("The recording's transcript didn't line up with the live transcript; the report uses the live transcript.")
            return None
        logger.info("[%s] transcript rebuilt from the recording: %d lines", self.session_id, len(merged))
        return merged

    async def _recorded_screen_shares(self) -> Optional[tuple]:
        """MeetStream's per-participant recording, read for screen-share segments. None, with a reason, if unavailable."""
        bot_id = self.bot.bot_id
        if not bot_id:
            return None
        budget = float(self.settings.postcall_timeout_seconds)
        deadline = self._clock() + budget
        while True:
            try:
                streams = await self._client.bots.recording_streams(bot_id)
                break
            except (meetstream.NotReadyError, meetstream.NotFoundError):
                # Live 2026-11-02: until video processing starts, MeetStream answers 404
                # "No video processing has been initiated for this bot", not 202; it was ready a few minutes later.
                if self._clock() + STREAMS_POLL_SECONDS > deadline:
                    return ("unavailable", f"MeetStream's per-participant recording wasn't ready within {int(budget)} s.")
                await asyncio.sleep(STREAMS_POLL_SECONDS)
            except meetstream.MeetStreamError as e:
                return ("unavailable", f"MeetStream's per-participant recording couldn't be read: {e}")
        logger.info("[%s] recording streams read for proctoring", self.session_id)
        return ("ok", recording_events(streams, self.started_at, self._is_candidate,
                                       lambda name: name.strip().lower() == self.settings.bot_name.strip().lower()))

    def _proctoring_record(self, recorded: Optional[tuple]) -> ProctoringRecord:
        events = list(self.proctoring_events)
        unverified = ["The shared screen's content is not analysed, and screen sharing can't show whether another device, "
                      "monitor or person was involved.",
                      "Other people in the call are only noticed if their camera was on (MeetStream's recording has a stream "
                      "per camera); someone with the camera off, or off-screen, isn't."]
        share_seconds = interruptions = None
        if not self.consented:
            unverified.append("The candidate didn't agree, so nothing was recorded for proctoring.")
        elif recorded is None:
            unverified.append("MeetStream's per-participant recording wasn't read.")
        elif recorded[0] == "unavailable":
            unverified.append(recorded[1])
        else:
            found, share_seconds, interruptions, notes = recorded[1]
            events += found
            unverified += notes
        if self.context.require_camera and not any(e.kind.startswith("camera_") for e in events):
            unverified.append("MeetStream doesn't report whether a camera is on, so the app can't confirm the camera stayed on. "
                              "The video recording shows it.")
        return ProctoringRecord(
            require_camera=self.context.require_camera, require_screen_share=self.context.require_screen_share,
            consent_given=self.consented, events=ordered(events), screen_share_seconds=share_seconds,
            interruptions=interruptions, unverified=unverified,
        )

    async def _delete_agent_config(self) -> None:
        config_id, self.agent_config_id = self.agent_config_id, None
        if not config_id:
            return
        try:
            await self._client.mia.delete(config_id)
        except meetstream.MeetStreamError as e:
            logger.warning("[%s] could not delete MIA agent config %s: %s", self.session_id, config_id, e)

    # ---------- helpers ----------

    def _is_candidate(self, speaker: str) -> bool:
        def norm(text: str) -> list[str]:
            return re.sub(r"[^\w\s]", " ", text.lower()).split()

        target = norm(self.context.candidate_meeting_name or self.context.candidate_name)
        said = norm(speaker)
        if not target or not said:
            return False
        return said == target or all(t in said for t in target) or said[0] == target[0]

    def _current_qid(self) -> Optional[str]:
        qs = self.context.questions
        return qs[self.q_index].id if 0 <= self.q_index < len(qs) else None

    def _is_echo(self, text: str) -> bool:
        if " ".join(re.findall(r"[a-z']+", text.lower())) in ACK_LINES:
            return True
        words = _content_words(text)
        if len(words) < ECHO_MIN_WORDS:
            return False
        replies = REPLY_WORDS.intersection(re.findall(r"[a-z']+", text.lower()))
        for u in self.transcript[-ECHO_LOOKBACK:]:
            if u.role == "interviewer":
                if replies - set(re.findall(r"[a-z']+", u.text.lower())):
                    continue  # it answers this line rather than repeating it
                spoken = set(_content_words(u.text))
                if sum(w in spoken for w in words) / len(words) >= ECHO_OVERLAP:
                    return True
        return False

    def _record(self, speaker: str, role: str, text: str, question_id: Optional[str] = None) -> None:
        u = Utterance(speaker=speaker, role=role, text=text, question_id=question_id)
        self.transcript.append(u)
        self._emit({"type": "utterance", "utterance": u.model_dump(mode="json")})

    def _set_state(self, state: str) -> None:
        self.state = state
        self._emit({"type": "state", **self.snapshot()})

    def _error(self, message: str) -> None:
        logger.error("[%s] %s", self.session_id, message)
        self._emit({"type": "error", "message": message})

    def _fail(self, message: str, already_ended: bool = False) -> None:
        self._error(message)
        if not already_ended:
            self._ended = True
            self.end_reason = "failed"
        self._set_state("failed")
