"""Dashboard browser tests (release validation B-12): the real dashboard in a real browser, against a real Recruence
server on a free port (fresh test database, template plans instead of an AI, a fake MeetStream: nothing external).

Run locally with Microsoft Edge (no browser download):  pytest tests/ui
In CI:  python -m playwright install --with-deps chromium,  RECRUENCE_UI_BROWSER=chromium RECRUENCE_UI_REQUIRED=1 pytest tests/ui
Without Playwright or a browser these tests are skipped, unless RECRUENCE_UI_REQUIRED=1 (then they fail).
"""

from __future__ import annotations

import os
import re
import socket
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest

playwright_api = pytest.importorskip("playwright.sync_api", reason="Playwright isn't installed (pip install playwright)")

from app.llm import OfflineAI  # noqa: E402
from app.server import create_app  # noqa: E402
from app.store import Store  # noqa: E402
from tests.fakes import FakeMeetStream  # noqa: E402

REQUIRED = os.environ.get("RECRUENCE_UI_REQUIRED") == "1"
OWNER = {"name": "Uma Owner", "email": "uma@example.com", "password": "ui-owner-password-1"}
ROOM = "https://meet.google.com/abc-defg-hij"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextmanager
def serve(app):
    import uvicorn
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "the test server didn't start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        thread.join(10)


@pytest.fixture
def server(settings):
    with serve(create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), meetstream_client_factory=FakeMeetStream,
                          run_loops=False)) as url:
        yield url


@pytest.fixture(scope="module")
def browser():
    which = os.environ.get("RECRUENCE_UI_BROWSER") or ("msedge" if sys.platform == "win32" else "chromium")
    try:
        p = playwright_api.sync_playwright().start()
        b = p.chromium.launch(headless=True, **({} if which == "chromium" else {"channel": which}))
    except Exception as e:  # noqa: BLE001 - no browser on this machine
        if REQUIRED:
            raise
        pytest.skip(f"No browser for the UI tests ({which}): {type(e).__name__}")
    yield b
    b.close()
    p.stop()


@pytest.fixture
def page(browser, server):
    context = browser.new_context(base_url=server, viewport={"width": 1366, "height": 900})
    pg = context.new_page()
    pg.set_default_timeout(15_000)
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))  # a JavaScript error anywhere fails the test
    yield pg
    context.close()
    assert not errors, f"JavaScript errors in the dashboard: {errors}"


def finished_setup(page) -> None:
    """The first account and a finished setup, through the API the wizard uses (for tests about later screens)."""
    r = page.request
    assert r.put("/api/setup/state", data={"mode": "personal"}).ok
    assert r.post("/api/auth/register", data={**OWNER, "workspace_name": "Acme Hiring"}).ok
    assert r.put("/api/setup/state", data={"ai_offline": True}).ok
    assert r.post("/api/setup/complete").ok
    page.goto("/?ready=1")  # a real page load: the open page is still showing the setup wizard
    page.get_by_role("heading", name="Dashboard").wait_for()


def published_role(page, title="AI Engineer") -> str:
    r = page.request
    rid = r.post("/api/roles", data={"role_title": title, "job_description": "Build LLM features in Python."}).json()["id"]
    assert r.post(f"/api/roles/{rid}/plan/generate").ok and r.post(f"/api/roles/{rid}/plan/approve").ok
    res = r.put(f"/api/roles/{rid}/hiring", data={"meeting_link": ROOM, "published": True,
                                                  "criteria": [{"text": "Python", "kind": "skill", "mandatory": True}]})
    assert res.ok, res.text()
    return rid


def click(page, name: str) -> None:
    page.get_by_role("button", name=name, exact=True).click()


# ---------- 1. First-run setup, step by step ----------

def test_first_run_setup_ends_on_the_dashboard(page):
    page.goto("/")
    page.get_by_text("How will this install be used?").wait_for()
    page.locator('input[name="setup-mode"][value="personal"]').check()
    click(page, "Continue")
    page.get_by_role("heading", name="Database").wait_for()
    click(page, "Continue")
    page.get_by_role("heading", name="Create your account").wait_for()
    fields = page.locator("#setup-root input")
    for i, value in enumerate([OWNER["name"], OWNER["email"], OWNER["password"], OWNER["password"], "Acme Hiring"]):
        fields.nth(i).fill(value)
    click(page, "Create account")
    page.get_by_text("Teammates join later").wait_for()  # the account exists: this install's owner
    click(page, "Continue")
    page.get_by_role("heading", name="AI provider").wait_for()
    click(page, "Continue without AI (template plans, no evaluations)")
    page.get_by_role("heading", name="MeetStream").wait_for()
    click(page, "Skip for now")
    page.get_by_role("heading", name="Email").wait_for()
    click(page, "Skip for now")
    page.get_by_role("heading", name="Careers website").wait_for()
    page.locator('input[name="careers-site"][value="recruence"]').check()
    page.get_by_text("Create the careers address below").wait_for()
    click(page, "Continue")
    page.get_by_role("heading", name="Review").wait_for()
    assert "Lists the jobs set up in Recruence" in page.locator("#setup-root").inner_text()
    click(page, "Finish setup")
    page.get_by_role("heading", name="Dashboard").wait_for()
    assert "No roles yet" in page.locator("main").inner_text()  # the empty state, not an error


