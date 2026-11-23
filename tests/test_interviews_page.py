"""The Interviews page (GET /api/interviews): one list for every interview in the workspace, whether a recruiter created
it or an eligible application was invited automatically, with its state, assignment method and meeting."""

from datetime import timedelta

from app import lifecycle
from app.models import utcnow
from tests.test_api import wait_for
from tests.test_recruiting import ZOOM, apply, client, published_role


def test_automatic_and_manual_interviews_share_one_list(settings):
    app, c = client(settings)
    store = app.state.store
    with c:
        rid, _, token = published_role(c)  # invites eligible applicants automatically
        assert apply(c, token).status_code == 201
        auto = wait_for(lambda: [x for x in c.get("/api/interviews").json() if x["assigned"] == "automatic"])[0]
        # Manual: a recruiter invites an applicant the role didn't invite by itself.
        c.put(f"/api/roles/{rid}/hiring", json={"auto_invite": False})
        assert apply(c, token, email="sam@example.com", full_name="Sam Lee").status_code == 201
        app_id = wait_for(lambda: [x for x in c.get(f"/api/roles/{rid}/pipeline").json()["candidates"]
                                   if x.get("stage") == "eligible"])[0]["application_id"]
        assert c.post(f"/api/applications/{app_id}/decision", json={"action": "invite"}).status_code == 200
        # Manual: a candidate added to the role by hand.
        added = c.post(f"/api/roles/{rid}/candidates", json={"candidate_name": "Ana Ruiz", "candidate_email": "ana@example.com"})
        assert added.status_code in (200, 201), added.text

        rows = {x["candidate_name"]: x for x in c.get("/api/interviews").json() if not x["demo"]}
        assert rows["Priya Raman"]["assigned"] == "automatic" and rows["Sam Lee"]["assigned"] == "manual"
        assert rows["Ana Ruiz"]["assigned"] == "manual" and rows["Priya Raman"]["id"] == auto["id"]
        for name in ("Priya Raman", "Sam Lee", "Ana Ruiz"):
            assert rows[name]["role_id"] == rid and rows[name]["state"] == "pending"
        assert rows["Priya Raman"]["meeting_link"] == ZOOM and rows["Priya Raman"]["platform"] == "Zoom"
        assert rows["Priya Raman"]["application_id"] and rows["Ana Ruiz"]["application_id"] is None
        assert {x["candidate_name"] for x in c.get("/api/interviews?assigned=manual").json()} >= {"Sam Lee", "Ana Ruiz"}
        assert [x["candidate_name"] for x in c.get("/api/interviews?assigned=automatic").json()] == ["Priya Raman"]
        assert {x["candidate_name"] for x in c.get(f"/api/interviews?role={rid}&state=pending").json()} == {"Priya Raman", "Sam Lee", "Ana Ruiz"}

        # Scheduled vs upcoming (within a day), in progress, completed, cancelled.
        i = store.get_interview(rows["Ana Ruiz"]["id"])
        store.save_interview(i.model_copy(update={"status": "scheduled", "scheduled_at": utcnow() + timedelta(days=3)}))
        assert c.get("/api/interviews?state=scheduled").json()[0]["candidate_name"] == "Ana Ruiz"
        store.save_interview(i.model_copy(update={"status": "scheduled", "scheduled_at": utcnow() + timedelta(hours=2)}))
        assert [x["candidate_name"] for x in c.get("/api/interviews?state=upcoming").json()] == ["Ana Ruiz"]
        store.save_interview(i.model_copy(update={"status": "in_progress"}))
        assert [x["candidate_name"] for x in c.get("/api/interviews?state=in_progress").json()] == ["Ana Ruiz"]
        store.save_interview(i.model_copy(update={"status": "completed"}))
        assert [x["candidate_name"] for x in c.get("/api/interviews?state=completed").json()] == ["Ana Ruiz"]
        store.save_interview(i.model_copy(update={"status": "ready", "cancelled_at": utcnow()}))
        assert [x["candidate_name"] for x in c.get("/api/interviews?state=cancelled").json()] == ["Ana Ruiz"]
        assert c.get("/api/interviews?state=nonsense").status_code == 422


def test_every_stage_has_a_state():
    for stage in lifecycle.LABELS:
        if stage != "scheduled":
            assert lifecycle._STATE_OF_STAGE[stage] in lifecycle.STATE_LABELS


def test_an_undelivered_invitation_is_not_shown_as_sent(settings):
    app, c = client(settings)
    store = app.state.store
    with c:
        _, _, token = published_role(c)  # invites eligible applicants automatically, and no email is set up here
        assert apply(c, token).status_code == 201
        row = wait_for(lambda: [x for x in c.get("/api/interviews").json() if x["invitation_undelivered"]])[0]
        i = store.get_interview(row["id"])
        assert i.invitation.email_error and i.invitation.email_sent_at is None
        store.save_interview(i.model_copy(update={"invitation": i.invitation.model_copy(update={"email_sent_at": utcnow(), "email_error": ""})}))
        assert c.get("/api/interviews").json()[0]["invitation_undelivered"] is False


