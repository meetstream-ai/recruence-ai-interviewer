"""The live interview runner against a fake MeetStream: consent, plan progression, transcript, ending."""

import asyncio
from dataclasses import replace

import meetstream
import pytest

from app.interview.agent import agent_config, first_message
from app.interview.runner import InterviewRunner
from app.meeting.meetstream import MeetStreamBot
from app.meeting.platforms import GOOGLE_MEET, ZOOM
from app.models import AnswerNote, Utterance
from app.planning import context_for
from tests.conftest import settle
from tests.fakes import MEET_LINK, FakeAI, FakeMeetStream, event, sentence


class Sink:
    def __init__(self):
        self.reports = []

    def push_report(self, report):
        self.reports.append(report)
        return "sink"


def build(settings, interview, client=None, ai=None, clock=None, platform=GOOGLE_MEET):
    client = client or FakeMeetStream()
    events = []
    bot = MeetStreamBot(client, meeting_link=MEET_LINK, platform=platform, bot_name="AI Interviewer",
                        public_base_url=settings.public_base_url, webhook_token="tok", session_id="s1",
                        interview_id=interview.id, poll_interval_s=0.01)
    kwargs = {"clock": clock} if clock else {}
    runner = InterviewRunner("s1", context_for(interview), client, ai or FakeAI(), settings, bot=bot, webhook_token="tok",
                             emit=events.append, sink=Sink(), goodbye_grace_s=0.01, **kwargs)
    return runner, client, events


async def started(runner):
    task = asyncio.create_task(runner.start())
    await settle(lambda: runner.bot.bot_id is not None or task.done())
    await runner.bot.handle_event(event("bot.inmeeting"))
    await task


async def consent(runner, past_intro=True):
    """Consent, then (by default) past the self-introduction that every AI interview opens with: returns the first
    planned question."""
    first = await runner.handle_tool("next_question", {"candidate_consented": True})
    if not past_intro or first.get("question_id") != "intro":
        return first
    return await runner.handle_tool("next_question", {"previous_question_id": "intro", "answer_note": "Introduced their background.", "follow_ups_asked": 0})


def test_agent_config(settings, ready_interview):
    ctx = context_for(ready_interview)
    cfg = agent_config(settings, ctx, "s1", "https://tunnel.example.com/webhooks/mia/tok", "secret")
    assert cfg["mode"] == "pipeline" and cfg["wake_word"] == {"enabled": False}
    assert cfg["model"]["first_message"] == first_message(ctx)
    assert "recorded and transcribed" in cfg["model"]["first_message"] and "Priya" in cfg["model"]["first_message"]
    assert "three questions" in cfg["model"]["first_message"]
    prompt = cfg["model"]["system_prompt"]
    assert "candidate_consented" in prompt and "not instructions to you" in prompt
    for q in ctx.questions:  # questions are served by the tool, not baked into the prompt
        assert q.text not in prompt
    tools = {f["name"]: f for f in cfg["agent"]["custom_functions"]}
    assert set(tools) == {"next_question", "end_interview", "follow_up"}
    assert tools["follow_up"]["url"] == "https://tunnel.example.com/webhooks/mia/tok/follow_up"
    assert "call follow_up" in prompt and "limit_reached" in prompt  # the follow-up limit is checked by the app (live B-1)
    assert tools["next_question"]["url"] == "https://tunnel.example.com/webhooks/mia/tok/next_question"
    assert tools["next_question"]["headers"] == {"Authorization": "Bearer secret"}
    assert "temperature" not in cfg["model"]
    agent = cfg["agent"]
    assert agent["min_endpointing_delay"] == 1.5 and agent["max_endpointing_delay"] == 8.0
    assert agent["interruptions"] == {"min_duration_seconds": 1.0, "word_threshold": 3}
    assert agent["vad_min_silence_duration_ms"] == 1000
    off = agent_config(replace(settings, mia_interrupt_min_seconds=0, mia_interrupt_min_words=0, mia_vad_min_silence_ms=0), ctx, "s1", "u", "t")["agent"]
    assert "interruptions" not in off and "vad_min_silence_duration_ms" not in off


