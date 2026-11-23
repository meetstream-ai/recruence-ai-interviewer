"""One AI provider for the whole interview (app/providers.py): what each provider covers, how the voice agent's parts
are worked out from it, overrides and fallbacks, and the Settings page and launch agreeing on it."""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.interview.agent import VoiceNotConfigured, agent_config
from app.llm import OfflineAI, build_ai
from app.planning import context_for
from app.providers import capabilities, voice_plan
from app.server import create_app
from app.store import Store

NO_VOICE = dict(mia_stt_provider="", mia_stt_model="", mia_llm_provider="", mia_llm_model="", mia_tts_provider="", mia_voice="")


def plain(settings, **kw):
    """Settings with no separate voice providers: everything comes from the AI provider."""
    return replace(settings, **{**NO_VOICE, "anthropic_api_key": "", "gemini_api_key": "", "openai_api_key": "", "xai_api_key": "", **kw})


@pytest.mark.parametrize("provider,key,full", [("openai", "openai_api_key", True), ("xai", "xai_api_key", True),
                                               ("claude", "anthropic_api_key", False), ("gemini", "gemini_api_key", False)])
def test_one_provider_covers_what_it_supports_and_nothing_more(settings, provider, key, full):
    s = plain(settings, llm_provider=provider, **{key: "k-test"})
    plan = voice_plan(s)
    assert plan.primary == provider and plan.llm.source == "provider"
    caps = capabilities(provider)
    assert caps["plans"] and caps["conversation"] and caps["stt"] is full and caps["tts"] is full
    if full:
        assert plan.ready and plan.stt.provider == plan.tts.provider == ("openai" if provider == "openai" else "xai")
        assert plan.meetstream_keys == [caps["meetstream_key"]]  # one key, added once in MeetStream
    else:
        # Claude and Gemini talk but don't transcribe or speak in MIA: never assumed, the gap is named.
        assert plan.missing == ("stt", "tts") and not plan.ready
        assert "speech-to-text and voice" in plan.problem() and "not ready" in s.mia_label


def test_no_provider_means_nothing_is_assumed(settings):
    s = plain(settings, llm_provider="auto")
    plan = voice_plan(s)
    assert plan.primary == "offline" and plan.missing == ("stt", "llm", "tts")
    assert capabilities("ollama") == {**capabilities("ollama"), "plans": True, "conversation": False, "stt": False, "tts": False}


def test_a_gap_is_filled_by_another_provider_only_for_that_part(settings):
    s = plain(settings, llm_provider="gemini", gemini_api_key="k", gemini_model="gemini-3.8-flash",
              mia_stt_provider="deepgram", mia_tts_provider="elevenlabs")
    assert voice_plan(s).missing == ("tts",)  # ElevenLabs needs a voice from your account
    s = replace(s, mia_voice="voice-123")
    plan = voice_plan(s)
    assert plan.ready and plan.llm.provider == "google" and plan.llm.model == "gemini-3.8-flash" and plan.llm.source == "provider"
    assert (plan.stt.provider, plan.stt.model, plan.stt.source) == ("deepgram", "nova-2", "override")
    assert (plan.tts.provider, plan.tts.model) == ("elevenlabs", "voice-123")
    assert plan.meetstream_keys == ["Deepgram API key", "Google (Gemini) API key", "ElevenLabs API key"]


def test_overrides_and_saved_setups_keep_working(settings):
    """An installation that set every voice part (in .env or saved, like this repository's own) uses exactly that."""
    s = plain(settings, llm_provider="gemini", gemini_api_key="k", mia_stt_provider="deepgram", mia_stt_model="nova-3",
              mia_llm_provider="google", mia_llm_model="gemini-3.8-flash", mia_tts_provider="elevenlabs", mia_voice="XrExE-test")
    plan = voice_plan(s)
    assert plan.ready and {p.source for p in (plan.stt, plan.llm, plan.tts)} == {"override"}
    assert s.mia_label == "deepgram STT → google:gemini-3.8-flash → elevenlabs TTS (XrExE-test)"


def test_voice_choice_stays_with_its_provider(settings):
    s = plain(settings, llm_provider="openai", openai_api_key="k", mia_voice="coral")
    assert voice_plan(s).tts.model == "coral"
    s = replace(s, mia_voice="ara")  # an xAI voice left over from another provider: the provider's default instead
    assert voice_plan(s).tts.model == "alloy"


