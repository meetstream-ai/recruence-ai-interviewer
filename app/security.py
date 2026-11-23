"""Recruiter access control.

- `/webhooks/*` is for MeetStream. It is reachable through the public tunnel and
  protected by per-session secrets (path token; bearer token for MIA tools).
- `/book/<token>` and `/api/public/booking/<token>` are the candidate's self-scheduling
  page, also public, protected by the booking's unguessable token (app/booking.py).
- Accounts (app/accounts.py): once anyone has an account in the connected database, people sign in with
  email and password; the browser gets an HttpOnly session cookie. Their workspace role decides what they
  may do there (owner and admin: everything in it; recruiter: no deleting, no audit log), and only the
  installation's admins change installation settings (AI providers, MeetStream, email, database).
- Everything else is the recruiter's. Requests that came through a proxy or tunnel
  are refused, so the public URL exposes only the webhooks, unless they come from
  the trusted identity proxy (below).
- With APP_ACCESS_TOKEN set, the API needs it, as `Authorization: Bearer <token>` or
  the HttpOnly cookie set by POST /api/auth/login. Without it, only direct
  localhost requests are served (development). Either way the caller is an admin.
- Trusted identity proxy (PROXY_AUTH_SECRET set): an SSO proxy in front of the
  dashboard signs users in and forwards each request with the shared secret in
  `X-MIA-Proxy-Secret` and the user's email (and optionally groups) in headers.
  Only then are proxied requests accepted and the user headers believed. Groups map
  to roles: viewer (read only), recruiter (everything but deleting and the full
  audit log), admin.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
from dataclasses import dataclass, field
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from app.store import CURRENT_ACTOR, CURRENT_WORKSPACE
from app.compare import same

COOKIE = "mia_interviewer_session"
ACCOUNT_COOKIE = "mia_account"
WORKSPACE_COOKIE = "mia_workspace"  # for access-token and proxy sign-ins, which have no session row
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded", "cf-connecting-ip")
PROXY_SECRET_HEADER = "x-mia-proxy-secret"
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
HEALTH_PATHS = ("/api/health", "/api/health/ready")
PUBLIC_PATHS = {"/", "/api/health", "/api/health/ready", "/api/auth/login", "/api/auth/status", "/api/setup/status", "/api/setup/unlock",
                "/api/auth/account-login", "/api/auth/register", "/api/auth/reset-request", "/api/auth/reset",
                "/api/connection/status", "/api/connection/unlock"}
# Paths for someone who isn't in a workspace yet: their account, workspaces (create, join), setup, connection.
NO_WORKSPACE_PREFIXES = ("/api/auth/", "/api/me", "/api/workspaces", "/api/accounts", "/api/setup/", "/api/connection/", "/api/health",
                         "/api/config", "/api/settings", "/api/database", "/api/platforms", "/static/")
# Installation settings: shared by every workspace in the database, changed only by the installation's admins.
INSTALL_PREFIXES = ("/api/config", "/api/settings/", "/api/database", "/api/connection/")
# Read only by installation admins too: infrastructure details (hosts, addresses, the database) and the
# installation's MeetStream calendar, which belongs to no single workspace.
INSTALL_READ_PREFIXES = ("/api/config", "/api/settings", "/api/database", "/api/calendar")
# Reachable on the public address without signing in: MeetStream's webhooks, and the candidate's booking
# page (/book/<token>) with its API. Each is guarded by an unguessable token in the path.
# Candidate pages, each behind an unguessable token: self-scheduling, a role's application form, an interview link.
PUBLIC_PREFIXES = ("/webhooks/", "/book/", "/api/public/booking/", "/api/public/apply/", "/api/public/careers/", "/interview/", "/i/",
                   "/api/public/interview/")
PUBLIC_ASSETS = {"/static/styles.css", "/static/book.js", "/static/interview.js", "/static/logo.png",
                 "/static/favicon.png", "/static/favicon.ico", "/static/apple-touch-icon.png"}
ROLES = ("viewer", "recruiter", "admin")
READ_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class User:
    name: str
    role: str  # what permitted() checks: viewer, recruiter or admin
    via: str  # "account", "proxy", "token", "local" or "setup"
    user_id: str = ""  # accounts only
    email: str = ""
    install_admin: bool = True  # accounts: the users.is_admin flag; the token, proxy admins and localhost run the install
    workspace_id: Optional[str] = None
    workspace_role: str = ""  # owner, admin or recruiter (accounts)


def role_for_workspace(workspace_role: str) -> str:
    return {"owner": "admin", "admin": "admin", "recruiter": "recruiter"}.get(workspace_role, "viewer")


@dataclass(frozen=True)
class ProxyAuth:
    secret: str = ""
    user_header: str = "x-forwarded-email"
    groups_header: str = "x-forwarded-groups"
    default_role: str = "recruiter"  # a role, or "none" to refuse users in no listed group
    admin_groups: frozenset[str] = field(default_factory=frozenset)
    recruiter_groups: frozenset[str] = field(default_factory=frozenset)
    viewer_groups: frozenset[str] = field(default_factory=frozenset)

    def role_for(self, groups: set[str]) -> Optional[str]:
        for role, members in (("admin", self.admin_groups), ("recruiter", self.recruiter_groups), ("viewer", self.viewer_groups)):
            if groups & members:
                return role
        return self.default_role if self.default_role in ROLES else None


class Denied(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


def cookie_value(token: str) -> str:
    """What the cookie holds: a digest, so the token itself never sits in the browser."""
    return hashlib.sha256(f"mia-interviewer:{token}".encode()).hexdigest()


def has_valid_credentials(request: Request, token: str) -> bool:
    if not token:
        return False
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and same(header[7:], token):
        return True
    return same(request.cookies.get(COOKIE, ""), cookie_value(token))


LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _host_name(request: Request) -> str:
    """The Host header without its port: what the browser thinks it's talking to."""
    host = (request.headers.get("host") or "").strip().lower()
    if host.startswith("["):  # [::1]:8000
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_direct_local(request: Request) -> bool:
    """A request from this computer to this computer's own name. The Host check closes DNS rebinding: a web page whose
    name was made to resolve to 127.0.0.1 reaches this port from the visitor's browser, but with its own name in Host,
    so before the first account exists it can't act as the local administrator."""
    peer = request.client.host if request.client else ""
    if peer not in LOOPBACK or any(h in request.headers for h in PROXY_HEADERS):
        return False
    name = _host_name(request)
    return name in LOOPBACK_NAMES or (peer == "testclient" and name == "testserver")


