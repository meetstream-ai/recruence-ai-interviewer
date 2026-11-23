"""Roles: one approved question set used for every candidate, and the comparison across candidates."""

from app.models import CompetencyScore, EvaluationReport, LLMEvaluation, RequirementResult, utcnow
from app.security import permitted
from tests.test_api import make_client

ROLE = {
    "role_title": "Backend Engineer",
    "level": "Senior",
    "job_description": "Build Python APIs on PostgreSQL and own them in production.",
    "interview_type": "behavioral",
    "competencies": [{"name": "Ownership", "description": "Owns outcomes"}, {"name": "Communication"}],
    "requirements": ["3+ years of Python in production"],
    "question_count": 2,
    "duration_minutes": 15,
}


def approved_role(c):
    rid = c.post("/api/roles", json=ROLE).json()["id"]
    assert c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Too Early"}).status_code == 409  # not approved
    plan = c.post(f"/api/roles/{rid}/plan/generate").json()["role"]["plan"]
    assert plan["candidate_summary"] == "" and len(plan["questions"]) == 2
    assert c.post(f"/api/roles/{rid}/plan/approve").json()["plan_approved_at"]
    return rid, plan


def test_every_candidate_gets_the_roles_questions(settings):
    _, c = make_client(settings)
    with c:
        rid, plan = approved_role(c)
        a = c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Ana Silva", "resume_text": "Eight years of Python."}).json()
        b = c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Sam Okafor"}).json()
        for i in (a, b):
            assert i["role_id"] == rid and i["status"] == "ready" and i["plan_approved_at"]
            assert [q["text"] for q in i["plan"]["questions"]] == [q["text"] for q in plan["questions"]]
            assert i["setup"]["role_title"] == "Backend Engineer" and i["setup"]["requirements"] == ROLE["requirements"]
        assert a["setup"]["resume_text"] == "Eight years of Python."

        # The questions can't be changed on a candidate's interview.
        assert c.post(f"/api/interviews/{a['id']}/plan/generate").status_code == 409
        assert c.put(f"/api/interviews/{a['id']}/plan", json=a["plan"]).status_code == 409
        # Editing a candidate changes only the candidate's details and keeps the approval.
        edited = c.put(f"/api/interviews/{a['id']}/setup", json={**a["setup"], "candidate_name": "Ana Souza", "role_title": "Hacked", "question_count": 9}).json()
        assert edited["setup"]["candidate_name"] == "Ana Souza" and edited["setup"]["role_title"] == "Backend Engineer"
        assert edited["setup"]["question_count"] == 2 and edited["status"] == "ready" and edited["plan_approved_at"]

        # Changing the role's questions affects candidates added afterwards only.
        new_plan = {**plan, "questions": [{**plan["questions"][0], "text": "A brand new first question?"}, plan["questions"][1]]}
        assert c.put(f"/api/roles/{rid}/plan", json=new_plan).json()["plan_approved_at"] is None
        assert c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Late"}).status_code == 409  # re-approve first
        c.post(f"/api/roles/{rid}/plan/approve")
        late = c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Late"}).json()
        assert late["plan"]["questions"][0]["text"] == "A brand new first question?"
        assert c.get(f"/api/interviews/{b['id']}").json()["interview"]["plan"]["questions"][0]["text"] == plan["questions"][0]["text"]

        listed = c.get("/api/roles").json()
        assert [(r["role_title"], r["candidates"], r["approved"], r["questions"]) for r in listed] == [("Backend Engineer", 3, True, 2)]
        assert {i["role_id"] for i in c.get("/api/interviews").json()} == {rid}  # the interview list marks role candidates


