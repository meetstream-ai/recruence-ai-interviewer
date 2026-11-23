# Architecture and implementation plan

## Principles

1. **The meeting platform is the interview room.** The candidate joins a normal Google Meet, Zoom or Teams call. We never build a video client.
2. **Meeting infrastructure is separate from interview logic.** The interview runner talks to one `MeetStreamBot`; platform differences are data in `platforms.py`.
3. **Our server owns the plan, the record and the evaluation.** MIA owns the real-time voice loop (listening, turn-taking, speaking, wording follow-ups). It gets each planned question from us through a tool call, so it cannot skip, reorder or invent planned questions.
4. **Candidate input is untrusted.** Resume, JD and speech are fenced in prompts and treated as data.
5. **AI output is advisory.** The recruiter's decision is stored separately from the AI recommendation.

## Components

```
                         ┌──────────────────────────── Recruence (FastAPI, one process) ────────────────────────────┐
 Recruiter browser       │                                                                                                     │
 ┌──────────────┐ token/ │  web/ dashboard ─▶ server.py (API) ─▶ store.py (SQL DB)                                              │
 │ list, create │ cookie │                        │                interviews · sessions · utterances · notes · reports · audit │
 │ plan, launch │───────▶│                        ├─▶ planning.py ─▶ llm/ (Claude | Gemini | offline template)                  │
 │ live, report │◀─ SSE ─│                        │                       ▲                                                      │
 └──────────────┘        │                        ▼                       │ evaluation.py (after the call)                       │
                         │        interview/manager.py  SessionManager · scheduler · retention · restart recovery            │
                         │                        │ builds InterviewContext from the stored interview                         │
                         │                        ▼                                                                             │
                         │        interview/runner.py  InterviewRunner: consent gate · plan progression · notes · transcript   │
                         │             │  interview/agent.py: MIA config (prompt, first message, tools)                         │
                         │             ▼                                                                                        │
                         │        meeting/meetstream.py  MeetStreamBot ◀── meeting/platforms.py (Meet · Zoom · Teams)            │
                         └──────────┬───────────────────────────────────▲──────────────────────▲─────────────────────────────────┘
          SDK: mia.create/delete,   │                                   │ /webhooks/meetstream │ /webhooks/mia/{token}/{tool}
          bots.create/remove/       │                                   │ /{token}/events      │ next_question, end_interview
          status/delete_data        ▼                                   │ /{token}/transcript  │ (bearer + X-Bot-ID)
                            ┌──────────────────────────── MeetStream ─────────────────────────────┐
                            │ bot in Google Meet / Zoom / Teams  ·  MIA: STT → LLM → TTS            │
                            └───────────────────────────────────────────────────────────────────────┘
```

| Brief component | Module | Responsibility |
|---|---|---|
| Recruiter dashboard | `app/web/`, `app/server.py` | Create interviews, review/approve plans, launch or schedule, watch live, review reports, decide, delete data |
| Interview orchestrator | `app/interview/manager.py`, `runner.py` | Session lifecycle and state, stages, persistence of every event, scheduling, recovery after restart |
| Recruence interviewer | `app/interview/agent.py`, `runner.py` | Agent config (prompt, first message, tools); tool handling with duplicate-call guard, consent gate, time budget |
| Interview context | `app/models.py` (`InterviewContext`), `store.py` | Role + JD, candidate + resume summary, competencies, plan; answers so far and remaining questions live in the runner |
| Transcription pipeline | `app/meeting/meetstream.py` | Live `sentence` webhooks: de-duplication, bot-id check, bot's own lines separated, echo filter |
| Evaluation engine | `app/evaluation.py`, `app/llm/*` | Rubric scoring, verbatim evidence filter, score clamping, report assembly |
| Report generation | `EvaluationReport`, report view | Transcript, scorecard, strengths, areas for improvement, notes, disclaimer, JSON export |
| Persistence | `app/store.py` | SQLite file by default, or PostgreSQL via `DATABASE_URL` (SQLAlchemy Core, schema migrations on start); interviews, sessions, utterances, notes, reports, audit events; retention purge |
| Platform adapters | `app/meeting/platforms.py` | Link validation and per-platform options (lobby limits, setup notes) for Meet, Zoom and Teams over one MeetStream client |

Why one MeetStream client rather than three adapters: `create_bot` is platform-agnostic (MeetStream detects the platform from the link). The differences we must respect are link rules, lobby limits, setup requirements and attribution quality, so they are data. A non-MeetStream meeting backend would be a second implementation of `MeetStreamBot`'s small surface (`join`, `leave`, `erase`, webhook handlers).

## Interview lifecycle

