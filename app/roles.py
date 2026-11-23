"""Roles: one approved question set used for every candidate interviewed for a job.

A candidate added to a role becomes an ordinary interview with a copy of the role's
approved plan and `role_id` set. Its questions can't be changed on the interview, so every
candidate gets the same structured interview; changing the role's plan affects candidates
added afterwards only. The comparison view lines the candidates up on the role's
competencies and must-have requirements.
"""

from __future__ import annotations

from typing import Optional

from app import lifecycle
from app.models import CandidateInput, Interview, InterviewSetup, Plan, Role, RoleSetup, utcnow
from app.store import Store

PLACEHOLDER_CANDIDATE = "the candidate"  # role plans are written before any candidate is known


def planning_setup(setup: RoleSetup) -> InterviewSetup:
    """The role as an interview setup for plan generation: no candidate, no resume."""
    return InterviewSetup(**setup.model_dump(), candidate_name=PLACEHOLDER_CANDIDATE)


def role_plan(plan: Plan) -> Plan:
    """A generated plan as a role's plan: the resume summary belongs to a candidate, not the role."""
    return plan.model_copy(update={"candidate_summary": ""})


def add_candidate(store: Store, role: Role, candidate: CandidateInput, assigned: str = "manual") -> Interview:
    """Create the candidate's interview from the role's approved plan. Raises ValueError if it isn't approved."""
    if role.plan is None or role.plan_approved_at is None:
        raise ValueError("Approve the role's questions before adding candidates")
    setup = InterviewSetup(**role.setup.model_dump(), **candidate.model_dump())
    plan = role.plan.model_copy(update={"source": f"role: {role.setup.role_title}"})
    return store.create_interview(setup, plan=plan, plan_approved_at=utcnow(), status="ready", role_id=role.id, assigned=assigned)


def candidate_setup(role: Role, update: InterviewSetup) -> InterviewSetup:
    """A role interview's setup after an edit: candidate fields may change, the role's never do."""
    return InterviewSetup(**role.setup.model_dump(), **CandidateInput(**update.model_dump()).model_dump())


def comparison(store: Store, role: Role) -> list[dict]:
    """One row per candidate: status, decision and the latest report's recommendation, scores and verdicts."""
    rows = []
    for i in store.interviews_for_role(role.id):
        report = store.get_report(i.latest_session_id) if i.latest_session_id else None
        ev = report.evaluation if report else None
        rows.append({
            "interview_id": i.id,
            "candidate_name": i.setup.candidate_name,
            "status": i.status,
            "stage": lifecycle.stage(i, bool(report and report.evaluation)),
            "decision": i.decision,
            "session_id": i.latest_session_id,
            "end_reason": report.end_reason if report else None,
            "recommendation": ev.recommendation if ev else None,
            "confidence": ev.confidence if ev else None,
            "scores": {s.competency_id: s.score for s in ev.competency_scores if s.score is not None} if ev else {},
            "verdicts": {r.requirement_id: r.verdict for r in ev.requirement_results} if ev else {},
        })
    return rows


RANKING_METHOD = (
    "Only completed, evaluated interviews are ranked. Score = average of the 1-5 competency scores x 20 "
    "(so 3/5 is 60/100). Pass = score at or above the role's passing score, with every competency assessed; "
    "a candidate with an unassessed competency has no result and ranks after the others. Ties: more must-haves met, then "
    "fewer not met; still tied, the same rank. Advisory: the decision is the hiring team's."
)


def score_100(overall: float) -> int:
    """The 1-5 average as a score out of 100 (x 20), so a pass mark can be set in familiar terms."""
    return round(overall * 20)