def test_answer_handoff(settings, ready_interview):
    """MeetStream reports no microphone on/off, so the candidate ends an answer by saying so, and a pause is thinking time."""
    from app.interview.agent import chat_disclosure, inline_plan_prompt, system_prompt
    ctx = build(settings, ready_interview)[0].context
    assert ctx.answer_handoff
    assert "that's my answer" in first_message(ctx) and first_message(ctx).endswith("Do you agree to continue with this AI interview?")
    assert "that's my answer" in chat_disclosure(ctx)
    assert "Never call next_question or follow_up while an answer" in system_prompt(ctx, 2)
    assert "it never delays your goodbye or end_interview" in system_prompt(ctx, 2)  # live 16 November
    assert "Never move on or ask a follow-up while an answer" in inline_plan_prompt(ctx, 2)
    for prompt in (system_prompt(ctx, 2), inline_plan_prompt(ctx, 2)):
        assert "A pause on its own is thinking time" in prompt

    off = build(replace(settings, mia_answer_handoff=False), ready_interview)[0].context
    assert not off.answer_handoff
    assert "that's my answer" not in first_message(off) and "that's my answer" not in chat_disclosure(off)
    assert "thinking time" not in system_prompt(off, 2)


def test_realtime_agent_config(settings, ready_interview):
    from dataclasses import replace
    ctx = context_for(ready_interview)
    s = replace(settings, mia_mode="realtime", mia_realtime_provider="google", mia_realtime_voice="Kore")
    cfg = agent_config(s, ctx, "s1", "https://tunnel.example.com/webhooks/mia/tok", "secret")
    assert cfg["mode"] == "realtime"
    assert set(cfg) == {"agent_name", "mode", "model", "agent"}  # no pipeline-only voice/transcriber/wake_word
    assert cfg["model"]["provider"] == "google" and cfg["model"]["voice"] == "Kore" and "model" not in cfg["model"]
    assert "first_message" not in cfg["model"]
    assert first_message(ctx) in cfg["model"]["system_prompt"]  # the opening is in the instructions instead
    assert [f["name"] for f in cfg["agent"]["custom_functions"]] == ["next_question", "end_interview", "follow_up"]
    assert s.mia_label == "google realtime"


async def test_start_reaches_awaiting_consent(settings, ready_interview):
    runner, client, events = build(settings, ready_interview)
    await started(runner)
    assert runner.state == "awaiting_consent"
    assert client.bots.created[0]["params"]["agent_config_id"] == "cfg-1"
    assert runner.transcript[0].text == first_message(runner.context)
    assert {"type": "ids", "bot_id": "bot-123"} in events


async def test_disclosure_posted_in_chat_on_join(settings, ready_interview):
    runner, client, _ = build(settings, ready_interview)
    await started(runner)
    [(bot_id, text)] = client.bots.messages
    assert bot_id == "bot-123" and "AI interviewer" in text and "recorded and transcribed" in text
    assert runner.transcript[-1].role == "system" and "meeting chat" in runner.transcript[-1].text


async def test_no_chat_notice_on_zoom(settings, ready_interview):
    """Live 2026-10-31 (call 13): one send_message showed up as the notice repeated over and over in Zoom's chat."""
    runner, client, _ = build(settings, ready_interview, platform=ZOOM)
    await started(runner)
    assert client.bots.messages == [] and runner.state == "awaiting_consent"
    # Nothing is posted; the only mention is the note that questions (on by default) aren't shown in Zoom's chat.
    assert [u.text for u in runner.transcript if "meeting chat" in u.text] == ["Questions aren't posted in the meeting chat on Zoom."]


async def test_chat_failure_does_not_stop_the_interview(settings, ready_interview):
    client = FakeMeetStream(send_error=meetstream.ServerError("chat down", status=500))
    runner, _, _ = build(settings, ready_interview, client=client)
    await started(runner)
    assert runner.state == "awaiting_consent" and client.bots.messages == []


