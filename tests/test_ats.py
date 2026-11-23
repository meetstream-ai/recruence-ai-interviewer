"""External ATS integrations (app/ats): webhooks in, Recruence's own pipeline, results out.

MOCKED: no Greenhouse, Lever or other ATS account is used here. The providers' APIs are httpx.MockTransport
handlers that answer the way their published documentation describes, and webhooks are signed here with test
secrets. These tests check Recruence's side (signatures, mapping, de-duplication, the pipeline, retries, isolation); they
don't prove the adapters work against the live services.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import replace

import httpx
import pytest

from app.ats import ADAPTERS
from app.ats.base import AdapterError, Result, http_error, result_text, url_problem
from app.ats.generic import sign
from app.ats.service import IntegrationService, Pipeline
from cryptography.fernet import Fernet

from app.secret_box import SecretBox
from app.store import CURRENT_WORKSPACE, Store
from tests.test_api import TUNNEL, wait_for
from tests.test_recruiting import CRITERIA, RESUME, apply, client, published_role
from tests.test_roles import evaluated

GH_SECRET = "gh-test-secret-0123456789"
LEVER_SECRET = "lever-test-signature-token"
GENERIC_SECRET = "generic-test-secret-0123456789"
RESUME_URL = "https://files.example-ats.test/resume/77.txt"


class FakeATS:
    """One MockTransport for every provider URL the tests use; records what Recruence sent."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.callback_status = [200]  # answers for the generic callback, in order (the last one repeats)
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if self.down:
            raise httpx.ConnectError("down", request=request)
        if url == RESUME_URL:
            return httpx.Response(200, content=RESUME)
        if "harvest.greenhouse.io" in url:
            if not request.headers.get("authorization", "").startswith("Basic "):
                return httpx.Response(401)
            if url.endswith("/activity_feed/notes"):
                return httpx.Response(201, json={"id": 1})
            if "/users" in url:
                return httpx.Response(200, json=[{"id": 1}])
        if "api.lever.co" in url:
            if "/opportunities/opp-1/resumes/r-1/download" in url:
                return httpx.Response(200, content=b"Sam Okafor. B.Tech in Computer Science. Five years of Python services.")
            if url.endswith("/opportunities/opp-1/resumes"):
                return httpx.Response(200, json={"data": [{"id": "r-1", "file": {"name": "sam.txt"}}]})
            if url.endswith("/opportunities/opp-1"):
                return httpx.Response(200, json={"data": {"name": "Sam Okafor", "emails": ["sam@example.com"], "headline": "Engineer at Acme",
                                                          "location": "Lagos", "links": [], "urls": {"show": "https://hire.lever.co/candidates/opp-1"}}})
            if "/postings/post-9" in url:
                return httpx.Response(200, json={"data": {"text": "Platform Engineer", "content": {"description": "Run the platform."}}})
            if "/notes" in url:
                return httpx.Response(201, json={"data": {}})
        if url.startswith("https://hooks.example-ats.test/"):
            status = self.callback_status.pop(0) if len(self.callback_status) > 1 else self.callback_status[0]
            return httpx.Response(status)
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def ats_hosts_resolve(monkeypatch):
    """The made-up ATS hosts here (*.example-ats.test) don't resolve; the network is a MockTransport. Resolution is
    tested on its own below."""
    from app.ats import service as service_module

    async def public(url):
        return "private" not in url
    monkeypatch.setattr(service_module, "resolves_publicly", public)


def ats_client(settings, **kw):
    app, c = client(replace(settings, dashboard_url="https://hiring.example.com"), **kw)
    fake = FakeATS()
    svc = app.state.integrations
    svc.http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    svc.backoff = 0
    return app, c, fake


def connect(c, provider, settings=None, secrets=None, name=""):
    r = c.post("/api/integrations", json={"provider": provider, "name": name, "settings": settings or {}, "secrets": secrets or {}})
    assert r.status_code == 201, r.text
    return r.json()


