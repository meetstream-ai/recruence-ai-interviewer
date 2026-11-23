"""A careers website whose server forwards every application to one address (POST /api/public/careers/<key>), in its
own field names, naming the job by its title. Modelled on the MeetStream Careers site: name, email, phone, linkedin,
portfolio, role, message and a PDF resume."""

from dataclasses import replace

from fastapi.testclient import TestClient

from app.server import create_app
from app.store import Store
from tests.fakes import FakeMeetStream
from tests.test_api import TUNNEL, wait_for
from tests.test_recruiting import CRITERIA, ScreeningAI
from tests.test_tenant_isolation import OTHER, OWNER

ZOOM = "https://zoom.us/j/1234567890"
RESUME = b"Priya Raman. B.Tech in Computer Science, 2018. Five years of Python services at a fintech."


def role(c, title):
    rid = c.post("/api/roles", json={"role_title": title, "job_description": "Build things."}).json()["id"]
    c.post(f"/api/roles/{rid}/plan/generate")
    c.post(f"/api/roles/{rid}/plan/approve")
    r = c.put(f"/api/roles/{rid}/hiring", json={"meeting_link": ZOOM, "published": True, "criteria": CRITERIA})
    assert r.status_code == 200, r.text
    return rid


def form(role_title, email="priya@example.com", **extra):
    return {"name": "Priya Raman", "email": email, "phone": "+91 90000 00000", "linkedin": "https://linkedin.com/in/priya",
            "portfolio": "https://github.com/priya", "role": role_title, "message": "Keen on backend work.", **extra}


def test_a_careers_site_posts_every_job_to_one_address(settings):
    s = replace(settings)
    app = create_app(settings=s, store=Store(s.database), ai=ScreeningAI(), meetstream_client_factory=FakeMeetStream, run_loops=False)
    store = app.state.store
    with TestClient(app) as c:
        c.post("/api/auth/register", json=OWNER)
        backend = role(c, "Backend Engineer · Python")
        role(c, "AI Engineer")
        ws = c.post("/api/workspaces/current/careers-key").json()
        path = ws["careers_path"]
        assert path.startswith("/api/public/careers/") and ws["careers_url"] == f"https://tunnel.example.com{path}"
        site = TestClient(app, client=("203.0.113.9", 4000))  # the careers site's server, not signed in
        pdf = ("Thalha_resume.pdf", RESUME, "application/pdf")

        r = site.post(path, data=form("Backend Engineer · Python"), files={"resume": ("cv.txt", RESUME, "text/plain")}, headers=TUNNEL)
        assert r.status_code == 201, r.text
        assert r.json() == {"received": True, "role_title": "Backend Engineer · Python"}
        a = wait_for(lambda: [x for x in store.all_applications() if x.screening])[0]
        assert a.role_id == backend and a.source == "careers_site" and a.candidate.full_name == "Priya Raman"
        assert a.candidate.links == "https://linkedin.com/in/priya https://github.com/priya" and a.candidate.cover_note == "Keen on backend work."
        assert a.resume_text.startswith("Priya Raman") and a.resume_filename == "cv.txt"
        assert store.role_workspace(a.role_id) == ws["id"]
        assert "consent collected by the careers website" in str(store.audit_log(50))

        # The same person again for the same job: refused; for another job: a second application.
        assert site.post(path, data=form("backend engineer - python"), headers=TUNNEL).status_code == 409
        assert site.post(path, data=form("AI Engineer"), headers=TUNNEL).status_code == 201
        # Unknown job, no job, wrong key, a resume that isn't readable.
        r = site.post(path, data=form("Chief Vibes Officer", email="x@example.com"), headers=TUNNEL)
        assert r.status_code == 404 and "Chief Vibes Officer" in r.json()["detail"]
        assert site.post(path, data=form("", email="y@example.com"), headers=TUNNEL).status_code == 422
        assert site.post("/api/public/careers/" + "x" * 32, data=form("AI Engineer", email="z@example.com"), headers=TUNNEL).status_code == 404
        bad = site.post(path, data=form("AI Engineer", email="w@example.com"), files={"resume": pdf}, headers=TUNNEL)
        assert bad.status_code == 422 and bad.json()["detail"].startswith("Resume:")
        # One server sends everyone's applications: more than the per-visitor limit gets through.
        for n in range(12):
            assert site.post(path, data=form("AI Engineer", email=f"p{n}@example.com"), headers=TUNNEL).status_code == 201

        # Another workspace's address doesn't reach this workspace's jobs; a replaced address stops working.
        b = TestClient(app, client=("172.17.0.1", 50000))
        b.post("/api/auth/register", json=OTHER)
        other = b.post("/api/workspaces/current/careers-key").json()["careers_path"]
        assert site.post(other, data=form("AI Engineer", email="q@example.com"), headers=TUNNEL).status_code == 404
        fresh = c.post("/api/workspaces/current/careers-key").json()["careers_path"]
        assert site.post(path, data=form("AI Engineer", email="r@example.com"), headers=TUNNEL).status_code == 404
        assert site.post(fresh, data=form("AI Engineer", email="r@example.com"), headers=TUNNEL).status_code == 201
        assert c.delete("/api/workspaces/current/careers-key").json()["careers_path"] == ""
        assert "careers_path" not in b.get("/api/workspaces/current").json() or b.get("/api/workspaces/current").json()["careers_path"] != fresh


