"""The dashboard's Settings page: what is configured, never the secrets themselves.

Settings live in `.env` (restart to apply); this only shows them, grouped, with the variable
that controls each one. Secret values appear only as "set" / "not set".
"""

from __future__ import annotations

from urllib.parse import urlparse

from app.config import Settings
from app.store import describe


def _set(value: str) -> str:
    return "set" if value else "not set"


def _on(flag: bool) -> str:
    return "on" if flag else "off"


def overview(settings: Settings, ai_name: str, ai_is_ai: bool, exporter_enabled: bool, mail_enabled: bool) -> list[dict]:
    s = settings
    sign_in = ("your SSO proxy (PROXY_AUTH_SECRET), plus the access token" if s.proxy_auth_secret
               else "access token (APP_ACCESS_TOKEN)" if s.app_access_token else "none: this machine only")
    export_host = urlparse(s.report_webhook_url).hostname or ""
    return [
        {"title": "Sign-in and roles", "items": [
            ["Sign-in", sign_in, "APP_ACCESS_TOKEN, PROXY_AUTH_SECRET"],
            ["Users not in a listed group", s.proxy_default_role if s.proxy_auth_secret else "not used", "PROXY_DEFAULT_ROLE"],
            ["Admin / recruiter / viewer groups",
             " / ".join(", ".join(g) or "none" for g in (s.proxy_admin_groups, s.proxy_recruiter_groups, s.proxy_viewer_groups)) if s.proxy_auth_secret else "not used",
             "PROXY_ADMIN_GROUPS, PROXY_RECRUITER_GROUPS, PROXY_VIEWER_GROUPS"],
        ]},
        {"title": "Plans and evaluation (the app's AI)", "items": [
            ["Model", ai_name + ("" if ai_is_ai else " (no AI key: template plans, heuristic scoring)"), "LLM_PROVIDER and the provider's settings"],
            ["Anthropic / Gemini / OpenAI key", f"{_set(s.anthropic_api_key)} / {_set(s.gemini_api_key)} / {_set(s.openai_api_key)}",
             "ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY"],
        ]},
        {"title": "AI interviewer (MeetStream's MIA)", "items": [
            ["Voice agent", s.mia_label, "MIA_MODE, MIA_STT_*, MIA_LLM_*, MIA_TTS_PROVIDER, MIA_VOICE"],
            ["Name in the meeting", s.bot_name, "BOT_NAME"],
            ["Waits for the candidate to finish", f"{s.mia_min_endpointing_delay}-{s.mia_max_endpointing_delay} s of silence",
             "MIA_MIN_ENDPOINTING_DELAY, MIA_MAX_ENDPOINTING_DELAY"],
            ["Interruption thresholds", f"{s.mia_interrupt_min_seconds} s, {s.mia_interrupt_min_words} words" if s.mia_interrupt_min_seconds or s.mia_interrupt_min_words else "off",
             "MIA_INTERRUPT_MIN_SECONDS, MIA_INTERRUPT_MIN_WORDS"],
            ["Follow-ups per question", str(s.max_follow_ups_per_question), "MAX_FOLLOW_UPS_PER_QUESTION"],
            ["AI notice in the meeting chat", _on(s.chat_disclosure) + " (never on Zoom)", "CHAT_DISCLOSURE"],
            ["Provider keys", "kept in the MeetStream dashboard, Configure → Integrations", "not in .env"],
        ]},
        {"title": "MeetStream", "items": [
            ["API key", _set(s.meetstream_api_key), "MEETSTREAM_API_KEY"],
            ["Public address", s.public_base_url or "not set", "PUBLIC_BASE_URL"],
            ["Check the address before each launch", _on(s.check_public_url), "CHECK_PUBLIC_URL"],
            ["Transcript rebuilt from the recording", _on(s.postcall_transcript) + f" (up to {s.postcall_timeout_seconds} s)", "POSTCALL_TRANSCRIPT, POSTCALL_TIMEOUT_SECONDS"],
            ["Lobby wait", f"{s.waiting_room_timeout} s", "WAITING_ROOM_TIMEOUT"],
            ["Recordings kept at MeetStream", f"{s.meetstream_retention_hours} h", "MEETSTREAM_RETENTION_HOURS"],
        ]},
        {"title": "Email", "items": [
            ["Sending", f"{_on(mail_enabled)}" + (f", via {s.smtp_host}:{s.smtp_port} from {s.smtp_from}" if mail_enabled else ""), "SMTP_HOST, SMTP_PORT, SMTP_FROM, SMTP_USERNAME, SMTP_PASSWORD"],
            ["How candidates ask for a human interviewer", s.recruiter_contact or "by replying to the invitation", "RECRUITER_CONTACT, MAIL_REPLY_TO"],
            ["\"Report ready\" notes to", f"{len(s.notify_emails)} address(es)" if s.notify_emails else "nobody", "NOTIFY_EMAILS"],
        ]},
        {"title": "Report export (ATS hook)", "items": [
            ["Export", f"on, to {export_host}" if exporter_enabled else "off", "REPORT_WEBHOOK_URL, REPORT_WEBHOOK_SECRET"],
            ["Includes the transcript", _on(s.report_webhook_transcript), "REPORT_WEBHOOK_TRANSCRIPT"],
        ]},
        {"title": "Data", "items": [
            ["Database", describe(s.database), "DATABASE_URL (empty: SQLite in DATA_DIR)"],
            ["Finished interviews deleted after", f"{s.data_retention_days} days" if s.data_retention_days else "never", "DATA_RETENTION_DAYS"],
        ]},
    ]