def gh_payload(app_id="901", email="priya@example.com", job_id="4001", action="new_candidate_application", stage="Application Review"):
    return {"action": action, "payload": {"application": {
        "id": int(app_id), "current_stage": {"id": sum(map(ord, stage)), "name": stage},
        "candidate": {"id": 77, "first_name": "Priya", "last_name": "Raman", "title": "Engineer", "company": "Fintech",
                      "email_addresses": [{"value": email, "type": "personal"}], "phone_numbers": [{"value": "+49 30 1234"}],
                      "website_addresses": [{"value": "https://priya.example.dev"}],
                      "attachments": [{"type": "resume", "filename": "cv.txt", "url": RESUME_URL}]},
        "jobs": [{"id": int(job_id), "name": "Backend Engineer"}],
        "answers": [{"question": "Notice period?", "answer": "One month"}]}}}


def post_gh(c, hook, payload, secret=GH_SECRET):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return c.post(hook["webhook_path"], content=body, headers={**TUNNEL, "Signature": f"sha256 {sig}", "Content-Type": "application/json"})


def post_generic(c, hook, payload, secret=GENERIC_SECRET, ts=None):
    body = json.dumps(payload).encode()
    ts = str(ts or int(time.time()))
    return c.post(hook["webhook_path"], content=body, headers={**TUNNEL, "X-MIA-Timestamp": ts, "X-MIA-Signature": sign(secret, ts, body),
                                                                "Content-Type": "application/json"})


def applications(store):
    return store.all_applications()


# ---------- native ATS ----------

def test_native_flow_questions_answers_notes_and_candidate_search(settings):
    app, c, _ = ats_client(settings)
    store = app.state.store
    with c:
        rid, _, token = published_role(c, questions=[{"text": "Are you authorised to work in Germany?", "required": True},
                                                     {"text": "Portfolio link", "required": False}])
        page = c.get(f"/api/public/apply/{token}", headers=TUNNEL).json()
        assert [q["id"] for q in page["questions"]] == ["a1", "a2"] and page["questions"][0]["required"]
        assert apply(c, token).status_code == 422  # the required question
        assert apply(c, token, q_a1="Yes, EU citizen").status_code == 201
        a = wait_for(lambda: (x := applications(store)) and x[0].status == "invited" and x[0])
        assert a.source == "careers_site" and a.external is None and a.sync is None
        assert [(x.question, x.answer) for x in a.answers] == [("Are you authorised to work in Germany?", "Yes, EU citizen"), ("Portfolio link", "")]

        note = c.post(f"/api/applications/{a.id}/notes", json={"text": "Strong systems answers in the phone screen."})
        assert note.status_code == 201 and note.json()["application"]["notes"][0]["text"].startswith("Strong")
        assert c.post(f"/api/applications/{a.id}/notes", json={"text": ""}).status_code == 422

        found = c.get("/api/candidates", params={"q": "priya"}).json()
        assert found["total"] == 1 and found["candidates"][0]["applications"][0]["role_id"] == rid
        assert c.get("/api/candidates", params={"source": "ats"}).json()["total"] == 0
        assert c.get("/api/candidates", params={"q": "nobody"}).json()["total"] == 0


def test_careers_site_applications_are_marked(settings):
    app, c, _ = ats_client(replace(settings, public_cors_origins=("https://careers.example.org",)))
    with c:
        _, _, token = published_role(c)
        r = c.post(f"/api/public/apply/{token}", data={"full_name": "Lee Park", "email": "lee@example.com", "consent": "yes"},
                   files={"resume": ("cv.txt", RESUME, "text/plain")}, headers={**TUNNEL, "Origin": "https://careers.example.org"})
        assert r.status_code == 201
        assert applications(app.state.store)[0].source == "careers_site"


# ---------- external ATS: inbound ----------