# ---------- the cloud backend behind its HTTPS proxy (REMOTE_DASHBOARD, docs/CLOUD.md) ----------

REMOTE = {"on": False, "networks": ()}  # set once per app by configure_remote()


def configure_remote(on: bool, trusted: tuple[str, ...] = ()) -> None:
    nets = []
    for item in trusted:
        try:
            nets.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError:
            continue
    REMOTE.update(on=bool(on), networks=tuple(nets))


def _trusted_peer(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    if peer in ("testclient", "localhost"):
        return True
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in REMOTE["networks"])


def proxied(request: Request) -> bool:
    return any(h in request.headers for h in PROXY_HEADERS)


def remote_client(request: Request) -> str:
    """The visitor's address behind a trusted proxy (remote dashboard), else the connection's peer.

    The rightmost X-Forwarded-For entry: the one the nearest proxy added (Caddy, a load balancer, or Cloudflare in
    front of cloudflared). Entries to its left came from the visitor and can be anything, so they would let one client
    pose as many and slip past the per-address limits. CF-Connecting-IP only when there's no X-Forwarded-For: behind a
    proxy that isn't Cloudflare, a visitor could send that header themselves."""
    peer = request.client.host if request.client else "?"
    if REMOTE["on"] and _trusted_peer(request):
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        return hops[-1] if hops else (request.headers.get("cf-connecting-ip", "").strip() or peer)
    return peer


def served_over_https(request: Request) -> bool:
    """HTTPS to the visitor: the connection itself, or (remote dashboard) what the trusted proxy says it was."""
    if request.url.scheme == "https":
        return True
    return REMOTE["on"] and _trusted_peer(request) and request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def plain_http_via_proxy(request: Request) -> bool:
    """Remote dashboard: the trusted proxy says the visitor came over plain http (it forwarded instead of redirecting).
    Requests without the header (the container's health check, the owner on the server itself) aren't affected."""
    return (REMOTE["on"] and request.url.scheme != "https" and _trusted_peer(request)
            and request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "http")


def from_trusted_proxy(request: Request, proxy: ProxyAuth) -> bool:
    sent = request.headers.get(PROXY_SECRET_HEADER, "")
    return same(sent, proxy.secret)


def identify(request: Request, access_token: str, proxy: ProxyAuth) -> Optional[User]:
    """Who sent a non-webhook request: None if not signed in. Raises Denied for an untrusted proxied request."""
    if from_trusted_proxy(request, proxy):
        email = request.headers.get(proxy.user_header, "").strip()
        if not email:
            raise Denied(401, "The identity proxy sent no user")
        raw = request.headers.get(proxy.groups_header, "") if proxy.groups_header else ""
        role = proxy.role_for({g.strip() for g in raw.split(",") if g.strip()})
        if role is None:
            raise Denied(403, "Your account has no role in this app")
        return User(email[:200], role, "proxy")
    if any(h in request.headers for h in PROXY_HEADERS):
        raise Denied(403, "Only MeetStream webhooks are served on the public URL")
    if access_token:
        return User("access token", "admin", "token") if has_valid_credentials(request, access_token) else None
    return User("localhost", "admin", "local") if is_direct_local(request) else None


