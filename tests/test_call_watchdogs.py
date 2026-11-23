"""What the app does when the voice agent doesn't (live B-1, 2026-11-12): an agent that never starts, a question the
conversation is stuck on, a candidate's request to stop that the agent ignores, and the follow-up limit. In B-1 the
agent said nothing for 5 minutes, then cut itself off on one question for 15 (10 follow-ups counted), and "Can you
please end the interview?" wasn't acted on, all without an error from MeetStream."""

import asyncio

from app.interview.runner import (AGENT_NO_START_NOTICE, AGENT_SLOW_START_NOTICE, QUESTION_STALL_NOTICE, STOPPING_NOTICE,
                                  STOP_REQUEST)
from tests.conftest import settle
from tests.fakes import MEET_LINK, FakeMeetStream, event, sentence
from tests.test_api import TUNNEL, make_client, ready_interview_id, wait_for
from tests.test_runner import build, consent, started


def alerts(events, kind=None):
    return [e for e in events if e["type"] == "alert" and (kind is None or e["kind"] == kind)]


def chat(client):
    return [text for _, text in client.bots.messages]


# ---------- an agent that never starts ----------

async def test_an_agent_that_never_starts_is_flagged_then_ended(settings, ready_interview):
    runner, client, events = build(settings, ready_interview)
    runner._start_alert_s, runner._start_give_up_s = 0.03, 0.08
    await started(runner)
    await runner.bot.handle_transcript(sentence("Hello? Can anyone hear me?"))  # the candidate, waiting
    await settle(lambda: alerts(events))
    assert AGENT_SLOW_START_NOTICE in chat(client) and runner.state == "awaiting_consent"
    await settle(lambda: runner.state == "completed")
    assert runner.report.end_reason == "agent_failed" and not runner.report.consent_given
    assert AGENT_NO_START_NOTICE in chat(client) and client.bots.removed == ["bot-123"]
    assert all(u.role != "candidate" for u in runner.report.transcript)  # no consent: nothing of the candidate kept
    assert [a["kind"] for a in alerts(events)] == ["agent_silent_start", "agent_silent_start"]


async def test_an_agent_that_starts_is_left_alone(settings, ready_interview):
    runner, client, events = build(settings, ready_interview)
    runner._start_alert_s, runner._start_give_up_s = 0.05, 0.1
    await started(runner)
    assert (await runner.handle_tool("next_question", {}))["status"] == "consent_required"  # it asked for consent
    await settle(lambda: runner._start_watch.done())
    assert not alerts(events) and runner.state == "awaiting_consent" and AGENT_SLOW_START_NOTICE not in chat(client)


async def test_agent_speech_counts_as_started(settings, ready_interview):
    runner, client, events = build(settings, ready_interview)
    runner._start_alert_s = 0.05
    await started(runner)
    await runner._on_line("AI Interviewer", "Do you agree to continue?", is_bot=True)
    await settle(lambda: runner._start_watch.done())
    assert not alerts(events)



# ---------- Zoom: the host must allow recording first (B-2, 2026-11-13) ----------

async def test_on_zoom_the_start_is_timed_from_the_recording_permission(settings, ready_interview):
    """R-13: live, the host allowed recording 2 m 20 s after the bot joined; the watchdog had been counting from the join."""
    from app.meeting.platforms import ZOOM
    runner, client, events = build(settings, ready_interview, platform=ZOOM)
    runner._start_alert_s, runner._start_give_up_s = 0.04, 0.5
    await started(runner)
    await asyncio.sleep(0.12)  # three times the alert time, still waiting for the host
    assert not alerts(events) and runner.state == "awaiting_consent"
    await runner.bot.handle_event(event("bot.recording_permission_allowed", status="RecordingPermissionAllowed"))
    await settle(lambda: alerts(events))
    assert any("allowed recording" in u.text for u in runner.transcript if u.role == "system")
    # R-12: no chat on Zoom, so the alert doesn't claim the candidate was told there, and nothing was posted.
    message = alerts(events)[0]["message"]
    assert "There's no meeting chat on Zoom" in message and "was told" not in message and chat(client) == []
    await runner.end("cancelled")


async def test_on_zoom_recording_never_allowed_is_named_as_such(settings, ready_interview):
    from app.meeting.platforms import ZOOM
    runner, client, events = build(settings, ready_interview, platform=ZOOM)
    runner._start_alert_s, runner._start_give_up_s = 0.03, 0.08
    await started(runner)
    await settle(lambda: runner.state == "completed")
    assert [a["kind"] for a in alerts(events)] == ["recording_not_allowed"]
    assert "didn't allow recording within" in alerts(events)[0]["message"] and "on Zoom" in alerts(events)[0]["message"]
    assert runner.report.end_reason == "agent_failed"


