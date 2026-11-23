"""Rebuilding the transcript from the recording. The data mirrors the live Google Meet calls of
2026-10-28: every post-call phrase is credited to the human, chunks cross speaker turns, the
two transcriptions disagree on some words, and the AI's follow-up exists only in the recording."""

from dataclasses import replace

import meetstream

from app.models import Utterance, utcnow
from app.interview.agent import first_message
from app.planning import context_for
from app.postcall import merge_postcall
from tests.conftest import settle
from tests.fakes import FakeAI, FakeMeetStream, chunks, sentence
from tests.test_runner import build, consent, started

GREETING = "Hello Priya, thanks for joining. I'm an AI interviewer. Do you agree to continue with this AI interview?"
Q1 = "Tell me about a time your ownership made a difference. What did you do?"
Q2 = "Tell me about a time your communication made a difference. What was the result?"


def live():
    return [
        Utterance(speaker="AI Interviewer", role="interviewer", text=GREETING),
        Utterance(speaker="Priya", role="candidate", text="Yes."),
        Utterance(speaker="System", role="system", text="The candidate agreed."),
        Utterance(speaker="AI Interviewer", role="interviewer", text=Q1, question_id="q1"),
        Utterance(speaker="Priya", role="candidate", text="I worked on lots of things. It usually goes fine.", question_id="q1"),
        Utterance(speaker="Priya", role="candidate", text="Last year our payment API timed out during a sale and I rolled back the deploy.", question_id="q1"),
        Utterance(speaker="AI Interviewer", role="interviewer", text=Q2, question_id="q2"),
        Utterance(speaker="Priya", role="candidate", text="During the migration I said I was shot weekly call with the support lead and explained the plan.", question_id="q2"),
    ]


RECORDING = chunks(
    "Hello Priya, thanks for joining. I'm an AI interviewer.",
    "Do you agree to continue with this AI interview? Yes.",
    "Tell me about a time your ownership made a difference. What did you do?",
    "I worked on lots of things. Usually it",
    "goes fine. Can you walk me through one specific example?",
    "Last year our payment API timed out during a big sale and I rolled back the deploy.",
    "Thanks for explaining that. Tell me about a time your communication made a difference. What was the result?",
    "During the migration I set up a short weekly call with a support lead and explained the plan.",
    "Thank you for your time today. Goodbye.",
)


def merged():
    return merge_postcall(RECORDING, live(), agent_name="AI Interviewer", candidate_name="Priya Raman", started_at=utcnow())


def test_recovers_the_follow_up_and_separates_speakers():
    lines = [(u.role, u.text) for u in merged()]
    assert lines == [
        ("interviewer", "Hello Priya, thanks for joining. I'm an AI interviewer. Do you agree to continue with this AI interview?"),
        ("candidate", "Yes."),
        ("interviewer", "Tell me about a time your ownership made a difference. What did you do?"),
        ("candidate", "I worked on lots of things. Usually it goes fine."),
        ("interviewer", "Can you walk me through one specific example?"),
        ("candidate", "Last year our payment API timed out during a big sale and I rolled back the deploy."),
        ("interviewer", "Thanks for explaining that. Tell me about a time your communication made a difference. What was the result?"),
        ("candidate", "During the migration I set up a short weekly call with a support lead and explained the plan."),
        ("interviewer", "Thank you for your time today. Goodbye."),
    ]


def test_question_ids_and_labels():
    out = merged()
    follow_up = next(u for u in out if u.text.startswith("Can you walk me"))
    assert follow_up.question_id == "q1" and follow_up.speaker == "AI Interviewer (from recording)"
    assert next(u for u in out if u.text.startswith("During the migration")).question_id == "q2"
    assert all(u.speaker == "Priya Raman" for u in out if u.role == "candidate")
    assert out[1].ts > out[0].ts


def test_unusable_recording_returns_nothing():
    assert merge_postcall([], live(), agent_name="A", candidate_name="C", started_at=utcnow()) == []
    unrelated = chunks("Completely different words about the weather today.")
    assert merge_postcall(unrelated, live(), agent_name="A", candidate_name="C", started_at=utcnow()) == []


def test_recording_without_the_agent_voice_is_not_used():
    """Live Teams call 2026-10-29: the recording held only the candidate's speech."""
    candidate_only = chunks(
        "Yes.",
        "I worked on lots of things. It usually goes fine.",
        "Last year our payment API timed out during a big sale and I rolled back the deploy.",
        "During the migration I set up a short weekly call with a support lead and explained the plan.",
    )
    assert merge_postcall(candidate_only, live(), agent_name="A", candidate_name="C", started_at=utcnow()) == []


