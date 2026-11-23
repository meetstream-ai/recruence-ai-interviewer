"""Settings from environment variables (and an optional .env file in the project root)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LLM_PROVIDERS = {"auto", "claude", "gemini", "openai", "xai", "ollama", "offline"}
XAI_BASE_URL = "https://api.x.ai/v1"  # OpenAI-compatible
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
APP_NAME = "Recruence"
# The product's earlier names: a saved or .env app name equal to one of them was the old default, not a choice.
FORMER_APP_NAMES = {"MIA Interviewer", "MIA AI Interviewer"}


def current_app_name(name: str) -> str:
    name = (name or "").strip()
    return APP_NAME if not name or name in FORMER_APP_NAMES else name


def public_url_problem(url: str) -> Optional[str]:
    """Why `url` can't be Recruence's public address (PUBLIC_BASE_URL), or None.

    It's where MeetStream sends its callbacks (/webhooks/...) and where candidates open their links (/apply, /interview,
    /book), so it must be HTTPS and reachable from the internet: a domain or tunnel pointing at this app, not this
    computer's own or a private network's address. Just the address: the app's paths are added to it."""
    import ipaddress
    from urllib.parse import urlsplit

    url = (url or "").strip()
    if not url:
        return "not set"
    if not url.startswith("https://"):
        return "must start with https:// (MeetStream and candidates connect over HTTPS)"
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return "has no host name"
    if parts.path.strip("/") or parts.query or parts.fragment:
        return "must be just the address, like https://interviews.example.com (no path or query)"
    if host in ("localhost", "0.0.0.0") or host.endswith((".localhost", ".local", ".internal", ".lan")):
        return (f"{host} only works on this computer or network: MeetStream and candidates on the internet can't reach it. "
                "Use your domain or a tunnel's https address")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_reserved:
        return (f"{host} is a private address: MeetStream and candidates on the internet can't reach it. "
                "Use your domain or a tunnel's https address")
    return None


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _list(name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in _env(name).split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    data_dir: Path = PROJECT_ROOT / "data"
    # Empty: SQLite in data_dir. Or a PostgreSQL URL, e.g. postgresql://user:pass@host:5432/mia (needs psycopg).
    database_url: str = ""
    log_level: str = "INFO"

    # Recruiter access. Empty: only direct localhost requests are served (development).
    app_access_token: str = ""
    # Several people (team setup, joining codes, more than one account) need a hosted PostgreSQL database; the local
    # SQLite file is for one person. Development and tests only: MIA_ALLOW_SHARED_SQLITE=true lifts that.
    allow_shared_sqlite: bool = False
    # Encrypts keys saved from the Settings page. Empty: a key file generated in DATA_DIR (app/secret_box.py).
    settings_encryption_key: str = ""
    # The cloud backend (docs/CLOUD.md): recruiters' accounts may use the dashboard through the HTTPS proxy in front of
    # it (a load balancer, Cloudflare Tunnel, Caddy...), and the desktop app connects to it instead of a local backend.
    # Off: proxied requests reach only the webhooks and the candidate pages, as before.
    remote_dashboard: bool = False
    # Proxies whose X-Forwarded-For / CF-Connecting-IP / X-Forwarded-Proto are believed (addresses or CIDR ranges).
    # Only used with remote_dashboard; the default covers a proxy on the same host or private network.
    trusted_proxies: tuple[str, ...] = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    # Trusted identity proxy (SSO in front of the dashboard). Empty secret: proxied requests reach only /webhooks/*.
    proxy_auth_secret: str = ""
    proxy_user_header: str = "x-forwarded-email"
    proxy_groups_header: str = "x-forwarded-groups"
    proxy_default_role: str = "recruiter"  # viewer, recruiter, admin, or none
    proxy_admin_groups: tuple[str, ...] = ()
    proxy_recruiter_groups: tuple[str, ...] = ()
    proxy_viewer_groups: tuple[str, ...] = ()

    # Candidate data of finished interviews is deleted after this many days (0 = keep).
    data_retention_days: int = 30
    scheduler_interval_seconds: float = 15.0

    # App LLM: plan generation and evaluation (not the live voice; that is MIA).
    llm_provider: str = "auto"
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    claude_plan_effort: str = "medium"
    claude_eval_effort: str = "high"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    gemini_fallback_models: tuple[str, ...] = ("gemini-3.5-flash", "gemini-3.5-flash-lite")
    gemini_plan_thinking: str = "low"
    gemini_eval_thinking: str = "medium"
    # OpenAI, or any OpenAI-compatible server (Ollama, vLLM, LM Studio, ...) via openai_base_url.
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"
    openai_base_url: str = OPENAI_DEFAULT_BASE_URL
    # xAI (Grok), through its OpenAI-compatible API. Its one key also covers the interview's speech-to-text,
    # conversation and voice in MeetStream (app/providers.py), where the same key is added once.
    xai_api_key: str = ""
    xai_model: str = "grok-4.20-0309-non-reasoning"
    # Ollama: models running on your own machine, through its OpenAI-compatible endpoint (no key).
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = ""
    # General, shown in the dashboard and used for defaults.
    app_name: str = APP_NAME
    time_zone: str = "UTC"  # for times written in emails; the dashboard shows each viewer's own time zone
    default_duration_minutes: int = 20

    # MeetStream
    meetstream_api_key: str = ""
    public_base_url: str = ""
    bot_name: str = "AI Interviewer"
    meetstream_retention_hours: int = 24
    # Seconds the bot waits in the lobby; capped per platform (Meet 600, Zoom 1200, Teams 1800).
    waiting_room_timeout: int = 600

    # MIA voice agent. Provider keys live in the MeetStream dashboard (Integrations), not here.
    # pipeline: separate STT/LLM/TTS (mia_stt_*, mia_llm_*, mia_tts_*). realtime: one speech-to-speech model.
    mia_mode: str = "pipeline"
    mia_realtime_provider: str = "google"
    mia_realtime_model: str = ""  # empty: MeetStream's default for the provider
    mia_realtime_voice: str = ""
    # Realtime only, experimental: give the agent the whole plan in its instructions instead of serving it
    # through next_question. Gemini Live went silent right after its first next_question call (calls 7 and 11).
    mia_realtime_inline_plan: bool = False
    # Each voice part comes from the AI provider above wherever it supports that part (app/providers.py). These name
    # a separate provider for one part: to fill a gap the AI provider leaves, or to override it. Empty: from the AI
    # provider (never assumed: a part it doesn't cover needs one of these before an interview can start).
    mia_llm_provider: str = ""
    mia_llm_model: str = ""  # empty: the AI provider's model
    mia_stt_provider: str = ""
    mia_stt_model: str = ""  # empty: the speech-to-text provider's default
    mia_tts_provider: str = ""
    mia_voice: str = ""  # empty: the voice provider's default voice
    mia_language: str = "en"
    max_follow_ups_per_question: int = 2
    # Turn-taking (seconds unless noted). Longer endpointing lets candidates pause to think without being cut off;
    # interruption thresholds stop a cough or "hmm" from cutting the agent's question off.
    mia_min_endpointing_delay: float = 1.5
    mia_max_endpointing_delay: float = 8.0
    mia_vad_min_silence_ms: int = 1000
    mia_interrupt_min_seconds: float = 1.0
    mia_interrupt_min_words: int = 3
    # Seconds between the agent's end_interview call and the bot leaving, so its goodbye is heard.
    goodbye_grace_seconds: float = 8.0
    # If the agent was told the plan is done but doesn't call end_interview, end it after this long.
    end_after_done_seconds: float = 45.0
    agent_recovery_seconds: float = 120.0  # after a "not recoverable" agent error, before the interview is ended
    # After the call, transcribe the recording to recover the agent's spoken follow-ups (MeetStream bills this).
    # Post the AI-interview and recording notice in the meeting chat when the bot joins, so the candidate
    # gets it in writing even if the spoken opening isn't heard.
    chat_disclosure: bool = True
    # Candidates end each answer by saying so; the AI treats a pause as thinking time (app/interview/agent.py).
    mia_answer_handoff: bool = True
    postcall_transcript: bool = True
    postcall_timeout_seconds: int = 300
    # Before a launch, check PUBLIC_BASE_URL's hostname resolves (a stopped quick tunnel's doesn't).
    check_public_url: bool = True
    # Signed export of each report and decision to your own endpoint (the generic ATS hook, app/export.py).
    report_webhook_url: str = ""
    report_webhook_secret: str = ""
    report_webhook_transcript: bool = False
    # Email (app/mailer.py): candidate invitations and "report ready" notes. No SMTP_HOST/SMTP_FROM: nothing is sent.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    mail_reply_to: str = ""
    # Email applicants that their application arrived (careers website only; an ATS sends its own).
    application_receipt: bool = True
    # Told to candidates as the way to ask for a human interviewer or an accommodation.
    recruiter_contact: str = ""
    notify_emails: tuple[str, ...] = ()
    # Websites (origins like https://careers.example.com) allowed to show a role and submit applications to Recruence's
    # public application API from their own pages (docs/DEMO_SITE.md). Empty: only Recruence's own application page.
    public_cors_origins: tuple[str, ...] = ()
    # The dashboard's address, for links in the "report ready" note (optional).
    dashboard_url: str = ""

    @property
    def mia_label(self) -> str:
        """Short description of the voice agent for reports and the dashboard."""
        if self.mia_mode == "realtime":
            return (
                f"{self.mia_realtime_provider} realtime"
                + (f" ({self.mia_realtime_model})" if self.mia_realtime_model else "")
                + (", plan in prompt" if self.inline_plan else "")
            )
        from app.providers import voice_plan
        return voice_plan(self).label

    @property
    def inline_plan(self) -> bool:
        return self.mia_mode == "realtime" and self.mia_realtime_inline_plan

    @property
    def db_path(self) -> Path:
        return self.data_dir / "interviewer.db"

    @property
    def database(self) -> Path | str:
        """What the store opens: DATABASE_URL if set, otherwise the SQLite file in DATA_DIR."""
        return self.database_url or self.db_path

    @property
    def meetstream_ready(self) -> tuple[bool, str]:
        if not self.meetstream_api_key:
            return False, "Set MEETSTREAM_API_KEY"
        problem = public_url_problem(self.public_base_url)
        if problem == "not set":
            return False, "Set PUBLIC_BASE_URL to the public https URL MeetStream can reach (e.g. a cloudflared tunnel)"
        if problem:
            return False, f"PUBLIC_BASE_URL {problem}"
        return True, ""

    @property
    def resolved_llm(self) -> str:
        """The LLM actually used. An explicit choice without its key falls back to offline."""
        p = self.llm_provider
        if p == "offline":
            return "offline"
        if p == "claude":
            return "claude" if self.anthropic_api_key else "offline"
        if p == "gemini":
            return "gemini" if self.gemini_api_key else "offline"
        if p == "ollama":
            return "ollama" if self.ollama_model else "offline"
        if p == "openai":
            # A self-hosted server usually needs no key; OpenAI itself does.
            return "openai" if self.openai_api_key or self.openai_base_url != OPENAI_DEFAULT_BASE_URL else "offline"
        if p == "xai":
            return "xai" if self.xai_api_key else "offline"
        if self.anthropic_api_key:
            return "claude"
        if self.gemini_api_key:
            return "gemini"
        if self.openai_api_key:
            return "openai"
        if self.xai_api_key:
            return "xai"
        return "offline"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        data_dir = Path(_env("DATA_DIR", "data"))
        if not data_dir.is_absolute():
            data_dir = PROJECT_ROOT / data_dir
        provider = _env("LLM_PROVIDER", "auto").lower() or "auto"
        if provider not in LLM_PROVIDERS:
            raise ValueError(f"LLM_PROVIDER must be one of {sorted(LLM_PROVIDERS)}, got {provider!r}")
        return cls(
            data_dir=data_dir,
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            app_access_token=_env("APP_ACCESS_TOKEN"),
            allow_shared_sqlite=_env("MIA_ALLOW_SHARED_SQLITE").lower() in {"1", "true", "yes", "on"},
            proxy_auth_secret=_env("PROXY_AUTH_SECRET"),
            proxy_user_header=_env("PROXY_USER_HEADER", "x-forwarded-email").lower() or "x-forwarded-email",
            proxy_groups_header=_env("PROXY_GROUPS_HEADER", "x-forwarded-groups").lower(),
            proxy_default_role=_env("PROXY_DEFAULT_ROLE", "recruiter").lower() or "recruiter",
            proxy_admin_groups=_list("PROXY_ADMIN_GROUPS"),
            proxy_recruiter_groups=_list("PROXY_RECRUITER_GROUPS"),
            proxy_viewer_groups=_list("PROXY_VIEWER_GROUPS"),
            data_retention_days=int(_env("DATA_RETENTION_DAYS", "30")),
            database_url=_env("DATABASE_URL"),
            settings_encryption_key=_env("SETTINGS_ENCRYPTION_KEY"),
            llm_provider=provider,
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            claude_model=_env("CLAUDE_MODEL", "claude-opus-5-5"),
            claude_plan_effort=_env("CLAUDE_PLAN_EFFORT", "medium"),
            claude_eval_effort=_env("CLAUDE_EVAL_EFFORT", "high"),
            gemini_api_key=_env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY"),
            gemini_model=_env("GEMINI_MODEL", "gemini-3.6-flash"),
            gemini_fallback_models=tuple(
                m.strip() for m in _env("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-3.5-flash-lite").split(",") if m.strip()
            ),
            gemini_plan_thinking=_env("GEMINI_PLAN_THINKING", "low"),
            gemini_eval_thinking=_env("GEMINI_EVAL_THINKING", "medium"),
            openai_api_key=_env("OPENAI_API_KEY"),
            openai_model=_env("OPENAI_MODEL", "gpt-4.1") or "gpt-4.1",
            openai_base_url=(_env("OPENAI_BASE_URL") or OPENAI_DEFAULT_BASE_URL).rstrip("/"),
            xai_api_key=_env("XAI_API_KEY"),
            xai_model=_env("XAI_MODEL", "grok-4.20-0309-non-reasoning") or "grok-4.20-0309-non-reasoning",
            ollama_base_url=(_env("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/"),
            ollama_model=_env("OLLAMA_MODEL"),
            app_name=current_app_name(_env("APP_NAME", APP_NAME)),
            time_zone=_env("TIME_ZONE", "UTC") or "UTC",
            default_duration_minutes=int(_env("DEFAULT_DURATION_MINUTES", "20")),
            meetstream_api_key=_env("MEETSTREAM_API_KEY"),
            public_base_url=_env("PUBLIC_BASE_URL").rstrip("/"),
            bot_name=_env("BOT_NAME", "AI Interviewer") or "AI Interviewer",
            meetstream_retention_hours=int(_env("MEETSTREAM_RETENTION_HOURS", "24")),
            waiting_room_timeout=int(_env("WAITING_ROOM_TIMEOUT", "600")),
            mia_mode=_env("MIA_MODE", "pipeline").lower() or "pipeline",
            mia_realtime_provider=_env("MIA_REALTIME_PROVIDER", "google"),
            mia_realtime_model=_env("MIA_REALTIME_MODEL"),
            mia_realtime_voice=_env("MIA_REALTIME_VOICE"),
            mia_realtime_inline_plan=_env("MIA_REALTIME_INLINE_PLAN", "false").lower() in {"1", "true", "yes", "on"},
            mia_llm_provider=_env("MIA_LLM_PROVIDER"),
            mia_llm_model=_env("MIA_LLM_MODEL"),
            mia_stt_provider=_env("MIA_STT_PROVIDER"),
            mia_stt_model=_env("MIA_STT_MODEL"),
            mia_tts_provider=_env("MIA_TTS_PROVIDER"),
            mia_voice=_env("MIA_VOICE"),
            mia_language=_env("MIA_LANGUAGE", "en") or "en",
            max_follow_ups_per_question=int(_env("MAX_FOLLOW_UPS_PER_QUESTION", "2")),
            mia_min_endpointing_delay=float(_env("MIA_MIN_ENDPOINTING_DELAY", "1.5")),
            mia_max_endpointing_delay=float(_env("MIA_MAX_ENDPOINTING_DELAY", "8.0")),
            mia_vad_min_silence_ms=int(_env("MIA_VAD_MIN_SILENCE_MS", "1000")),
            mia_interrupt_min_seconds=float(_env("MIA_INTERRUPT_MIN_SECONDS", "1.0")),
            mia_interrupt_min_words=int(_env("MIA_INTERRUPT_MIN_WORDS", "3")),
            chat_disclosure=_env("CHAT_DISCLOSURE", "true").lower() in {"1", "true", "yes", "on"},
            mia_answer_handoff=_env("MIA_ANSWER_HANDOFF", "true").lower() in {"1", "true", "yes", "on"},
            postcall_transcript=_env("POSTCALL_TRANSCRIPT", "true").lower() in {"1", "true", "yes", "on"},
            postcall_timeout_seconds=int(_env("POSTCALL_TIMEOUT_SECONDS", "300")),
            check_public_url=_env("CHECK_PUBLIC_URL", "true").lower() in {"1", "true", "yes", "on"},
            report_webhook_url=_env("REPORT_WEBHOOK_URL"),
            report_webhook_secret=_env("REPORT_WEBHOOK_SECRET"),
            report_webhook_transcript=_env("REPORT_WEBHOOK_TRANSCRIPT", "false").lower() in {"1", "true", "yes", "on"},
            smtp_host=_env("SMTP_HOST"),
            smtp_port=int(_env("SMTP_PORT", "587") or "587"),
            smtp_username=_env("SMTP_USERNAME"),
            smtp_password=_env("SMTP_PASSWORD"),
            smtp_from=_env("SMTP_FROM"),
            smtp_starttls=_env("SMTP_STARTTLS", "true").lower() in {"1", "true", "yes", "on"},
            smtp_ssl=_env("SMTP_SSL", "false").lower() in {"1", "true", "yes", "on"},
            mail_reply_to=_env("MAIL_REPLY_TO"),
            application_receipt=_env("APPLICATION_RECEIPT", "true").lower() in {"1", "true", "yes", "on"},
            recruiter_contact=_env("RECRUITER_CONTACT"),
            notify_emails=_list("NOTIFY_EMAILS"),
            public_cors_origins=tuple(o.rstrip("/") for o in _list("PUBLIC_CORS_ORIGINS")),
            remote_dashboard=_env("REMOTE_DASHBOARD", "false").lower() in {"1", "true", "yes", "on"},
            trusted_proxies=_list("TRUSTED_PROXIES") or Settings.trusted_proxies,
            dashboard_url=_env("DASHBOARD_URL"),
        )