```
created ─▶ joining ─▶ awaiting_consent ─(next_question, candidate_consented=true)─▶ questioning ─(plan done / time up)─▶ closing
   │          │               │                                                                                  │
   │          │               └─(candidate declines → end_interview)─────────────────────────────────────────────┤
   │          └─(denied / lobby timeout / MIA config error)─▶ failed (agent config deleted, nothing to evaluate)    │
   └─(recruiter ends early, meeting ends)─────────────────────────────────────────────────────────────────────────┤
                                                                                                                  ▼
                                                                          leaving ─▶ evaluating ─▶ completed (report saved)
```

Interview record status: `draft` (no approved plan) → `ready` → `scheduled` → `in_progress` → `completed` | `failed`. Each launch is a new session, so a failed interview can be launched again.

## Data model (SQLite or PostgreSQL)

| Table | Contents |
|---|---|
| `roles` | a job (JD, type, criteria, requirements, length) and its approved question set, shared by every candidate added to it (`app/roles.py`) |
| `interviews` | setup (JD, resume, type, criteria), plan JSON, status, meeting link, schedule, consent attestation, recruiter decision; `role_id` when created from a role, whose questions it copies and can't change |
| `sessions` | one per launch: platform, bot id, agent config id, state, timestamps, end reason |
| `utterances` | transcript lines with speaker, role, question id |
| `notes` | MIA's per-answer notes for the hiring team |
| `reports` | the evaluation report JSON |
| `audit_events` | action, ids, timestamps; never candidate content |

Deleting an interview cascades locally and asks MeetStream to erase each bot's media and transcripts.

## Security and privacy

- **Recruiter access:** `APP_ACCESS_TOKEN`, as a bearer header or exchanged for an HttpOnly cookie on the sign-in screen. If unset, only direct localhost requests are served (development). For teams, a trusted SSO proxy (`PROXY_AUTH_SECRET` sent in `X-MIA-Proxy-Secret`) supplies each user's email and groups, mapped to viewer / recruiter / admin; audit entries record the user.
- **Public tunnel:** requests carrying proxy headers may reach `/webhooks/*` only.
- **MeetStream callbacks and live transcripts** are unsigned (documented), so each session has a random path token and every payload's `bot_id` must match the session's bot.
- **MIA tools:** a per-session bearer token in the agent config, plus `X-Bot-ID` must match our bot. The agent config is deleted when the session ends.
- **Retention:** `DATA_RETENTION_DAYS` purges finished interviews; `MEETSTREAM_RETENTION_HOURS` sets MeetStream's recording retention; recruiters can delete an interview at any time (local + MeetStream `delete_data`).
- **Consent:** a recruiter attestation is required to launch; MIA discloses AI use, recording and transcription and must confirm consent before question 1 is released.

## Implementation plan

| Step | Deliverable | Done when |
|---|---|---|
| 1 | Models, SQLite store, platform rules | Unit tests for store CRUD, retention, link validation |
| 2 | MeetStream client wrapper (join, lifecycle, transcript webhooks, leave, erase) | Tests against a fake SDK client |
| 3 | Plan generation (Claude, Gemini, template) + validation | Plans always have valid ids and mappings |
| 4 | MIA agent config + interview runner (consent gate, plan progression, notes, end) | State-transition and question-progression tests |
| 5 | Evaluation + report | Evidence filter and score clamping tests |
| 6 | Session manager: persistence, scheduler, retention, restart recovery | Report readable after restart; scheduled interview launches |
| 7 | API + access control + dashboard | Full recruiter flow over HTTP with a fake MeetStream |
| 8 | README, troubleshooting, limitations | A newcomer can run it |
| 9 | Live interviews on Google Meet, Teams and Zoom | Full AI interviews in real calls, with a person as the candidate |

## Deviations from the brief

- **Scheduling** is done by the app's own scheduler (the server must be running). MeetStream's `join_at` was not used because each session creates its own MIA agent config right before joining, and `join_at` with MIA is not documented. For the same reason MeetStream's calendar auto-join (`calendar.schedule_event`, auto-schedule) isn't used: the app only reads the connected calendar's events (`calendar.events`, `app/meeting/calendar.py`) so the recruiter can pick a meeting's link and start time, and its own scheduler still starts the bot.
- **Resume input**: paste, or upload PDF / Word (.docx) / text. The server extracts the text (pypdf, python-docx) and keeps only the text. Scanned PDFs without a text layer are reported, not OCR'd.
- **ATS integration** is generic: a signed export of reports and decisions to an endpoint the operator runs (`app/export.py`), plus JSON download. No ATS-specific adapter.
- **One recruiter role**: a single access token, no per-user accounts.
- **No rehearsal mode without MeetStream**: the end-to-end path is tested with a fake MeetStream client in the test suite.