def test_keeps_a_live_answer_the_recording_lost():
    """Live Meet call 17 (2026-11-01): the recording's transcript had no trace of the answer to question 2."""
    lost_answer = chunks(*[p for p in (w["text"] for w in RECORDING[0]["words"]) if not p.startswith("During the migration")])
    out = merge_postcall(lost_answer, live(), agent_name="AI Interviewer", candidate_name="Priya Raman", started_at=utcnow())
    lines = [(u.role, u.text) for u in out]
    kept = ("candidate", "During the migration I said I was shot weekly call with the support lead and explained the plan.")
    q2_at = next(n for n, (_, text) in enumerate(lines) if text.endswith("What was the result?"))
    assert lines[q2_at + 1] == kept
    assert lines[q2_at + 2] == ("interviewer", "Thank you for your time today. Goodbye.")
    line = out[q2_at + 1]
    assert line.speaker == "Priya Raman" and line.question_id == "q2"
    # Answers the recording did hold are not repeated from the live transcript.
    assert sum(1 for _, text in lines if "rolled back the deploy" in text) == 1


def test_a_lost_consent_goes_after_the_greeting():
    no_yes = chunks(*[p.replace(" Yes.", "") for p in (w["text"] for w in RECORDING[0]["words"])])
    out = merge_postcall(no_yes, live(), agent_name="AI Interviewer", candidate_name="Priya Raman", started_at=utcnow())
    assert [(u.role, u.text) for u in out[:3]] == [
        ("interviewer", "Hello Priya, thanks for joining. I'm an AI interviewer. Do you agree to continue with this AI interview?"),
        ("candidate", "Yes."),
        ("interviewer", "Tell me about a time your ownership made a difference. What did you do?"),
    ]


def test_segment_list_shape_gives_the_same_result():
    phrases = [w["text"] for w in RECORDING[0]["words"]]
    segs = [{"speaker": "Priya Raman", "transcript": p, "start_time": n * 2.5} for n, p in enumerate(phrases)]
    out = merge_postcall(segs, live(), agent_name="AI Interviewer", candidate_name="Priya Raman", started_at=utcnow())
    assert [(u.role, u.text) for u in out] == [(u.role, u.text) for u in merged()]


# ---------- in the interview flow ----------


async def run_interview(settings, ready_interview, client, ai):
    runner, _, events = build(replace(settings, postcall_transcript=True), ready_interview, client=client, ai=ai)
    await started(runner)
    await consent(runner)
    await runner.bot.handle_transcript(sentence("I rolled back the deploy and added an index."))
    await runner.end()
    await settle(lambda: runner.state == "completed")
    return runner, events


async def test_report_uses_the_recording(settings, ready_interview):
    q1 = ready_interview.plan.questions[0].text
    greeting = first_message(context_for(ready_interview))
    post = chunks(greeting, q1, "I rolled back the deploy and added an index.", "Can you tell me more about the index?")
    ai = FakeAI()
    client = FakeMeetStream(post=post)
    runner, events = await run_interview(settings, ready_interview, client, ai)
    r = runner.report
    assert r.transcript_source == "recording"
    assert ("interviewer", "Can you tell me more about the index?") in [(u.role, u.text) for u in r.transcript]
    assert client.transcripts.requested == [("bot-123", {"provider": {"meetstream": {"language": "en"}}})]
    assert any(u.text == "Can you tell me more about the index?" for u in ai.evaluations[0].transcript)
    assert "transcribing" in [e["state"] for e in events if e["type"] == "state"]


async def test_falls_back_to_live_transcript(settings, ready_interview):
    client = FakeMeetStream(post_error=meetstream.ServerError("transcription failed", status=500))
    runner, events = await run_interview(settings, ready_interview, client, FakeAI())
    assert runner.report.transcript_source == "live" and runner.report.evaluation is not None
    assert any("live transcript" in e.get("message", "") for e in events if e["type"] == "error")


async def test_no_recording_transcript_without_consent(settings, ready_interview):
    client = FakeMeetStream(post=chunks("anything"))
    runner, _, _ = build(replace(settings, postcall_transcript=True), ready_interview, client=client)
    await started(runner)
    await runner.handle_tool("end_interview", {"reason": "consent_declined"})
    await settle(lambda: runner.state == "completed")
    assert client.transcripts.requested == [] and client.bots.erased == ["bot-123"]
