"""Demo data: installs a consistent sample workspace, idempotently, labelled, inert, and removable without touching real data."""

from datetime import timedelta

from fastapi.testclient import TestClient

from app import demo
from app.llm import OfflineAI
from app.models import ApplicationInput, RoleSetup, utcnow
from app.security import permitted
from app.server import create_app
from app.store import Store
from tests.fakes import MEET_LINK
from tests.test_api import TUNNEL


def test_install_is_idempotent_and_remove_leaves_real_data_alone(store, setup):
    real_role = store.create_role(RoleSetup(role_title="Real role", job_description="Real JD"))
    real_interview = store.create_interview(setup)
    real_app = store.create_application(real_role.id, ApplicationInput(full_name="Real Person", email="real@example.org"))
    store.audit("interview_created", real_interview.id)

    first = demo.install(store)
    assert first == {"roles": 3, "interviews": 12, "applications": 11, "reports": 7}
    again = demo.install(store)  # no duplicates
    assert again == first and demo.status(store) == {"installed": True, "roles": 3, "interviews": 12, "applications": 11}
    assert len(store.list_roles()) == 4 and len(store.list_interviews()) == 13

    removed = demo.remove(store)
    assert removed == {"roles": 3, "interviews": 12, "applications": 11} and not demo.status(store)["installed"]
    assert [r.id for r in store.list_roles()] == [real_role.id]
    assert store.get_interview(real_interview.id) is not None and store.get_application(real_app.id) is not None
    assert [e["action"] for e in store.audit_log(50, real_interview.id)] == ["interview_created"]
    assert not any(e["detail"].startswith("[demo]") for e in store.audit_log(200))


def test_demo_records_are_labelled_and_connected(store):
    demo.install(store)
    rows = store.list_interviews()
    assert rows and all(r["demo"] for r in rows)
    backend = next(r for r in store.list_roles() if r.setup.role_title == "Senior Backend Engineer")
    from app import roles
    pipe = roles.pipeline(store, backend)
    stages = {c["candidate_name"]: c["stage"] for c in pipe["candidates"]}
    assert stages == {"Aisha Khan": "passed", "Marcus Lee": "passed", "Elena Petrova": "failed", "Tomás García": "invited",
                      "Daniel Okoye": "invitation_expired", "Sofia Rossi": "needs_review", "Liam Chen": "not_eligible"}
    ranked = [(r["candidate_name"], r["score"], r["passed"]) for r in pipe["ranking"]["ranked"]]
    assert ranked == [("Aisha Khan", 87, True), ("Marcus Lee", 73, True), ("Elena Petrova", 53, False)]
    assert pipe["summary"]["applied"] == 7 and pipe["summary"]["needs_review"] == 1
    assert pipe["summary"]["to_review"] == sum(c["stage"] in ("screening", "needs_review", "not_eligible", "eligible") for c in pipe["candidates"]) == 2
    # Every report points at its interview and session; demo evaluations say they were written by hand.
    for c in pipe["candidates"]:
        if c["session_id"]:
            report = store.get_report(c["session_id"])
            assert report.interview_id == c["interview_id"] and report.generated_by.startswith("Demo data")
            for score in report.evaluation.competency_scores:  # evidence is the candidate's own words
                assert any(score.evidence[0] in u.text for u in report.transcript if u.role == "candidate")
    designer = next(r for r in store.list_roles() if r.setup.role_title == "Product Designer")
    d_stages = {c["candidate_name"]: c["stage"] for c in roles.pipeline(store, designer)["candidates"]}
    assert d_stages["Grace Kim"] == "evaluation_failed" and d_stages["Lucas Martin"] == "scheduled"
    assert store.activity(utcnow() - timedelta(days=30))["completed"]  # the dashboard chart has something to show


def test_demo_data_never_reaches_a_meeting_an_inbox_or_the_public(settings):
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    store = app.state.store
    with TestClient(app) as c:
        assert c.post("/api/demo").json()["installed"] is True
        interviews = {i["candidate_name"]: i for i in c.get("/api/interviews").json()}
        nina = interviews["Nina Patel"]["id"]  # ready to launch, were it real
        r = c.post(f"/api/interviews/{nina}/launch", json={"meeting_link": MEET_LINK, "consent_attested": True})
        assert r.status_code == 409 and "demo data" in r.json()["detail"]
        assert c.post(f"/api/interviews/{nina}/invite", json={"meeting_link": MEET_LINK}).status_code == 409
        tomas = store.get_interview(interviews["Tomás García"]["id"])
        assert c.get(f"/api/public/interview/{tomas.invitation.token}", headers=TUNNEL).status_code == 404
        backend = next(r for r in store.list_roles() if r.demo and r.hiring.apply_token)
        assert c.get(f"/api/public/apply/{backend.hiring.apply_token}", headers=TUNNEL).status_code == 404
        assert store.due_interviews(utcnow() + timedelta(days=3)) == []  # the scheduled demo interview never launches
        sofia = next(a for r in store.list_roles() for a in store.applications_for_role(r.id) if a.candidate.full_name == "Sofia Rossi")
        assert c.post(f"/api/applications/{sofia.id}/decision", json={"action": "invite"}).status_code == 409
        assert c.delete("/api/demo").json()["installed"] is False
        assert c.get("/api/interviews").json() == []
    for role in ("viewer", "recruiter"):
        assert permitted(role, "GET", "/api/demo") and not permitted(role, "POST", "/api/demo") and not permitted(role, "DELETE", "/api/demo")


def test_demo_data_doesnt_count_as_an_existing_install(store):
    demo.install(store)
    assert store.has_data() is False  # a restart during setup still shows the wizard
