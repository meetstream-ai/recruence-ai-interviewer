"""Candidate self-scheduling: the recruiter offers times, the candidate picks one on a public page."""

from dataclasses import replace
from datetime import timedelta

from app.models import utcnow
from tests.fakes import MEET_LINK
from tests.test_api import SETUP, TUNNEL, make_client, ready_interview_id

ZOOM = "https://zoom.us/j/1234567890"


def times(*hours):
    base = (utcnow() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    return [(base + timedelta(hours=h)).isoformat() for h in hours]


def offer(c, iid, slots=None, link=ZOOM):
    r = c.post(f"/api/interviews/{iid}/booking", json={"meeting_link": link, "slots": slots or times(0, 2, 4)})
    assert r.status_code == 200, r.text
    return r.json()["url"].rsplit("/", 1)[1], r.json()


def test_candidate_books_changes_and_cancels(settings):
    app, c = make_client(settings)
    with c:
        iid = ready_interview_id(c)
        slots = times(0, 2, 4)
        token, created = offer(c, iid, slots)
        assert created["url"] == f"https://tunnel.example.com/book/{token}"
        assert c.get(f"/api/interviews/{iid}").json()["booking_url"] == created["url"]

        # The page and its API work through the public tunnel, without signing in.
        assert c.get(f"/book/{token}", headers=TUNNEL).status_code == 200
        assert c.get("/static/book.js", headers=TUNNEL).status_code == 200
        assert c.get("/static/logo.png", headers=TUNNEL).headers["content-type"] == "image/png"
        view = c.get(f"/api/public/booking/{token}", headers=TUNNEL).json()
        assert view["state"] == "open" and view["first_name"] == "Priya" and view["platform"] == "Zoom"
        assert view["slots"] == slots and view["meeting_link"] is None  # the link only once booked
        # Nothing about the candidate beyond the first name; nothing from the resume or JD.
        text = str(view)
        for private in ("Raman", "Seven years", "PostgreSQL", "@"):
            assert private not in text

        assert c.post(f"/api/public/booking/{token}", json={"slot": slots[1]}, headers=TUNNEL).status_code == 422  # notice not confirmed
        booked = c.post(f"/api/public/booking/{token}", json={"slot": slots[1], "acknowledged": True}, headers=TUNNEL).json()
        assert booked["state"] == "booked" and booked["booked_at"] == slots[1] and booked["can_change"]
        assert booked["meeting_link"] is None and "/i/" in booked["interview_link"]  # the meeting only behind the interview link
        link = booked["interview_link"].rsplit("/", 1)[1]
        assert c.get(f"/api/public/booking/{token}", headers=TUNNEL).json()["interview_link"] == booked["interview_link"]
        i = c.get(f"/api/interviews/{iid}").json()["interview"]
        assert i["status"] == "scheduled" and i["meeting_link"] == ZOOM and i["platform"] == "Zoom" and i["consent_attested_at"]
        ics = c.get(f"/api/public/booking/{token}/calendar.ics", headers=TUNNEL)
        assert ics.headers["content-type"].startswith("text/calendar") and "DTSTART:" in ics.text and ZOOM not in ics.text
        assert f"/i/{link}" in ics.text and "Recruence" not in ics.text

        changed = c.post(f"/api/public/booking/{token}", json={"slot": slots[2], "acknowledged": True}, headers=TUNNEL).json()
        assert changed["booked_at"] == slots[2]
        assert changed.get("interview_link") == booked["interview_link"]  # a new time keeps the same link
        cancelled = c.post(f"/api/public/booking/{token}/cancel", headers=TUNNEL).json()
        assert cancelled["state"] == "open"
        assert c.get(f"/api/public/interview/{link}", headers=TUNNEL).status_code == 404  # the link for that time stops working
        assert c.get(f"/api/interviews/{iid}").json()["interview"]["status"] == "ready"

        actions = [e["action"] for e in c.get(f"/api/interviews/{iid}").json()["audit"]]
        assert {"booking_link_created", "booked_by_candidate", "booking_cancelled_by_candidate"} <= set(actions)


def test_rules_for_the_candidate(settings):
    app, c = make_client(settings)
    with c:
        iid = ready_interview_id(c)
        slots = times(0, 2)
        token, _ = offer(c, iid, slots)
        later = times(9)[0]
        r = c.post(f"/api/public/booking/{token}", json={"slot": later, "acknowledged": True}, headers=TUNNEL)
        assert r.status_code == 422 and "isn't one of the times offered" in r.json()["detail"]
        assert c.post(f"/api/public/booking/{token}/cancel", headers=TUNNEL).status_code == 409  # nothing booked
        assert c.get("/api/public/booking/not-a-real-token-at-all-xx", headers=TUNNEL).status_code == 404
        assert c.get("/api/public/booking/short", headers=TUNNEL).status_code == 404

        # Too close to the start to change: book, then move the interview's time to within the cutoff.
        c.post(f"/api/public/booking/{token}", json={"slot": slots[0], "acknowledged": True}, headers=TUNNEL)
        store = app.state.store
        i = store.get_interview(iid)
        store.save_interview(i.model_copy(update={"scheduled_at": utcnow() + timedelta(minutes=20)}))
        view = c.get(f"/api/public/booking/{token}", headers=TUNNEL).json()
        assert view["state"] == "booked" and not view["can_change"]
        r = c.post(f"/api/public/booking/{token}", json={"slot": slots[1], "acknowledged": True}, headers=TUNNEL)
        assert r.status_code == 409 and "too close" in r.json()["detail"]

        # Once it has run, the page says so.
        store.save_interview(store.get_interview(iid).model_copy(update={"status": "completed"}))
        assert c.get(f"/api/public/booking/{token}", headers=TUNNEL).json()["state"] == "closed"


def test_withdrawn_link_stops_working_and_recruiter_rules(settings):
    app, c = make_client(settings)
    with c:
        draft = c.post("/api/interviews", json=SETUP).json()["id"]
        r = c.post(f"/api/interviews/{draft}/booking", json={"meeting_link": ZOOM, "slots": times(1)})
        assert r.status_code == 409 and "Approve" in r.json()["detail"]
        iid = ready_interview_id(c)
        past = [(utcnow() - timedelta(hours=1)).isoformat()]
        assert c.post(f"/api/interviews/{iid}/booking", json={"meeting_link": ZOOM, "slots": past}).status_code == 422
        assert c.post(f"/api/interviews/{iid}/booking", json={"meeting_link": "https://example.com/x", "slots": times(1)}).status_code == 422
        token, _ = offer(c, iid)
        assert c.delete(f"/api/interviews/{iid}/booking").status_code == 200
        assert c.get(f"/api/public/booking/{token}", headers=TUNNEL).status_code == 404
        assert c.get(f"/api/interviews/{iid}").json()["booking_url"] is None
        # A new link replaces the old token.
        t1, _ = offer(c, iid)
        t2, _ = offer(c, iid)
        assert c.get(f"/api/public/booking/{t1}", headers=TUNNEL).status_code == 404
        assert c.get(f"/api/public/booking/{t2}", headers=TUNNEL).status_code == 200
        # Recruiter endpoints stay closed on the public address.
        assert c.post(f"/api/interviews/{iid}/booking", json={"meeting_link": ZOOM, "slots": times(1)}, headers=TUNNEL).status_code == 403
        assert c.get("/static/app.js", headers=TUNNEL).status_code == 403


def test_invitation_carries_the_booking_link(settings):
    app, c = make_client(settings)
    with c:
        iid = ready_interview_id(c)
        token, _ = offer(c, iid)
        body = c.post(f"/api/interviews/{iid}/invite/preview", json={}).json()["body"]
        assert f"/book/{token}" in body and "choose a time" in body and "recorded and transcribed" in body
        c.post(f"/api/public/booking/{token}", json={"slot": times(0)[0], "acknowledged": True}, headers=TUNNEL)
        booked_body = c.post(f"/api/interviews/{iid}/invite/preview", json={"meeting_link": MEET_LINK}).json()["body"]
        assert "/book/" not in booked_body  # once booked, the invitation names the call as usual


def test_scheduled_launch_uses_the_booked_time(settings):
    """The app's scheduler picks a candidate-booked interview up like any scheduled one."""
    app, c = make_client(settings)
    with c:
        iid = ready_interview_id(c)
        token, _ = offer(c, iid, times(0))
        c.post(f"/api/public/booking/{token}", json={"slot": times(0)[0], "acknowledged": True}, headers=TUNNEL)
        store = app.state.store
        due = store.due_interviews(utcnow() + timedelta(days=2))
        assert [i.id for i in due] == [iid] and due[0].consent_attested_at is not None


def test_booking_survives_with_the_interview_and_goes_with_it(settings):
    app, c = make_client(replace(settings))
    with c:
        iid = ready_interview_id(c)
        token, _ = offer(c, iid)
        assert c.delete(f"/api/interviews/{iid}").status_code == 200
        assert c.get(f"/api/public/booking/{token}", headers=TUNNEL).status_code == 404


def test_confirmation_email_and_calendar_text(settings, ready_interview):
    from app import booking

    start = (utcnow() + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
    i = ready_interview.model_copy(update={"scheduled_at": start, "meeting_link": ZOOM,
                                           "setup": ready_interview.setup.model_copy(update={"candidate_email": "priya@example.com"})})
    join = "https://tunnel.example.com/i/link-token-for-the-candidate"
    mail = booking.confirmation(i, replace(settings, notify_emails=("team@example.com",)), join, "Lumen Labs")
    assert mail.to == ["priya@example.com"] and mail.subject.startswith("Confirmed:") and mail.subject.endswith("at Lumen Labs")
    assert "09:30 UTC" in mail.body and join in mail.body and ZOOM not in mail.body and "recorded and transcribed" in mail.body
    assert "one-time code" in mail.body and "The Lumen Labs hiring team" in mail.body
    [(name, mime, data)] = mail.attachments  # the calendar file goes with the confirmation, not only on the page
    assert name == "interview.ics" and mime == "text/calendar" and data.decode() == booking.ics(i, settings, join)
    note = booking.team_note(i, replace(settings, notify_emails=("team@example.com",)))
    assert note.to == ["team@example.com"] and "booked" in note.subject
    cal = booking.ics(i, settings)
    assert f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}" in cal and cal.endswith("END:VCALENDAR\r\n")


def test_email_times_use_the_configured_time_zone():
    from datetime import datetime, timezone

    from app.booking import when_text

    at = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)
    assert when_text(at) == "Tue 03 Nov 2026, 09:30 UTC"
    assert when_text(at, "Asia/Kolkata") == "Tue 03 Nov 2026, 15:00 IST"
    assert when_text(at, "Not/AZone") == "Tue 03 Nov 2026, 09:30 UTC"