def test_comparison_lines_candidates_up(settings):
    app, c = make_client(settings)
    store = app.state.store
    with c:
        rid, plan = approved_role(c)
        ana = c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Ana Silva"}).json()
        c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Sam Okafor"})
        comp_ids = [x["id"] for x in plan["competencies"]]
        now = utcnow()
        store.create_session("s-ana", ana["id"], "Google Meet")
        store.push_report(EvaluationReport(
            session_id="s-ana", interview_id=ana["id"], role_title="Backend Engineer", candidate_name="Ana Silva", platform="Google Meet",
            started_at=now, ended_at=now, duration_seconds=600, questions_planned=2, questions_asked=2, follow_ups_asked=1,
            end_reason="completed", consent_given=True, generated_by="test", notes=[], transcript=[],
            evaluation=LLMEvaluation(
                recommendation="yes", summary="s", confidence="medium", strengths=[], areas_for_improvement=[], suggested_next_round_focus=[],
                competency_scores=[CompetencyScore(competency_id=comp_ids[0], competency_name="Ownership", score=4, rationale="r", evidence=[]),
                                   CompetencyScore(competency_id=comp_ids[1], competency_name="Communication", score=3, rationale="r", evidence=[])],
                requirement_results=[RequirementResult(requirement_id="r1", requirement="3+ years", verdict="meets", rationale="r", evidence=["x"])])))
        store.save_interview(store.get_interview(ana["id"]).model_copy(update={"latest_session_id": "s-ana", "status": "completed", "decision": "advance"}))

        rows = c.get(f"/api/roles/{rid}").json()["candidates"]
        assert [r["candidate_name"] for r in rows] == ["Ana Silva", "Sam Okafor"]  # in the order they were added
        assert rows[0]["recommendation"] == "yes" and rows[0]["decision"] == "advance" and rows[0]["end_reason"] == "completed"
        assert rows[0]["scores"] == {comp_ids[0]: 4, comp_ids[1]: 3} and rows[0]["verdicts"] == {"r1": "meets"}
        assert rows[1]["recommendation"] is None and rows[1]["scores"] == {} and rows[1]["status"] == "ready"


def test_deleting_a_role(settings):
    _, c = make_client(settings)
    with c:
        rid, _ = approved_role(c)
        iid = c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Ana"}).json()["id"]
        assert c.delete(f"/api/roles/{rid}").status_code == 409  # candidates first
        assert c.delete(f"/api/interviews/{iid}").status_code == 200
        assert c.delete(f"/api/roles/{rid}").status_code == 200
        assert c.get(f"/api/roles/{rid}").status_code == 404
    assert not permitted("recruiter", "DELETE", f"/api/roles/{rid}") and permitted("recruiter", "POST", "/api/roles")
    assert not permitted("viewer", "POST", f"/api/roles/{rid}/candidates") and permitted("viewer", "GET", "/api/roles")


def test_role_setup_change_withdraws_approval(settings):
    _, c = make_client(settings)
    with c:
        rid, _ = approved_role(c)
        saved = c.put(f"/api/roles/{rid}/setup", json={**ROLE, "duration_minutes": 30}).json()
        assert saved["setup"]["duration_minutes"] == 30 and saved["plan_approved_at"] is None
        assert c.post("/api/roles", json={**ROLE, "candidate_name": "ignored", "role_title": ""}).status_code == 422


def evaluated(store, interview_id, name, scores, verdict, comp_ids, recommendation="yes", confidence="medium"):
    now = utcnow()
    sid = f"s-{interview_id}"
    store.create_session(sid, interview_id, "Google Meet")
    store.push_report(EvaluationReport(
        session_id=sid, interview_id=interview_id, role_title="Backend Engineer", candidate_name=name, platform="Google Meet",
        started_at=now, ended_at=now, duration_seconds=600, questions_planned=2, questions_asked=2, follow_ups_asked=0,
        end_reason="completed", consent_given=True, generated_by="test", notes=[], transcript=[],
        evaluation=LLMEvaluation(
            recommendation=recommendation, summary="s", confidence=confidence, strengths=[f"{name} strength"], areas_for_improvement=[], suggested_next_round_focus=[],
            competency_scores=[CompetencyScore(competency_id=cid, competency_name=cid, score=s, rationale="r", evidence=[f"{name} said {cid}"])
                               for cid, s in zip(comp_ids, scores)],
            requirement_results=[RequirementResult(requirement_id="r1", requirement="3+ years", verdict=verdict, rationale="r", evidence=[])])))
    store.save_interview(store.get_interview(interview_id).model_copy(update={"latest_session_id": sid, "status": "completed"}))


