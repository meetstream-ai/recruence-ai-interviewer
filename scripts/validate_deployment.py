"""Validate a deployed Recruence cloud backend (release validation B-6, docs/CLOUD.md).

  python scripts/validate_deployment.py --url https://recruence.example.com
  python scripts/validate_deployment.py --url https://recruence.example.com --email admin@example.com
  python scripts/validate_deployment.py --url https://recruence.example.com --log app.log --secrets-from deploy/cloud/.env

Checks HTTPS and its certificate, the http -> https redirect, security headers, that signed-out and wrong-token requests
are refused, that errors don't leak internals, and (with --email) signing in through the proxy, Secure cookies and
whether MeetStream can reach the backend. With --log it scans a saved container log for the secret values in
--secrets-from and for email addresses, printing only which setting leaked, never a value. Creates nothing, unless
--check-registration is given (see its help). Exit code 0 when nothing failed.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import socket
import ssl
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

OK, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
# Settings whose values are secret (public ones such as PUBLIC_BASE_URL or DOMAIN may appear in logs).
SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|DATABASE_URL", re.I)


def certificate(host: str, port: int = 443, ca_file: str | None = None) -> tuple[bool, str]:
    ctx = ssl.create_default_context(cafile=ca_file)
    try:
        with socket.create_connection((host, port), timeout=10) as raw, ctx.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert()
    except ssl.SSLCertVerificationError as e:
        return False, f"certificate not trusted: {e.verify_message}"
    except OSError as e:
        return False, f"no TLS connection: {type(e).__name__}"
    expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    left = expires - datetime.now(timezone.utc)
    issuer = dict(x[0] for x in cert.get("issuer", ())).get("organizationName") or dict(x[0] for x in cert.get("issuer", ())).get("commonName", "?")
    when = f"{left.days} days" if left.days >= 1 else f"{int(left.total_seconds() // 3600)} hours (a short-lived certificate)"
    return left.days > 7, f"trusted, issued by {issuer}, expires in {when}"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True, help="the backend's public https:// address")
    ap.add_argument("--email", help="an installation admin account, for the signed-in checks (password asked at the prompt)")
    ap.add_argument("--log", help="a saved container log to scan (docker compose logs app > app.log)")
    ap.add_argument("--secrets-from", help="the deployment's .env: the values of its secret settings (keys, tokens, passwords, DATABASE_URL) are looked for in --log (never printed)")
    ap.add_argument("--ca-file", help="trust this CA too (a staging or local rehearsal with a private CA, e.g. Caddy's internal one); "
                                      "never needed for a real deployment")
    ap.add_argument("--check-registration", action="store_true",
                    help="try to register a throwaway account: must be refused (403). If registration is open, this creates one")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    parts = urlsplit(base)
    results: list[tuple[str, str, str]] = []

    def check(name: str, status: str, detail: str = "") -> None:
        results.append((name, status, detail))

    if parts.scheme != "https":
        print("Give the public https:// address.")
        return 2
    ok, detail = certificate(parts.hostname, parts.port or 443, args.ca_file)
    verify = ssl.create_default_context(cafile=args.ca_file) if args.ca_file else True
    check("Certificate", OK if ok else FAIL, detail)
    c = httpx.Client(base_url=base, timeout=20, follow_redirects=False, verify=verify)
    try:
        health = c.get("/api/health")
        body = health.json()
    except httpx.HTTPError as e:
        check("Health", FAIL, type(e).__name__)
        return report(results)
    except ValueError:
        check("Health", FAIL, f"HTTP {health.status_code}, not Recruence's health answer: is this the right address?")
        return report(results)
    check("Health", OK if health.status_code == 200 and body.get("ok") else FAIL, f"HTTP {health.status_code}")
    try:
        ready = c.get("/api/health/ready")
        rb = ready.json()
        check("Database ready", OK if ready.status_code == 200 and rb.get("ok") else FAIL,
              f"schema {rb['database'].get('schema_version')}" if ready.status_code == 200 and rb.get("ok") else (rb.get("database") or {}).get("problem", f"HTTP {ready.status_code}"))
    except (httpx.HTTPError, ValueError) as e:
        check("Database ready", FAIL, f"/api/health/ready: {type(e).__name__} (a Recruence without the readiness check?)")
    ms = body.get("meetstream", {})
    check("Live meetings configured", OK if ms.get("ready") else WARN, "ready" if ms.get("ready") else ms.get("reason", ""))
    try:
        plain = httpx.get(f"http://{parts.netloc}/", timeout=10, follow_redirects=False)
        loc = plain.headers.get("location", "")
        check("http redirects to https", OK if plain.status_code in (301, 302, 307, 308) and loc.startswith("https://") else FAIL,
              f"HTTP {plain.status_code} {loc[:60]}")
    except httpx.HTTPError as e:
        check("http redirects to https", WARN, f"port 80 not answering ({type(e).__name__}); fine if only 443 is open")

    page = c.get("/")
    h = {k.lower(): v for k, v in page.headers.items()}
    check("Content-Security-Policy", OK if "frame-ancestors 'none'" in h.get("content-security-policy", "") else FAIL, h.get("content-security-policy", "missing")[:70])
    check("X-Frame-Options", OK if h.get("x-frame-options", "").upper() == "DENY" else FAIL, h.get("x-frame-options", "missing"))
    check("X-Content-Type-Options", OK if h.get("x-content-type-options") == "nosniff" else FAIL, h.get("x-content-type-options", "missing"))
    check("Strict-Transport-Security", OK if "max-age" in h.get("strict-transport-security", "") else WARN,
          h.get("strict-transport-security", "missing: Recruence sends it when the trusted proxy says the visit was https (REMOTE_DASHBOARD, X-Forwarded-Proto)"))

    r = c.get("/api/roles")
    check("Signed-out API refused", OK if r.status_code == 401 else FAIL, f"HTTP {r.status_code}")
    fake = secrets.token_urlsafe(24)
    for name, method, path in (("Wrong careers key", "GET", f"/api/public/careers/{fake}"), ("Wrong application key", "GET", f"/api/public/apply/{fake}"),
                               ("Wrong webhook token", "POST", f"/webhooks/meetstream/{fake}/events"),
                               ("Wrong AI tool token", "POST", f"/webhooks/mia/{fake}/next_question"),
                               ("Wrong interview link", "GET", f"/api/public/interview/{fake}")):
        r = c.request(method, path, json={} if method == "POST" else None)
        check(name, OK if r.status_code in (401, 404) else FAIL, f"HTTP {r.status_code}")
    r = c.get(f"/api/{fake}")
    leaky = any(s in r.text for s in ("Traceback", 'File "/', "sqlalchemy", "psycopg"))
    check("Errors don't leak internals", FAIL if leaky else OK, f"unknown path → HTTP {r.status_code}")
    r = c.post("/api/auth/account-login", content=b"{not json", headers={"Content-Type": "application/json"})
    check("Malformed request handled", OK if r.status_code in (400, 422) and "Traceback" not in r.text else FAIL, f"HTTP {r.status_code}")

    if args.check_registration:
        r = c.post("/api/auth/register", json={"name": "Validation Probe", "email": f"probe-{fake[:8].lower()}@example.com",
                                               "password": secrets.token_urlsafe(16)})
        check("Strangers can't register", OK if r.status_code == 403 else FAIL,
              f"HTTP {r.status_code}" + ("" if r.status_code == 403 else ": registration is OPEN (a throwaway account was created; delete it)"))
    else:
        check("Strangers can't register", SKIP, "run with --check-registration to test (creates an account only if registration is open)")

    if args.email:
        pw = os.environ.get("RECRUENCE_PASSWORD") or getpass.getpass(f"Password for {args.email}: ")
        r = c.post("/api/auth/account-login", json={"email": args.email, "password": pw})
        ck = r.headers.get("set-cookie", "")
        check("Sign in through the proxy", OK if r.status_code == 200 else FAIL, f"HTTP {r.status_code}")
        check("Session cookie flags", OK if all(f in ck for f in ("Secure", "HttpOnly")) and "samesite=strict" in ck.lower() else FAIL,
              ", ".join(f for f in ("Secure", "HttpOnly", "SameSite=Strict") if f.lower() in ck.lower()) or "no cookie")
        if r.status_code == 200:
            cs = httpx.Client(base_url=base, timeout=60, cookies=r.cookies, verify=verify)
            check("Signed-in API", OK if cs.get("/api/roles").status_code == 200 else FAIL)
            u = cs.post("/api/settings/check/public_url", json={})
            try:
                uj = u.json()
            except ValueError:
                uj = {"message": f"HTTP {u.status_code}, not a Recruence answer"}
            check("MeetStream can reach the backend", OK if u.status_code == 200 and uj.get("ok") else FAIL, str(uj.get("message", ""))[:90])
    else:
        check("Signed-in checks", SKIP, "run with --email for sign-in, cookies and the webhook reachability check")

    if args.log:
        text = open(args.log, encoding="utf-8", errors="ignore").read()
        leaks = []
        if args.secrets_from:
            for line in open(args.secrets_from, encoding="utf-8", errors="ignore"):
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    v = v.strip().strip('"').strip("'")
                    if SECRET_NAME.search(k) and len(v) >= 12 and v in text:
                        leaks.append(k.strip())
        check("No secrets in the log", FAIL if leaks else OK, ("values of: " + ", ".join(leaks)) if leaks else "none of the deployment's secret values")
        emails = {m for m in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text) if not m.endswith(("example.com", "example.org"))}
        check("No email addresses in the log", WARN if emails else OK, f"{len(emails)} distinct address(es)" if emails else "none")
        check("No tracebacks in the log", WARN if "Traceback" in text else OK, f"{text.count('Traceback')} traceback(s)")
    return report(results)


def report(results) -> int:
    width = max(len(n) for n, _, _ in results)
    for name, status, detail in results:
        print(f"{status:4}  {name:{width}}  {detail}")
    failed = [n for n, s, _ in results if s == FAIL]
    print("\nNo failures." if not failed else f"\n{len(failed)} check(s) failed.")
    print("Also check by hand: container restart keeps data and sessions; "
          "an application from the careers site reaches the right role; the live interview (B-1) on this backend.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