def test_a_later_send_clears_the_undelivered_mark(settings):
    from tests.test_gateway import invited
    app, c, store, sent, interview, _ = invited(settings)
    try:
        i = store.get_interview(interview.id)
        store.save_interview(i.model_copy(update={"invitation": i.invitation.model_copy(update={"email_sent_at": None, "email_error": "Couldn't connect"})}))
        assert c.get(f"/api/interviews/{i.id}").json()["invitation_undelivered"] is True
        assert c.post(f"/api/interviews/{i.id}/invite", json={}).status_code == 200
        assert c.get(f"/api/interviews/{i.id}").json()["invitation_undelivered"] is False
        assert [x for x in c.get("/api/interviews").json() if x["id"] == i.id][0]["invitation_undelivered"] is False
    finally:
        c.__exit__(None, None, None)


def test_a_failed_invitation_returns_at_once_and_is_retried_in_the_background(settings):
    """The recruiter's Invite used to wait for every retry (about 2.5 minutes) and Candidates still said "Invited"."""
    import time
    from dataclasses import replace

    outcomes = ["fail", "ok"]  # the first try fails, the background retry gets through
    sent = []

    def send(s, email):
        if outcomes.pop(0) == "fail":
            raise ConnectionRefusedError(10061, "refused")
        sent.append(email)

    # Only the invitation's tries are scripted here: the applicant's receipt email is off (tested in test_recruiting.py).
    app, c = client(replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com", application_receipt=False), mail=send)
    app.state.mailer.retry_delays = (0.3,)
    with c:
        rid, _, token = published_role(c)
        c.put(f"/api/roles/{rid}/hiring", json={"auto_invite": False})
        assert apply(c, token).status_code == 201
        app_id = wait_for(lambda: [x for x in c.get(f"/api/roles/{rid}/pipeline").json()["candidates"] if x["stage"] == "eligible"])[0]["application_id"]
        started = time.monotonic()
        r = c.post(f"/api/applications/{app_id}/decision", json={"action": "invite"})
        assert time.monotonic() - started < 2  # one try, not the retries
        assert r.status_code == 200 and "Couldn't connect" in r.json()["invitation_email"]  # the dashboard says so
        row = c.get(f"/api/roles/{rid}/pipeline").json()["candidates"][0]
        assert row["stage"] == "invited" and row["invitation_undelivered"] is True and "Couldn't connect" in row["invitation_email"]
        person = c.get("/api/candidates").json()["candidates"][0]
        assert person["applications"][0]["status"] == "invited" and person["applications"][0]["invitation_undelivered"] is True
        # The background retry gets through and clears the mark everywhere.
        wait_for(lambda: sent)
        wait_for(lambda: not c.get(f"/api/roles/{rid}/pipeline").json()["candidates"][0]["invitation_undelivered"])
        assert c.get("/api/candidates").json()["candidates"][0]["applications"][0]["invitation_undelivered"] is False
        assert "/i/" in sent[0].body


def test_an_invitation_is_held_while_its_link_cant_work(settings, monkeypatch):
    """Audit 2026-11-10 (M1): an invitation went out carrying a stopped tunnel's address. Now it's held with the reason,
    the role can't be published without a Public URL, and Send again works once the address is back."""
    from dataclasses import replace

    from app import server as server_module
    sent = []
    s = replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com", check_public_url=True)
    resolves = {"ok": False}

    async def fake_resolves(url, timeout_s=5.0):
        return resolves["ok"]
    monkeypatch.setattr(server_module, "public_host_resolves", fake_resolves)
    app, c = client(s, mail=lambda st, e: sent.append(e))
    with c:
        rid, _, token = published_role(c)
        assert apply(c, token).status_code == 201
        row = wait_for(lambda: [x for x in c.get(f"/api/roles/{rid}/pipeline").json()["candidates"] if x["invitation_undelivered"]])[0]
        assert "doesn't resolve right now" in row["invitation_email"] and not [e for e in sent if "/i/" in e.body]
        assert any(e["action"] == "invitation_held" for e in app.state.store.audit_log(50, row["interview_id"]))
        refused = c.post(f"/api/interviews/{row['interview_id']}/invite", json={})
        assert refused.status_code == 409 and "doesn't resolve" in refused.json()["detail"]
        resolves["ok"] = True  # the tunnel is back
        assert c.post(f"/api/interviews/{row['interview_id']}/invite", json={}).status_code == 200
        assert [e for e in sent if "/i/" in e.body]
        assert c.get(f"/api/roles/{rid}/pipeline").json()["candidates"][0]["invitation_undelivered"] is False

    app, c = client(replace(settings, public_base_url=""))
    with c:
        from tests.test_roles import approved_role
        rid, _ = approved_role(c)
        r = c.put(f"/api/roles/{rid}/hiring", json={"meeting_link": ZOOM, "published": True})
        assert r.status_code == 409 and "Public URL" in r.json()["detail"]
