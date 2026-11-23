"""The recruiter flow end to end over HTTP, with a fake MeetStream and no network."""

import time
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.llm import OfflineAI
from app.models import utcnow
from app.server import create_app
from app.store import Store
from tests.fakes import MEET_LINK, FakeMeetStream, event, sentence

TUNNEL = {"X-Forwarded-For": "34.230.88.153"}

SETUP = {
    "role_title": "Backend Engineer",
    "level": "Senior",
    "job_description": "Build Python APIs on PostgreSQL and own them in production.",
    "candidate_name": "Priya Raman",
    "resume_text": "Seven years of Python services.",
    "interview_type": "behavioral",
    "competencies": [{"name": "Ownership", "description": "Owns outcomes"}, {"name": "Communication"}],
    "question_count": 2,
    "duration_minutes": 15,
}


def make_client(settings, fake=None, store=None):
    app = create_app(settings=settings, store=store or Store(settings.database), ai=OfflineAI(),
                     meetstream_client_factory=lambda: fake or FakeMeetStream(), run_loops=False)
    return app, TestClient(app)


def wait_for(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def ready_interview_id(c):
    iid = c.post("/api/interviews", json=SETUP).json()["id"]
    res = c.post(f"/api/interviews/{iid}/plan/generate").json()
    assert res["warning"] is None and len(res["interview"]["plan"]["questions"]) == 2
    assert c.post(f"/api/interviews/{iid}/plan/approve").status_code == 200
    return iid


def test_full_recruiter_flow(settings):
    fake = FakeMeetStream()
    app, c = make_client(settings, fake)
    with c:
        iid = c.post("/api/interviews", json=SETUP).json()["id"]
        plan = c.post(f"/api/interviews/{iid}/plan/generate").json()["interview"]["plan"]
        assert c.put(f"/api/interviews/{iid}/plan", json={**plan, "questions": []}).status_code == 422
        assert c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET_LINK, "consent_attested": True}).status_code == 409

        plan["questions"][0]["text"] = "Tell me about a production incident you owned end to end."
        edited = c.put(f"/api/interviews/{iid}/plan", json=plan).json()
        assert "edited by recruiter" in edited["plan"]["source"] and edited["plan_approved_at"] is None
        assert c.post(f"/api/interviews/{iid}/plan/approve").json()["status"] == "ready"

        assert c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET_LINK}).status_code == 422  # no consent
        assert c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": "https://whereby.com/x", "consent_attested": True}).status_code == 422
        sid = c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET_LINK, "consent_attested": True}).json()["session_id"]

        live = app.state.manager.live[sid]
        token, runner = live.webhook_token, live.runner
        wait_for(lambda: fake.bots.created)
        assert fake.mia.created[0]["agent"]["custom_functions"][0]["url"] == f"https://tunnel.example.com/webhooks/mia/{token}/next_question"
        assert c.post(f"/webhooks/meetstream/{token}/events", json=event("bot.inmeeting"), headers=TUNNEL).status_code == 200
        wait_for(lambda: c.get(f"/api/sessions/{sid}").json()["snapshot"]["state"] == "awaiting_consent")

        tool = f"/webhooks/mia/{token}"
        auth = {**TUNNEL, "Authorization": f"Bearer {runner.tool_token}", "X-Bot-ID": "bot-123"}
        envelope = lambda args: {"name": "next_question", "bot": {"bot_id": "bot-123", "dynamic_vars": {}}, "args": args}
        assert c.post(f"{tool}/next_question", json=envelope({}), headers=TUNNEL).status_code == 401
        assert c.post(f"{tool}/delete_everything", json={}, headers=auth).status_code == 404
        assert c.post(f"{tool}/next_question", json=envelope({}), headers=auth).json()["status"] == "consent_required"
        intro = c.post(f"{tool}/next_question", json=envelope({"candidate_consented": True}), headers=auth).json()
        assert intro["question_id"] == "intro" and "introduce yourself" in intro["question"]
        q1 = c.post(f"{tool}/next_question", json=envelope({"previous_question_id": "intro", "answer_note": "Background."}), headers=auth).json()
        assert q1["question"] == "Tell me about a production incident you owned end to end."

        answer = "I owned the checkout outage: I rolled back the deploy, wrote the postmortem and added alerts."
        assert c.post(f"/webhooks/meetstream/{token}/transcript", json=sentence(answer), headers=TUNNEL).status_code == 200
        assert c.post(f"/webhooks/meetstream/not-a-token/transcript", json=sentence("x"), headers=TUNNEL).status_code == 404
        wait_for(lambda: any(u["role"] == "candidate" for u in c.get(f"/api/sessions/{sid}").json()["transcript"]))

        c.post(f"{tool}/next_question", json=envelope({"previous_question_id": "q1", "answer_note": "Owned the outage."}), headers=auth)
        done = c.post(f"{tool}/next_question", json=envelope({"previous_question_id": "q2", "answer_note": "n2"}), headers=auth).json()
        assert done["status"] == "done"
        c.post(f"{tool}/end_interview", json={"name": "end_interview", "bot": {"bot_id": "bot-123"}, "args": {"reason": "completed"}}, headers=auth)

        snap = wait_for(lambda: (s := c.get(f"/api/sessions/{sid}").json())["report"] and s)
        report = snap["report"]
        assert report["end_reason"] == "completed" and report["consent_given"] and report["questions_asked"] == 3  # with the introduction
        assert report["evaluation"]["summary"].startswith("NOT AN AI EVALUATION")
        assert [n["note"] for n in snap["notes"]] == ["Background.", "Owned the outage.", "n2"]
        assert fake.bots.removed == ["bot-123"] and fake.mia.deleted == ["cfg-1"]
        # MeetStream keeps calling back after the session ends (bot.stopped, post-call events): acknowledge them.
        late = c.post(f"/webhooks/meetstream/{token}/events", json=event("bot.stopped", status="Stopped"), headers=TUNNEL)
        assert late.status_code == 200 and late.json()["ignored"] == "session finished"
        assert c.post(f"{tool}/next_question", json=envelope({}), headers=auth).status_code == 404

        detail = c.get(f"/api/interviews/{iid}").json()
        assert detail["interview"]["status"] == "completed" and detail["live_session_id"] is None
        assert detail["sessions"][0]["bot_id"] == "bot-123" and detail["sessions"][0]["end_reason"] == "completed"
        actions = [a["action"] for a in detail["audit"]]
        assert {"plan_approved", "consent_attested", "session_launched", "candidate_consented", "session_completed"} <= set(actions)
        assert c.post(f"/api/interviews/{iid}/decision", json={"decision": "advance", "note": "Strong ownership"}).json()["decision"] == "advance"
        [row] = c.get("/api/interviews").json()
        assert row["recommendation"] == report["evaluation"]["recommendation"] and row["decision"] == "advance"
        again = c.post(f"/api/sessions/{sid}/evaluate")
        assert again.status_code == 200 and again.json()["generated_by"].startswith("template + heuristic (no AI) (re-evaluation)")
        assert c.get(f"/api/sessions/{sid}/report").json()["generated_by"] == again.json()["generated_by"]
        assert c.post("/api/sessions/nope/evaluate").status_code == 404

    # A fresh process on the same database still serves the session and report.
    _, c2 = make_client(settings, fake)
    with c2:
        again = c2.get(f"/api/sessions/{sid}").json()
        assert again["live"] is False and again["report"]["session_id"] == sid
        assert any(u["text"] == answer for u in again["transcript"])
        assert c2.delete(f"/api/interviews/{iid}").json()["meetstream"] == {"bot-123": "erased"}
        assert c2.get(f"/api/interviews/{iid}").status_code == 404
        assert c2.get(f"/api/sessions/{sid}").status_code == 404