def test_ranking_ranks_only_evaluated_candidates_and_explains_itself(settings):
    app, c = make_client(settings)
    store = app.state.store
    with c:
        rid, plan = approved_role(c)
        ids = {n: c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": n}).json()["id"] for n in ("Ana Silva", "Sam Okafor", "Lee Park", "Ravi Nair")}
        comp_ids = [x["id"] for x in plan["competencies"]]
        evaluated(store, ids["Ana Silva"], "Ana Silva", [4, 3], "not_enough_evidence", comp_ids)
        evaluated(store, ids["Sam Okafor"], "Sam Okafor", [4, 3], "meets", comp_ids)  # same average, meets the requirement
        evaluated(store, ids["Lee Park"], "Lee Park", [5, 4], "does_not_meet", comp_ids, recommendation="no", confidence="low")
        # Ravi was never interviewed.
        r = c.get(f"/api/roles/{rid}/ranking").json()
        assert r["ready"] is True and "Advisory" in r["method"] and r["passing_score"] == 70
        assert [(x["rank"], x["candidate_name"], x["overall"], x["score"], x["passed"]) for x in r["ranked"]] == [
            (1, "Lee Park", 4.5, 90, True), (2, "Sam Okafor", 3.5, 70, True), (3, "Ana Silva", 3.5, 70, True)]
        lee = r["ranked"][0]
        assert lee["does_not_meet"] == 1 and lee["meets"] == 0 and lee["not_enough_evidence"] == 0
        ana = r["ranked"][2]
        assert ana["not_enough_evidence"] == 1 and ana["does_not_meet"] == 0  # missing evidence isn't a failure
        assert r["unranked"] == [{**r["unranked"][0], "candidate_name": "Ravi Nair", "reason": "Not interviewed yet"}]
        assert r["competencies"][0]["leaders"] == ["Lee Park"] and r["competencies"][0]["top"] == 5
        names = {x["candidate_name"]: x for x in r["further_assessment"]}
        assert names["Ana Silva"]["requirements"] == ["3+ years of Python in production"] and names["Lee Park"]["low_confidence"]
        assert ana["evidence"][comp_ids[0]] == ["Ana Silva said " + comp_ids[0]]


def test_ranking_needs_two_evaluated_candidates(settings):
    app, c = make_client(settings)
    with c:
        rid, plan = approved_role(c)
        aid = c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Ana Silva"}).json()["id"]
        evaluated(app.state.store, aid, "Ana Silva", [4, 4], "meets", [x["id"] for x in plan["competencies"]])
        r = c.get(f"/api/roles/{rid}/ranking").json()
        assert r["ready"] is False and len(r["ranked"]) == 1


def test_an_unassessed_competency_blocks_a_pass_without_counting_as_a_fail(settings):
    """Live call 21: with unassessed competencies out of the average, an interview cut short could clear the pass mark."""
    app, c = make_client(settings)
    store = app.state.store
    with c:
        rid, plan = approved_role(c)
        ids = {n: c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": n}).json()["id"] for n in ("Ana Silva", "Sam Okafor")}
        comp_ids = [x["id"] for x in plan["competencies"]]
        evaluated(store, ids["Ana Silva"], "Ana Silva", [5, None], "meets", comp_ids)
        evaluated(store, ids["Sam Okafor"], "Sam Okafor", [4, 3], "meets", comp_ids)
        r = c.get(f"/api/roles/{rid}/ranking").json()
        assert [(x["rank"], x["candidate_name"], x["passed"]) for x in r["ranked"]] == [(1, "Sam Okafor", True), (2, "Ana Silva", None)]
        assert r["passed"] == ["Sam Okafor"] and r["failed"] == [] and r["incomplete"] == ["Ana Silva"]
        assert [x["competencies"] for x in r["further_assessment"] if x["candidate_name"] == "Ana Silva"][0]
        p = c.get(f"/api/roles/{rid}/pipeline").json()
        assert {x["candidate_name"]: x["stage"] for x in p["candidates"]} == {"Sam Okafor": "passed", "Ana Silva": "incomplete"}
        assert p["summary"]["completed"] == 2 and p["summary"]["incomplete"] == 1


def test_nothing_to_assess_isnt_a_failed_evaluation(settings):
    """It was evaluated, and found no evidence: "Evaluation failed" would send the recruiter to run it again for nothing."""
    app, c = make_client(settings)
    store = app.state.store
    with c:
        rid, plan = approved_role(c)
        iid = c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Ana Silva"}).json()["id"]
        evaluated(store, iid, "Ana Silva", [None, None], "meets", [x["id"] for x in plan["competencies"]])
        p = c.get(f"/api/roles/{rid}/pipeline").json()
        assert p["candidates"][0]["stage"] == "incomplete" and p["summary"]["incomplete"] == 1
