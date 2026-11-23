"""Interview formats (voice, video, proctored) and what a proctored interview records, from which source."""

from datetime import timedelta

import meetstream

from app.interview.agent import agent_config, first_message, chat_disclosure, setup_request, system_prompt
from app.models import InterviewOptions, utcnow
from app.planning import context_for
from app.proctoring import live_event, recording_events
from tests.conftest import settle
from tests.fakes import FakeMeetStream, MEET_LINK, event
from tests.test_api import make_client, ready_interview_id
from tests.test_runner import build, consent, started

PROCTORED = {"format": "proctored", "require_camera": True, "require_screen_share": True}


def proctored(interview, **options):
    return interview.model_copy(update={"options": InterviewOptions(**{**PROCTORED, **options})})


def stream(kind, start, duration, name="Priya Raman"):
    return {"type": kind, "participant": {"name": name}, "start_timestamp": {"absolute": start.isoformat()}, "duration": duration}


def test_formats_are_saved_before_launch_and_kept_at_launch(settings):
    _, c = make_client(settings)
    with c:
        iid = ready_interview_id(c)
        r = c.put(f"/api/interviews/{iid}/options", json=PROCTORED)
        assert r.status_code == 200 and r.json()["options"]["format"] == "proctored" and r.json()["options"]["record_video"] is True
        assert c.put(f"/api/interviews/{iid}/options", json={"mode": "analyst"}).status_code == 422  # proctoring needs the AI
        assert c.put(f"/api/interviews/{iid}/options", json={"colour": "blue"}).status_code == 422
        # The invitation describes what's asked of the candidate.
        body = c.post(f"/api/interviews/{iid}/invite/preview", json={"meeting_link": MEET_LINK}).json()["body"]
        assert "proctored interview" in body and "share your entire screen" in body and "isn't analysed" in body
        # A launch without a format keeps the saved one.
        r = c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET_LINK, "consent_attested": True,
                                                          "scheduled_at": (utcnow() + timedelta(days=1)).isoformat()})
        assert r.status_code == 200
        assert c.get(f"/api/interviews/{iid}").json()["interview"]["options"]["format"] == "proctored"
        assert c.put(f"/api/interviews/{iid}/options", json={"format": "voice"}).status_code == 409  # scheduled: locked


def test_older_saved_options_become_a_format():
    assert InterviewOptions(record_video=True).format == "video"
    assert InterviewOptions().format == "voice" and InterviewOptions().record_video is False
    assert InterviewOptions(format="proctored").record_video is True


def test_the_ai_tells_the_candidate_and_asks_for_the_screen(settings, ready_interview):
    ctx = context_for(proctored(ready_interview))
    assert "proctored interview" in first_message(ctx) and "your entire screen shared" in first_message(ctx)
    assert "proctored" in chat_disclosure(ctx) and "not analysed" in chat_disclosure(ctx)
    assert setup_request(ctx).startswith("Before we begin, please share your entire screen. Screen sharing is required for this proctored interview.")
    assert "setup_required" in system_prompt(ctx, 2) and "never comment on the screen's content" in system_prompt(ctx, 2)
    params = agent_config(settings, ctx, "s1", "https://t/x", "tok")["agent"]["custom_functions"][0]["parameters"]["properties"]
    assert "proctoring_setup" in params
    plain = context_for(ready_interview)
    assert "proctored" not in first_message(plain) and "setup_required" not in system_prompt(plain, 2)
    assert "proctoring_setup" not in agent_config(settings, plain, "s1", "https://t/x", "tok")["agent"]["custom_functions"][0]["parameters"]["properties"]
    camera_only = context_for(proctored(ready_interview, require_screen_share=False))
    assert setup_request(camera_only).startswith("Before we begin, please turn your camera on.")


async def test_no_question_before_the_setup_and_every_entry_says_how_it_is_known(settings, ready_interview):
    client = FakeMeetStream()
    runner, _, _ = build(settings, proctored(ready_interview), client=client)
    await started(runner)
    params = client.bots.created[0]["params"]
    assert params["video_required"] is True and params["video_separate_streams"] is True

    first = await consent(runner)
    assert first["status"] == "setup_required" and "share your entire screen" in first["instruction"]
    assert (await runner.handle_tool("next_question", {}))["status"] == "setup_required"  # still no question
    q1 = await runner.handle_tool("next_question", {"proctoring_setup": "ready"})
    assert q1["status"] == "question" and q1["question_number"] == 1

    # A screen-share stop reported live is logged and passed to the AI with its next question.
    await runner.bot.handle_event({**event("participant_events.screenshare_off"), "participant_name": "Priya Raman"})
    q2 = await runner.handle_tool("next_question", {"previous_question_id": q1["question_id"], "answer_note": "ok", "follow_ups_asked": 0})
    assert q2["question_number"] == 2 and "share their entire screen again" in q2["instruction"]

    t0 = runner.started_at
    client.bots.streams = [stream("webcam", t0, 600), stream("screenshare", t0 + timedelta(seconds=40), 200),
                           stream("screenshare", t0 + timedelta(seconds=300), 250)]
    await runner.end()
    await settle(lambda: runner.state == "completed")
    p = runner.report.proctoring
    assert p.consent_given and p.screen_share_seconds == 450 and p.interruptions == 1
    by_kind = {e.kind: e for e in p.events}
    assert by_kind["setup_requested"].source == "app"
    assert by_kind["setup_confirmed_by_candidate"].source == "candidate" and "not something the app could check" in by_kind["setup_confirmed_by_candidate"].text
    assert by_kind["screen_share_stopped"].source in ("meetstream_live", "meetstream_recording")
    gap = by_kind["screen_share_interrupted"]
    assert gap.source == "meetstream_recording" and gap.duration_seconds == 60 and "misconduct" not in gap.text
    assert any("camera" in u for u in p.unverified) and any("content is not analysed" in u for u in p.unverified)


