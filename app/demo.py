"""Demo data: a small, realistic recruiting workspace for exploring the app without any external service.

Everything here is fictional: the people, their emails (@example.com), meeting links and transcripts. Every record
carries `demo=True` and a fixed id starting with "de", so it is labelled as Demo in the dashboard, can't be launched,
emailed or booked (app/server.py refuses), and can be removed exactly without touching anything else.

Installing is idempotent: it removes any earlier demo records first, then writes them again with dates relative to
now. Removing deletes only demo records (and their sessions, transcripts, reports, applications, links and audit
entries); real roles and candidates are never read or changed.

The evaluations are written by hand and say so (`generated_by`), so a demo report is never mistaken for an AI's.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import sqlalchemy as sa

from app.models import (
    AnswerNote, Application, ApplicationInput, Booking, Competency, CompetencyScore, CriterionResult, EligibilityCriterion,
    EvaluationReport, Hiring, Interview, InterviewOptions, InterviewSetup, Invitation, LLMEvaluation, Plan, PlannedQuestion,
    RequirementResult, Role, RoleSetup, Screening, Utterance, utcnow,
)
from app.store import CURRENT_WORKSPACE, Store, applications, audit_events, interviews, roles, sessions

PREFIX = "de"
GENERATED_BY = "Demo data: written by hand for the demo, not an AI evaluation"
BOT = "AI Interviewer"


def _id(kind: str, n: int) -> str:
    """Fixed hex ids: de + kind digit + counter, 12 characters (the dashboard's routes take hex ids)."""
    return f"{PREFIX}{kind}{n:09d}"


# ---------- the content ----------

ROLES = [
    {
        "n": 1, "title": "Senior Backend Engineer", "level": "Senior", "type": "technical", "days_ago": 14, "published": True,
        "jd": "We're hiring a Senior Backend Engineer to design and run the Python services behind our payments platform.\n\n"
              "**You will**\n- Own services from design to production on PostgreSQL and Kubernetes\n- Lead incident response and postmortems\n"
              "- Mentor engineers and review designs\n\n**You have**\n- 4+ years building Python services in production\n- A degree in computer science or equivalent experience",
        "competencies": [("System design", "Designs services that scale and fail safely"), ("Ownership", "Owns outcomes end to end"),
                         ("Communication", "Explains trade-offs clearly")],
        "requirements": ["Has run Python services in production"],
        "questions": [
            ("Tell me about a service you designed that had to handle a sharp rise in traffic. What did you change, and why?", [0]),
            ("Describe an incident you led. How did you find the cause, and what changed afterwards?", [1, 2]),
            ("How do you decide between a quick fix and a deeper redesign when a deadline is close?", [0, 2]),
        ],
        "criteria": [("A degree in computer science or equivalent experience", True), ("4+ years of Python services in production", True),
                     ("Experience with Kubernetes", False)],
        "passing": 70,
    },
    {
        "n": 2, "title": "Product Designer", "level": "Mid-level", "type": "mixed", "days_ago": 11, "published": True,
        "jd": "Join our product team as a Product Designer working on the recruiter dashboard.\n\n**You will**\n- Turn research into flows and prototypes\n"
              "- Work closely with engineers through delivery\n\n**You have**\n- A portfolio of shipped product work\n- Experience running user research",
        "competencies": [("Design craft", "Clear, consistent interfaces"), ("User research", "Learns from users and acts on it"),
                         ("Collaboration", "Works well with engineering and product")],
        "requirements": ["Has shipped product work used by real customers"],
        "questions": [
            ("Walk me through a design you shipped that you're proud of. What problem did it solve?", [0]),
            ("Tell me about research that changed your mind about a design.", [1]),
            ("Describe a disagreement with an engineer and how you resolved it.", [2]),
        ],
        "criteria": [("A portfolio of shipped product work", True), ("Experience running user research", True), ("Figma", False)],
        "passing": 65,
    },
    {
        "n": 3, "title": "Data Analyst", "level": "Junior", "type": "screening", "days_ago": 6, "published": False,
        "jd": "A Data Analyst to build dashboards and answer questions for the operations team, using SQL and Python.",
        "competencies": [("SQL and analysis", "Gets reliable answers from data"), ("Communication", "Explains findings to non-specialists")],
        "requirements": [],
        "questions": [
            ("Tell me about an analysis that changed a decision. How did you check your numbers?", [0]),
            ("How would you explain a surprising result to someone without a data background?", [1]),
        ],
        "criteria": [], "passing": 70,
    },
]

# Candidates: (role n, name, email, years, current role, education, resume, outcome, scores, recommendation, decision)
# outcome: evaluated | invited | expired | needs_review | not_eligible | eval_failed | scheduled | ready
CANDIDATES = [
    (1, "Aisha Khan", 7, "Staff Engineer, Lumen Pay", "B.Tech in Computer Science",
     "Seven years building Python services in production at two fintechs. Led the move of the ledger to PostgreSQL partitions. Runs services on Kubernetes.",
     "evaluated", [5, 4, 4], "strong_yes", "advance",
     ["We were at about four hundred requests a second and expected ten times that during a sale. I split the ledger writes onto a queue and partitioned the table by month, and we load-tested it at fifteen times the traffic before the launch.",
      "Payments started failing at two in the morning. I opened an incident channel, rolled back the last deploy within ten minutes, and the postmortem showed a missing index. Afterwards we added query plans to code review.",
      "If customers are blocked, I ship the smallest safe fix behind a flag and write down the redesign with a date. I don't let the quick fix become the design."]),
    (1, "Marcus Lee", 5, "Senior Software Engineer, Brightline", "B.Sc. in Software Engineering",
     "Five years of Python and Go services in production. Built an event pipeline processing two million messages a day.",
     "evaluated", [4, 3, 4], "yes", "hold",
     ["Our event pipeline grew from two hundred thousand to two million messages a day. I moved it from cron jobs to a queue with workers that scale on lag.",
      "A bad config took down notifications for an hour. I wasn't the lead, but I found the config change in the deploy history and we added a review step for config.",
      "I usually try to understand how much time we really have and then pick the option that doesn't block the redesign later."]),
    (1, "Elena Petrova", 4, "Backend Developer, Nordwind Logistics", "M.Sc. in Mathematics",
     "Four years of Python backend work: REST APIs with Django and reporting jobs. Some exposure to Docker.",
     "evaluated", [3, 2, 3], "no", "reject",
     ["We added caching to the reports API when it got slow. I'm not sure what the traffic numbers were.",
      "I haven't led an incident. When something broke, my lead usually handled it and I helped look at logs.",
      "I'd probably do the quick fix first because deadlines matter, and then see if there's time later."]),
    (1, "Tomás García", 6, "Backend Engineer, Ventura Health", "B.Eng. in Computer Engineering",
     "Six years of Python services in production on AWS and Kubernetes; on-call lead for a patient-records API.", "invited", None, None, None, None),
    (1, "Daniel Okoye", 5, "Software Engineer, Kestrel Media", "B.Sc. in Computer Science",
     "Five years building Python microservices in production; maintains the company's internal API gateway.", "expired", None, None, None, None),
    (1, "Sofia Rossi", 6, "Platform Engineer, Arcadia Games", "Self-taught; bootcamp in 2018",
     "Six years running Python and Rust services in production; led migration to Kubernetes.", "needs_review", None, None, None, None),
    (1, "Liam Chen", 2, "Junior Developer, Pixel Forge", "B.Sc. in Computer Science",
     "Two years of Python experience, mostly scripts and a Flask API for internal tools.", "not_eligible", None, None, None, None),
    (2, "Hannah Schmidt", 6, "Senior Product Designer, Fieldnote", "B.A. in Interaction Design",
     "Portfolio: shipped the onboarding and billing flows used by 40,000 customers. Runs monthly user research sessions. Figma.",
     "evaluated", [4, 4, 5], "strong_yes", "advance",
     ["I redesigned our onboarding: sign-up to first project took eleven minutes and people dropped off. After the redesign it took four minutes and activation went up by a fifth.",
      "I was sure recruiters wanted more filters. In five interviews they kept saying they wanted fewer, smarter defaults, so we shipped saved views instead.",
      "An engineer said my animation would be too slow on older laptops. We prototyped it together, measured it, and agreed on a simpler version."]),
    (2, "Omar Haddad", 4, "Product Designer, Cobalt Studio", "B.Des. in Visual Communication",
     "Portfolio of shipped mobile and web products for agency clients. Some usability testing. Figma.",
     "evaluated", [3, 3, 4], "yes", None,
     ["I designed a booking flow for a clinic chain. It was shipped and bookings moved from phone to online for most patients.",
      "We tested a prototype with five users and found the date picker confused people, so we changed it to a list of days.",
      "When an engineer pushed back on a layout, I asked what was hard about it and we found a version that used existing components."]),
    (2, "Grace Kim", 5, "Product Designer, Meridian Bank", "M.A. in Human-Computer Interaction",
     "Portfolio of shipped banking app features; led research for the savings feature. Figma.", "eval_failed", None, None, None,
     ["I led the design of the savings goals feature in our banking app.", "Research showed people wanted round-ups, so we added them.",
      "I share early sketches with engineers so there are no surprises."]),
    (2, "Lucas Martin", 3, "UX Designer, Tidewater Travel", "B.A. in Graphic Design",
     "Portfolio of shipped travel booking flows. Usability testing with customers.", "scheduled", None, None, None, None),
    (3, "Nina Patel", 2, "Operations Analyst, Greenleaf Retail", "B.Sc. in Economics",
     "Two years building SQL dashboards for store operations.", "ready", None, None, None, None),
]

OPENING = ("Hello {first}, thank you for joining. I'm an AI interviewer for the {role} role. I'll ask you {n} questions and may ask follow-ups. "
           "This conversation is recorded and transcribed, and a person on the hiring team will review it. You can ask to stop at any time. "
           "Do you agree to continue with this AI interview?")


# ---------- building records ----------

def _plan(spec: dict) -> Plan:
    comps = [Competency(id=f"c{i + 1}", name=n, description=d) for i, (n, d) in enumerate(spec["competencies"])]
    qs = [PlannedQuestion(id=f"q{i + 1}", text=t, competency_ids=[comps[c].id for c in cs], look_for=["Concrete example", "Their own role", "Outcome"])
          for i, (t, cs) in enumerate(spec["questions"])]
    return Plan(competencies=comps, questions=qs, source="Demo data")


def _transcript(name: str, spec: dict, answers: list[str], start: datetime) -> list[Utterance]:
    first = name.split()[0]
    ts = start
    lines = [Utterance(speaker=BOT, role="interviewer", text=OPENING.format(first=first, role=spec["title"], n=len(answers)), ts=ts)]
    ts += timedelta(seconds=40)
    lines.append(Utterance(speaker=name, role="candidate", text="Yes, I agree.", ts=ts))
    for i, ((q, _), a) in enumerate(zip(spec["questions"], answers)):
        ts += timedelta(seconds=20)
        lines.append(Utterance(speaker=BOT, role="interviewer", text=q, ts=ts, question_id=f"q{i + 1}"))
        ts += timedelta(seconds=90)
        lines.append(Utterance(speaker=name, role="candidate", text=a, ts=ts, question_id=f"q{i + 1}"))
    ts += timedelta(seconds=20)
    lines.append(Utterance(speaker=BOT, role="interviewer", text=f"Thank you, {first}. That's all my questions. The hiring team will be in touch. Goodbye.", ts=ts))
    return lines


def _evaluation(spec: dict, plan: Plan, name: str, scores: list[int], recommendation: str, answers: list[str]) -> LLMEvaluation:
    first_sentence = lambda a: a.split(". ")[0].rstrip(".")  # a verbatim quote from the answer
    comp_scores = []
    for c, s in zip(plan.competencies, scores):
        q_index = next(i for i, q in enumerate(plan.questions) if c.id in q.competency_ids)
        comp_scores.append(CompetencyScore(competency_id=c.id, competency_name=c.name, score=s,
                                           rationale=f"{name.split()[0]}'s answer to question {q_index + 1} " + (
                                               "gave a specific example with a measurable outcome." if s >= 4 else
                                               "gave an example with some detail but little on the outcome." if s == 3 else
                                               "stayed general, with no example of their own."),
                                           evidence=[first_sentence(answers[q_index])]))
    req = [RequirementResult(requirement_id=f"r{i + 1}", requirement=r, verdict="meets" if min(scores) >= 3 else "not_enough_evidence",
                             rationale="Described running services in production." if min(scores) >= 3 else "Didn't describe it directly.",
                             evidence=[first_sentence(answers[0])] if min(scores) >= 3 else [])
           for i, r in enumerate(spec["requirements"])]
    strong = [c.name for c, s in zip(plan.competencies, scores) if s >= 4]
    weak = [c.name for c, s in zip(plan.competencies, scores) if s <= 3]
    return LLMEvaluation(
        recommendation=recommendation,
        summary=f"{name} gave {'specific, well-structured' if sum(scores) / len(scores) >= 4 else 'mixed' if sum(scores) / len(scores) >= 3 else 'general'} "
                f"answers{', strongest on ' + ' and '.join(strong) if strong else ''}.",
        competency_scores=comp_scores,
        strengths=[f"Clear examples on {s.lower()}" for s in strong] or ["Answered every question"],
        areas_for_improvement=[f"More concrete detail on {w.lower()}" for w in weak] or ["Could quantify outcomes more often"],
        suggested_next_round_focus=[f"Probe {w.lower()} with a practical exercise" for w in weak][:2],
        confidence="high" if len(answers) >= 3 else "medium",
        requirement_results=req,
    )


def install(store: Store, now: Optional[datetime] = None) -> dict:
    """Write the demo workspace (removing any earlier demo records first). Returns what was written."""
    now = now or utcnow()
    ws = CURRENT_WORKSPACE.get()
    # The demo's ids are fixed, so a database holds one copy, in one workspace.
    if ws is not None and any(w != ws for w in _demo_workspaces(store)):
        raise ValueError("Demo data is already loaded in another workspace of this database. Remove it there first.")
    remove(store)
    counts = {"roles": 0, "interviews": 0, "applications": 0, "reports": 0}
    role_by_n: dict[int, tuple[Role, dict]] = {}
    for spec in ROLES:
        created = now - timedelta(days=spec["days_ago"])
        setup = RoleSetup(role_title=spec["title"], level=spec["level"], job_description=spec["jd"], interview_type=spec["type"],
                          competencies=[{"name": n, "description": d} for n, d in spec["competencies"]],
                          requirements=spec["requirements"], question_count=len(spec["questions"]), duration_minutes=20)
        hiring = Hiring(passing_score=spec["passing"], published=spec["published"],
                        apply_token=f"demo-apply-{spec['n']:02d}-not-a-real-link-token" if spec["published"] else None,
                        criteria=[EligibilityCriterion(id=f"e{i + 1}", text=t, mandatory=m) for i, (t, m) in enumerate(spec["criteria"])],
                        meeting_link=f"https://zoom.us/j/900000000{spec['n']}", invitation_days=5,
                        application_deadline=now + timedelta(days=10) if spec["published"] else None)
        role = Role(id=_id("1", spec["n"]), created_at=created, updated_at=created, setup=setup, plan=_plan(spec),
                    plan_approved_at=created + timedelta(hours=2), hiring=hiring, demo=True)
        store._run(roles.insert().values(id=role.id, updated_at=role.updated_at.isoformat(), data=role.model_dump_json()))
        role_by_n[spec["n"]] = (role, spec)
        counts["roles"] += 1

    for k, (rn, name, years, current, education, resume, outcome, scores, rec, decision, *rest) in enumerate(CANDIDATES, start=1):
        answers = rest[0] if rest else None
        role, spec = role_by_n[rn]
        applied = role.created_at + timedelta(days=1 + k % 4, hours=k)
        email = name.lower().replace(" ", ".").replace("á", "a") + "@example.com"
        app_id = _id("3", k) if spec["published"] else None
        interview_id = _id("2", k) if outcome not in ("needs_review", "not_eligible") else None

        if app_id:
            results, reasons, verdict = _screening(spec, outcome, resume, years)
            application = Application(
                id=app_id, role_id=role.id, created_at=applied, updated_at=applied,
                status="invited" if interview_id else verdict,
                candidate=ApplicationInput(full_name=name, email=email, years_experience=years, current_role=current, education=education,
                                           location="Remote"),
                resume_text=f"{name}\n{current}\n{education}\n\n{resume}", resume_filename="resume.pdf", interview_id=interview_id,
                screening=Screening(outcome=verdict, reasons=reasons, results=results, summary=resume.split(".")[0] + ".",
                                    screened_by="Demo data", screened_at=applied + timedelta(minutes=1)),
                demo=True)
            store._run(applications.insert().values(
                id=application.id, role_id=role.id, email=email, status=application.status, interview_id=interview_id,
                created_at=applied.isoformat(), updated_at=applied.isoformat(), data=application.model_dump_json()))
            counts["applications"] += 1
        if not interview_id:
            continue

        setup = InterviewSetup(**role.setup.model_dump(), candidate_name=name, candidate_email=email, resume_text=resume)
        fields: dict = {"status": "ready"}
        invited = applied + timedelta(minutes=2)
        invitation = (Invitation(token=f"demo-link-{k:03d}-not-a-real-interview-token", created_at=invited,
                                 expires_at=invited + timedelta(days=5)) if app_id else None)
        if outcome == "expired":
            invitation = invitation.model_copy(update={"created_at": now - timedelta(days=8), "expires_at": now - timedelta(days=3)})
        if outcome == "invited":
            invitation = invitation.model_copy(update={"created_at": now - timedelta(days=1), "expires_at": now + timedelta(days=4),
                                                       "opened_at": now - timedelta(hours=20)})
        if outcome == "scheduled":
            slot = (now + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
            fields.update(status="scheduled", scheduled_at=slot, meeting_link=role.hiring.meeting_link, platform="Zoom",
                          consent_attested_at=now - timedelta(days=1),
                          booking=Booking(token=f"demo-book-{k:03d}-not-a-real-booking", meeting_link=role.hiring.meeting_link, platform="Zoom",
                                          slots=[slot, slot + timedelta(hours=2)], created_at=now - timedelta(days=2), booked_at=now - timedelta(days=1)))
            invitation = None
        session_id = None
        if outcome in ("evaluated", "eval_failed"):
            session_id = _id("4", k)
            started = applied + timedelta(days=1, hours=3)
            invitation = invitation.model_copy(update={"opened_at": started - timedelta(minutes=5), "started_at": started, "attempts": 1})
            fields.update(status="completed", meeting_link=role.hiring.meeting_link, platform="Zoom", consent_attested_at=started,
                          latest_session_id=session_id)
            if decision:
                fields.update(decision=decision, decided_at=started + timedelta(days=1),
                              decision_note={"advance": "Strong examples; move to the panel.", "hold": "Good, but compare with the next candidates.",
                                             "reject": "Not enough depth for a senior role."}[decision])
        interview = Interview(id=interview_id, created_at=applied, updated_at=applied, setup=setup, plan=role.plan,
                              plan_approved_at=applied, role_id=role.id, invitation=invitation, application_id=app_id,
                              options=InterviewOptions(), invited_at=invited if invitation else None, demo=True,
                              **{k2: v for k2, v in fields.items()})
        store._run(interviews.insert().values(id=interview.id, **store._interview_columns(interview)))
        counts["interviews"] += 1
        if session_id:
            _session(store, interview, spec, role.plan, name, scores, rec, answers, outcome == "eval_failed")
            counts["reports"] += 1

    counts["interviews"] += _one_offs(store, now)
    counts["reports"] += 1
    _audit(store, now)
    if ws is not None:  # written directly above: put them in this workspace
        found = demo_ids(store, everywhere=True)
        for table, key in ((roles, "roles"), (interviews, "interviews"), (applications, "applications")):
            if found[key]:
                store._run(table.update().where(table.c.id.in_(found[key])).values(workspace_id=ws))
        store._run(audit_events.update().where(audit_events.c.workspace_id.is_(None), sa.or_(
            audit_events.c.interview_id.in_(found["interviews"] or [""]), audit_events.c.detail.like("[demo]%"))).values(workspace_id=ws))
    store.audit("demo_data_installed", detail="[demo] " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    return counts


def _screening(spec, outcome, resume, years):
    results = []
    for i, (text, mandatory) in enumerate(spec["criteria"]):
        cid = f"e{i + 1}"
        if outcome == "not_eligible" and "years" in text.lower():
            results.append(CriterionResult(criterion_id=cid, verdict="not_met", evidence=f"{['Two', 'Three'][0]} years of Python experience",
                                           reasoning="States two years; the role asks for four or more."))
        elif outcome == "needs_review" and "degree" in text.lower():
            results.append(CriterionResult(criterion_id=cid, verdict="unclear", evidence="", reasoning="No degree listed; equivalent experience may apply."))
        elif not mandatory:
            results.append(CriterionResult(criterion_id=cid, verdict="unclear", evidence="", reasoning="Not mentioned."))
        else:
            results.append(CriterionResult(criterion_id=cid, verdict="met", evidence=resume.split(".")[0], reasoning="Shown in the resume."))
    if outcome == "not_eligible":
        return results, ["Not met: " + next(t for t, m in spec["criteria"] if "years" in t.lower())], "not_eligible"
    if outcome == "needs_review":
        return results, ["Unclear: " + next(t for t, m in spec["criteria"] if "degree" in t.lower())], "needs_review"
    return results, ["Every mandatory criterion is met."], "eligible"


def _session(store: Store, interview: Interview, spec, plan, name, scores, rec, answers, failed: bool) -> None:
    sid = interview.latest_session_id
    started = interview.invitation.started_at if interview.invitation else interview.created_at + timedelta(days=1)
    transcript = _transcript(name, spec, answers, started)
    ended = transcript[-1].ts + timedelta(seconds=15)
    store._run(sessions.insert().values(id=sid, interview_id=interview.id, platform="Zoom", state="completed", created_at=started.isoformat(),
                                        ended_at=ended.isoformat(), end_reason="completed", bot_id=None))
    for u in transcript:
        store.add_utterance(sid, u)
    notes = [AnswerNote(question_id=f"q{i + 1}", note=a.split(". ")[0] + ".", follow_ups=0) for i, a in enumerate(answers)]
    for n in notes:
        store.upsert_note(sid, n)
    store.push_report(EvaluationReport(
        session_id=sid, interview_id=interview.id, role_title=spec["title"], candidate_name=name, platform="Zoom",
        started_at=started, ended_at=ended, duration_seconds=int((ended - started).total_seconds()),
        questions_planned=len(plan.questions), questions_asked=len(answers), follow_ups_asked=0, end_reason="completed", consent_given=True,
        generated_by=GENERATED_BY,
        evaluation=None if failed else _evaluation(spec, plan, name, scores, rec, answers),
        evaluation_error="Demo: the AI provider was unavailable, so this interview wasn't evaluated. Re-run the evaluation once a provider is set up."
        if failed else None,
        notes=notes, transcript=transcript, transcript_source="live"))


def _one_offs(store: Store, now: datetime) -> int:
    """Two interviews outside any role: a Meeting Analyst interview that was evaluated, and a cancelled one."""
    spec = {"title": "Customer Success Manager", "requirements": [],
            "questions": [("Tell me about a customer who was about to leave and what you did.", [0]),
                          ("How do you decide which accounts need attention first?", [1])]}
    plan = Plan(competencies=[Competency(id="c1", name="Customer empathy"), Competency(id="c2", name="Prioritisation")],
                questions=[PlannedQuestion(id=f"q{i + 1}", text=q, competency_ids=[f"c{i + 1}"]) for i, (q, _) in enumerate(spec["questions"])],
                source="Demo data")
    answers = ["A logistics customer was about to cancel after two outages. I set up a weekly call with their operations lead and we fixed the alerting, and they renewed for two years.",
               "I look at usage trends and open tickets every Monday and call the accounts where both are getting worse."]
    created = now - timedelta(days=4)
    base = InterviewSetup(role_title=spec["title"], job_description="Own a portfolio of mid-market customers and keep them successful.",
                          interview_type="behavioral", candidate_name="Ravi Menon", candidate_email="ravi.menon@example.com",
                          question_count=2, duration_minutes=15)
    sid = _id("4", 90)
    analyst = Interview(id=_id("2", 90), created_at=created, updated_at=created, setup=base, plan=plan, plan_approved_at=created,
                        status="completed", meeting_link="https://meet.google.com/demo-abc-xyz", platform="Google Meet",
                        consent_attested_at=created + timedelta(days=1), latest_session_id=sid,
                        options=InterviewOptions(mode="analyst", candidate_meeting_name="Ravi Menon"), demo=True)
    store._run(interviews.insert().values(id=analyst.id, **store._interview_columns(analyst)))
    started = created + timedelta(days=1, hours=1)
    lines = [Utterance(speaker="Dana Whitfield (recruiter)", role="interviewer", text=spec["questions"][0][0], ts=started, question_id=None),
             Utterance(speaker="Ravi Menon", role="candidate", text=answers[0], ts=started + timedelta(seconds=80)),
             Utterance(speaker="Dana Whitfield (recruiter)", role="interviewer", text=spec["questions"][1][0], ts=started + timedelta(seconds=110)),
             Utterance(speaker="Ravi Menon", role="candidate", text=answers[1], ts=started + timedelta(seconds=170))]
    ended = started + timedelta(minutes=14)
    store._run(sessions.insert().values(id=sid, interview_id=analyst.id, platform="Google Meet", state="completed",
                                        created_at=started.isoformat(), ended_at=ended.isoformat(), end_reason="completed"))
    for u in lines:
        store.add_utterance(sid, u)
    ev = LLMEvaluation(recommendation="yes", summary="Ravi gave a concrete retention example with a measurable result and a clear weekly routine.",
                       competency_scores=[CompetencyScore(competency_id="c1", competency_name="Customer empathy", score=4, rationale="Specific example with outcome.",
                                                          evidence=["I set up a weekly call with their operations lead and we fixed the alerting"]),
                                          CompetencyScore(competency_id="c2", competency_name="Prioritisation", score=3, rationale="A routine, but little on trade-offs.",
                                                          evidence=["I look at usage trends and open tickets every Monday"])],
                       strengths=["Measurable retention outcome"], areas_for_improvement=["How he weighs competing accounts"],
                       suggested_next_round_focus=["A prioritisation case"], confidence="medium")
    store.push_report(EvaluationReport(
        session_id=sid, interview_id=analyst.id, role_title=spec["title"], candidate_name="Ravi Menon", platform="Google Meet",
        started_at=started, ended_at=ended, duration_seconds=int((ended - started).total_seconds()), questions_planned=2, questions_asked=2,
        follow_ups_asked=0, end_reason="completed", consent_given=True, generated_by=GENERATED_BY + "; Meeting Analyst (silent bot)",
        evaluation=ev, notes=[], transcript=lines, mode="analyst"))
    cancelled = Interview(id=_id("2", 91), created_at=now - timedelta(days=3), updated_at=now - timedelta(days=2),
                          setup=base.model_copy(update={"role_title": "Sales Development Representative", "candidate_name": "Mei Tanaka",
                                                        "candidate_email": "mei.tanaka@example.com"}),
                          plan=plan, plan_approved_at=now - timedelta(days=3), status="ready", cancelled_at=now - timedelta(days=2), demo=True)
    store._run(interviews.insert().values(id=cancelled.id, **store._interview_columns(cancelled)))
    return 2


def _audit(store: Store, now: datetime) -> None:
    """A little recruiter activity for the dashboard and the interviews' activity lists."""
    entries = [
        (now - timedelta(days=13, hours=4), "role_created", None, "Senior Backend Engineer"),
        (now - timedelta(days=12, hours=2), "role_hiring_updated", None, "published"),
        (now - timedelta(days=9), "decision_recorded", _id("2", 1), "advance"),
        (now - timedelta(days=8), "decision_recorded", _id("2", 2), "hold"),
        (now - timedelta(days=8), "decision_recorded", _id("2", 3), "reject"),
        (now - timedelta(days=5), "decision_recorded", _id("2", 8), "advance"),
        (now - timedelta(days=2), "interview_cancelled", _id("2", 91), ""),
    ]
    for ts, action, interview_id, detail in entries:
        store._run(audit_events.insert().values(ts=ts.isoformat(), action=action, interview_id=interview_id,
                                                detail=f"[demo] {detail}".strip(), actor="Demo recruiter"))


# ---------- removing ----------

def demo_ids(store: Store, everywhere: bool = False) -> dict[str, list[str]]:
    """Ids of the demo records present (in the request's workspace, unless `everywhere`): only those with the demo
    marker and the demo id prefix."""
    def ids(table):
        stmt = sa.select(table.c.id, table.c.data)
        return [r["id"] for r in store._rows(stmt if everywhere else store._scope(stmt, table))
                if r["id"].startswith(PREFIX) and '"demo":true' in r["data"].replace(" ", "")]
    return {"roles": ids(roles), "interviews": ids(interviews), "applications": ids(applications)}


def _demo_workspaces(store: Store) -> set:
    rows = store._rows(sa.select(roles.c.workspace_id, roles.c.data).where(roles.c.id.like(f"{PREFIX}%")))
    return {r["workspace_id"] for r in rows if '"demo":true' in r["data"].replace(" ", "")}


def remove(store: Store) -> dict:
    found = demo_ids(store)
    for interview_id in found["interviews"]:
        store.delete_interview(interview_id)  # sessions, transcripts, notes, reports, links follow (foreign keys)
    for application_id in found["applications"]:
        store.delete_application(application_id)
    for role_id in found["roles"]:
        store.delete_role(role_id)
    store._run(audit_events.delete().where(sa.or_(audit_events.c.interview_id.in_(found["interviews"] or [""]),
                                                  audit_events.c.detail.like("[demo]%"))))
    return {k: len(v) for k, v in found.items()}


def status(store: Store) -> dict:
    found = demo_ids(store)
    return {"installed": bool(found["roles"] or found["interviews"]), **{k: len(v) for k, v in found.items()}}