def test_greenhouse_candidate_goes_through_the_same_pipeline_once(settings):
    app, c, fake = ats_client(settings)
    store = app.state.store
    with c:
        rid, _, _ = published_role(c)
        hook = connect(c, "greenhouse", {"role_map": {"4001": rid}, "on_behalf_of_user_id": "12"}, {"webhook_secret": GH_SECRET, "api_key": "gh-key"})
        assert hook["secrets"] == {"webhook_secret": True, "api_key": True} and "gh-key" not in json.dumps(c.get("/api/integrations").json())

        assert post_gh(c, hook, {"action": "ping", "payload": {"web_hook_id": 3}}).json()["ignored"]
        assert post_gh(c, hook, gh_payload(), secret="wrong-secret").status_code == 401
        r = post_gh(c, hook, gh_payload())
        assert r.status_code == 200 and r.json()["accepted"] == 1, r.text
        a = wait_for(lambda: (x := applications(store)) and x[0].status == "invited" and x[0])  # Recruence screened it and invited
        assert a.role_id == rid and a.source == "ats" and a.candidate.full_name == "Priya Raman" and a.candidate.email == "priya@example.com"
        assert a.external.provider == "greenhouse" and a.external.application_id == "901" and a.external.candidate_id == "77" and a.external.job_id == "4001"
        assert a.resume_text.startswith("Priya Raman") and a.resume_filename == "cv.txt"
        assert [(x.question, x.answer) for x in a.answers] == [("Notice period?", "One month")]
        assert a.screening.outcome == "eligible" and a.interview_id

        # The same delivery again (Greenhouse retries), and the same application in a later delivery: nothing new.
        assert post_gh(c, hook, gh_payload()).json()["duplicate"] is True
        again = post_gh(c, hook, gh_payload(stage="Phone Screen"))  # a different event for the same ATS application
        assert again.status_code == 200 and "already in Recruence" in again.json()["results"][0] and "Priya" not in again.text
        assert len(applications(store)) == 1 and len(store.list_interviews()) == 1

        log = c.get(f"/api/integrations/{hook['id']}/events").json()["events"]
        assert {e["status"] for e in log} >= {"ignored", "rejected", "ok", "duplicate"}
        assert "Priya" not in json.dumps([e for e in log if e["status"] == "rejected"])  # rejected bodies aren't kept


def test_a_person_who_applied_on_the_page_is_linked_not_duplicated(settings):
    app, c, _ = ats_client(settings)
    store = app.state.store
    with c:
        rid, _, token = published_role(c)
        assert apply(c, token).status_code == 201
        first = wait_for(lambda: (x := applications(store)) and x[0].status == "invited" and x[0])
        hook = connect(c, "greenhouse", {"role_map": {"4001": rid}}, {"webhook_secret": GH_SECRET})
        r = post_gh(c, hook, gh_payload(email="PRIYA@example.com"))
        assert r.json()["accepted"] == 1 and "not added again" in r.json()["results"][0]
        apps = applications(store)
        assert len(apps) == 1 and apps[0].id == first.id and apps[0].external.application_id == "901" and apps[0].source == "careers_site"
        assert len(store.list_interviews()) == 1  # no second invitation


def test_one_person_can_apply_for_several_roles(settings):
    app, c, _ = ats_client(settings)
    store = app.state.store
    with c:
        rid1, _, _ = published_role(c)
        rid2, _, _ = published_role(c)
        hook = connect(c, "greenhouse", {"role_map": {"4001": rid1, "4002": rid2}}, {"webhook_secret": GH_SECRET})
        assert post_gh(c, hook, gh_payload(app_id="901", job_id="4001")).json()["accepted"] == 1
        assert post_gh(c, hook, gh_payload(app_id="902", job_id="4002")).json()["accepted"] == 1
        wait_for(lambda: all(a.status == "invited" for a in applications(store)) and len(applications(store)) == 2)
        people = c.get("/api/candidates", params={"source": "ats"}).json()
        assert people["total"] == 1 and {x["role_id"] for x in people["candidates"][0]["applications"]} == {rid1, rid2}


def test_trusting_the_ats_screening_and_an_unmapped_job(settings):
    app, c, _ = ats_client(settings)
    store = app.state.store
    with c:
        hook = connect(c, "greenhouse", {"screening": "trust_ats", "stage_name": "AI Interview"}, {"webhook_secret": GH_SECRET})
        assert post_gh(c, hook, gh_payload()).json()["ignored"] == "waiting for the stage change instead"
        assert post_gh(c, hook, gh_payload(action="candidate_stage_change", stage="Phone Screen")).json()["ignored"]
        r = post_gh(c, hook, gh_payload(action="candidate_stage_change", stage="AI Interview", job_id="5555"))
        assert r.json()["accepted"] == 1, r.text
        a = applications(store)[0]
        role = store.get_role(a.role_id)
        assert role.setup.role_title == "Backend Engineer" and not role.hiring.published  # a draft role, from the ATS job
        assert c.get("/api/integrations").json()["integrations"][0]["settings"]["role_map"] == {"5555": role.id}
        # Eligible because the ATS said so (no AI screening); not invited, because the draft role has no questions or room yet.
        assert a.screening.outcome == "eligible" and a.screening.screened_by == "greenhouse" and a.status == "eligible"
        assert a.review_note.startswith("Not invited") and a.interview_id is None