def test_prompt_repeats_unheard_opening(settings, ready_interview):
    ctx = context_for(ready_interview)
    prompt = agent_config(settings, ctx, "s1", "u", "t")["model"]["system_prompt"]
    assert first_message(ctx) in prompt and "didn't hear it" in prompt


def test_prompt_moves_on_when_asked_and_ends_after_goodbye(settings, ready_interview):
    """Live call 17: "Can we move on?" got an odd reply instead of the next question, and end_interview never came."""
    ctx = context_for(ready_interview)
    for inline in (False, True):
        s = replace(settings, mia_mode="realtime" if inline else "pipeline", mia_realtime_inline_plan=inline)
        cfg = agent_config(s, ctx, "s1", "u", "t")
        prompt = (cfg.get("model") or cfg.get("realtime") or {}).get("system_prompt") or str(cfg)
        assert "asks to move on or skip a follow-up" in prompt
        assert "right after the goodbye" in prompt


async def test_no_question_before_consent(settings, ready_interview):
    runner, _, _ = build(settings, ready_interview)
    await started(runner)
    for args in ({}, {"candidate_consented": False}, {"candidate_consented": "yes"}):
        res = await runner.handle_tool("next_question", args)
        assert res["status"] == "consent_required" and "question" not in res
    assert runner.asked == [] and not runner.consented
    opening = await consent(runner, past_intro=False)
    assert opening["status"] == "question" and opening["question_id"] == "intro"  # always the self-introduction first
    first = await runner.handle_tool("next_question", {"previous_question_id": "intro", "answer_note": "n"})
    assert first["status"] == "question" and first["question_id"] == "q1"
    assert runner.state == "questioning" and runner.consented
    assert any(u.role == "system" and "agreed" in u.text for u in runner.transcript)


async def test_full_interview(settings, ready_interview):
    ai = FakeAI(evidence=["led a migration to PostgreSQL", "invented quote"], score=9)
    runner, client, events = build(settings, ready_interview, ai=ai)
    await started(runner)
    qs = runner.context.questions

    assert qs[0].id == "intro" and (await consent(runner))["question"] == qs[1].text
    assert (await runner.handle_tool("next_question", {}))["question_id"] == "q1"  # repeat: no skip
    await runner.bot.handle_transcript(sentence("I led a migration to PostgreSQL with zero downtime."))
    assert runner.transcript[-1].role == "candidate" and runner.transcript[-1].question_id == "q1"
    assert (await runner.handle_tool("next_question", {"previous_question_id": "q7"}))["question_id"] == "q1"  # stale id

    second = await runner.handle_tool("next_question", {"previous_question_id": "q1", "answer_note": "Concrete migration.", "follow_ups_asked": 1})
    assert second["question_id"] == "q2" and second["max_follow_ups"] == settings.max_follow_ups_per_question
    await runner.handle_tool("next_question", {"previous_question_id": "q2", "answer_note": "n2"})
    done = await runner.handle_tool("next_question", {"previous_question_id": "q3", "answer_note": "n3"})
    assert done["status"] == "done" and "without waiting for the candidate" in done["instruction"]  # live 16 November

    assert (await runner.handle_tool("end_interview", {"reason": "completed"}))["status"] == "ok"
    await settle(lambda: runner.state == "completed")
    r = runner.report
    assert r.end_reason == "completed" and r.consent_given
    assert r.questions_asked == 4 and r.follow_ups_asked == 1  # the introduction and the three planned questions
    assert [s.score for s in r.evaluation.competency_scores] == [5, 5]  # clamped
    assert r.evaluation.competency_scores[0].evidence == ["led a migration to PostgreSQL"]  # invented quote dropped
    assert r.evaluation.competency_scores[0].competency_name == "System design"
    assert client.bots.removed == ["bot-123"] and client.mia.deleted == ["cfg-1"]
    assert runner._sink.reports == [r]
    assert [u.text for u in r.transcript if u.role == "interviewer"][1:] == [q.text for q in qs]
    assert [e["type"] for e in events].count("report") == 1


