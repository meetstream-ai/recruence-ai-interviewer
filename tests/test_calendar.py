"""Upcoming meetings from the calendar connected in MeetStream (read only)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import meetstream
from fastapi.testclient import TestClient

from app.llm import OfflineAI
from app.meeting.calendar import staleness, sync_problems, upcoming_meetings
from app.models import utcnow
from app.server import create_app
from app.store import Store
from tests.fakes import FakeMeetStream

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def event(start, link="https://meet.google.com/abc-defg-hij", title="Interview: Backend", **extra):
    return {"id": f"evt_{start}", "start_time": start, "end_time": None, "meeting_url": link, "is_deleted": False,
            "raw": {"summary": title, "attendees": [{"email": "someone@example.com"}]}, **extra}


def test_only_upcoming_meetings_with_a_supported_link():
    data = {"results": [
        event("2026-11-03T10:00:00Z", title="  Later   one "),
        event("2026-11-02T08:55:00Z", title="Started 5 minutes ago"),          # within the grace period
        event("2026-11-02T08:30:00Z", title="Started half an hour ago"),       # too late
        event("2026-11-02T12:00:00Z", link="https://teams.live.com/meet/123?p=x", title="Teams"),
        event("2026-11-02T13:00:00Z", link="https://company.webex.com/meet/x"),  # not Meet / Zoom / Teams
        event("2026-11-02T14:00:00Z", link=""),                                  # no meeting link
        event("2026-11-02T15:00:00Z", is_deleted=True),
        event("not a date"),
        event("2026-11-02T16:00:00+05:30", title=""),                          # 10:30 UTC; untitled
        "junk",
    ], "has_more": True}
    got = upcoming_meetings(data, NOW)
    assert [(m["title"], m["platform"]) for m in got] == [
        ("Started 5 minutes ago", "Google Meet"),
        ("(no title)", "Google Meet"),   # sorted by actual time, not by the text of the timestamp
        ("Teams", "Microsoft Teams"),
        ("Later one", "Google Meet"),
    ]
    assert set(got[0]) == {"id", "title", "start", "end", "meeting_link", "platform"}  # no attendees passed on
    assert upcoming_meetings(None, NOW) == [] and upcoming_meetings({"results": "x"}, NOW) == []


class FakeCalendar:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.calls = data, error, 0

    async def events(self, **kw):
        self.kwargs = getattr(self, "kwargs", []) + [kw]
        self.calls += 1
        if self.error:
            raise self.error
        return self.data


def client_with(settings, calendar):
    fake = FakeMeetStream()
    fake.calendar = calendar
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), meetstream_client_factory=lambda: fake, run_loops=False)
    return TestClient(app)


def test_calendar_events_route(settings):
    soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    calendar = FakeCalendar({"results": [event(soon)], "has_more": False})
    with client_with(settings, calendar) as c:
        body = c.get("/api/calendar/events").json()
        assert body["more"] is False and [m["platform"] for m in body["events"]] == ["Google Meet"]
        assert calendar.calls == 1

    with client_with(settings, FakeCalendar(error=meetstream.ServerError("down", status=500))) as c:
        r = c.get("/api/calendar/events")
        assert r.status_code == 502 and "calendar connected in MeetStream" in r.json()["detail"]

    with client_with(replace(settings, meetstream_api_key=""), FakeCalendar()) as c:
        assert c.get("/api/calendar/events").status_code == 503


def test_calendar_resync_forces_a_sync_and_records_its_status(settings):
    soon = (utcnow() + timedelta(hours=2)).isoformat()
    calendar = FakeCalendar({"results": [event(soon)], "has_more": False})
    with client_with(settings, calendar) as c:
        assert c.get("/api/calendar/status").json() == {"sync": None}
        body = c.post("/api/calendar/resync").json()
        assert calendar.kwargs[-1] == {"params": {"sync": "true"}}
        assert body["sync"]["ok"] is True and body["sync"]["events"] == 1 and len(body["events"]) == 1
        first_ok = body["sync"]["at"]
        calendar.error = meetstream.ServerError("calendar token expired", status=500)
        r = c.post("/api/calendar/resync")
        assert r.status_code == 502 and "calendar token expired" in r.json()["detail"]
        status = c.get("/api/calendar/status").json()["sync"]
        assert status["ok"] is False and status["last_ok_at"] == first_ok


def test_a_calendar_copy_that_stopped_changing_is_reported():
    """R-10 (B-11, 13 November): MeetStream's copy had stopped at 26 October; every resync still "succeeded"."""
    old = {"results": [event("2026-11-03T10:00:00Z", updated_at="2026-10-26T05:05:57Z", created_at="2026-10-20T00:00:00Z"),
                       event("2026-11-04T10:00:00Z", updated_at="2026-10-25T00:00:00Z")]}
    assert staleness(old, datetime(2026, 11, 13, 9, 27, tzinfo=timezone.utc)) == {"since": "2026-10-26T05:05:57+00:00", "days": 18}
    fresh = {"results": [event("2026-11-03T10:00:00Z", updated_at="2026-11-12T23:00:00Z")]}
    assert staleness(fresh, datetime(2026, 11, 13, 9, 27, tzinfo=timezone.utc)) is None
    assert staleness({"results": [event("2026-11-03T10:00:00Z")]}, NOW) is None  # no dates to judge by: say nothing
    assert staleness(None, NOW) is None and staleness({"results": "x"}, NOW) is None


