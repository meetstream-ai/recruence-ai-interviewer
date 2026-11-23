"""Recruiting: public applications, eligibility screening, automatic invitations, the candidate's own interview link."""

import re
from dataclasses import replace
from datetime import timedelta

from fastapi.testclient import TestClient

from app.llm import OfflineAI
from app.models import CriterionResult, EligibilityCriterion, ScreeningOutput, utcnow
from app.recruiting import decide
from app.server import create_app
from app.store import Store
from tests.fakes import FakeMeetStream, MEET_LINK
from tests.test_api import TUNNEL, wait_for
from tests.test_roles import approved_role, evaluated

ZOOM = "https://zoom.us/j/1234567890"
CRITERIA = [{"text": "Degree in computer science", "mandatory": True}, {"text": "3+ years of Python", "mandatory": True},
            {"text": "Kubernetes", "mandatory": False}]
RESUME = b"Priya Raman. B.Tech in Computer Science, 2018. Five years of Python services at a fintech."


class ScreeningAI(OfflineAI):
    """Template plans (no AI) but scripted screening, as an AI provider would answer."""

    is_ai = True

    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {"e1": ("met", "B.Tech in Computer Science"), "e2": ("met", "Five years of Python services"),
                                     "e3": ("unclear", "")}
        self.calls = 0

    async def screen(self, system, user):
        self.calls += 1
        return ScreeningOutput(results=[CriterionResult(criterion_id=k, verdict=v, evidence=q, reasoning="r") for k, (v, q) in self.verdicts.items()],
                               summary="Python backend engineer.")


def client(settings, ai=None, mail=None):
    app = create_app(settings=settings, store=Store(settings.database), ai=ai or ScreeningAI(),
                     meetstream_client_factory=FakeMeetStream, run_loops=False, mail_send=mail)
    return app, TestClient(app)


def published_role(c, **hiring):
    rid, plan = approved_role(c)
    body = {"criteria": CRITERIA, "meeting_link": ZOOM, "published": True, "passing_score": 70, "invitation_days": 5, **hiring}
    r = c.put(f"/api/roles/{rid}/hiring", json=body)
    assert r.status_code == 200, r.text
    return rid, plan, r.json()["careers"]["job_key"]


def apply(c, token, email="priya@example.com", resume=RESUME, **fields):
    data = {"full_name": "Priya Raman", "email": email, "years_experience": "5", "consent": "yes", **fields}
    return c.post(f"/api/public/apply/{token}", data=data, files={"resume": ("cv.txt", resume, "text/plain")}, headers=TUNNEL)


def test_publishing_needs_approved_questions_and_a_room(settings):
    _, c = client(settings)
    with c:
        rid = c.post("/api/roles", json={"role_title": "X", "job_description": "Y"}).json()["id"]
        assert c.put(f"/api/roles/{rid}/hiring", json={"published": True, "meeting_link": ZOOM}).status_code == 409  # not approved
        rid, _ = approved_role(c)
        assert c.put(f"/api/roles/{rid}/hiring", json={"published": True}).status_code == 409  # no room
        assert c.put(f"/api/roles/{rid}/hiring", json={"meeting_link": "https://example.com/x"}).status_code == 422
        r = c.put(f"/api/roles/{rid}/hiring", json={"published": True, "meeting_link": ZOOM, "criteria": CRITERIA}).json()
        careers = r["careers"]
        assert careers["api_url"] == f"https://tunnel.example.com/api/public/apply/{careers['job_key']}" and "apply_token" not in r["hiring"]
        assert [x["id"] for x in r["hiring"]["criteria"]] == ["e1", "e2", "e3"]