def test_the_launch_uses_the_plan_or_stops_with_the_reason(settings, ready_interview):
    ctx = context_for(ready_interview)
    s = plain(settings, llm_provider="openai", openai_api_key="k", openai_model="gpt-4.1")
    cfg = agent_config(s, ctx, "s1", "https://t/x", "tok")
    assert cfg["transcriber"]["provider"] == "openai" and cfg["transcriber"]["model"] == "whisper-1"
    assert cfg["model"]["provider"] == "openai" and cfg["model"]["model"] == "gpt-4.1"
    assert cfg["voice"] == {"provider": "openai", "voice_id": "alloy"}
    assert "api_key" not in json.dumps(cfg) and "k" != cfg["model"].get("api_key")  # keys stay in MeetStream
    with pytest.raises(VoiceNotConfigured, match="speech-to-text and voice"):
        agent_config(plain(settings, llm_provider="claude", anthropic_api_key="k"), ctx, "s1", "https://t/x", "tok")


def test_xai_also_writes_plans(settings):
    ai = build_ai(plain(settings, llm_provider="xai", xai_api_key="k", xai_model="grok-test"))
    assert ai.name.startswith("OpenAI-compatible") or "grok-test" in ai.name


def test_the_settings_page_shows_the_plan_the_launch_uses(settings):
    s = plain(settings, llm_provider="gemini", gemini_api_key="k")
    app = create_app(settings=s, store=Store(s.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        v = c.get("/api/config").json()
        assert v["voice"]["primary"] == "gemini" and v["voice"]["ready"] is False and v["voice"]["missing"] == ["speech-to-text", "voice"]
        assert {p["id"]: (p["stt"], p["tts"]) for p in v["providers"]} == {
            "openai": (True, True), "xai": (True, True), "claude": (False, False), "gemini": (False, False), "ollama": (False, False)}
        item = next(i for i in c.get("/api/settings/readiness").json()["items"] if i["key"] == "voice")
        assert item["status"] == "missing" and "doesn't cover" in item["detail"]
        assert app.state.store.saved_settings() == []  # nothing saved by looking
        r = c.put("/api/config", json={"changes": {"mia_stt_provider": "deepgram", "mia_tts_provider": "openai"}})
        assert r.status_code == 200
        v = r.json()["voice"]
        assert v["ready"] and v["parts"]["stt"]["source"] == "override" and v["parts"]["llm"]["source"] == "provider"
        item = next(i for i in c.get("/api/settings/readiness").json()["items"] if i["key"] == "voice")
        assert item["status"] == "manual" and "Deepgram API key" in item["detail"]
        # Back to "from the AI provider": an empty value is a valid choice.
        assert c.put("/api/config", json={"changes": {"mia_stt_provider": ""}}).json()["voice"]["parts"]["stt"]["source"] == "missing"


def test_what_each_provider_card_claims_matches_what_was_seen():
    """Cards show what was seen working, not only what's documented: every part has evidence, a supported part is never
    marked unavailable (and the reverse), and Claude's conversation is flagged as reported not working."""
    from app.providers import EVIDENCE, PROVIDERS
    for pid in PROVIDERS:
        caps = capabilities(pid)
        assert set(caps["evidence"]) == {"plans", "conversation", "stt", "tts"}, pid
        for part, ev in caps["evidence"].items():
            assert ev["level"] in {"verified", "documented", "reported_broken", "realtime", "unavailable"} and ev["note"]
            assert (ev["level"] in {"unavailable", "realtime"}) == (not caps[part]), (pid, part)
            assert ev["level"] != "realtime" or caps["realtime"], (pid, part)  # only a provider with a realtime model
    assert EVIDENCE["claude"]["conversation"][0] == "reported_broken"
    # MeetStream's pipeline has no Google speech-to-text or voice; Gemini hears and speaks only as Gemini Live.
    assert EVIDENCE["gemini"]["stt"][0] == EVIDENCE["gemini"]["tts"][0] == "realtime"
    # One test key's quota and capacity problems aren't a property of Gemini: the card doesn't cite them (12 November).
    assert not any(w in EVIDENCE["gemini"][p][1] for p in ("conversation", "stt", "tts") for w in ("silent", "unreliabl", "call 19", "calls 7"))
    assert all(level == "documented" for level, _ in EVIDENCE["openai"].values())  # nothing OpenAI was tried with a real key


# ---------- Gemini for everything: MeetStream's realtime mode (Gemini Live) ----------

def test_gemini_live_covers_every_part_of_the_interview(settings):
    """In the pipeline Gemini only converses (MeetStream has no Google speech-to-text or voice); Gemini Live hears,
    thinks and speaks, so with it the plan is ready with Gemini alone."""
    s = plain(settings, llm_provider="gemini", gemini_api_key="k")
    assert voice_plan(s).missing == ("stt", "tts")
    caps = capabilities("gemini")
    assert caps["realtime"]["provider"] == "google" and caps["realtime"]["default_voice"] == "Puck"
    assert caps["realtime"]["model"] == "gemini-2.5-flash-native-audio-preview-12-2025"
    assert capabilities("claude")["realtime"] is None and capabilities("ollama")["realtime"] is None

    live = voice_plan(replace(s, mia_mode="realtime", mia_realtime_provider="google"))
    assert live.ready and live.realtime and live.problem() == ""
    assert {p.provider for p in (live.stt, live.llm, live.tts)} == {"google"}
    assert {p.source for p in (live.stt, live.llm, live.tts)} == {"realtime"}
    assert live.llm.model == "gemini-2.5-flash-native-audio-preview-12-2025" and live.tts.model == "Puck"
    assert live.meetstream_keys == ["Google (Gemini) API key"]
    chosen = voice_plan(replace(s, mia_mode="realtime", mia_realtime_voice="Kore", mia_realtime_model="gemini-live-x"))
    assert chosen.tts.model == "Kore" and chosen.llm.model == "gemini-live-x"
    # A voice Gemini Live doesn't have falls back to its default; an unknown realtime provider isn't ready.
    assert voice_plan(replace(s, mia_mode="realtime", mia_realtime_voice="alloy")).tts.model == "Puck"
    odd = voice_plan(replace(s, mia_mode="realtime", mia_realtime_provider="nobody"))
    assert not odd.ready and "realtime provider" in odd.problem()


def test_gemini_live_launches_as_one_realtime_model(settings, ready_interview):
    ctx = context_for(ready_interview)
    s = plain(settings, llm_provider="gemini", gemini_api_key="k")
    with pytest.raises(VoiceNotConfigured):
        agent_config(s, ctx, "s1", "https://t/x", "tok")
    cfg = agent_config(replace(s, mia_mode="realtime", mia_realtime_voice="Kore"), ctx, "s1", "https://t/x", "tok")
    assert cfg["mode"] == "realtime" and cfg["model"]["provider"] == "google" and cfg["model"]["voice"] == "Kore"
    assert "transcriber" not in cfg and "voice" not in cfg  # nothing from another provider: all-or-nothing
    assert "model" not in cfg["model"]  # MeetStream's default unless one is chosen
    wrong = agent_config(replace(s, mia_mode="realtime", mia_realtime_voice="alloy"), ctx, "s1", "https://t/x", "tok")
    assert wrong["model"]["voice"] == "Puck"
    with pytest.raises(VoiceNotConfigured):
        agent_config(replace(s, mia_mode="realtime", mia_realtime_provider="nobody"), ctx, "s1", "https://t/x", "tok")
    # Realtime setups from before keep working: OpenAI and xAI realtime, with MeetStream's default model.
    xai = agent_config(replace(s, mia_mode="realtime", mia_realtime_provider="xai", mia_realtime_voice="eve"), ctx, "s1", "https://t/x", "tok")
    assert xai["model"]["provider"] == "xai" and xai["model"]["voice"] == "eve" and "model" not in xai["model"]


def test_gemini_live_is_chosen_from_the_settings_page(settings):
    s = plain(settings, llm_provider="gemini", gemini_api_key="k")
    app = create_app(settings=s, store=Store(s.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        fields = {f["key"]: f for sec in c.get("/api/config").json()["sections"] for f in sec["fields"]}
        assert fields["mia_mode"]["options"] == ["pipeline", "realtime"] and "google" in fields["mia_realtime_provider"]["options"]
        v = c.put("/api/config", json={"changes": {"mia_mode": "realtime", "mia_realtime_provider": "google"}}).json()["voice"]
        assert v["mode"] == "realtime" and v["ready"] and v["missing"] == [] and v["realtime"]["voice"] == "Puck"
        assert v["parts"]["stt"] == {"provider": "google", "model": "gemini-2.5-flash-native-audio-preview-12-2025", "source": "realtime"}
        item = next(i for i in c.get("/api/settings/readiness").json()["items"] if i["key"] == "voice")
        assert item["status"] != "missing"
        assert c.put("/api/config", json={"changes": {"mia_realtime_voice": "Leda"}}).json()["voice"]["realtime"]["voice"] == "Leda"
        # A voice Gemini Live doesn't have isn't sent: its default is used.
        assert c.put("/api/config", json={"changes": {"mia_realtime_voice": "alloy"}}).json()["voice"]["realtime"]["voice"] == "Puck"
        assert c.put("/api/config", json={"changes": {"mia_realtime_provider": "nobody"}}).status_code in (400, 422)
        back = c.put("/api/config", json={"changes": {"mia_mode": "pipeline"}}).json()["voice"]
        assert back["mode"] == "pipeline" and back["realtime"] is None and back["missing"] == ["speech-to-text", "voice"]


def test_gemini_alone_is_told_which_voice_setup_works(settings):
    plan = voice_plan(plain(settings, llm_provider="gemini", gemini_api_key="k"))
    assert not plan.ready and set(plan.missing) == {"stt", "tts"} and not plan.realtime  # separate parts stay the default
    assert "Deepgram" in plan.problem() and "ElevenLabs" in plan.problem() and "Integrations" in plan.problem()
