"""FastAPI app: recruiter API, live updates (SSE), MeetStream webhooks, MIA tool endpoints, and the dashboard.

Run: uvicorn app.server:create_app --factory --port 8000
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Union

import anyio.from_thread
from sqlalchemy import exc as sa_exc
import httpx
import meetstream
from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings, public_url_problem
from app.interview.agent import TOOL_NAMES
from app.interview.manager import LaunchError, SessionManager, public_host_resolves, room_key
from app.llm import InterviewAI, SwitchableAI, build_ai
from app.llm.probe import list_models, test_provider
from app import config_manager
from app.config_manager import AI_KEYS, ConfigError, ConfigManager
from app.secret_box import SecretError, load_box
from app.meeting.calendar import staleness, sync_problems, upcoming_meetings
from app.meeting.platforms import PLATFORMS, UnsupportedMeetingLink, detect_platform
from app import booking as bookings, dbconfig, demo, lifecycle, recruiting, roles, settings_view
from app import setup as first_run
from app.models import ApplicationAnswer, ApplicationQuestion, Invitation, JobDetails, Screening
from app.models import ApplicationInput, Booking, CandidateInput, EligibilityCriterion, InterviewFormat, InterviewOptions, Decision, Interview, InterviewSetup, InterviewStatus, Plan, Recommendation, Role, RoleSetup, utcnow
from app.evaluation import evaluate_interview
from app.llm.base import EvaluationInput
from app.planning import PlanError, context_for, generate_plan, validate_plan
from app.resume import MAX_BYTES, ResumeError, extract_resume
from app.export import ExportingSink, ReportExporter
from app.mailer import Email, Mailer, MailError, NotifyingSink, cancellation, interview_received, invitation
from app.security import (COOKIE, ProxyAuth, User, cookie_value, is_direct_local, make_guard, plain_http_via_proxy,
                          served_over_https)
from app.store import CURRENT_WORKSPACE, DuplicateApplication, NoWorkspace, Store, describe
from app.accounts import Accounts
from app import gateway, meetings, workspace_api
from app.ats import routes as ats_routes
from app.ats.service import IntegrationService, Pipeline, ResultSyncSink
from app.security import ACCOUNT_COOKIE
from app.compare import same

WEB_DIR = Path(__file__).parent / "web"
ROLE_APPLICATIONS_PER_HOUR = 200  # per role, from all addresses together
SSE_KEEPALIVE_SECONDS = 15
LOCKED_STATUSES = {"scheduled", "in_progress"}

logger = logging.getLogger(__name__)
READY_TIMEOUT_SECONDS = 3.0
HSTS_SECONDS = 180 * 24 * 3600  # 180 days
DATABASE_DOWN = "The database isn't answering right now. Anything you were saving wasn't saved: try again in a minute."


class LoginBody(BaseModel):
    token: str = Field(min_length=1, max_length=500)


class LaunchBody(BaseModel):
    meeting_link: str = Field(min_length=1, max_length=2000)
    consent_attested: bool = False
    scheduled_at: Optional[datetime] = None
    # How this interview runs (kept on the interview, so a scheduled launch uses them too).
    mode: Literal["ai", "analyst"] = "ai"
    candidate_meeting_name: str = Field(default="", max_length=200)
    questions_in_chat: Optional[bool] = None  # empty: the interview's own setting (on by default)
    record_video: bool = False  # older clients; format supersedes it
    format: Optional[InterviewFormat] = None  # empty: keep the interview's saved format
    require_camera: Optional[bool] = None
    require_screen_share: Optional[bool] = None


class DecisionBody(BaseModel):
    decision: Decision
    note: str = Field(default="", max_length=4000)


class ConfigBody(BaseModel):
    changes: dict[str, Any]


class ProviderTestBody(BaseModel):
    provider: Literal["gemini", "claude", "openai", "xai", "ollama"]
    api_key: str = Field(default="", max_length=500)  # empty: the saved key
    model: str = Field(default="", max_length=200)
    base_url: str = Field(default="", max_length=500)


class BookingBody(BaseModel):
    meeting_link: str = Field(min_length=1, max_length=2000)
    slots: list[datetime] = Field(min_length=1, max_length=bookings.MAX_SLOTS)
    expires_at: Optional[datetime] = None  # book by then; empty: until the last offered time


class UnlockBody(BaseModel):
    code: str = Field(min_length=4, max_length=20)


class SetupStateBody(BaseModel):
    mode: Optional[Literal["personal", "team"]] = None
    admin_name: Optional[str] = Field(default=None, max_length=200)
    admin_email: Optional[str] = Field(default=None, max_length=320)
    skip: Optional[Literal["meetstream", "email", "careers"]] = None
    unskip: Optional[Literal["meetstream", "email", "careers"]] = None
    ai_offline: Optional[bool] = None  # chose to run without an AI model (template plans)


class SetupAccessBody(BaseModel):
    method: Literal["token", "local_only"]


class SetupDatabaseBody(BaseModel):
    choice: Literal["current", "postgresql"]
    url: str = Field(default="", max_length=2000)
    data: Literal["copy", "use_existing"] = "copy"


class DatabaseBody(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    # Empty target: "copy" this install's data into it, or "empty" to start afresh. Target with Recruence data: "use_existing".
    data: Literal["copy", "empty", "use_existing"] = "copy"
    replace: bool = False  # confirm replacing a connection already saved here


class HiringBody(BaseModel):
    """A role's recruiting settings. Omitted fields keep their saved values."""

    passing_score: Optional[int] = Field(default=None, ge=0, le=100)
    criteria: Optional[list[EligibilityCriterion]] = Field(default=None, max_length=15)
    published: Optional[bool] = None
    application_deadline: Optional[datetime] = None
    clear_deadline: bool = False
    invitation_days: Optional[int] = Field(default=None, ge=1, le=60)
    auto_invite: Optional[bool] = None  # older clients: True is "auto", False is "manual"
    resume_review: Optional[Literal["auto", "manual"]] = None
    meeting_link: Optional[str] = Field(default=None, max_length=2000)
    options: Optional[dict] = None
    screening_threshold: Optional[int] = Field(default=None, ge=0, le=100)
    paused: Optional[bool] = None
    job: Optional[JobDetails] = None
    questions: Optional[list[ApplicationQuestion]] = Field(default=None, max_length=10)


class ApplicationDecisionBody(BaseModel):
    action: Literal["invite", "reject", "rescreen", "withdraw"]
    note: str = Field(default="", max_length=1000)


class ResendBody(BaseModel):
    days: Optional[int] = Field(default=None, ge=1, le=60)
    reopen: bool = False  # allow another attempt after a completed interview (e.g. one cut short by a technical fault)
    # Reschedule: when the interview can be started (both optional; the link then expires at window_end).
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None


class StartBody(BaseModel):
    acknowledged: bool = False


class CodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=12)


class BookBody(BaseModel):
    slot: datetime
    acknowledged: bool = False


class CancelBody(BaseModel):
    notify_candidate: bool = False


class MeetingBody(BaseModel):
    """A meeting the recruiter made for this interview (manual mode; app/meetings.py)."""
    url: str = Field(min_length=1, max_length=2000)


class InviteBody(BaseModel):
    meeting_link: str = Field(default="", max_length=2000)
    # The interview time as the recruiter's browser shows it, e.g. "Mon 3 Nov 2026, 10:00 IST". Empty: no time given.
    when_text: str = Field(default="", max_length=120)


RESUME_SECONDS = 20  # the longest a resume file may take to read


PUBLIC_HEALTH_PATH = "/webhooks/health"
# Tests put an httpx transport here that stands in for the internet (a tunnel and what's behind it).
public_transport: Optional[httpx.AsyncBaseTransport] = None
# Cloudflare's answers when its tunnel or proxy is up but nothing answers behind it (530: tunnel has no connector;
# 502/504: the origin, e.g. http://127.0.0.1:<port>, refused or timed out).
EDGE_NO_ORIGIN = {502, 503, 504, 521, 522, 523, 524, 530}


def resume_content_type(filename: str, data: bytes) -> str:
    """The stored type of an uploaded resume: only the three kinds Recruence reads (app/resume.py)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if data.startswith(b"%PDF") or ext == "pdf":
        return "application/pdf"
    if ext == "docx" or data.startswith(b"PK"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "text/plain; charset=utf-8"


async def fetch_public_health(base_url: str) -> httpx.Response:
    """GET <PUBLIC_BASE_URL>/webhooks/health from the outside, as MeetStream would reach it. Redirects aren't followed:
    MeetStream posts its callbacks to the address as given, so one that redirects doesn't work for it. Proxy settings
    from the environment (HTTPS_PROXY) apply, as for the app's other outbound calls."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=8), follow_redirects=False, transport=public_transport) as client:
        return await client.get(f"{base_url.rstrip('/')}{PUBLIC_HEALTH_PATH}", headers={"Accept": "application/json"})


def _snippet(r: httpx.Response) -> str:
    """The start of a response body, as plain text for a message (never HTML markup or binary)."""
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in ("application/json", "text/plain"):
        return f"a {ctype or 'response'} page" if r.content else "an empty response"
    text = " ".join(r.text.split())
    text = "".join(ch for ch in text if ch.isprintable())
    return (text[:160] + "…") if len(text) > 160 else text or "an empty response"


def public_health_verdict(url: str, r: httpx.Response, instance_id: str, local: str = "") -> dict:
    """What the Public URL's answer at /webhooks/health means, kept apart: reachable, Recruence, and this very app.
    {ok, result, status, message}. result: this_app, other_recruence, redirect, no_origin, not_recruence."""
    status = r.status_code
    where = f"{url.rstrip('/')}{PUBLIC_HEALTH_PATH}"
    behind = f" (this app listens on {local})" if local else ""
    if status in (301, 302, 303, 307, 308):
        return {"ok": False, "result": "redirect", "status": status,
                "message": f"{where} redirects (HTTP {status}) to {r.headers.get('location') or 'another address'}. MeetStream "
                           "doesn't follow redirects: the Public URL must lead straight to this app."}
    if status in EDGE_NO_ORIGIN:
        return {"ok": False, "result": "no_origin", "status": status,
                "message": f"{url} is reachable, but nothing answers behind it (HTTP {status}, {_snippet(r)}). Start this app, "
                           f"or point the tunnel at it{behind}."}
    body = None
    if status == 200:
        try:
            body = r.json()
        except ValueError:
            body = None
    if isinstance(body, dict) and body.get("ok") is True and isinstance(body.get("instance"), str):
        if body["instance"] == instance_id:
            return {"ok": True, "result": "this_app", "status": status, "message": f"{url} reaches this app"}
        return {"ok": False, "result": "other_recruence", "status": status,
                "message": f"{url} reaches Recruence, but another copy of it, not this one (another computer, or an older "
                           f"run still behind the tunnel). Point the tunnel at this app{behind}."}
    # Something answers, but not Recruence: typically the tunnel leads to another site, such as the careers website.
    return {"ok": False, "result": "not_recruence", "status": status,
            "message": f"{url} is reachable, but it isn't this app: {where} answered HTTP {status} ({_snippet(r)}). The "
                       f"address leads to another website or app, such as your careers site. The Public URL must be a "
                       f"tunnel or domain that leads to Recruence itself{behind}; a careers site needs its own address."}


