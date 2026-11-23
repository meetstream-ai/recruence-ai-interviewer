"""Per-interview options: Meeting Analyst mode (a silent bot), questions in the meeting chat, video recording."""

from datetime import timedelta

from app.meeting.platforms import ZOOM
from app.models import InterviewOptions, utcnow
from app.planning import context_for
from app.llm.prompts import evaluation_prompt
from app.llm.base import EvaluationInput
from tests.conftest import settle
from tests.fakes import FakeAI, FakeMeetStream, sentence
from tests.test_api import TUNNEL, make_client, ready_interview_id
from tests.test_runner import build, consent, started

ZOOM_LINK = "https://zoom.us/j/1234567890"


def with_options(interview, **options):
    return interview.model_copy(update={"options": InterviewOptions(**options)})


async def test_meeting_analyst_is_silent_and_evaluates_the_human_led_conversation(settings, ready_interview):
    client, ai = FakeMeetStream(), FakeAI()
    runner, _, _ = build(settings, with_options(ready_interview, mode="analyst", candidate_meeting_name="Priya R."), client=client, ai=ai)
    await started(runner)
    assert client.mia.created == []  # no voice agent at all
    params = client.bots.created[0]["params"]
    assert "agent_config_id" not in params
    assert runner.consented and runner.state == "questioning"
    assert client.bots.messages and client.bots.messages[0][1].startswith("Recording notice:")

    await runner.bot.handle_transcript(sentence("Tell me about a production incident you handled?", speaker="Dana Okafor"))
    await runner.bot.handle_transcript(sentence("Our payments API timed out and I rolled back the release.", speaker="Priya R."))
    roles = [(u.speaker, u.role) for u in runner.transcript if u.role != "system"]
    assert roles == [("Dana Okafor", "interviewer"), ("Priya R.", "candidate")]

    await runner.end()
    await settle(lambda: runner.state == "completed")
    assert runner.report.mode == "analyst" and runner.report.transcript_source == "live"
    assert "Meeting Analyst" in runner.report.generated_by
    assert ai.evaluations and any(u.role == "candidate" for u in ai.evaluations[0].transcript)


def test_analyst_evaluation_prompt_says_a_person_led(ready_interview):
    ctx = context_for(with_options(ready_interview, mode="analyst"))
    prompt = evaluation_prompt(EvaluationInput(ctx, [], [], []))
    assert "A person (not the AI) led this interview" in prompt and "were asked" not in prompt
    assert "were asked" in evaluation_prompt(EvaluationInput(context_for(ready_interview), [], [], []))


async def test_each_question_is_posted_once_in_the_meeting_chat(settings, ready_interview):
    client = FakeMeetStream()
    runner, _, _ = build(settings, with_options(ready_interview, questions_in_chat=True), client=client)
    await started(runner)
    q1 = await consent(runner, past_intro=False)
    await runner.handle_tool("next_question", {"previous_question_id": "wrong-id"})  # no advance: no repeat
    await runner.handle_tool("next_question", {"previous_question_id": q1["question_id"], "answer_note": "ok", "follow_ups_asked": 0})
    await settle(lambda: sum(m.startswith("Question ") for _, m in client.bots.messages) >= 2)
    questions = [m for _, m in client.bots.messages if m.startswith("Question ")]
    asked = runner.context.questions  # the introduction, then the plan
    assert questions == [f"Question 1 of {len(asked)}: {asked[0].text}", f"Question 2 of {len(asked)}: {asked[1].text}"]


async def test_no_questions_in_chat_on_zoom(settings, ready_interview):
    client = FakeMeetStream()
    interview = with_options(ready_interview, questions_in_chat=True).model_copy(update={"meeting_link": ZOOM_LINK})
    runner, _, _ = build(settings, interview, client=client, platform=ZOOM)
    await started(runner)
    await consent(runner)
    assert not any(m.startswith("Question ") for _, m in client.bots.messages)
    assert any("aren't posted in the meeting chat on Zoom" in u.text for u in runner.transcript)


