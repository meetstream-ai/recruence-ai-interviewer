"""Turning recruiter input into a valid interview plan, and validating recruiter edits.

The generator (LLM or template) proposes; code decides ids, counts and mappings,
so a plan that reaches an interview is always well-formed.
"""

from __future__ import annotations

import logging
import re

from app.llm.base import InterviewAI, LLMError
from app.llm.offline import OfflineAI
from app.models import Competency, GeneratedPlan, InterviewContext, Interview, InterviewSetup, Plan, PlannedQuestion, Requirement

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 12
MAX_QUESTION_CHARS = 600
MAX_LOOK_FOR = 5


# Every AI interview opens with the candidate's own introduction, before the job's planned questions. It is added when
# an interview runs (context_for), never stored in a plan, so no plan edit can remove it and older plans run as they are.
INTRO_ID = "intro"
INTRO_QUESTION = ("To get started, could you briefly introduce yourself, walk me through your background and experience, "
                  "and tell me what interests you about this role?")
INTRO_FOLLOW_UPS = 3  # at most; none when the introduction gives nothing specific to probe
INTRO_LOOK_FOR = [
    "skills, tools and technologies they name",
    "previous roles, responsibilities and what they personally did",
    "projects, achievements and technical claims worth probing for depth",
    "what draws them to this role",
]
# A planned question that is itself a self-introduction ("tell me about yourself") isn't asked a second time.
_INTRO_LIKE = re.compile(r"\b(introduce yourself|tell (me|us) (a (little|bit) )?about yourself|walk (me|us) through your (background|cv|resume))\b", re.I)


def is_intro_like(text: str) -> bool:
    return bool(_INTRO_LIKE.search(text or ""))


def with_intro(questions: list[PlannedQuestion]) -> list[PlannedQuestion]:
    """The self-introduction first, then the planned questions (without one that repeats it)."""
    planned = [q for q in questions if q.id != INTRO_ID and not is_intro_like(q.text)] or [q for q in questions if q.id != INTRO_ID]
    intro = PlannedQuestion(id=INTRO_ID, text=INTRO_QUESTION, competency_ids=[], look_for=list(INTRO_LOOK_FOR), max_follow_ups=INTRO_FOLLOW_UPS)
    return [intro, *planned]


class PlanError(ValueError):
    pass


def _key(name: str) -> str:
    return " ".join(name.lower().split())


def build_plan(setup: InterviewSetup, generated: GeneratedPlan, source: str) -> Plan:
    if setup.competencies:  # the recruiter's criteria win over anything generated
        competencies = [
            Competency(id=f"c{i + 1}", name=c.name.strip(), description=c.description.strip(), rubric=c.rubric.strip())
            for i, c in enumerate(setup.competencies)
        ]
    else:
        seen: set[str] = set()
        competencies = []
        for c in generated.competencies:
            if c.name.strip() and _key(c.name) not in seen and len(competencies) < 6:
                seen.add(_key(c.name))
                competencies.append(Competency(id=f"c{len(competencies) + 1}", name=c.name.strip(), description=c.description.strip()))
    if not competencies:
        raise PlanError("The plan has no competencies")
    by_name = {_key(c.name): c.id for c in competencies}

    questions: list[PlannedQuestion] = []
    for g in generated.questions:
        text = " ".join(g.text.split())
        if not text:
            continue
        ids = list(dict.fromkeys(by_name[_key(n)] for n in g.competency_names if _key(n) in by_name))
        if not ids:  # unmapped: give it to the competency with the fewest questions so far
            counts = {c.id: sum(c.id in q.competency_ids for q in questions) for c in competencies}
            ids = [min(counts, key=counts.get)]
        questions.append(
            PlannedQuestion(
                id=f"q{len(questions) + 1}",
                text=text[:MAX_QUESTION_CHARS],
                competency_ids=ids,
                look_for=[p.strip() for p in g.look_for if p.strip()][:MAX_LOOK_FOR],
            )
        )
        if len(questions) == setup.question_count:
            break
    if not questions:
        raise PlanError("The plan has no questions")
    return Plan(
        competencies=competencies,
        questions=questions,
        candidate_summary=generated.candidate_summary.strip()[:1500],
        source=source,
    )


async def generate_plan(ai: InterviewAI, setup: InterviewSetup) -> tuple[Plan, str | None]:
    """Generate a plan. If the AI fails, fall back to the labelled template and return the reason."""
    try:
        generated = await ai.generate_plan(setup)
        return build_plan(setup, generated, ai.served_name), None
    except (LLMError, PlanError) as e:
        if not ai.is_ai:
            raise
        logger.warning("plan generation with %s failed, using the template: %s", ai.name, e)
        fallback = OfflineAI()
        return build_plan(setup, await fallback.generate_plan(setup), fallback.name), str(e)


def validate_plan(plan: Plan) -> Plan:
    """Check a recruiter-edited plan; renumber question ids. Raises PlanError."""
    if not plan.competencies:
        raise PlanError("Add at least one competency")
    comp_ids = [c.id for c in plan.competencies]
    if len(set(comp_ids)) != len(comp_ids) or any(not c.name.strip() for c in plan.competencies):
        raise PlanError("Competencies need unique ids and names")
    if not plan.questions:
        raise PlanError("Add at least one question")
    if len(plan.questions) > MAX_QUESTIONS:
        raise PlanError(f"At most {MAX_QUESTIONS} questions")
    questions = []
    for i, q in enumerate(plan.questions):
        text = " ".join(q.text.split())
        if not text:
            raise PlanError(f"Question {i + 1} is empty")
        if len(text) > MAX_QUESTION_CHARS:
            raise PlanError(f"Question {i + 1} is too long to be asked aloud")
        unknown = [c for c in q.competency_ids if c not in comp_ids]
        if unknown or not q.competency_ids:
            raise PlanError(f"Question {i + 1} must map to at least one listed competency")
        if q.max_follow_ups is not None and not 0 <= q.max_follow_ups <= 5:
            raise PlanError(f"Question {i + 1}: follow-ups must be between 0 and 5")
        questions.append(
            q.model_copy(update={"id": f"q{i + 1}", "text": text, "look_for": [p.strip() for p in q.look_for if p.strip()][:MAX_LOOK_FOR]})
        )
    return plan.model_copy(update={"questions": questions})


def context_for(interview: Interview) -> InterviewContext:
    if interview.plan is None:
        raise PlanError("This interview has no plan")
    s = interview.setup
    return InterviewContext(
        interview_id=interview.id,
        role_title=s.role_title,
        level=s.level,
        job_description=s.job_description,
        interview_type=s.interview_type,
        candidate_name=s.candidate_name,
        candidate_summary=interview.plan.candidate_summary,
        competencies=interview.plan.competencies,
        # The analyst mode's bot never speaks, so it asks nothing; every AI interview opens with the introduction.
        questions=interview.plan.questions if interview.options.mode == "analyst" else with_intro(interview.plan.questions),
        duration_minutes=s.duration_minutes,
        requirements=[Requirement(id=f"r{n}", text=text) for n, text in enumerate(s.requirements, 1)],
        mode=interview.options.mode,
        candidate_meeting_name=interview.options.candidate_meeting_name,
        questions_in_chat=interview.options.questions_in_chat,
        record_video=interview.options.record_video,
        format=interview.options.format,
        require_camera=interview.options.require_camera,
        require_screen_share=interview.options.require_screen_share,
    )