async def test_two_calls_per_transition_keep_the_note(settings, ready_interview):
    runner, _, events = build(settings, ready_interview)
    await started(runner)
    await consent(runner)
    a = await runner.handle_tool("next_question", {"previous_question_id": "q1"})
    b = await runner.handle_tool("next_question", {"previous_question_id": "q1", "answer_note": "Late note.", "follow_ups_asked": 2})
    assert a["question_id"] == b["question_id"] == "q2"
    assert runner.notes["q1"].note == "Late note." and runner.follow_ups == 2
    c = await runner.handle_tool("next_question", {"previous_question_id": "q1", "answer_note": "Overwrite attempt."})
    assert c["question_id"] == "q2" and runner.notes["q1"].note == "Late note."
    assert [e["note"]["note"] for e in events if e["type"] == "note"] == ["Introduced their background.", "Late note."]


async def test_consent_declined_keeps_nothing_of_the_candidate(settings, ready_interview):
    ai = FakeAI()
    runner, client, events = build(settings, ready_interview, ai=ai)
    await started(runner)
    await runner.bot.handle_transcript(sentence("No, I would prefer a human interviewer."))
    await runner.handle_tool("end_interview", {"reason": "consent_declined"})
    await settle(lambda: runner.state == "completed")
    r = runner.report
    assert r.end_reason == "consent_declined" and not r.consent_given
    assert r.evaluation is None and "did not consent" in r.evaluation_error
    assert all(u.role != "candidate" for u in r.transcript)
    assert ai.evaluations == []
    assert client.bots.erased == ["bot-123"]
    assert any(e["type"] == "erased" for e in events)


async def test_app_ends_when_agent_forgets_end_interview(settings, ready_interview):
    """Live 2026-10-29 (grok-4.20): "done" was returned but the agent never called end_interview."""
    runner, client, _ = build(settings, ready_interview)
    runner._end_after_done_s = 0.05
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence("An answer about outages."))
    for prev in ("q1", "q2", "q3"):
        res = await runner.handle_tool("next_question", {"previous_question_id": prev, "answer_note": "n"})
    assert res["status"] == "done"
    await settle(lambda: runner.state == "completed")
    assert runner.report.end_reason == "completed" and client.bots.removed == ["bot-123"]


async def test_done_watch_is_cancelled_when_agent_ends(settings, ready_interview):
    runner, _, _ = build(settings, ready_interview)
    await started(runner)
    await consent(runner)
    for prev in ("q1", "q2", "q3"):
        await runner.handle_tool("next_question", {"previous_question_id": prev, "answer_note": "n"})
    await runner.handle_tool("end_interview", {"reason": "completed"})
    await settle(lambda: runner.state == "completed")
    assert runner._done_watch.cancelled() or runner._done_watch.done()


async def test_time_limit_ends_the_plan(settings, ready_interview):
    now = [0.0]
    runner, _, _ = build(settings, ready_interview, clock=lambda: now[0])
    await started(runner)
    await consent(runner)
    now[0] = 21 * 60  # plan is 20 minutes
    res = await runner.handle_tool("next_question", {"previous_question_id": "q1", "answer_note": "n"})
    assert res["status"] == "done"
    await runner.handle_tool("end_interview", {})
    await settle(lambda: runner.state == "completed")
    assert runner.report.end_reason == "time_limit" and runner.report.questions_asked == 2  # the introduction and q1


async def test_meeting_ended_by_host(settings, ready_interview):
    runner, client, _ = build(settings, ready_interview)
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence("My answer about outages and on-call."))
    await runner.bot.handle_event(event("bot.stopped", status="Stopped", message="Meeting ended"))
    await settle(lambda: runner.state == "completed")
    assert runner.report.end_reason == "meeting_ended"
    assert client.bots.removed == [] and client.mia.deleted == ["cfg-1"]


