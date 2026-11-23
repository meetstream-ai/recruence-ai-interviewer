import pytest

from app.llm.offline import OfflineAI
from app.models import GeneratedCompetency, GeneratedPlan, GeneratedQuestion, InterviewSetup
from app.planning import PlanError, build_plan, generate_plan, validate_plan
from tests.fakes import FakeAI


def generated(questions, comps=("System design", "Ownership")):
    return GeneratedPlan(
        candidate_summary="Python services engineer.",
        competencies=[GeneratedCompetency(name=c, description="") for c in comps],
        questions=[GeneratedQuestion(text=t, competency_names=n, look_for=["a", " ", "b"]) for t, n in questions],
    )


def test_recruiter_competencies_win_and_names_map(setup):
    g = generated(
        [("Design a payment API.", ["system design"]), ("Tell me about an outage.", ["Ownership", "Made Up"])],
        comps=("Something else",),
    )
    plan = build_plan(setup, g, "fake")
    assert [c.name for c in plan.competencies] == ["System design", "Ownership"]
    assert [c.id for c in plan.competencies] == ["c1", "c2"]
    assert [q.competency_ids for q in plan.questions] == [["c1"], ["c2"]]
    assert plan.questions[0].look_for == ["a", "b"]


def test_unmapped_questions_are_balanced_and_count_capped(setup):
    g = generated([(f"Question {n}?", ["nope"]) for n in range(6)])
    plan = build_plan(setup, g, "fake")
    assert len(plan.questions) == setup.question_count == 3
    assert [q.competency_ids[0] for q in plan.questions] == ["c1", "c2", "c1"]
    assert [q.id for q in plan.questions] == ["q1", "q2", "q3"]


def test_generated_competencies_used_when_recruiter_gave_none(setup):
    s = setup.model_copy(update={"competencies": []})
    plan = build_plan(s, generated([("Q?", ["Ownership"])], comps=("Ownership", "ownership", "Craft")), "fake")
    assert [c.name for c in plan.competencies] == ["Ownership", "Craft"]


def test_empty_plan_rejected(setup):
    with pytest.raises(PlanError):
        build_plan(setup, generated([("   ", ["Ownership"])]), "fake")


async def test_ai_failure_falls_back_to_labelled_template(setup):
    ai = FakeAI(plan_error="overloaded")
    plan, warning = await generate_plan(ai, setup)
    assert warning == "overloaded"
    assert "no AI" in plan.source and len(plan.questions) == 3


async def test_ai_plan_used_when_it_works(setup):
    ai = FakeAI(plan=generated([("Design a payment API.", ["System design"])]))
    plan, warning = await generate_plan(ai, setup)
    assert warning is None and plan.source == "fake-ai" and plan.questions[0].text == "Design a payment API."
    assert ai.plan_requests[0].resume_text.startswith("Seven years")


async def test_offline_template_covers_each_type(setup):
    for kind in ("behavioral", "technical", "screening", "mixed"):
        s = setup.model_copy(update={"interview_type": kind, "competencies": [], "question_count": 5})
        plan = build_plan(s, await OfflineAI().generate_plan(s), "t")
        assert len(plan.questions) == 5 and len({q.text for q in plan.questions}) == 5


async def test_validate_plan_renumbers_and_rejects_bad_edits(setup):
    plan = build_plan(setup, await OfflineAI().generate_plan(setup), "t")
    q = plan.questions
    edited = validate_plan(plan.model_copy(update={"questions": [q[2], q[0]]}))
    assert [x.id for x in edited.questions] == ["q1", "q2"] and edited.questions[0].text == q[2].text

    bad = [
        plan.model_copy(update={"questions": []}),
        plan.model_copy(update={"questions": [q[0].model_copy(update={"text": "  "})]}),
        plan.model_copy(update={"questions": [q[0].model_copy(update={"competency_ids": ["c9"]})]}),
        plan.model_copy(update={"questions": [q[0].model_copy(update={"max_follow_ups": 9})]}),
        plan.model_copy(update={"questions": [q[0]] * 13}),
    ]
    for p in bad:
        with pytest.raises(PlanError):
            validate_plan(p)


def test_setup_rejects_out_of_range_values():
    with pytest.raises(ValueError):
        InterviewSetup(role_title="x", job_description="y", candidate_name="z", question_count=11)
