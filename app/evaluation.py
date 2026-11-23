"""Post-interview evaluation: ask the LLM, then enforce what code can check.

- Only the plan's competencies are kept, in plan order, with plan names.
- Scores are clamped to 1-5. A competency with no surviving quote has no score (not assessed): it
  wasn't shown to be weak, and a score must rest on the candidate's own words.
- Evidence quotes must appear in what the candidate actually said (tolerant of case,
  punctuation and spacing); anything else is dropped as unsupported.
- Every must-have requirement gets exactly one result. A "meets" or "does not meet"
  verdict without a surviving quote becomes "not enough evidence": a pass/fail call
  must rest on the candidate's own words.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Optional

from app.llm.base import EvaluationInput, InterviewAI, LLMError
from app.models import CompetencyScore, InterviewContext, LLMEvaluation, RequirementResult, Utterance

logger = logging.getLogger(__name__)
CLOSE_QUOTE = 0.85  # how closely (0-1) a quote must match a stretch of what was said


def squash(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def said_by_candidate(quote: str, said: str) -> bool:
    """Whether the quote is in what the candidate said. Speech recognition mishears a word here and there, so a quote of
    four words or more also counts when a stretch of the same length matches closely; an invented one doesn't."""
    wanted = squash(quote)
    if not wanted:
        return False
    if wanted in said:
        return True
    words, spoken = wanted.split(), said.split()
    if len(words) < 4:
        return False
    return any(SequenceMatcher(None, wanted, " ".join(spoken[i:i + len(words)]), autojunk=False).ratio() >= CLOSE_QUOTE
               for i in range(max(1, len(spoken) - len(words) + 1)))


def normalise_evaluation(ctx: InterviewContext, transcript: list[Utterance], ev: LLMEvaluation) -> LLMEvaluation:
    said = squash(" ".join(u.text for u in transcript if u.role == "candidate"))
    by_id = {s.competency_id: s for s in ev.competency_scores}
    scores = []
    for comp in ctx.competencies:
        s = by_id.get(comp.id)
        if s is None:
            logger.warning("evaluator omitted competency %s", comp.id)
            s = CompetencyScore(competency_id=comp.id, competency_name=comp.name, score=None, rationale="The evaluator gave no score.", evidence=[])
        evidence = [q for q in s.evidence if said_by_candidate(q, said)]
        if len(evidence) < len(s.evidence):
            logger.warning("dropped %d unsupported evidence quote(s) for %s", len(s.evidence) - len(evidence), comp.id)
        score = max(1, min(5, s.score)) if evidence and s.score is not None else None
        scores.append(s.model_copy(update={"score": score, "competency_name": comp.name, "evidence": evidence}))
    results_by_id = {r.requirement_id: r for r in ev.requirement_results}
    results = []
    for req in ctx.requirements:
        r = results_by_id.get(req.id)
        if r is None:
            logger.warning("evaluator omitted requirement %s", req.id)
            r = RequirementResult(requirement_id=req.id, verdict="not_enough_evidence", rationale="The evaluator gave no verdict.", evidence=[])
        evidence = [q for q in r.evidence if squash(q) and squash(q) in said]
        update = {"requirement": req.text, "evidence": evidence}
        if r.verdict != "not_enough_evidence" and not evidence:
            logger.warning("requirement %s: %s verdict without verbatim evidence; downgraded", req.id, r.verdict)
            update |= {"verdict": "not_enough_evidence", "rationale": f"No verbatim evidence for a verdict. Evaluator's note: {r.rationale}"}
        results.append(r.model_copy(update=update))
    return ev.model_copy(update={"competency_scores": scores, "requirement_results": results})


async def evaluate_interview(ai: InterviewAI, data: EvaluationInput) -> tuple[Optional[LLMEvaluation], Optional[str]]:
    """Returns (evaluation, None) or (None, error message). Never raises for model failures."""
    if not any(u.role == "candidate" for u in data.transcript):
        return None, "No candidate speech was captured, so there is nothing to evaluate."
    try:
        raw = await ai.evaluate(data)
    except LLMError as e:
        return None, str(e)
    return normalise_evaluation(data.context, data.transcript, raw), None
