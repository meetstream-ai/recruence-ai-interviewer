"""Keep secrets that live in URL paths out of the logs.

Candidate links (/interview/<token>, its short form /i/<token>, /book/<token>, /api/public/apply/<token>), their APIs,
a workspace's careers address (/api/public/careers/<key>: anyone holding it can post applications), and the webhook
paths (/webhooks/meetstream/<token>/…, /webhooks/mia/<token>/…, /webhooks/ats/<token>) carry their secret in the path.
tests/test_security_audit.py checks every such route the app registers, so a new one can't be missed. The web
server's access log writes every path; anyone reading the log could reuse a candidate's link or post to a live
session's webhook. This filter replaces those path segments with "***" before anything is written.
"""

from __future__ import annotations

import logging
import re

_SECRET_PATH = re.compile(
    r"(/(?:i|interview|book|apply|api/public/(?:interview|booking|apply|careers)|webhooks/(?:meetstream|mia|ats))/)[A-Za-z0-9_\-]{8,}")


def redact(text: str) -> str:
    return _SECRET_PATH.sub(r"\1***", text)


class RedactTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args) \
                if isinstance(record.args, tuple) else record.args
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def install() -> None:
    """On the web server's access log and the app's own loggers (idempotent)."""
    for name in ("uvicorn.access", "uvicorn.error", "app", "httpx"):
        log = logging.getLogger(name)
        if not any(isinstance(f, RedactTokens) for f in log.filters):
            log.addFilter(RedactTokens())