def test_an_eligible_applicant_is_invited_and_takes_the_interview_once(settings):
    sent = []
    app, c = client(replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com"), mail=lambda s, e: sent.append(e))
    store = app.state.store
    with c:
        rid, plan, token = published_role(c)
        page = c.get(f"/api/public/apply/{token}", headers=TUNNEL).json()
        assert page["open"] and page["role_title"] == "Backend Engineer" and len(page["requirements"]) == 3
        assert not any(q["text"] in str(page) for q in plan["questions"])  # the interview questions are never public
        assert c.get(f"/apply/{token}", headers=TUNNEL).status_code in (403, 404)  # Recruence hosts no application page: the company's site does

        assert apply(c, token, consent="").status_code == 422
        assert apply(c, token, email="not-an-email").status_code == 422
        assert apply(c, token).status_code == 201
        assert apply(c, token, email="PRIYA@example.com").status_code == 409  # one application per person per role

        pipe = wait_for(lambda: (p := c.get(f"/api/roles/{rid}/pipeline").json()) and p["candidates"][0]["stage"] == "invited" and p)
        row = pipe["candidates"][0]
        assert pipe["summary"]["applied"] == 1 and pipe["summary"]["eligible"] == 1 and pipe["summary"]["invited"] == 1
        a = c.get(f"/api/applications/{row['application_id']}").json()["application"]
        assert a["screening"]["outcome"] == "eligible" and a["resume_text"].startswith("Priya Raman")
        wait_for(lambda: len(sent) >= 2)
        receipt = next(e for e in sent if e.subject.startswith("We received your application"))  # audit #10
        assert receipt.to == ["priya@example.com"] and "Backend Engineer" in receipt.subject and "with the help of AI" in receipt.body
        invite = next(e for e in sent if e.subject.startswith("Your interview for"))
        assert invite.to == ["priya@example.com"] and "/i/" in invite.body and "before" in invite.body and "one-time code" in invite.body
        assert len(sent) == 2  # the duplicate application got no second receipt
        interview = store.get_interview(row["interview_id"])
        assert interview.meeting_link == ZOOM and interview.invitation.email_sent_at
        assert interview.invitation.expires_at - interview.invitation.created_at == timedelta(days=5)

        link = c.get(f"/api/interviews/{interview.id}/invitation").json()["url"].rsplit("/", 1)[1]
        assert interview.invitation.token == "" and link not in interview.model_dump_json()  # never stored in clear
        view = c.get(f"/api/public/interview/{link}", headers=TUNNEL).json()
        assert view["state"] == "open" and view["meeting_link"] is None and view["first_name"] == "Priya"
        assert view["verification"] == {"required": True, "verified": False, "email": "p••••@example.com", "can_send": True}
        assert c.post(f"/api/public/interview/{link}/start", json={"acknowledged": True}, headers=TUNNEL).status_code == 403  # code first
        assert c.post(f"/api/public/interview/{link}/code", headers=TUNNEL).json()["email"] == "p••••@example.com"
        code = re.search(r"(?<![0-9])([0-9]{6})(?![0-9])", sent[-1].body).group(1)
        assert sent[-1].to == ["priya@example.com"] and code not in str(store.audit_log(50))
        assert code not in sent[-1].subject  # subjects show in phone notifications (audit #10)
        assert c.post(f"/api/public/interview/{link}/verify", json={"code": "000000" if code != "000000" else "111111"}, headers=TUNNEL).status_code == 422
        assert c.post(f"/api/public/interview/{link}/verify", json={"code": code}, headers=TUNNEL).json() == {"verified": True}
        assert c.post(f"/api/public/interview/{link}/start", json={}, headers=TUNNEL).status_code == 422  # notice not confirmed
        started = c.post(f"/api/public/interview/{link}/start", json={"acknowledged": True}, headers=TUNNEL).json()
        assert started["state"] == "in_progress" and started["meeting_link"] == ZOOM
        again = c.post(f"/api/public/interview/{link}/start", json={"acknowledged": True}, headers=TUNNEL).json()
        assert again["state"] == "in_progress" and again["meeting_link"] == ZOOM  # the same device: the same call, no second bot
        assert len(app.state.manager.live) == 1
        # A friend with the same link, on another device: no room, and the hiring team is told.
        friend = TestClient(app)
        seen = friend.get(f"/api/public/interview/{link}", headers=TUNNEL).json()
        assert seen["meeting_link"] is None and seen["elsewhere"] is True
        assert friend.post(f"/api/public/interview/{link}/start", json={"acknowledged": True}, headers=TUNNEL).status_code == 409
        assert [f.kind for f in store.get_interview(interview.id).flags] == ["second_device"]


def test_screening_never_rejects_on_an_ambiguous_judgement():
    now = utcnow()
    crit = [EligibilityCriterion(id="e1", text="Degree"), EligibilityCriterion(id="e2", text="Python"), EligibilityCriterion(id="e3", text="K8s", mandatory=False)]
    r = lambda *v: [CriterionResult(criterion_id=f"e{n}", verdict=x) for n, x in enumerate(v, 1)]
    assert decide(crit, r("met", "met", "not_met"), "", "t", now).outcome == "eligible"  # preferred never decides
    assert decide(crit, r("met", "unclear", "met"), "", "t", now).outcome == "needs_review"
    assert decide(crit, r("not_met", "unclear", "met"), "", "t", now).outcome == "not_eligible"


def test_unquoted_verdicts_go_to_review_and_recruiters_decide(settings):
    ai = ScreeningAI({"e1": ("met", "PhD from Oxford"), "e2": ("met", "Five years of Python services"), "e3": ("unclear", "")})
    app, c = client(settings, ai=ai)
    with c:
        rid, _, token = published_role(c)
        apply(c, token)
        row = wait_for(lambda: (p := c.get(f"/api/roles/{rid}/pipeline").json()["candidates"]) and p[0]["stage"] == "needs_review" and p[0])
        a = c.get(f"/api/applications/{row['application_id']}").json()["application"]
        e1 = next(x for x in a["screening"]["results"] if x["criterion_id"] == "e1")
        assert e1["verdict"] == "unclear" and "No supporting quote" in e1["reasoning"]  # a quote not in the application doesn't count
        r = c.post(f"/api/applications/{row['application_id']}/decision", json={"action": "invite"}).json()["application"]
        assert r["status"] == "invited" and r["interview_id"] and r["reviewed_by"]
        assert c.post(f"/api/applications/{row['application_id']}/decision", json={"action": "reject"}).status_code == 409


def test_without_an_ai_model_every_application_waits_for_a_person(settings):
    app, c = client(settings, ai=OfflineAI())
    with c:
        rid, _, token = published_role(c)
        apply(c, token)
        row = wait_for(lambda: (p := c.get(f"/api/roles/{rid}/pipeline").json()["candidates"]) and p[0]["stage"] == "needs_review" and p[0])
        assert row["interview_id"] is None


def test_closed_roles_and_expired_links(settings):
    app, c = client(settings)
    store = app.state.store
    with c:
        rid, _, token = published_role(c)
        c.put(f"/api/roles/{rid}/hiring", json={"application_deadline": (utcnow() - timedelta(minutes=1)).isoformat()})
        assert c.get(f"/api/public/apply/{token}", headers=TUNNEL).json()["open"] is False
        assert apply(c, token).status_code == 409
        c.put(f"/api/roles/{rid}/hiring", json={"clear_deadline": True})
        apply(c, token)
        row = wait_for(lambda: (p := c.get(f"/api/roles/{rid}/pipeline").json()["candidates"]) and p[0]["interview_id"] and p[0])
        i = store.get_interview(row["interview_id"])
        store.save_interview(i.model_copy(update={"invitation": i.invitation.model_copy(update={"expires_at": utcnow() - timedelta(seconds=1)})}))
        link = c.get(f"/api/interviews/{i.id}/invitation").json()["url"].rsplit("/", 1)[1]
        assert c.get(f"/api/public/interview/{link}", headers=TUNNEL).json()["state"] == "expired"
        assert c.post(f"/api/public/interview/{link}/start", json={"acknowledged": True}, headers=TUNNEL).status_code == 410
        assert c.get(f"/api/roles/{rid}/pipeline").json()["candidates"][0]["stage"] == "invitation_expired"

        # The recruiter sends a fresh link: the old one stops working, the new one is open.
        fresh = c.post(f"/api/interviews/{i.id}/invitation/resend", json={"days": 3}).json()
        assert c.get(f"/api/public/interview/{link}", headers=TUNNEL).status_code == 404
        new = fresh["url"].rsplit("/", 1)[1]
        assert c.get(f"/api/public/interview/{new}", headers=TUNNEL).json()["state"] == "open"


def test_pass_mark_is_applied_when_shown_and_never_changes_the_score(settings):
    app, c = client(settings)
    store = app.state.store
    with c:
        rid, plan, token = published_role(c, passing_score=70)
        comp_ids = [x["id"] for x in plan["competencies"]]
        ids = {n: c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": n}).json()["id"] for n in ("Ana", "Sam", "Lee")}
        evaluated(store, ids["Ana"], "Ana", [4, 4], "meets", comp_ids)  # 80
        evaluated(store, ids["Sam"], "Sam", [3, 4], "meets", comp_ids)  # 70: at the mark passes
        evaluated(store, ids["Lee"], "Lee", [3, 3], "meets", comp_ids)  # 60
        rows = {r["candidate_name"]: r for r in c.get(f"/api/roles/{rid}/pipeline").json()["candidates"]}
        assert [(rows[n]["rank"], rows[n]["score"], rows[n]["stage"]) for n in ("Ana", "Sam", "Lee")] == [(1, 80, "passed"), (2, 70, "passed"), (3, 60, "failed")]
        c.put(f"/api/roles/{rid}/hiring", json={"passing_score": 75})
        rows = {r["candidate_name"]: r for r in c.get(f"/api/roles/{rid}/pipeline").json()["candidates"]}
        assert rows["Sam"]["score"] == 70 and rows["Sam"]["stage"] == "failed" and rows["Ana"]["stage"] == "passed"
        report = c.get(f"/api/roles/{rid}/ranking").json()
        assert report["passing_score"] == 75 and report["passed"] == ["Ana"] and sorted(report["failed"]) == ["Lee", "Sam"]


def test_deleting_a_candidate_deletes_their_application(settings):
    app, c = client(settings)
    store = app.state.store
    with c:
        rid, _, token = published_role(c)
        apply(c, token)
        row = wait_for(lambda: (p := c.get(f"/api/roles/{rid}/pipeline").json()["candidates"]) and p[0]["interview_id"] and p[0])
        assert c.delete(f"/api/interviews/{row['interview_id']}").status_code == 200
        assert store.get_application(row["application_id"]) is None


def test_oversized_applications_are_refused_before_reading(settings):
    _, c = client(settings)
    with c:
        _, _, token = published_role(c)
        big = b"x" * (7 * 1024 * 1024)
        assert apply(c, token, resume=big).status_code == 413


def test_the_team_hears_once_when_an_invitation_expires_unused(settings):
    from app import demo

    sent = []
    s = replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com", notify_emails=("team@example.com",))
    app, c = client(s, mail=lambda _, e: sent.append(e))
    store = app.state.store
    notify = app.state.notify_expired_invitations
    with c:
        rid, _, token = published_role(c, auto_invite=True)
        apply(c, token)
        row = wait_for(lambda: (p := c.get(f"/api/roles/{rid}/pipeline").json()["candidates"]) and p[0]["interview_id"] and p[0])
        demo.install(store)  # Daniel Okoye's demo link is expired too, but demo data never emails anyone
        assert c.portal.call(notify) == []  # not expired yet
        later = utcnow() + timedelta(days=6)
        sent.clear()
        assert c.portal.call(notify, later) == [row["interview_id"]]
        [note] = sent
        assert note.to == ["team@example.com"] and "Priya Raman" in note.subject and "expired" in note.subject
        assert c.portal.call(notify, later) == []  # once per link
        # A resent link that also runs out is a new link, so the team hears again.
        c.post(f"/api/interviews/{row['interview_id']}/invitation/resend", json={"days": 3})
        assert c.portal.call(notify, later + timedelta(days=4)) == [row["interview_id"]]


def test_screening_score_is_grounded_and_the_threshold_sends_low_scores_to_a_person():
    from app.recruiting import decide, score

    crit = [EligibilityCriterion(id="e1", text="CS degree", mandatory=True, kind="education"),
            EligibilityCriterion(id="e2", text="3+ years Python", mandatory=True, kind="experience"),
            EligibilityCriterion(id="e3", text="Kubernetes", mandatory=False, kind="skill"),
            EligibilityCriterion(id="e4", text="Terraform", mandatory=False, kind="skill")]
    res = [CriterionResult(criterion_id="e1", verdict="met", evidence="B.Tech"), CriterionResult(criterion_id="e2", verdict="met", evidence="5 years"),
           CriterionResult(criterion_id="e3", verdict="unclear"), CriterionResult(criterion_id="e4", verdict="not_met", evidence="none")]
    total, breakdown, missing = score(crit, res)
    assert total == 67 and breakdown == {"education": 100, "experience": 100, "skill": 0} and missing == ["Kubernetes", "Terraform"]
    now = utcnow()
    assert decide(crit, res, "", "m", now).outcome == "eligible"  # threshold off: mandatory criteria decide
    low = decide(crit, res, "", "m", now, threshold=80)
    assert low.outcome == "needs_review" and "67/100" in low.reasons[0] and low.score == 67  # never rejected for a low score
    assert decide(crit, res, "", "m", now, threshold=60).outcome == "eligible"


def test_paused_roles_job_details_and_withdrawn_applications(settings):
    app, c = client(settings)
    with c:
        rid, _, token = published_role(c, job={"responsibilities": "Own the payments API.", "employment_type": "Full-time",
                                               "location": "Berlin", "work_arrangement": "Hybrid"})
        page = c.get(f"/api/public/apply/{token}", headers=TUNNEL).json()
        assert page["job"]["location"] == "Berlin" and page["job"]["work_arrangement"] == "Hybrid"
        c.put(f"/api/roles/{rid}/hiring", json={"paused": True})
        assert c.get(f"/api/public/apply/{token}", headers=TUNNEL).json()["open"] is False
        assert apply(c, token).status_code in (409, 410)
        c.put(f"/api/roles/{rid}/hiring", json={"paused": False, "auto_invite": False})
        assert apply(c, token).status_code == 201
        row = wait_for(lambda: (p := c.get(f"/api/roles/{rid}/pipeline").json()["candidates"]) and p[0]["stage"] != "screening" and p[0])
        c.post(f"/api/applications/{row['application_id']}/decision", json={"action": "withdraw", "note": "Took another offer"})
        assert c.get(f"/api/roles/{rid}/pipeline").json()["candidates"][0]["stage"] == "withdrawn"


def test_report_notes_reach_the_workspace_and_optionally_the_candidate(settings, ready_interview):
    import asyncio
    from app.mailer import Mailer, NotifyingSink, interview_received
    from tests.test_mailer import report

    sent = []
    s = replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com", notify_emails=("lead@example.com",))
    mailer = Mailer(s, send=lambda _, e: sent.append(e))
    store = Store(settings.database)
    i = store.create_interview(ready_interview.setup.model_copy(update={"candidate_email": "priya@example.com"}))
    store.create_session("s1", i.id, "Google Meet")
    sink = NotifyingSink(store, mailer, store, lambda r: ["team@example.com", "lead@example.com"],
                         lambda r: interview_received(store.get_interview(r.interview_id), s))

    async def run():
        sink.push_report(report(i.id))
        await mailer.aclose()
    asyncio.run(run())
    team, cand = sent
    assert team.to == ["lead@example.com", "team@example.com"]  # NOTIFY_EMAILS first, then the workspace list, once each
    assert cand.to == ["priya@example.com"] and "received" in cand.subject
    assert not any(w in cand.body.lower() for w in ("score", "recommend", "strong", "evaluation"))  # nothing about how it went



async def test_screening_time_is_when_it_finished(settings):
    """Audit #10: the dashboard showed when screening started (a slow model plus its fallback took minutes)."""
    import asyncio

    from app.models import Application, ApplicationInput, EligibilityCriterion, Role, RoleSetup
    from app.recruiting import screen

    class SlowAI(OfflineAI):
        is_ai = True
        served_name = "slow-model"

        async def screen(self, system, prompt):
            await asyncio.sleep(0.3)
            return ScreeningOutput(results=[CriterionResult(criterion_id="c1", verdict="unclear", reasoning="r")], summary="s")

    role = Role(id="r1", setup=RoleSetup(role_title="AI Engineer", job_description="Build LLM features in Python."), created_at=utcnow(), updated_at=utcnow())
    role.hiring.criteria = [EligibilityCriterion(id="c1", text="Python", mandatory=True)]
    a = Application(id="a1", role_id="r1", created_at=utcnow(), updated_at=utcnow(),
                    candidate=ApplicationInput(full_name="Priya", email="p@example.com"), resume_text="Python developer")
    started = utcnow()
    result = await screen(SlowAI(), role, a)
    assert (result.screened_at - started).total_seconds() >= 0.25



def test_no_application_receipt_when_turned_off(settings):
    sent = []
    _, c = client(replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com", application_receipt=False),
                  mail=lambda s, e: sent.append(e))
    with c:
        rid, _, token = published_role(c)
        assert apply(c, token).status_code == 201
        wait_for(lambda: sent)  # the invitation
        assert not any(e.subject.startswith("We received your application") for e in sent)


def test_the_original_resume_file_is_kept_and_downloadable(settings):
    """Audit M1: only the resume's text was kept. The file is now stored with the application, downloaded as an
    attachment by the dashboard, and removed with the application."""
    app, c = client(settings)
    store = app.state.store
    with c:
        rid, _, token = published_role(c)
        assert apply(c, token).status_code == 201
        app_id = wait_for(lambda: c.get(f"/api/roles/{rid}/pipeline").json()["candidates"])[0]["application_id"]
        assert c.get(f"/api/applications/{app_id}").json()["resume_file"] is True
        r = c.get(f"/api/applications/{app_id}/resume")
        assert r.status_code == 200 and r.content == RESUME
        assert r.headers["content-disposition"] == 'attachment; filename="cv.txt"' and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-type"].startswith("text/plain") and r.headers["cache-control"] == "no-store"
        assert any(e["action"] == "resume_downloaded" for e in c.get("/api/audit").json())
        assert c.get("/api/public/apply/x/resume", headers=TUNNEL).status_code in (404, 405)  # nothing public
        store.delete_application(app_id)
        assert store.resume_file(app_id) is None  # gone with its application
        assert c.get(f"/api/applications/{app_id}/resume").status_code == 404


def test_an_application_without_a_file_says_so(settings):
    app, c = client(settings)
    with c:
        rid, _, token = published_role(c)
        r = c.post(f"/api/public/apply/{token}", headers=TUNNEL, data={"full_name": "Lin Wei", "email": "lin@example.com",
                                                                      "years_experience": "3", "consent": "yes", "cover_note": "Python developer"})
        assert r.status_code == 201, r.text
        app_id = wait_for(lambda: c.get(f"/api/roles/{rid}/pipeline").json()["candidates"])[0]["application_id"]
        assert c.get(f"/api/applications/{app_id}").json()["resume_file"] is False
        assert "Only the resume's text" in c.get(f"/api/applications/{app_id}/resume").json()["detail"]