async def test_recruiter_ends_early_still_evaluates(settings, ready_interview):
    ai = FakeAI()
    runner, _, _ = build(settings, ready_interview, ai=ai)
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence("Partial answer."))
    await runner.end()
    await runner.end()  # idempotent
    assert runner.state == "completed" and runner.report.end_reason == "ended_by_recruiter"
    assert len(ai.evaluations) == 1


async def test_evaluation_failure_keeps_report(settings, ready_interview):
    runner, _, events = build(settings, ready_interview, ai=FakeAI(eval_error="model down"))
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence("An answer."))
    await runner.end()
    assert runner.report.evaluation is None and runner.report.evaluation_error == "model down"
    assert any(e["type"] == "error" for e in events)


async def test_agent_config_failure(settings, ready_interview):
    client = FakeMeetStream(mia_create_error=meetstream.BadRequestError("No xai key", status=400))
    runner, _, events = build(settings, ready_interview, client=client)
    await runner.start()
    assert runner.state == "failed" and client.bots.created == []
    assert any("Integrations" in e.get("message", "") for e in events)


async def test_agent_config_500_points_at_the_public_url(settings, ready_interview):
    """Live 2026-11-01: MeetStream answered 500 when the tool URL's tunnel had stopped."""
    client = FakeMeetStream(mia_create_error=meetstream.ServerError("Internal server error.", status=500))
    runner, _, events = build(settings, ready_interview, client=client)
    await runner.start()
    [message] = [e["message"] for e in events if e["type"] == "error"]
    assert runner.state == "failed" and "PUBLIC_BASE_URL" in message and settings.public_base_url in message


async def test_denied_join_cleans_up_agent(settings, ready_interview):
    runner, client, _ = build(settings, ready_interview)
    task = asyncio.create_task(runner.start())
    await settle(lambda: runner.bot.bot_id is not None)
    await runner.bot.handle_event(event("bot.denied", status="Denied"))
    await task
    assert runner.state == "failed" and client.mia.deleted == ["cfg-1"]


async def test_end_while_joining_fails_without_evaluation(settings, ready_interview):
    ai = FakeAI()
    runner, client, _ = build(settings, ready_interview, ai=ai)
    task = asyncio.create_task(runner.start())
    await settle(lambda: runner.bot.bot_id is not None)
    await runner.end()
    await task
    assert runner.state == "failed" and ai.evaluations == [] and client.mia.deleted == ["cfg-1"]


@pytest.mark.parametrize(
    "line, dropped",
    [
        ("I'm an AI interviewer for the Backend Engineer role and this conversation is recorded", True),
        ("Yes, I agree to continue.", False),
        ("We sharded the payments database by merchant and added read replicas.", False),
    ],
)
async def test_echo_of_interviewer_is_dropped(settings, ready_interview, line, dropped):
    runner, _, _ = build(settings, ready_interview)
    await started(runner)
    before = len(runner.transcript)
    await runner.bot.handle_transcript(sentence(line))
    assert (len(runner.transcript) == before) is dropped


@pytest.mark.parametrize(
    "line, dropped",
    [
        # Replies that repeat the consent question's words: kept (seen live: such a consent was dropped as an echo).
        ("Yes, I agree to this interview being recorded and transcribed.", False),
        ("Sure, the interview can be recorded and transcribed.", False),
        ("No, I don't want this interview recorded and transcribed.", False),
        # The question itself, heard back through the candidate's speakers: still an echo.
        ("Do you agree to this interview being recorded and transcribed?", True),
    ],
)
async def test_a_reply_that_repeats_the_question_is_not_an_echo(settings, ready_interview, line, dropped):
    runner, _, _ = build(settings, ready_interview)
    await started(runner)
    runner._record("AI Interviewer (spoken)", "interviewer", "Do you agree to this interview being recorded and transcribed?")
    before = len(runner.transcript)
    await runner.bot.handle_transcript(sentence(line))
    assert (len(runner.transcript) == before) is dropped


