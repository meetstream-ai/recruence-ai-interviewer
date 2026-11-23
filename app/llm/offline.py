"""No-AI fallback, used when no LLM key is configured.

Plans come from fixed templates per competency. The "evaluation" is a crude
coverage heuristic over the plan's look-for points. Both are labelled as not AI in
their output and in the UI, so the workflow can be tried without a key.
"""

from __future__ import annotations

import re

from app.llm.base import EvaluationInput, InterviewAI
from app.models import (
    CompetencyScore,
    GeneratedCompetency,
    GeneratedPlan,
    GeneratedQuestion,
    InterviewSetup,
    LLMEvaluation,
)

DEFAULT_COMPETENCIES = {
    "behavioral": [
        ("Communication", "Explains situations and decisions clearly and concisely."),
        ("Collaboration", "Works effectively with others and handles disagreement constructively."),
        ("Problem solving", "Breaks problems down and reaches sound decisions."),
        ("Ownership", "Takes responsibility for outcomes and follows through."),
    ],
    "technical": [
        ("Technical depth", "Command of the core technologies and concepts the role needs."),
        ("Problem solving", "Approaches unfamiliar problems methodically and weighs trade-offs."),
        ("Communication", "Explains technical ideas clearly."),
    ],
    "screening": [
        ("Relevant experience", "Background matches the role's core requirements."),
        ("Motivation for the role", "Understands the role and has clear reasons for wanting it."),
        ("Communication", "Answers clearly and to the point."),
    ],
    "mixed": [
        ("Relevant experience", "Background matches the role's core requirements."),
        ("Problem solving", "Approaches problems methodically and weighs trade-offs."),
        ("Collaboration", "Works effectively with others."),
        ("Communication", "Explains ideas clearly and concisely."),
    ],
}

# Several wordings per type, so plans with more questions than competencies don't repeat themselves.
TEMPLATES = {
    "behavioral": [
        "Tell me about a specific time your {c} made a difference at work. What was the situation, what did you do, and what was the result?",
        "Describe a situation where your {c} was tested. What did you do, and what would you do differently now?",
        "Tell me about a time you had to improve your {c}. What prompted it and what changed?",
    ],
    "technical": [
        "For this {role} role, walk me through how you would approach a problem that tests your {c}. Which trade-offs would you consider?",
        "Tell me about a technical decision from your past work that depended on {c}. What options did you weigh and why did you choose the one you did?",
        "Describe a difficult technical problem involving {c} that you solved. How did you find the cause?",
    ],
    "screening": [
        "Looking at the {role} role, what in your background best shows your {c}?",
        "Which part of your recent work is most relevant to {c} in this {role} role, and why?",
        "What would you want the hiring team to know about your {c} that isn't obvious from your resume?",
    ],
}
LOOK_FOR = ["a specific, concrete example", "the candidate's own actions", "the outcome or what was learned"]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


class OfflineAI(InterviewAI):
    name = "template + heuristic (no AI)"
    is_ai = False

    async def generate_plan(self, setup: InterviewSetup) -> GeneratedPlan:
        comps = [GeneratedCompetency(name=c.name, description=c.description) for c in setup.competencies] or [
            GeneratedCompetency(name=n, description=d) for n, d in DEFAULT_COMPETENCIES[setup.interview_type]
        ]
        questions = []
        for i in range(setup.question_count):
            comp = comps[i % len(comps)]
            kind = setup.interview_type if setup.interview_type != "mixed" else ("behavioral", "technical")[i % 2]
            variants = TEMPLATES[kind]
            text = variants[(i // len(comps)) % len(variants)].format(c=comp.name.lower(), role=setup.role_title)
            questions.append(GeneratedQuestion(text=text, competency_names=[comp.name], look_for=list(LOOK_FOR)))
        return GeneratedPlan(
            candidate_summary="(Template plan: the resume was not analysed because no AI model is configured.)",
            competencies=comps,
            questions=questions,
        )

    async def evaluate(self, data: EvaluationInput) -> LLMEvaluation:
        said = " ".join(u.text for u in data.transcript if u.role == "candidate")
        words = _words(said)
        scores = []
        for comp in data.context.competencies:
            points = [p for q in data.context.questions if comp.id in q.competency_ids for p in q.look_for]
            hits = [p for p in points if any(w in words for w in _words(p) if len(w) > 3)]
            ratio = len(hits) / len(points) if points else 0.0
            scores.append(
                CompetencyScore(
                    competency_id=comp.id,
                    competency_name=comp.name,
                    score=max(1, min(5, 1 + round(ratio * 4))),
                    rationale=f"Heuristic, not AI: keyword overlap with {len(hits)} of {len(points)} look-for points.",
                    evidence=[],
                )
            )
        enough = len(said.split()) >= 60
        avg = sum(s.score for s in scores) / len(scores) if scores else 0
        rec = "insufficient_evidence" if not enough else ("yes" if avg >= 4 else "no")
        return LLMEvaluation(
            recommendation=rec,
            summary=(
                "NOT AN AI EVALUATION. No LLM key is configured, so scores are keyword overlap with the plan's "
                "look-for points. Read the transcript; configure ANTHROPIC_API_KEY or GEMINI_API_KEY for a real evaluation."
            ),
            competency_scores=scores,
            strengths=[],
            areas_for_improvement=[],
            suggested_next_round_focus=[s.competency_name for s in scores if s.score <= 3],
            confidence="low",
        )
