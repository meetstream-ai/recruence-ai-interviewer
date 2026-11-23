"""Talk-time and key moments, derived from a report's transcript and evaluation."""

import json

import sqlalchemy as sa

from app.store import reports

from app.models import CompetencyScore, EvaluationReport, LLMEvaluation, RequirementResult, Utterance, utcnow


def report(lines, evaluation=None, source="recording"):
    now = utcnow()
    return EvaluationReport(
        session_id="s1", interview_id="i1", role_title="r", candidate_name="Priya", platform="Google Meet",
        started_at=now, ended_at=now, duration_seconds=60, questions_planned=2, questions_asked=2, follow_ups_asked=0,
        end_reason="completed", consent_given=True, generated_by="test", evaluation=evaluation, notes=[],
        transcript=[Utterance(speaker=s, role=role, text=t, question_id=q) for s, role, t, q in lines],
        transcript_source=source,
    )


LINES = [
    ("AI Interviewer", "interviewer", "Tell me about an outage you handled.", "q1"),       # 7 words
    ("Priya", "candidate", "We lost the payments database at night.", "q1"),               # 7
    ("Priya", "candidate", "I rolled back the deploy within ten minutes.", "q1"),          # 8
    ("System", "system", "The candidate agreed.", None),                                  # not counted
    ("AI Interviewer", "interviewer", "How do you mentor engineers?", "q2"),               # 5
    ("Priya", "candidate", "Weekly pairing sessions and design reviews.", "q2"),           # 6
    ("Priya", "candidate", "Thanks.", None),                                              # 1, unassigned
]


def evaluation():
    return LLMEvaluation(
        recommendation="yes", summary="s", confidence="medium", strengths=[], areas_for_improvement=[], suggested_next_round_focus=[],
        competency_scores=[
            CompetencyScore(competency_id="c1", competency_name="Ownership", score=4, rationale="r",
                            evidence=["rolled back the deploy within ten minutes", "We lost the payments database at night. I rolled back"]),
            CompetencyScore(competency_id="c2", competency_name="Mentoring", score=3, rationale="r", evidence=["weekly PAIRING sessions"]),
        ],
        requirement_results=[RequirementResult(requirement_id="r1", requirement="Led incident response", verdict="meets",
                                               rationale="r", evidence=["I rolled back the deploy"])],
    )


def test_talk_time_counts_words_by_role_and_question():
    t = report(LINES).talk
    assert (t["interviewer_words"], t["candidate_words"]) == (12, 22)
    assert t["candidate_share"] == round(22 / 34, 2) and t["unit"] == "words" and t["basis"] == "recording"
    assert t["per_question"] == [{"question_id": "q1", "candidate_words": 15}, {"question_id": "q2", "candidate_words": 6}]
    assert t["unassigned_candidate_words"] == 1
    assert report([], source="live").talk["candidate_share"] is None


def test_key_moments_point_at_the_lines_behind_the_evidence():
    moments = report(LINES, evaluation()).key_moments
    assert [(m["line"], m["label"]) for m in moments] == [
        (1, "Ownership"),               # quote spanning two lines: placed on the line with its opening words
        (2, "Ownership"),
        (2, "Led incident response"),   # requirement evidence too
        (5, "Mentoring"),               # case and punctuation don't matter
    ]
    assert report(LINES).key_moments == []  # no evaluation, no moments


def test_derived_fields_are_served_but_not_needed_when_reading_back(store, ready_interview):
    r = report(LINES, evaluation()).model_copy(update={"interview_id": ready_interview.id})
    store.create_session("s1", ready_interview.id, "Google Meet")
    store.push_report(r)
    dumped = r.model_dump(mode="json")
    assert dumped["talk"]["candidate_words"] == 22 and len(dumped["key_moments"]) == 4
    stored = json.loads(store._rows(sa.select(reports.c.data).where(reports.c.session_id == "s1"))[0]["data"])
    assert "talk" not in stored and "key_moments" not in stored  # derived, so not stored
    back = store.get_report("s1")
    assert back.talk == r.talk and back.key_moments == r.key_moments


def test_quote_spanning_lines_lands_where_it_starts_not_on_a_lookalike():
    lines = [
        ("Priya", "candidate", "I think we should test first.", "q1"),                    # same opening, unrelated
        ("Priya", "candidate", "Later I said I think we should roll", "q1"),
        ("Priya", "candidate", "back, because the deploy broke checkout.", "q1"),
    ]
    ev = evaluation().model_copy(update={"requirement_results": [], "competency_scores": [
        CompetencyScore(competency_id="c1", competency_name="Ownership", score=4, rationale="r",
                        evidence=["I think we should roll back, because the deploy broke checkout", "a quote nobody said"])]})
    assert [(m["line"], m["quote"][:20]) for m in report(lines, ev).key_moments] == [(1, "I think we should ro")]
