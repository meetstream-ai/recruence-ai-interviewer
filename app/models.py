"""Domain models.

Recruiter input (InterviewSetup) -> approved Plan -> InterviewContext (what a session
runs on) -> transcript, notes and an EvaluationReport.

Models returned by an LLM (GeneratedPlan, LLMEvaluation) carry no numeric schema
constraints, because structured outputs don't enforce them; code checks ranges.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, StringConstraints, computed_field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


InterviewType = Literal["behavioral", "technical", "screening", "mixed"]
InterviewStatus = Literal["draft", "ready", "scheduled", "in_progress", "completed", "failed"]
Decision = Literal["advance", "hold", "reject"]
Recommendation = Literal["strong_yes", "yes", "no", "strong_no", "insufficient_evidence"]
RequirementVerdict = Literal["meets", "does_not_meet", "not_enough_evidence"]
RequirementText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
SpeakerRole = Literal["interviewer", "candidate", "system"]


# ---------- recruiter input ----------


class CompetencyInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    rubric: str = Field(default="", max_length=2000)


class RoleSetup(BaseModel):
    """The job: what every candidate for it is interviewed on. The job description is untrusted text."""

    role_title: str = Field(min_length=1, max_length=200)
    level: str = Field(default="", max_length=100)
    job_description: str = Field(min_length=1, max_length=30_000)
    interview_type: InterviewType = "mixed"
    competencies: list[CompetencyInput] = Field(default_factory=list, max_length=8)
    # Must-haves judged Meets / Does not meet / Not enough evidence, e.g. "3+ years running Python services".
    requirements: list[RequirementText] = Field(default_factory=list, max_length=10)
    question_count: int = Field(default=4, ge=1, le=10)
    duration_minutes: int = Field(default=20, ge=5, le=90)


class CandidateInput(BaseModel):
    """One candidate added to a role. The resume is untrusted text."""

    candidate_name: str = Field(min_length=1, max_length=200)
    candidate_email: str = Field(default="", max_length=320)
    resume_text: str = Field(default="", max_length=60_000)


class InterviewSetup(RoleSetup, CandidateInput):
    """What the recruiter provides for one interview: the job plus the candidate."""


# ---------- plan ----------


class Requirement(BaseModel):
    id: str  # r1, r2, ... in the order the recruiter listed them
    text: str


class Competency(BaseModel):
    id: str
    name: str
    description: str = ""
    # Optional scoring anchors, e.g. "1: no example. 3: one concrete example with outcome. 5: ..."
    rubric: str = ""


class PlannedQuestion(BaseModel):
    id: str
    text: str
    competency_ids: list[str]
    # What a strong answer covers. Private hints for follow-ups; never read out.
    look_for: list[str] = Field(default_factory=list)
    max_follow_ups: Optional[int] = None


class Plan(BaseModel):
    competencies: list[Competency]
    questions: list[PlannedQuestion]
    candidate_summary: str = ""  # short, job-relevant summary of the resume
    source: str = ""  # who produced it: "claude:...", "gemini:...", "template (no AI)", "edited by recruiter"


InterviewFormat = Literal["voice", "video", "proctored"]


class InterviewOptions(BaseModel):
    """How one interview runs. Set before launch (it shapes the invitation and booking page) or at launch."""

    # "ai": MIA conducts the interview. "analyst": a silent bot only transcribes a conversation a person leads.
    mode: Literal["ai", "analyst"] = "ai"
    # Analyst mode: the candidate's name as it appears in the meeting, to tell their lines from the interviewer's.
    candidate_meeting_name: str = Field(default="", max_length=200)
    # Post each planned question in the meeting chat as the AI asks it, word for word what it says (not on Zoom, where
    # MeetStream repeats chat messages). On by default: candidates can read the question while they hear it.
    questions_in_chat: bool = True
    # voice: audio only. video: the candidate's camera is recorded. proctored: camera and whole-screen sharing are
    # asked for, and screen-share activity MeetStream reports is logged for the recruiter (app/proctoring.py).
    format: InterviewFormat = "voice"
    require_camera: bool = True  # proctored only: the AI asks for the camera to be on
    require_screen_share: bool = True  # proctored only: the AI asks for the whole screen to be shared before question 1
    record_video: bool = False  # kept in step with format; stored interviews from before formats only have this

    @model_validator(mode="after")
    def _consistent(self) -> "InterviewOptions":
        if self.format == "voice" and self.record_video:
            self.format = "video"  # saved before formats existed, or sent by an older client
        if self.format == "proctored" and self.mode == "analyst":
            raise ValueError("A proctored interview needs the AI interviewer: in Meeting Analyst mode you lead the call yourself")
        self.record_video = self.format != "voice"
        return self


class Booking(BaseModel):
    """A self-scheduling link: the candidate picks one of these times for the meeting at meeting_link."""

    token: str  # the secret part of the public /book/<token> address
    meeting_link: str
    platform: str
    slots: list[datetime]
    created_at: datetime
    booked_at: Optional[datetime] = None  # when the candidate last picked a time; None while unbooked
    # The candidate must pick a time before this; None (links made before expiry existed): the last offered time.
    expires_at: Optional[datetime] = None


class Invitation(BaseModel):
    """A candidate's own interview link (/i/<token>). The link is stored as a SHA-256 hash for lookup
    (app/store.py invitation_links) and encrypted here so the hiring team can copy it again; `token` is only set on
    links from before that (moved to token_enc when the app starts)."""

    token: str = ""
    token_enc: str = ""
    # When the interview can be started: explicit, else around the booked time, else until expires_at (app/gateway.py).
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    created_at: datetime
    expires_at: datetime
    email_sent_at: Optional[datetime] = None
    email_error: str = ""
    opened_at: Optional[datetime] = None
    started_at: Optional[datetime] = None  # the last start; a start is refused while one is under way or done
    attempts: int = 0
    expiry_notified_at: Optional[datetime] = None  # the hiring team was told the link expired unused (once per link)

    @property
    def undelivered(self) -> bool:
        """The email failed and hasn't gone out since."""
        return bool(self.email_error and not self.email_sent_at)


