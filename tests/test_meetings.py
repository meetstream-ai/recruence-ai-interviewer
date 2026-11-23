"""Meetings (app/meetings.py): one record per interview, a candidate's own meeting set by hand, and booked interviews
that candidates join through their interview link (app/gateway.py), never a meeting link in an email."""

from dataclasses import replace
from datetime import timedelta

import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import gateway
from app.models import utcnow
from app.store import meetings
from tests.test_api import TUNNEL, ready_interview_id
from tests.test_booking import offer, times
from tests.test_gateway import code_from, invited, verified_client

OWN = "https://meet.google.com/own-room-xyz"


def test_a_candidate_gets_their_own_meeting_and_the_link_changes_nothing_for_them(settings):
    app, c, store, sent, interview, token = invited(settings)
    try:
        url = f"/api/interviews/{interview.id}/meeting"
        before = c.get(f"/api/interviews/{interview.id}").json()["meeting"]
        assert before["shared_room"] is True and before["own"] is None and before["capture"]["level"] == "not verified"  # the role's Zoom room
        assert c.put(url, json={"url": "https://example.com/not-a-meeting"}).status_code == 422
        r = c.put(url, json={"url": OWN}).json()
        assert r["meeting"]["own"]["url"] == OWN and r["meeting"]["own"]["platform"] == "Google Meet" and r["meeting"]["shared_room"] is False
        assert r["meeting"]["capture"]["level"] == "verified" and store.get_interview(interview.id).meeting_link == OWN
        with store._engine.connect() as conn:
            row = conn.execute(sa.select(meetings)).mappings().one()
        assert row["mode"] == "manual" and row["platform"] == "Google Meet" and OWN not in str(dict(row))  # encrypted at rest
        assert OWN not in str(store.audit_log(50, interview.id))  # the audit log names the platform, never the link
        # The candidate's invitation already went out: no new email, the interview link now leads to the new meeting.
        emails = len(sent)
        mine = verified_client(app, token, sent)
        started = mine.post(f"/api/public/interview/{token}/start", json={"acknowledged": True}, headers=TUNNEL).json()
        assert started["meeting_link"] == OWN and len([e for e in sent if "code" not in e.subject.lower()]) == emails
        # Not while it's under way; afterwards, back to the role's room.
        assert c.put(url, json={"url": "https://meet.google.com/other-room-abc"}).status_code == 409
    finally:
        c.__exit__(None, None, None)


def test_back_to_the_roles_room(settings):
    app, c, store, sent, interview, token = invited(settings)
    try:
        url = f"/api/interviews/{interview.id}/meeting"
        c.put(url, json={"url": OWN})
        back = c.delete(url).json()["meeting"]
        assert back["own"] is None and back["shared_room"] is True and store.meeting_for(interview.id) is None
        assert store.get_interview(interview.id).meeting_link.startswith("https://zoom.us/")
        providers = c.get("/api/meetings/providers").json()
        assert [p["id"] for p in providers["providers"]] == ["manual"] and providers["providers"][0]["capabilities"]["create"] is False
        assert set(providers["capture"]) == {"Google Meet", "Zoom", "Microsoft Teams"}
    finally:
        c.__exit__(None, None, None)


def test_a_booked_interview_opens_through_the_interview_link_at_its_time(settings):
    from app.llm import OfflineAI
    from app.server import create_app
    from app.store import Store
    from tests.fakes import FakeMeetStream
    sent = []
    s = replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com")
    app = create_app(settings=s, store=Store(s.database), ai=OfflineAI(), meetstream_client_factory=FakeMeetStream, run_loops=False,
                     mail_send=lambda _, e: sent.append(e))
    c = TestClient(app)
    store = app.state.store
    with c:
        iid = ready_interview_id(c)
        store.save_interview(store.get_interview(iid).model_copy(update={
            "setup": store.get_interview(iid).setup.model_copy(update={"candidate_email": "priya@example.com"})}))
        slots = times(0, 2)
        token, _ = offer(c, iid, slots)
        booked = c.post(f"/api/public/booking/{token}", json={"slot": slots[1], "acknowledged": True}, headers=TUNNEL).json()
        link = booked["interview_link"].rsplit("/", 1)[1]
        # Before its time: not open yet, and nothing reveals the meeting.
        view = TestClient(app).get(f"/api/public/interview/{link}", headers=TUNNEL).json()
        assert view["state"] == "not_yet" and view["meeting_link"] is None
        # Move the booked time to a few minutes from now: inside the window.
        i = store.get_interview(iid)
        store.save_interview(i.model_copy(update={"scheduled_at": utcnow() + timedelta(minutes=5)}))
        d = TestClient(app)
        assert d.post(f"/api/public/interview/{link}/start", json={"acknowledged": True}, headers=TUNNEL).status_code == 403  # code first
        assert d.post(f"/api/public/interview/{link}/code", headers=TUNNEL).status_code == 200
        assert d.post(f"/api/public/interview/{link}/verify", json={"code": code_from(sent)}, headers=TUNNEL).status_code == 200
        confirmation = next(e for e in sent if e.subject.startswith("Confirmed:"))
        assert f"/i/{link}" in confirmation.body and "zoom.us" not in confirmation.body  # the email never carried the meeting
        joined = d.post(f"/api/public/interview/{link}/start", json={"acknowledged": True}, headers=TUNNEL).json()
        assert joined["state"] == "ready_to_join" and joined["meeting_link"] == "https://zoom.us/j/1234567890"
        assert app.state.manager.live == {}  # nothing started here: the scheduler starts the bot at the time
        assert TestClient(app).get(f"/api/public/interview/{link}", headers=TUNNEL).json()["meeting_link"] is None  # only that device
        assert gateway.token_hash(link) != link
