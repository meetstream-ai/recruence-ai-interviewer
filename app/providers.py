"""One AI provider for the whole interview, wherever it can: what each provider covers, and how the voice agent's
parts are worked out from the chosen one.

The app's AI provider (Settings → AI provider: LLM_PROVIDER and that provider's key and model) writes plans, screens
applications and evaluates interviews. The same provider also runs the interview's voice parts in MeetStream's MIA
when MIA supports it for that part (docs.meetstream.ai/guides/mia/mia-api-guide):

| Provider | Plans, screening, evaluation | Conversation (voice LLM) | Speech-to-text | Voice (TTS) |
|----------|-----------------------------|--------------------------|----------------|-------------|
| OpenAI   | yes                         | openai                   | openai whisper-1 | openai     |
| xAI      | yes (OpenAI-compatible API) | xai                      | xai stt-1        | xai        |
| Claude   | yes                         | anthropic                | no               | no         |
| Gemini   | yes                         | google                   | Gemini Live*     | Gemini Live* |
| Ollama   | yes                         | no                       | no               | no         |

"yes" means the code and MeetStream's documentation support it; EVIDENCE below says what has been seen working.
* MeetStream's pipeline has no google speech-to-text or voice, so Gemini can't hear or speak there. Its realtime mode can:
Gemini Live (MIA_MODE=realtime, or Settings → AI provider → "Gemini for everything") is one model that hears, thinks and
speaks, so Gemini alone runs the whole interview. Realtime is all-or-nothing: no part can come from another provider.

A part the provider doesn't cover is never assumed: it needs another provider (MIA_STT_PROVIDER, MIA_TTS_PROVIDER, or
MIA_LLM_PROVIDER for the conversation), and an interview can't start until it has one. Those same settings, when set,
override the provider for that part (the advanced setup; older installations that set them keep working as they were).

MeetStream keeps the voice providers' keys itself (dashboard → Integrations): agent configs name providers, never
keys, so the key entered in Recruence powers plans and evaluations, and the same provider's key is added in MeetStream once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.config import Settings


@dataclass(frozen=True)
class Voice:
    provider: str  # MeetStream's name for it
    default: str
    choices: tuple[str, ...] = ()  # documented voice ids; empty: an id from the person's own account


@dataclass(frozen=True)
class Provider:
    id: str  # LLM_PROVIDER value
    label: str
    key_field: str = ""  # its API key setting, if it needs one
    model_field: str = ""
    voice_llm: str = ""  # MeetStream's provider name for the conversation; "" if MIA doesn't support it
    stt: Optional[tuple[str, str]] = None  # (MeetStream provider, default model)
    tts: Optional[Voice] = None
    meetstream_key: str = ""  # what to add in MeetStream → Integrations for the voice parts
    realtime: Optional["Realtime"] = None  # MeetStream's realtime model for it: one model hears, thinks and speaks


@dataclass(frozen=True)
class Realtime:
    provider: str  # MeetStream's realtime provider name
    model: str  # MeetStream's documented default model ("" : MeetStream's own default)
    voice: Voice


OPENAI_VOICES = ("alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse")
XAI_VOICES = ("ara", "eve", "leo", "rex")
# Gemini Live in MeetStream's realtime mode (MIA API guide, read 2026-11-10): its documented model and voices.
GEMINI_LIVE = Realtime("google", "gemini-2.5-flash-native-audio-preview-12-2025",
                       Voice("google", "Puck", ("Puck", "Charon", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr")))

PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider("openai", "OpenAI", "openai_api_key", "openai_model", voice_llm="openai", stt=("openai", "whisper-1"),
             tts=Voice("openai", "alloy", OPENAI_VOICES), meetstream_key="OpenAI API key",
             realtime=Realtime("openai", "", Voice("openai", "alloy", OPENAI_VOICES))),
    Provider("xai", "xAI (Grok)", "xai_api_key", "xai_model", voice_llm="xai", stt=("xai", "stt-1"),
             tts=Voice("xai", "ara", XAI_VOICES), meetstream_key="xAI API key",
             realtime=Realtime("xai", "", Voice("xai", "ara", XAI_VOICES))),
    Provider("claude", "Anthropic Claude", "anthropic_api_key", "claude_model", voice_llm="anthropic", meetstream_key="Anthropic API key"),
    Provider("gemini", "Google Gemini", "gemini_api_key", "gemini_model", voice_llm="google", meetstream_key="Google (Gemini) API key",
             realtime=GEMINI_LIVE),
    Provider("ollama", "Ollama (local)", "", "ollama_model"),
)}

# What has actually been seen working, per provider and part, as opposed to what MeetStream documents
# (docs.meetstream.ai MIA API guide, read 2026-11-05) or the code supports.
#   verified: seen working (live interviews for the voice parts; real screening or evaluations for plans)
#   documented: built and documented by MeetStream or the provider, not yet tried here
#   reported_broken: listed, but reported not to work in practice; not verified here
#   realtime: only through the provider's realtime model (one model for the whole interview)
#   unavailable: MeetStream's pipeline has no such provider for this part
EVIDENCE: dict[str, dict[str, tuple[str, str]]] = {
    "gemini": {
        "plans": ("verified", "Plans, screening and evaluations from real interviews (Gemini API)."),
        "conversation": ("verified", "Live interviews on Meet and Teams (gemini-3.6-flash). Use a key whose quota covers a whole interview (about 20 requests)."),
        "stt": ("realtime", "Through Gemini Live (Gemini for everything): MeetStream's pipeline has no Google speech-to-text."),
        "tts": ("realtime", "Through Gemini Live (Gemini for everything): MeetStream's pipeline has no Google voice."),
    },
    "xai": {
        "plans": ("documented", "Through xAI's OpenAI-compatible API; tested against a mock, not a real xAI key."),
        "conversation": ("verified", "Calls 1-5 (Meet and Teams)."),
        "stt": ("verified", "stt-1 in calls 1-5; later refused with 403 when the xAI account had a problem (calls 6, 9, 10)."),
        "tts": ("verified", "Calls 1-5."),
    },
    "openai": {
        "plans": ("documented", "OpenAI's chat completions API; tested against a mock, not a real OpenAI key."),
        "conversation": ("documented", "MeetStream lists OpenAI models for the conversation; not tried in a live interview."),
        "stt": ("documented", "MeetStream lists whisper-1; not tried in a live interview."),
        "tts": ("documented", "MeetStream lists OpenAI voices (tts-1); not tried in a live interview."),
    },
    "claude": {
        "plans": ("documented", "Anthropic's Messages API; tested against a mock, not a real Anthropic key."),
        "conversation": ("reported_broken", "MeetStream lists anthropic for the conversation, but it was reported not to work in practice; never verified here."),
        "stt": ("unavailable", "Claude has no speech-to-text API; another provider hears for it."),
        "tts": ("unavailable", "Claude has no voice API; another provider speaks for it."),
    },
    "ollama": {
        "plans": ("verified", "Plans, screening and evaluations with a local model (QA run with Ollama in Docker)."),
        "conversation": ("unavailable", "MeetStream runs the interview in its cloud and can't reach a model on this computer."),
        "stt": ("unavailable", "Not a MeetStream provider. Speech depends on the model and isn't wired into MeetStream."),
        "tts": ("unavailable", "Not a MeetStream provider."),
    },
}
# Providers MIA can use for one voice part only, to fill a gap or by choice.
STT_PROVIDERS: dict[str, str] = {"openai": "whisper-1", "xai": "stt-1", "deepgram": "nova-2", "assemblyai": "u3-rt-pro"}
TTS_PROVIDERS: dict[str, Voice] = {"openai": Voice("openai", "alloy", OPENAI_VOICES), "xai": Voice("xai", "ara", XAI_VOICES),
                                   "elevenlabs": Voice("elevenlabs", "")}
VOICE_LLM_PROVIDERS = ("openai", "xai", "anthropic", "google")
# MeetStream's realtime providers, by its name for them.
REALTIME_PROVIDERS: dict[str, Realtime] = {p.realtime.provider: p.realtime for p in PROVIDERS.values() if p.realtime}
# MeetStream's name for a voice provider -> what to add in its Integrations.
MEETSTREAM_KEYS = {"openai": "OpenAI API key", "xai": "xAI API key", "anthropic": "Anthropic API key", "google": "Google (Gemini) API key",
                   "deepgram": "Deepgram API key", "assemblyai": "AssemblyAI API key", "elevenlabs": "ElevenLabs API key"}
PART_NAMES = {"llm": "conversation", "stt": "speech-to-text", "tts": "voice"}


@dataclass(frozen=True)
class Part:
    provider: str = ""  # MeetStream provider name; "" when nothing covers this part
    model: str = ""  # model, or voice id for the voice part
    source: str = "missing"  # "provider" (from the AI provider), "override" (set separately), "realtime", "missing"


@dataclass(frozen=True)
class VoicePlan:
    primary: str  # the AI provider in use ("offline" for none)
    llm: Part
    stt: Part
    tts: Part
    missing: tuple[str, ...] = field(default_factory=tuple)  # parts no provider covers: "llm", "stt", "tts"
    realtime: bool = False  # one realtime model hears, thinks and speaks (llm, stt and tts are the same model)

    @property
    def ready(self) -> bool:
        return not self.missing

    @property
    def meetstream_keys(self) -> list[str]:
        """The keys MeetStream needs (in its Integrations) for this interview's voice parts."""
        used = [p.provider for p in (self.stt, self.llm, self.tts) if p.provider]
        return list(dict.fromkeys(MEETSTREAM_KEYS.get(p, f"{p} API key") for p in used))

    @property
    def label(self) -> str:
        if self.missing:
            return "not ready: no provider for " + ", ".join(PART_NAMES[m] for m in self.missing)
        if self.realtime:
            return f"{self.llm.provider} realtime: {self.llm.model or 'MeetStream default model'} hears, thinks and speaks ({self.tts.model or 'default voice'})"
        return (f"{self.stt.provider} STT → {self.llm.provider}:{self.llm.model or 'default'} → "
                f"{self.tts.provider} TTS ({self.tts.model or 'default voice'})")

    def problem(self) -> str:
        """Why an interview can't start with this plan, or ""."""
        if not self.missing:
            return ""
        names = [PART_NAMES[m] for m in self.missing]
        parts = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
        if self.realtime:
            return (f"Realtime mode needs a realtime provider MeetStream supports ({', '.join(REALTIME_PROVIDERS)}). "
                    "Choose it in Settings → AI provider, or switch back to separate parts.")
        if self.primary not in PROVIDERS:
            return "No AI provider is set up yet. Choose one in Settings → AI provider."
        if self.primary == "gemini" and set(self.missing) == {"stt", "tts"}:
            # MeetStream's pipeline has no Google speech-to-text or voice. Gemini Live (realtime) can, but went silent in
            # live tests (calls 7, 8, 11 and 2026-11-11); Deepgram -> Gemini -> ElevenLabs worked (calls 14, 15, 17, 20).
            return ("Google Gemini doesn't cover the interview's speech-to-text and voice: MeetStream can't hear or speak with "
                    "Google. Set Deepgram for speech-to-text and ElevenLabs for the voice in Settings → AI interviewer → "
                    "Advanced (the setup tested with Gemini), and add their keys in MeetStream → Integrations.")
        return (f"{PROVIDERS[self.primary].label} doesn't cover the interview's {parts}. Choose another provider for "
                f"{'it' if len(names) == 1 else 'them'} in Settings → AI provider (or AI interviewer → Advanced).")