def remote_problems(settings: Settings, check_database: bool = True) -> list[str]:
    """What a cloud backend (REMOTE_DASHBOARD) must have, so it never runs on a throwaway local database or loses the
    key that decrypts the settings saved in the database. check_database: off when the caller hands over the store."""
    out = []
    if check_database and not settings.database_url.startswith(("postgresql://", "postgres://", "postgresql+")):
        out.append("DATABASE_URL must be the PostgreSQL database (Neon), not the local SQLite file")
    if not settings.settings_encryption_key:
        out.append("SETTINGS_ENCRYPTION_KEY must be set (the same key the desktop app used, so saved settings can be read)")
    if not settings.public_base_url.startswith("https://"):
        out.append("PUBLIC_BASE_URL must be the backend's https:// address")
    return out


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    ai: InterviewAI | None = None,
    meetstream_client_factory: Callable[[], Any] | None = None,
    run_loops: bool = True,
    mail_send: Callable[[Settings, Email], None] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from app import logsafe
    logsafe.install()  # candidate link and webhook tokens never reach the logs
    from app.security import configure_remote
    configure_remote(settings.remote_dashboard, settings.trusted_proxies)
    if settings.remote_dashboard:
        problems = remote_problems(settings, check_database=store is None)
        if problems:
            raise RuntimeError("REMOTE_DASHBOARD is on, but " + "; ".join(problems) + " (docs/CLOUD.md).")
        logger.info("Remote dashboard: accounts sign in through the HTTPS proxy at %s", settings.public_base_url)
    try:
        box = load_box(settings.data_dir, settings.settings_encryption_key)
    except SecretError as e:
        raise RuntimeError(str(e)) from e
    # The database: DATABASE_URL, else a connection saved from Settings, else local SQLite (app/dbconfig.py).
    if store is None:
        try:
            db_target, db_source = dbconfig.effective(settings.database_url, settings.db_path, settings.data_dir, box)
        except dbconfig.DatabaseConfigError as e:
            raise RuntimeError(str(e)) from e
        db_problem = ""
        try:
            store = Store(db_target)
        except Exception as e:  # noqa: BLE001 - any driver error
            if db_source != "saved":
                raise
            # A saved connection (Neon, Supabase...) that is down at start: open on the local database instead of not at
            # all, so an admin can reconnect from the connection screen. DATABASE_URL failing still stops the app.
            reason = f"{type(e).__name__}: {e}"
            db_problem = (f"The saved database ({describe(db_target)}) couldn't be reached when Recruence started "
                          f"({dbconfig._scrub(reason, str(db_target))[:200]}). The local database is in use until you reconnect.")
            logger.warning(db_problem)
            db_target, db_source = settings.db_path, "fallback"
            store = Store(db_target)
    else:
        db_target, db_source, db_problem = settings.database, "given", ""
    db_state = {"source": db_source, "problem": db_problem}  # where the database in use came from; a live switch changes it
    # This installation's id; the database records each installation that uses it (app/store.py installations).
    install_id = dbconfig.installation_id(settings.data_dir)
    install_label = dbconfig.installation_label(bool(os.environ.get("MIA_DESKTOP_PORT")))
    try:
        store.record_installation(install_id, install_label)
    except Exception as e:  # noqa: BLE001 - a read-only or full database shouldn't stop the app
        logger.warning("couldn't record this installation in the database: %s", type(e).__name__)

    def secure_links() -> None:
        """Interview links from before hashed storage: moved to a hash and an encrypted copy (links don't change)."""
        try:
            moved = gateway.secure_old_links(store, box)
            if moved:
                logger.info("moved %d interview link(s) to hashed storage", moved)
        except Exception as e:  # noqa: BLE001 - never stops the app; the old links keep working until moved
            logger.warning("couldn't move older interview links: %s", type(e).__name__)

    secure_links()

    def chat_questions_on_for_roles() -> None:
        """Once per database: questions in the meeting chat became the default (2026-11-11), and a role had no way to
        choose it before, so a role's stored "off" is the old default, not a choice. Interviews could choose: unchanged."""
        try:
            done = store.get_state("data_fixes") or {}
            if done.get("roles_questions_in_chat"):
                return
            changed = 0
            for role in store.list_roles():
                o = role.hiring.options
                if o.mode == "ai" and not o.questions_in_chat:
                    store.rewrite_role(role.model_copy(update={"hiring": role.hiring.model_copy(update={
                        "options": o.model_copy(update={"questions_in_chat": True})})}))
                    changed += 1
            store.set_state("data_fixes", {**done, "roles_questions_in_chat": utcnow().isoformat()})
            if changed:
                logger.info("questions in the meeting chat switched on for %d role(s), the new default", changed)
        except Exception as e:  # noqa: BLE001 - never stops the app; tried again at the next start
            logger.warning("couldn't apply the questions-in-chat default to existing roles: %s", type(e).__name__)

    chat_questions_on_for_roles()
    # Settings saved from the dashboard go over .env before anything is built from them.
    setup = first_run.SetupState(settings.data_dir)
    config = ConfigManager(settings, store, box)
    config.apply_saved()
    ai = SwitchableAI(ai or build_ai(settings))
    exporter = ReportExporter(settings.report_webhook_url, settings.report_webhook_secret, store,
                              include_transcript=settings.report_webhook_transcript)
    mailer = Mailer(settings, send=mail_send)
    accounts = Accounts(store)

    def branding(interview: Interview) -> dict:
        """The company the candidate applied to, from its workspace (Settings → Workspace): what candidates see, never
        the product's name."""
        ws = store.interview_workspace(interview.id)
        s = accounts.settings_of(ws) if ws else {}
        return {"company": str(s.get("company_name", "")).strip(), "contact": str(s.get("support_contact", "")).strip()}

    def meeting_view(interview: Interview) -> dict:
        """Where the interview takes place, for the dashboard: its own meeting, or the role's shared room."""
        row = store.meeting_for(interview.id)
        own = None
        if row:
            try:
                url = box.decrypt(row["join_url_enc"])
            except Exception:  # noqa: BLE001 - another key: shown as missing, the room link still works
                url = ""
            own = {"url": url, "platform": row["platform"], "mode": row["mode"], "provider": row["provider"], "set_by": row["created_by"],
                   "updated_at": row["updated_at"], "status": row["status"]}
        role = store.get_role(interview.role_id) if interview.role_id else None
        shared = bool(role and interview.meeting_link and interview.meeting_link == role.hiring.meeting_link and not own)
        return {"own": own, "shared_room": shared, "link": interview.meeting_link, "platform": interview.platform,
                "capture": meetings.capture_note(interview.platform) if interview.platform else None}

    def ensure_link(interview: Interview) -> tuple[Interview, str]:
        """The interview's gateway link (app/gateway.py), made if it has none: what every candidate email carries, never
        the meeting's own link. A scheduled interview's link opens around its time."""
        token = gateway.link_token(interview, box) if interview.invitation else ""
        if token:
            return interview, token
        now = utcnow()
        end = (interview.scheduled_at + timedelta(minutes=interview.setup.duration_minutes) + gateway.WINDOW_AFTER
               if interview.scheduled_at else now + timedelta(days=7))
        token = recruiting.new_token()
        inv = Invitation(created_at=now, expires_at=recruiting.utc(end) if interview.scheduled_at else end)
        saved = store.save_interview(gateway.seal(interview.model_copy(update={"invitation": inv}), token, box))
        store.set_invite_token(saved.id, token)
        store.audit("invitation_link_created", saved.id)
        return saved, token

    def dashboard_interview(interview: Interview) -> dict:
        return interview.model_dump(mode="json", exclude={"invitation": {"token", "token_enc"}} if interview.invitation else None)

    gateway_hits: dict[str, list[float]] = {}

    def limited_ip(key: str, limit: int, window: float) -> bool:
        import time
        now = time.monotonic()
        hits = [x for x in gateway_hits.get(key, []) if now - x < window]
        gateway_hits[key] = hits + [now]
        prune(gateway_hits, window)
        return len(hits) >= limit

    def workspace_recipients(report) -> list[str]:
        ws = store.interview_workspace(report.interview_id)
        return list(accounts.settings_of(ws).get("notify_emails", [])) if ws else []

    # Finished reports: saved, then (when set up) exported and announced to the hiring team: NOTIFY_EMAILS and
    # the interview's workspace's notification list (checked when each report is ready, so later changes count).
    sink: Any = store
    if exporter.enabled:
        sink = ExportingSink(sink, exporter)
    def candidate_note(report) -> Optional[Email]:
        """The workspace's optional "we received your interview" email: only after a consented, real interview."""
        ws = store.interview_workspace(report.interview_id)
        if not (ws and accounts.settings_of(ws).get("candidate_notices")) or not report.consent_given:
            return None
        interview = store.get_interview(report.interview_id)
        if interview is None or interview.demo or not interview.setup.candidate_email:
            return None
        return interview_received(interview, settings)

    sink = NotifyingSink(sink, mailer, store, workspace_recipients, candidate_note)

    def passing_score(interview: Interview) -> int:
        role = store.get_role(interview.role_id) if interview.role_id else None
        return role.hiring.passing_score if role else 70

    # External ATSs (app/ats): their candidates go through the same steps as everyone's (process_application,
    # invite_application below); interview results go back to the ATS a candidate came from.
    integrations_svc = IntegrationService(store, box, Pipeline(
        screen=lambda application_id: manager.run_background(process_application(application_id), f"screening {application_id}"),
        invite=lambda role, a: invite_application(role, a, "automatic"),  # an ATS's candidates: auto-invite or its decision
        background=lambda coro, label: manager.run_background(coro, label),
        # The dashboard's address (DASHBOARD_URL): the public address serves only candidates and webhooks.
        report_url=lambda sid: f"{settings.dashboard_url.rstrip('/')}/#/s/{sid}" if settings.dashboard_url else "",
        passing_score=passing_score))
    sink = ResultSyncSink(sink, integrations_svc)
    async def reaches_this_app() -> Optional[bool | str]:
        """Whether the Public URL leads back to this very process: True, or why not (a message), or None when it
        couldn't tell (no connection). On a database several installations share, it may lead to another one."""
        try:
            r = await fetch_public_health(settings.public_base_url)
        except httpx.HTTPError:
            return None
        verdict = public_health_verdict(settings.public_base_url, r, instance_id)
        return True if verdict["ok"] else verdict["message"]

    manager = SessionManager(settings, store, ai, meetstream_client_factory, sink=sink, install_id=install_id,
                             reaches_this_app=reaches_this_app)
    accounts_state: dict = {"exist": None}

    def accounts_exist() -> bool:
        if not accounts_state["exist"]:
            accounts_state["exist"] = accounts.count() > 0
        return accounts_state["exist"]

    def accounts_changed() -> None:
        accounts_state["exist"] = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        ready, why = settings.meetstream_ready
        logger.info("LLM: %s | MeetStream: %s", ai.name, "ready" if ready else f"not configured ({why})")
        setup.settle_first_launch(
            existing_data=store.has_data(),
            preconfigured=bool((settings.app_access_token or settings.proxy_auth_secret) and ai.is_ai),
            # On the fallback database the accounts are elsewhere, not gone: never reopen setup because of that.
            access_set=bool(settings.app_access_token or settings.proxy_auth_secret or accounts_exist() or db_state["source"] == "fallback"),
        )
        if not setup.completed:
            # Shown in the log only: anyone who can read it has the server, so it's safe to unlock setup with.
            logger.warning("First-run setup: open the dashboard and, if asked, enter the setup code %s", setup.issue_code())
        if not settings.app_access_token:
            logger.warning("APP_ACCESS_TOKEN is not set: the dashboard only answers direct localhost requests")
        if run_loops:
            await manager.start_loops()
            expiry_task = asyncio.create_task(expiry_loop())
            ats_task = asyncio.create_task(ats_retry_loop())
            # Applications received just before a restart are screened now.
            for role in store.list_roles():
                for a in store.applications_for_role(role.id):
                    if a.status == "screening":
                        manager.run_background(process_application(a.id), f"screening {a.id}")
        yield
        if run_loops:
            expiry_task.cancel()
            ats_task.cancel()
        await manager.aclose()
        await exporter.aclose()
        await mailer.aclose()

    app = FastAPI(title="Recruence", lifespan=lifespan)
    app.state.manager = manager
    app.state.store = store
    app.state.mailer = mailer
    app.state.setup = setup
    app.state.exporter = exporter
    app.state.integrations = integrations_svc
    app.state.accounts = accounts
    proxy = ProxyAuth(
        secret=settings.proxy_auth_secret, user_header=settings.proxy_user_header,
        groups_header=settings.proxy_groups_header, default_role=settings.proxy_default_role,
        admin_groups=frozenset(settings.proxy_admin_groups), recruiter_groups=frozenset(settings.proxy_recruiter_groups),
        viewer_groups=frozenset(settings.proxy_viewer_groups),
    )
    def setup_user(request: Request) -> Optional[User]:
        return User("setup", "admin", "setup") if setup.valid_session(request.cookies.get(first_run.COOKIE, "")) else None

    app.middleware("http")(make_guard(lambda: settings.app_access_token, proxy, setup_user,
                                      account_user=workspace_api.account_user_resolver(accounts), accounts_exist=accounts_exist,
                                      workspace_for=workspace_api.workspace_resolver(accounts, store)))

    @app.exception_handler(NoWorkspace)
    async def no_workspace(_: Request, exc: NoWorkspace):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=409)

    SHARED_NEEDS_HOSTED = ("Sharing Recruence with other people needs a hosted PostgreSQL database (Neon, Supabase, Amazon RDS, your own "
                           "server): the local database is for one person on this computer. Connect one in Settings → Database → "
                           "Switch database.")

    def sharing_problem() -> Optional[str]:
        """Why several people can't use this installation's database, or None if they can."""
        return SHARED_NEEDS_HOSTED if store.backend == "sqlite" and not settings.allow_shared_sqlite else None

    def team_mode() -> bool:
        return setup.data.get("mode") == "team"

    def switch_database(target: Any, source: str) -> None:
        """Use another database now (app/workspace_api.py connect). Everyone is signed out of the old one first;
        then settings, AI provider and MeetStream client follow the new database's saved settings."""
        if manager.live:
            raise HTTPException(409, "An interview is running. Switch the database when no interview is live.")
        try:
            store._run(workspace_api.auth_sessions.delete())
            store.reconnect(target)
        except Exception as e:  # noqa: BLE001 - the old database stays in use
            raise HTTPException(422, f"Couldn't use that database: {type(e).__name__}: {str(e)[:200]}") from None
        db_state["source"] = source
        db_state["problem"] = ""
        store.record_installation(install_id, install_label)
        secure_links()
        config.reload()
        ai.swap(build_ai(settings))
        manager.reset_client()
        accounts_changed()
        logger.info("database switched to %s", describe(target))
        store.audit("database_switched", detail=describe(target))

    workspace_api.register(app, workspace_api.Deps(
        store=store, accounts=accounts, settings=settings, mailer=mailer, setup=setup, box=box, gate=workspace_api.ConnectionGate(),
        db_state=db_state, switch_database=switch_database, accounts_changed=accounts_changed, background=manager.run_background,
        sharing_problem=sharing_problem, team_mode=team_mode, install_id=install_id))
    ats_routes.register(app, integrations_svc, store, lambda: settings.public_base_url)

    # A company's own careers site (PUBLIC_CORS_ORIGINS) may read a role and submit applications from its pages:
    # only the public application API, no cookies, nothing else in Recruence.
    @app.middleware("http")
    async def careers_site_cors(request: Request, call_next):
        origin = (request.headers.get("origin") or "").rstrip("/")
        allowed = bool(origin) and request.url.path.startswith("/api/public/apply/") and origin in settings.public_cors_origins
        cors = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "GET, POST",
                "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "600", "Vary": "Origin"} if allowed else {}
        if request.method == "OPTIONS" and allowed:
            return Response(status_code=204, headers=cors)
        response = await call_next(request)
        for key, value in cors.items():
            response.headers[key] = value
        return response

    # Browser hardening on every response. The pages load nothing from other sites and build their content as text, so
    # the policy can be strict: scripts only from here, no framing (clickjacking on the candidate pages), no plugins.
    # Inline style attributes stay allowed: the pages set some (bar heights, tooltip positions) from their own script.
    SECURITY_HEADERS = {
        "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                                    "media-src 'self' blob:; font-src 'self' data:; connect-src 'self'; object-src 'none'; "
                                    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
    }

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # HSTS only when the visitor reached us over HTTPS (directly, or through the trusted proxy of the remote
        # dashboard): browsers then refuse plain http to this host. No includeSubDomains: other hosts of the
        # company's domain aren't ours to decide for. A proxy that sets its own header keeps it.
        if served_over_https(request):
            response.headers.setdefault("Strict-Transport-Security", f"max-age={HSTS_SECONDS}")
        return response

    @app.middleware("http")
    async def revalidate_dashboard(request: Request, call_next):
        # The dashboard has no build step or hashed file names, so browsers must revalidate it after an update.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # Registered last, so it wraps every other middleware (the sign-in guard reads accounts from the database too): a
    # database that stops answering gives a clear 503 everywhere, never a bare 500, and its details stay in the log.
    @app.middleware("http")
    async def database_outage(request: Request, call_next):
        try:
            return await call_next(request)
        except (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError) as e:
            logger.warning("Database unavailable during %s %s: %s", request.method, request.url.path, type(e.orig if getattr(e, "orig", None) else e).__name__)
            from fastapi.responses import JSONResponse
            body = ({"ok": False, "database": {"ok": False, "problem": DATABASE_DOWN}} if request.url.path == "/api/health/ready"
                    else {"detail": DATABASE_DOWN})
            headers = {**SECURITY_HEADERS, "Retry-After": "30"}
            if served_over_https(request):
                headers["Strict-Transport-Security"] = f"max-age={HSTS_SECONDS}"
            return JSONResponse(body, status_code=503, headers=headers)

    # Registered after the outage handler, so it runs first. A cloud backend's proxy may forward plain http instead of
    # redirecting it (Cloudflare quick tunnels do, and so can a load balancer): passwords, cookies and candidates' data
    # must never travel that way. Pages and reads move to the https address; anything that sends data is refused before
    # it's read, so a password sent over http is never checked or accepted. The redirect is built from PUBLIC_BASE_URL,
    # never from the Host header the visitor sent.
    @app.middleware("http")
    async def https_only_in_cloud(request: Request, call_next):
        if not plain_http_via_proxy(request):
            return await call_next(request)
        from fastapi.responses import JSONResponse, RedirectResponse
        if request.method in ("GET", "HEAD"):
            query = f"?{request.url.query}" if request.url.query else ""
            target = settings.public_base_url.rstrip("/") + request.url.path + query
            return RedirectResponse(target, status_code=308, headers=SECURITY_HEADERS)
        return JSONResponse({"detail": f"Use the https address: {settings.public_base_url}"}, status_code=403,
                            headers=SECURITY_HEADERS)

    def interview_or_404(interview_id: str) -> Interview:
        interview = store.get_interview(interview_id)
        if interview is None:
            raise HTTPException(404, "Interview not found")
        return interview

    def editable(interview: Interview) -> None:
        if interview.status in LOCKED_STATUSES:
            raise HTTPException(409, f"The interview is {interview.status.replace('_', ' ')}; unschedule or end it first")

    def not_cancelled(interview: Interview) -> None:
        if interview.cancelled_at:
            raise HTTPException(409, "This interview is cancelled; reopen it first")
        not_demo(interview)

    def not_demo(record: Any) -> None:
        """Demo data (app/demo.py) is for looking around: nothing about it reaches a meeting, an inbox or a candidate."""
        if getattr(record, "demo", False):
            raise HTTPException(409, "This is demo data: it can't be run, booked, invited or emailed. Remove it in Settings → System.")

    def own_plan(interview: Interview) -> None:
        if interview.role_id:
            raise HTTPException(409, "This interview uses its role's questions, the same for every candidate; change them on the role")

    def role_or_404(role_id: str) -> Role:
        role = store.get_role(role_id)
        if role is None:
            raise HTTPException(404, "Role not found")
        return role

    def prune(counters: dict[str, list[float]], window: float, keep: int = 5000) -> None:
        """Drop counters with nothing recent once there are many: rate-limit memory can't grow without end."""
        import time
        if len(counters) <= keep:
            return
        now = time.monotonic()
        for key in [k for k, hits in counters.items() if not hits or now - hits[-1] >= window]:
            counters.pop(key, None)

    # ---------- health and auth ----------

    @app.get("/api/health")
    def health() -> dict:
        ready, why = settings.meetstream_ready
        return {
            "ok": True,
            "llm": {"name": ai.name, "is_ai": ai.is_ai},
            "meetstream": {"ready": ready, "reason": why, "bot_name": settings.bot_name},
            "mia": settings.mia_label,
            "retention_days": settings.data_retention_days,
            "app_name": settings.app_name,
            "default_duration_minutes": settings.default_duration_minutes,
            "report_export": {"enabled": exporter.enabled, "problem": exporter.problem or ""},
            "email": {"enabled": mailer.enabled, "report_notifications": mailer.enabled and bool(settings.notify_emails)},
        }

    @app.get("/api/health/ready")
    async def health_ready(response: Response) -> dict:
        """Readiness, for the container health check and uptime monitors: 503 while the database doesn't answer.
        /api/health stays a liveness check (the process answers), which the desktop app and the dashboard rely on to
        start and to show the Database screen when the database is the problem."""
        try:
            version = await asyncio.wait_for(asyncio.to_thread(store.ping), timeout=READY_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001 - any failure (refused, timeout, auth) means not ready
            response.status_code = 503
            logger.warning("Readiness check: the database didn't answer (%s)", type(e).__name__)
            return {"ok": False, "database": {"ok": False, "problem": DATABASE_DOWN}}
        return {"ok": True, "database": {"ok": True, "schema_version": version}}

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> dict:
        user = getattr(request.state, "user", None)
        have_accounts = accounts_exist()
        required = (bool(settings.app_access_token) or have_accounts) and not (user and user.via == "proxy")
        return {
            "auth_required": required,
            "accounts": have_accounts,
            "database_problem": (db_state.get("problem", "") if user and user.install_admin
                                 else workspace_api.PUBLIC_DB_PROBLEM if db_state.get("problem") else ""),
            "signed_in": user is not None or not (settings.app_access_token or have_accounts),
            "user": {"name": user.name, "role": user.role, "via": user.via, "email": user.email, "is_admin": user.install_admin, "user_id": user.user_id,
                     "workspace_id": user.workspace_id, "workspace_role": user.workspace_role} if user else None,
        }

    token_failures: dict[str, list[float]] = {}

    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request, response: Response) -> dict:
        import time
        token = settings.app_access_token
        who, now = client_address(request), time.monotonic()
        recent = [t for t in token_failures.get(who, []) if now - t < 15 * 60]
        if len(recent) >= 10:
            raise HTTPException(429, "Too many wrong tokens from here. Wait a few minutes and try again.")
        if not same(body.token, token):
            token_failures[who] = recent + [now]
            prune(token_failures, 15 * 60)
            raise HTTPException(401, "Wrong access token")
        token_failures.pop(who, None)
        response.set_cookie(COOKIE, cookie_value(token), httponly=True, samesite="strict", max_age=12 * 3600)
        return {"signed_in": True}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict:
        if request.cookies.get(ACCOUNT_COOKIE):
            accounts.end_session(request.cookies[ACCOUNT_COOKIE])
        response.delete_cookie(COOKIE)
        response.delete_cookie(ACCOUNT_COOKIE)
        return {"signed_in": False}

    @app.get("/api/platforms")
    def platforms() -> list[dict]:
        return [
            {"key": p.key, "name": p.name, "setup": p.setup, "speaker_attribution": p.speaker_attribution, "live_tested": p.live_tested}
            for p in PLATFORMS.values()
        ]

    # ---------- resume upload ----------

    async def read_resume(filename: str, data: bytes):
        """extract_resume in a worker thread with a time limit: a slow or crafted PDF mustn't stall the server (and
        any live interview's webhooks) while it's read."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(extract_resume, filename, data), RESUME_SECONDS)
        except asyncio.TimeoutError:
            raise ResumeError("Reading this file took too long; upload a simpler PDF or paste the resume text") from None

    @app.post("/api/resume/extract")
    async def resume_extract(file: UploadFile = File(...)) -> dict:
        """Return the text of an uploaded resume (PDF, DOCX or text). The file itself is not kept."""
        data = await file.read(MAX_BYTES + 1)
        try:
            resume = await read_resume(file.filename or "", data)
        except ResumeError as e:
            raise HTTPException(422, str(e)) from None
        return {"text": resume.text, "kind": resume.kind, "chars": len(resume.text), "truncated": resume.truncated}

    # ---------- interviews ----------

    @app.get("/api/interviews")
    def list_interviews(
        q: str = Query("", max_length=100),
        status: Optional[InterviewStatus] = None,
        platform: Optional[str] = Query(None, max_length=40),
        recommendation: Optional[Union[Recommendation, Literal["none"]]] = None,
        decision: Optional[Union[Decision, Literal["none"]]] = None,
        stage: Optional[lifecycle.Stage] = None,
        state: Optional[lifecycle.State] = None,
        assigned: Optional[Literal["automatic", "manual", "unknown"]] = None,
        role: Optional[str] = Query(None, max_length=40),
    ) -> list[dict]:
        """Every interview in the workspace, however it was created (the Interviews page and the dashboard)."""
        return store.list_interviews(q, status, platform, recommendation, decision, stage, state, assigned, role)

    @app.get("/api/activity")
    def activity(days: int = Query(30, ge=1, le=90)) -> dict:
        """Interview activity for the dashboard chart, as UTC timestamps grouped by kind."""
        # One extra day so the viewer's local "oldest day" is complete whatever their time zone.
        since = utcnow() - timedelta(days=days + 1)
        return {"days": days, **store.activity(since)}

    @app.post("/api/interviews", status_code=201)
    def create_interview(setup: InterviewSetup) -> dict:
        interview = store.create_interview(setup, assigned="manual")
        store.audit("interview_created", interview.id)
        return interview.model_dump(mode="json")

    @app.get("/api/interviews/{interview_id}")
    def get_interview(interview_id: str, request: Request) -> dict:
        interview = interview_or_404(interview_id)
        live = manager.live_for_interview(interview_id)
        audit = store.audit_log(50, interview_id)
        user = getattr(request.state, "user", None)
        if not (user and user.role == "admin"):
            # Who did what is admin-only, like the full log (/api/audit); others see what happened.
            audit = [{k: v for k, v in e.items() if k != "actor"} for e in audit]
        report = store.get_report(interview.latest_session_id) if interview.latest_session_id else None
        return {
            "interview": dashboard_interview(interview),
            "meeting": meeting_view(interview),
            "stage": lifecycle.stage(interview, bool(report and report.evaluation)),
            "invitation_undelivered": bool(interview.invitation and interview.invitation.undelivered),
            "sessions": store.list_sessions(interview_id),
            "live_session_id": live.id if live else None,
            "audit": audit,
            "booking_url": bookings.booking_url(settings, interview.booking.token) if interview.booking else None,
        }

    @app.put("/api/interviews/{interview_id}/setup")
    def update_setup(interview_id: str, setup: InterviewSetup) -> dict:
        interview = interview_or_404(interview_id)
        editable(interview)
        role = store.get_role(interview.role_id) if interview.role_id else None
        if role is not None:
            # A role's candidate: only the candidate's details change; the role's questions and approval stay.
            saved = store.save_interview(interview.model_copy(update={"setup": roles.candidate_setup(role, setup)}))
        else:
            saved = store.save_interview(interview.model_copy(update={"setup": setup, "plan_approved_at": None, "status": "draft"}))
        store.audit("setup_updated", interview_id)
        return saved.model_dump(mode="json")

    @app.post("/api/interviews/{interview_id}/plan/generate")
    async def generate(interview_id: str) -> dict:
        interview = interview_or_404(interview_id)
        editable(interview)
        own_plan(interview)
        try:
            plan, warning = await generate_plan(ai, interview.setup)
        except PlanError as e:
            raise HTTPException(422, str(e)) from None
        saved = store.save_interview(interview.model_copy(update={"plan": plan, "plan_approved_at": None, "status": "draft"}))
        store.audit("plan_generated", interview_id, detail=plan.source + (f"; AI failed: {warning}" if warning else ""))
        return {"interview": saved.model_dump(mode="json"), "warning": warning}

    @app.put("/api/interviews/{interview_id}/plan")
    def edit_plan(interview_id: str, plan: Plan) -> dict:
        interview = interview_or_404(interview_id)
        editable(interview)
        own_plan(interview)
        try:
            plan = validate_plan(plan)
        except PlanError as e:
            raise HTTPException(422, str(e)) from None
        base = (interview.plan.source if interview.plan else "") or "recruiter"
        plan = plan.model_copy(update={"source": base if base.endswith("edited by recruiter") else f"{base}, edited by recruiter"})
        saved = store.save_interview(interview.model_copy(update={"plan": plan, "plan_approved_at": None, "status": "draft"}))
        store.audit("plan_edited", interview_id)
        return saved.model_dump(mode="json")

    @app.post("/api/interviews/{interview_id}/plan/approve")
    def approve_plan(interview_id: str) -> dict:
        interview = interview_or_404(interview_id)
        editable(interview)
        if interview.plan is None:
            raise HTTPException(409, "Generate or write a plan first")
        saved = store.save_interview(interview.model_copy(update={"plan_approved_at": utcnow(), "status": "ready"}))
        store.audit("plan_approved", interview_id, detail=f"{len(interview.plan.questions)} questions")
        return saved.model_dump(mode="json")

    # ---------- roles: one approved question set for every candidate ----------

    @app.get("/api/roles")
    def list_roles() -> list[dict]:
        counts = store.role_candidate_counts()
        return [roles.summary(r, counts.get(r.id, 0)) for r in store.list_roles()]

    @app.post("/api/roles", status_code=201)
    def create_role(setup: RoleSetup) -> dict:
        role = store.create_role(setup)
        store.audit("role_created", detail=role.id)
        return role.model_dump(mode="json")

    @app.get("/api/roles/{role_id}")
    def get_role(role_id: str) -> dict:
        role = role_or_404(role_id)
        return {"role": role.model_dump(mode="json"), "candidates": roles.comparison(store, role)}

    @app.get("/api/roles/{role_id}/ranking")
    def role_ranking(role_id: str) -> dict:
        return roles.ranking(store, role_or_404(role_id))

    @app.put("/api/roles/{role_id}/setup")
    def update_role_setup(role_id: str, setup: RoleSetup) -> dict:
        role = role_or_404(role_id)
        saved = store.save_role(role.model_copy(update={"setup": setup, "plan_approved_at": None}))
        store.audit("role_setup_updated", detail=role_id)
        return saved.model_dump(mode="json")

    @app.post("/api/roles/{role_id}/plan/generate")
    async def generate_role_plan(role_id: str) -> dict:
        role = role_or_404(role_id)
        try:
            plan, warning = await generate_plan(ai, roles.planning_setup(role.setup))
        except PlanError as e:
            raise HTTPException(422, str(e)) from None
        saved = store.save_role(role.model_copy(update={"plan": roles.role_plan(plan), "plan_approved_at": None}))
        store.audit("role_plan_generated", detail=f"{role_id}; {plan.source}" + (f"; AI failed: {warning}" if warning else ""))
        return {"role": saved.model_dump(mode="json"), "warning": warning}

    @app.put("/api/roles/{role_id}/plan")
    def edit_role_plan(role_id: str, plan: Plan) -> dict:
        role = role_or_404(role_id)
        try:
            plan = validate_plan(plan)
        except PlanError as e:
            raise HTTPException(422, str(e)) from None
        base = (role.plan.source if role.plan else "") or "recruiter"
        plan = plan.model_copy(update={"source": base if base.endswith("edited by recruiter") else f"{base}, edited by recruiter"})
        saved = store.save_role(role.model_copy(update={"plan": plan, "plan_approved_at": None}))
        store.audit("role_plan_edited", detail=role_id)
        return saved.model_dump(mode="json")

    @app.post("/api/roles/{role_id}/plan/approve")
    def approve_role_plan(role_id: str) -> dict:
        role = role_or_404(role_id)
        if role.plan is None:
            raise HTTPException(409, "Generate or write the questions first")
        saved = store.save_role(role.model_copy(update={"plan_approved_at": utcnow()}))
        store.audit("role_plan_approved", detail=f"{role_id}; {len(role.plan.questions)} questions")
        return saved.model_dump(mode="json")

    @app.post("/api/roles/{role_id}/candidates", status_code=201)
    def add_candidate(role_id: str, candidate: CandidateInput) -> dict:
        role = role_or_404(role_id)
        try:
            interview = roles.add_candidate(store, role, candidate)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        store.audit("candidate_added", interview.id, detail=f"role {role_id}")
        return interview.model_dump(mode="json")

    # ---------- first-run setup (app/setup.py): every value is saved through the same APIs as Settings ----------

    def setup_view(request: Request) -> dict:
        user = getattr(request.state, "user", None)
        local = is_direct_local(request)
        # Who may continue an unfinished setup: this computer (the desktop app, or a browser on the server) while the
        # database has no accounts; a browser elsewhere with the code from the server log (Docker or a server) until
        # then; and once the connected database has accounts, whoever signs in with one of them. A database chosen
        # in setup can already have accounts (it belongs to another install), so it's sign-in then, never a code
        # that a desktop user can't see.
        signed_out = not setup.completed and user is None and not settings.app_access_token
        base = {"completed": setup.completed, "signed_in": user is not None,
                "code_required": signed_out and not accounts_exist(),
                "sign_in_required": signed_out and accounts_exist()}
        if user is None or user.role != "admin":
            return base  # nothing about the configuration until the visitor is allowed in
        cfg_fields = {f["key"]: f for s in config.view()["sections"] for f in s["fields"]}
        db = database_view()
        pending = setup.data.get("database")
        llm = ai.name if ai.is_ai else None
        steps = first_run.step_states(
            state=setup,
            access={"configured": bool(settings.app_access_token or settings.proxy_auth_secret or accounts_exist())
                    or (setup.data.get("access") == "local_only" and local),
                    "method": "proxy" if settings.proxy_auth_secret else "account" if accounts_exist() else "token" if settings.app_access_token
                    else setup.data.get("access"),
                    "accounts": accounts_exist(), "signed_in_account": bool(user.via == "account"),
                    "token_source": config.source("app_access_token") if settings.app_access_token else None,
                    "local_request": local, "proxy": bool(settings.proxy_auth_secret)},
            database={**db, "pending": ({"database": pending.get("database"), "data": pending.get("data")} if pending else None),
                      "team_needs_hosted": team_mode() and bool(sharing_problem())},
            ai={"configured": ai.is_ai, "name": llm, "provider": settings.llm_provider,
                "offline_chosen": bool(setup.data.get("ai_offline")) and not ai.is_ai},
            meetstream={"key_set": bool(settings.meetstream_api_key), "key_source": cfg_fields["meetstream_api_key"]["source"],
                        "public_base_url": settings.public_base_url, "url_source": cfg_fields["public_base_url"]["source"]},
            email={"configured": mailer.enabled, "from": settings.smtp_from, "host_source": cfg_fields["smtp_host"]["source"]},
            careers={"site": accounts.settings_of(user.workspace_id).get("careers_site", "") if user.workspace_id else ""},
        )
        return {**base, "mode": setup.data.get("mode"), "admin_name": setup.data.get("admin_name", ""),
                "admin_email": setup.data.get("admin_email", ""), "steps": steps, "order": first_run.STEPS,
                "optional": sorted(first_run.OPTIONAL)}

    @app.get("/api/setup/status")
    def setup_status(request: Request) -> dict:
        return setup_view(request)

    @app.post("/api/setup/unlock")
    def setup_unlock(body: UnlockBody, request: Request, response: Response) -> dict:
        if setup.completed:
            raise HTTPException(409, "Setup is already complete. Sign in with the access token.")
        if any(h in request.headers for h in ("x-forwarded-for", "forwarded", "cf-connecting-ip", "x-real-ip")):
            raise HTTPException(403, "Run setup on the server's own network, not through the public address.")
        session = setup.unlock(body.code)
        if session is None:
            store.audit("setup_code_rejected")
            raise HTTPException(401, "That isn't the setup code. It's printed in the server log when the app starts.")
        response.set_cookie(first_run.COOKIE, session, httponly=True, samesite="strict", max_age=6 * 3600)
        store.audit("setup_unlocked")
        return {"unlocked": True}

    def setup_open() -> None:
        if setup.completed:
            raise HTTPException(409, "Setup is already complete; change these in Settings.")

    @app.put("/api/setup/state")
    def setup_state(body: SetupStateBody, request: Request) -> dict:
        setup_open()
        values = body.model_dump(exclude_none=True, exclude={"skip", "unskip"})
        skipped = set(setup.data.get("skipped", []))
        if body.skip:
            skipped.add(body.skip)
        if body.unskip:
            skipped.discard(body.unskip)
        setup.update(**values, skipped=sorted(skipped))
        return setup_view(request)

    @app.post("/api/setup/access")
    def setup_access(body: SetupAccessBody, request: Request, response: Response) -> dict:
        setup_open()
        if body.method == "local_only":
            if not is_direct_local(request):
                raise HTTPException(409, "Only possible when the browser runs on the server itself. Create an access token instead.")
            if settings.app_access_token:
                raise HTTPException(409, "An access token is already set; it stays in use.")
            setup.update(access="local_only")
            return {**setup_view(request), "token": None}
        if config.source("app_access_token") == "env":
            raise HTTPException(409, "APP_ACCESS_TOKEN is set in the environment; it stays in use.")
        token = secrets.token_urlsafe(24)
        config.set_access_token(token)
        response.set_cookie(COOKIE, cookie_value(token), httponly=True, samesite="strict", max_age=12 * 3600)
        setup.update(access="token")
        store.audit("access_token_created", detail="first-run setup")
        return {**setup_view(request), "token": token}  # shown once

    @app.post("/api/setup/database")
    async def setup_database(body: SetupDatabaseBody, request: Request) -> dict:
        """The wizard's database choice, connected at once so the first account is created where it will live.
        An empty database is set up; one that already holds Recruence data is used as it is, only when confirmed
        (its own accounts then sign in). Nothing is copied, merged or deleted."""
        setup_open()
        if body.choice == "current":
            if team_mode() and sharing_problem():
                raise HTTPException(409, "A team installation needs a hosted PostgreSQL database that every member's Recruence "
                                         "reaches: the local database is for one person. Paste a connection string instead.")
            setup.data.pop("database", None)
            setup.update()
            return setup_view(request)
        if db_state["source"] == "env":
            raise HTTPException(409, "DATABASE_URL in the environment decides the database.")
        result = await asyncio.to_thread(dbconfig.test, body.url)
        if not result["ok"]:
            raise HTTPException(422, result["message"])
        if result["has_mia_tables"] and body.data != "use_existing":
            raise HTTPException(409, f"{result.get('summary') or 'That database already holds Recruence data.'} Confirm using this existing installation, or choose another database.")
        url = dbconfig.normalise(body.url)
        if describe(url) != describe(store.target):
            switch_database(url, "saved")
            dbconfig.save_profile(settings.data_dir, box, "Setup database", url, by="first-run setup",
                                  database_id=store.database_identity().get("database_id", ""))
        # Connecting creates Recruence's tables in an empty database (Store), or brings an existing installation's schema
        # up to date; nothing existing is dropped, cleared or overwritten.
        setup.update(database={"database": result["database"], "data": "use_existing" if result["has_mia_tables"] else "new"})
        return {**setup_view(request), "has_accounts": accounts_exist(), "database_state": result.get("state", ""),
                "summary": result.get("summary", "")}

    @app.post("/api/setup/existing-account")
    def setup_with_existing_account(request: Request) -> dict:
        """"I already have an account": once the database step has connected a database that has accounts and the
        person has signed in with one of them, this installation uses that database as it is (its settings, AI
        provider and MeetStream come from it), so there is nothing else to set up."""
        setup_open()
        user = getattr(request.state, "user", None)
        if user is None or user.via != "account":
            raise HTTPException(409, "Sign in with your account first")
        # A member someone approved, not just the owner of their own personal workspace (every account has one).
        member = accounts.in_someone_elses_workspace(user.user_id)
        if not (user.install_admin or member):
            raise HTTPException(403, "This account isn't an administrator or a member of any workspace in this database yet. "
                                     "Ask a workspace owner to approve your joining request, then sign in again.")
        setup.update(access="account", admin_email=user.email, admin_name=user.name)
        setup.complete("existing account")
        store.audit("setup_completed", detail="signed in with an existing account")
        return {"completed": True}

    @app.post("/api/setup/complete")
    async def setup_complete(request: Request) -> dict:
        setup_open()
        view = setup_view(request)
        steps = view["steps"]
        missing = [label for key, label in (("database", "Database (a team needs a hosted PostgreSQL database)"),
                                            ("access", "Access"), ("ai", "AI provider")) if steps[key]["status"] != "done"]
        if not view.get("mode"):
            missing.insert(0, "Deployment type")
        if missing:
            raise HTTPException(422, f"Finish these steps first: {', '.join(missing)}")
        if accounts_exist():
            setup.data["access"] = "account"  # accounts live in the database: setup reopens if a restart can't find any
        elif config.source("app_access_token") == "saved":
            setup.data["access"] = "token"  # a token kept in the database: setup reopens if a restart can't find it
        setup.complete()
        store.audit("setup_completed", detail=view.get("mode") or "")
        return {"completed": True, "restart_required": False}

    # ---------- demo data (app/demo.py): sample records to explore with, marked and removable ----------

    @app.get("/api/demo")
    def demo_status() -> dict:
        return demo.status(store)

    @app.post("/api/demo")
    def demo_install() -> dict:
        try:
            written = demo.install(store)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        return {**demo.status(store), "written": written}

    @app.delete("/api/demo")
    def demo_remove() -> dict:
        removed = demo.remove(store)
        store.audit("demo_data_removed", detail=", ".join(f"{v} {k}" for k, v in removed.items()))
        return {**demo.status(store), "removed": removed}

    # ---------- the database connection (admins; used from the next start) ----------

    def database_view() -> dict:
        try:
            saved = dbconfig.saved_url(settings.data_dir, box)
        except dbconfig.DatabaseConfigError as e:
            saved, problem = None, str(e)
        else:
            problem = ""
        # The saved choice differs from the database in use: it applies at the next start.
        restart = db_state["source"] != "env" and ((bool(saved) and describe(saved) != describe(store.target)) or (db_state["source"] == "saved" and not saved))
        return {
            "current": describe(store.target), "backend": store.backend, "source": db_state["source"],
            "saved": describe(saved) if saved else None,
            "restart_required": restart,
            "env_locked": db_state["source"] == "env", "problem": problem,
        }

    @app.get("/api/database")
    def database_status() -> dict:
        return database_view()

    @app.get("/api/database/ownership")
    def database_ownership() -> dict:
        """Who this database belongs to and which installations have used it, for its admins to review. Read only:
        nothing is removed or changed here (Settings → Access → Accounts disables an account)."""
        ident = store.database_identity()
        owner = accounts.user(ident.get("owner_user_id")) if ident.get("owner_user_id") else None
        return {
            "database_id": ident.get("database_id", ""),
            "owner": {"name": owner["name"], "email": owner["email"]} if owner else None,
            "claimed_at": ident.get("claimed_at"),
            "owner_installation_is_this_one": ident.get("owner_install_id") == install_id if ident.get("owner_install_id") else None,
            "installations": [{"label": i["label"], "first_seen": i["first_seen"], "last_seen": i["last_seen"], "this_one": i["install_id"] == install_id}
                              for i in store.installations()],
            "accounts_without_workspace": [{"name": u["name"], "email": u["email"], "created_at": u["created_at"], "admin": bool(u["is_admin"]),
                                            "disabled": bool(u["disabled"])} for u in accounts.without_workspace()],
        }

    def install_admin_only(request: Request) -> None:
        user = getattr(request.state, "user", None)
        if user is None or not user.install_admin:
            raise HTTPException(403, "Only the installation's admins can see this")

    @app.get("/api/database/diagnostics")
    async def database_diagnostics(request: Request) -> dict:
        """For installation admins: which database, whether it answers, its server and schema versions, missing tables.
        Never the connection string or password."""
        install_admin_only(request)
        import sqlalchemy as sa
        from app.store import SCHEMA_VERSION, metadata

        target = store.target
        out = {"backend": store.backend, "database": describe(target), "source": db_state["source"],
               "expected_schema": SCHEMA_VERSION, "last_ok_at": db_state.get("last_ok_at")}
        if db_state["source"] == "saved":
            active = dbconfig.active_profile(settings.data_dir)
            out["profile"] = next((p["name"] for p in dbconfig.profiles(settings.data_dir) if p["id"] == active), None)

        def probe() -> dict:
            with store._engine.connect() as c:
                version = (c.execute(sa.text("SHOW server_version")).scalar() if store.backend == "postgresql"
                           else f"SQLite {c.execute(sa.text('SELECT sqlite_version()')).scalar()}")
                tables = set(sa.inspect(c).get_table_names())
            return {"server_version": version, "schema_version": store.ping(),
                    "missing_tables": sorted({t.name for t in metadata.sorted_tables} - tables)}
        try:
            out |= await asyncio.to_thread(probe)
        except Exception as e:  # noqa: BLE001 - any driver error: shown, without credentials
            text = f"{type(e).__name__}: {e}"
            return {**out, "connected": False, "error": dbconfig._scrub(text, str(target)) if "://" in str(target) else text[:300]}
        db_state["last_ok_at"] = utcnow().isoformat()
        return {**out, "connected": True, "last_ok_at": db_state["last_ok_at"],
                "schema_ok": out["schema_version"] == SCHEMA_VERSION and not out["missing_tables"]}

    @app.get("/api/settings/readiness")
    def interview_readiness(request: Request) -> dict:
        """What an AI interview in a meeting needs, and where each stands (docs/INTERVIEW_SETUP.md). For admins."""
        install_admin_only(request)
        checks = setup.data.get("checks", {})

        def state(configured: bool, check: Optional[dict]) -> str:
            if not configured:
                return "missing"
            if check is None:
                return "unchecked"
            return "ok" if check.get("ok") else "failed"

        ws = getattr(request.state, "user", None)
        ws_notify = accounts.settings_of(ws.workspace_id).get("notify_emails", []) if ws and ws.workspace_id else []
        url_problem = public_url_problem(settings.public_base_url)
        https = url_problem is None
        items = [
            {"key": "ai", "required": True, "tab": "ai", "label": "AI provider",
             "status": state(ai.is_ai, checks.get("ai")) if ai.is_ai else "missing",
             "detail": ai.name if ai.is_ai else "None: template plans only; applications aren't screened and interviews aren't evaluated."},
            {"key": "meetstream", "required": True, "tab": "meetstream", "label": "MeetStream API key",
             "status": state(bool(settings.meetstream_api_key), checks.get("meetstream")),
             "detail": "Set" if settings.meetstream_api_key else "Not set: the AI interviewer can't join meetings."},
            {"key": "public_url", "required": True, "tab": "meetstream", "label": "Public address",
             "status": state(https, checks.get("public_url")),
             "detail": settings.public_base_url if https else (
                 "Not set: MeetStream can't reach this app and candidate links can't be opened." if url_problem == "not set"
                 else f"{settings.public_base_url} {url_problem}.")},
            {"key": "voice", "required": True, "tab": "voice", "label": "Voice agent",
             "status": "manual" if config.voice_view()["ready"] else "missing",
             "detail": (f"{settings.mia_label}. Add the {', '.join(config.voice_view()['meetstream_keys']) or 'provider keys'} in MeetStream → Integrations."
                        if config.voice_view()["ready"] else config.voice_view()["problem"])},
            {"key": "email", "required": False, "tab": "email", "label": "Email",
             "status": state(mailer.enabled, checks.get("email")),
             "detail": f"From {settings.smtp_from}" if mailer.enabled else "Not set: candidates can't get invitations or interview codes."},
            {"key": "notify", "required": False, "tab": "workspace", "label": "Who gets report-ready notes",
             "status": "ok" if (settings.notify_emails or ws_notify) else "missing",
             "detail": ", ".join([*settings.notify_emails, *ws_notify]) or "Nobody: reports appear in the dashboard only."},
        ]
        blocking = [i["label"] for i in items if i["required"] and i["status"] in ("missing", "failed")]
        return {"items": items, "ready": not blocking, "blocking": blocking,
                "desktop": bool(os.environ.get("MIA_DESKTOP_PORT")), "local_port": os.environ.get("MIA_DESKTOP_PORT")}

    @app.post("/api/database/test")
    async def database_test(body: DatabaseBody) -> dict:
        return await asyncio.to_thread(dbconfig.test, body.url)

    @app.put("/api/database")
    async def database_save(body: DatabaseBody, request: Request) -> dict:
        if db_state["source"] == "env":
            raise HTTPException(409, "DATABASE_URL in .env decides the database. Remove it there to choose one here.")
        if dbconfig.saved_url(settings.data_dir, box) and not body.replace:
            raise HTTPException(409, "A database connection is already saved here. Confirm that you want to replace it.")
        result = await asyncio.to_thread(dbconfig.test, body.url)
        if not result["ok"]:
            raise HTTPException(422, result["message"])
        copied = None
        if result["has_data"]:
            if body.data != "use_existing":
                rows = ", ".join(f"{n} {k}" for k, n in result["rows"].items() if n)
                raise HTTPException(409, f"That database already holds Recruence data ({rows}). Confirm using it instead of this install's "
                                         "data, or choose an empty database to copy into.")
        elif body.data == "copy":
            if manager.live:
                raise HTTPException(409, "An interview is live. Copy the data when no interview is running.")
            try:
                copied = await asyncio.to_thread(dbconfig.copy_all, store.target, dbconfig.normalise(body.url))
            except dbconfig.DatabaseConfigError as e:
                raise HTTPException(409, str(e)) from None
        user = getattr(request.state, "user", None)
        dbconfig.save(settings.data_dir, box, dbconfig.normalise(body.url), by=user.name if user else "")
        store.audit("database_connection_saved", detail=f"{result['database']}; data: {body.data}" + (f"; copied {sum(copied.values())} rows" if copied else ""))
        return {**database_view(), "copied": copied}

    @app.delete("/api/database")
    def database_forget() -> dict:
        if not dbconfig.forget(settings.data_dir):
            raise HTTPException(404, "No database connection is saved here")
        store.audit("database_connection_removed")
        return database_view()

    # ---------- recruiting: a role's applications, screening and self-service interview links ----------

    def role_hiring_view(role: Role) -> dict:
        h = role.hiring
        if role.demo:  # its link is a fixed string and never public
            return {"hiring": h.model_dump(mode="json", exclude={"apply_token"}), "careers": None, "open": False,
                    "closed_reason": "Demo role: not taking applications"}
        # Candidates apply on the company's own website, which posts to this role's application API.
        careers = {"job_key": h.apply_token, "api_url": recruiting.apply_api_url(settings, h.apply_token),
                   "allowed_websites": list(settings.public_cors_origins)} if h.apply_token else None
        return {"hiring": h.model_dump(mode="json", exclude={"apply_token"}), "careers": careers,
                "open": recruiting.role_is_open(role) is None, "closed_reason": recruiting.role_is_open(role)}

    @app.get("/api/insights/roles")
    def role_insights() -> list[dict]:
        """For the dashboard: each role's pipeline counts and its top evaluated candidate."""
        out = []
        for role in store.list_roles():
            pipe = roles.pipeline(store, role)
            top = pipe["ranking"]["ranked"][0] if pipe["ranking"]["ranked"] else None
            out.append({"id": role.id, "role_title": role.setup.role_title, "published": role.hiring.published, "demo": role.demo,
                        "open": not role.demo and recruiting.role_is_open(role) is None, "summary": pipe["summary"],
                        "top": {"candidate_name": top["candidate_name"], "score": top["score"]} if top else None})
        return out

    @app.put("/api/roles/{role_id}/hiring")
    async def update_hiring(role_id: str, body: HiringBody) -> dict:
        role = role_or_404(role_id)
        h = role.hiring
        update = body.model_dump(exclude_none=True, exclude={"clear_deadline", "criteria", "options", "meeting_link", "job", "questions"})
        if body.questions is not None:
            update["questions"] = recruiting.assign_question_ids(body.questions)
        # One decision, two fields: resume_review says who decides; auto_invite follows it.
        if body.resume_review is not None:
            update["auto_invite"] = body.resume_review != "manual"
        elif body.auto_invite is not None:
            update["resume_review"] = "auto" if body.auto_invite else "manual"
        if body.job is not None:
            update["job"] = body.job
        if body.criteria is not None:
            update["criteria"] = recruiting.assign_ids(body.criteria)
        if body.clear_deadline:
            update["application_deadline"] = None
        if body.meeting_link is not None:
            link = body.meeting_link.strip()
            if link:
                try:
                    detect_platform(link)
                except UnsupportedMeetingLink as e:
                    raise HTTPException(422, str(e)) from None
            update["meeting_link"] = link
        if body.options is not None:
            merged = {**h.options.model_dump(exclude={"record_video"}), **body.options}
            update["options"] = options_or_422(**{k: v for k, v in merged.items() if k in InterviewOptions.model_fields and k != "record_video"})
        if body.published and role.plan_approved_at is None:
            raise HTTPException(409, "Approve the role's questions before publishing it")
        if body.published and not (update.get("meeting_link", h.meeting_link)):
            raise HTTPException(409, "Set the interview room (a meeting link) before publishing")
        if body.published and not h.published and public_url_problem(settings.public_base_url):
            raise HTTPException(409, "Set the Public URL in Settings → MeetStream before publishing: the invitations sent to "
                                     "applicants carry links on it.")
        if body.published and not h.apply_token:
            update["apply_token"] = recruiting.new_token()
        saved = store.save_role(role.model_copy(update={"hiring": h.model_copy(update=update)}))
        store.audit("role_hiring_updated", detail=f"{role_id}: {', '.join(sorted(update)) or 'no change'}")
        if saved.hiring.published and not h.published:
            for waiting in store.applications_for_role(role_id):
                if waiting.status == "needs_review" and waiting.screening and waiting.screening.screened_by == WAITING:
                    store.save_application(waiting.model_copy(update={"status": "screening", "screening": None}))
                    manager.run_background(process_application(waiting.id), f"screening {waiting.id}")
        if saved.hiring.published and not h.published:
            for waiting in store.applications_for_role(role_id):
                if waiting.status == "needs_review" and waiting.screening and waiting.screening.screened_by == WAITING:
                    store.save_application(waiting.model_copy(update={"status": "screening", "screening": None}))
                    manager.run_background(process_application(waiting.id), f"screening {waiting.id}")
        return {"role": saved.model_dump(mode="json", exclude={"hiring": {"apply_token"}}), **role_hiring_view(saved)}

    @app.get("/api/roles/{role_id}/pipeline")
    def role_pipeline(role_id: str) -> dict:
        role = role_or_404(role_id)
        return {**roles.pipeline(store, role), **role_hiring_view(role)}

    def application_or_404(application_id: str):
        a = store.get_application(application_id)
        if a is None:
            raise HTTPException(404, "Application not found")
        return a

    @app.get("/api/applications/{application_id}")
    def get_application(application_id: str) -> dict:
        a = application_or_404(application_id)
        role = role_or_404(a.role_id)
        return {"application": a.model_dump(mode="json"), "resume_file": store.has_resume_file(a.id),
                "criteria": [c.model_dump() for c in role.hiring.criteria], "role_title": role.setup.role_title}

    @app.get("/api/applications/{application_id}/resume")
    def download_resume(application_id: str) -> Response:
        """The applicant's original resume file, as an attachment (never shown inline: it's the applicant's upload)."""
        a = application_or_404(application_id)
        f = store.resume_file(a.id)
        if f is None:
            raise HTTPException(404, "Only the resume's text was kept for this application")
        store.audit("resume_downloaded", detail=a.id)
        safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in f["filename"]).strip() or "resume"
        return Response(content=bytes(f["data"]), media_type=f["content_type"],
                        headers={"Content-Disposition": f'attachment; filename="{safe}"', "X-Content-Type-Options": "nosniff",
                                 "Cache-Control": "no-store"})

    async def invite_application(role: Role, a, assigned: str = "manual") -> None:
        """Create the candidate's interview and link, and email it. Records why if it can't. `assigned`: "automatic"
        when the role's auto-invite (or an ATS) did it, "manual" when a recruiter did.
        Read again first: screening awaits the AI, and the recruiter may have invited meanwhile. The check and the
        creation below don't await in between, so one invitation wins."""
        current = store.get_application(a.id)
        if current is None or current.interview_id or current.status in ("withdrawn", "invited"):
            return
        a = current
        try:
            interview = recruiting.create_interview(store, role, a, box, assigned=assigned)
        except (ValueError, UnsupportedMeetingLink) as e:
            store.save_application(a.model_copy(update={"status": "eligible", "review_note": f"Not invited: {e}"}))
            store.audit("application_invite_failed", detail=f"{a.id}: {e}")
            return
        store.save_application(a.model_copy(update={"status": "invited", "interview_id": interview.id}))
        store.audit("candidate_invited", interview.id, detail=f"application {a.id}")
        await send_invitation_link(interview)

    async def candidate_link_problem() -> str:
        """Why a link emailed to a candidate now wouldn't work, or "". The links are built on the Public URL; one that is
        missing, malformed or (with CHECK_PUBLIC_URL) doesn't resolve leads nowhere, as a stopped tunnel does."""
        url = settings.public_base_url
        problem = public_url_problem(url)
        if problem == "not set":
            return "No Public URL is set, so the link in the email would lead nowhere. Set it in Settings → MeetStream."
        if problem:
            return f"The Public URL {problem}, so the link in the email wouldn't work. Fix it in Settings → MeetStream."
        if settings.check_public_url and not await public_host_resolves(url):
            return (f"The Public URL ({url}) doesn't resolve right now, so the link in the email wouldn't work. "
                    "Start the tunnel or fix the address in Settings → MeetStream.")
        return ""

    async def send_invitation_link(interview: Interview) -> None:
        """Email the candidate their link: one try now, so the recruiter's Invite or Send returns at once and a failure
        shows as "not delivered" straight away; then the mailer's retries in the background (30 s, 2 min), which clear
        it if one gets through. Retries stop if the link was replaced meanwhile (that sends its own email)."""
        if not (mailer.enabled and interview.setup.candidate_email):
            why = "Email isn't set up" if not mailer.enabled else "No email address"
            store.save_interview(interview.model_copy(update={"invitation": interview.invitation.model_copy(update={"email_error": why})}))
            return
        problem = await candidate_link_problem()
        if problem:  # held, not sent: the interview shows "not delivered" with why, and Send again once it's fixed
            store.save_interview(interview.model_copy(update={"invitation": interview.invitation.model_copy(update={"email_error": problem[:200]})}))
            store.audit("invitation_held", interview.id, detail="the Public URL can't be reached")
            return
        email = recruiting.invitation_email(interview, settings, gateway.link_token(interview, box), branding(interview)["company"])
        if not await deliver_invitation(interview, email) and mailer.retry_delays:
            manager.run_background(retry_invitation(interview, email), f"invitation retry {interview.id}")

    async def deliver_invitation(interview: Interview, email: Email) -> bool:
        """One try; records the outcome on the interview if it still has this link. True when the server accepted it."""
        current = store.get_interview(interview.id)
        if not (current and current.invitation and current.invitation.token_enc == interview.invitation.token_enc):
            return True  # a newer link replaced this one: nothing more to do for it
        try:
            await mailer.send(email)
            inv = current.invitation.model_copy(update={"email_sent_at": utcnow(), "email_error": ""})
            store.audit("invitation_sent", interview.id)
        except MailError as e:
            inv = current.invitation.model_copy(update={"email_error": str(e)[:200]})
        current = store.get_interview(interview.id)
        if current and current.invitation and current.invitation.token_enc == inv.token_enc:  # still the same link
            store.save_interview(current.model_copy(update={"invitation": inv, "invited_at": inv.email_sent_at or current.invited_at}))
        return bool(inv.email_sent_at) and not inv.email_error

    async def retry_invitation(interview: Interview, email: Email) -> None:
        for delay in mailer.retry_delays:
            await asyncio.sleep(delay)
            current = store.get_interview(interview.id)
            if current is None or current.invitation is None or current.invitation.token_enc != interview.invitation.token_enc:
                return  # replaced or gone
            if current.invitation.email_sent_at and not current.invitation.email_error:
                return  # sent meanwhile
            if await deliver_invitation(interview, email):
                return
        logger.warning("[%s] the invitation email still couldn't be sent after retrying", interview.id)

    screening_now: set[str] = set()

    async def process_application(application_id: str) -> None:
        if application_id in screening_now:  # already being screened (e.g. "Screen again" twice): once is enough
            return
        a = store.get_application(application_id)
        role = store.get_role(a.role_id) if a else None
        if a is None or role is None or a.status != "screening":
            return
        screening_now.add(application_id)
        try:
            result = await recruiting.screen(ai, role, a)
        finally:
            screening_now.discard(application_id)
        a = store.get_application(application_id) or a
        if a.interview_id or a.status == "withdrawn":  # invited or withdrawn while the AI was answering: keep that
            store.save_application(a.model_copy(update={"screening": result}))
            return
        a = store.save_application(a.model_copy(update={"screening": result, "status": result.outcome}))
        store.audit("application_screened", detail=f"{a.id}: {result.outcome}")
        if result.outcome == "eligible" and role.hiring.auto_invite:
            await invite_application(role, a, "automatic")

    @app.post("/api/applications/{application_id}/decision")
    async def decide_application(application_id: str, body: ApplicationDecisionBody, request: Request) -> dict:
        a = application_or_404(application_id)
        not_demo(a)
        role = role_or_404(a.role_id)
        user = getattr(request.state, "user", None)
        who = user.name if user else "recruiter"
        if a.interview_id:
            raise HTTPException(409, "This candidate is already invited")
        if body.action == "withdraw":  # the candidate pulled out (they told the hiring team)
            store.save_application(a.model_copy(update={"status": "withdrawn", "reviewed_by": who, "review_note": body.note}))
            store.audit("application_withdrawn", detail=a.id)
        elif body.action == "rescreen":
            a = store.save_application(a.model_copy(update={"status": "screening"}))
            await process_application(a.id)
        elif body.action == "reject":
            store.save_application(a.model_copy(update={"status": "not_eligible", "reviewed_by": who, "review_note": body.note}))
            store.audit("application_rejected", detail=a.id)
        else:
            a = store.save_application(a.model_copy(update={"reviewed_by": who, "review_note": body.note}))
            await invite_application(role, a)
        saved = store.get_application(application_id)
        interview = store.get_interview(saved.interview_id) if saved.interview_id else None
        inv = interview.invitation if interview else None
        # Whether the email with the link went out, so the dashboard doesn't say "Invited" as if the candidate knew.
        return {"application": saved.model_dump(mode="json"),
                "invitation_email": None if inv is None else "sent" if not inv.undelivered and inv.email_sent_at else (inv.email_error or "not sent")}

    @app.post("/api/interviews/{interview_id}/invitation/resend")
    async def resend_invitation(interview_id: str, body: ResendBody) -> dict:
        """A fresh link and deadline (the old link stops working), emailed again. Also reopens an expired or used-up link."""
        interview = interview_or_404(interview_id)
        not_demo(interview)
        if interview.invitation is None:
            raise HTTPException(404, "This interview has no interview link")
        if interview.status == "in_progress" or manager.live_for_interview(interview_id):
            raise HTTPException(409, "The interview is under way")
        if interview.status == "completed" and not body.reopen:
            raise HTTPException(409, "The interview has already taken place; reopen it to allow another attempt")
        role = store.get_role(interview.role_id) if interview.role_id else None
        days = body.days or (role.hiring.invitation_days if role else 5)
        if body.window_start and body.window_end and body.window_end <= body.window_start:
            raise HTTPException(422, "The window must end after it starts")
        fresh, token = recruiting.reissue(interview, days, box, window_start=body.window_start, window_end=body.window_end)
        if interview.status == "completed":
            fresh = fresh.model_copy(update={"status": "ready"})  # the earlier session and report are kept
        saved = store.save_interview(fresh)
        store.set_invite_token(interview_id, token)  # the old link, its codes and its sessions stop working
        store.audit("invitation_reissued", interview_id, detail=f"{days} days" + (", with a time window" if body.window_start or body.window_end else ""))
        await send_invitation_link(saved)
        return {"interview": dashboard_interview(store.get_interview(interview_id)), "url": recruiting.interview_url(settings, token)}

    @app.get("/api/interviews/{interview_id}/invitation")
    def invitation_link(interview_id: str) -> dict:
        interview = interview_or_404(interview_id)
        if interview.invitation is None:
            raise HTTPException(404, "This interview has no interview link")
        token = gateway.link_token(interview, box)
        if not token:
            raise HTTPException(409, "This link can't be shown (it was made with another encryption key). Send a new link.")
        return {"url": recruiting.interview_url(settings, token),
                "invitation": interview.invitation.model_dump(mode="json", exclude={"token", "token_enc"})}

    # Public: the role's application API, for the company's careers website.
    recent_applications: dict[str, list[float]] = {}

    def client_address(request: Request) -> str:
        """Who is asking: the tunnel's or local proxy's CF-Connecting-IP / X-Forwarded-For only when the request came
        from this machine (cloudflared and nginx connect from localhost); a remote client can't choose its own.
        On a cloud backend (REMOTE_DASHBOARD) the proxy runs elsewhere (another container, a load balancer): its
        forwarded address is believed when it's one of TRUSTED_PROXIES, or every candidate would count as the proxy and
        share one rate limit (R-01)."""
        from app.security import REMOTE, remote_client
        if REMOTE["on"]:
            return remote_client(request)
        peer = request.client.host if request.client else "?"
        if peer in ("127.0.0.1", "::1", "localhost", "testclient"):
            forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
            return forwarded or peer
        return peer

    def too_many(request: Request, role_id: str, limit: int = 10, window: float = 3600, role_limit: int = ROLE_APPLICATIONS_PER_HOUR) -> bool:
        """At most `limit` applications an hour from one address, and `role_limit` for one role from everyone: a
        backstop for AI screening costs, whatever addresses a flood comes from."""
        import time
        now = time.monotonic()
        who = client_address(request)
        hits = [t for t in recent_applications.get(who, []) if now - t < window]
        role_hits = [t for t in recent_applications.get(f"role:{role_id}", []) if now - t < window]
        if len(hits) >= limit or len(role_hits) >= role_limit:
            return True
        recent_applications[who] = hits + [now]
        recent_applications[f"role:{role_id}"] = role_hits + [now]
        prune(recent_applications, window)
        return False

    def role_for_apply(token: str) -> Role:
        role = next((r for r in store.list_roles() if same(token, r.hiring.apply_token)), None) \
            if 16 <= len(token) <= 64 else None
        if role is None or role.demo:  # demo roles' links are fixed strings: never public
            raise HTTPException(404, "This job isn't open for applications in Recruence.")
        return role

    @app.get("/api/public/apply/{token}")
    def public_role(token: str) -> dict:
        return recruiting.public_role(role_for_apply(token))

    @app.post("/api/public/apply/{token}", status_code=201)
    async def submit_application(token: str, request: Request, resume: Optional[UploadFile] = File(None)) -> dict:
        role = role_for_apply(token)
        if not request.headers.get("content-length", "").isdigit():
            raise HTTPException(411, "Send the application with its size (Content-Length).")
        if int(request.headers.get("content-length") or 0) > MAX_BYTES + 1024 * 1024:
            raise HTTPException(413, "The application is too large: the resume can be at most 5 MB.")
        form = await request.form()
        fields = {k: str(v).strip() for k, v in form.items() if k != "resume" and isinstance(v, str)}
        if fields.get("consent") != "yes":
            raise HTTPException(422, "Please confirm that you have read how your application is handled")
        return await take_application(role, request, fields, resume)

    # A careers website's own server sends every application to one address for its workspace (Settings → Recruitment
    # sources), naming the job by its title (or job key). Common field names are understood (name, message, linkedin,
    # portfolio), so a site built for another system works as it is. The site shows its own notice and consent box;
    # that it collected consent is recorded with the application.
    CAREERS_ALIASES = {"name": "full_name", "candidate_name": "full_name", "message": "cover_note", "cover_letter": "cover_note",
                       "experience": "years_experience", "title": "current_role"}

    def careers_roles(workspace_id: str) -> list[Role]:
        token = CURRENT_WORKSPACE.set(workspace_id)
        try:
            return [r for r in store.list_roles() if r.hiring.published and r.hiring.apply_token and not r.demo]
        finally:
            CURRENT_WORKSPACE.reset(token)

    def careers_role(workspace_id: str, wanted: str, ref: str = "") -> Role:
        """The published job an application names: by its ref from the job list (exact, whatever the titles), else by
        its job key or title. A ref that isn't one of this workspace's published jobs is refused, never guessed."""
        if ref:
            match = [r for r in careers_roles(workspace_id) if same(ref, r.id)]
            if not match:
                raise HTTPException(404, "No published job with that reference here")
            return match[0]
        match = [r for r in careers_roles(workspace_id)
                 if r.hiring.apply_token == wanted or recruiting.same_title(r.setup.role_title, wanted)]
        if len(match) != 1:
            raise HTTPException(404, f"No published job called {wanted!r} here" if not match else f"More than one published job is called {wanted!r}")
        return match[0]

    @app.get("/api/public/careers/{key}")
    def careers_jobs(key: str) -> dict:
        """The workspace's published jobs, for its careers site to list: what each job's own application page shows
        (title, description, details, requirements, application questions, whether it's taking applications) and a
        `ref` to send back with an application, so it reaches that exact job. Never candidates or job keys. A site
        can then offer only jobs Recruence takes, instead of finding out when an application is refused."""
        workspace_id = accounts.workspace_for_careers_key(key)
        if workspace_id is None:
            raise HTTPException(404, "This careers address isn't valid any more. Ask the hiring team for the current one.")
        jobs = []
        for r in careers_roles(workspace_id):
            closed = recruiting.role_is_open(r)
            page = recruiting.public_role(r)  # the same public view as the job's own application page
            jobs.append({"ref": r.id, "title": r.setup.role_title, "level": r.setup.level, "open": closed is None, "closed_reason": closed,
                         "summary": recruiting.job_summary(r.setup.job_description), "job": r.hiring.job.model_dump(),
                         "deadline": r.hiring.application_deadline.isoformat() if r.hiring.application_deadline else None,
                         "job_description": page["job_description"], "requirements": page["requirements"], "questions": page["questions"]})
        jobs.sort(key=lambda j: j["title"].casefold())
        return {"jobs": jobs}

    @app.post("/api/public/careers/{key}", status_code=201)
    async def careers_application(key: str, request: Request, resume: Optional[UploadFile] = File(None)) -> dict:
        workspace_id = accounts.workspace_for_careers_key(key)
        if workspace_id is None:
            raise HTTPException(404, "This careers address isn't valid any more. Ask the hiring team for the current one.")
        if not request.headers.get("content-length", "").isdigit():
            raise HTTPException(411, "Send the application with its size (Content-Length).")
        if int(request.headers.get("content-length") or 0) > MAX_BYTES + 1024 * 1024:
            raise HTTPException(413, "The application is too large: the resume can be at most 5 MB.")
        form = await request.form()
        fields = {}
        for k, v in form.items():
            if k != "resume" and isinstance(v, str):
                fields.setdefault(CAREERS_ALIASES.get(k, k), str(v).strip())
        links = [fields.pop(k, "") for k in ("linkedin", "portfolio", "github", "website")]
        if not fields.get("links"):
            fields["links"] = " ".join(x for x in links if x)[:500]
        ref = fields.get("job_ref", "")[:64]
        wanted = fields.get("job") or fields.get("role") or fields.get("position") or ""
        if not wanted and not ref:
            raise HTTPException(422, "Say which job: a field called job_ref (from the job list), role (the job's title) or job (its job key)")
        if not ref and wanted and accounts.settings_of(workspace_id).get("careers_new_jobs") == "draft" \
                and not any(r.hiring.apply_token == wanted or recruiting.same_title(r.setup.role_title, wanted) for r in careers_roles(workspace_id)):
            # A job the company's site has and Recruence doesn't: kept, against a draft role, until someone publishes it.
            role = draft_role_for(workspace_id, wanted, fields.get("job_description", ""))
            return await take_application(role, request, fields, resume, per_address=False, consent="collected by the careers website",
                                          waiting=True)
        if not ref and wanted and accounts.settings_of(workspace_id).get("careers_new_jobs") == "draft" \
                and not any(r.hiring.apply_token == wanted or recruiting.same_title(r.setup.role_title, wanted) for r in careers_roles(workspace_id)):
            # A job the company's site has and Recruence doesn't: kept, against a draft role, until someone publishes it.
            role = draft_role_for(workspace_id, wanted, fields.get("job_description", ""))
            return await take_application(role, request, fields, resume, per_address=False, consent="collected by the careers website",
                                          waiting=True)
        role = careers_role(workspace_id, wanted, ref)
        return await take_application(role, request, fields, resume, per_address=False, consent="collected by the careers website")

    WAITING = "waiting for the role to be published"

    def draft_role_for(workspace_id: str, title: str, description: str) -> Role:
        """The unpublished role with this title, or a new draft one from the careers site's job title (and description)."""
        token = CURRENT_WORKSPACE.set(workspace_id)
        try:
            title = " ".join(title.split())[:200]
            existing = [r for r in store.list_roles() if not r.hiring.published and not r.demo and recruiting.same_title(r.setup.role_title, title)]
            if existing:
                return existing[0]
            text = " ".join(description.split()) and description.strip()[:30_000]
            role = store.create_role(RoleSetup(role_title=title, job_description=text or (
                f"Created from your careers website, for applications to \"{title}\". Add the job description and requirements, "
                "approve the questions and publish the role: its applications are screened then.")))
            store.audit("role_created", detail=f"{role.id}: from the careers website")
            return role
        finally:
            CURRENT_WORKSPACE.reset(token)

    WAITING = "waiting for the role to be published"

    def draft_role_for(workspace_id: str, title: str, description: str) -> Role:
        """The unpublished role with this title, or a new draft one from the careers site's job title (and description)."""
        token = CURRENT_WORKSPACE.set(workspace_id)
        try:
            title = " ".join(title.split())[:200]
            existing = [r for r in store.list_roles() if not r.hiring.published and not r.demo and recruiting.same_title(r.setup.role_title, title)]
            if existing:
                return existing[0]
            text = " ".join(description.split()) and description.strip()[:30_000]
            role = store.create_role(RoleSetup(role_title=title, job_description=text or (
                f"Created from your careers website, for applications to \"{title}\". Add the job description and requirements, "
                "approve the questions and publish the role: its applications are screened then.")))
            store.audit("role_created", detail=f"{role.id}: from the careers website")
            return role
        finally:
            CURRENT_WORKSPACE.reset(token)

    async def take_application(role: Role, request: Request, fields: dict, resume: Optional[UploadFile], per_address: bool = True,
                               consent: str = "", waiting: bool = False) -> dict:
        """One application into the role's pipeline: checked, stored, then screened in the background. waiting: the role
        is a draft (created from the careers site); the application is kept and screened once the role is published."""
        closed = None if waiting else recruiting.role_is_open(role)
        if closed:
            raise HTTPException(409, closed)
        if fields.get("years_experience") in ("", None):
            fields.pop("years_experience", None)
        try:
            candidate = ApplicationInput(**{k: v for k, v in fields.items() if k in ApplicationInput.model_fields})
        except ValidationError as e:
            raise HTTPException(422, "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())) from None
        answers = []
        for q in role.hiring.questions:
            answer = fields.get(f"q_{q.id}", "")[:4000]
            if q.required and not answer:
                raise HTTPException(422, f"Please answer: {q.text}")
            answers.append(ApplicationAnswer(question_id=q.id, question=q.text, answer=answer))
        # A careers site's server sends everyone's applications from one address: only the per-role limit applies.
        if too_many(request, role.id, limit=10 if per_address else 10**6):
            raise HTTPException(429, "Too many applications right now. Please try again later.")
        text, name, error, data = "", "", "", b""
        if resume is not None and resume.filename:
            data = await resume.read(MAX_BYTES + 1)
            try:
                extracted = await read_resume(resume.filename, data)
                text, name = extracted.text, resume.filename[:200]
            except ResumeError as e:
                raise HTTPException(422, f"Resume: {e}") from None
        try:
            # Only the company's own website posts here (Recruence has no application page of its own).
            held = {"status": "needs_review", "screening": Screening(
                outcome="needs_review", screened_by=WAITING, screened_at=utcnow(),
                reasons=["This role was created from your careers website. Add its requirements and publish it: the application is screened then."])} if waiting else {}
            a = store.create_application(role.id, candidate, resume_text=text, resume_filename=name, resume_error=error,
                                         answers=answers, source="careers_site", **held)
        except DuplicateApplication as e:
            raise HTTPException(409, str(e)) from None
        if name and data and len(data) <= MAX_BYTES:  # the original file, for the recruiter to open (audit M1)
            store.save_resume_file(a.id, name, resume_content_type(name, data), data)
        store.audit("application_received", detail=f"{role.id}: {a.id}" + (f"; consent {consent}" if consent else "")
                    + ("; waiting for the role" if waiting else ""))  # no name or email
        if not waiting:
            manager.run_background(process_application(a.id), f"screening {a.id}")
        if settings.application_receipt and mailer.enabled and a.candidate.email:
            manager.run_background(send_receipt(role, a), f"application receipt {a.id}")
        return {"received": True, "role_title": role.setup.role_title}

    async def send_receipt(role: Role, a) -> None:
        """The applicant's "we received your application" email (audit #10); a failure is logged, never shown to them."""
        ws = store.role_workspace(role.id)
        company = str((accounts.settings_of(ws) if ws else {}).get("company_name", "")).strip()
        try:
            await mailer.send_retrying(recruiting.application_receipt_email(role, a, settings, company))
            store.audit("application_receipt_sent", detail=a.id)  # no name or address
        except MailError as e:
            logger.warning("application receipt for %s not sent: %s", a.id, e)
            store.audit("application_receipt_failed", detail=a.id)

    # Public: the candidate's own interview link, the interview access gateway (app/gateway.py): a code emailed to the
    # application's address, one verified device, the interview's time window, then the meeting.
    def interview_for_link(token: str) -> Interview:
        interview = store.interview_for_invite(token) if 16 <= len(token) <= 64 else None
        if interview is None or interview.demo or interview.invitation is None:
            raise HTTPException(404, "This interview link isn't valid any more. Please contact the hiring team.")
        return interview

    def scheduled(interview: Interview) -> bool:
        return interview.status == "scheduled" and interview.scheduled_at is not None

    def needs_code(interview: Interview) -> bool:
        ws = store.interview_workspace(interview.id)
        return (accounts.settings_of(ws).get("gateway_verification", "email_code") if ws else "email_code") != "none"

    def gateway_flag(interview: Interview, kind: str, detail: str) -> None:
        current = store.get_interview(interview.id) or interview
        flagged = gateway.flag(current, kind, detail)
        if flagged is not current:
            store.save_interview(flagged)
            store.audit("interview_flagged", interview.id, detail=kind)

    def gateway_view(interview: Interview, token: str, cookie: str, live: bool) -> dict:
        view = recruiting.public_interview(interview, settings, live)
        opens, closes = gateway.window(interview)
        verified = gateway.session_is_current(store, token, cookie)
        brand = branding(interview)
        view.update({
            "company": brand["company"], "contact": brand["contact"] or view["contact"],
            "verification": {"required": needs_code(interview), "verified": verified,
                             "email": gateway.masked(interview.setup.candidate_email), "can_send": mailer.enabled and bool(interview.setup.candidate_email)},
            "opens_at": opens.isoformat() if opens else None, "closes_at": closes.isoformat(),
            "elsewhere": gateway.other_device_active(store, token, cookie),
        })
        if view["state"] == "open" and interview.options.mode == "analyst" and not scheduled(interview):
            view["state"] = "awaiting_time"  # human-led: it opens at the time the hiring team arranges
        elif view["state"] == "open" and opens and utcnow() < opens:
            view["state"] = "not_yet"
        elif view["state"] == "open" and scheduled(interview) and verified:
            # A booked or scheduled interview, inside its window: the bot is started at the time by the scheduler,
            # so the verified device just gets the meeting.
            view["state"], view["meeting_link"] = "ready_to_join", interview.meeting_link
        if not verified:
            view["meeting_link"] = None  # only the verified device ever gets the room
        return view

    @app.get("/i/{token}")
    @app.get("/interview/{token}")
    def interview_page(token: str) -> FileResponse:
        return FileResponse(WEB_DIR / "interview.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/api/public/interview/{token}")
    def public_interview_view(token: str, request: Request) -> dict:
        interview = interview_for_link(token)
        if interview.invitation.opened_at is None:
            interview = store.save_interview(interview.model_copy(update={"invitation": interview.invitation.model_copy(update={"opened_at": utcnow()})}))
            store.audit("invitation_opened", interview.id)
        return gateway_view(interview, token, request.cookies.get(gateway.SESSION_COOKIE, ""), bool(manager.live_for_interview(interview.id)))

    def gateway_limited(request: Request, token: str) -> None:
        if limited_ip(f"gateway:{client_address(request)}", 30, 3600):
            raise HTTPException(429, "Too many attempts from here. Please wait a while and try again.")

    @app.post("/api/public/interview/{token}/code")
    async def send_gateway_code(token: str, request: Request) -> dict:
        """Email a one-time code to the address the candidate applied with."""
        gateway_limited(request, token)
        interview = interview_for_link(token)
        if recruiting.interview_state(interview, bool(manager.live_for_interview(interview.id))) in ("done", "closed", "expired"):
            raise HTTPException(409, "This interview link can't be used any more. Please contact the hiring team.")
        if not (mailer.enabled and interview.setup.candidate_email):
            raise HTTPException(409, "A code can't be sent for this interview. Please contact the hiring team.")
        try:
            code = gateway.send_code(store, token)
        except gateway.GatewayError as e:
            store.audit("gateway_code_limited", interview.id)
            raise HTTPException(e.status, str(e)) from None
        brand = branding(interview)
        who = f"the {brand['company']} hiring team" if brand["company"] else "the hiring team"
        try:
            await mailer.send(Email(to=[interview.setup.candidate_email], subject=f"Your code for the {interview.setup.role_title} interview",  # not the code: subjects show in notifications
                                    body=f"Your code for the {interview.setup.role_title} interview is:\n\n    {code}\n\n"
                                         f"It works for {gateway.CODE_MINUTES} minutes. If you didn't ask for it, you can ignore this email.\n\n"
                                         f"Best regards,\n{who[0].upper()}{who[1:]}\n", reply_to=settings.mail_reply_to))
        except MailError:
            raise HTTPException(503, "The code couldn't be sent right now. Please try again in a moment.") from None
        store.audit("gateway_code_sent", interview.id)  # never the code or the address
        return {"sent": True, "email": gateway.masked(interview.setup.candidate_email), "minutes": gateway.CODE_MINUTES}

    @app.post("/api/public/interview/{token}/verify")
    def verify_gateway_code(token: str, body: CodeBody, request: Request, response: Response) -> dict:
        gateway_limited(request, token)
        interview = interview_for_link(token)
        cookie = request.cookies.get(gateway.SESSION_COOKIE, "")
        if gateway.session_is_current(store, token, cookie):
            return {"verified": True}
        if not gateway.check_code(store, token, body.code):
            store.audit("gateway_code_rejected", interview.id)
            if gateway.too_many_failures(store, token):
                gateway_flag(interview, "code_failures", "Several wrong codes were entered on the candidate's link.")
            raise HTTPException(422, "That code isn't right, or it has expired. Check the latest email, or send a new code.")
        session = gateway.new_session()
        if not gateway.bind(store, token, interview.id, session):
            gateway_flag(interview, "second_device", "The right code was entered on a second device while the interview was open on another one.")
            store.audit("gateway_second_device_blocked", interview.id)
            raise HTTPException(409, "This interview is already open on another device. Continue there, or contact the hiring team.")
        response.set_cookie(gateway.SESSION_COOKIE, session, httponly=True, samesite="lax", secure=request.url.scheme == "https",
                            max_age=24 * 3600, path="/")
        store.audit("gateway_verified", interview.id)
        return {"verified": True}

    @app.post("/api/public/interview/{token}/start")
    async def start_own_interview(token: str, body: StartBody, request: Request, response: Response) -> dict:
        interview = interview_for_link(token)
        cookie = request.cookies.get(gateway.SESSION_COOKIE, "")
        live = bool(manager.live_for_interview(interview.id))
        state = recruiting.interview_state(interview, live)
        verified = gateway.session_is_current(store, token, cookie)
        if state in ("done", "closed", "expired"):  # said plainly, before any code is asked for
            raise HTTPException(409 if state != "expired" else 410, {
                "expired": "This interview link has expired. Please contact the hiring team.",
                "done": "You have already completed this interview. Thank you.",
                "closed": "This interview link can't be used any more. Please contact the hiring team.",
            }[state])
        if needs_code(interview) and not verified:
            if gateway.other_device_active(store, token, cookie):
                gateway_flag(interview, "second_device", "A second device tried to open the interview while it was open on another one.")
                store.audit("gateway_second_device_blocked", interview.id)
                raise HTTPException(409, "This interview is already open on another device. Continue there, or contact the hiring team.")
            raise HTTPException(403, "Enter the code we emailed you first.")
        if state == "in_progress":
            if not verified:  # no code needed here, but the interview stays on the device that started it
                gateway_flag(interview, "second_device", "A second device tried to open the interview while it was under way.")
                raise HTTPException(409, "This interview is already open on another device. Continue there, or contact the hiring team.")
            store.audit("gateway_reconnected", interview.id)
            return gateway_view(interview, token, cookie, True)  # the same device again: the same call
        opens, closes = gateway.window(interview)
        if state == "open" and opens and utcnow() < opens:
            raise HTTPException(409, "Your interview isn't open yet. Please come back at the time in your invitation.")
        if state == "open" and utcnow() >= closes:
            state = "expired"
        if state != "open":
            raise HTTPException(409 if state != "expired" else 410, {
                "expired": "This interview link has expired. Please contact the hiring team.",
                "done": "You have already completed this interview. Thank you.",
                "closed": "This interview link can't be used any more. Please contact the hiring team.",
            }[state])
        if interview.options.mode == "analyst" and not scheduled(interview):
            # Human-led: someone from the hiring team has to be there, so it starts at the time they arrange.
            raise HTTPException(409, "A member of the hiring team leads this interview. They'll email you its time; come back to this link then.")
        if not body.acknowledged:
            raise HTTPException(422, "Please confirm that you have read how the AI interview works")
        if scheduled(interview):
            # Booked or scheduled: nothing is started here (the scheduler starts the bot at the time); this device
            # becomes the interview's device and gets the meeting.
            if not verified:
                cookie = gateway.new_session()
                if not gateway.bind(store, token, interview.id, cookie):
                    gateway_flag(interview, "second_device", "A second device tried to open the scheduled interview.")
                    raise HTTPException(409, "This interview is already open on another device. Continue there, or contact the hiring team.")
                response.set_cookie(gateway.SESSION_COOKIE, cookie, httponly=True, samesite="lax", secure=request.url.scheme == "https",
                                    max_age=24 * 3600, path="/")
            store.audit("candidate_joined_scheduled", interview.id)
            return {**gateway_view(interview, token, cookie, live), "state": "ready_to_join", "meeting_link": interview.meeting_link}
        if not verified:  # verification switched off: the device that starts is the interview's device
            cookie = gateway.new_session()
            if not gateway.bind(store, token, interview.id, cookie):
                gateway_flag(interview, "second_device", "A second device tried to start the interview.")
                raise HTTPException(409, "This interview is already open on another device. Continue there, or contact the hiring team.")
            response.set_cookie(gateway.SESSION_COOKIE, cookie, httponly=True, samesite="lax", secure=request.url.scheme == "https",
                                max_age=24 * 3600, path="/")
        if manager.room_busy(interview.meeting_link):
            raise HTTPException(409, "Another interview is using the interview room right now. Please try again in about 15 minutes.")
        now = utcnow()
        inv = interview.invitation.model_copy(update={"started_at": now, "attempts": interview.invitation.attempts + 1})
        store.save_interview(interview.model_copy(update={"invitation": inv, "consent_attested_at": now,
                                                          "status": "ready" if interview.status == "failed" else interview.status}))
        store.audit("consent_attested", interview.id, detail="notice confirmed by the candidate on their interview link")
        try:
            await manager.launch(interview.id)
        except LaunchError as e:
            # Not the candidate's fault: give the attempt back and say so.
            cur = store.get_interview(interview.id)
            store.save_interview(cur.model_copy(update={"invitation": cur.invitation.model_copy(update={"started_at": None, "attempts": inv.attempts - 1})}))
            store.audit("candidate_start_failed", interview.id, detail=str(e)[:200])
            raise HTTPException(503, "The interview couldn't start right now. Please try again in a few minutes, or contact the hiring team.") from None
        store.audit("interview_started_by_candidate", interview.id)
        return gateway_view(store.get_interview(interview.id), token, cookie, True)

    @app.post("/api/interviews/{interview_id}/flags/resolve")
    def resolve_flags(interview_id: str, request: Request) -> dict:
        """The hiring team has looked at the link's flags (they stay on record, marked reviewed)."""
        interview = interview_or_404(interview_id)
        user = getattr(request.state, "user", None)
        now = utcnow()
        flags = [f if f.resolved_at else f.model_copy(update={"resolved_at": now, "resolved_by": (user.name if user else "recruiter")[:100]})
                 for f in interview.flags]
        saved = store.save_interview(interview.model_copy(update={"flags": flags}))
        store.audit("interview_flags_reviewed", interview_id)
        return {"flags": [f.model_dump(mode="json") for f in saved.flags]}

    @app.delete("/api/roles/{role_id}")
    def delete_role(role_id: str) -> dict:
        role_or_404(role_id)
        if store.interviews_for_role(role_id):
            raise HTTPException(409, "Delete this role's candidate interviews first")
        store.delete_role(role_id)
        store.audit("role_deleted", detail=role_id)
        return {"deleted": role_id}

    @app.get("/api/calendar/events")
    async def calendar_events() -> dict:
        """Upcoming meetings from the calendar connected in MeetStream (read only), to fill in a launch."""
        ready, why = settings.meetstream_ready
        if not ready:
            raise HTTPException(503, why)
        try:
            data = await manager.client().calendar.events()
        except meetstream.MeetStreamError as e:
            raise HTTPException(502, f"Couldn't read the calendar connected in MeetStream: {e}") from None
        return {"events": upcoming_meetings(data, utcnow()), "more": bool(isinstance(data, dict) and data.get("has_more")),
                "stale": staleness(data, utcnow()), "problems": sync_problems(data)}

    @app.get("/api/calendar/status")
    def calendar_status() -> dict:
        return {"sync": store.get_state("calendar_sync")}

    @app.post("/api/calendar/resync")
    async def calendar_resync() -> dict:
        """Ask MeetStream to re-read the connected calendar now (sync=true), then return the fresh events.

        MeetStream only reads the calendar (it never writes events), so a resync can't duplicate anything.
        """
        ready, why = settings.meetstream_ready
        if not ready:
            raise HTTPException(503, why)
        at = utcnow().isoformat()
        try:
            data = await manager.client().calendar.events(params={"sync": "true"})
        except meetstream.MeetStreamError as e:
            status = {"at": at, "ok": False, "message": f"MeetStream couldn't sync the calendar: {e}"}
            last = store.get_state("calendar_sync") or {}
            status["last_ok_at"] = last.get("at") if last.get("ok") else last.get("last_ok_at")
            store.set_state("calendar_sync", status)
            store.audit("calendar_resync_failed")
            raise HTTPException(502, status["message"]) from None
        events = upcoming_meetings(data, utcnow())
        # MeetStream answered, but its copy may have stopped changing (R-10): kept with the status, so the dashboard
        # says so next to "Last resynced" instead of implying the meetings are current.
        # MeetStream also says which connected calendars it couldn't read (R-14): kept with the status too.
        problems = sync_problems(data)
        status = {"at": at, "ok": True, "events": len(events), "last_ok_at": at, "stale": staleness(data, utcnow()),
                  "problems": problems}
        store.set_state("calendar_sync", status)
        store.audit("calendar_resynced", detail=f"{len(events)} upcoming meetings" + (", copy not changed since "
                    + status["stale"]["since"][:10] if status["stale"] else "")
                    + (f", {len(problems)} calendar(s) MeetStream couldn't read ({', '.join(sorted({p['provider'] for p in problems}))})"
                       if problems else ""))  # the provider only: the account stays out of the audit log
        return {"sync": status, "events": events, "more": bool(isinstance(data, dict) and data.get("has_more")),
                "stale": status["stale"], "problems": problems}

    @app.post("/api/interviews/{interview_id}/launch")
    async def launch(interview_id: str, body: LaunchBody) -> dict:
        interview = interview_or_404(interview_id)
        editable(interview)
        not_cancelled(interview)
        if not body.consent_attested:
            raise HTTPException(422, "Confirm that the candidate was told about the AI interview, recording and transcription")
        try:
            platform = detect_platform(body.meeting_link)
        except UnsupportedMeetingLink as e:
            raise HTTPException(422, str(e)) from None
        if interview.plan_approved_at is None:
            raise HTTPException(409, "Approve the interview plan first")
        ready, why = settings.meetstream_ready
        if not ready:
            raise HTTPException(409, f"Live meetings are not configured: {why}")
        saved = interview.options
        options = options_or_422(
            mode=body.mode, candidate_meeting_name=body.candidate_meeting_name.strip(),
            questions_in_chat=(saved.questions_in_chat if body.questions_in_chat is None else body.questions_in_chat) and body.mode == "ai",
            format=body.format or ("video" if body.record_video else saved.format),
            require_camera=saved.require_camera if body.require_camera is None else body.require_camera,
            require_screen_share=saved.require_screen_share if body.require_screen_share is None else body.require_screen_share,
        )

        when = body.scheduled_at
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        future = when is not None and when > utcnow() + timedelta(seconds=30)
        if future and bookings.clashes(when, interview.setup.duration_minutes, room_bookings(body.meeting_link, interview.id)):
            raise HTTPException(409, "Another interview is booked in this meeting room at that time. Choose another time, "
                                     "or use another meeting link for this candidate.")
        interview = store.save_interview(
            interview.model_copy(
                update={
                    "meeting_link": body.meeting_link.strip(),
                    "platform": platform.name,
                    "consent_attested_at": utcnow(),
                    "options": options,
                    "scheduled_at": when if future else None,
                    "status": "scheduled" if future else interview.status,
                }
            )
        )
        store.audit("consent_attested", interview_id, detail="meeting analyst" if body.mode == "analyst" else "")
        if future:
            store.audit("interview_scheduled", interview_id, detail=f"{platform.key} at {when.isoformat()}")
            return {"scheduled_at": when.isoformat(), "session_id": None}
        try:
            session_id = await manager.launch(interview_id)
        except LaunchError as e:
            raise HTTPException(409, str(e)) from None
        return {"scheduled_at": None, "session_id": session_id}

    def options_or_422(**fields: Any) -> InterviewOptions:
        try:
            return InterviewOptions(**fields)
        except ValidationError as e:
            raise HTTPException(422, "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())) from None

    @app.put("/api/interviews/{interview_id}/options")
    def update_options(interview_id: str, body: dict) -> dict:
        """How the interview runs, set before launch: it shapes the invitation, the booking page and a scheduled start."""
        interview = interview_or_404(interview_id)
        editable(interview)
        allowed = set(InterviewOptions.model_fields) - {"record_video"}
        unknown = set(body) - allowed
        if unknown:
            raise HTTPException(422, f"Unknown options: {', '.join(sorted(unknown))}")
        merged = {**interview.options.model_dump(exclude={"record_video"}), **body}
        if merged.get("mode") == "analyst":
            merged["questions_in_chat"] = False
        options = options_or_422(**merged)
        saved = store.save_interview(interview.model_copy(update={"options": options}))
        store.audit("options_updated", interview_id, detail=f"{options.mode}, {options.format}")
        return saved.model_dump(mode="json")

    @app.post("/api/interviews/{interview_id}/unschedule")
    def unschedule(interview_id: str) -> dict:
        interview = interview_or_404(interview_id)
        if interview.status != "scheduled":
            raise HTTPException(409, "The interview is not scheduled")
        saved = store.save_interview(interview.model_copy(update={"status": "ready", "scheduled_at": None}))
        store.audit("interview_unscheduled", interview_id)
        return saved.model_dump(mode="json")

    @app.post("/api/interviews/{interview_id}/cancel")
    async def cancel_interview(interview_id: str, body: CancelBody) -> dict:
        """Stops the interview going ahead: unschedules it and withdraws its self-scheduling link. It can be reopened."""
        interview = interview_or_404(interview_id)
        why = lifecycle.can_cancel(interview)
        if why:
            raise HTTPException(409, why)
        update: dict = {"cancelled_at": utcnow(), "booking": None}
        if interview.status == "scheduled":
            update.update(status="ready", scheduled_at=None)
            # The interview link made for that time stops working too (reopening doesn't bring it back).
            update["invitation"] = None
            store.set_invite_token(interview_id, None)
        saved = store.save_interview(interview.model_copy(update=update))
        if interview.booking:
            store.set_booking_token(interview_id, None)
        notify = body.notify_candidate and bool(interview.setup.candidate_email)
        store.audit("interview_cancelled", interview_id, detail="candidate notified" if notify else "")
        if notify:
            manager.run_background(mail_quietly(cancellation(saved, settings), "cancellation"), f"cancellation mail {interview_id}")
        return saved.model_dump(mode="json")

    @app.post("/api/interviews/{interview_id}/reopen")
    def reopen_interview(interview_id: str) -> dict:
        interview = interview_or_404(interview_id)
        if not interview.cancelled_at:
            raise HTTPException(409, "The interview isn't cancelled")
        saved = store.save_interview(interview.model_copy(update={"cancelled_at": None}))
        store.audit("interview_reopened", interview_id)
        return saved.model_dump(mode="json")

    # ---------- self-scheduling: the recruiter offers times, the candidate picks one ----------

    @app.post("/api/interviews/{interview_id}/booking")
    def create_booking(interview_id: str, body: BookingBody) -> dict:
        interview = interview_or_404(interview_id)
        editable(interview)
        not_cancelled(interview)
        if interview.status == "completed":
            raise HTTPException(409, "This interview is completed")
        if interview.plan_approved_at is None:
            raise HTTPException(409, "Approve the interview plan first")
        ready, why = settings.meetstream_ready
        if not ready:
            raise HTTPException(409, f"Live meetings are not configured: {why}")
        try:
            platform = detect_platform(body.meeting_link)
            slots = bookings.clean_slots(body.slots)
            expires = bookings.clean_expiry(body.expires_at, slots)
        except UnsupportedMeetingLink as e:
            raise HTTPException(422, str(e)) from None
        except bookings.BookingError as e:
            raise HTTPException(e.status, str(e)) from None
        b = Booking(token=bookings.new_token(), meeting_link=body.meeting_link.strip(), platform=platform.name,
                    slots=slots, created_at=utcnow(), expires_at=expires)
        saved = store.save_interview(interview.model_copy(update={"booking": b}))
        store.set_booking_token(interview_id, b.token)
        store.audit("booking_link_created", interview_id, detail=f"{len(slots)} times on {platform.key}")
        return {"interview": saved.model_dump(mode="json"), "url": bookings.booking_url(settings, b.token)}

    @app.delete("/api/interviews/{interview_id}/booking")
    def withdraw_booking(interview_id: str) -> dict:
        interview = interview_or_404(interview_id)
        if interview.booking is None:
            raise HTTPException(404, "There is no self-scheduling link")
        # A time the candidate already booked stays booked; unschedule it separately.
        saved = store.save_interview(interview.model_copy(update={"booking": None}))
        store.set_booking_token(interview_id, None)
        store.audit("booking_link_withdrawn", interview_id)
        return saved.model_dump(mode="json")

    def booking_or_404(token: str) -> Interview:
        interview = store.interview_for_booking(token) if 16 <= len(token) <= 64 else None
        if interview is None or not bookings.is_live(interview, token):
            raise HTTPException(404, "This booking link isn't valid any more. Please contact the hiring team.")
        return interview

    async def mail_quietly(email: Email, what: str) -> None:
        """For background sends: retried (Mailer.send_retrying), and a final failure is logged, never raised."""
        if not (mailer.enabled and email.to):
            return
        try:
            await mailer.send_retrying(email)
        except MailError as e:
            logger.warning("couldn't send the %s: %s", what, e)

    async def notify_expired_invitations(now: Optional[datetime] = None) -> list[str]:
        """Tell the hiring team once about each invitation that expired unused: NOTIFY_EMAILS and the interview's
        workspace's list, like report notes. Hourly, and at start."""
        if not mailer.enabled:
            return []
        told = []
        for row in store.list_interviews():
            interview = store.get_interview(row["id"])
            if interview is None or not recruiting.expired_unused(interview, now):
                continue
            note = recruiting.expiry_note(interview, settings)
            ws = store.interview_workspace(interview.id)
            to = list(dict.fromkeys([*note.to, *(accounts.settings_of(ws).get("notify_emails", []) if ws else [])]))
            if not to:
                continue
            try:
                await mailer.send_retrying(Email(to=to, subject=note.subject, body=note.body))
            except MailError as e:
                logger.warning("couldn't send the expiry note for %s: %s", interview.id, e)
                continue
            inv = interview.invitation.model_copy(update={"expiry_notified_at": utcnow()})
            store.save_interview(interview.model_copy(update={"invitation": inv}))
            store.audit("invitation_expiry_notified", interview.id)
            told.append(interview.id)
        return told

    app.state.notify_expired_invitations = notify_expired_invitations

    async def ats_retry_loop() -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await integrations_svc.retry_failed()
            except Exception:
                logger.exception("resending ATS results failed")

    async def expiry_loop() -> None:
        while True:
            try:
                await notify_expired_invitations()
            except Exception:
                logger.exception("expiry notes failed")
            await asyncio.sleep(3600)

    def room_bookings(meeting_link: str, exclude_id: str) -> list[tuple[datetime, datetime]]:
        """When other interviews are booked in this meeting room, each with the gap kept after it."""
        room = room_key(meeting_link)
        if not room:
            return []
        return [(bookings.utc(i.scheduled_at), bookings.utc(i.scheduled_at) + timedelta(minutes=i.setup.duration_minutes) + bookings.ROOM_BUFFER)
                for i in store.scheduled_interviews() if i.id != exclude_id and room_key(i.meeting_link) == room]

    @app.get("/api/public/booking/{token}")
    def public_booking(token: str) -> dict:
        interview = booking_or_404(token)
        view = bookings.public_view(interview, settings, busy=room_bookings(interview.booking.meeting_link, interview.id))
        if view["state"] == "booked":
            token_ = gateway.link_token(interview, box) if interview.invitation else ""
            view["interview_link"] = recruiting.interview_url(settings, token_) if token_ else None
        return view

    @app.post("/api/public/booking/{token}")
    async def candidate_books(token: str, body: BookBody) -> dict:
        interview = booking_or_404(token)
        changing = bookings.booked(interview)
        try:
            updated = bookings.book(interview, token, body.slot, body.acknowledged,
                                    busy=room_bookings(interview.booking.meeting_link, interview.id))
        except bookings.BookingError as e:
            raise HTTPException(e.status, str(e)) from None
        saved = store.save_interview(updated)
        store.audit("booked_by_candidate", saved.id, detail=f"{'changed to ' if changing else ''}{saved.scheduled_at.isoformat()}")
        store.audit("consent_attested", saved.id, detail="notice confirmed by the candidate on the booking page")
        saved, link = ensure_link(saved)
        if changing:  # the window follows the new time; the same link
            saved = store.save_interview(saved.model_copy(update={"invitation": saved.invitation.model_copy(update={
                "expires_at": max(recruiting.utc(saved.invitation.expires_at),
                                  recruiting.utc(saved.scheduled_at) + timedelta(minutes=saved.setup.duration_minutes) + gateway.WINDOW_AFTER)})}))
        join = recruiting.interview_url(settings, link)
        manager.run_background(mail_quietly(bookings.confirmation(saved, settings, join, branding(saved)["company"]), "booking confirmation"),
                               f"booking mail {saved.id}")
        manager.run_background(mail_quietly(bookings.team_note(saved, settings), "booking note"), f"booking note {saved.id}")
        return {**bookings.public_view(saved, settings, busy=room_bookings(saved.booking.meeting_link, saved.id)), "interview_link": join}

    @app.post("/api/public/booking/{token}/cancel")
    async def candidate_cancels(token: str) -> dict:
        interview = booking_or_404(token)
        try:
            updated = bookings.cancel(interview, token)
        except bookings.BookingError as e:
            raise HTTPException(e.status, str(e)) from None
        # The interview link made for that time stops working (a new booking makes a new one).
        store.set_invite_token(updated.id, None)
        saved = store.save_interview(updated.model_copy(update={"invitation": None}))
        store.audit("booking_cancelled_by_candidate", saved.id)
        manager.run_background(mail_quietly(bookings.team_note(interview, settings, cancelled=True), "booking note"), f"booking note {saved.id}")
        return bookings.public_view(saved, settings, busy=room_bookings(saved.booking.meeting_link, saved.id))

    @app.get("/api/public/booking/{token}/calendar.ics")
    def booking_calendar(token: str) -> Response:
        interview = booking_or_404(token)
        if not bookings.booked(interview):
            raise HTTPException(404, "No time is booked yet")
        token_ = gateway.link_token(interview, box) if interview.invitation else ""
        return Response(bookings.ics(interview, settings, recruiting.interview_url(settings, token_) if token_ else ""), media_type="text/calendar",
                        headers={"Content-Disposition": 'attachment; filename="interview.ics"'})

    @app.get("/book/{token}")
    def booking_page(token: str) -> FileResponse:
        return FileResponse(WEB_DIR / "book.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    # ---------- the candidate's invitation email ----------

    def invitation_for(interview: Interview, body: InviteBody, preview: bool = False) -> Email:
        """The candidate's invitation. A preview changes nothing (no link or meeting is made for it)."""
        not_cancelled(interview)
        if interview.booking and interview.status != "scheduled":
            # An open self-scheduling link: the candidate chooses the time, so the email carries the link.
            if bookings.expired(interview):
                raise HTTPException(409, "The self-scheduling link has expired; offer new times first")
            return invitation(interview, settings, "", "", booking_link=bookings.booking_url(settings, interview.booking.token),
                              expires_text=bookings.when_text(bookings.deadline(interview.booking), settings.time_zone))
        link = body.meeting_link.strip() or interview.meeting_link
        if not link:
            raise HTTPException(422, "Add the meeting link first")
        try:
            detect_platform(link)
        except UnsupportedMeetingLink as e:
            raise HTTPException(422, str(e)) from None
        if preview:
            token = gateway.link_token(interview, box) if interview.invitation else ""
            shown = recruiting.interview_url(settings, token) if token else f"{settings.public_base_url.rstrip('/')}/i/(the candidate's own link)"
            return invitation(interview, settings, shown, body.when_text.strip(), company=branding(interview)["company"])
        if link != interview.meeting_link:  # typed in the invitation dialog: this interview's own meeting
            made = meetings.ManualMeetings.check(link)
            store.put_meeting(interview.id, mode="manual", provider="manual", platform=made.platform, join_url_enc=box.encrypt(made.join_url),
                              external_id="", starts_at=interview.scheduled_at.isoformat() if interview.scheduled_at else None, ends_at=None,
                              status="scheduled", created_by="invitation")
            interview = store.save_interview(interview.model_copy(update={"meeting_link": made.join_url, "platform": made.platform}))
        interview, token = ensure_link(interview)
        return invitation(interview, settings, recruiting.interview_url(settings, token), body.when_text.strip(),
                          company=branding(interview)["company"])

    @app.post("/api/interviews/{interview_id}/invite/preview")
    def preview_invitation(interview_id: str, body: InviteBody) -> dict:
        email = invitation_for(interview_or_404(interview_id), body, preview=True)
        return {"to": email.to, "subject": email.subject, "body": email.body, "can_send": mailer.enabled and bool(email.to)}

    @app.put("/api/interviews/{interview_id}/meeting")
    def set_meeting(interview_id: str, body: MeetingBody, request: Request) -> dict:
        """This interview's own meeting (a room for this candidate only), made by the recruiter. Candidates who already
        have their interview link don't need a new email: the link shows the new meeting."""
        interview = interview_or_404(interview_id)
        not_demo(interview)
        if interview.status == "in_progress" or manager.live_for_interview(interview_id):
            raise HTTPException(409, "The interview is under way: change the meeting after it ends")
        try:
            made = meetings.ManualMeetings.check(body.url)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        user = getattr(request.state, "user", None)
        store.put_meeting(interview_id, mode="manual", provider="manual", platform=made.platform, join_url_enc=box.encrypt(made.join_url),
                          external_id="", starts_at=interview.scheduled_at.isoformat() if interview.scheduled_at else None,
                          ends_at=None, status="scheduled", created_by=(user.name if user else "recruiter")[:100])
        saved = store.save_interview(interview.model_copy(update={"meeting_link": made.join_url, "platform": made.platform}))
        store.audit("meeting_set", interview_id, detail=made.platform)  # never the link
        return {"interview": dashboard_interview(saved), "meeting": meeting_view(saved)}

    @app.delete("/api/interviews/{interview_id}/meeting")
    def clear_meeting(interview_id: str) -> dict:
        """Back to the role's shared room (or no room, for an interview without a role)."""
        interview = interview_or_404(interview_id)
        not_demo(interview)
        if interview.status == "in_progress" or manager.live_for_interview(interview_id):
            raise HTTPException(409, "The interview is under way: change the meeting after it ends")
        role = store.get_role(interview.role_id) if interview.role_id else None
        link = role.hiring.meeting_link if role else ""
        platform = detect_platform(link).name if link else ""
        store.delete_meeting(interview_id)
        saved = store.save_interview(interview.model_copy(update={"meeting_link": link, "platform": platform}))
        store.audit("meeting_cleared", interview_id)
        return {"interview": dashboard_interview(saved), "meeting": meeting_view(saved)}

    @app.get("/api/meetings/providers")
    def meeting_providers() -> dict:
        return {"providers": [{"id": p.id, "label": p.label, "capabilities": p.capabilities()} for p in meetings.PROVIDERS.values()],
                "capture": {name: {"level": lvl, "note": note} for name, (lvl, note) in meetings.CAPTURE.items()}}

    @app.post("/api/interviews/{interview_id}/invite")
    async def send_invitation(interview_id: str, body: InviteBody) -> dict:
        interview = interview_or_404(interview_id)
        not_demo(interview)
        email = invitation_for(interview, body)
        if not email.to:
            raise HTTPException(422, "Add the candidate's email address first")
        problem = await candidate_link_problem() if interview.invitation else ""
        if problem:
            raise HTTPException(409, problem)
        try:
            await mailer.send(email)
        except MailError as e:
            raise HTTPException(502 if mailer.enabled else 409, str(e)) from None
        now = utcnow()
        update: dict = {"invited_at": now}
        if interview.invitation:  # an earlier failure is over: this email went out
            update["invitation"] = interview.invitation.model_copy(update={"email_sent_at": now, "email_error": ""})
        store.save_interview(interview.model_copy(update=update))
        store.audit("invitation_sent", interview_id)  # no address or content in the audit log
        return {"sent": True, "to": email.to}

    @app.post("/api/interviews/{interview_id}/decision")
    def decide(interview_id: str, body: DecisionBody) -> dict:
        interview = interview_or_404(interview_id)
        if interview.status != "completed":
            raise HTTPException(409, "Record a decision after the interview is completed")
        saved = store.save_interview(
            interview.model_copy(update={"decision": body.decision, "decision_note": body.note.strip(), "decided_at": utcnow()})
        )
        store.audit("decision_recorded", interview_id, detail=body.decision)
        if exporter.enabled:
            # This sync route runs in a worker thread; the export task belongs on the event loop.
            anyio.from_thread.run_sync(exporter.decision_recorded, saved)
        if saved.application_id:
            anyio.from_thread.run_sync(integrations_svc.decision_recorded, saved)
        return saved.model_dump(mode="json")

    @app.delete("/api/interviews/{interview_id}")
    async def delete_interview(interview_id: str) -> dict:
        interview_or_404(interview_id)
        try:
            return await manager.delete_interview(interview_id)
        except LaunchError as e:
            raise HTTPException(409, str(e)) from None

    # ---------- sessions ----------

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        row = store.get_session(session_id)
        if row is None:
            raise HTTPException(404, "Session not found")
        live = manager.live.get(session_id)
        report = store.get_report(session_id)
        return {
            "session": row,
            "live": live is not None,
            "snapshot": live.runner.snapshot() if live else None,
            "interview_id": row["interview_id"],
            "transcript": [u.model_dump(mode="json") for u in store.transcript(session_id)],
            "notes": [n.model_dump() for n in store.notes(session_id)],
            "report": report.model_dump(mode="json") if report else None,
        }

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, request: Request) -> StreamingResponse:
        live = manager.live.get(session_id)
        if live is None:
            raise HTTPException(404, "This session is not live")
        bus = live.bus

        async def stream():
            q = bus.subscribe()
            try:
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"id: {event['seq']}\ndata: {json.dumps(event, default=str)}\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/sessions/{session_id}/end", status_code=202)
    async def end_session(session_id: str) -> dict:
        if not await manager.end(session_id):
            raise HTTPException(409, "This session is not live")
        return {"accepted": True}

    evaluating_now: set[str] = set()

    @app.post("/api/sessions/{session_id}/evaluate")
    async def reevaluate(session_id: str) -> dict:
        """Run the evaluation again on a finished session's stored transcript (e.g. after adding an LLM key)."""
        row = store.get_session(session_id)
        report = store.get_report(session_id)
        if row is None or report is None:
            raise HTTPException(404, "No finished session with a report")
        if session_id in manager.live:
            raise HTTPException(409, "The session is still live")
        if not report.consent_given:
            raise HTTPException(409, "The candidate did not consent, so the interview is not evaluated")
        interview = interview_or_404(row["interview_id"])
        try:
            ctx = context_for(interview)
        except PlanError as e:
            raise HTTPException(409, str(e)) from None
        asked = list(dict.fromkeys(u.question_id for u in report.transcript if u.question_id))
        if any(qid not in {q.id for q in ctx.questions} for qid in asked):
            raise HTTPException(409, "The interview plan has changed since this session, so it can't be re-evaluated against it")
        if session_id in evaluating_now:
            raise HTTPException(409, "This interview is being evaluated already; the report updates when it's done")
        evaluating_now.add(session_id)
        try:
            evaluation, error = await evaluate_interview(ai, EvaluationInput(ctx, report.transcript, report.notes, asked))
        finally:
            evaluating_now.discard(session_id)
        if error:
            raise HTTPException(502, f"Evaluation failed: {error}")
        updated = report.model_copy(update={
            "evaluation": evaluation,
            "evaluation_error": None,
            "generated_by": f"{ai.served_name} (re-evaluation); " + report.generated_by.split("; ", 1)[-1],
        })
        store.push_report(updated)
        exporter.report_saved(updated)
        integrations_svc.result_ready(updated)
        store.audit("session_reevaluated", row["interview_id"], session_id, ai.served_name)
        return updated.model_dump(mode="json")

    @app.get("/api/sessions/{session_id}/report")
    def get_report(session_id: str) -> dict:
        report = store.get_report(session_id)
        if report is None:
            raise HTTPException(404, "No report for this session")
        return report.model_dump(mode="json")

    @app.get("/api/audit")
    def audit(limit: int = 200) -> list[dict]:
        return store.audit_log(max(1, min(limit, 1000)))

    # ---------- settings: what's configured (never secret values), and checks ----------

    def config_changed(keys: set[str]) -> None:
        if keys & AI_KEYS:
            ai.swap(build_ai(settings))
            logger.info("app AI is now %s", ai.name)
            setup.forget_check("ai")  # an earlier test was of other settings, so it no longer describes the saved provider
        if "meetstream_api_key" in keys:
            manager.reset_client()

    config.on_change = config_changed

    @app.get("/api/config")
    def config_view() -> dict:
        return {**config.view(), "database": describe(store.target)}

    @app.put("/api/config")
    def config_update(body: ConfigBody) -> dict:
        try:
            result = config.update(body.changes)
        except ConfigError as e:
            raise HTTPException(422, {"message": "Some settings aren't valid", "errors": e.errors}) from None
        store.audit("settings_changed", detail=", ".join(sorted(body.changes)))  # names only, never values
        return {**result, **config.view()}

    @app.delete("/api/config/{key}")
    def config_reset(key: str) -> dict:
        try:
            result = config.reset(key)
        except ConfigError as e:
            raise HTTPException(404, e.errors[key]) from None
        store.audit("setting_reset", detail=key)
        return {**result, **config.view()}

    @app.post("/api/config/test-ai")
    async def config_test_ai(body: ProviderTestBody) -> dict:
        saved = {"gemini": settings.gemini_api_key, "claude": settings.anthropic_api_key, "openai": settings.openai_api_key,
                 "xai": settings.xai_api_key, "ollama": ""}
        base = body.base_url or (settings.openai_base_url if body.provider == "openai" else settings.ollama_base_url if body.provider == "ollama" else "")
        # Gemini: the interview's conversation model is checked too when it's a Google model on the same key.
        voice = (settings.mia_llm_model,) if body.provider == "gemini" and settings.mia_llm_provider in ("", "google") and settings.mia_llm_model else ()
        result = await test_provider(body.provider, body.api_key or saved[body.provider], body.model, base, also_models=voice)
        setup.record_check("ai", result["ok"], result["message"], provider=body.provider, reason=result.get("reason", ""))
        return result

    @app.get("/api/config/ollama-models")
    async def config_ollama_models(base_url: str = Query("", max_length=500)) -> dict:
        try:
            return {"models": await list_models("ollama", base_url=base_url or settings.ollama_base_url)}
        except Exception as e:  # noqa: BLE001 - any failure means "couldn't list"
            raise HTTPException(502, f"Couldn't reach Ollama at {base_url or settings.ollama_base_url}: {type(e).__name__}") from None

    @app.post("/api/config/access-token/regenerate")
    def regenerate_token(response: Response) -> dict:
        token = secrets.token_urlsafe(24)
        config.set_access_token(token)
        # Keep this admin signed in; everyone else signs in again with the new token.
        response.set_cookie(COOKIE, cookie_value(token), httponly=True, samesite="strict", max_age=12 * 3600)
        store.audit("access_token_regenerated")
        return {"token": token}  # shown once

    @app.post("/api/config/access-token/revoke")
    def revoke_token(request: Request) -> dict:
        if not is_direct_local(request):
            raise HTTPException(409, "Without a token only this server's own machine can open the dashboard, so revoking it from here would lock you out. Regenerate it instead, or revoke it on the server itself.")
        config.set_access_token("")
        store.audit("access_token_revoked")
        return {"revoked": True}

    @app.get("/api/settings")
    def settings_overview() -> dict:
        return {"sections": settings_view.overview(settings, ai.name, ai.is_ai, exporter.enabled, mailer.enabled)}

    CHECK_KEYS = {"meetstream": ("meetstream_api_key",), "public_url": ("public_base_url",),
                  "email": ("smtp_host", "smtp_port", "smtp_username", "smtp_password", "smtp_from", "smtp_starttls", "smtp_ssl", "mail_reply_to")}

    def form_values(check: str, body: dict | None) -> tuple[Settings, bool, str]:
        """The settings a check tests: the saved ones with the form's unsaved values over them (validated as a save
        would), whether any differ from what's saved, and a problem with them. An empty password field means the
        saved password. Nothing is saved."""
        raw = (body or {}).get("values") or {}
        if not isinstance(raw, dict):
            return settings, False, "The values to check aren't readable"
        typed = {}
        for key in CHECK_KEYS.get(check, ()):
            if key not in raw:
                continue
            field = config_manager.BY_KEY[key]
            if field.kind == "secret" and not str(raw[key] or "").strip():
                continue
            try:
                typed[key] = config_manager.coerce(field, raw[key])
            except ValueError as e:
                return settings, True, f"{field.label} {e}."
        changed = {k: v for k, v in typed.items() if getattr(settings, k) != v}
        return (dataclasses.replace(settings, **changed) if changed else settings), bool(changed), ""

    def checked(name: str, result: dict, unsaved: bool) -> dict:
        """Only a check of the saved values counts for setup; a check of unsaved ones says so."""
        if unsaved:
            return {**result, "unsaved": True, "message": result["message"] + (" Save to keep it." if result["ok"] else "")}
        setup.record_check(name, result["ok"], result["message"])
        return result

    async def public_url_check(url: str | None = None, local: str = "") -> dict:
        """Does PUBLIC_BASE_URL reach this very app? DNS alone isn't enough: a stale tunnel can resolve to nothing of
        ours, and a live one can lead to another site. Reachable, Recruence and this app are told apart, with the
        status the address really answered."""
        url = settings.public_base_url if url is None else url
        if not url:
            return {"ok": False, "message": "PUBLIC_BASE_URL isn't set"}
        if not await public_host_resolves(url):
            return {"ok": False, "message": f"{url} doesn't resolve: start the tunnel or fix the address"}
        try:
            r = await fetch_public_health(url)
        except httpx.TimeoutException:
            return {"ok": False, "result": "unreachable", "message": f"{url} resolves, but didn't answer within 10 seconds"}
        except httpx.HTTPError as e:
            reason = str(e).strip()[:200] or type(e).__name__
            return {"ok": False, "result": "unreachable",
                    "message": f"{url} resolves, but this computer couldn't connect to it ({type(e).__name__}: {reason})"}
        verdict = public_health_verdict(url, r, instance_id, local)
        if not verdict["ok"]:
            logger.info("Public URL check: %s answered HTTP %s at %s (%s)", url, verdict["status"], PUBLIC_HEALTH_PATH, verdict["result"])
        return verdict

    @app.post("/api/settings/check/{check}")
    async def run_check(check: str, request: Request, body: dict | None = None) -> dict:
        tested, unsaved, problem = form_values(check, body)
        if problem:
            return {"ok": False, "message": problem, "unsaved": True}
        if check == "public_url":
            server = request.scope.get("server") or ("", None)
            local = f"http://127.0.0.1:{server[1]}" if server[1] else ""
            return checked("public_url", await public_url_check(tested.public_base_url, local), unsaved)
        if check == "meetstream":
            # The key alone: the public URL has its own check.
            if not tested.meetstream_api_key:
                return {"ok": False, "message": "Add the MeetStream API key first"}
            try:
                if unsaved:
                    await manager.client_for(tested.meetstream_api_key, lambda c: c.mia.list())
                else:
                    await manager.client().mia.list()
            except meetstream.MeetStreamError as e:
                return checked("meetstream", {"ok": False, "message": f"MeetStream refused the API key or is unreachable: {e}"}, unsaved)
            return checked("meetstream", {"ok": True, "message": "MeetStream accepted the API key."}, unsaved)
        if check == "database":
            try:
                version = await asyncio.to_thread(store.ping)
            except Exception as e:  # noqa: BLE001 - any driver error means the database can't be used
                return {"ok": False, "message": f"The database didn't answer: {type(e).__name__}: {str(e)[:300]}"}
            return {"ok": True, "message": f"{describe(store.target)} answered (schema version {version})"}
        if check == "email":
            to = str((body or {}).get("to") or "").strip()
            if not to or "@" not in to:
                raise HTTPException(422, "Give an address to send the test email to")
            try:
                await mailer.send_with(tested, Email(to=[to], subject=f"{settings.app_name}: test email",
                                                     body=f"Email from {settings.app_name} works.", reply_to=tested.mail_reply_to))
            except MailError as e:
                return checked("email", {"ok": False, "message": str(e)}, unsaved)
            store.audit("test_email_sent")
            return checked("email", {"ok": True, "message": f"The server accepted a test email to {to}."}, unsaved)
        raise HTTPException(404, "Unknown check")

    # ---------- MeetStream webhooks (unsigned: secret path token + bot_id check) ----------

    instance_id = secrets.token_hex(8)  # per process: lets the public URL check recognise this very app

    @app.get("/webhooks/health")
    def webhooks_health() -> dict:
        """Public, for the reachability check. Says nothing about the install beyond a random per-process id."""
        return {"ok": True, "instance": instance_id}

    async def _json_object(request: Request) -> dict:
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Body must be JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "Body must be a JSON object")
        return payload

    @app.post("/webhooks/meetstream/{token}/{kind}")
    async def meetstream_webhook(token: str, kind: str, request: Request) -> dict:
        live = manager.by_token(token)
        if kind not in ("events", "transcript"):
            raise HTTPException(404, "Unknown webhook")
        if live is None:
            if manager.recently_finished(token):
                return {"ok": True, "ignored": "session finished"}
            raise HTTPException(404, "Unknown webhook")
        payload = await _json_object(request)
        bot = live.runner.bot
        handler = bot.handle_event if kind == "events" else bot.handle_transcript
        # Acknowledge at once: deliveries are not retried and slow endpoints time out.
        manager.run_background(handler(payload), f"{kind} {live.id}")
        return {"ok": True}

    # ---------- MIA custom functions ----------

    @app.post("/webhooks/mia/{token}/{tool}")
    async def mia_tool(token: str, tool: str, request: Request) -> dict:
        live = manager.by_token(token)
        if live is None or tool not in TOOL_NAMES:
            raise HTTPException(404, "Unknown tool endpoint")
        runner = live.runner
        if not runner.authorised(request.headers.get("authorization"), request.headers.get("x-bot-id")):
            raise HTTPException(401, "Invalid tool credentials")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        # Default MIA envelope: {"name", "bot", "args"}. Accept a bare args object too.
        args = body.get("args", body) if isinstance(body, dict) else {}
        args = args if isinstance(args, dict) else {}
        result = await runner.handle_tool(tool, args)
        shown = {k: (f"<{len(str(v))} chars>" if k == "answer_note" else v) for k, v in args.items()}
        logger.info(
            "[%s] MIA %s(%s) -> %s %s", runner.session_id, tool, shown, result.get("status"), result.get("question_id") or ""
        )
        return result

    # ---------- dashboard ----------

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