class InterviewFlag(BaseModel):
    """Something on the candidate's link the hiring team should look at (a second device, many wrong codes). Evidence,
    never a verdict: nothing is rejected because of it."""

    at: datetime
    kind: str  # second_device, code_failures, ...
    detail: str = ""
    resolved_at: Optional[datetime] = None
    resolved_by: str = ""


class Interview(BaseModel):
    id: str
    created_at: datetime
    updated_at: datetime
    status: InterviewStatus = "draft"
    setup: InterviewSetup
    plan: Optional[Plan] = None
    plan_approved_at: Optional[datetime] = None
    meeting_link: str = ""
    platform: str = ""
    scheduled_at: Optional[datetime] = None
    consent_attested_at: Optional[datetime] = None
    decision: Optional[Decision] = None
    decision_note: str = ""
    decided_at: Optional[datetime] = None
    latest_session_id: Optional[str] = None
    last_error: str = ""
    # Set when the interview was added to a role: its plan is the role's approved plan, the same for every candidate.
    role_id: Optional[str] = None
    # Set while the candidate can choose their own time (app/booking.py).
    booking: Optional[Booking] = None
    options: InterviewOptions = Field(default_factory=InterviewOptions)
    invited_at: Optional[datetime] = None  # the last invitation email sent from the app
    # A role candidate who applied: the link they use to start the interview themselves, within its validity.
    invitation: Optional["Invitation"] = None
    application_id: Optional[str] = None
    demo: bool = False  # sample data (app/demo.py): shown as Demo, never run in a real meeting or emailed
    flags: list[InterviewFlag] = Field(default_factory=list)
    # Set when the recruiter cancels; launching, booking and inviting are refused until it's reopened.
    cancelled_at: Optional[datetime] = None
    # How it came about: "automatic" (an eligible application invited by the role's auto-invite, or an ATS's decision),
    # "manual" (a recruiter added or invited the candidate). "" for interviews from before this was recorded.
    assigned: Literal["", "automatic", "manual"] = ""


class EligibilityCriterion(BaseModel):
    """What an application must (mandatory) or should (preferred) show, e.g. "B.Tech or equivalent in CS"."""

    id: str = Field(default="", max_length=20)  # e1, e2, ... assigned on save
    text: str = Field(min_length=1, max_length=300)
    mandatory: bool = True
    kind: Literal["skill", "experience", "education", "other"] = "other"  # groups the screening score's breakdown


