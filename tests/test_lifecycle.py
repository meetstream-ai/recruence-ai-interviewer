"""Interview lifecycle: stages, booking-link expiry, cancel and reopen, invitation marker, email retries."""

import smtplib
from dataclasses import replace
from datetime import timedelta

import pytest

from app import booking as bookings
from app.lifecycle import can_cancel, stage
from app.mailer import Email, Mailer, MailError, invitation
from app.models import Booking, InterviewOptions, utcnow
from tests.fakes import MEET_LINK
from tests.test_api import TUNNEL, make_client, ready_interview_id
from tests.test_booking import ZOOM, offer, times
from tests.test_mailer import SMTP, client as mail_client, sender


def test_stages(ready_interview):
    now = utcnow()
    i = ready_interview
    b = Booking(token="t" * 32, meeting_link=ZOOM, platform="Zoom", slots=[now + timedelta(days=1)], created_at=now)
    assert stage(i.model_copy(update={"status": "draft"}), False) == "draft"
    assert stage(i, False) == "ready"
    assert stage(i.model_copy(update={"invited_at": now}), False) == "invitation_sent"
    assert stage(i.model_copy(update={"booking": b}), False) == "pending_candidate"
    assert stage(i.model_copy(update={"booking": b.model_copy(update={"expires_at": now - timedelta(minutes=1)})}), False) == "expired"
    assert stage(i.model_copy(update={"status": "scheduled", "scheduled_at": now}), False) == "scheduled"
    assert stage(i.model_copy(update={"status": "in_progress"}), False) == "in_progress"
    assert stage(i.model_copy(update={"status": "completed"}), False) == "evaluation_pending"
    assert stage(i.model_copy(update={"status": "completed"}), True) == "report_ready"
    assert stage(i.model_copy(update={"status": "failed"}), False) == "failed"
    assert stage(i.model_copy(update={"cancelled_at": now, "invited_at": now}), False) == "cancelled"
    assert can_cancel(i) is None and can_cancel(i.model_copy(update={"status": "in_progress"}))
    assert can_cancel(i.model_copy(update={"status": "completed"})) and can_cancel(i.model_copy(update={"cancelled_at": now}))


def test_link_expiry_rules():
    now = utcnow().replace(microsecond=0)  # offered times are whole seconds (clean_slots)
    slots = [now + timedelta(days=1), now + timedelta(days=2)]
    assert bookings.clean_expiry(None, slots, now) == slots[1]  # default: the last offered time
    assert bookings.clean_expiry(now + timedelta(days=5), slots, now) == slots[1]  # brought back to it
    assert bookings.clean_expiry(now + timedelta(hours=3), slots, now) == now + timedelta(hours=3)
    with pytest.raises(bookings.BookingError):
        bookings.clean_expiry(now + timedelta(minutes=5), slots, now)


def test_an_expired_link_stops_taking_bookings(settings):
    app, c = make_client(settings)
    store = app.state.store
    with c:
        iid = ready_interview_id(c)
        slots = times(0, 2, 4)
        until = (utcnow() + timedelta(hours=6)).replace(microsecond=0)
        r = c.post(f"/api/interviews/{iid}/booking", json={"meeting_link": ZOOM, "slots": slots, "expires_at": until.isoformat()})
        assert r.status_code == 200
        token = r.json()["url"].rsplit("/", 1)[1]
        view = c.get(f"/api/public/booking/{token}", headers=TUNNEL).json()
        assert view["state"] == "open" and view["expires_at"] == until.isoformat()
        assert c.get(f"/api/interviews/{iid}").json()["stage"] == "pending_candidate"
        assert "Please choose by" in c.post(f"/api/interviews/{iid}/invite/preview", json={}).json()["body"]

        # Time passes: the deadline is now behind us.
        i = store.get_interview(iid)
        store.save_interview(i.model_copy(update={"booking": i.booking.model_copy(update={"expires_at": utcnow() - timedelta(minutes=1)})}))
        view = c.get(f"/api/public/booking/{token}", headers=TUNNEL).json()
        assert view["state"] == "expired" and view["slots"] == [] and view["expires_at"] is None
        assert c.post(f"/api/public/booking/{token}", json={"slot": slots[0], "acknowledged": True}, headers=TUNNEL).status_code == 410
        assert c.get(f"/api/interviews/{iid}").json()["stage"] == "expired"
        assert [x["stage"] for x in c.get("/api/interviews", params={"stage": "expired"}).json()] == ["expired"]
        assert c.post(f"/api/interviews/{iid}/invite/preview", json={}).status_code == 409


