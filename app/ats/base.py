"""The common interface every external ATS adapter implements (app/ats/greenhouse.py, lever.py, generic.py).

An adapter only translates: it checks that a webhook really comes from the provider, turns the provider's payload
into `InboundApplication`s (fetching details from the provider's API when its webhooks are thin), and posts Recruence's
interview result back in the provider's terms. Everything else (storing, screening, inviting, interviewing,
evaluating) is Recruence's one pipeline, in app/ats/service.py and app/server.py, whatever the source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


class AdapterError(Exception):
    """The provider refused or failed; `retry` says whether trying again later could help."""

    def __init__(self, message: str, retry: bool = True):
        super().__init__(message)
        self.retry = retry


class SignatureError(AdapterError):
    def __init__(self, message: str = "The webhook signature doesn't match"):
        super().__init__(message, retry=False)


@dataclass
class InboundApplication:
    """One candidate for one job, as an external ATS describes it, in Recruence's terms."""

    external_application_id: str
    external_candidate_id: str = ""
    external_job_id: str = ""
    job_title: str = ""
    job_description: str = ""
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    current_role: str = ""
    links: str = ""
    resume_text: str = ""
    resume_url: str = ""  # fetched (size-limited) and read like an uploaded file
    resume_filename: str = ""
    resume_bytes: bytes = b""  # already downloaded by the adapter (APIs that need its credentials)
    answers: list[tuple[str, str]] = field(default_factory=list)  # (question, answer)
    url: str = ""  # the record in the ATS
    ats_screening: Optional[bool] = None  # the ATS's own decision, when it sends one (True: eligible)


@dataclass
class InboundEvent:
    event_id: str  # the provider's delivery or event id: a retried webhook is recognised by it
    kind: str  # e.g. new_candidate_application, candidateStageChange
    applications: list[InboundApplication] = field(default_factory=list)
    ignored: str = ""  # why nothing was taken from it (e.g. a stage Recruence doesn't act on)


@dataclass
class Result:
    """What Recruence sends back after an interview (or a recruiter's decision)."""

    candidate_name: str
    role_title: str
    stage: str  # Recruence's stage: passed, failed, evaluation_pending, ...
    mapped_status: str  # the label the integration's status mapping gives that stage
    score: Optional[int]  # out of 100
    recommendation: str
    summary: str
    report_url: str
    decision: str = ""


class Adapter:
    provider = ""
    label = ""
    # What the provider actually supports (shown in Settings and docs/ATS_INTEGRATIONS.md).
    capabilities: dict[str, str] = {}
    secret_fields: tuple[str, ...] = ()  # credentials, stored encrypted
    setting_fields: tuple[str, ...] = ()
    # Verified end to end against the real system (signed webhooks in, results out over public HTTPS). An adapter that isn't
    # is shown as "Not tested" and never as ready, until it has been validated with an organisation's account.
    verified = False
    UNVERIFIED_NOTE = "Not tested – Organization account required"

    def verify(self, headers: dict[str, str], body: bytes, secrets: dict[str, str]) -> None:
        """Raise SignatureError if the webhook isn't from the provider."""
        raise NotImplementedError

    def parse(self, payload: Any, settings: dict) -> InboundEvent:
        raise NotImplementedError

    async def complete(self, event: InboundEvent, client: httpx.AsyncClient, secrets: dict[str, str], settings: dict) -> InboundEvent:
        """Fill in details the webhook didn't carry (from the provider's API). Default: nothing to add."""
        return event

    async def push_result(self, client: httpx.AsyncClient, ref: dict, result: Result, secrets: dict[str, str], settings: dict) -> str:
        """Send the result to the provider; returns a short description. Raise AdapterError on failure."""
        raise NotImplementedError

    async def test(self, client: httpx.AsyncClient, secrets: dict[str, str], settings: dict) -> str:
        """Check the credentials with the cheapest call the provider has. Raise AdapterError if they don't work."""
        raise NotImplementedError


def result_text(r: Result) -> str:
    """The note posted to an ATS: short, factual, advisory."""
    lines = [f"Recruence AI interview: {r.mapped_status or r.stage.replace('_', ' ')}",
             f"Score: {r.score}/100" if r.score is not None else "Score: not evaluated",
             f"AI recommendation (advisory): {r.recommendation.replace('_', ' ') or 'none'}"]
    if r.decision:
        lines.append(f"Recruiter decision in Recruence: {r.decision}")
    if r.summary:
        lines.append(f"Summary: {r.summary}")
    if r.report_url:
        lines.append(f"Report: {r.report_url}")
    return "\n".join(lines)


def url_problem(url: str) -> Optional[str]:
    """Why Recruence shouldn't send credentials or candidates' data to `url`, or None: HTTPS to a public host only."""
    import ipaddress
    from urllib.parse import urlsplit
    if not url.startswith("https://"):
        return "must start with https://"
    host = (urlsplit(url).hostname or "").lower()
    if not host or host in ("localhost", "0.0.0.0") or host.endswith((".localhost", ".local", ".internal", ".lan")):
        return "must be a public address"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_reserved or ip.is_multicast:
        return "must be a public address"
    return None


async def resolves_publicly(url: str) -> bool:
    """Whether every address the URL's host resolves to is public. url_problem checks the written host; this catches a
    public-looking name that resolves to a private or local address (the server would otherwise fetch from its own
    network for whoever sent the URL)."""
    import asyncio
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for *_, addr in infos:
        ip = ipaddress.ip_address(addr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_reserved or ip.is_multicast:
            return False
    return bool(infos)


def http_error(provider: str, e: Exception) -> AdapterError:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        retry = code >= 500 or code in (408, 429)
        hint = " (check the API key)" if code in (401, 403) else ""
        return AdapterError(f"{provider} answered {code}{hint}", retry=retry)
    who = "the" + provider[3:] if provider.startswith("The ") else provider  # mid-sentence: "Couldn't reach the callback URL"
    return AdapterError(f"Couldn't reach {who}: {type(e).__name__}", retry=True)