def test_requirements_in_setup(settings):
    _, c = make_client(settings)
    with c:
        r = c.post("/api/interviews", json={**SETUP, "requirements": ["  3+ years of Python  ", "Led on-call"]})
        assert r.status_code == 201 and r.json()["setup"]["requirements"] == ["3+ years of Python", "Led on-call"]
        assert c.post("/api/interviews", json={**SETUP, "requirements": [""]}).status_code == 422
        assert c.post("/api/interviews", json={**SETUP, "requirements": ["x" * 301]}).status_code == 422
        assert c.post("/api/interviews", json={**SETUP, "requirements": [f"r{n}" for n in range(11)]}).status_code == 422
        assert c.post("/api/interviews", json=SETUP).json()["setup"]["requirements"] == []  # optional


def test_search_and_filter_interviews(settings):
    from app.models import EvaluationReport, LLMEvaluation
    app, c = make_client(settings)
    store = app.state.store
    with c:
        ids = {}
        for name, role in (("Priya Raman", "Backend Engineer"), ("Sam Okafor", "Data Analyst"), ("Lena Fischer", "Senior Backend Engineer")):
            ids[name] = c.post("/api/interviews", json={**SETUP, "candidate_name": name, "role_title": role}).json()["id"]
        # Priya: completed on Zoom, evaluated "yes", advanced. Sam: completed on Teams, not evaluated. Lena: draft.
        now = utcnow()
        for name, platform in (("Priya Raman", "Zoom"), ("Sam Okafor", "Microsoft Teams")):
            sid = f"s-{ids[name]}"
            store.create_session(sid, ids[name], platform)
            evaluation = LLMEvaluation(recommendation="yes", summary="", competency_scores=[], strengths=[], areas_for_improvement=[],
                                       suggested_next_round_focus=[], confidence="medium") if name == "Priya Raman" else None
            store.push_report(EvaluationReport(
                session_id=sid, interview_id=ids[name], role_title="r", candidate_name=name, platform=platform, started_at=now,
                ended_at=now, duration_seconds=1, questions_planned=2, questions_asked=2, follow_ups_asked=0, end_reason="completed",
                consent_given=True, generated_by="test", evaluation=evaluation, notes=[], transcript=[]))
            i = store.get_interview(ids[name])
            store.save_interview(i.model_copy(update={"status": "completed", "platform": platform, "latest_session_id": sid,
                                                      "decision": "advance" if name == "Priya Raman" else None}))

        def names(**params):
            r = c.get("/api/interviews", params=params)
            assert r.status_code == 200, r.text
            return sorted(i["candidate_name"] for i in r.json())

        assert names() == ["Lena Fischer", "Priya Raman", "Sam Okafor"]
        assert names(q="backend") == ["Lena Fischer", "Priya Raman"]  # role, any case
        assert names(q="  sam  ") == ["Sam Okafor"]  # name, spaces trimmed
        assert names(status="completed") == ["Priya Raman", "Sam Okafor"]
        assert names(status="draft", q="lena") == ["Lena Fischer"]
        assert names(platform="Zoom") == ["Priya Raman"]
        assert names(recommendation="yes") == ["Priya Raman"]
        assert names(recommendation="none") == ["Lena Fischer", "Sam Okafor"]
        assert names(decision="advance") == ["Priya Raman"]
        assert names(decision="none", status="completed") == ["Sam Okafor"]
        assert names(q="nobody") == []
        for bad in ({"status": "done"}, {"recommendation": "maybe"}, {"decision": "later"}, {"q": "x" * 101}):
            assert c.get("/api/interviews", params=bad).status_code == 422


