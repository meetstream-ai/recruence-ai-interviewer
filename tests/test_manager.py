from datetime import timedelta

from app.interview.manager import SessionManager
from app.llm import OfflineAI
from app.models import utcnow
from tests.conftest import settle
from tests.fakes import FakeMeetStream, event, sentence


def manager(settings, store, fake=None):
    fake = fake or FakeMeetStream()
    return SessionManager(settings, store, OfflineAI(), lambda: fake), fake


async def test_background_work_is_not_attributed_to_the_requester(settings, store):
    from app.store import CURRENT_ACTOR
    m, _ = manager(settings, store)
    token = CURRENT_ACTOR.set("ana@example.com (recruiter)")  # as the access guard does for a request
    try:
        async def later():
            store.audit("session_failed", "i1", "s1", "lobby timeout")
        await m.run_background(later(), "test")
    finally:
        CURRENT_ACTOR.reset(token)
    [entry] = store.audit_log(5, "i1")
    assert entry["action"] == "session_failed" and entry["actor"] == ""


async def test_launch_refuses_when_the_public_url_does_not_resolve(settings, store, ready_interview, monkeypatch):
    """Live 2026-11-01: a stopped quick tunnel in PUBLIC_BASE_URL made MeetStream answer 500. Catch it first."""
    from dataclasses import replace
    import pytest
    from app.interview import manager as manager_module
    from app.interview.manager import LaunchError

    looked_up = []

    async def resolves(url):
        looked_up.append(url)
        return False

    monkeypatch.setattr(manager_module, "public_host_resolves", resolves)
    m, fake = manager(replace(settings, check_public_url=True), store)
    with pytest.raises(LaunchError, match="doesn't resolve"):
        await m.launch(ready_interview.id)
    assert looked_up == [settings.public_base_url] and fake.mia.created == [] and fake.bots.created == []

    m, fake = manager(settings, store)  # check off (as in the other tests): no lookup, launch proceeds
    assert await m.launch(ready_interview.id)
    assert len(looked_up) == 1


async def test_public_host_resolves_rejects_urls_without_a_host():
    from app.interview.manager import public_host_resolves
    assert not await public_host_resolves("")  # no hostname: answered without any lookup
    assert not await public_host_resolves("not a url")


async def test_scheduler_launches_due_interviews(settings, store, ready_interview):
    store.save_interview(ready_interview.model_copy(update={"status": "scheduled", "scheduled_at": utcnow() + timedelta(minutes=10)}))
    m, fake = manager(settings, store)
    assert await m.launch_due() == []
    [sid] = await m.launch_due(utcnow() + timedelta(minutes=11))
    await settle(lambda: fake.bots.created)
    assert store.get_interview(ready_interview.id).status == "in_progress"
    assert store.get_interview(ready_interview.id).latest_session_id == sid
    await m.end(sid)
    await settle(lambda: store.get_session(sid)["state"] == "failed")  # ended before the bot was admitted


async def test_scheduled_launch_failure_is_recorded(settings, store, ready_interview):
    store.save_interview(
        ready_interview.model_copy(update={"status": "scheduled", "scheduled_at": utcnow(), "consent_attested_at": None})
    )
    m, _ = manager(settings, store)
    assert await m.launch_due(utcnow() + timedelta(seconds=1)) == []
    i = store.get_interview(ready_interview.id)
    assert i.status == "failed" and "Scheduled start failed" in i.last_error


async def test_recovery_after_restart(settings, store, ready_interview):
    store.create_session("s-old", ready_interview.id, "Google Meet")
    store.update_session("s-old", state="questioning", bot_id="bot-9", agent_config_id="cfg-9")
    store.save_interview(ready_interview.model_copy(update={"status": "in_progress", "latest_session_id": "s-old"}))
    m, fake = manager(settings, store)
    await m.recover()
    assert store.get_session("s-old")["state"] == "failed" and store.get_session("s-old")["end_reason"] == "server_restarted"
    assert store.get_interview(ready_interview.id).status == "failed"
    assert fake.bots.removed == ["bot-9"] and fake.mia.deleted == ["cfg-9"]


async def test_retention_purges_and_erases(settings, store, ready_interview):
    store.create_session("s1", ready_interview.id, "Google Meet")
    store.update_session("s1", bot_id="bot-1", state="completed")
    store.save_interview(ready_interview.model_copy(update={"status": "completed"}))
    m, fake = manager(settings, store)
    assert await m.purge_expired() == []
    assert await m.purge_expired(utcnow() + timedelta(days=31)) == [ready_interview.id]
    assert store.get_interview(ready_interview.id) is None and fake.bots.erased == ["bot-1"]
    assert store.audit_log(1)[0]["action"] == "interview_deleted"


async def test_no_consent_removes_stored_candidate_lines(settings, store, ready_interview):
    """Live Teams test 2026-10-29: the report dropped the candidate's words, but the stored live lines stayed."""
    m, fake = manager(settings, store)
    sid = await m.launch(ready_interview.id)
    runner = m.live[sid].runner
    await settle(lambda: runner.bot.bot_id is not None)
    await runner.bot.handle_event(event("bot.inmeeting"))
    await settle(lambda: runner.state == "awaiting_consent")
    await runner.bot.handle_transcript(sentence("No thanks, I would rather speak to a person."))
    assert any(u.role == "candidate" for u in store.transcript(sid))
    await m.end(sid)
    await settle(lambda: store.get_session(sid)["state"] == "completed")
    assert all(u.role != "candidate" for u in store.transcript(sid))
    assert fake.bots.erased == ["bot-123"]
    assert "candidate_lines_deleted" in [a["action"] for a in store.audit_log(20, ready_interview.id)]


async def test_two_starts_at_once_launch_one_bot(settings, store, ready_interview, monkeypatch):
    """Security audit: launch() awaits (the public-address check) before the live session exists; two quick starts
    of one interview, or two in the same room, must not both get through."""
    import asyncio
    from dataclasses import replace
    import pytest
    from app.interview import manager as manager_module
    from app.interview.manager import LaunchError

    async def slow_resolve(url):
        await asyncio.sleep(0.05)
        return True
    monkeypatch.setattr(manager_module, "public_host_resolves", slow_resolve)
    m, fake = manager(replace(settings, check_public_url=True), store)
    other = store.save_interview(store.create_interview(ready_interview.setup).model_copy(update={
        "plan": ready_interview.plan, "plan_approved_at": ready_interview.plan_approved_at, "status": "ready",
        "meeting_link": ready_interview.meeting_link, "consent_attested_at": ready_interview.consent_attested_at}))
    results = await asyncio.gather(m.launch(ready_interview.id), m.launch(ready_interview.id), m.launch(other.id),
                                   return_exceptions=True)
    assert isinstance(results[0], str)
    assert all(isinstance(r, LaunchError) for r in results[1:])  # the same interview again, and the same room
    assert len(m.live) == 1
    with pytest.raises(LaunchError):
        await m.launch(ready_interview.id)  # still refused once it's live