@pytest.mark.parametrize("line", ["Thank you.", "Thank you for your time.", "Thanks for explaining that!"])
async def test_short_acknowledgement_echoes_are_dropped(settings, ready_interview, line):
    """Live call 1 (no headphones): the agent's short acknowledgements arrived under the candidate's name."""
    runner, _, _ = build(settings, ready_interview)
    await started(runner)
    await consent(runner)
    before = len(runner.transcript)
    await runner.bot.handle_transcript(sentence(line))
    assert len(runner.transcript) == before
    await runner.bot.handle_transcript(sentence("Yes."))
    assert runner.transcript[-1].text == "Yes."


async def test_lines_after_end_interview_are_ignored(settings, ready_interview):
    runner, _, _ = build(settings, ready_interview)
    runner._goodbye_grace_s = 0.2
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence("A real answer about the outage."))
    await runner.handle_tool("end_interview", {"reason": "completed"})
    await runner.bot.handle_transcript(sentence("the."))
    await settle(lambda: runner.state == "completed")
    assert [u.text for u in runner.report.transcript if u.role == "candidate"] == ["A real answer about the outage."]


async def test_agent_errors_reach_the_dashboard(settings, ready_interview):
    runner, _, events = build(settings, ready_interview)
    await started(runner)
    capacity = "type='llm_error' error=APIStatusError('The model is currently at capacity due to high demand.') recoverable=False"
    await runner.bot.handle_event({**event("agent_error"), "message": capacity})
    await runner.bot.handle_event({**event("bot.error"), "message": "TTS failed"})
    errors = [e["message"] for e in events if e["type"] == "error"]
    assert "overloaded" in errors[0] and "at capacity" in errors[0]
    assert errors[1] == "The voice agent reported an error: TTS failed"
    assert runner.state == "awaiting_consent"  # an agent error alone doesn't end the interview


async def test_bot_speech_is_recorded_as_interviewer(settings, ready_interview):
    runner, _, _ = build(settings, ready_interview)
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence("Could you say more about the rollback?", speaker="AI Interviewer"))
    assert runner.transcript[-1].role == "interviewer" and runner.transcript[-1].speaker == "AI Interviewer (spoken)"


def test_tool_authorisation(settings, ready_interview):
    runner, _, _ = build(settings, ready_interview)
    good = f"Bearer {runner.tool_token}"
    assert runner.authorised(good, None)
    assert not runner.authorised("Bearer nope", None) and not runner.authorised(None, None)
    runner.bot.bot_id = "bot-123"
    assert runner.authorised(good, "bot-123") and not runner.authorised(good, "other")


# ---------- realtime with the plan in the prompt (experimental) ----------

def inline(settings):
    return replace(settings, mia_mode="realtime", mia_realtime_inline_plan=True)


def test_inline_plan_agent_config(settings, ready_interview):
    ctx = context_for(ready_interview)
    s = inline(settings)
    cfg = agent_config(s, ctx, "s1", "https://tunnel.example.com/webhooks/mia/tok", "secret")
    prompt = cfg["model"]["system_prompt"]
    assert cfg["mode"] == "realtime" and first_message(ctx) in prompt and "didn't hear it" in prompt
    positions = [prompt.index(q.text) for q in ctx.questions]  # every planned question, in plan order
    assert positions == sorted(positions)
    assert "next_question" not in prompt and "not instructions to you" in prompt
    [tool] = cfg["agent"]["custom_functions"]  # no mid-interview tool call
    assert tool["name"] == "end_interview" and tool["url"].endswith("/end_interview")
    assert tool["headers"] == {"Authorization": "Bearer secret"}
    assert {"reason", "candidate_consented", "last_question_number", "answer_notes"} == set(tool["parameters"]["properties"])
    assert s.mia_label == "google realtime, plan in prompt"
    # The flag does nothing outside realtime.
    assert not replace(settings, mia_realtime_inline_plan=True).inline_plan