def test_lever_details_come_from_its_api(settings):
    app, c, fake = ats_client(settings)
    store = app.state.store
    with c:
        rid, _, _ = published_role(c)
        hook = connect(c, "lever", {"role_map": {"post-9": rid}, "perform_as_user_id": "u-1"}, {"webhook_secret": LEVER_SECRET, "api_key": "lv-key"})
        token, triggered = "tok-abc", 1700000000000
        signature = hmac.new(LEVER_SECRET.encode(), f"{token}{triggered}".encode(), hashlib.sha256).hexdigest()
        payload = {"event": "applicationCreated", "triggeredAt": triggered, "token": token, "signature": signature,
                   "data": {"opportunityId": "opp-1", "applicationId": "app-1", "postingId": "post-9"}}
        assert c.post(hook["webhook_path"], json={**payload, "signature": "0" * 64}, headers=TUNNEL).status_code == 401
        r = c.post(hook["webhook_path"], json=payload, headers=TUNNEL)
        assert r.status_code == 200 and r.json()["accepted"] == 1, r.text
        a = wait_for(lambda: (x := applications(store)) and x[0].status != "screening" and x[0])
        assert a.status == "invited", (a.status, a.screening, a.review_note)
        assert a.candidate.full_name == "Sam Okafor" and a.candidate.email == "sam@example.com" and a.resume_text.startswith("Sam Okafor")
        assert a.external.url == "https://hire.lever.co/candidates/opp-1" and a.external.candidate_id == "opp-1"
        assert all(rq.headers["authorization"].startswith("Basic ") for rq in fake.requests if "lever" in str(rq.url))


def test_generic_webhooks_signature_replay_and_bad_fields(settings):
    app, c, _ = ats_client(settings)
    store = app.state.store
    with c:
        rid, _, _ = published_role(c)
        hook = connect(c, "generic", {"role_map": {"J-1": rid}}, {"webhook_secret": GENERIC_SECRET})
        good = {"event_id": "evt-1", "job": {"id": "J-1"}, "application": {"id": "A-1"},
                "candidate": {"name": "Ana Silva", "email": "ana@example.com"}, "resume": {"text": "Ana Silva. Computer science degree. Python for six years."}}
        assert post_generic(c, hook, good, ts=int(time.time()) - 3600).status_code == 401  # too old: a replay
        assert c.post(hook["webhook_path"], json=good, headers=TUNNEL).status_code == 401  # unsigned
        assert post_generic(c, hook, {"event_id": "evt-0", "candidate": {}}).status_code == 422  # no application id
        no_email = post_generic(c, hook, {**good, "event_id": "evt-2", "application": {"id": "A-2"}, "candidate": {"name": "X"}})
        assert no_email.json()["accepted"] == 0 and "no email" in no_email.json()["results"][0]
        assert post_generic(c, hook, good).json()["accepted"] == 1
        assert wait_for(lambda: (x := applications(store)) and x[0].status != "screening" and x[0]).source == "ats"
        assert c.post("/webhooks/ats/" + "x" * 32, content=b"{}", headers=TUNNEL).status_code == 404

        # A field mapping for another shape.
        c.put(f"/api/integrations/{hook['id']}", json={"settings": {"field_map": {"email": "who.mail", "full_name": "who.n", "application_id": "ref"}}})
        mapped = post_generic(c, hook, {"event_id": "evt-3", "ref": "A-3", "job": {"id": "J-1"}, "who": {"mail": "lee@example.com", "n": "Lee Park"}})
        assert mapped.json()["accepted"] == 1
        assert {a.candidate.email for a in applications(store)} == {"ana@example.com", "lee@example.com"}
        assert c.put(f"/api/integrations/{hook['id']}", json={"settings": {"field_map": {"nope": "x"}}}).status_code == 422