def test_schedule_and_unschedule(settings):
    _, c = make_client(settings)
    with c:
        iid = ready_interview_id(c)
        when = (utcnow() + timedelta(hours=2)).isoformat()
        res = c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET_LINK, "consent_attested": True, "scheduled_at": when}).json()
        assert res["session_id"] is None
        assert c.get(f"/api/interviews/{iid}").json()["interview"]["status"] == "scheduled"
        assert c.post(f"/api/interviews/{iid}/plan/generate").status_code == 409  # locked while scheduled
        assert c.post(f"/api/interviews/{iid}/unschedule").json()["status"] == "ready"


def test_launch_needs_meetstream(settings):
    _, c = make_client(replace(settings, meetstream_api_key=""))
    with c:
        iid = ready_interview_id(c)
        res = c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET_LINK, "consent_attested": True})
        assert res.status_code == 409 and "MEETSTREAM_API_KEY" in res.json()["detail"]
        assert c.get("/api/health").json()["meetstream"]["ready"] is False


def test_cannot_delete_or_relaunch_while_live(settings):
    _, c = make_client(settings)
    with c:
        iid = ready_interview_id(c)
        c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET_LINK, "consent_attested": True})
        assert c.delete(f"/api/interviews/{iid}").status_code == 409
        assert c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET_LINK, "consent_attested": True}).status_code == 409


def test_decision_only_after_completion(settings):
    _, c = make_client(settings)
    with c:
        iid = ready_interview_id(c)
        assert c.post(f"/api/interviews/{iid}/decision", json={"decision": "advance"}).status_code == 409


def test_validation_errors(settings):
    _, c = make_client(settings)
    with c:
        assert c.post("/api/interviews", json={**SETUP, "question_count": 50}).status_code == 422
        assert c.post("/api/interviews", json={**SETUP, "job_description": ""}).status_code == 422
        assert c.get("/api/interviews/nope").status_code == 404


# ---------- access control ----------