class ApplicationQuestion(BaseModel):
    id: str = Field(default="", max_length=20)  # a1, a2, ... assigned on save
    text: str = Field(min_length=1, max_length=300)
    required: bool = False


class JobDetails(BaseModel):
    """What the public application page says about the job, beyond the description."""

    responsibilities: str = Field(default="", max_length=4000)
    employment_type: str = Field(default="", max_length=60)  # e.g. Full-time, Contract
    location: str = Field(default="", max_length=200)
    work_arrangement: str = Field(default="", max_length=60)  # On-site, Hybrid, Remote


class Hiring(BaseModel):
    """How a role recruits: the application API for the company's website, screening, invitations and the pass mark.

    Separate from the interview itself (RoleSetup and the plan): eligibility is judged on the application,
    the interview on what the candidate says.
    """

    passing_score: int = Field(default=70, ge=0, le=100)  # out of 100; applied when shown, never stored on a candidate
    criteria: list[EligibilityCriterion] = Field(default_factory=list, max_length=15)
    published: bool = False
    apply_token: Optional[str] = None  # the job key in /api/public/apply/<token>; set when first published
    application_deadline: Optional[datetime] = None
    invitation_days: int = Field(default=5, ge=1, le=60)  # how long an invited candidate has to take the interview
    auto_invite: bool = True  # invite eligible candidates without waiting for the recruiter (kept equal to resume_review != "manual")
    # Who decides on each application (the role's setup, "How are candidates reviewed?"):
    #   auto:   Recruence's AI screens the resume and eligible applicants are invited straight away;
    #   manual: Recruence's AI screens as a suggestion, and a recruiter approves each invitation.
    resume_review: Literal["auto", "manual"] = "auto"
    # Screening score (0-100) an application needs to be invited automatically; below it, a person reviews it.
    # 0: only the mandatory criteria decide.
    screening_threshold: int = Field(default=0, ge=0, le=100)
    paused: bool = False  # published, but not taking applications for now
    job: JobDetails = Field(default_factory=JobDetails)
    # Extra questions on the application form, answered by the candidate (shown to the screening AI too).
    questions: list["ApplicationQuestion"] = Field(default_factory=list, max_length=10)
    # The meeting room candidates join when they start their interview (one reusable link, e.g. a recurring Zoom meeting's /j/ link).
    meeting_link: str = Field(default="", max_length=2000)
    options: "InterviewOptions" = Field(default_factory=lambda: InterviewOptions())

    @model_validator(mode="before")
    @classmethod
    def _review_from_auto_invite(cls, data):
        """Roles saved before resume_review existed: their auto_invite says which it was."""
        if isinstance(data, dict) and "resume_review" not in data and "auto_invite" in data:
            data = {**data, "resume_review": "auto" if data["auto_invite"] else "manual"}
        if isinstance(data, dict) and data.get("resume_review") in ("auto", "manual"):
            data = {**data, "auto_invite": data["resume_review"] != "manual"}
        return data


class Role(BaseModel):
    """A job with one approved question set, used for every candidate interviewed for it."""

    id: str
    created_at: datetime
    updated_at: datetime
    setup: RoleSetup
    plan: Optional[Plan] = None
    plan_approved_at: Optional[datetime] = None
    hiring: Hiring = Field(default_factory=Hiring)
    demo: bool = False  # sample data (app/demo.py): shown as Demo, never run in a real meeting


class InterviewContext(BaseModel):
    """The read-only view one session runs on."""

    interview_id: str
    role_title: str
    level: str = ""
    job_description: str = ""
    interview_type: InterviewType = "mixed"
    candidate_name: str
    candidate_summary: str = ""
    competencies: list[Competency]
    questions: list[PlannedQuestion]
    duration_minutes: int = 20
    requirements: list[Requirement] = Field(default_factory=list)
    mode: Literal["ai", "analyst"] = "ai"
    candidate_meeting_name: str = ""
    questions_in_chat: bool = True
    record_video: bool = False
    format: InterviewFormat = "voice"
    require_camera: bool = True
    require_screen_share: bool = True
    # The candidate says when an answer is finished ("that's my answer"); a pause alone doesn't end it.
    answer_handoff: bool = True

    @property
    def proctored(self) -> bool:
        return self.format == "proctored" and self.mode == "ai"

    @property
    def candidate_first_name(self) -> str:
        return self.candidate_name.split()[0] if self.candidate_name.split() else "there"


