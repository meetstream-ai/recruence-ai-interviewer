"""A database that stops answering while Recruence runs (R-02, R-03).

R-02: the readiness check (/api/health/ready, the container's health check) must say so with a 503, while
/api/health stays a liveness check. R-03: requests get a clear 503 with the usual security headers, never a bare 500
or a misleading answer (a valid careers key isn't "no longer valid"), and everything works again once it's back.
The outage is simulated by pointing the store at a database file that can't be opened."""

from __future__ import annotations

import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.server import DATABASE_DOWN
from tests.test_cloud import CAREERS, cloud, owner_session, signed_in
from tests.test_resume import make_pdf
from tests.test_tenant_isolation import OWNER


class Outage:
    """Swap the store's engines for one that can't connect, and back."""

    def __init__(self, store, tmp_path):
        self.store = store
        self.saved = (store._engine, store._single)
        self.broken = sa.create_engine(f"sqlite:///{(tmp_path / 'missing-folder' / 'gone.db').as_posix()}")

    def __enter__(self):
        self.store._engine = self.store._single = self.broken
        return self

    def __exit__(self, *_):
        self.store._engine, self.store._single = self.saved


def app_with(settings):
    from app.llm import OfflineAI
    from app.server import create_app
    from app.store import Store
    from tests.fakes import FakeMeetStream
    store = Store(settings.database)
    return create_app(settings=settings, store=store, ai=OfflineAI(), meetstream_client_factory=FakeMeetStream, run_loops=False), store


def test_readiness_reports_the_database_and_health_stays_up(settings, tmp_path):
    app, store = app_with(settings)
    with TestClient(app) as c:
        r = c.get("/api/health/ready")
        assert r.status_code == 200 and r.json()["ok"] and r.json()["database"]["ok"]
        with Outage(store, tmp_path):
            r = c.get("/api/health/ready")
            assert r.status_code == 503
            assert r.json() == {"ok": False, "database": {"ok": False, "problem": DATABASE_DOWN}}
            assert c.get("/api/health").status_code == 200  # liveness: the desktop app and dashboard start on it
        assert c.get("/api/health/ready").status_code == 200  # back by itself


def test_requests_during_an_outage_get_a_clear_503_and_recover(settings, tmp_path):
    app, store = app_with(cloud(settings))
    owner_session(app)
    c, _ = signed_in(app)
    key = c.post("/api/workspaces/current/careers-key").json()["careers_path"]
    site = TestClient(app, base_url="https://testserver", headers=CAREERS)
    login = {"email": OWNER["email"], "password": OWNER["password"]}
    with Outage(store, tmp_path):
        for r in (TestClient(app, base_url="https://testserver", headers=CAREERS).post("/api/auth/account-login", json=login),  # sign-in
                  c.get("/api/roles"),  # signed in: the guard reads the account
                  site.get(key),  # a valid careers key isn't reported as invalid
                  site.post(key, data={"role": "X", "name": "Ann", "email": "ann@example.com"},
                            files={"resume": ("a.pdf", make_pdf(["Ann"]), "application/pdf")})):
            assert r.status_code == 503, (r.request.url.path, r.status_code, r.text)
            assert r.json() == {"detail": DATABASE_DOWN}
            assert "Traceback" not in r.text and "sqlite" not in r.text.lower()
            assert r.headers["X-Frame-Options"] == "DENY" and r.headers["Retry-After"] == "30"
            assert r.headers["Strict-Transport-Security"].startswith("max-age=")  # reached over HTTPS via the proxy
    assert c.get("/api/roles").status_code == 200
    assert site.get(key).status_code == 200
    assert TestClient(app, base_url="https://testserver", headers=CAREERS).post("/api/auth/account-login", json=login).status_code == 200