def test_a_booked_time_outlives_the_deadline(settings):
    app, c = make_client(settings)
    store = app.state.store
    with c:
        iid = ready_interview_id(c)
        token, _ = offer(c, iid, times(0, 2))
        c.post(f"/api/public/booking/{token}", json={"slot": times(0)[0], "acknowledged": True}, headers=TUNNEL)
        i = store.get_interview(iid)
        store.save_interview(i.model_copy(update={"booking": i.booking.model_copy(update={"expires_at": utcnow() - timedelta(minutes=1)})}))
        assert c.get(f"/api/public/booking/{token}", headers=TUNNEL).json()["state"] == "booked"
        assert c.get(f"/api/interviews/{iid}").json()["stage"] == "scheduled"


def test_cancel_and_reopen(settings):
    app, c = make_client(settings)
    with c:
        iid = ready_interview_id(c)
        token, _ = offer(c, iid, times(0, 2))
        c.post(f"/api/public/booking/{token}", json={"slot": times(0)[0], "acknowledged": True}, headers=TUNNEL)
        assert c.get(f"/api/interviews/{iid}").json()["interview"]["status"] == "scheduled"

        r = c.post(f"/api/interviews/{iid}/cancel", json={})
        assert r.status_code == 200
        i = r.json()
        assert i["cancelled_at"] and i["status"] == "ready" and i["scheduled_at"] is None and i["booking"] is None
        assert c.get(f"/api/public/booking/{token}", headers=TUNNEL).status_code == 404  # the link is dead
        assert app.state.store.due_interviews(utcnow() + timedelta(days=3)) == []  # nothing for the scheduler
        assert c.get(f"/api/interviews/{iid}").json()["stage"] == "cancelled"
        assert c.post(f"/api/interviews/{iid}/cancel", json={}).status_code == 409
        launch = {"meeting_link": MEET_LINK, "consent_attested": True}
        assert c.post(f"/api/interviews/{iid}/launch", json=launch).status_code == 409
        assert c.post(f"/api/interviews/{iid}/booking", json={"meeting_link": ZOOM, "slots": times(0)}).status_code == 409
        assert c.post(f"/api/interviews/{iid}/invite/preview", json={"meeting_link": MEET_LINK}).status_code == 409

        assert c.post(f"/api/interviews/{iid}/reopen").json()["cancelled_at"] is None
        assert c.get(f"/api/interviews/{iid}").json()["stage"] == "ready"
        assert c.post(f"/api/interviews/{iid}/reopen").status_code == 409
        actions = [e["action"] for e in c.get(f"/api/interviews/{iid}").json()["audit"]]
        assert {"interview_cancelled", "interview_reopened"} <= set(actions)


def test_cancelling_can_tell_the_candidate_and_invitations_are_marked(settings, ready_interview):
    sent, send = sender()
    app, c = mail_client(replace(settings, **SMTP), send)
    store = app.state.store
    iid = ready_interview.id
    store.save_interview(store.get_interview(iid).model_copy(
        update={"setup": ready_interview.setup.model_copy(update={"candidate_email": "priya@example.com"})}))
    with c:
        assert c.post(f"/api/interviews/{iid}/invite", json={"when_text": "Mon 10:00"}).status_code == 200
        assert store.get_interview(iid).invited_at is not None
        assert c.get(f"/api/interviews/{iid}").json()["stage"] == "invitation_sent"
        assert c.post(f"/api/interviews/{iid}/cancel", json={"notify_candidate": True}).status_code == 200
    # The cancellation goes out in the background; the client's shutdown waits for it.
    assert [e.subject for e in sent] == ["Your interview for the Backend Engineer role",
                                         "Your interview for the Backend Engineer role is cancelled"]
    assert sent[1].to == ["priya@example.com"] and "cancelled" in sent[1].body


def test_analyst_invitation_says_a_person_interviews(settings, ready_interview):
    i = ready_interview.model_copy(update={"options": InterviewOptions(mode="analyst")})
    body = invitation(i, replace(settings, **SMTP), MEET_LINK, "Mon 10:00").body
    assert "A member of the hiring team interviews you" in body and "doesn't speak" in body
    assert "conducted by an AI interviewer" not in body


async def test_background_emails_are_retried(settings):
    calls = []

    def flaky(settings, email):
        calls.append(email)
        if len(calls) < 3:
            raise smtplib.SMTPServerDisconnected("dropped")

    m = Mailer(replace(settings, **SMTP), send=flaky)
    m.retry_delays = (0.01, 0.01)
    assert await m.send_retrying(Email(to=["a@example.com"], subject="s", body="b")) == 3

    def broken(settings, email):
        raise smtplib.SMTPServerDisconnected("down")

    m = Mailer(replace(settings, **SMTP), send=broken)
    m.retry_delays = (0.01, 0.01)
    with pytest.raises(MailError):
        await m.send_retrying(Email(to=["a@example.com"], subject="s", body="b"))