# ---------- LLM output: plan generation ----------


class GeneratedCompetency(BaseModel):
    name: str
    description: str


class GeneratedQuestion(BaseModel):
    text: str
    competency_names: list[str]
    look_for: list[str]


class GeneratedPlan(BaseModel):
    candidate_summary: str
    competencies: list[GeneratedCompetency]
    questions: list[GeneratedQuestion]


# ---------- runtime ----------


class Utterance(BaseModel):
    speaker: str
    role: SpeakerRole
    text: str
    ts: datetime = Field(default_factory=utcnow)
    question_id: Optional[str] = None


class AnswerNote(BaseModel):
    """MIA's note for the hiring team when it closes a question."""

    question_id: str
    note: str
    follow_ups: int = 0


# ---------- LLM output: evaluation ----------


class CompetencyScore(BaseModel):
    competency_id: str
    competency_name: str
    score: Optional[int]  # 1-5, clamped in code. None: not assessed (no evidence), which isn't the same as weak
    rationale: str
    evidence: list[str]


class RequirementResult(BaseModel):
    requirement_id: str
    requirement: str = ""  # the requirement's text, filled in by code
    verdict: RequirementVerdict
    rationale: str
    evidence: list[str]


class LLMEvaluation(BaseModel):
    recommendation: Recommendation
    summary: str
    competency_scores: list[CompetencyScore]
    strengths: list[str]
    areas_for_improvement: list[str]
    suggested_next_round_focus: list[str]
    confidence: Literal["low", "medium", "high"]
    # One per must-have requirement, when the interview has any.
    requirement_results: list[RequirementResult] = Field(default_factory=list)

    @property
    def unassessed(self) -> list[str]:
        """Competencies with no score. While there are any, the score can't say whether the candidate passed."""
        return [s.competency_name for s in self.competency_scores if s.score is None]


class EvaluationReport(BaseModel):
    session_id: str
    interview_id: str
    role_title: str
    candidate_name: str
    platform: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: int
    questions_planned: int
    questions_asked: int
    follow_ups_asked: int
    end_reason: str
    consent_given: bool
    generated_by: str
    evaluation: Optional[LLMEvaluation] = None
    evaluation_error: Optional[str] = None
    notes: list[AnswerNote]
    transcript: list[Utterance]
    # "recording": rebuilt from the call recording, speakers inferred, includes the AI's follow-ups.
    # "live": MeetStream's live transcript, which carries only human speech (no AI follow-ups).
    transcript_source: str = "live"
    mode: str = "ai"  # "ai" or "analyst" (a person led the interview)
    proctoring: Optional["ProctoringRecord"] = None  # proctored interviews only
    disclaimer: str = (
        "Advisory output from an AI system. It is not a hiring decision. A person must review it, "
        "together with the transcript, before any decision is made."
    )

    # Derived from the transcript and evaluation each time (app/metrics.py): served in the API, never
    # stored (the store excludes REPORT_DERIVED_FIELDS).
    @computed_field
    @property
    def talk(self) -> dict:
        from app.metrics import talk_time
        return talk_time(self)

    @computed_field
    @property
    def key_moments(self) -> list[dict]:
        from app.metrics import key_moments
        return key_moments(self)


REPORT_DERIVED_FIELDS = {"talk", "key_moments"}


ProctoringSource = Literal["app", "candidate", "meetstream_live", "meetstream_recording"]


class ProctoringEvent(BaseModel):
    """One entry in a proctored interview's timeline. `source` says how it is known:

    app: what this app itself did or saw (the AI was told to ask for screen sharing, at this time).
    candidate: what the candidate said or the AI reported they said. Not verified.
    meetstream_live / meetstream_recording: reported by MeetStream during the call, or found in its recording afterwards.
    """

    at: Optional[datetime] = None
    offset_seconds: Optional[float] = None  # from the bot's admission, when no wall-clock time is known
    kind: str  # e.g. screen_share_requested, screen_share_started, screen_share_stopped, screen_share_unavailable, camera_off
    source: ProctoringSource
    text: str
    participant: str = ""
    duration_seconds: Optional[float] = None


