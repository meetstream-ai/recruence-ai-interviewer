"""Every AI interview opens with the candidate's self-introduction, then follow-ups on what they actually said, then
the job's planned questions (app/planning.py with_intro, app/interview/agent.py)."""

from dataclasses import replace

from app.interview.agent import agent_config, first_message, inline_plan_prompt, system_prompt
from app.llm.base import EvaluationInput
from app.llm.prompts import EVALUATOR_SYSTEM, PLANNER_SYSTEM, evaluation_prompt
from app.models import PlannedQuestion, Utterance
from app.planning import INTRO_FOLLOW_UPS, INTRO_ID, INTRO_QUESTION, context_for, is_intro_like, with_intro
from tests.test_runner import build, consent, started


def test_the_introduction_is_always_the_opening_question(ready_interview):
    ctx = context_for(ready_interview)
    assert ctx.questions[0].id == INTRO_ID and ctx.questions[0].text == INTRO_QUESTION
    assert [q.id for q in ctx.questions[1:]] == [q.id for q in ready_interview.plan.questions]  # then the plan, in order
    assert ready_interview.plan.questions[0].id != INTRO_ID  # never stored in the plan: older plans run unchanged
    # A recruiter's edited plan can't remove it, and a plan of its own doesn't add a second one.
    edited = ready_interview.model_copy(update={"plan": ready_interview.plan.model_copy(update={"questions": [
        PlannedQuestion(id="q1", text="Tell me a little about yourself.", competency_ids=[]),
        PlannedQuestion(id="q2", text="How do you design an idempotent payment API?", competency_ids=[]),
    ]})})
    qs = context_for(edited).questions
    assert [q.id for q in qs] == [INTRO_ID, "q2"]
    assert with_intro(with_intro(qs)) == with_intro(qs)  # never doubled


def test_duplicate_introductions_are_recognised():
    for text in ("Could you introduce yourself?", "Tell me about yourself.", "Tell us a bit about yourself", "Walk me through your resume."):
        assert is_intro_like(text), text
    for text in ("Tell me about a time you fixed an outage.", "Walk me through your design for a rate limiter."):
        assert not is_intro_like(text), text


def test_a_silent_analyst_bot_asks_nothing(ready_interview):
    analyst = ready_interview.model_copy(update={"options": ready_interview.options.model_copy(update={"mode": "analyst"})})
    assert context_for(analyst).questions[0].id != INTRO_ID


def test_the_agent_is_told_to_follow_up_on_what_was_said(settings, ready_interview):
    ctx = context_for(ready_interview)
    prompt = system_prompt(ctx, settings.max_follow_ups_per_question)
    rules = prompt[prompt.index("2c."):prompt.index("\n3.")]
    for must in ("Listen to the whole answer without interrupting", "from what they actually said",
                 f"between none and {INTRO_FOLLOW_UPS} follow-ups", "none if the introduction gives nothing specific",
                 "Never ask a generic question", "never ask about something they already explained",
                 "never return to a topic you already followed up on", "one specific follow-up at a time",
                 "about a fifth of the interview's time"):
        assert must in rules, must
    assert "bridging sentence" in prompt  # a natural transition into the planned questions
    opening = first_message(ctx)
    assert "start with a short introduction from you" in opening and "three questions" in opening  # the planned ones
    realtime = inline_plan_prompt(ctx, settings.max_follow_ups_per_question)
    assert INTRO_QUESTION in realtime and realtime.index(INTRO_QUESTION) < realtime.index(ready_interview.plan.questions[0].text)
    assert "Question 1 is the candidate's self-introduction" in realtime
    cfg = agent_config(settings, ctx, "s1", "https://t/x", "tok")
    assert "2c." in cfg["model"]["system_prompt"]


async def test_the_interview_serves_the_introduction_first_then_the_plan(settings, ready_interview):
    runner, _, _ = build(replace(settings, max_follow_ups_per_question=0), ready_interview)
    await started(runner)
    opening = await consent(runner, past_intro=False)
    assert opening["question_id"] == INTRO_ID and opening["question_number"] == 1 and opening["question"] == INTRO_QUESTION
    assert opening["max_follow_ups"] == INTRO_FOLLOW_UPS  # its own budget, whatever the global follow-up limit
    assert opening["look_for"] and all(len(x) < 80 for x in opening["look_for"])  # private hints, not questions
    again = await runner.handle_tool("next_question", {})
    assert again["question_id"] == INTRO_ID  # no skipping past it
    nxt = await runner.handle_tool("next_question", {"previous_question_id": INTRO_ID, "answer_note": "RAG app with LangChain and pgvector.",
                                                     "follow_ups_asked": 2})
    assert nxt["question_id"] == ready_interview.plan.questions[0].id and nxt["max_follow_ups"] == 0
    assert runner.notes[INTRO_ID].follow_ups == 2 and runner.follow_ups == 2
    assert [u.question_id for u in runner.transcript if u.role == "interviewer"][-2:] == [INTRO_ID, nxt["question_id"]]
    # Past the last planned question the plan is done: no loop back into the introduction.
    for q in ready_interview.plan.questions:
        res = await runner.handle_tool("next_question", {"previous_question_id": q.id, "answer_note": "n"})
    assert res["status"] == "done"


def test_the_evaluation_uses_the_introduction_without_overweighting_it(ready_interview):
    ctx = context_for(ready_interview)
    transcript = [Utterance(speaker="AI Interviewer", role="interviewer", text=INTRO_QUESTION, question_id=INTRO_ID),
                  Utterance(speaker="Priya", role="candidate", text="I built a RAG service with LangChain and PostgreSQL.", question_id=INTRO_ID)]
    text = evaluation_prompt(EvaluationInput(ctx, transcript, [], [INTRO_ID]))
    assert f"- {INTRO_ID} []: {INTRO_QUESTION}" in text and "I built a RAG service" in text
    assert "self-introduction" in EVALUATOR_SYSTEM and "never more" in EVALUATOR_SYSTEM
    assert "Don't write a self-introduction question" in PLANNER_SYSTEM