def test_a_paused_integration_takes_nothing(settings):
    app, c, _ = ats_client(settings)
    with c:
        hook = connect(c, "greenhouse", {}, {"webhook_secret": GH_SECRET})
        c.put(f"/api/integrations/{hook['id']}", json={"enabled": False})
        assert post_gh(c, hook, gh_payload()).status_code == 404
        assert applications(app.state.store) == []


# ---------- external ATS: results out ----------

def ats_candidate_interviewed(c, app, settings_for_hook):
    store = app.state.store
    rid, plan, _ = published_role(c)
    hook = connect(c, "generic", {"role_map": {"J-1": rid}, "callback_url": "https://hooks.example-ats.test/mia", **settings_for_hook},
                   {"webhook_secret": GENERIC_SECRET})
    post_generic(c, hook, {"event_id": "evt-1", "job": {"id": "J-1"}, "application": {"id": "A-1"},
                           "candidate": {"name": "Ana Silva", "email": "ana@example.com"}, "resume": {"text": RESUME.decode()}})
    a = wait_for(lambda: (x := applications(store)) and x[0].interview_id and x[0])
    evaluated(store, a.interview_id, "Ana Silva", [4, 4], "meets", [x["id"] for x in plan["competencies"]])
    return hook, a


def test_results_go_back_signed_and_failures_are_retried(settings):
    app, c, fake = ats_client(settings)
    store, svc = app.state.store, app.state.integrations
    with c:
        hook, a = ats_candidate_interviewed(c, app, {"status_map": {"passed": "Advance to onsite"}})
        fake.callback_status = [503, 503, 503, 200]
        c.portal.call(svc._send_for_interview, a.interview_id)
        app_now = store.get_application(a.id)
        assert app_now.sync.status == "failed" and "503" in app_now.sync.message
        out = [e for e in store.integration_events(hook["id"]) if e["direction"] == "out"]
        assert out[0]["status"] == "failed" and out[0]["attempts"] == 3

        assert c.post(f"/api/integrations/{hook['id']}/retry").json()["sent"] == 1
        assert store.get_application(a.id).sync.status == "synced"
        sent = [r for r in fake.requests if str(r.url).startswith("https://hooks.example-ats.test/")][-1]
        ts, body = sent.headers["x-mia-timestamp"], sent.content
        assert sent.headers["x-mia-signature"] == sign(GENERIC_SECRET, ts, body)
        result = json.loads(body)["result"]
        assert result["stage"] == "passed" and result["mapped_status"] == "Advance to onsite" and result["score"] == 80
        # The dashboard's own address: the public one refuses the dashboard.
        assert result["report_url"].startswith("https://hiring.example.com/#/s/") and json.loads(body)["application"]["application_id"] == "A-1"

        # The same result isn't sent twice; a recruiter's decision is a new one.
        before = len(fake.requests)
        c.portal.call(svc._send_for_interview, a.interview_id)
        assert len(fake.requests) == before
        assert c.post(f"/api/interviews/{a.interview_id}/decision", json={"decision": "advance"}).status_code == 200
        wait_for(lambda: len(fake.requests) > before)
        assert json.loads(fake.requests[-1].content)["result"]["decision"] == "advance"


def test_greenhouse_result_note_and_a_provider_that_is_down(settings):
    app, c, fake = ats_client(settings)
    store, svc = app.state.store, app.state.integrations
    with c:
        rid, plan, token = published_role(c)
        hook = connect(c, "greenhouse", {"role_map": {"4001": rid}, "on_behalf_of_user_id": "12"}, {"webhook_secret": GH_SECRET, "api_key": "gh-key"})
        post_gh(c, hook, gh_payload())
        a = wait_for(lambda: (x := applications(store)) and x[0].interview_id and x[0])
        evaluated(store, a.interview_id, "Priya Raman", [3, 3], "meets", [x["id"] for x in plan["competencies"]])
        fake.down = True
        c.portal.call(svc._send_for_interview, a.interview_id)
        assert store.get_application(a.id).sync.status == "failed"
        # Recruence's own applications keep working while the ATS is unreachable.
        assert apply(c, token, email="lee@example.com", full_name="Lee Park").status_code == 201
        wait_for(lambda: len([x for x in applications(store) if x.status == "invited"]) == 2)
        fake.down = False
        assert c.post(f"/api/integrations/{hook['id']}/retry").json()["sent"] == 1
        note = [r for r in fake.requests if str(r.url).endswith("/candidates/77/activity_feed/notes")][-1]
        assert note.headers["on-behalf-of"] == "12"
        text = json.loads(note.content)["body"]
        assert "Recruence AI interview: not passed" in text and "Score: 60/100" in text and "advisory" in text
        assert store.get_application(a.id).sync.status == "synced"