# ---------- 2. A role, from the form to published ----------

def test_a_role_is_created_set_up_and_published_through_the_screens(page):
    page.goto("/")
    finished_setup(page)
    page.goto("/#/roles/new")
    page.locator("#f-role").fill("Data Analyst")
    page.locator("#f-level").fill("Junior")
    page.locator("#f-jd").fill("Analyse clinic scheduling data with SQL and Python.")
    page.locator("#f-count").fill("3")
    click(page, "Create role and generate questions")
    page.wait_for_url(re.compile(r"#/r/\w+/setup\?step=questions"))
    click(page, "Approve plan")
    click(page, "Continue")
    page.get_by_role("heading", name="How are candidates reviewed?").wait_for()
    page.locator('input[name="review"][value="auto"]').check()
    click(page, "Continue")
    page.get_by_role("heading", name="Who leads the interview?").wait_for()
    chat = page.get_by_text("Show each question in the meeting chat")
    assert page.locator("label.check input").first.is_checked(), "questions in the chat should be on by default"
    assert chat.is_visible()
    click(page, "Continue")
    page.locator('input[aria-label="Meeting link"]').fill(ROOM)
    click(page, "Continue")
    page.get_by_text("Check each choice").wait_for()
    page.on("dialog", lambda d: d.accept())
    click(page, "Confirm and publish")
    # Publishing first checks the Public URL; when that check fails it asks "Publish anyway?" (M4). Wait for either.
    anyway = page.get_by_role("button", name="Publish anyway")
    page.get_by_text("Accepting applications").or_(anyway).first.wait_for()
    if anyway.is_visible():
        anyway.click()
    page.get_by_text("Accepting applications").wait_for()
    rid = re.search(r"#/r/(\w+)", page.url).group(1)
    role = page.request.get(f"/api/roles/{rid}").json()
    role = role.get("role", role)
    assert role["hiring"]["published"] and role["hiring"]["options"]["questions_in_chat"] is True


# ---------- 3. An application, the pipeline, interviews grouped by role ----------

def test_an_application_shows_in_the_pipeline_and_interviews_group_by_role(page):
    from tests.test_resume import make_pdf
    page.goto("/")
    finished_setup(page)
    rid = published_role(page)
    path = page.request.post("/api/workspaces/current/careers-key").json()["careers_path"]
    r = page.request.post(path, multipart={"role": "AI Engineer", "name": "Lin Wei", "email": "lin@example.com",
                                           "resume": {"name": "lin.pdf", "mimeType": "application/pdf", "buffer": make_pdf(["Lin Wei", "Python"])}})
    assert r.status == 201, r.text()
    page.goto(f"/#/r/{rid}")
    page.get_by_text("Lin Wei").first.wait_for()  # the application is under the role
    # Without an AI, screening leaves it for a person (Needs review): the recruiter invites her, as on the Review button.
    app_id = next(c["application_id"] for c in page.request.get(f"/api/roles/{rid}/pipeline").json()["candidates"] if c["candidate_name"] == "Lin Wei")
    assert page.request.post(f"/api/applications/{app_id}/decision", data={"action": "invite"}).ok
    page.goto("/#/interviews")
    page.get_by_role("heading", name=re.compile(r"AI Engineer · 1 interview")).wait_for()
    click(page, "Show as one list")
    assert page.get_by_role("heading", name=re.compile(r"AI Engineer · ")).count() == 0
    assert "list=1" in page.url
    click(page, "Group by role")
    page.get_by_role("heading", name=re.compile(r"AI Engineer · 1 interview")).wait_for()


# ---------- 4. Recruitment sources: how the careers website is set up ----------