def test_token_required_when_configured(settings):
    _, c = make_client(replace(settings, app_access_token="s3cret"))
    with c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/auth/status").json() == {"auth_required": True, "accounts": False, "database_problem": "", "signed_in": False, "user": None}
        assert c.get("/api/interviews").status_code == 401
        assert c.post("/api/auth/login", json={"token": "wrong"}).status_code == 401
        assert c.get("/api/interviews", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.post("/api/auth/login", json={"token": "s3cret"}).status_code == 200
        assert "s3cret" not in c.cookies.get("mia_interviewer_session", "")
        assert c.get("/api/interviews").status_code == 200  # cookie
        c.post("/api/auth/logout")
        c.cookies.clear()
        assert c.get("/api/interviews").status_code == 401


@pytest.mark.parametrize("token", ["", "s3cret"])
def test_public_tunnel_only_reaches_webhooks(settings, token):
    _, c = make_client(replace(settings, app_access_token=token))
    with c:
        headers = {**TUNNEL, "Authorization": f"Bearer {token}"}
        assert c.get("/api/interviews", headers=headers).status_code == 403
        assert c.get("/", headers=TUNNEL).status_code == 403
        assert c.post("/webhooks/meetstream/unknown/events", json={}, headers=TUNNEL).status_code == 404


# ---------- trusted identity proxy ----------

PROXY_SETTINGS = {"app_access_token": "s3cret", "proxy_auth_secret": "proxy-secret",
                  "proxy_admin_groups": ("hr-admins",), "proxy_viewer_groups": ("hiring-managers",)}


def via_proxy(email="ana@example.com", groups=None, secret="proxy-secret"):
    h = {**TUNNEL, "X-MIA-Proxy-Secret": secret}
    if email is not None:
        h["X-Forwarded-Email"] = email
    if groups is not None:
        h["X-Forwarded-Groups"] = groups
    return h


def test_proxy_user_is_a_recruiter_and_named_in_the_audit(settings):
    app, c = make_client(replace(settings, **PROXY_SETTINGS))
    with c:
        h = via_proxy()
        assert c.get("/", headers=h).status_code == 200
        status = c.get("/api/auth/status", headers=h).json()
        assert status["auth_required"] is False and status["signed_in"] is True
        assert {k: status["user"][k] for k in ("name", "role", "via")} == {"name": "ana@example.com", "role": "recruiter", "via": "proxy"}
        created = c.post("/api/interviews", json=SETUP, headers=h)
        assert created.status_code == 201
        iid = created.json()["id"]
        [entry] = app.state.store.audit_log(10, iid)
        assert entry["action"] == "interview_created" and entry["actor"] == "ana@example.com (recruiter)"
        assert c.get("/api/audit", headers=h).status_code == 403  # admin only
        assert c.delete(f"/api/interviews/{iid}", headers=h).status_code == 403
        # The access token still works directly, as an admin, and isn't needed through the proxy.
        assert c.get("/api/audit", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_proxy_groups_map_to_roles(settings):
    app, c = make_client(replace(settings, **PROXY_SETTINGS))
    with c:
        admin = via_proxy("lee@example.com", "staff, hr-admins")
        viewer = via_proxy("max@example.com", "hiring-managers")
        iid = c.post("/api/interviews", json=SETUP, headers=admin).json()["id"]
        assert c.get("/api/audit", headers=admin).status_code == 200
        assert c.get(f"/api/interviews/{iid}", headers=viewer).status_code == 200
        # Who did what is admin-only: viewers see the activity, not the names.
        [seen_by_viewer] = c.get(f"/api/interviews/{iid}", headers=viewer).json()["audit"]
        [seen_by_admin] = c.get(f"/api/interviews/{iid}", headers=admin).json()["audit"]
        assert "actor" not in seen_by_viewer and seen_by_viewer["action"] == "interview_created"
        assert seen_by_admin["actor"] == "lee@example.com (admin)"
        assert c.post("/api/interviews", json=SETUP, headers=viewer).status_code == 403
        assert c.post(f"/api/interviews/{iid}/plan/generate", headers=viewer).status_code == 403
        assert c.delete(f"/api/interviews/{iid}", headers=admin).status_code == 200


def test_untrusted_proxied_requests_are_refused(settings):
    _, c = make_client(replace(settings, **PROXY_SETTINGS))
    with c:
        assert c.get("/api/interviews", headers=via_proxy(secret="guess")).status_code == 403
        assert c.get("/api/interviews", headers={**TUNNEL, "X-Forwarded-Email": "ana@example.com"}).status_code == 403
        assert c.get("/api/interviews", headers=via_proxy(email=None)).status_code == 401
        assert c.post("/webhooks/meetstream/unknown/events", json={}, headers=TUNNEL).status_code == 404
    # With no secret configured, the secret header means nothing.
    _, c = make_client(replace(settings, app_access_token="s3cret"))
    with c:
        assert c.get("/api/interviews", headers=via_proxy(secret="")).status_code == 403


def test_proxy_default_role_none_refuses_unlisted_users(settings):
    _, c = make_client(replace(settings, **PROXY_SETTINGS, proxy_default_role="none"))
    with c:
        assert c.get("/api/interviews", headers=via_proxy("eve@example.com", "staff")).status_code == 403
        assert c.get("/api/interviews", headers=via_proxy("max@example.com", "hiring-managers")).status_code == 200


def test_without_token_only_localhost(settings):
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app, client=("203.0.113.9", 5000)) as remote:
        assert remote.get("/api/interviews").status_code == 403
    with TestClient(app) as local:
        assert local.get("/api/interviews").status_code == 200
        assert local.get("/").status_code == 200