def permitted(role: str, method: str, path: str) -> bool:
    if role == "admin":
        return True
    if path == "/api/audit" or (method == "DELETE" and path.startswith(("/api/interviews/", "/api/roles/"))):
        return False
    if path == "/api/demo" and method not in READ_METHODS:
        return False  # adds or removes records across the workspace: admins only
    if path.startswith("/api/database") and (method not in READ_METHODS or path != "/api/database"):
        return False  # the connection and its tests reach other servers: admins only
    if method not in READ_METHODS and path.startswith(("/api/settings/", "/api/config")):
        return False  # checks reach MeetStream and can send email: admins only
    return role == "recruiter" or method in READ_METHODS


def make_guard(access_token, proxy: ProxyAuth | None = None, setup_user=None, account_user=None, accounts_exist=None,
               workspace_for=None):
    """`access_token`: the token, or a function returning the current one (it can be regenerated while the app runs).
    `setup_user(request)`: during first-run setup, the admin who entered the setup code (app/setup.py), or None.
    `account_user(request)`: the signed-in account (session cookie) as a User with its workspace, or None.
    `accounts_exist()`: once true, a browser on the server itself also signs in (no more open localhost).
    `workspace_for(request, user)`: the workspace for a token, proxy or localhost user (None: no workspaces yet)."""
    proxy = proxy or ProxyAuth()

    async def guard(request: Request, call_next):
        path = request.url.path
        # Health checks name no one and read no account, so they answer even while the database is down (R-02).
        if path.startswith(PUBLIC_PREFIXES) or path in PUBLIC_ASSETS or path in HEALTH_PATHS:
            return await call_next(request)
        # Browsers set this header and pages can't change it: no other origin, not even another port on this machine,
        # can drive the dashboard from a visitor's browser, whatever the cookies or the open-localhost mode allow.
        if request.method not in READ_METHODS and request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return JSONResponse({"detail": "Requests from other websites aren't accepted"}, status_code=403)
        try:
            token = access_token() if callable(access_token) else access_token
            # Remote dashboard: recruiters' accounts (and the access token) work through the HTTPS proxy in front of the
            # cloud backend. Never the open-localhost mode or the setup code: those stay on the server itself.
            remote = REMOTE["on"] and proxied(request) and not from_trusted_proxy(request, proxy)
            user = account_user(request) if account_user is not None and (remote or not proxied(request)) else None
            if user is None and remote:
                if token and has_valid_credentials(request, token):
                    user = User("access token", "admin", "token")
            elif user is None:
                user = identify(request, token, proxy)
                if user is not None and user.via == "local" and accounts_exist is not None and accounts_exist():
                    user = None  # accounts exist: everyone signs in, here too
            if user is None and setup_user is not None and not any(h in request.headers for h in PROXY_HEADERS):
                user = setup_user(request)
            if user is not None and user.via != "account" and workspace_for is not None:
                from dataclasses import replace
                user = replace(user, workspace_id=workspace_for(request, user))
        except Denied as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status)
        request.state.user = user
        # /api/connection/*: the handlers decide (an admin, the setup, or a pass from the log code; app/workspace_api.py).
        if not (path in PUBLIC_PATHS or path.startswith(("/static/", "/api/connection/"))):
            if user is None:
                if accounts_exist is not None and accounts_exist():
                    return JSONResponse({"detail": "Sign in to continue"}, status_code=401)
                if token:
                    return JSONResponse({"detail": "Sign in with the access token"}, status_code=401)
                return JSONResponse({"detail": "Set APP_ACCESS_TOKEN to allow access from other machines"}, status_code=403)
            if not permitted(user.role, request.method, path):
                return JSONResponse({"detail": f"Your role ({user.role}) can't do this"}, status_code=403)
            if (path.startswith(INSTALL_PREFIXES) and request.method not in READ_METHODS and path != "/api/connection/unlock"
                    and not user.install_admin):
                return JSONResponse({"detail": "Only the installation's admins change these settings"}, status_code=403)
            if path.startswith(INSTALL_READ_PREFIXES) and not user.install_admin:
                return JSONResponse({"detail": "Only the installation's admins can see this"}, status_code=403)
            if user.via == "account" and not user.workspace_id and not path.startswith(NO_WORKSPACE_PREFIXES):
                if request.method not in READ_METHODS:
                    return JSONResponse({"detail": "Join or create a workspace first"}, status_code=409)
        # Audit entries written while handling this request name who made it; records are limited to its workspace.
        actor = (f"{user.name} ({user.role})" if user.via == "proxy" else user.email or user.name) if user else ""
        reset = CURRENT_ACTOR.set(actor)
        ws = user.workspace_id if user and (user.via == "account" or user.workspace_id) else None
        reset_ws = CURRENT_WORKSPACE.set("" if user and user.via == "account" and not ws else ws)
        try:
            return await call_next(request)
        finally:
            CURRENT_ACTOR.reset(reset)
            CURRENT_WORKSPACE.reset(reset_ws)

    return guard