async def test_on_zoom_a_refused_recording_ends_the_interview_at_once(settings, ready_interview):
    """MeetStream documents bot.recording_permission_denied (Zoom): no need to wait out the give-up time."""
    from app.meeting.platforms import ZOOM
    runner, client, events = build(settings, ready_interview, platform=ZOOM)
    runner._start_alert_s, runner._start_give_up_s = 0.03, 30.0
    await started(runner)
    await runner.bot.handle_event(event("bot.recording_permission_denied", status="RecordingPermissionDenied"))
    await settle(lambda: runner.state == "completed")
    assert [a["kind"] for a in alerts(events)] == ["recording_not_allowed"]
    assert "refused to let the bot record on Zoom" in alerts(events)[0]["message"]
    assert runner.report.end_reason == "agent_failed"
    assert not [e for e in events if e.get("type") == "error" and "voice agent" in str(e).lower()]  # not taken for an agent error


async def test_on_zoom_an_agent_that_starts_after_the_permission_is_left_alone(settings, ready_interview):
    from app.meeting.platforms import ZOOM
    runner, client, events = build(settings, ready_interview, platform=ZOOM)
    runner._start_alert_s, runner._start_give_up_s = 0.05, 0.3
    await started(runner)
    await runner.bot.handle_event(event("bot.recording", status="Recording"))  # the poll or a lost callback: recording itself counts
    assert (await runner.handle_tool("next_question", {}))["status"] == "consent_required"
    await settle(lambda: runner._start_watch.done())
    assert not alerts(events)


async def test_on_meet_the_alert_says_the_candidate_was_told_in_the_chat(settings, ready_interview):
    runner, client, events = build(settings, ready_interview)
    runner._start_alert_s, runner._start_give_up_s = 0.03, 5
    await started(runner)
    await settle(lambda: alerts(events))
    assert "The candidate was told to stay in the meeting in the meeting chat." in alerts(events)[0]["message"]
    await runner.end("cancelled")


# ---------- a question the conversation is stuck on ----------

async def test_a_stuck_question_is_flagged_and_the_candidate_told_how_to_move_on(settings, ready_interview):
    runner, client, events = build(settings, ready_interview)
    runner.context = runner.context.model_copy(update={"duration_minutes": 0})  # no time share: the minimum applies
    runner._question_stall_min_s = 0.05
    await started(runner)
    await consent(runner, past_intro=False)  # the introduction is open
    await settle(lambda: alerts(events, "question_stalled"))
    assert QUESTION_STALL_NOTICE in chat(client)
    assert runner.state == "questioning" and not runner._ended  # flagged, not ended: the recruiter decides
    assert "Question 1" in alerts(events)[0]["message"]


async def test_moving_on_in_time_raises_nothing(settings, ready_interview):
    runner, client, events = build(settings, ready_interview)
    runner.context = runner.context.model_copy(update={"duration_minutes": 0})
    runner._question_stall_min_s = 0.08
    await started(runner)
    await consent(runner, past_intro=False)
    await runner.handle_tool("next_question", {"previous_question_id": "intro", "answer_note": "n"})  # moves on at once
    first_watch = runner._stall_watch
    await runner.handle_tool("next_question", {"previous_question_id": "q1", "answer_note": "n"})
    await asyncio.sleep(0)  # a cancelled task finishes on the next turn of the loop
    assert first_watch.cancelled()
    await runner.end()
    assert not alerts(events, "question_stalled")


# ---------- a request to stop ----------

def test_what_counts_as_a_request_to_stop():
    for said in ("Can you please end the interview?", "End the interview now.", "Could you stop this AI interview",
                 "I'd like to end the call please", "Let's finish the interview here."):
        assert STOP_REQUEST.search(said), said
    for said in ("I mean, this interview is really irritating.", "Then I would end the call with the client",
                 "We stopped the meeting series after the launch", "It ends the interview loop for the ranking model"):
        assert not STOP_REQUEST.search(said), said


async def test_a_stop_request_the_agent_ignores_is_ended_by_the_app(settings, ready_interview):
    runner, client, events = build(settings, ready_interview)
    runner._stop_grace_s = 0.05
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence("Can you please end the interview?"))
    await settle(lambda: runner.state == "completed")
    assert runner.report.end_reason == "candidate_stopped" and runner.report.consent_given
    assert STOPPING_NOTICE in chat(client) and alerts(events, "candidate_asked_to_stop")


