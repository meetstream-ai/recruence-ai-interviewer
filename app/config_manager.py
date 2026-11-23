"""User-editable settings: the Settings page's fields, layered over .env and saved in the database.

`.env` stays the bootstrap (database location, encryption key). Everything here can be changed from the
dashboard by an admin. Values are validated, saved in `app_settings` (secrets encrypted with
app/secret_box.py) and applied:

- **live** fields at once, on the shared Settings object, since the app reads them when it uses them
  (the AI client is rebuilt; the MeetStream client is reset);
- the rest on the next start, and the Settings page says so.

Secrets never leave the server: the page only learns whether each one is set.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import LLM_PROVIDERS, Settings, current_app_name, public_url_problem
from app.providers import GEMINI_LIVE, PART_NAMES, PROVIDERS, REALTIME_PROVIDERS, STT_PROVIDERS, TTS_PROVIDERS, VOICE_LLM_PROVIDERS, capabilities, voice_plan
from app.secret_box import SecretBox, SecretError
from app.store import Store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Field:
    key: str  # the Settings attribute
    section: str
    label: str
    kind: str  # text, url, https, int, float, bool, select, secret, list, zone
    env: str
    live: bool = True
    help: str = ""
    options: tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None


SECTIONS = [
    ("general", "General"),
    ("access", "Access"),
    ("database", "Database and data"),
    ("ai", "AI provider"),
    ("meetstream", "MeetStream"),
    ("email", "Email"),
    ("voice", "AI interviewer"),
    ("system", "System"),
]

# The built-in value of every setting (Settings' own defaults): what applies when nothing was chosen.
DEFAULTS = {f.name: f.default for f in dataclasses.fields(Settings) if f.default is not dataclasses.MISSING}

# Separate voice providers (app/providers.py): "" means the part comes from the AI provider.
STT = ("", *STT_PROVIDERS)
VOICE_LLM = ("", *VOICE_LLM_PROVIDERS)
TTS = ("", *TTS_PROVIDERS)

FIELDS: list[Field] = [
    # General
    Field("app_name", "general", "Application name", "text", "APP_NAME", help="Shown in the dashboard and in emails."),
    Field("bot_name", "general", "AI interviewer's name", "text", "BOT_NAME", help="The name the bot joins meetings with."),
    Field("public_base_url", "meetstream", "Public application URL", "https", "PUBLIC_BASE_URL",
          help="This app's address on the internet, e.g. https://interviews.example.com or a tunnel's https address. "
               "MeetStream sends its callbacks there (the AI interviewer joining, transcript, end of call), and candidates' "
               "application, interview and booking links use it. Not localhost or a private address: those can't be "
               "reached from outside, and installing the desktop app doesn't make it reachable. See docs/INTERVIEW_SETUP.md."),
    Field("mia_language", "general", "Interview language", "select", "MIA_LANGUAGE", options=("en",), help="English only for now."),
    Field("time_zone", "general", "Time zone for emails", "zone", "TIME_ZONE",
          help="An IANA name such as Europe/London or Asia/Kolkata. The dashboard always shows each viewer's own time zone."),
    Field("default_duration_minutes", "general", "Default interview length (minutes)", "int", "DEFAULT_DURATION_MINUTES", minimum=5, maximum=90),
    # App AI: plans and evaluation
    Field("llm_provider", "ai", "AI provider", "select", "LLM_PROVIDER", options=tuple(sorted(LLM_PROVIDERS)),
          help="Writes plans, screens applications, evaluates interviews, and runs the interview's conversation, speech-to-text "
               "and voice wherever it supports them. \"auto\" uses the first provider with a key: Claude, Gemini, OpenAI, then xAI. "
               "\"offline\" uses template plans and heuristic scores."),
    Field("gemini_api_key", "ai", "Gemini API key", "secret", "GEMINI_API_KEY"),
    Field("gemini_model", "ai", "Gemini model", "text", "GEMINI_MODEL"),
    Field("anthropic_api_key", "ai", "Anthropic API key", "secret", "ANTHROPIC_API_KEY"),
    Field("claude_model", "ai", "Claude model", "text", "CLAUDE_MODEL"),
    Field("openai_api_key", "ai", "OpenAI API key", "secret", "OPENAI_API_KEY", help="Also for any OpenAI-compatible server."),
    Field("openai_model", "ai", "OpenAI model", "text", "OPENAI_MODEL"),
    Field("openai_base_url", "ai", "OpenAI-compatible base URL", "url", "OPENAI_BASE_URL"),
    Field("xai_api_key", "ai", "xAI API key", "secret", "XAI_API_KEY", help="console.x.ai. The same key, added in MeetStream, runs the interview's voice."),
    Field("xai_model", "ai", "xAI model", "text", "XAI_MODEL"),
    Field("ollama_base_url", "ai", "Ollama URL", "url", "OLLAMA_BASE_URL", help="Where Ollama runs, e.g. http://localhost:11434."),
    Field("ollama_model", "ai", "Ollama model", "text", "OLLAMA_MODEL", help="One of the models installed in Ollama (load the list below)."),
    # Voice agent (MIA). Read at each launch.
    Field("mia_mode", "voice", "How the interview runs", "select", "MIA_MODE", options=("pipeline", "realtime"),
          help="pipeline: separate speech-to-text, conversation and voice. realtime: one model hears, thinks and speaks "
               "(Gemini Live), so Gemini alone runs the whole interview."),
    Field("mia_realtime_provider", "voice", "Realtime model provider", "select", "MIA_REALTIME_PROVIDER", options=tuple(REALTIME_PROVIDERS),
          help="google: Gemini Live, so Gemini runs the whole interview. Add that provider's key in MeetStream → Integrations too."),
    Field("mia_realtime_model", "voice", "Realtime model", "text", "MIA_REALTIME_MODEL",
          help=f"Empty: MeetStream's default ({GEMINI_LIVE.model})."),
    Field("mia_realtime_voice", "voice", "Realtime voice", "text", "MIA_REALTIME_VOICE",
          help=f"Empty: the realtime model's default. Gemini Live: {', '.join(GEMINI_LIVE.voice.choices)} ({GEMINI_LIVE.voice.default} by default)."),
    Field("mia_realtime_inline_plan", "voice", "Give the realtime model the whole plan up front (experimental)", "bool", "MIA_REALTIME_INLINE_PLAN",
          help="Instead of asking for each question in turn. Try it if Gemini Live goes quiet after its first question."),
    Field("mia_stt_provider", "voice", "Speech-to-text provider", "select", "MIA_STT_PROVIDER", options=STT,
          help="Empty: your AI provider, if it does speech-to-text."),
    Field("mia_stt_model", "voice", "Speech-to-text model", "text", "MIA_STT_MODEL", help="Empty: the provider's default."),
    Field("mia_llm_provider", "voice", "Conversation provider", "select", "MIA_LLM_PROVIDER", options=VOICE_LLM,
          help="Empty: your AI provider."),
    Field("mia_llm_model", "voice", "Conversation model", "text", "MIA_LLM_MODEL", help="Empty: your AI provider's model."),
    Field("mia_tts_provider", "voice", "Voice provider", "select", "MIA_TTS_PROVIDER", options=TTS,
          help="Empty: your AI provider, if it has voices."),
    Field("mia_voice", "voice", "Voice", "text", "MIA_VOICE",
          help="Empty: the voice provider's default. xai: ara, eve, leo, rex. openai: alloy, coral, nova… elevenlabs: a voice ID from your account."),
    Field("mia_min_endpointing_delay", "voice", "Wait before replying, minimum (s)", "float", "MIA_MIN_ENDPOINTING_DELAY", minimum=0, maximum=10,
          help="How long a pause must be before the AI answers. Longer lets candidates think."),
    Field("mia_max_endpointing_delay", "voice", "Wait before replying, maximum (s)", "float", "MIA_MAX_ENDPOINTING_DELAY", minimum=0, maximum=20),
    Field("mia_interrupt_min_seconds", "voice", "Interruption: minimum speech (s)", "float", "MIA_INTERRUPT_MIN_SECONDS", minimum=0, maximum=5, help="0 turns it off."),
    Field("mia_interrupt_min_words", "voice", "Interruption: minimum words", "int", "MIA_INTERRUPT_MIN_WORDS", minimum=0, maximum=20, help="0 turns it off."),
    Field("mia_vad_min_silence_ms", "voice", "Silence that ends speech (ms)", "int", "MIA_VAD_MIN_SILENCE_MS", minimum=0, maximum=5000, help="0 uses MeetStream's default."),
    Field("max_follow_ups_per_question", "voice", "Follow-ups per question", "int", "MAX_FOLLOW_UPS_PER_QUESTION", minimum=0, maximum=5),
    Field("mia_answer_handoff", "voice", "Candidates say when they've finished an answer", "bool", "MIA_ANSWER_HANDOFF",
          help="The AI treats pauses as thinking time and moves on when the candidate says something like \"that's my answer\". "
               "MeetStream doesn't report microphone on/off, so this is how candidates control when an answer ends."),
    Field("chat_disclosure", "voice", "Post the AI and recording notice in meeting chat", "bool", "CHAT_DISCLOSURE", help="Not on Zoom."),
    Field("postcall_transcript", "voice", "Rebuild the transcript from the recording", "bool", "POSTCALL_TRANSCRIPT",
          help="Recovers the AI's spoken follow-ups. Billed by MeetStream."),
    # Integrations
    Field("meetstream_api_key", "meetstream", "MeetStream API key", "secret", "MEETSTREAM_API_KEY"),
    Field("check_public_url", "meetstream", "Check the public URL before each launch", "bool", "CHECK_PUBLIC_URL"),
    Field("public_cors_origins", "meetstream", "Websites that may submit applications", "list", "PUBLIC_CORS_ORIGINS",
          help="Your own careers site's address (e.g. https://careers.example.com, or http://127.0.0.1:8300 for the demo site), "
               "comma-separated. Its pages can then show a role and send applications to Recruence. Nothing else in Recruence is opened to it."),
    Field("smtp_host", "email", "SMTP server", "text", "SMTP_HOST"),
    Field("smtp_port", "email", "SMTP port", "int", "SMTP_PORT", minimum=1, maximum=65535),
    Field("smtp_username", "email", "SMTP username", "text", "SMTP_USERNAME"),
    Field("smtp_password", "email", "SMTP password", "secret", "SMTP_PASSWORD"),
    Field("smtp_from", "email", "From address", "text", "SMTP_FROM"),
    Field("smtp_starttls", "email", "Use STARTTLS", "bool", "SMTP_STARTTLS"),
    Field("smtp_ssl", "email", "Use SSL/TLS from the start", "bool", "SMTP_SSL"),
    Field("mail_reply_to", "email", "Reply-to address", "text", "MAIL_REPLY_TO"),
    Field("application_receipt", "email", "Email applicants that their application arrived", "bool", "APPLICATION_RECEIPT",
          help="Applications from your careers website. Applications from an ATS get the ATS's own email."),
    Field("recruiter_contact", "email", "How candidates ask for a human interviewer", "text", "RECRUITER_CONTACT",
          help="An email address or phone number written in invitations."),
    Field("notify_emails", "email", "\"Report ready\" notes to", "list", "NOTIFY_EMAILS", live=False, help="Comma-separated addresses."),
    Field("report_webhook_url", "system", "Report export URL (ATS hook)", "url", "REPORT_WEBHOOK_URL", live=False),
    Field("report_webhook_secret", "system", "Report export signing secret", "secret", "REPORT_WEBHOOK_SECRET", live=False),
    Field("report_webhook_transcript", "system", "Include the transcript in exports", "bool", "REPORT_WEBHOOK_TRANSCRIPT", live=False),
    # Access (the access token itself is managed with Regenerate / Revoke)
    Field("proxy_auth_secret", "access", "Identity proxy shared secret", "secret", "PROXY_AUTH_SECRET", live=False,
          help="Set when an SSO proxy (oauth2-proxy, Cloudflare Access…) signs users in."),
    Field("proxy_default_role", "access", "Role for signed-in users in no listed group", "select", "PROXY_DEFAULT_ROLE",
          live=False, options=("none", "viewer", "recruiter", "admin")),
    Field("proxy_admin_groups", "access", "Admin groups", "list", "PROXY_ADMIN_GROUPS", live=False),
    Field("proxy_recruiter_groups", "access", "Recruiter groups", "list", "PROXY_RECRUITER_GROUPS", live=False),
    Field("proxy_viewer_groups", "access", "Viewer groups", "list", "PROXY_VIEWER_GROUPS", live=False),
    # Data
    Field("data_retention_days", "database", "Delete finished interviews after (days)", "int", "DATA_RETENTION_DAYS", minimum=0, maximum=3650,
          help="Resume, transcripts and reports. 0 keeps them until you delete them."),
    Field("meetstream_retention_hours", "database", "Recordings kept at MeetStream (hours)", "int", "MEETSTREAM_RETENTION_HOURS", minimum=1, maximum=8760,
          help="Applies to calls started from now on."),
]
BY_KEY = {f.key: f for f in FIELDS}
SECRET_KEYS = {f.key for f in FIELDS if f.kind == "secret"} | {"app_access_token"}
AI_KEYS = {f.key for f in FIELDS if f.section == "ai"}


class ConfigError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def coerce(field: Field, raw: Any) -> Any:
    """The typed value for a field, or ValueError with a message for the person."""
    if field.kind == "bool":
        if isinstance(raw, bool):
            return raw
        raise ValueError("must be on or off")
    if field.kind in ("int", "float"):
        try:
            value = int(raw) if field.kind == "int" else float(raw)
        except (TypeError, ValueError):
            raise ValueError("must be a number") from None
        if field.minimum is not None and value < field.minimum or field.maximum is not None and value > field.maximum:
            raise ValueError(f"must be between {field.minimum:g} and {field.maximum:g}")
        return value
    if field.kind == "list":
        items = raw if isinstance(raw, list) else str(raw or "").split(",")
        values = tuple(x.strip() for x in items if str(x).strip())
        if field.key == "public_cors_origins":
            from urllib.parse import urlsplit
            clean = []
            for v in values:
                parts = urlsplit(v)
                if parts.scheme not in ("http", "https") or not parts.hostname or parts.path.strip("/") or parts.query:
                    raise ValueError(f"{v} isn't a website address like https://careers.example.com")
                clean.append(f"{parts.scheme}://{parts.netloc}")
            values = tuple(clean)
        return values
    text = str(raw if raw is not None else "").strip()
    if len(text) > 2000:
        raise ValueError("is too long")
    if field.kind == "select" and text not in field.options:
        raise ValueError(f"must be one of: {', '.join(field.options)}")
    if field.kind in ("url", "https") and text:
        if not text.startswith(("https://",) if field.kind == "https" else ("http://", "https://")):
            raise ValueError("must start with https://" if field.kind == "https" else "must start with http:// or https://")
        text = text.rstrip("/")
        if field.key == "public_base_url" and (problem := public_url_problem(text)):
            raise ValueError(problem)
    if field.kind == "zone":
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("isn't a known time zone (use a name like Europe/London)") from None
    if field.kind == "text" and field.key in ("app_name", "bot_name") and not text:
        raise ValueError("can't be empty")
    return text


class ConfigManager:
    def __init__(self, settings: Settings, store: Store, box: SecretBox, on_change: Callable[[set[str]], None] = lambda keys: None):
        self.settings = settings
        self.store = store
        self.box = box
        self.on_change = on_change
        # What .env (or the built-in default) says, to fall back to when a saved value is removed.
        self._base = {f.key: getattr(settings, f.key) for f in FIELDS} | {"app_access_token": settings.app_access_token}
        self.saved: set[str] = set()
        self.pending_restart: set[str] = set()
        self.problems: list[str] = []
        # A cloud backend's public address is the server's own (PUBLIC_BASE_URL, required there): a Public URL saved
        # by a desktop install that used this database (its tunnel) mustn't send candidates and MeetStream elsewhere.
        self.server_fixed: set[str] = {"public_base_url"} if settings.remote_dashboard and settings.public_base_url else set()

    def _set(self, key: str, value: Any) -> None:
        object.__setattr__(self.settings, key, value)  # Settings is frozen for everyone else; this is its one writer

    def apply_saved(self) -> None:
        """At start: every saved value over .env."""
        for row in self.store.saved_settings():
            key = row["key"]
            if key not in BY_KEY and key != "app_access_token":
                continue
            try:
                value = self.box.decrypt(row["value"]) if row["secret"] else json.loads(row["value"])
                if key in BY_KEY and BY_KEY[key].kind == "list":
                    value = tuple(value)
                if key == "app_name":  # a saved earlier product name shows as the current one; the row isn't changed
                    value = current_app_name(value)
            except (SecretError, ValueError) as e:
                self.problems.append(f"{key}: {e}")
                continue
            if key in self.server_fixed:
                if value != getattr(self.settings, key):
                    logger.warning("Ignoring the %s saved in the database (%s): this cloud backend uses %s=%s. Candidate "
                                   "links and MeetStream callbacks go there.", key, value, BY_KEY[key].env, getattr(self.settings, key))
                continue
            self._set(key, value)
            self.saved.add(key)
        if self.problems:
            # Names only, never values. Typically a SETTINGS_ENCRYPTION_KEY that isn't the one these were saved with.
            logger.warning("Saved settings couldn't be read and were skipped: %s. If they're secrets, check that "
                           "SETTINGS_ENCRYPTION_KEY is the key they were saved with (docs/CLOUD.md).",
                           ", ".join(p.split(":", 1)[0] for p in self.problems))

    def reload(self) -> None:
        """After switching databases: drop the old database's saved values (back to .env), then apply the new one's."""
        before = {key: getattr(self.settings, key) for key in self._base}
        for key in self.saved:
            self._set(key, self._base[key])
        self.saved, self.pending_restart, self.problems = set(), set(), []
        self.apply_saved()
        changed = {key for key in self._base if getattr(self.settings, key) != before[key]}
        if changed:
            self.on_change(changed)

    def _persist(self, key: str, value: Any) -> None:
        if key in SECRET_KEYS:
            self.store.save_setting(key, self.box.encrypt(str(value)), secret=True)
        else:
            self.store.save_setting(key, json.dumps(list(value) if isinstance(value, tuple) else value), secret=False)
        self.saved.add(key)

    def update(self, changes: dict[str, Any]) -> dict:
        """Validate every change first, then save and apply. Returns what applied now and what waits for a restart."""
        errors, typed = {}, {}
        for key, raw in changes.items():
            field = BY_KEY.get(key)
            if field is None:
                errors[key] = "isn't a setting that can be changed here"
                continue
            if key in self.server_fixed:
                errors[key] = f"is set on the server for this cloud backend ({field.env}): change it there"
                continue
            try:
                typed[key] = coerce(field, raw)
            except ValueError as e:
                errors[key] = str(e)
        if errors:
            raise ConfigError(errors)
        applied, restart = set(), set()
        for key, value in typed.items():
            self._persist(key, value)
            if BY_KEY[key].live:
                self._set(key, value)
                applied.add(key)
            else:
                self.pending_restart.add(key)
                restart.add(key)
        if applied:
            self.on_change(applied)
        return {"applied": sorted(applied), "restart_required": sorted(restart)}

    def reset(self, key: str) -> dict:
        """Forget the saved value: back to .env (or the default)."""
        if key not in BY_KEY:
            raise ConfigError({key: "isn't a setting that can be changed here"})
        self.store.delete_setting(key)
        self.saved.discard(key)
        if BY_KEY[key].live:
            self._set(key, self._base[key])
            self.on_change({key})
            return {"applied": [key], "restart_required": []}
        self.pending_restart.add(key)
        return {"applied": [], "restart_required": [key]}

    def set_access_token(self, token: str) -> None:
        """Regenerate ("" revokes): applies at once; existing sign-in cookies stop working."""
        self._persist("app_access_token", token)
        self._set("app_access_token", token)
        self.on_change({"app_access_token"})

    def source(self, key: str) -> str:
        if key in self.server_fixed:
            return "server"
        if key in self.saved:
            return "saved"
        env = BY_KEY[key].env if key in BY_KEY else "APP_ACCESS_TOKEN"
        return "env" if os.environ.get(env, "") != "" else "default"

    def view(self) -> dict:
        """The Settings page's fields. Secrets appear only as set / not set."""
        sections = []
        for sid, title in SECTIONS:
            fields = []
            for f in (x for x in FIELDS if x.section == sid):
                value = getattr(self.settings, f.key)
                item = {
                    "key": f.key, "label": f.label, "kind": f.kind, "help": f.help, "env": f.env, "live": f.live,
                    "options": list(f.options), "min": f.minimum, "max": f.maximum,
                    "source": self.source(f.key), "restart_pending": f.key in self.pending_restart,
                }
                if f.kind == "secret":
                    item["set"] = bool(value)
                else:
                    item["value"] = list(value) if isinstance(value, tuple) else value
                    default = DEFAULTS.get(f.key)
                    item["default"] = list(default) if isinstance(default, tuple) else default
                fields.append(item)
            sections.append({"id": sid, "title": title, "fields": fields})
        return {
            "sections": sections,
            "access_token": {"set": bool(self.settings.app_access_token), "source": self.source("app_access_token")},
            "encryption_key": self.box.source if self.box.source == "SETTINGS_ENCRYPTION_KEY" else "a key file in the data folder",
            "problems": list(self.problems),
            "restart_required": sorted(self.pending_restart),
            # Every field here is one value for the whole installation (all workspaces); workspace settings are on
            # the Workspace tab (app/workspace_api.py).
            "scope": "installation",
            "voice": self.voice_view(),
            "providers": self.providers_view(),
            "stt_providers": STT_PROVIDERS, "tts_providers": {k: {"default": v.default, "voices": list(v.choices)} for k, v in TTS_PROVIDERS.items()},
        }

    def voice_view(self) -> dict:
        """What the AI interviewer uses at its next launch (app/interview/agent.py agent_config, from
        app/providers.py voice_plan): each part's provider and where it comes from. The voice providers' keys live in
        MeetStream's Integrations, which Recruence can't read."""
        s = self.settings
        plan = voice_plan(s)
        part = lambda p: {"provider": p.provider, "model": p.model, "source": p.source}  # noqa: E731
        return {
            "mode": s.mia_mode,
            "realtime": {"provider": plan.llm.provider, "model": plan.llm.model, "voice": plan.tts.model} if plan.realtime else None,
            "primary": plan.primary, "label": plan.label, "ready": plan.ready, "problem": plan.problem(),
            "parts": {"stt": part(plan.stt), "llm": part(plan.llm), "tts": part(plan.tts)},
            "missing": [PART_NAMES[m] for m in plan.missing],
            "meetstream_keys": plan.meetstream_keys,
        }

    @staticmethod
    def providers_view() -> list[dict]:
        """Each AI provider and what it covers (never assumed: voice parts only where MIA supports them)."""
        return [{"id": p.id, "label": p.label, "key_field": p.key_field, "model_field": p.model_field, **capabilities(p.id)}
                for p in PROVIDERS.values()]


def field_keys() -> list[str]:
    return [f.key for f in FIELDS]


assert all(f.key in {x.name for x in dataclasses.fields(Settings)} for f in FIELDS), "every field is a Settings attribute"