def test_resync_and_the_picker_say_when_the_copy_is_outdated(settings):
    soon = (utcnow() + timedelta(hours=2)).isoformat()
    weeks_ago = (utcnow() - timedelta(days=18)).isoformat()
    calendar = FakeCalendar({"results": [event(soon, updated_at=weeks_ago)], "has_more": False})
    with client_with(settings, calendar) as c:
        body = c.post("/api/calendar/resync").json()
        assert body["sync"]["ok"] is True and body["stale"]["days"] == 18 and body["sync"]["stale"] == body["stale"]
        assert c.get("/api/calendar/status").json()["sync"]["stale"]["days"] == 18  # shown when the page opens
        assert c.get("/api/calendar/events").json()["stale"]["days"] == 18
        calendar.data = {"results": [event(soon, updated_at=utcnow().isoformat())], "has_more": False}
        assert c.post("/api/calendar/resync").json()["stale"] is None
        assert c.get("/api/calendar/status").json()["sync"]["stale"] is None


REFRESH_ERROR = "Failed to fetch calendar events: Failed to refresh token: 400 Client Error: Bad Request for url: https://oauth2.googleapis.com/token"


def test_calendars_meetstream_couldnt_read_are_reported():
    """R-14, seen live on 14 November: MeetStream answered 200 with an old copy and said why in `partial_errors`."""
    problems = sync_problems({"results": [], "partial_errors": {"google::recruiter@example.com": REFRESH_ERROR,
                                                                "outlook::ops@example.com": "Service unavailable"}})
    google, outlook = problems
    assert google["provider"] == "Google" and google["account"] == "recruiter@example.com" and google["reconnect"] is True
    assert "Reconnect it in MeetStream" in google["advice"] and google["message"] == REFRESH_ERROR
    assert outlook["provider"] == "Outlook" and outlook["reconnect"] is False and "couldn't read this Outlook calendar" in outlook["advice"]
    assert sync_problems({"results": [], "partial_errors": None}) == [] and sync_problems(None) == []
    assert sync_problems({"partial_errors": {"google::x": "e" * 1000}})[0]["message"] == "e" * 300


def test_resync_and_the_picker_report_meetstreams_calendar_errors(settings):
    soon = (utcnow() + timedelta(hours=2)).isoformat()
    calendar = FakeCalendar({"results": [event(soon)], "has_more": False,
                             "partial_errors": {"google::recruiter@example.com": REFRESH_ERROR}})
    with client_with(settings, calendar) as c:
        body = c.post("/api/calendar/resync").json()
        assert body["sync"]["ok"] is True and body["problems"][0]["reconnect"] is True and body["sync"]["problems"] == body["problems"]
        assert c.get("/api/calendar/status").json()["sync"]["problems"][0]["provider"] == "Google"  # shown when the page opens
        assert c.get("/api/calendar/events").json()["problems"][0]["account"] == "recruiter@example.com"
        audit = [a for a in c.get("/api/audit").json() if a["action"] == "calendar_resynced"][0]
        assert "1 calendar(s) MeetStream couldn't read (Google)" in audit["detail"] and "example.com" not in audit["detail"]
        calendar.data = {"results": [event(soon)], "has_more": False, "partial_errors": None}
        assert c.post("/api/calendar/resync").json()["problems"] == []
        assert c.get("/api/calendar/status").json()["sync"]["problems"] == []