async def test_a_candidate_who_cannot_share_is_not_blocked(settings, ready_interview):
    client = FakeMeetStream()
    client.bots.streams_error = meetstream.MeetStreamError("boom")
    runner, _, _ = build(settings, proctored(ready_interview), client=client)
    await started(runner)
    await consent(runner)
    q1 = await runner.handle_tool("next_question", {"proctoring_setup": "unable", "proctoring_note": "Their browser blocked sharing."})
    assert q1["status"] == "question"
    await runner.end()
    await settle(lambda: runner.state == "completed")
    p = runner.report.proctoring
    unable = next(e for e in p.events if e.kind == "setup_unavailable")
    assert unable.source == "candidate" and "browser blocked" in unable.text
    assert p.screen_share_seconds is None and any("couldn't be read" in u for u in p.unverified)


async def test_nothing_is_fetched_without_consent(settings, ready_interview):
    client = FakeMeetStream()
    runner, _, _ = build(settings, proctored(ready_interview), client=client)
    await started(runner)
    await runner.handle_tool("end_interview", {"reason": "consent_declined"})
    await settle(lambda: runner.state == "completed")
    p = runner.report.proctoring
    assert not p.consent_given and p.screen_share_seconds is None
    assert any("didn't agree" in u for u in p.unverified)


def test_recording_streams_are_read_defensively():
    t0 = utcnow()
    events, total, gaps, notes = recording_events([
        {"type": "screenshare", "participant": {"name": "Dana (recruiter)"}, "start_timestamp": {"relative": 10}, "duration": 30},
        {"type": "screenshare", "participant": {"name": "Priya Raman"}, "start_timestamp": {"relative": 60}, "duration": 100},
        {"type": "screenshare", "participant": {"name": "Priya Raman"}, "start_timestamp": {"relative": 161}, "duration": 50},  # 1 s seam
        {"type": "screenshare", "start_timestamp": {}},
    ], t0, lambda name: name.startswith("Priya"))
    assert total == 150 and gaps == 0
    assert [e.kind for e in events][:2] == ["other_screen_share", "screen_share_started"]
    assert any("no usable start time" in n for n in notes) and any("no separate camera stream" in n for n in notes)
    assert recording_events([], t0, lambda n: True)[3][0] == "MeetStream's recording has no screen share."


def test_live_events_only_when_they_are_screen_or_camera_events():
    now = utcnow()
    assert live_event({"bot_event": "participant_events.join"}, now) is None
    e = live_event({"event": "participant_events.webcam_off", "participant": {"name": "Priya"}}, now)
    assert e.kind == "camera_off" and e.source == "meetstream_live" and e.participant == "Priya"
    assert live_event({"action": "screenshare_on", "timestamp": 1793500000}, now).at.year >= 2026


async def test_the_recording_is_awaited_through_meetstreams_early_404(settings, ready_interview, monkeypatch):
    """Live 2026-11-02: get_recording_streams answered 404 'No video processing has been initiated' right after the
    call and the streams a few minutes later. A screen shared before the bot joined starts with the recording."""
    import app.interview.runner as runner_module
    monkeypatch.setattr(runner_module, "STREAMS_POLL_SECONDS", 0.01)
    client = FakeMeetStream()
    runner, _, _ = build(settings, proctored(ready_interview), client=client)
    await started(runner)
    await consent(runner)
    await runner.handle_tool("next_question", {"proctoring_setup": "ready"})
    t0 = runner.started_at
    answers = [meetstream.NotFoundError("No video processing has been initiated for this bot."),
               [stream("webcam", t0, 178.5), stream("screenshare", t0, 178.5)]]

    async def streams(bot_id):
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer
    client.bots.recording_streams = streams
    await runner.end()
    await settle(lambda: runner.state == "completed")
    p = runner.report.proctoring
    assert p.screen_share_seconds == 178.5 and p.interruptions == 0
    started_event = next(e for e in p.events if e.kind == "screen_share_started")
    assert "already being shared when the recording started" in started_event.text


def test_other_people_on_camera_are_listed_but_not_the_bot_or_the_candidate():
    t0 = utcnow()
    events, *_ = recording_events([
        {"type": "webcam", "participant": {"name": "Priya Raman"}, "start_timestamp": {"absolute": t0.isoformat()}, "duration": 600},
        {"type": "webcam", "participant": {"name": "AI Interviewer"}, "start_timestamp": {"relative": 0}, "duration": 600},
        {"type": "webcam", "participant": {"name": "Sam Helper"}, "start_timestamp": {"relative": 120}, "duration": 60},
        {"type": "webcam", "participant": {"name": "Sam Helper"}, "start_timestamp": {"relative": 300}, "duration": 60},  # once per person
    ], t0, lambda name: name.startswith("Priya"), lambda name: name == "AI Interviewer")
    others = [e for e in events if e.kind == "other_participant"]
    assert [(e.participant, e.source) for e in others] == [("Sam Helper", "meetstream_recording")]
    assert "camera on" in others[0].text and (others[0].at - t0).total_seconds() == 120