def test_a_careers_site_can_list_the_jobs_it_may_offer(settings):
    """GET on the careers address: the workspace's published jobs, so the site offers only jobs Recruence takes."""
    s = replace(settings)
    app = create_app(settings=s, store=Store(s.database), ai=ScreeningAI(), meetstream_client_factory=FakeMeetStream, run_loops=False)
    with TestClient(app) as c:
        c.post("/api/auth/register", json=OWNER)
        role(c, "Backend Engineer · Python")
        paused = role(c, "AI Engineer")
        c.put(f"/api/roles/{paused}/hiring", json={"paused": True})
        c.post("/api/roles", json={"role_title": "Draft Role", "job_description": "Not published."})
        path = c.post("/api/workspaces/current/careers-key").json()["careers_path"]
        site = TestClient(app, client=("203.0.113.9", 4000))
        jobs = site.get(path, headers=TUNNEL).json()["jobs"]
        assert [(j["title"], j["open"]) for j in jobs] == [("AI Engineer", False), ("Backend Engineer · Python", True)]
        assert jobs[0]["closed_reason"].startswith("Applications for this role are paused") and jobs[1]["summary"] == "Build things."
        text = str(jobs)
        assert "apply_token" not in text and "Draft Role" not in text  # nothing beyond the job pages: no job keys
        assert all(len(j["ref"]) == 12 and j["questions"] == [] and j["job_description"] == "Build things." for j in jobs)
        assert site.get("/api/public/careers/" + "x" * 32, headers=TUNNEL).status_code == 404
        # Another workspace's address lists only its own jobs.
        b = TestClient(app, client=("172.17.0.1", 50000))
        b.post("/api/auth/register", json=OTHER)
        assert site.get(b.post("/api/workspaces/current/careers-key").json()["careers_path"], headers=TUNNEL).json() == {"jobs": []}


def test_titles_match_however_they_are_written():
    from app.recruiting import job_summary, same_title
    assert same_title("Backend Engineer · Python", "backend engineer - python") and same_title("AI  Engineer", "ai engineer")
    assert not same_title("AI Engineer", "AI Research Intern") and not same_title("", "")
    long = "Build things. " * 30
    assert job_summary(long).endswith(".") and len(job_summary(long)) <= 220
    assert job_summary("word " * 80).endswith("…")



def test_an_application_with_a_ref_reaches_that_exact_job(settings):
    """The careers site sends back the ref from the job list: the application lands on that job even when its title is
    written differently or another job's title is sent alongside, and the job's questions come with the list."""
    s = replace(settings)
    app = create_app(settings=s, store=Store(s.database), ai=ScreeningAI(), meetstream_client_factory=FakeMeetStream, run_loops=False)
    store = app.state.store
    with TestClient(app) as c:
        c.post("/api/auth/register", json=OWNER)
        backend = role(c, "Backend Engineer")
        ai = role(c, "AI Engineer")
        c.put(f"/api/roles/{ai}/hiring", json={"questions": [{"text": "Why this role?", "required": True}, {"text": "Notice period?"}]})
        path = c.post("/api/workspaces/current/careers-key").json()["careers_path"]
        site = TestClient(app, client=("203.0.113.9", 4000))
        jobs = {j["title"]: j for j in site.get(path, headers=TUNNEL).json()["jobs"]}
        listed = jobs["AI Engineer"]
        assert listed["ref"] == ai and [q["text"] for q in listed["questions"]] == ["Why this role?", "Notice period?"]
        assert listed["questions"][0]["required"] is True and listed["requirements"]
        q1, q2 = (q["id"] for q in listed["questions"])

        # A required question unanswered: refused with the question.
        r = site.post(path, data=form("Backend Engineer", job_ref=ai), headers=TUNNEL)
        assert r.status_code == 422 and "Why this role?" in r.json()["detail"]
        # The ref wins over the title: this lands on AI Engineer, with its answers.
        r = site.post(path, data=form("Backend Engineer", job_ref=ai, **{f"q_{q1}": "I build LLM systems.", f"q_{q2}": "30 days"}),
                      files={"resume": ("cv.txt", RESUME, "text/plain")}, headers=TUNNEL)
        assert r.status_code == 201 and r.json()["role_title"] == "AI Engineer"
        (a,) = store.all_applications()
        assert a.role_id == ai and a.role_id != backend
        assert [(x.question, x.answer) for x in a.answers] == [("Why this role?", "I build LLM systems."), ("Notice period?", "30 days")]
        # A ref that isn't a published job here: refused, not guessed from the title.
        r = site.post(path, data=form("AI Engineer", email="other@example.com", job_ref="0" * 12), headers=TUNNEL)
        assert r.status_code == 404 and "reference" in r.json()["detail"]
        # Unpublished: its ref stops working.
        c.put(f"/api/roles/{backend}/hiring", json={"published": False})
        assert site.post(path, data=form("Backend Engineer", email="late@example.com", job_ref=backend), headers=TUNNEL).status_code == 404
        assert backend not in {j["ref"] for j in site.get(path, headers=TUNNEL).json()["jobs"]}


