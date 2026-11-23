"""One interview per meeting room at a time, however it starts: the candidate's link, the recruiter, or the scheduler at a
booked time. A shared room's booked times aren't offered twice, and a recruiter can't book over another interview."""

from datetime import timedelta

import pytest

from app import booking as bookings
from app.interview.manager import ROOM_WAIT_MINUTES, LaunchError, RoomBusy
from app.models import utcnow
from tests.conftest import settle
from tests.test_manager import manager


def twin(store, interview, **update):
    """Another candidate's interview in the same meeting room."""
    other = store.create_interview(interview.setup.model_copy(update={"candidate_name": "Sam Lee"}))
    return store.save_interview(other.model_copy(update={
        "plan": interview.plan, "plan_approved_at": interview.plan_approved_at, "status": "ready",
        "meeting_link": interview.meeting_link + "/", "consent_attested_at": interview.consent_attested_at, **update}))


async def test_a_live_interview_keeps_its_room(settings, store, ready_interview):
    m, fake = manager(settings, store)
    other = twin(store, ready_interview)
    sid = await m.launch(ready_interview.id)
    await settle(lambda: fake.bots.created)
    with pytest.raises(RoomBusy, match="under way in this meeting room"):
        await m.launch(other.id)  # the same room, written with a trailing slash: still the same room
    assert len(m.live) == 1 and m.room_busy(other.meeting_link)
    await m.end(sid)
    await settle(lambda: not m.live)
    assert await m.launch(other.id)  # free again


async def test_a_booked_interview_waits_for_its_room_then_gives_up(settings, store, ready_interview):
    m, fake = manager(settings, store)
    now = utcnow()
    booked = twin(store, ready_interview, status="scheduled", scheduled_at=now - timedelta(minutes=1))
    sid = await m.launch(ready_interview.id)  # the interview before it is still going
    await settle(lambda: fake.bots.created)
    assert await m.launch_due(now) == []
    assert store.get_interview(booked.id).status == "scheduled"  # waiting, not failed
    assert any(e["action"] == "scheduled_launch_waiting" for e in store.audit_log(20, booked.id))
    assert await m.launch_due(now + timedelta(minutes=ROOM_WAIT_MINUTES + 1)) == []
    gave_up = store.get_interview(booked.id)
    assert gave_up.status == "failed" and "under way in this meeting room" in gave_up.last_error
    await m.end(sid)


def test_overlapping_times_in_one_room():
    start = utcnow().replace(microsecond=0) + timedelta(days=1)
    busy = [(start, start + timedelta(minutes=20) + bookings.ROOM_BUFFER)]
    assert bookings.clashes(start, 20, busy) and bookings.clashes(start + timedelta(minutes=25), 20, busy)
    assert bookings.clashes(start - timedelta(minutes=25), 20, busy)  # would run into it (with the gap)
    assert not bookings.clashes(start + timedelta(minutes=30), 20, busy) and not bookings.clashes(start - timedelta(minutes=31), 20, busy)


def test_a_time_taken_in_the_room_is_not_offered_or_accepted(settings, ready_interview):
    from app.models import Booking
    now = utcnow().replace(microsecond=0)
    slots = [now + timedelta(days=1), now + timedelta(days=1, hours=2)]
    interview = ready_interview.model_copy(update={"booking": Booking(
        token="t" * 32, meeting_link=ready_interview.meeting_link, platform="Google Meet", slots=slots, created_at=now)})
    busy = [(slots[0], slots[0] + timedelta(minutes=20) + bookings.ROOM_BUFFER)]
    view = bookings.public_view(interview, settings, now, busy=busy)
    assert view["slots"] == [slots[1].isoformat()]  # the first is someone else's
    with pytest.raises(bookings.BookingError, match="just taken"):
        bookings.book(interview, "t" * 32, slots[0], True, now, busy=busy)
    booked = bookings.book(interview, "t" * 32, slots[1], True, now, busy=busy)
    assert booked.scheduled_at == slots[1]
    # Their own booking stays on offer to them even if it overlaps someone else's later booking.
    mine = bookings.public_view(booked, settings, now, busy=[(slots[1], slots[1] + timedelta(minutes=30))])
    assert slots[1].isoformat() in mine["slots"]