def ranking(store: Store, role: Role) -> dict:
    """The role's leaderboard and comparative report, from the stored evaluations only (nothing is generated)."""
    comps = role.plan.competencies if role.plan else []
    reqs = [f"r{n}" for n in range(1, len(role.setup.requirements) + 1)]
    ranked, unranked = [], []
    for i in store.interviews_for_role(role.id):
        report = store.get_report(i.latest_session_id) if i.latest_session_id else None
        ev = report.evaluation if report else None
        row = {"interview_id": i.id, "candidate_name": i.setup.candidate_name, "status": i.status,
               "session_id": i.latest_session_id, "decision": i.decision, "decision_note": i.decision_note}
        if ev is None:
            reason = ("Interview scheduled" if i.status == "scheduled" else "Interview in progress" if i.status == "in_progress"
                      else "Not interviewed yet" if report is None else f"Not evaluated: {report.evaluation_error or 'no evaluation'}")
            unranked.append({**row, "reason": reason})
            continue
        scores = {s.competency_id: s.score for s in ev.competency_scores if s.score is not None}
        if not scores:
            reason = "Not assessed: no competency had evidence" if ev.competency_scores else "No competency scores in the evaluation"
            unranked.append({**row, "reason": reason})
            continue
        verdicts = {r.requirement_id: r.verdict for r in ev.requirement_results}
        overall = round(sum(scores.values()) / len(scores), 2)
        ranked.append({
            **row,
            "overall": overall,
            "score": score_100(overall),
            "passed": None if ev.unassessed else score_100(overall) >= role.hiring.passing_score,  # None: no result yet
            "scores": scores,
            "evidence": {s.competency_id: s.evidence[:2] for s in ev.competency_scores},
            "verdicts": verdicts,
            "meets": sum(v == "meets" for v in verdicts.values()),
            "does_not_meet": sum(v == "does_not_meet" for v in verdicts.values()),
            "not_enough_evidence": sum(v == "not_enough_evidence" for v in verdicts.values()) + sum(r not in verdicts for r in reqs),
            "missing_competencies": [c.name for c in comps if c.id not in scores],
            "recommendation": ev.recommendation,
            "summary": ev.summary,
            "confidence": ev.confidence,
            "strengths": ev.strengths[:3],
            "concerns": ev.areas_for_improvement[:3],
        })
    ranked.sort(key=lambda r: (r["passed"] is None, -r["overall"], -r["meets"], r["does_not_meet"], r["candidate_name"].lower()))
    previous, rank = None, 0
    for n, r in enumerate(ranked, 1):
        key = (r["passed"] is None, r["overall"], r["meets"], r["does_not_meet"])
        if key != previous:
            rank, previous = n, key
        r["rank"] = rank
    competencies = []
    for c in comps:
        scored = [(r["candidate_name"], r["scores"][c.id]) for r in ranked if c.id in r["scores"]]
        top = max((s for _, s in scored), default=None)
        competencies.append({"id": c.id, "name": c.name, "top": top,
                             "leaders": [name for name, s in scored if s == top] if top is not None else []})
    further = []
    for r in ranked:
        open_reqs = [role.setup.requirements[int(k[1:]) - 1] for k in reqs if r["verdicts"].get(k, "not_enough_evidence") == "not_enough_evidence"]
        if open_reqs or r["missing_competencies"] or r["confidence"] == "low":
            further.append({"candidate_name": r["candidate_name"], "requirements": open_reqs,
                            "competencies": r["missing_competencies"], "low_confidence": r["confidence"] == "low"})
    return {
        "method": RANKING_METHOD,
        "passing_score": role.hiring.passing_score,
        "passed": [r["candidate_name"] for r in ranked if r["passed"]],
        "failed": [r["candidate_name"] for r in ranked if r["passed"] is False],
        "incomplete": [r["candidate_name"] for r in ranked if r["passed"] is None],
        "ready": len(ranked) >= 2,  # a comparison needs at least two evaluated candidates
        "ranked": ranked,
        "unranked": unranked,
        "competencies": competencies,
        "requirements": [{"id": k, "text": t} for k, t in zip(reqs, role.setup.requirements)],
        "further_assessment": further,
    }


def summary(role: Role, candidates: Optional[int] = None) -> dict:
    return {
        "id": role.id,
        "role_title": role.setup.role_title,
        "level": role.setup.level,
        "interview_type": role.setup.interview_type,
        "questions": len(role.plan.questions) if role.plan else 0,
        "approved": role.plan_approved_at is not None,
        "candidates": candidates,
        "updated_at": role.updated_at.isoformat(),
        "demo": role.demo,
    }


# Where each candidate of a role stands, from application to result. One label each, in pipeline order.
PIPELINE_STAGES = {
    "screening": "Screening",
    "needs_review": "Needs review",
    "not_eligible": "Not eligible",
    "eligible": "Eligible",
    "invited": "Invited",
    "invitation_expired": "Invitation expired",
    "scheduled": "Scheduled",
    "in_progress": "In progress",
    "evaluation_pending": "Evaluation pending",
    "evaluation_failed": "Evaluation failed",
    "passed": "Passed",
    "failed": "Did not pass",
    "incomplete": "Needs more evidence",
    "interview_failed": "Interview failed",
    "ready": "Not started",
    "withdrawn": "Withdrawn",
    "cancelled": "Cancelled",
}