async def test_video_recording_is_requested_only_when_chosen(settings, ready_interview):
    for record, expected in ((False, False), (True, True)):
        client = FakeMeetStream()
        runner, _, _ = build(settings, with_options(ready_interview, record_video=record), client=client)
        await started(runner)
        params = client.bots.created[0]["params"]
        assert params["video_required"] is expected
        assert (params["recording_config"].get("video_layout") == "speaker_view") is expected
        await runner.end()


def test_launch_saves_the_options(settings):
    _, c = make_client(settings)
    with c:
        iid = ready_interview_id(c)
        later = (utcnow() + timedelta(days=1)).isoformat()
        r = c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": ZOOM_LINK, "consent_attested": True, "scheduled_at": later,
                                                         "mode": "analyst", "candidate_meeting_name": "Priya", "questions_in_chat": True, "record_video": True})
        assert r.status_code == 200
        options = c.get(f"/api/interviews/{iid}").json()["interview"]["options"]
        # Questions in chat only make sense when the AI asks them.
        assert options == {"mode": "analyst", "candidate_meeting_name": "Priya", "questions_in_chat": False, "record_video": True,
                           "format": "video", "require_camera": True, "require_screen_share": True}  # record_video from an older client
        assert c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": ZOOM_LINK, "consent_attested": True, "mode": "robot"}).status_code == 422
        assert c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": ZOOM_LINK, "consent_attested": True}, headers=TUNNEL).status_code == 403


async def test_the_question_in_the_chat_is_exactly_what_the_ai_is_told_to_say(settings, ready_interview):
    assert InterviewOptions().questions_in_chat is True  # on by default
    client = FakeMeetStream()
    runner, _, _ = build(settings, ready_interview, client=client)  # default options
    await started(runner)
    q1 = await consent(runner, past_intro=False)
    await settle(lambda: any(m.startswith("Question 1 ") for _, m in client.bots.messages))
    posted = next(m for _, m in client.bots.messages if m.startswith("Question 1 "))
    assert posted.split(": ", 1)[1] == q1["question"]  # the chat shows the very text next_question gave the agent
    # The agent is told to read it exactly as written, because the candidate also reads it; not when it isn't shown.
    from app.interview.agent import system_prompt
    shown = system_prompt(context_for(ready_interview), 2)
    hidden = system_prompt(context_for(with_options(ready_interview, questions_in_chat=False)), 2)
    assert "appears in the meeting chat" in shown and "without rephrasing" in shown
    assert "appears in the meeting chat" not in hidden


def test_a_role_carries_the_chat_setting_to_its_interviews_and_launches_keep_it(settings):
    from tests.test_recruiting import approved_role, client as recruiting_client
    _, c = recruiting_client(settings)
    with c:
        rid, _ = approved_role(c)
        r = c.put(f"/api/roles/{rid}/hiring", json={"options": {"mode": "ai", "format": "voice", "questions_in_chat": False}})
        assert r.json()["role"]["hiring"]["options"]["questions_in_chat"] is False
        r = c.put(f"/api/roles/{rid}/hiring", json={"options": {"questions_in_chat": True}})
        assert r.json()["role"]["hiring"]["options"]["questions_in_chat"] is True


def test_existing_roles_get_questions_in_chat_once_and_later_choices_stick(settings):
    from app.store import Store
    from tests.test_recruiting import approved_role, client as recruiting_client
    app, c = recruiting_client(settings)
    with c:
        rid, _ = approved_role(c)
    # A role saved before the default changed: "off" was written into it, and the fix hasn't run on this database yet.
    store = app.state.store
    role = store.get_role(rid)
    store.rewrite_role(role.model_copy(update={"hiring": role.hiring.model_copy(update={
        "options": role.hiring.options.model_copy(update={"questions_in_chat": False})})}))
    store.set_state("data_fixes", {})
    before = store.get_role(rid).updated_at
    app2, c2 = recruiting_client(settings)  # the next start
    with c2:
        fixed = app2.state.store.get_role(rid)
        assert fixed.hiring.options.questions_in_chat is True and fixed.updated_at == before  # not counted as an edit
        c2.put(f"/api/roles/{rid}/hiring", json={"options": {"questions_in_chat": False}})  # the recruiter's choice
    app3, c3 = recruiting_client(settings)  # and another start: the fix doesn't run twice
    with c3:
        assert app3.state.store.get_role(rid).hiring.options.questions_in_chat is False