def test_the_recruiter_cannot_book_over_another_interview_in_the_room(settings):
    from fastapi.testclient import TestClient

    from app.llm import OfflineAI
    from app.server import create_app
    from app.store import Store
    from tests.fakes import FakeMeetStream
    from tests.test_api import ready_interview_id
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), meetstream_client_factory=FakeMeetStream, run_loops=False)
    with TestClient(app) as c:
        first, second, third = ready_interview_id(c), ready_interview_id(c), ready_interview_id(c)
        room = "https://meet.google.com/abc-defg-hij"
        at = (utcnow() + timedelta(days=1)).replace(microsecond=0)
        body = {"meeting_link": room, "consent_attested": True, "scheduled_at": at.isoformat()}
        assert c.post(f"/api/interviews/{first}/launch", json=body).status_code == 200
        r = c.post(f"/api/interviews/{second}/launch", json={**body, "scheduled_at": (at + timedelta(minutes=15)).isoformat()})
        assert r.status_code == 409 and "booked in this meeting room" in r.json()["detail"]
        later = c.post(f"/api/interviews/{second}/launch", json={**body, "scheduled_at": (at + timedelta(hours=1)).isoformat()})
        assert later.status_code == 200
        other_room = c.post(f"/api/interviews/{third}/launch", json={**body, "meeting_link": "https://meet.google.com/xyz-abcd-efg"})
        assert other_room.status_code == 200  # another room at the same time is fine
    assert LaunchError  # RoomBusy is a LaunchError: the routes already turn it into a 409 with its reason


async def test_installations_sharing_a_database_leave_each_others_interviews_alone(settings, store, ready_interview):
    """Audit 2026-11-10 (H1): every installation on a shared database used to mark all unfinished sessions failed when
    it started, and every installation's scheduler started every booked interview."""
    from tests.fakes import FakeMeetStream
    from app.interview.manager import SessionManager
    from app.llm import OfflineAI
    mine = SessionManager(settings, store, OfflineAI(), FakeMeetStream, install_id="install-a")
    sid = await mine.launch(ready_interview.id)
    await settle(lambda: store.get_session(sid)["state"] != "created")
    theirs = SessionManager(settings, store, OfflineAI(), FakeMeetStream, install_id="install-b")
    await theirs.recover()  # another installation starts up
    assert store.get_session(sid)["state"] not in ("failed",) and store.get_interview(ready_interview.id).status == "in_progress"
    await mine.end(sid)

    # A booked interview is started once, by the first installation to claim it.
    booked = twin(store, ready_interview, meeting_link="https://meet.google.com/zzz-zzzz-zzz", status="scheduled",
                  scheduled_at=utcnow() - timedelta(minutes=1))
    assert store.claim_launch(booked.id, "install-b") is True
    assert await mine.launch_due() == []  # install-b has it
    assert store.get_interview(booked.id).status == "scheduled"
    store.release_launch(booked.id)
    assert len(await mine.launch_due()) == 1


async def test_an_installation_without_meetstream_leaves_booked_interviews_to_others(settings, store, ready_interview):
    from dataclasses import replace
    m, _ = manager(replace(settings, meetstream_api_key=""), store)
    booked = twin(store, ready_interview, status="scheduled", scheduled_at=utcnow() - timedelta(minutes=1))
    assert await m.launch_due() == []
    assert store.get_interview(booked.id).status == "scheduled"  # not marked failed by an install that can't run it


async def test_a_start_is_refused_when_the_public_url_leads_to_another_installation(settings, store, ready_interview):
    from dataclasses import replace

    from app.interview import manager as manager_module
    from app.interview.manager import SessionManager
    from app.llm import OfflineAI
    from tests.fakes import FakeMeetStream

    async def elsewhere():
        return False

    async def resolves(url, timeout_s=5.0):
        return True
    old = manager_module.public_host_resolves
    manager_module.public_host_resolves = resolves
    try:
        m = SessionManager(replace(settings, check_public_url=True), store, OfflineAI(), FakeMeetStream, reaches_this_app=elsewhere)
        with pytest.raises(LaunchError, match="leads to another Recruence"):
            await m.launch(ready_interview.id)
    finally:
        manager_module.public_host_resolves = old