def test_the_careers_website_choices_are_saved(page):
    page.goto("/")
    finished_setup(page)
    page.goto("/#/settings?tab=sources")
    page.get_by_text("How is your careers website set up?").wait_for()
    page.locator('input[name="careers-site"][value="own"]').check()
    page.get_by_text("When you add a new job to your site").wait_for()
    page.locator('input[name="careers-new-jobs"][value="draft"]').check()
    page.get_by_text("Nothing to do in advance").wait_for()
    page.reload()
    page.get_by_text("How is your careers website set up?").wait_for()
    assert page.locator('input[name="careers-site"][value="own"]').is_checked()
    assert page.locator('input[name="careers-new-jobs"][value="draft"]').is_checked()
    ws = page.request.get("/api/workspaces/current").json()
    assert ws["careers_site"] == "own" and ws["careers_new_jobs"] == "draft"


def test_ats_integrations_are_advanced_and_untested_ones_are_labelled(page):
    """Greenhouse and Lever sit in a collapsed advanced section, labelled "Not tested – Organization account required",
    and a connected one is never shown as Active (owner's decision, 14 November 2026)."""
    page.goto("/")
    finished_setup(page)
    page.goto("/#/settings?tab=sources")
    section = page.locator("details.advanced-integrations")
    section.wait_for()
    assert not section.evaluate("d => d.open")  # collapsed while nothing is connected
    page.get_by_text("Additional integrations (advanced)").click()
    page.get_by_text("Greenhouse and Lever: Not tested – Organization account required").wait_for()
    page.get_by_role("button", name="Connect an ATS").click()
    page.get_by_label("Provider").locator("option").first.wait_for(state="attached")
    options = page.get_by_label("Provider").locator("option").all_inner_texts()
    assert any(o.startswith("Greenhouse (Not tested – Organization account required)") for o in options), options
    assert any(o.startswith("Lever (Not tested") for o in options) and any(o.startswith("Generic") and "Not tested" not in o for o in options)
    page.keyboard.press("Escape")
    assert page.request.post("/api/integrations", data={"provider": "greenhouse", "name": "GH sandbox", "settings": {}, "secrets": {}}).ok
    page.reload()
    section.wait_for()
    assert section.evaluate("d => d.open")  # open once something is connected
    card = page.locator("details.advanced-integrations section", has_text="GH sandbox")
    card.get_by_text("Not tested", exact=True).wait_for()
    assert card.get_by_text("Active", exact=True).count() == 0


# ---------- 5. The candidate's interview link: email code, notice, start ----------

def test_a_candidate_confirms_the_code_and_starts_the_interview(settings, browser):
    """The candidate's own pages (app/web/interview.js): the link from the invitation, a 6-digit code emailed to the
    address they applied with (captured here, never sent), the notice, then Start. The bot is the fake MeetStream."""
    from tests.test_api import TUNNEL
    from tests.test_recruiting import CRITERIA, RESUME, ScreeningAI
    sent = []
    s = replace(settings, smtp_host="smtp.example.com", smtp_from="hiring@example.com")
    app = create_app(settings=s, store=Store(s.database), ai=ScreeningAI(), meetstream_client_factory=FakeMeetStream,
                     run_loops=False, mail_send=lambda _s, e: sent.append(e))
    with serve(app) as url:
        recruiter = browser.new_context(base_url=url)
        r = recruiter.request
        rid = r.post("/api/roles", data={"role_title": "AI Engineer", "job_description": "Build LLM features in Python."}).json()["id"]
        assert r.post(f"/api/roles/{rid}/plan/generate").ok and r.post(f"/api/roles/{rid}/plan/approve").ok
        hiring = r.put(f"/api/roles/{rid}/hiring", data={"criteria": CRITERIA, "meeting_link": ROOM, "published": True,
                                                         "passing_score": 70, "invitation_days": 5})
        assert hiring.ok, hiring.text()
        key = hiring.json()["careers"]["job_key"]
        applied = r.post(f"/api/public/apply/{key}", headers=TUNNEL, multipart={
            "full_name": "Priya Raman", "email": "priya@example.com", "years_experience": "5", "consent": "yes",
            "resume": {"name": "cv.txt", "mimeType": "text/plain", "buffer": RESUME}})
        assert applied.status == 201, applied.text()
        deadline, row = time.time() + 20, None
        while time.time() < deadline and not (row and row["interview_id"]):
            rows = r.get(f"/api/roles/{rid}/pipeline").json()["candidates"]
            row = rows[0] if rows else None
            time.sleep(0.2)
        assert row and row["interview_id"], "the application wasn't screened and invited"
        link = r.get(f"/api/interviews/{row['interview_id']}/invitation").json()["url"]
        path = "/" + link.split("/", 3)[3]  # /i/<token> on this server
        recruiter.close()

        # The candidate, on their own device (arriving the way candidates do: through the public address).
        context = browser.new_context(base_url=url, extra_http_headers=TUNNEL, viewport={"width": 420, "height": 860})
        page = context.new_page()
        page.set_default_timeout(15_000)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto("/i/not-a-real-link-0000000000000000000000")
        page.get_by_role("heading", name="This link isn't valid").wait_for()
        page.goto(path)
        page.get_by_role("heading", name="Hello Priya").wait_for()
        click(page, "Email me a code")
        page.get_by_text("We sent a 6-digit code").wait_for()
        code = re.search(r"(?<![0-9])([0-9]{6})(?![0-9])", sent[-1].body).group(1)
        page.get_by_label("Code").fill("12345")
        click(page, "Continue")
        page.get_by_text("Enter the 6-digit code from the email.").wait_for()
        page.get_by_label("Code").fill(code)
        click(page, "Continue")
        start = page.get_by_role("button", name="Start my interview now")
        start.wait_for()
        start.click()
        page.get_by_text("Please confirm that you have read how the interview works.").wait_for()
        page.locator("#ack").check()
        start.click()
        page.get_by_role("heading", name="Your interview has started").wait_for()
        join = page.get_by_role("link", name="Join the call")
        assert join.get_attribute("href") == ROOM  # only now, on the device that entered the code
        context.close()
        assert not errors, f"JavaScript errors on the candidate page: {errors}"


