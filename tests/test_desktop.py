"""The desktop backend (app/desktop.py) and the admin-only diagnostics the desktop app's users rely on."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.llm import OfflineAI
from app.server import create_app
from app.store import SCHEMA_VERSION, Store

ROOT = Path(__file__).resolve().parent.parent
OWNER = {"name": "Olive Owner", "email": "olive@example.com", "password": "owner-password-1"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_desktop_backend_starts_answers_and_stops_on_request(tmp_path):
    port = free_port()
    env = {**os.environ, "DATA_DIR": str(tmp_path), "MIA_DESKTOP_PORT": str(port), "MIA_PARENT_PID": str(os.getpid()),
           "DATABASE_URL": "", "LOG_LEVEL": "WARNING", "APP_ACCESS_TOKEN": "", "MEETSTREAM_API_KEY": ""}
    proc = subprocess.Popen([sys.executable, "-m", "app.desktop"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 40
        health = None
        while time.monotonic() < deadline and health is None:
            try:
                health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2))
            except OSError:
                time.sleep(0.3)
        assert health and health["ok"] is True
        assert (tmp_path / "interviewer.db").exists()  # data in the folder the desktop app chose
        (tmp_path / ".stop").write_text("stop")  # what desktop/main.js does on quit
        assert proc.wait(timeout=20) == 0  # a clean shutdown, not a kill
        assert not (tmp_path / ".stop").exists()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_diagnostics_and_readiness_are_for_installation_admins(settings):
    app = create_app(settings=replace(settings), store=Store(settings.database), ai=OfflineAI(), run_loops=False)
    with TestClient(app) as c:
        d = c.get("/api/database/diagnostics").json()
        assert d["connected"] is True and d["schema_version"] == SCHEMA_VERSION and d["schema_ok"] and d["last_ok_at"]
        assert "password" not in json.dumps(d).lower()
        r = c.get("/api/settings/readiness").json()
        by = {i["key"]: i for i in r["items"]}
        assert by["ai"]["status"] == "missing" and r["ready"] is False and "AI provider" in r["blocking"]
        assert by["meetstream"]["status"] == "unchecked" and by["voice"]["status"] == "manual"
        # A recruiter in a workspace sees neither.
        c.post("/api/auth/register", json={**OWNER, "workspace_name": "Acme"})
        code = c.post("/api/workspaces/current/code", json={"days": 1}).json()["join_code"]
        member = TestClient(app, client=("172.17.0.1", 50000))
        member.post("/api/auth/register", json={"name": "Rex Recruiter", "email": "rex@example.com", "password": "recruiter-pass-1"})
        member.post("/api/workspaces/join", json={"code": code})
        uid = next(m["id"] for m in c.get("/api/workspaces/current").json()["members"] if m["status"] == "pending")
        c.post(f"/api/workspaces/current/members/{uid}/decision", json={"approve": True})
        ws = c.get("/api/workspaces/current").json()["id"]
        member.post("/api/workspaces/switch", json={"workspace_id": ws})
        assert member.get("/api/database/diagnostics").status_code == 403
        assert member.get("/api/settings/readiness").status_code == 403