def test_the_workspace_remembers_how_its_careers_site_is_set_up(settings):
    app = create_app(settings=settings, store=Store(settings.database), ai=ScreeningAI(), meetstream_client_factory=FakeMeetStream, run_loops=False)
    with TestClient(app) as c:
        c.post("/api/auth/register", json=OWNER)
        assert c.get("/api/workspaces/current").json()["careers_site"] == ""  # not asked yet
        assert c.put("/api/workspaces/current", json={"careers_site": "own"}).json()["careers_site"] == "own"
        assert c.get("/api/workspaces/current").json()["careers_site"] == "own"
        assert c.put("/api/workspaces/current", json={"careers_site": "recruence"}).json()["careers_site"] == "recruence"
        assert c.put("/api/workspaces/current", json={"careers_site": "somewhere"}).status_code == 422
        # A site with its own job pages: its server forwards an application, naming the job by its title.
        role(c, "Data Analyst")
        path = c.post("/api/workspaces/current/careers-key").json()["careers_path"]
        site = TestClient(app, client=("203.0.113.9", 4000))
        r = site.post(path, data={"role": "Data Analyst", "name": "Lin Wei", "email": "lin@example.com"},
                      files={"resume": ("cv.txt", RESUME, "text/plain")}, headers=TUNNEL)
        assert r.status_code == 201, r.text
        assert site.post(path, data={"role": "Chief Astronaut", "name": "Lin Wei", "email": "lin2@example.com"}, headers=TUNNEL).status_code == 404


def test_a_new_job_on_the_site_waits_for_its_role_or_is_refused(settings):
    app = create_app(settings=settings, store=Store(settings.database), ai=ScreeningAI(), meetstream_client_factory=FakeMeetStream, run_loops=False)
    store = app.state.store
    with TestClient(app) as c:
        c.post("/api/auth/register", json=OWNER)
        path = c.post("/api/workspaces/current/careers-key").json()["careers_path"]
        site = TestClient(app, client=("203.0.113.9", 4000))
        cv = {"resume": ("cv.txt", RESUME, "text/plain")}
        new_job = {"role": "Data Analyst", "name": "Lin Wei", "email": "lin@example.com", "job_description": "Analyse clinic data with SQL."}

        # "I add each role myself" (the default): a title Recruence doesn't have is refused, and nothing is created.
        assert c.get("/api/workspaces/current").json()["careers_new_jobs"] == "manual"
        assert site.post(path, data=new_job, files=cv, headers=TUNNEL).status_code == 404
        assert c.get("/api/roles").json() == [] and store.all_applications() == []

        # "Add new jobs automatically": a draft role from the title and the site's text; the application waits, unscreened.
        assert c.put("/api/workspaces/current", json={"careers_new_jobs": "draft"}).json()["careers_new_jobs"] == "draft"
        r = site.post(path, data=new_job, files=cv, headers=TUNNEL)
        assert r.status_code == 201 and r.json()["role_title"] == "Data Analyst", r.text
        roles = c.get("/api/roles").json()
        assert len(roles) == 1
        role = store.get_role(roles[0]["id"])
        assert role.setup.role_title == "Data Analyst" and not role.hiring.published and role.setup.job_description == "Analyse clinic data with SQL."
        a = store.all_applications()[0]
        assert a.status == "needs_review" and a.screening.screened_by.startswith("waiting") and a.source == "careers_site"
        # A second applicant for the same new job joins the same draft role; the same person again is refused.
        assert site.post(path, data={**new_job, "email": "sam@example.com"}, files=cv, headers=TUNNEL).status_code == 201
        assert site.post(path, data=new_job, files=cv, headers=TUNNEL).status_code == 409
        assert len(c.get("/api/roles").json()) == 1 and len(store.all_applications()) == 2

        # Publishing the role screens everyone who was waiting for it.
        c.post(f"/api/roles/{role.id}/plan/generate")
        c.post(f"/api/roles/{role.id}/plan/approve")
        assert c.put(f"/api/roles/{role.id}/hiring", json={"meeting_link": ZOOM, "published": True, "criteria": CRITERIA}).status_code == 200
        done = wait_for(lambda: (x := store.all_applications()) and all(y.screening and not y.screening.screened_by.startswith("waiting") for y in x) and x)
        assert {y.status for y in done} <= {"eligible", "invited", "needs_review", "not_eligible"} and all(y.screening.results for y in done)
        assert c.put("/api/workspaces/current", json={"careers_new_jobs": "sometimes"}).status_code == 422