# ---------- 6. Calendar picker: MeetStream's copy of the calendar stopped changing (R-10) ----------

def test_the_calendar_picker_warns_when_meetstreams_copy_is_outdated(settings, browser):
    from datetime import datetime, timedelta, timezone

    from tests.test_resume import make_pdf
    now = datetime.now(timezone.utc)
    meeting = {"id": "evt_1", "start_time": (now + timedelta(days=1)).isoformat(), "end_time": None, "is_deleted": False,
               "meeting_url": "https://meet.google.com/abc-defg-hij", "raw": {"summary": "Interview: AI Engineer"},
               "updated_at": (now - timedelta(days=18)).isoformat()}

    refresh_error = "Failed to fetch calendar events: Failed to refresh token: 400 Client Error: Bad Request for url: https://oauth2.googleapis.com/token"

    class Calendar:
        # R-14: MeetStream also says why its copy is old (the Google sign-in it can't refresh).
        data = {"results": [meeting], "has_more": False, "partial_errors": {"google::recruiter@example.com": refresh_error}}

        async def events(self, **kw):
            return self.data

    fake = FakeMeetStream()
    fake.calendar = Calendar()
    app = create_app(settings=settings, store=Store(settings.database), ai=OfflineAI(), meetstream_client_factory=lambda: fake,
                     run_loops=False)
    with serve(app) as url:
        context = browser.new_context(base_url=url, viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.set_default_timeout(15_000)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto("/")
        finished_setup(page)
        rid = published_role(page)
        path = page.request.post("/api/workspaces/current/careers-key").json()["careers_path"]
        assert page.request.post(path, multipart={"role": "AI Engineer", "name": "Lin Wei", "email": "lin@example.com", "resume": {
            "name": "lin.pdf", "mimeType": "application/pdf", "buffer": make_pdf(["Lin Wei", "Python"])}}).status == 201
        cand = page.request.get(f"/api/roles/{rid}/pipeline").json()["candidates"][0]
        assert page.request.post(f"/api/applications/{cand['application_id']}/decision", data={"action": "invite"}).ok
        iid = page.request.get(f"/api/roles/{rid}/pipeline").json()["candidates"][0]["interview_id"]
        page.goto(f"/#/i/{iid}")
        click(page, "Resync calendar")
        warning = page.get_by_text(re.compile(r"MeetStream's copy of this calendar hasn't changed since .* \(18 days\)"))
        warning.wait_for()
        assert "reconnect the calendar in MeetStream" in warning.inner_text()
        problem = page.get_by_text(re.compile(r"Google calendar \(recruiter@example.com\): MeetStream can no longer use its sign-in"))
        problem.wait_for()
        assert "Failed to refresh token" in problem.inner_text()
        page.get_by_role("combobox", name="Upcoming meetings").wait_for()  # the meetings are still offered
        Calendar.data = {"results": [{**meeting, "updated_at": now.isoformat()}], "has_more": False}
        click(page, "Resync calendar")
        warning.wait_for(state="hidden")  # a fresh copy: no warning
        problem.wait_for(state="hidden")  # and no error from MeetStream
        context.close()
        assert not errors, f"JavaScript errors in the dashboard: {errors}"