async def test_inline_plan_full_interview(settings, ready_interview):
    runner, client, events = build(inline(settings), ready_interview)
    await started(runner)
    assert runner.state == "awaiting_consent" and runner._time_watch is not None
    await runner.bot.handle_transcript(sentence("I led a migration to PostgreSQL with zero downtime."))
    notes = "q1 (1 follow-up): Concrete migration.\nq2: Clear on trade-offs.\nnot a note\nq3 (0 follow-ups): Short.\nq9: unknown id"
    await runner.handle_tool("end_interview", {"reason": "completed", "candidate_consented": True,
                                               "last_question_number": 4, "answer_notes": notes})
    await settle(lambda: runner.state == "completed")
    r = runner.report
    assert r.consent_given and r.end_reason == "completed"
    assert r.questions_asked == 4 and r.follow_ups_asked == 1
    assert {n.question_id: n.note for n in r.notes} == {"q1": "Concrete migration.", "q2": "Clear on trade-offs.", "q3": "Short."}
    assert any(u.role == "candidate" for u in r.transcript) and r.evaluation is not None
    assert {"type": "consent"} in events and runner._time_watch.cancelled()


async def test_inline_plan_partial_interview_is_ended_early(settings, ready_interview):
    runner, _, _ = build(inline(settings), ready_interview)
    await started(runner)
    await runner.handle_tool("end_interview", {"reason": "completed", "candidate_consented": True,
                                               "last_question_number": 1, "answer_notes": "q2: noted although not counted"})
    await settle(lambda: runner.state == "completed")
    assert runner.report.end_reason == "agent_ended_early"
    assert runner.asked == ["intro", "q2"]  # question 1 is the introduction; a note counts as asked


async def test_inline_plan_without_confirmed_consent_keeps_nothing(settings, ready_interview):
    for args in ({"reason": "consent_declined", "candidate_consented": False}, {"reason": "completed"}, None):
        ai = FakeAI()
        runner, client, _ = build(inline(settings), ready_interview, ai=ai)
        await started(runner)
        await runner.bot.handle_transcript(sentence("Something the candidate said."))
        if args is None:
            await runner.end()  # e.g. the agent stalled and the recruiter ended it
        else:
            await runner.handle_tool("end_interview", {**args, "last_question_number": 2, "answer_notes": "q1: n"})
        await settle(lambda: runner.state == "completed")
        r = runner.report
        assert not r.consent_given and r.evaluation is None and ai.evaluations == []
        assert all(u.role != "candidate" for u in r.transcript) and r.notes == []
        assert client.bots.erased == ["bot-123"]


async def test_inline_plan_hard_time_limit(settings, ready_interview, monkeypatch):
    monkeypatch.setattr("app.interview.runner.INLINE_OVERRUN_SECONDS", 0.05)
    runner, _, _ = build(inline(settings), ready_interview)
    runner.context.duration_minutes = 0
    await started(runner)
    await settle(lambda: runner.state == "completed")
    assert runner.report.end_reason == "time_limit" and runner.time_up


FATAL = "type='llm_error' error=APIStatusError('quota exceeded', status_code=429) recoverable=False"


async def test_a_dead_agent_tells_the_candidate_and_ends_the_interview(settings, ready_interview):
    """Live call 21: the agent died on a quota error and the candidate was left waiting in silence."""
    runner, client, events = build(settings, ready_interview)
    runner._agent_recovery_s = 0.02
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence("A real answer about the outage."))
    await runner.bot.handle_event({**event("agent_error"), "message": FATAL})
    await runner.bot.handle_event({**event("agent_error"), "message": FATAL})  # one notice, however many errors
    await settle(lambda: runner.state == "completed")
    posted = [m for _, m in client.bots.messages]
    assert sum("technical problem" in m for m in posted) == 1 and sum("couldn't recover" in m for m in posted) == 1
    assert runner.report.end_reason == "agent_failed" and runner.report.evaluation is not None
    assert any("didn't recover" in e["message"] for e in events if e["type"] == "error")


async def test_an_agent_that_comes_back_keeps_the_interview_going(settings, ready_interview):
    runner, client, _ = build(settings, ready_interview)
    runner._agent_recovery_s = 0.05
    await started(runner)
    await consent(runner)
    await runner.bot.handle_event({**event("agent_error"), "message": FATAL})
    await runner.handle_tool("next_question", {"previous_question_id": "q1", "answer_note": "n"})
    await asyncio.sleep(0.1)
    assert runner.state == "questioning" and not any("couldn't recover" in m for _, m in client.bots.messages)
    await runner.end()


