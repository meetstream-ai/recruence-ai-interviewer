"""B-9 end to end over real HTTP: a throwaway Recruence (temp SQLite, fake MeetStream, scripted screening; nothing paid,
no real data) and an independent receiver that checks every signature with its own HMAC code.

  python scripts/b9_webhooks_e2e.py                                         # no tunnel: the callback's failure path
  python scripts/b9_webhooks_e2e.py https://<tunnel to 127.0.0.1:9310>      # with a tunnel: the callback succeeds

1. Report webhook (REPORT_WEBHOOK_URL, http to this machine): report.completed and decision.recorded, signed; a 503 is
   retried with the same delivery id; a 400 isn't retried; the transcript is left out by default.
2. Generic ATS: a signed application in (bad signature and a replayed timestamp refused), the same pipeline
   (screening, invitation, interview, report), the result back to a public https callback (the receiver's tunnel),
   signed, with the mapped status; the recruiter's decision sent as a new result; a 503 retried then synced.
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request, Response  # noqa: E402

from app.config import Settings  # noqa: E402
from app.server import create_app  # noqa: E402
from app.store import Store  # noqa: E402
from tests.fakes import MEET_LINK, FakeMeetStream, event, sentence  # noqa: E402
from tests.test_recruiting import ScreeningAI, published_role  # noqa: E402
from tests.ui.test_dashboard_ui import serve  # noqa: E402

TUNNEL_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else ""
# Without a tunnel the ATS callback goes to an address that doesn't exist: the failure path (no delivery, retries, sync failed).
CALLBACK = f"{TUNNEL_URL}/ats-callback" if TUNNEL_URL else "https://ats-callback.b9-test.invalid/ats-callback"
RECEIVER_PORT = 9310
REPORT_SECRET = "b9-report-secret-" + os.urandom(8).hex()
ATS_SECRET = "b9-generic-secret-" + os.urandom(8).hex()
FWD = {"X-Forwarded-For": "34.230.88.153"}  # as MeetStream / an ATS reach the public webhook paths
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def independent_sig(secret: str, ts: str, body: bytes) -> str:  # the documented scheme, written here from scratch
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


# ---------- the receiver ----------
received: list[dict] = []
plan: dict[str, list[int]] = {"/report": [], "/ats-callback": []}  # status codes to answer next, per path
recv = FastAPI()


async def take(request: Request, path: str, secret: str) -> Response:
    body = await request.body()
    ts = request.headers.get("x-mia-timestamp", "")
    ok_sig = hmac.compare_digest(request.headers.get("x-mia-signature", ""), independent_sig(secret, ts, body))
    fresh = ts.isdigit() and abs(time.time() - int(ts)) < 300
    status = plan[path].pop(0) if plan[path] else 200
    received.append({"path": path, "status": status, "sig_ok": ok_sig, "fresh": fresh, "event": request.headers.get("x-mia-event"),
                     "delivery": request.headers.get("x-mia-delivery"), "via": request.headers.get("cf-connecting-ip", "direct"),
                     "body": json.loads(body or b"{}")})
    return Response(status_code=status)


@recv.post("/report")
async def report_hook(request: Request):
    return await take(request, "/report", REPORT_SECRET)


@recv.post("/ats-callback")
async def ats_hook(request: Request):
    return await take(request, "/ats-callback", ATS_SECRET)


@recv.get("/health")
async def health():
    return {"ok": True}


def wait_for(fn, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    return None


def got(path, event=None):
    return [r for r in received if r["path"] == path and (event is None or r["event"] == event)]


srv = uvicorn.Server(uvicorn.Config(recv, host="127.0.0.1", port=RECEIVER_PORT, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
wait_for(lambda: srv.started, 10)
if TUNNEL_URL:
    check("Receiver reachable through its public tunnel", wait_for(lambda: httpx.get(f"{TUNNEL_URL}/health", timeout=10).status_code == 200, 60), TUNNEL_URL)

# ---------- a throwaway Recruence ----------
tmp = Path(tempfile.mkdtemp(prefix="b9-"))
settings = Settings(
    data_dir=tmp, database_url="", log_level="WARNING", meetstream_api_key="ms-key", public_base_url="https://tunnel.example.com",
    dashboard_url="https://hiring.example.com", scheduler_interval_seconds=3600, goodbye_grace_seconds=0.01, postcall_transcript=False,
    check_public_url=False, allow_shared_sqlite=True,
    mia_stt_provider="xai", mia_llm_provider="xai", mia_llm_model="grok-4.20-0309-non-reasoning", mia_tts_provider="xai", mia_voice="ara",
    report_webhook_url=f"http://127.0.0.1:{RECEIVER_PORT}/report", report_webhook_secret=REPORT_SECRET,
)
fake = FakeMeetStream()
app = create_app(settings=settings, store=Store(settings.database), ai=ScreeningAI(), meetstream_client_factory=lambda: fake, run_loops=False)
app.state.integrations.backoff = 0.5  # quicker ATS retries for the test
app.state.exporter._backoff_s = 0.5
with serve(app) as base:
    c = httpx.Client(base_url=base, timeout=30)
    rid, _, _ = published_role(c)
    r = c.post("/api/integrations", json={"provider": "generic", "name": "Test ATS", "settings": {
        "role_map": {"J-1": rid}, "callback_url": CALLBACK, "status_map": {"passed": "Advance to onsite"}},
        "secrets": {"webhook_secret": ATS_SECRET}})
    check("Generic integration created with a public https callback", r.status_code == 201, f"HTTP {r.status_code}")
    hook = r.json()
    bad = c.post("/api/integrations", json={"provider": "generic", "settings": {"callback_url": "http://127.0.0.1:9310/ats-callback"},
                                            "secrets": {"webhook_secret": ATS_SECRET}})
    check("A private http callback is refused", bad.status_code == 422, f"HTTP {bad.status_code}: {bad.json().get('detail', '')[:70]}")

    # Inbound: an application from the "ATS", signed independently.
    payload = {"event_id": "evt-b9-1", "job": {"id": "J-1", "title": "Backend Engineer"}, "application": {"id": "A-B9-1"},
               "candidate": {"id": "C-B9", "name": "Ana Silva", "email": "ana@example.com"},
               "resume": {"text": "Ana Silva. B.Tech in Computer Science, 2018. Five years of Python services at a fintech."}}
    body = json.dumps(payload).encode()

    def post_in(ts, secret=ATS_SECRET):
        return c.post(hook["webhook_path"], content=body, headers={**FWD, "Content-Type": "application/json",
                                                                    "X-MIA-Timestamp": str(ts), "X-MIA-Signature": independent_sig(secret, str(ts), body)})
    check("Inbound: wrong signature refused", post_in(int(time.time()), "wrong-secret-0123456789").status_code == 401)
    check("Inbound: replayed (1 h old) timestamp refused", post_in(int(time.time()) - 3600).status_code == 401)
    ok = post_in(int(time.time()))
    check("Inbound: signed application accepted", ok.status_code == 200 and ok.json().get("accepted") == 1, ok.text[:120])
    dup = post_in(int(time.time()))
    check("Inbound: the same event again isn't a second application", dup.status_code == 200 and dup.json().get("accepted", 0) == 0, dup.text[:120])

    cand = wait_for(lambda: (x := [a for a in c.get(f"/api/roles/{rid}/pipeline").json()["candidates"] if a.get("interview_id")]) and x[0], 30)
    check("Same pipeline: screened and invited", bool(cand), f"status {cand and cand.get('status')}")
    iid = cand["interview_id"]

    # The interview, driven through the real webhook endpoints (fake MeetStream: no bot, no cost).
    sid = c.post(f"/api/interviews/{iid}/launch", json={"meeting_link": MEET_LINK, "consent_attested": True}).json()["session_id"]
    live = app.state.manager.live[sid]
    token, tool_token = live.webhook_token, live.runner.tool_token
    wait_for(lambda: fake.bots.created, 10)
    c.post(f"/webhooks/meetstream/{token}/events", json=event("bot.inmeeting"), headers=FWD)
    wait_for(lambda: c.get(f"/api/sessions/{sid}").json()["snapshot"]["state"] == "awaiting_consent", 15)
    auth = {**FWD, "Authorization": f"Bearer {tool_token}", "X-Bot-ID": "bot-123"}

    def tool(name, args):
        return c.post(f"/webhooks/mia/{token}/{name}", json={"name": name, "bot": {"bot_id": "bot-123"}, "args": args}, headers=auth).json()
    tool("next_question", {"candidate_consented": True})
    c.post(f"/webhooks/meetstream/{token}/transcript", json=sentence("I build Python services and led a PostgreSQL migration.", speaker="Ana Silva"), headers=FWD)
    prev = "intro"
    while True:
        res = tool("next_question", {"previous_question_id": prev, "answer_note": "Concrete answer.", "follow_ups_asked": 0})
        if res["status"] != "question":
            break
        c.post(f"/webhooks/meetstream/{token}/transcript", json=sentence(f"My answer to {res['question_id']}: I owned it end to end.", speaker="Ana Silva"), headers=FWD)
        prev = res["question_id"]
    tool("end_interview", {"reason": "completed"})
    rep = wait_for(lambda: (s := c.get(f"/api/sessions/{sid}").json())["report"], 30)
    check("Interview completed and report stored", bool(rep), f"end_reason {rep and rep['end_reason']}")

    # 1. Report webhook: report.completed
    r1 = wait_for(lambda: got("/report", "report.completed"), 20)
    if r1:
        x = r1[-1]
        check("Report webhook: report.completed delivered over HTTP", x["status"] == 200)
        check("Report webhook: signature valid (independent HMAC) and timestamp fresh", x["sig_ok"] and x["fresh"])
        d = x["body"]["data"]
        check("Report webhook: transcript left out by default", "transcript" not in d and "key_moments" not in d)
        check("Report webhook: payload has the outcome", d.get("session_id") == sid and d.get("consent_given") is True and "evaluation" in d,
              f"evaluated={d.get('evaluated')}")
    else:
        check("Report webhook: report.completed delivered over HTTP", False, "nothing received")

    # 2. ATS result back, through the public https tunnel
    a1 = wait_for(lambda: got("/ats-callback", "interview.result"), 45) if TUNNEL_URL else None
    if not TUNNEL_URL:
        appl = wait_for(lambda: (x := [a for a in app.state.store.all_applications() if a.sync and a.sync.status == "failed"]) and x[0], 60)
        out = [e for e in app.state.store.integration_events(hook["id"]) if e["direction"] == "out"]
        check("ATS result (no tunnel): unreachable callback recorded as failed, after retries",
              bool(appl) and bool(out) and out[0]["status"] == "failed" and out[0]["attempts"] >= 2,
              f"sync={appl and appl.sync.status!r}; {appl and appl.sync.message[:80]!r}; attempts={out and out[0]['attempts']}")
    elif a1:
        x = a1[-1]
        b = x["body"]
        check("ATS result: delivered to the public https callback", x["status"] == 200 and x["via"] != "direct", f"via Cloudflare ({x['via'] != 'direct'})")
        check("ATS result: signature valid (independent HMAC) and timestamp fresh", x["sig_ok"] and x["fresh"])
        check("ATS result: refers to the ATS's own ids", b["application"].get("application_id") == "A-B9-1" and b["application"].get("candidate_id") == "C-B9")
        res = b.get("result", {})
        check("ATS result: stage, mapped status and report link", res.get("stage") in ("passed", "not_passed", "incomplete", "evaluation_failed") and "report_url" in res,
              f"stage {res.get('stage')} mapped {res.get('mapped_status')!r}")
    else:
        check("ATS result: delivered to the public https callback", False, "nothing received in 45 s")

    # 3. A decision: both hooks again; a 503 first on each, retried with the same delivery id
    plan["/report"] = [503]
    plan["/ats-callback"] = [503]
    before_rep, before_ats = len(got("/report")), len(got("/ats-callback"))
    c.post(f"/api/interviews/{iid}/decision", json={"decision": "advance", "note": "B-9 test"})
    dec = wait_for(lambda: [x for x in got("/report", "decision.recorded") if x["status"] == 200], 30)
    tries = [x for x in got("/report", "decision.recorded")]
    check("Report webhook: decision.recorded delivered after a 503", bool(dec), f"{len(tries)} attempt(s)")
    if dec:
        check("Report webhook: the retry kept the same delivery id", len({x["delivery"] for x in tries}) == 1 and len(tries) >= 2)
        check("Report webhook: decision payload", dec[-1]["body"]["data"].get("decision") == "advance" and dec[-1]["sig_ok"])
    ats_dec = wait_for(lambda: [x for x in got("/ats-callback")[before_ats:] if x["status"] == 200], 45) if TUNNEL_URL else None
    if TUNNEL_URL: check("ATS result: the decision sent again after a 503", bool(ats_dec) and ats_dec[-1]["body"]["result"].get("decision") == "advance",
          f"{len(got('/ats-callback')) - before_ats} attempt(s)")

    # 4. A 400 isn't retried
    plan["/report"] = [400, 400, 400]
    n = len(got("/report"))
    c.post(f"/api/interviews/{iid}/decision", json={"decision": "hold", "note": "B-9 test, 400"})
    time.sleep(4)
    check("Report webhook: a 400 isn't retried", len(got("/report")) - n == 1, f"{len(got('/report')) - n} attempt(s)")
    audit = [e["action"] for e in c.get(f"/api/interviews/{iid}").json()["audit"]]
    check("Audit: exports recorded, without content", "report_exported" in audit and "report_export_failed" in audit)
    plan["/report"] = []

srv.should_exit = True
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