def test_results_that_cant_succeed_are_not_retried(settings):
    app, c, fake = ats_client(settings)
    store, svc = app.state.store, app.state.integrations
    with c:
        rid, plan, _ = published_role(c)
        hook = connect(c, "greenhouse", {"role_map": {"4001": rid}}, {"webhook_secret": GH_SECRET, "api_key": "gh-key"})  # no user id
        post_gh(c, hook, gh_payload())
        a = wait_for(lambda: (x := applications(store)) and x[0].interview_id and x[0])
        evaluated(store, a.interview_id, "Priya Raman", [4, 4], "meets", [x["id"] for x in plan["competencies"]])
        c.portal.call(svc._send_for_interview, a.interview_id)
        assert "user id" in store.get_application(a.id).sync.message
        assert c.post(f"/api/integrations/{hook['id']}/retry").json()["sent"] == 0  # fixing the setting is needed first


# ---------- configuration, permissions, isolation ----------

def test_credentials_are_encrypted_and_settings_checked(settings):
    app, c, _ = ats_client(settings)
    store = app.state.store
    with c:
        hook = connect(c, "greenhouse", {}, {"webhook_secret": GH_SECRET, "api_key": "gh-key-plain"})
        raw = store.integration(hook["id"])["data"]
        assert "gh-key-plain" not in json.dumps(raw) and GH_SECRET not in json.dumps(raw)
        # "" keeps a saved credential, null removes it.
        c.put(f"/api/integrations/{hook['id']}", json={"secrets": {"api_key": ""}})
        assert c.get("/api/integrations").json()["integrations"][0]["secrets"]["api_key"] is True
        c.put(f"/api/integrations/{hook['id']}", json={"secrets": {"api_key": None}})
        assert c.get("/api/integrations").json()["integrations"][0]["secrets"]["api_key"] is False
        for bad in ({"callback_url": "http://hooks.example.com"}, {"api_base": "https://127.0.0.1/v1"}, {"role_map": {"1": "no-such-role"}},
                    {"status_map": {"unknown": "x"}}, {"screening": "maybe"}):
            assert c.put(f"/api/integrations/{hook['id']}", json={"settings": bad}).status_code == 422, bad
        assert c.post("/api/integrations", json={"provider": "workday"}).status_code == 422
        assert c.post(f"/api/integrations/{hook['id']}/test").json()["ok"] is False  # no API key now
        c.put(f"/api/integrations/{hook['id']}", json={"secrets": {"api_key": "gh-key-2"}})
        assert c.post(f"/api/integrations/{hook['id']}/test").json() == {"ok": True, "message": "Connected to the Harvest API; the API key works."}
        assert c.delete(f"/api/integrations/{hook['id']}").json()["deleted"]
        assert c.get("/api/integrations").json()["integrations"] == []