async def test_a_recoverable_agent_error_does_not_start_the_countdown(settings, ready_interview):
    runner, client, _ = build(settings, ready_interview)
    await started(runner)
    await runner.bot.handle_event({**event("bot.error"), "message": "TTS failed"})
    assert runner._agent_watch is None and not any("technical problem" in m for _, m in client.bots.messages)
    await runner.end()


async def test_an_agent_error_during_the_goodbye_changes_nothing(settings, ready_interview):
    runner, client, _ = build(settings, ready_interview)
    await started(runner)
    await consent(runner)
    await runner.handle_tool("end_interview", {"reason": "completed"})
    await runner.bot.handle_event({**event("agent_error"), "message": FATAL})
    assert runner._agent_watch is None
    await settle(lambda: runner.state == "completed")
    assert runner.report.end_reason == "agent_ended_early"  # its own reason stands: the error doesn't replace it


def test_follow_ups_on_an_unfinished_question_are_counted_from_the_recording(settings, ready_interview):
    """Live call 21: the agent only reports follow-ups when it moves on, so the report said 0 when there were some."""
    runner, _, _ = build(settings, ready_interview)
    runner.q_index = 0
    qid = runner._current_qid()
    def said(text):
        return Utterance(speaker="AI", role="interviewer", text=text, question_id=qid)
    runner.transcript = [said("To get started, could you introduce yourself?"), said("How did you build the search?"),
                         said("Thanks for that."), said("Why vector embeddings?")]
    runner._count_open_follow_ups()
    assert runner.follow_ups == 2
    runner.notes[qid] = AnswerNote(question_id=qid, note="Agent's own count.", follow_ups=1)
    runner._count_open_follow_ups()
    assert runner.follow_ups == 1  # what the agent reported wins


async def test_a_late_tool_call_cant_stop_the_shutdown_of_a_dead_agent(settings, ready_interview):
    """The watch runs the shutdown itself: cancelling it there would lose the report and leave the interview stuck."""
    runner, _, _ = build(settings, ready_interview)
    runner._agent_recovery_s = 0.02
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence("A real answer about the outage."))
    await runner.bot.handle_event({**event("agent_error"), "message": FATAL})
    await settle(lambda: runner._ended)
    await runner.handle_tool("next_question", {"previous_question_id": "q1", "answer_note": "n"})
    await settle(lambda: runner.state == "completed")
    assert runner.report is not None and runner.report.end_reason == "agent_failed"


async def test_a_candidate_still_talking_holds_off_the_end_of_the_interview(settings, ready_interview):
    """An agent that came back only calls a tool when it moves on, which can take longer than the wait."""
    runner, client, _ = build(settings, ready_interview)
    runner._agent_recovery_s = 0.15
    await started(runner)
    await consent(runner)
    await runner.bot.handle_event({**event("agent_error"), "message": FATAL})
    for n in range(4):
        await asyncio.sleep(0.08)
        await runner.bot.handle_transcript(sentence(f"Still answering the question, part {n}."))
    assert runner.state == "questioning" and not any("couldn't recover" in m for _, m in client.bots.messages)
    await settle(lambda: runner.state == "completed")  # and when the talking stops, the wait runs out
    assert runner.report.end_reason == "agent_failed"


def test_a_question_said_again_isnt_a_follow_up(settings, ready_interview):
    runner, _, _ = build(settings, ready_interview)
    runner.q_index = 0
    qid = runner._current_qid()

    def said(text):
        return Utterance(speaker="AI", role="interviewer", text=text, question_id=qid)
    runner.transcript = [said("Could you briefly introduce yourself and your background?"),
                         said("Could you briefly introduce yourself, and your background?"), said("Why that framework?")]
    runner._count_open_follow_ups()
    assert runner.follow_ups == 1