def capabilities(provider_id: str) -> dict:
    """What a provider covers, for the Settings page. Voice parts are never assumed from LLM support."""
    p = PROVIDERS.get(provider_id)
    if p is None:
        return {"plans": False, "conversation": False, "stt": False, "tts": False}
    return {"plans": True, "conversation": bool(p.voice_llm), "stt": p.stt is not None, "tts": p.tts is not None,
            "voices": list(p.tts.choices) if p.tts else [], "default_voice": p.tts.default if p.tts else "",
            "stt_model": p.stt[1] if p.stt else "", "meetstream_key": p.meetstream_key,
            # One model for the whole interview (MeetStream's realtime mode): it hears and speaks even where the pipeline can't.
            "realtime": {"provider": p.realtime.provider, "model": p.realtime.model, "voices": list(p.realtime.voice.choices),
                         "default_voice": p.realtime.voice.default} if p.realtime else None,
            "evidence": {part: {"level": level, "note": note} for part, (level, note) in EVIDENCE.get(provider_id, {}).items()}}


def voice_plan(settings: Settings) -> VoicePlan:
    """The provider and model for each of the voice agent's parts, as the next interview will use them."""
    primary = settings.resolved_llm
    if settings.mia_mode == "realtime":
        # One model hears, thinks and speaks: every part is it. The separate part settings aren't used.
        rt = REALTIME_PROVIDERS.get(settings.mia_realtime_provider)
        if rt is None:
            return VoicePlan(primary, Part(), Part(), Part(), ("stt", "llm", "tts"), realtime=True)
        model = settings.mia_realtime_model or rt.model
        voice = settings.mia_realtime_voice if settings.mia_realtime_voice in rt.voice.choices else rt.voice.default
        return VoicePlan(primary, Part(rt.provider, model, "realtime"), Part(rt.provider, model, "realtime"),
                         Part(rt.provider, voice, "realtime"), (), realtime=True)
    prov = PROVIDERS.get(primary)
    primary_model = getattr(settings, prov.model_field, "") if prov and prov.model_field else ""

    if settings.mia_llm_provider:
        llm = Part(settings.mia_llm_provider, settings.mia_llm_model, "override")
    elif prov and prov.voice_llm:
        llm = Part(prov.voice_llm, settings.mia_llm_model or primary_model, "provider")
    else:
        llm = Part()

    if settings.mia_stt_provider:
        stt = Part(settings.mia_stt_provider, settings.mia_stt_model or STT_PROVIDERS.get(settings.mia_stt_provider, ""), "override")
    elif prov and prov.stt:
        stt = Part(prov.stt[0], settings.mia_stt_model or prov.stt[1], "provider")
    else:
        stt = Part()

    if settings.mia_tts_provider:
        tv = TTS_PROVIDERS.get(settings.mia_tts_provider)
        tts = Part(settings.mia_tts_provider, settings.mia_voice or (tv.default if tv else ""), "override")
    elif prov and prov.tts:
        voice = settings.mia_voice if settings.mia_voice and (not prov.tts.choices or settings.mia_voice in prov.tts.choices) else prov.tts.default
        tts = Part(prov.tts.provider, voice, "provider")
    else:
        tts = Part()

    # A voice part needs its provider; the voice also needs a voice id (ElevenLabs has no shared default).
    missing = tuple(name for name, part in (("stt", stt), ("llm", llm), ("tts", tts)) if not part.provider or (name == "tts" and not part.model))
    return VoicePlan(primary, llm, stt, tts, missing)
