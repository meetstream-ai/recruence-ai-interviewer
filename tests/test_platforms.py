import pytest

from app.meeting.platforms import GOOGLE_MEET, TEAMS, ZOOM, UnsupportedMeetingLink, detect_platform


@pytest.mark.parametrize(
    "link, platform",
    [
        ("https://meet.google.com/abc-defg-hij", GOOGLE_MEET),
        ("https://us02web.zoom.us/j/81234567890?pwd=abc", ZOOM),
        ("https://zoom.us/j/81234567890", ZOOM),
        ("https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc", TEAMS),
        ("https://teams.live.com/meet/9876543210", TEAMS),
        ("  https://meet.google.com/abc-defg-hij  ", GOOGLE_MEET),
    ],
)
def test_supported_links(link, platform):
    assert detect_platform(link) is platform


@pytest.mark.parametrize(
    "link, reason",
    [
        ("http://meet.google.com/abc-defg-hij", "https"),
        ("meet.google.com/abc-defg-hij", "full meeting link"),
        ("https://meet.google.com/lookup/abc", "/lookup/"),
        ("https://meet.google.com/", "no meeting code"),
        ("https://g.co/meet/abc", "g.co"),
        ("https://zoom.us/my/someone", "personal meeting room"),
        ("https://zoom.us/w/123", "webinar"),
        ("https://zoom.us/s/123", "/s/"),
        ("https://zoom.us/wc/join/123", "web-client"),
        ("https://acme.zoomgov.com/j/123", "ZoomGov"),
        ("https://teams.cloud.microsoft/meet/123", "teams.cloud.microsoft"),
        ("https://teams.microsoft.com/", "no meeting path"),
        ("https://whereby.com/room", "Only Google Meet"),
        ("", "full meeting link"),
    ],
)
def test_unsupported_links_explain_why(link, reason):
    with pytest.raises(UnsupportedMeetingLink, match=reason):
        detect_platform(link)


def test_lobby_limits_follow_meetstream_docs():
    assert (GOOGLE_MEET.max_waiting_room_seconds, ZOOM.max_waiting_room_seconds, TEAMS.max_waiting_room_seconds) == (600, 1200, 1800)
