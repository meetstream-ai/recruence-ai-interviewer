import asyncio

import meetstream
import pytest

from app.meeting.meetstream import MeetingError, MeetStreamBot, erase_bot_data
from app.meeting.platforms import GOOGLE_MEET, TEAMS, detect_platform
from tests.fakes import MEET_LINK, FakeMeetStream, event, sentence


def make_bot(client, platform=GOOGLE_MEET, link=MEET_LINK, **kw):
    return MeetStreamBot(
        client, meeting_link=link, platform=platform, bot_name="AI Interviewer", public_base_url="https://tunnel.example.com/",
        webhook_token="tok", session_id="s1", interview_id="i1", poll_interval_s=0.01, **kw,
    )


async def joined(bot):
    task = asyncio.create_task(bot.join("cfg-1"))
    while bot.bot_id is None:
        await asyncio.sleep(0)
    await bot.handle_event(event("bot.inmeeting"))
    await task


def test_payload_attaches_mia_without_bridge_fields():
    p = make_bot(FakeMeetStream(), retention_hours=6).create_payload("cfg-1")
    assert p["agent_config_id"] == "cfg-1"
    assert "socket_connection_url" not in p and "live_audio_required" not in p
    assert p["callback_url"] == "https://tunnel.example.com/webhooks/meetstream/tok/events"
    assert p["live_transcription_required"] == {"webhook_url": "https://tunnel.example.com/webhooks/meetstream/tok/transcript"}
    assert p["recording_config"]["transcript"]["provider"] == {"meetstream_streaming": {"transcription_mode": "sentence"}}
    assert p["recording_config"]["retention"] == {"type": "timed", "hours": 6}
    assert p["video_required"] is False


def test_lobby_timeout_capped_per_platform():
    assert make_bot(FakeMeetStream(), waiting_room_timeout=1500).waiting_room_timeout == 600
    teams = make_bot(FakeMeetStream(), platform=TEAMS, link="https://teams.microsoft.com/l/x", waiting_room_timeout=1500)
    assert teams.create_payload("c")["automatic_leave"]["waiting_room_timeout"] == 1500


async def test_join_by_webhook_uses_session_as_idempotency_key():
    client = FakeMeetStream()
    bot = make_bot(client)
    await joined(bot)
    assert bot.in_meeting and client.bots.created[0]["idempotency_key"] == "s1"


async def test_join_by_status_poll_when_callback_is_lost():
    client = FakeMeetStream(status="InMeeting")
    bot = make_bot(client)
    await asyncio.wait_for(bot.join("cfg-1"), 2)
    assert bot.in_meeting


async def test_denied_join_raises():
    bot = make_bot(FakeMeetStream())
    task = asyncio.create_task(bot.join("cfg-1"))
    while bot.bot_id is None:
        await asyncio.sleep(0)
    await bot.handle_event(event("bot.denied", status="Denied", message="Host denied entry"))
    with pytest.raises(MeetingError, match="Host denied"):
        await task


async def test_create_failure_raises():
    bot = make_bot(FakeMeetStream(create_error=meetstream.BadRequestError("bad link", status=400)))
    with pytest.raises(MeetingError, match="bad link"):
        await bot.join("cfg-1")


async def test_transcript_dedup_spoofing_and_bot_lines():
    bot = make_bot(FakeMeetStream())
    lines = []

    async def on_line(speaker, text, is_bot):
        lines.append((speaker, text, is_bot))

    async def on_ended(reason):
        pass

    bot.bind(on_line, on_ended)
    await joined(bot)
    assert await bot.handle_transcript(sentence("Hello there", ts="t1"))
    assert not await bot.handle_transcript(sentence("Hello there", ts="t1"))  # duplicate delivery
    assert not await bot.handle_transcript(sentence("Spoofed", bot_id="other-bot"))
    assert not await bot.handle_transcript(sentence("   "))
    assert await bot.handle_transcript(sentence("Question one", speaker="ai interviewer"))
    assert lines == [("Priya Raman", "Hello there", False), ("ai interviewer", "Question one", True)]


async def test_meeting_ending_notifies_once_and_only_after_joining():
    bot = make_bot(FakeMeetStream())
    ended = []

    async def on_ended(reason):
        ended.append(reason)

    async def on_line(*a):
        pass

    bot.bind(on_line, on_ended)
    await joined(bot)
    await bot.handle_event(event("bot.stopped", status="Stopped", message="Meeting ended"))
    await bot.handle_event(event("bot.stopped", status="Stopped", message="again"))
    assert ended == ["bot.stopped: Meeting ended"] and not bot.in_meeting


async def test_leave_treats_202_as_success():
    client = FakeMeetStream(remove_error=meetstream.NotReadyError("accepted", status=202))
    bot = make_bot(client)
    await joined(bot)
    await bot.leave()
    assert client.bots.removed == ["bot-123"] and not bot.in_meeting


async def test_erase_outcomes():
    client = FakeMeetStream()
    assert await erase_bot_data(client, "b1") == "erased"

    async def not_found(bot_id):
        raise meetstream.NotFoundError("gone", status=404)

    client.bots.delete_data = not_found
    assert await erase_bot_data(client, "b1") == "not found (already deleted)"


def test_detect_platform_feeds_bot():
    assert make_bot(FakeMeetStream(), platform=detect_platform(MEET_LINK)).platform.name == "Google Meet"