def pipeline(store: Store, role: Role) -> dict:
    """Every candidate of the role (applied or added by the recruiter), their stage, and the ranking where evaluated."""
    rank = ranking(store, role)
    by_interview = {r["interview_id"]: r for r in rank["ranked"]}
    unranked = {r["interview_id"]: r for r in rank["unranked"]}
    interviews = {i.id: i for i in store.interviews_for_role(role.id)}
    rows = []
    for a in store.applications_for_role(role.id):
        i = interviews.pop(a.interview_id, None) if a.interview_id else None
        rows.append(_row(a, i, by_interview, unranked))
    rows += [_row(None, i, by_interview, unranked) for i in interviews.values()]
    counts = {k: 0 for k in PIPELINE_STAGES}
    for r in rows:
        counts[r["stage"]] += 1
    applications = [r for r in rows if r["application_id"]]
    summary = {
        "applied": len(applications),
        "eligible": sum(r["screening"] == "eligible" for r in applications),
        "needs_review": counts["needs_review"],
        "not_eligible": counts["not_eligible"],
        # Waiting on a recruiter: screening, unclear, rejected by the screener (to confirm or override), or eligible
        # and not invited yet. The same set as the role page's "To review" filter.
        "to_review": counts["screening"] + counts["needs_review"] + counts["not_eligible"] + counts["eligible"],
        "invited": sum(r["invited"] for r in rows),
        "awaiting": counts["invited"] + counts["eligible"] + counts["scheduled"] + counts["ready"] + counts["interview_failed"],
        "in_progress": counts["in_progress"],
        "completed": counts["passed"] + counts["failed"] + counts["incomplete"],
        "evaluation_pending": counts["evaluation_pending"] + counts["evaluation_failed"],
        "passed": counts["passed"],
        "failed": counts["failed"],
        "incomplete": counts["incomplete"],
    }
    order = list(PIPELINE_STAGES)
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, order.index(r["stage"]), r["candidate_name"].lower()))
    return {"candidates": rows, "summary": summary, "stages": PIPELINE_STAGES, "ranking": rank}


def _row(a, i, by_interview: dict, unranked: dict) -> dict:
    r = by_interview.get(i.id) if i else None
    stage = _stage(a, i, r, unranked[i.id]["reason"] if i and i.id in unranked else "")
    inv = i.invitation if i else None
    return {
        "candidate_name": a.candidate.full_name if a else i.setup.candidate_name,
        "email": a.candidate.email if a else (i.setup.candidate_email if i else ""),
        "application_id": a.id if a else None,
        "applied_at": a.created_at.isoformat() if a else None,
        "screening_score": a.screening.score if a and a.screening else None,
        "screening_failed": bool(a and a.screening and a.screening.error),
        "screening": a.screening.outcome if a and a.screening else (a.status if a else None),
        "interview_id": i.id if i else None,
        "session_id": i.latest_session_id if i else None,
        "stage": stage,
        "invited": bool(inv),
        "invitation_expires_at": inv.expires_at.isoformat() if inv else None,
        "invitation_email": ("sent" if inv.email_sent_at else ("failed: " + inv.email_error) if inv.email_error else "not sent") if inv else None,
        "invitation_undelivered": bool(inv and inv.undelivered),
        "rank": r["rank"] if r else None,
        "score": r["score"] if r else None,
        "passed": r["passed"] if r else None,
        "reason": unranked[i.id]["reason"] if i and i.id in unranked else "",
        "decision": i.decision if i else None,
        "demo": bool((a and a.demo) or (i and i.demo)),
    }


def _stage(a, i, ranked_row, reason: str) -> str:
    if a is not None and a.status == "withdrawn":
        return "withdrawn"
    if i is None:
        return a.status if a.status in ("screening", "needs_review", "not_eligible") else "eligible"
    if ranked_row:
        return "incomplete" if ranked_row["passed"] is None else "passed" if ranked_row["passed"] else "failed"
    s = lifecycle.stage(i, False)
    if s == "evaluation_pending":
        if reason.startswith("Not assessed"):
            return "incomplete"  # it was evaluated, and the evaluation found nothing to score: running it again won't help
        return "evaluation_failed" if reason.startswith(("Not evaluated", "No competency")) else "evaluation_pending"
    return {"invitation_sent": "invited", "pending_candidate": "invited", "expired": "invitation_expired", "draft": "ready",
            "failed": "interview_failed"}.get(s, s)