class ProctoringRecord(BaseModel):
    """What the recruiter sees about proctoring. Facts only: an interruption is a technical event, not misconduct."""

    require_camera: bool = True
    require_screen_share: bool = True
    consent_given: bool = False
    events: list[ProctoringEvent] = Field(default_factory=list)
    # From MeetStream's per-participant recording, when it was available: total time a screen was shared, and the gaps.
    screen_share_seconds: Optional[float] = None
    interruptions: Optional[int] = None
    # What couldn't be checked, in plain words (e.g. "MeetStream's recording wasn't ready within 5 minutes").
    unverified: list[str] = Field(default_factory=list)


EvaluationReport.model_rebuild()


# ---------- applications ----------

ApplicationStatus = Literal["screening", "eligible", "needs_review", "not_eligible", "invited", "withdrawn"]
ApplicationSource = Literal["apply_page", "careers_site", "recruiter", "ats"]
CriterionVerdict = Literal["met", "not_met", "unclear"]


class ApplicationInput(BaseModel):
    """What a candidate submits on the public application page. All of it is untrusted text."""

    full_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str = Field(default="", max_length=40)
    location: str = Field(default="", max_length=200)
    years_experience: Optional[float] = Field(default=None, ge=0, le=60)
    current_role: str = Field(default="", max_length=200)
    education: str = Field(default="", max_length=1000)
    links: str = Field(default="", max_length=500)  # LinkedIn, portfolio
    cover_note: str = Field(default="", max_length=4000)


class CriterionResult(BaseModel):
    criterion_id: str
    verdict: CriterionVerdict
    evidence: str = ""  # a quote from the application; "met" and "not_met" need one or become "unclear"
    reasoning: str = ""


class ScreeningOutput(BaseModel):
    """The screening model's answer, before the code applies the rules."""

    results: list[CriterionResult]
    summary: str = ""


class Screening(BaseModel):
    outcome: Literal["eligible", "needs_review", "not_eligible"]
    reasons: list[str] = Field(default_factory=list)
    results: list[CriterionResult] = Field(default_factory=list)
    summary: str = ""
    screened_by: str = ""
    screened_at: datetime
    error: str = ""  # set when the AI screening itself failed ("screening failed": a person reviews it)
    # Out of 100, from the verdicts above (app/recruiting.py score): only quoted "met" counts. None: not scored.
    score: Optional[int] = None
    breakdown: dict[str, int] = Field(default_factory=dict)  # per criterion kind (skill, experience, education, other)
    missing: list[str] = Field(default_factory=list)  # criteria not met or not shown


class Application(BaseModel):
    id: str
    role_id: str
    created_at: datetime
    updated_at: datetime
    status: ApplicationStatus = "screening"
    candidate: ApplicationInput
    resume_text: str = ""
    resume_filename: str = ""
    resume_error: str = ""
    screening: Optional[Screening] = None
    interview_id: Optional[str] = None  # created when the candidate is invited
    reviewed_by: str = ""  # set when a recruiter overrides the screening
    review_note: str = ""
    demo: bool = False
    # Where it came from: Recruence's application page, a company careers site (PUBLIC_CORS_ORIGINS), added by a recruiter,
    # or an external ATS (app/ats). Every source goes through the same screening, invitation and interview.
    source: ApplicationSource = "careers_site"  # "apply_page": Recruence's former application page (older records)
    external: Optional["ExternalRef"] = None  # the external ATS's ids, when it came from one
    sync: Optional["SyncState"] = None  # results sent back to that ATS
    answers: list["ApplicationAnswer"] = Field(default_factory=list)
    notes: list["RecruiterNote"] = Field(default_factory=list)


ApplicationSourceValues = ("apply_page", "careers_site", "recruiter", "ats")


class ExternalRef(BaseModel):
    integration_id: str
    provider: str  # greenhouse, lever, generic
    candidate_id: str = ""
    application_id: str = ""
    job_id: str = ""
    url: str = ""  # the record in the external ATS, when it has a link


class SyncState(BaseModel):
    status: Literal["pending", "synced", "failed", "not_needed"] = "pending"
    at: Optional[datetime] = None
    message: str = ""


class ApplicationAnswer(BaseModel):
    question_id: str = ""
    question: str = Field(max_length=300)
    answer: str = Field(default="", max_length=4000)


class RecruiterNote(BaseModel):
    at: datetime
    by: str = ""
    text: str = Field(min_length=1, max_length=4000)


Interview.model_rebuild()
Role.model_rebuild()
Application.model_rebuild()
