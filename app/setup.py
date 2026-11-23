"""First-run setup: what has been configured, what's left, and who may run the wizard.

Progress lives in DATA_DIR/setup.json, beside the encryption key and outside the database, so it survives a
database switch made during setup. It holds no secrets: choices, timestamps and the outcome of the last check of
each integration. Every value itself is saved through the existing mechanisms (ConfigManager for settings, the
access token, app/dbconfig.py for the database), so the wizard and the Settings page can never disagree.

First launch: setup.json has no completion time. Two cases count as already set up, so nobody is sent through the
wizard by an upgrade or a fully scripted deployment:
- an existing install (the database already holds roles, interviews or saved settings);
- a preconfigured one: access (APP_ACCESS_TOKEN or the identity proxy) and an AI provider set in the environment.

Who may run it: a signed-in admin, or the person at the server's own machine (no token set). Anyone else, e.g. a
browser reaching Docker through its port mapping, enters the one-time setup code the server prints in its log. The
code works only until setup is complete and never through the public tunnel (proxied requests are refused).
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any, Optional

from app.models import utcnow
from app.compare import same

logger = logging.getLogger(__name__)

FILE = "setup.json"
COOKIE = "mia_setup"
# The database comes before the account: the first account is created in the database it will live in.
STEPS = ("welcome", "database", "access", "ai", "meetstream", "email", "careers", "review")
OPTIONAL = {"meetstream", "email", "careers"}
MAX_FAILURES = 10


class SetupState:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / FILE
        self.data: dict[str, Any] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("setup progress in %s couldn't be read; starting it again", self.path)
        # In memory only: the code printed in the log, and the browsers that entered it.
        self.code: Optional[str] = None
        self._sessions: set[str] = set()
        self._failures = 0

    # ---------- persistence ----------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)  # never a half-written file

    def update(self, **values: Any) -> None:
        self.data.update({k: v for k, v in values.items()})
        self.data.setdefault("started_at", utcnow().isoformat())
        self.save()

    def record_check(self, name: str, ok: bool, message: str, **extra: Any) -> None:
        """The last outcome of a connection test (AI provider, MeetStream, public URL, email). No secrets."""
        checks = self.data.setdefault("checks", {})
        checks[name] = {"ok": ok, "message": message[:300], "at": utcnow().isoformat(), **extra}
        self.save()

    def forget_check(self, name: str) -> None:
        if self.data.get("checks", {}).pop(name, None) is not None:
            self.save()

    @property
    def completed(self) -> bool:
        return bool(self.data.get("completed_at"))

    def complete(self, reason: str = "wizard") -> None:
        self.update(completed_at=utcnow().isoformat(), completed_by=reason)
        self.code = None
        self._sessions.clear()

    # ---------- first launch ----------

    def settle_first_launch(self, existing_data: bool, preconfigured: bool, access_set: bool = True) -> None:
        """Mark existing and fully preconfigured installs as set up, once. Reopen setup if the access token it created
        is gone: the token is a setting in the database, so pointing the app at another database loses it, and a
        finished setup with no way to sign in would leave everyone but localhost locked out."""
        if self.completed and self.data.get("access") in ("token", "account") and not access_set:
            logger.warning("The access token created in setup isn't in this database (was the database changed?); "
                           "setup is open again to create one")
            for key in ("completed_at", "completed_by", "access"):
                self.data.pop(key, None)
            self.update(reopened=True)
            return
        if self.completed:
            return
        if self.data.get("reopened") and access_set:  # back on the database that holds the token
            self.data.pop("reopened")
            self.update(access="token")
            self.complete("access token found again")
            return
        if existing_data and not self.data.get("started_at"):
            self.complete("existing install")
        elif preconfigured and not self.data.get("started_at"):
            self.complete("configured by environment")

    def issue_code(self) -> str:
        """The one-time setup code for browsers that aren't on the server itself. Printed in the log at start."""
        if self.code is None:
            raw = secrets.token_hex(4).upper()
            self.code = f"{raw[:4]}-{raw[4:]}"
        return self.code

    def unlock(self, code: str) -> Optional[str]:
        """A session value for the setup cookie if the code is right; None otherwise."""
        if self.completed or not self.code:
            return None
        given = code.strip().upper().replace(" ", "")
        if len(given) == 8:
            given = f"{given[:4]}-{given[4:]}"
        if not same(given, self.code):
            self._failures += 1
            if self._failures >= MAX_FAILURES:
                # Guessing a 32-bit code takes far more tries; stop it outright. Restarting prints a new one.
                logger.warning("setup code disabled after %d wrong attempts; restart the app for a new one", self._failures)
                self.code = None
            return None
        session = secrets.token_urlsafe(32)
        self._sessions.add(session)
        return session

    def valid_session(self, value: str) -> bool:
        return bool(value) and not self.completed and any(same(value, s) for s in self._sessions)


def step_states(*, state: SetupState, access: dict, database: dict, ai: dict, meetstream: dict, email: dict,
                careers: Optional[dict] = None) -> dict:
    """Each step as done / skipped / incomplete / failed, with what to show. Values come from the real configuration."""
    skipped = set(state.data.get("skipped", []))
    checks = state.data.get("checks", {})

    def mark(name: str, done: bool, failed: bool = False) -> str:
        if done:
            return "done"
        if failed:
            return "failed"
        if name in skipped:
            return "skipped"
        return "incomplete"

    ai_check = checks.get("ai") or {}
    ms_check, url_check = checks.get("meetstream") or {}, checks.get("public_url") or {}
    mail_check = checks.get("email") or {}
    return {
        "welcome": {"status": mark("welcome", bool(state.data.get("mode"))), "mode": state.data.get("mode")},
        "access": {"status": mark("access", access["configured"]), **access},
        # A team install needs a hosted database; the local file only counts for one person.
        "database": {"status": mark("database", not database.get("team_needs_hosted")), **database},
        "ai": {"status": mark("ai", ai["configured"] or ai.get("offline_chosen", False), failed=ai_check.get("ok") is False and not ai["configured"]),
               **ai, "check": ai_check or None},
        "meetstream": {"status": mark("meetstream", meetstream["key_set"] and bool(ms_check.get("ok")) and bool(url_check.get("ok")),
                                      failed=ms_check.get("ok") is False or url_check.get("ok") is False),
                       **meetstream, "check": ms_check or None, "url_check": url_check or None},
        "email": {"status": mark("email", email["configured"] and bool(mail_check.get("ok")), failed=mail_check.get("ok") is False),
                  **email, "check": mail_check or None},
        # How the company's careers website is set up (the workspace's careers_site): what connecting it takes.
        "careers": {"status": mark("careers", bool((careers or {}).get("site"))), **(careers or {"site": ""})},
    }