async def test_integrations_and_their_candidates_stay_in_their_workspace(tmp_path):
    store = Store(str(tmp_path / "ws.db"))
    fake = FakeATS()
    screened = []
    svc = IntegrationService(store, SecretBox(Fernet.generate_key(), "test"), Pipeline(
        screen=screened.append, invite=None, background=None, report_url=lambda s: "", passing_score=lambda i: 70),
        http=lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    t = CURRENT_WORKSPACE.set("ws-a")
    try:
        row = svc.save(None, "greenhouse", "GH", {}, {"webhook_secret": GH_SECRET})
    finally:
        CURRENT_WORKSPACE.reset(t)
    body = json.dumps(gh_payload()).encode()
    sig = hmac.new(GH_SECRET.encode(), body, hashlib.sha256).hexdigest()
    result = await svc.ingest(row["inbound_token"], {"Signature": f"sha256 {sig}"}, body)
    assert result["accepted"] == 1 and screened
    for ws, visible in (("ws-a", 1), ("ws-b", 0)):
        t = CURRENT_WORKSPACE.set(ws)
        try:
            assert len(store.integrations()) == visible and len(store.all_applications()) == visible and len(store.list_roles()) == visible
            assert (store.integration(row["id"]) is not None) == bool(visible)
        finally:
            CURRENT_WORKSPACE.reset(t)
    store.close()


# ---------- adapters ----------

def test_adapter_parsing_and_error_handling():
    gh = ADAPTERS["greenhouse"]
    assert gh.parse({"action": "ping", "payload": {"web_hook_id": 1}}, {}).ignored
    assert gh.parse({"action": "offer_created", "payload": {}}, {}).ignored
    with pytest.raises(AdapterError):
        gh.parse({"action": "new_candidate_application", "payload": {"application": {}}}, {})
    ev = gh.parse(gh_payload(), {})
    a = ev.applications[0]
    assert (a.full_name, a.email, a.external_job_id, a.resume_url, a.links) == ("Priya Raman", "priya@example.com", "4001", RESUME_URL, "https://priya.example.dev")
    assert ADAPTERS["lever"].parse({"event": "candidateHired", "data": {}}, {}).ignored

    assert http_error("X", httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://a"), response=httpx.Response(503))).retry
    assert not http_error("X", httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://a"), response=httpx.Response(401))).retry
    assert http_error("X", httpx.ConnectError("x")).retry
    # Seen in the B-9 run (12 November): "Couldn't reach The callback URL". Mid-sentence it's lower case.
    assert str(http_error("The callback URL", httpx.ConnectError("x"))) == "Couldn't reach the callback URL: ConnectError"
    assert str(http_error("The callback URL", httpx.HTTPStatusError("x", request=httpx.Request("POST", "https://a"),
                                                                    response=httpx.Response(503)))) == "The callback URL answered 503"
    for bad in ("http://x.example.com", "https://localhost/x", "https://10.0.0.5/x", "https://169.254.169.254/latest"):
        assert url_problem(bad), bad
    assert url_problem("https://harvest.greenhouse.io/v1") is None
    text = result_text(Result("Ana", "Engineer", "passed", "Advance", 82, "yes", "Clear answers.", "https://m.example/#/s/1"))
    assert "Advance" in text and "82/100" in text and "advisory" in text


def test_the_existing_report_webhook_and_ats_sync_are_independent(settings):
    """A native role's candidate has no ATS: nothing is sent anywhere, and nothing fails."""
    app, c, fake = ats_client(settings)
    store, svc = app.state.store, app.state.integrations
    with c:
        rid, plan, token = published_role(c)
        apply(c, token)
        a = wait_for(lambda: (x := applications(store)) and x[0].interview_id and x[0])
        evaluated(store, a.interview_id, "Priya Raman", [4, 4], "meets", [x["id"] for x in plan["competencies"]])
        c.portal.call(svc._send_for_interview, a.interview_id)
        assert fake.requests == [] and store.get_application(a.id).sync is None


def test_only_workspace_admins_manage_sources_and_workspaces_stay_apart(settings):
    from tests.test_accounts import OWNER, make, remote, signup
    app, owner = make(replace(settings))
    with owner:
        owner.post("/api/auth/register", json=OWNER)
        ws_a = owner.get("/api/workspaces/current").json()["id"]
        hook = connect(owner, "greenhouse", {}, {"webhook_secret": GH_SECRET, "api_key": "gh-key"})
        member = remote(app)
        signup(member, "Rhea Recruiter", "rhea@example.com")
        code = owner.post("/api/workspaces/current/code", json={"days": 7}).json()["join_code"]
        member.post("/api/workspaces/join", json={"code": code})
        uid = next(m["id"] for m in owner.get("/api/workspaces/current").json()["members"] if m["email"] == "rhea@example.com")
        owner.post(f"/api/workspaces/current/members/{uid}/decision", json={"approve": True, "role": "recruiter"})
        member.post("/api/workspaces/switch", json={"workspace_id": ws_a})
        # A recruiter sees the sources (never credentials) but can't change them.
        listed = member.get("/api/integrations").json()["integrations"]
        assert [i["id"] for i in listed] == [hook["id"]] and "gh-key" not in json.dumps(listed)
        assert member.post("/api/integrations", json={"provider": "generic"}).status_code == 403
        assert member.put(f"/api/integrations/{hook['id']}", json={"enabled": False}).status_code == 403
        assert member.post(f"/api/integrations/{hook['id']}/test").status_code == 403
        assert member.delete(f"/api/integrations/{hook['id']}").status_code == 403
        # Another workspace doesn't see this one's integration at all.
        owner.post("/api/workspaces", json={"name": "Second company"})
        assert owner.get("/api/integrations").json()["integrations"] == []
        assert owner.get(f"/api/integrations/{hook['id']}/events").status_code == 404
        assert owner.delete(f"/api/integrations/{hook['id']}").status_code == 404



async def test_a_resume_link_to_a_private_address_is_not_fetched():
    """Audit 2026-11-10 (L4): the written host was checked, but a public-looking name could resolve to this network."""
    from app.ats.base import resolves_publicly
    assert await resolves_publicly("https://localhost/resume.pdf") is False  # resolves to loopback
    assert await resolves_publicly("https://127.0.0.1/resume.pdf") is False
    assert await resolves_publicly("https://no-such-host.invalid/resume.pdf") is False  # doesn't resolve at all


def test_each_role_chooses_who_reviews_its_applications(settings):
    from app.models import Hiring

    # Roles saved before the choice existed keep what they did: auto_invite says which it was.
    assert Hiring.model_validate({"auto_invite": False}).resume_review == "manual"
    assert Hiring.model_validate({"auto_invite": True}).resume_review == "auto"
    assert Hiring.model_validate({"resume_review": "manual", "auto_invite": True}).auto_invite is False

    app, c, _ = ats_client(settings)
    store = app.state.store
    with c:
        rid, _, token = published_role(c, resume_review="manual")
        role = c.get(f"/api/roles/{rid}").json()
        role = role.get("role", role)
        assert role["hiring"]["resume_review"] == "manual" and role["hiring"]["auto_invite"] is False
        # Manual approval: the AI screens it as a suggestion, but nobody is invited until a recruiter says so.
        assert apply(c, token).status_code == 201
        a = wait_for(lambda: (x := applications(store)) and x[0].status != "screening" and x[0])
        assert a.screening.outcome == "eligible" and a.status == "eligible" and a.interview_id is None
        # An older client sending auto_invite still works; an unknown choice is refused.
        assert c.put(f"/api/roles/{rid}/hiring", json={"auto_invite": True}).json()["role"]["hiring"]["resume_review"] == "auto"
        assert c.put(f"/api/roles/{rid}/hiring", json={"resume_review": "manual"}).json()["role"]["hiring"]["auto_invite"] is False
        for wrong in ("ats", "somebody"):
            assert c.put(f"/api/roles/{rid}/hiring", json={"resume_review": wrong}).status_code == 422
        # Automatic: the next applicant is screened and invited without waiting.
        c.put(f"/api/roles/{rid}/hiring", json={"resume_review": "auto"})
        assert apply(c, token, email="lin@example.com").status_code == 201
        b = wait_for(lambda: next((x for x in applications(store) if x.candidate.email == "lin@example.com" and x.status == "invited"), None))
        assert b.screening.outcome == "eligible" and b.interview_id


def test_untested_providers_are_labelled_and_never_ready(settings):
    """Greenhouse and Lever haven't been validated against an organisation's account (B-9): the API says so, so the
    dashboard labels them "Not tested" and doesn't show them as ready. The Generic ATS was verified end to end."""
    _, c, _ = ats_client(settings)
    providers = {p["provider"]: p for p in c.get("/api/integrations/providers").json()}
    for name in ("greenhouse", "lever"):
        assert providers[name]["verified"] is False
        assert providers[name]["status_note"] == "Not tested – Organization account required"
    assert providers["generic"]["verified"] is True and providers["generic"]["status_note"] == ""
    connect(c, "greenhouse", settings={"role_map": {}}, name="GH")
    connect(c, "generic", name="Generic")
    views = {i["provider"]: i for i in c.get("/api/integrations").json()["integrations"]}
    assert views["greenhouse"]["verified"] is False and "Organization account required" in views["greenhouse"]["status_note"]
    assert views["generic"]["verified"] is True
