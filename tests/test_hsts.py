"""Strict-Transport-Security (found missing in the 12 November cloud rehearsal): sent only on responses that reached
the visitor over HTTPS, never on plain http (the desktop app on this computer), and never with includeSubDomains."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_cloud import RECRUITER, cloud, make

HSTS = "Strict-Transport-Security"


def test_https_through_the_proxy_gets_hsts(settings):
    app = make(cloud(settings))
    with TestClient(app, base_url="https://testserver", headers=RECRUITER) as c:
        for path in ("/", "/api/health", "/api/roles"):  # a page, a public API, a refused one (401): all of them
            value = c.get(path).headers.get(HSTS, "")
            assert value.startswith("max-age=") and int(value.split("=")[1]) >= 180 * 24 * 3600, (path, value)
            assert "includesubdomains" not in value.lower()


def test_plain_http_gets_no_hsts(settings):
    with TestClient(make(settings)) as local:  # the desktop app: http on 127.0.0.1
        assert HSTS not in local.get("/").headers
    app = make(cloud(settings))
    # The proxy says it was http: the answer is the redirect to https (R-04), and it carries no HSTS itself.
    with TestClient(app, headers={**RECRUITER, "X-Forwarded-Proto": "http"}, follow_redirects=False) as c:
        r = c.get("/api/health")
        assert r.status_code == 308 and HSTS not in r.headers