async def test_a_stop_request_the_agent_handles_needs_nothing_from_the_app(settings, ready_interview):
    runner, client, events = build(settings, ready_interview)
    runner._stop_grace_s = 0.2
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence("Please stop the interview."))
    await runner.handle_tool("end_interview", {"reason": "candidate_stopped"})  # the agent does it, within the grace
    await settle(lambda: runner.state == "completed")
    assert runner.report.end_reason == "candidate_stopped"
    assert not alerts(events) and STOPPING_NOTICE not in chat(client)


async def test_a_long_answer_that_mentions_ending_a_call_is_not_a_stop(settings, ready_interview):
    runner, _, events = build(settings, ready_interview)
    runner._stop_grace_s = 0.03
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence(
        "On call I had to decide quickly, so I would just end the call with the vendor, write up what we knew, page the "
        "database team and then come back to the incident channel with a plan we could all follow."))
    assert runner._stop_watch is None and not alerts(events)


# ---------- the follow-up limit ----------

async def test_follow_ups_past_the_limit_are_refused(settings, ready_interview):
    runner, _, _ = build(settings, ready_interview)
    await started(runner)
    assert (await runner.handle_tool("follow_up", {}))["status"] == "error"  # nothing open before consent
    first = await consent(runner)
    limit = first["max_follow_ups"]
    for left in range(limit - 1, -1, -1):
        assert await runner.handle_tool("follow_up", {"question_id": "q1"}) == {"status": "ok", "follow_ups_left": left}
    refused = await runner.handle_tool("follow_up", {"question_id": "q1"})
    assert refused["status"] == "limit_reached" and 'previous_question_id "q1"' in refused["instruction"]
    # The agent under-reports when it moves on: the note keeps the count the app allowed.
    await runner.handle_tool("next_question", {"previous_question_id": "q1", "answer_note": "n", "follow_ups_asked": 0})
    assert runner.notes["q1"].follow_ups == limit
    assert (await runner.handle_tool("follow_up", {}))["status"] == "ok"  # the next question has its own allowance


async def test_an_unfinished_question_keeps_the_counted_follow_ups(settings, ready_interview):
    runner, _, _ = build(settings, ready_interview)
    await started(runner)
    await consent(runner)
    await runner.handle_tool("follow_up", {"question_id": "q1"})
    await runner.end()
    assert runner.report.follow_ups_asked == 1


# ---------- through the app: the tool endpoint and the flag for the hiring team ----------

def test_follow_up_is_a_tool_endpoint_and_alerts_become_flags(settings):
    fake = FakeMeetStream()
    app, c = make_client(settings, fake)
    with c:
        iid = ready_interview_id(c)
        sid = c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET_LINK, "consent_attested": True}).json()["session_id"]
        live = app.state.manager.live[sid]
        token, runner = live.webhook_token, live.runner
        wait_for(lambda: fake.bots.created)
        names = [f["name"] for f in fake.mia.created[0]["agent"]["custom_functions"]]
        assert names == ["next_question", "end_interview", "follow_up"]
        assert c.post(f"/webhooks/meetstream/{token}/events", json=event("bot.inmeeting"), headers=TUNNEL).status_code == 200
        wait_for(lambda: c.get(f"/api/sessions/{sid}").json()["snapshot"]["state"] == "awaiting_consent")
        tool = f"/webhooks/mia/{token}"
        auth = {"Authorization": f"Bearer {runner.tool_token}", **TUNNEL}
        assert c.post(f"{tool}/follow_up", json={"args": {}}, headers=TUNNEL).status_code == 401
        assert c.post(f"{tool}/follow_up", json={"args": {}}, headers=auth).json()["status"] == "error"  # before consent

        app.state.manager._on_event(sid, iid, {"type": "alert", "kind": "question_stalled", "message": "Question 2 has been open for 7 minutes."})
        app.state.manager._on_event(sid, iid, {"type": "alert", "kind": "question_stalled", "message": "again"})  # once per kind
        d = c.get(f"/api/interviews/{iid}").json()
        flags = d["interview"]["flags"]
        assert [(f["kind"], f["detail"]) for f in flags] == [("question_stalled", "Question 2 has been open for 7 minutes.")]
        assert [e["detail"] for e in d["audit"] if e["action"] == "interview_alert"] == ["question_stalled", "question_stalled"]
        c.post(f"/api/sessions/{sid}/end")
