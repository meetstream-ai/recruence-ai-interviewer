<div align="center">

<img src="app/web/logo.png" alt="Recruence logo" width="112" />

# Recruence

**An AI interviewer that joins the candidate's Google Meet, Zoom or Teams call, asks your approved questions and hands back an evidence-backed report — self-hosted, with the AI and database you choose.**

[![Download](https://img.shields.io/badge/download-latest%20release-2563eb)](https://github.com/meetstream-ai/Recruence-AI-Interviewer/releases/latest)
[![CI](https://github.com/meetstream-ai/Recruence-AI-Interviewer/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/meetstream-ai/Recruence-AI-Interviewer/actions/workflows/ci.yml)
![Platforms](https://img.shields.io/badge/Windows%20%7C%20macOS%20%7C%20Linux%20%7C%20Docker-2563eb)
![Python](https://img.shields.io/badge/python-3.12-3776ab)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e)](LICENSE)

[Quick start](#-quick-start) · [Features](#-features) · [Screenshots](#-screenshots) · [How it works](#-how-it-works) · [Providers](#-choosing-your-providers) · [Configuration](#%EF%B8%8F-configuration) · [FAQ](#-faq)

</div>

&nbsp;

A recruiter turns a job description into a structured interview plan once, per role. Candidates apply on the company's own careers website; Recruence screens each application against the role's requirements, invites the eligible ones and gives each a personal interview link. The candidate starts the interview when it suits them: **[MeetStream](https://meetstream.ai)**'s voice agent joins their call, asks for consent, asks the approved questions one by one with adaptive follow-ups, and leaves. Afterwards Recruence scores every competency **with verbatim quotes as evidence**, ranks the role's candidates, and a person makes the decision.

Recruence is an open-source companion app for MeetStream's meeting bot API and MIA voice agent.

## 🚀 Quick start

**[⬇ Download the latest release](https://github.com/meetstream-ai/Recruence-AI-Interviewer/releases/latest)** for Windows, macOS or Linux, open it and follow the seven-step setup. Or run it as a server:

```bash
docker compose up -d --build                 # → http://localhost:8000
docker compose logs app | grep "setup code"  # the one-time code for the first-run setup
```

## 📥 Installation

| Platform | Get it | First launch |
|---|---|---|
| **Windows** | `Recruence-Setup-<version>.exe` | SmartScreen: **More info → Run anyway** (unsigned build) |
| **macOS** | `Recruence-<version>-macOS-arm64.dmg` (Apple silicon) · `…-macOS-x64.dmg` (Intel) | Drag to Applications, then **right-click → Open** once (unsigned build) |
| **Linux** | `Recruence-<version>-Linux-x86_64.AppImage` | `chmod +x Recruence-*.AppImage`, then run it |
| **Docker** | `docker compose up -d --build` | <http://localhost:8000> and the setup code from the log |
| **Source** | Python 3.12 (+ Node 22 for the desktop app) | [Local install](#local-python-312) |

> [!TIP]
> No `.env`, no database setup, no migrations: the first-run setup configures everything and the app creates and upgrades its own schema. The desktop app keeps its data in your user folder (`%APPDATA%\Recruence`, `~/Library/Application Support/Recruence`, `~/.config/Recruence`), never in the install folder. Details: [docs/ELECTRON.md](docs/ELECTRON.md).

Interviews in meetings and candidate links need a public HTTPS address for the app (a domain, or a tunnel such as `cloudflared tunnel --url http://127.0.0.1:8765`): see [docs/INTERVIEW_SETUP.md](docs/INTERVIEW_SETUP.md). Everything else works on your own computer.

## 💡 Why Recruence?

- **Every candidate gets the same interview.** Questions are written and approved once per role and released one at a time by the app, so the voice agent can't skip, reorder or invent them.
- **Evidence, not vibes.** Each score and each must-have verdict quotes what the candidate actually said; without a quote it stays "Not enough evidence".
- **A person decides.** The AI recommends; the recruiter's decision is stored apart from it.
- **Candidates set the pace.** Pauses count as thinking time; the candidate says "that's my answer" when they've finished.
- **Bring your own AI.** Gemini, Claude, OpenAI or any OpenAI-compatible server, xAI, or a local Ollama model for plans and evaluations; the voice parts come from the same provider wherever MeetStream supports it.
- **Your data stays where you put it.** Local SQLite by default, or your own PostgreSQL (Neon, Supabase, RDS…). Self-hosted, no telemetry.

## ✨ Features

- 🧾 **Roles and plans** — a job description becomes competency-mapped questions with private "look for" hints; edit, reorder and approve once for every candidate.
- 🌐 **Careers-site intake** — your own careers website lists published jobs and posts applications (with PDF/Word resumes) to Recruence; applicants get a confirmation email. [`examples/demo-company-site`](examples/demo-company-site) shows how.
- 🔎 **Screening** — every application is checked against the role's must-have and preferred requirements, each verdict quoting the application; eligible candidates can be invited automatically.
- 🔐 **Candidate gateway** — a personal interview link, a one-time code emailed to the address they applied with, one device at a time, inside the interview's time window.
- 🎙️ **AI-led interviews** — MeetStream's voice agent in Google Meet, Teams or Zoom: consent first, the approved questions, adaptive follow-ups within enforced limits, live transcript in the dashboard. Or **human-led**, with a silent bot that only transcribes.
- 🛟 **Call monitoring** — Recruence watches every call: an agent that doesn't start, gets stuck, misses a request to stop, loses its voice or doesn't hang up is flagged, explained to the candidate and, where needed, ended cleanly.
- 📊 **Reports and ranking** — 1–5 per competency with verbatim evidence, must-have verdicts, key moments, talk time; role reports compare and rank candidates against a pass mark.
- 📅 **Scheduling** — start now, schedule, pick a meeting from your calendar, or let candidates choose a time on a private booking page with an `.ics` invite; one meeting room is never double-booked.
- 👥 **Workspaces and accounts** — independent recruiting spaces in one database, joining codes, owner / admin / recruiter roles, audit log.
- 🔌 **Integrations** — a signed report webhook and a generic signed ATS connector; Greenhouse and Lever connectors under advanced integrations.
- 🖥️ **Desktop and server** — an Electron app for Windows, macOS and Linux, or Docker behind a reverse proxy, with several interviews running at once.

<details>
<summary><b>Every feature in detail</b></summary>

| | |
|---|---|
| Roles | Write and approve a job's questions once, then add candidates: every candidate gets exactly the same questions, which can't be changed on their individual interview. The role page ranks candidates from their stored evaluations (overall score, then requirements met; ties share a rank; failed or missing evaluations are listed apart, never ranked), compares them side by side (recommendation, score per competency, verdict per requirement, decision) and writes a comparative summary: who leads each competency, where requirements still lack evidence |
| New interview | Start from scratch or from an existing role: use the role's approved questions as they are, or copy its details and tailor them. A 15-minute time picker with the time zone shown, used for every time you pick |
| Interview setup | Job description, resume (paste, or upload PDF / Word .docx / text), interview type (behavioural, technical, screening, mixed), evaluation criteria with optional rubrics, question count, length. A one-off interview tailors its plan to the resume |
| Plan generation | Your AI provider writes questions mapped to competencies with private "look for" hints; code validates it. Without an AI key, a clearly labelled template plan |
| Plan review | Edit, reorder, add or remove questions; set follow-up limits; approve |
| Meetings | Google Meet, Zoom, Microsoft Teams through MeetStream, with per-platform link checks and lobby limits |
| Live interview | MIA voice agent; consent before question 1; adaptive follow-ups, each one allowed by the app within the question's limit; live transcript and interviewer notes in the dashboard. Each planned question can also be posted in the meeting chat as it's asked (Meet and Teams) |
| Answer hand-off | The opening and the chat notice tell candidates that pauses are fine and to say "that's my answer" when they've finished. The agent treats a pause as thinking time and moves on only once the answer is complete; it waits up to 8 seconds of silence before replying. A setting turns this off |
| Call monitoring | Alerts and chat messages when the agent hasn't started 90 seconds after joining (and the call ends at 5 minutes, with nothing kept), when a question stays open far too long, when the voice agent reports an error (the call ends if it doesn't recover within 2 minutes), and when the candidate asks to stop and the agent carries on (ended 25 seconds later). If the questions are finished and the agent doesn't hang up, the app ends the call 45 seconds later. On Zoom the clocks start once the host allows recording, and a host who refuses recording ends the interview at once |
| Meeting Analyst | A person leads the interview; a silent bot (no MIA agent) only transcribes, and the report evaluates the conversation and lists the questions the interviewer asked |
| Formats | Voice (audio only), Video (the camera is recorded) or Proctored, chosen per interview before anything is sent, so the invitation and booking page say what's needed |
| Proctoring | The AI asks for the camera and the whole screen and holds question 1 until the candidate says it's done (or can't). After the call, MeetStream's per-participant recording gives when the screen was shared, for how long, and any gaps. The report lists each entry with how it is known (this app, the candidate's word, MeetStream) and what couldn't be checked: the camera state during the call, the screen's content, other devices. Interruptions are technical events, never findings about the candidate |
| Full transcript | After the call the recording is transcribed and the speakers inferred, so the record includes the AI's spoken follow-ups (MeetStream's live transcript carries only human speech); when the two don't line up, the report uses the live transcript |
| Evaluation | 1–5 per competency, verbatim evidence only, strengths, areas for improvement, next-round focus, confidence |
| Must-have requirements | Optional list in the setup (e.g. "3+ years running Python services"); each is judged Meets / Does not meet / Not enough evidence. A verdict needs a verbatim quote from the candidate, otherwise it stays "Not enough evidence" |
| Key moments, talk time | The report lists where each evidence quote was said and jumps to that line in the transcript; talk time shows the candidate's share of the words, overall and per question (computed by code, not AI) |
| Dashboard | Overview, activity chart (created, run, completed, decided; 14 / 30 / 90 days) and hiring by role (applied, to review, invited, interviewed, passed, top candidate). The Interviews page lists every interview with search and filters by stage, platform, AI recommendation and decision |
| Recruiting | Publish a role: candidates apply on your **company's own careers website**, which reads the job from and sends applications to the role's application API (`/api/public/apply/<job key>`), until an optional deadline. Recruence hosts no application page; `examples/demo-company-site` shows a site doing it. Applicants get an email confirming their application arrived. Each application is screened against the role's eligibility requirements (mandatory or preferred): the AI must quote the application for every met / not-met verdict, all mandatory met is eligible, a quoted not-met is not eligible, anything unclear (or no AI, or no readable resume) waits for a person. Eligible candidates are invited automatically: an interview with the role's questions and a personal link (`/i/<token>`) valid for the role's number of days. Each candidate can have their own meeting (set on the interview page), or share the role's room one at a time; the meeting's own link never goes out by email, for booked times either. The link opens only after a one-time code sent to the email address they applied with, on one device at a time, inside the interview's time window; a second device is refused and flagged for the hiring team. Candidates see the company's name (Settings → Workspace), never the product's. The candidate starts it when it suits them, the AI interviewer joins the interview room, and evaluation, ranking and the role report follow. One start per link (a failed bot join gives the attempt back); recruiters can send a new link or allow another attempt |
| Applications | Each application keeps its screening, answers, notes and the applicant's original resume file, which recruiters can download from the application |
| Role page | Candidates (every applicant and added candidate with their stage, rank, score out of 100 and pass/fail against the role's pass mark, filters, application review with the screening reasons and quotes), Role report (score comparison against the pass mark, competency comparison, strengths, weaknesses and observations per candidate, who needs further assessment), Setup (the five setup steps: questions, assignment, interview mode, settings with pass mark, requirements, deadline, invitation validity, interview room and format, then review and publish), Job. Changing the pass mark re-applies it; scores never change |
| Lifecycle | Each interview shows one stage: draft, ready, invitation sent, pending candidate, scheduled, in progress, evaluation pending, report ready, expired, cancelled, failed. Interviews can be cancelled (optionally emailing the candidate) and reopened. Background emails are retried twice |
| Review | Report page, print / save as PDF, JSON export, recruiter decision (advance / hold / reject) stored apart from the AI recommendation |
| Email | The applicant's confirmation; an invitation to the candidate (link, time window, the AI and recording notice, how to ask for a human interviewer or an accommodation), previewed before sending or copied into your own email; the access code (never in the subject line); a "report ready" note to the hiring team. Plain SMTP |
| Settings page | Change settings from the dashboard (admins): general, AI providers (with connection tests that list the provider's models), the AI interviewer, integrations, access and data. Saved in the database over `.env`; keys and passwords encrypted (Fernet) and never shown again. Most changes apply at once, the rest after a restart, and each says which. Regenerate or revoke the access token. Checks for the public address, the MeetStream key and email |
| Integrations | A signed export of each report and decision to your own endpoint (HMAC-SHA256, retried); a generic signed ATS connector for applications in and results out; Greenhouse and Lever connectors under Settings → Recruitment sources → Additional integrations (they need an organization account with the provider) |
| Privacy | Access token, accounts or your SSO proxy with roles, public URL limited to webhooks and candidate pages, retention purge, one-click deletion (local + MeetStream recordings), audit log without candidate content, no data kept if consent is declined |
| Scheduling | Start now, or at a set time (in-process scheduler). Pick the meeting from the calendar connected in MeetStream to fill in its link and start time; "Resync calendar" asks MeetStream to fetch it again, and the picker says when MeetStream can't read the calendar and how to reconnect it |
| Self-scheduling | Offer the candidate a few times and one reusable meeting link; they get a private page (`/book/<token>`), read how the interview works, confirm it, and pick, change or cancel a time (up to 30 minutes before). The bot then joins at that time. The link expires at a deadline you set (or the last offered time) and can be withdrawn. The invitation email carries the link and its deadline; a confirmation and an `.ics` calendar file follow |

</details>

## 📸 Screenshots

<div align="center">
<img src="docs/screenshots/dashboard.png" alt="Dashboard: interview totals, activity chart and hiring by role" width="900" />
<br /><sub>Dashboard: interviews, activity and hiring by role</sub><br /><br />
<img src="docs/screenshots/role-candidates.png" alt="A role's candidates ranked by interview score with pass or fail against the pass mark" width="900" />
<br /><sub>A role's candidates, ranked against the pass mark</sub><br /><br />
<img src="docs/screenshots/interview-report.png" alt="Evaluation report: score out of 100, recommendation, must-have requirements with quoted evidence" width="900" />
<br /><sub>An evaluation report: every verdict quotes the candidate</sub><br /><br />
<img src="docs/screenshots/role-report.png" alt="Role report comparing candidates' overall scores and competencies" width="900" />
<br /><sub>Role report: candidates compared per competency</sub><br /><br />
<img src="docs/screenshots/role-setup.png" alt="Role setup in five steps: questions, assignment, interview mode, settings, review" width="900" />
<br /><sub>Setting up a role in five steps</sub>
</div>

<sub>Screenshots use fictional sample data.</sub>

## 🔄 How it works

```
Recruiter dashboard ──▶ plan from JD + criteria ──▶ recruiter edits and approves
        │
        └─ candidate starts from their link, or the recruiter launches / schedules
                 │
                 ▼
   MeetStream bot + MIA agent joins Meet / Zoom / Teams ── admitted from the lobby
                 │  MIA speaks the notice and asks for consent
                 │  MIA ──next_question──▶ this app: releases planned questions one at a time
                 │  MIA ──follow_up──▶ this app: allows each follow-up within the question's limit
                 │  MeetStream ──live transcript, events──▶ this app: stored per question, watched for problems
                 │  MIA ──end_interview──▶ this app
                 ▼
   bot leaves · agent config deleted · this app's LLM scores each competency with verbatim evidence
                 ▼
   report: recommendation, scores + evidence, strengths, areas for improvement, transcript ──▶ recruiter decides
```

- **MIA owns the live conversation**: speech-to-text, turn-taking, interruptions, voice and the wording of follow-ups.
- **This app owns the interview**: the plan (served one question at a time, so the agent can't skip or reorder), the consent gate, follow-up limits, the time budget, call monitoring, the record, the evaluation and the review.

## 🧭 Getting started

### Local (Python 3.12)

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt          # Windows: .venv\Scripts\python
.venv/bin/python -m uvicorn app.server:create_app --factory --port 8000
```

Open http://localhost:8000. From the same machine no code is needed. To build the desktop app yourself: `npm install`, then `npm run package:win`, `package:mac` or `package:linux` on that system ([docs/ELECTRON.md](docs/ELECTRON.md)).

### First-run setup

Seven steps, each saved as you go and resumable after a restart:

1. **Welcome**: what's already configured, and personal or team use.
2. **Database**: the local SQLite file, or your own PostgreSQL (Supabase, Neon, Railway, RDS, self-hosted): tested and connected at once, so your account is created where it will live. An empty database is set up; one that already holds Recruence data is used as it is only when you confirm (then its own accounts sign in). Nothing is ever copied over, merged or deleted.
3. **Account**: your name, email and a password. The first account administers the installation and owns the first workspace. An access token (for scripts) or, on your own computer, no sign-in remain available under "Other ways to sign in".
4. **AI provider**: Gemini, Claude, OpenAI or compatible, xAI or Ollama, with your own key; the connection test lists the provider's models. Or continue without AI (template plans).
5. **MeetStream** (optional): your API key and public URL, each checked for real. Interviews can't run in meetings until both pass.
6. **Email** (optional): any SMTP server, with a test email. Needed for invitations and notices.
7. **Review**, then finish.

### Accounts, workspaces and databases

- **Accounts** live in the connected database: email and password (scrypt-hashed), sessions in HttpOnly cookies that expire after 14 days, password change signs other browsers out, reset by email when SMTP is set up. Once an account exists, everyone signs in, on the server itself too.
- **Workspaces** are independent recruiting spaces in one database: their own roles, candidates, interviews, reports, audit log and notification settings. Every request is limited to the signed-in person's current workspace in the data layer, whatever the endpoint. Switch between your workspaces from the picker in the header, without signing out.
- **Members**: an owner or admin creates a joining code (Settings → Workspace; it can expire, be replaced or turned off) and shares it. Someone with an account enters it, and nothing is visible to them until the owner or an admin approves the request. Roles: owner (everything, including transfer), admin (members and settings of the workspace), recruiter (roles, candidates, interviews and reports; no deleting).
- **Shared use needs a hosted database.** A team installation, joining codes and more than one account need PostgreSQL (Neon, Supabase, Amazon RDS or Aurora, your own); the local SQLite file is for one person.
- **Installation settings** (AI providers, MeetStream, email, database) are shared by every workspace in the database and changed only by the installation's admins (the first account).
- **Accounts** (Settings → Access, installation admins): everyone with an account in the database. Set a temporary password for someone locked out, make someone an installation admin (there's always at least one), and switch self-registration on or off. Every account has a workspace of its own and can create more; joining someone else's needs their joining code and approval.
- **Switching databases** (account menu → Switch database, admins): you're signed out and choose a saved connection or enter a new one. Afterwards only that database's accounts, workspaces and data exist; every session in the old one ends. Saved connections are encrypted on the server and never shown again. Not while an interview is running, and not when `DATABASE_URL` is set in the environment (it always wins). If nobody can sign in, `/#/connect` prints a one-time code in the server log to reach the same screen.

Workspace switching and database switching are different things: a workspace is a part of one database; a database is a separate set of everything.

### Preview the setup

Settings → System → **Preview setup** walks through every step of the first-run setup with nothing saved; every check is simulated and says so (type "bad" into a field to see the failure states).

Everything stays editable in **Settings**. Values in `.env` (see [`.env.example`](.env.example)) prefill the steps; an install with an access token and an AI provider set in the environment skips the setup, and an existing install is never sent through it.

## 🎙️ Running a live interview

1. **MeetStream.** Sign up at https://app.meetstream.ai and create an API key. Enter it in the setup (or Settings → MeetStream).
2. **Voice keys.** In the MeetStream dashboard → Configure → **Integrations**, add the keys for the voice agent's parts. Settings → AI interviewer lists exactly which ones your setup needs ([Choosing your providers](#-choosing-your-providers)).
3. **Public URL.** MeetStream must reach this app. For a quick start, a tunnel:
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```
   Put the `https://…trycloudflare.com` address in Settings → MeetStream → Public URL; the check confirms it reaches this app. Only webhooks and candidate pages are served through it; the dashboard stays local. For production, use a named tunnel or your own domain ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).
4. **Email** (Settings → Email) for invitations and access codes.
5. **Publish a role** (questions approved, interview room set) and let candidates apply from your careers site, or add a candidate on the role page.
6. **The interview.** The candidate starts it from their link, or you launch it from the interview page. **Admit "AI Interviewer"** from the meeting lobby; the agent introduces itself and asks for consent.
7. Follow the live transcript; the report appears when the interview ends. Record your decision.

Platform notes: **Google Meet**: start the meeting before the bot joins. **Microsoft Teams**: guests join from the lobby; free `teams.live.com` meetings work. **Zoom**: needs a Zoom Meeting SDK app configured in MeetStream, and the interview room must be a meeting in that app's own Zoom account (meetings in other accounts need Zoom's ZAK or OBF tokens). Use the meeting's `/j/` join link; the host allows the bot to record when it joins. Details: [docs/INTERVIEW_SETUP.md](docs/INTERVIEW_SETUP.md).

## 🐳 Self-hosting with Docker

```bash
docker compose up -d --build
docker compose logs app | grep "setup code"
```

Open http://localhost:8000, enter the setup code and complete the setup. To configure everything up front instead, `cp .env.example .env`, fill it in (at least `APP_ACCESS_TOKEN` and an AI key) and start: the setup is then skipped.

- **Sign-in is required:** inside the container, your browser's requests come from Docker's network, not from `localhost`, so the app asks for an account or the access token.
- **Data:** interviews, transcripts and reports are kept in the `mia-data` volume (SQLite at `/data/interviewer.db`), and the retention purge runs inside the container. For PostgreSQL instead, set `DATABASE_URL` and `POSTGRES_PASSWORD` in `.env` and run `docker compose --profile postgres up -d --build` (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)). Back up that volume, or delete it to erase everything.
- **Plain `docker run`** instead of compose: also pass `-e DATA_DIR=/data`, because `.env`'s `DATA_DIR=data` is relative and the app folder isn't writable. Compose sets this for you.
- **Secrets** come from `.env` at run time. They are never copied into the image (see `.dockerignore`).
- **Exposure:** the compose file binds port 8000 to `127.0.0.1` only. MeetStream needs a public HTTPS address for its webhooks: point a tunnel at `http://localhost:8000`, or put a reverse proxy with TLS in front, and set the address as `PUBLIC_BASE_URL`. Requests that arrive through a proxy can only reach webhooks and candidate pages.
- **Production:** a quick tunnel's address changes on every start, so use a named Cloudflare tunnel or your own domain with a reverse proxy ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)). The image runs as a non-root user, with a readiness check on `/api/health/ready` that reports the database.
- **Cloud backend for the desktop app:** run this image in the cloud with `REMOTE_DASHBOARD=true` (`deploy/cloud/`, e.g. on Railway with Neon) and point the desktop app at it (Settings → System → Desktop app connection). Candidates apply and take interviews while recruiters' computers are off. See [docs/CLOUD.md](docs/CLOUD.md).

## 🤖 Choosing your providers

Two kinds of AI are involved, and you choose each separately.

| | What it does | Where its keys live |
|---|---|---|
| **App LLM** | Screens applications, writes interview plans and evaluates finished interviews | Settings → AI provider (or `.env`) |
| **Voice agent** (MeetStream's MIA) | Listens, talks and asks follow-ups in the call | MeetStream dashboard → Configure → Integrations |

### App LLM (plans, screening and evaluation)

| `LLM_PROVIDER` | Needs | Notes |
|---|---|---|
| `auto` (default) | – | The first key set among `ANTHROPIC_API_KEY` (Claude), `GEMINI_API_KEY` (Gemini), `OPENAI_API_KEY` (OpenAI), then `XAI_API_KEY` (xAI); none: `offline` (Ollama is chosen explicitly) |
| `claude` | `ANTHROPIC_API_KEY` | Model `CLAUDE_MODEL` (default `claude-opus-5-5`); effort per task with `CLAUDE_PLAN_EFFORT`, `CLAUDE_EVAL_EFFORT` |
| `gemini` | `GEMINI_API_KEY` | Model `GEMINI_MODEL` (default `gemini-3.6-flash`) with `GEMINI_FALLBACK_MODELS` tried in turn when one is overloaded, out of quota or closed to your key. Use a key with billing for real hiring |
| `openai` | `OPENAI_API_KEY` for OpenAI; none for most self-hosted servers | Any server with an OpenAI-style `/chat/completions`: OpenAI itself, or your own model through `OPENAI_BASE_URL`. Model `OPENAI_MODEL` (default `gpt-4.1`) |
| `xai` | `XAI_API_KEY` | Model `XAI_MODEL` (default `grok-4.20-0309-non-reasoning`), through xAI's OpenAI-compatible API |
| `ollama` | `OLLAMA_MODEL` (a model you pulled) | Ollama at `OLLAMA_BASE_URL` (default `http://localhost:11434`), through its OpenAI-compatible endpoint. Nothing leaves your server |
| `offline` | – | No AI: a labelled template plan and a keyword heuristic instead of an evaluation |

Running plans and evaluations on your own model keeps candidate data on your machines. Examples:

| Server | `OPENAI_BASE_URL` | `OPENAI_MODEL` |
|---|---|---|
| OpenAI | (leave empty) | e.g. `gpt-4.1` |
| Ollama | `http://localhost:11434/v1` | the model you pulled, e.g. `qwen2.5:14b` |
| vLLM | `http://localhost:8000/v1` (use another port than this app's) | the served model name |
| LM Studio | `http://localhost:1234/v1` | the loaded model |

The answer must be JSON in a fixed shape: the app asks for it with a JSON schema, falls back to plain JSON mode if the server rejects schemas, and checks the result. A model that can't produce it falls back to the template plan, and the report shows the error; larger models do better.

### One provider for the whole interview

The AI provider you choose also runs the interview's conversation, speech-to-text and voice, wherever MeetStream's MIA supports it for that part. A part the provider doesn't cover shows as needing another provider, and an interview can't start until it has one (`app/providers.py`).

| AI provider | Plans, screening, evaluation | Conversation | Speech-to-text | Voice |
|---|---|---|---|---|
| OpenAI (`OPENAI_API_KEY`) | yes | yes | yes (`whisper-1`) | yes (`alloy`, `coral`, `nova`, …) |
| xAI (`XAI_API_KEY`, `XAI_MODEL`) | yes | yes | yes (`stt-1`) | yes (`ara`, `eve`, `leo`, `rex`) |
| Anthropic Claude | yes | yes | needs another provider | needs another provider |
| Google Gemini | yes | yes | needs another provider | needs another provider |
| Ollama | yes | needs another provider | needs another provider | needs another provider |

So the usual setup is: choose the provider, add its key and model, test the connection, save. MeetStream keeps its own copy of the voice providers' keys (an agent config names providers, never keys), so add the same key once in the MeetStream dashboard → Configure → Integrations; Settings lists which keys it needs. To fill a gap, or to use a different provider for one part on purpose, set that part below; empty means "from the AI provider".

### Voice agent

Recruence runs the voice agent in `pipeline` mode by default (Settings → AI interviewer → Voice, or `MIA_MODE`): speech-to-text, conversation and voice, each from the AI provider or set separately. The values are those in MeetStream's [MIA API guide](https://docs.meetstream.ai/guides/mia/mia-api-guide):

| Part | Setting (empty: from the AI provider) | Values |
|---|---|---|
| Speech-to-text | `MIA_STT_PROVIDER`, `MIA_STT_MODEL` | `xai` (`stt-1`), `deepgram` (`nova-2` default, `nova-3`, …), `openai` (`whisper-1`), `assemblyai` (`u3-rt-pro`, `universal-streaming`, …) |
| Conversation | `MIA_LLM_PROVIDER`, `MIA_LLM_MODEL` | `xai` (e.g. `grok-4.20-0309-non-reasoning`), `openai` (e.g. `gpt-4.1`), `google` (e.g. `gemini-3.6-flash`), `anthropic` |
| Voice | `MIA_TTS_PROVIDER`, `MIA_VOICE` | `xai` (`ara`, `eve`, `leo`, `rex`), `openai` (`alloy`, `coral`, `nova`, …), `elevenlabs` (a voice ID from your ElevenLabs account) |

**Recommended setups**

| Setup | Keys in MeetStream → Integrations |
|---|---|
| **Deepgram `nova-3` → Gemini `gemini-3.6-flash` → ElevenLabs** | Deepgram, Google (Gemini, with billing), ElevenLabs |
| **All-xAI**: xAI speech-to-text → `grok-4.20-0309-non-reasoning` → xAI voice | xAI |

In the pipeline, Google can run the conversation but not speech-to-text or the voice, so a Gemini-based setup adds a speech-to-text and a voice provider. Keep each provider's account in credit: when one runs out, MeetStream reports an agent error and Recruence alerts you and ends the call cleanly.

MeetStream also offers a `realtime` mode, where one speech-to-speech model hears, thinks and speaks (`MIA_REALTIME_PROVIDER`: `google`, `openai` or `xai`, with `MIA_REALTIME_MODEL` and `MIA_REALTIME_VOICE`). The pipeline is the recommended mode for interviews; the turn-taking settings (`MIA_*_ENDPOINTING_DELAY`, `MIA_INTERRUPT_*`, `MIA_VAD_MIN_SILENCE_MS`) apply to it.

## ⚙️ Configuration

All settings are environment variables; [`.env.example`](.env.example) documents each one. Admins can also change most of them on the Settings page: saved values override `.env` until reset there.

| Variable | Default | Purpose |
|---|---|---|
| `APP_ACCESS_TOKEN` | – | Sign-in token for scripts and single-user servers. Empty: accounts, or localhost only |
| `REMOTE_DASHBOARD`, `TRUSTED_PROXIES`, `INSTALLATION_ID` | `false`, private networks, – | The cloud backend: accounts sign in through its HTTPS proxy; needs PostgreSQL, `SETTINGS_ENCRYPTION_KEY` and an https `PUBLIC_BASE_URL` ([docs/CLOUD.md](docs/CLOUD.md)) |
| `PROXY_AUTH_SECRET`, `PROXY_USER_HEADER`, `PROXY_GROUPS_HEADER`, `PROXY_*_GROUPS`, `PROXY_DEFAULT_ROLE` | –, `X-Forwarded-Email`, `X-Forwarded-Groups`, –, `recruiter` | Team sign-in through your own SSO proxy, with viewer / recruiter / admin roles and named audit entries ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)) |
| `APP_NAME`, `TIME_ZONE`, `DEFAULT_DURATION_MINUTES` | `Recruence`, `UTC`, `20` | Name in the dashboard; time zone for times in emails; preselected interview length |
| `SETTINGS_ENCRYPTION_KEY` | generated | Fernet key for secrets saved on the Settings page. Empty: generated once at `DATA_DIR/settings.key` (back it up apart from the database) |
| `LLM_PROVIDER` | `auto` | `auto`, `claude`, `gemini`, `openai`, `xai`, `ollama` or `offline` |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` | –, `claude-opus-5-5` | Claude for plans, screening and evaluation |
| `CLAUDE_PLAN_EFFORT`, `CLAUDE_EVAL_EFFORT` | `medium`, `high` | Claude effort per task |
| `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS` | –, `gemini-3.6-flash`, 3.5 → 3.5 flash-lite | Gemini; the next model is tried when one is overloaded, rate-limited, unreachable or closed to your key |
| `GEMINI_PLAN_THINKING`, `GEMINI_EVAL_THINKING` | `low`, `medium` | Gemini 3 thinking level per task, tuned for speed |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` | –, `gpt-4.1`, OpenAI | OpenAI, or any OpenAI-compatible server such as Ollama, vLLM or LM Studio |
| `XAI_API_KEY`, `XAI_MODEL` | –, `grok-4.20-0309-non-reasoning` | xAI as the AI provider |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | `http://localhost:11434`, – | Ollama (`LLM_PROVIDER=ollama`) |
| `MEETSTREAM_API_KEY`, `PUBLIC_BASE_URL` | – | Live meetings |
| `BOT_NAME` | `AI Interviewer` | Name shown in the meeting |
| `MEETSTREAM_RETENTION_HOURS` | `24` | MeetStream recording retention |
| `WAITING_ROOM_TIMEOUT` | `600` | Lobby wait, capped per platform |
| `MIA_STT_PROVIDER`, `MIA_STT_MODEL`, `MIA_LLM_PROVIDER`, `MIA_LLM_MODEL`, `MIA_TTS_PROVIDER`, `MIA_VOICE`, `MIA_LANGUAGE` | empty: from the AI provider; `en` | A separate provider for one part of the interview ([One provider for the whole interview](#one-provider-for-the-whole-interview)) |
| `MIA_MIN_ENDPOINTING_DELAY`, `MIA_MAX_ENDPOINTING_DELAY` | `1.5`, `8.0` | Seconds of silence before the agent replies (dynamic between the two) |
| `MIA_ANSWER_HANDOFF` | `true` | Candidates say "that's my answer" when they've finished; pauses count as thinking time |
| `MIA_MODE`, `MIA_REALTIME_PROVIDER` | `pipeline`, `google` | `pipeline` (recommended) or `realtime` (one speech-to-speech model) |
| `MAX_FOLLOW_UPS_PER_QUESTION` | `2` | Default follow-up cap, enforced by the app (per-question override in the plan) |
| `REPORT_WEBHOOK_URL`, `REPORT_WEBHOOK_SECRET`, `REPORT_WEBHOOK_TRANSCRIPT` | –, –, `false` | Signed export of reports and decisions ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#sending-reports-to-your-ats)) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_STARTTLS`, `SMTP_SSL` | –, `587`, –, –, –, `true`, `false` | Email. Without `SMTP_HOST` and `SMTP_FROM` nothing is sent; invitations can still be previewed and copied |
| `MAIL_REPLY_TO`, `RECRUITER_CONTACT`, `NOTIFY_EMAILS`, `DASHBOARD_URL` | – | Reply-to address; how candidates ask for a human interviewer (shown in the invitation); who gets "report ready", booking and expired-link notes (comma-separated); the dashboard address for links in them |
| `APPLICATION_RECEIPT` | `true` | Email applicants from your careers site that their application arrived |
| `CHECK_PUBLIC_URL` | `true` | Check `PUBLIC_BASE_URL` resolves before each launch |
| `POSTCALL_TRANSCRIPT`, `POSTCALL_TIMEOUT_SECONDS` | `true`, `300` | Transcribe the recording after the call to capture the AI's follow-ups (billed by MeetStream) |
| `DATABASE_URL` | empty | Empty: SQLite in `DATA_DIR`. Or PostgreSQL (`postgresql://user:pass@host:5432/db`; `pip install -r requirements-postgres.txt`). Schema created and upgraded on start |
| `DATA_DIR`, `DATA_RETENTION_DAYS` | `data`, `30` | SQLite location and retention |

## 🔌 API

<details>
<summary><b>Every endpoint</b></summary>

The dashboard uses this API; it is also usable directly (bearer token or session cookie).

| Method | Path | |
|---|---|---|
| GET | `/api/health`, `/api/health/ready` | LLM, MeetStream and MIA status / readiness including the database (503 when it's down) |
| POST | `/api/auth/login` | Exchange the access token for a session cookie |
| POST | `/api/auth/register`, `/api/auth/account-login`, `/api/auth/logout` | Create an account (the first one in a database administers it) / sign in / sign out |
| POST | `/api/auth/reset-request`, `/api/auth/reset` | Email a password reset link / set the new password |
| GET / PUT | `/api/me`; POST `/api/me/password` | Your account and workspaces / your name / your password |
| POST | `/api/workspaces`, `/api/workspaces/switch`, `/api/workspaces/join` | Create a workspace (admins) / switch to one of yours / ask to join with a code |
| GET / PUT | `/api/workspaces/current` | The current workspace (with members and the joining code for owners and admins) / its name, description, report recipients, candidate notices |
| POST / DELETE | `/api/workspaces/current/code` | New joining code (expiry in days, or none) / turn it off |
| POST / PUT / DELETE | `/api/workspaces/current/members/{id}[/decision]` | Approve or reject a request / change a role (owner) / remove a member |
| GET / POST | `/api/connection/status`, `/switch-start`, `/test`, `/connect`, `/request-code`, `/unlock` | The database connection: switch to a saved or new database (admins, or with the code from the log) |
| GET / POST | `/api/interviews` | List with each interview's `stage` (optional `q`, `stage`, `status`, `platform`, `recommendation`, `decision`; `none` = not evaluated / undecided) / create (body: interview setup) |
| GET | `/api/activity?days=` | Created / run / completed / decided timestamps for the dashboard chart |
| GET / POST | `/api/roles` | List roles / create one (body: the job, without a candidate) |
| GET | `/api/roles/{id}` | The role and its candidates compared (recommendation, scores, requirement verdicts, decision) |
| GET | `/api/roles/{id}/ranking` | The role's ranking and comparative report, from stored evaluations only (scores out of 100, pass / fail against the pass mark) |
| GET | `/api/roles/{id}/pipeline` | Every candidate of the role with their stage, rank, score and result, plus counts |
| PUT | `/api/roles/{id}/hiring` | Pass mark, eligibility requirements, publish, deadline, invitation days, auto-invite, interview room, format |
| GET / POST | `/api/applications/{id}`, `/api/applications/{id}/decision` | An application with its screening; `{action: invite\|reject\|rescreen}` |
| GET | `/api/applications/{id}/resume` | The applicant's original resume file, as a download |
| GET / POST | `/api/interviews/{id}/invitation`, `/invitation/resend` | A candidate's interview link; a new link (`{days?, reopen?}`, the old one stops working) |
| GET / POST / DELETE | `/api/demo` | Sample data: status, load, remove (admin) |
| GET | `/api/insights/roles` | Hiring by role, for the dashboard |
| GET / POST / PUT / DELETE | `/api/database`, `/api/database/test` | The database in use; test, save (used from the next start) or remove a hosted PostgreSQL connection (admin) |
| GET / POST | `/api/public/apply/{token}`, `/api/public/interview/{token}`, `/start` | Candidate pages: the role and the application form (multipart, with the resume); the interview link and starting it |
| PUT | `/api/roles/{id}/setup` | Update the job (withdraws the questions' approval) |
| POST / PUT / POST | `/api/roles/{id}/plan/generate`, `/plan`, `/plan/approve` | Generate, edit, approve the role's questions |
| POST | `/api/roles/{id}/candidates` | `{candidate_name, candidate_email?, resume_text?}`: add a candidate; creates their interview with the approved questions |
| DELETE | `/api/roles/{id}` | Delete a role with no candidates (admin) |
| POST | `/api/interviews/{id}/invite/preview`, `/invite` | `{meeting_link?, when_text?}`: preview or send the candidate's invitation email |
| POST | `/api/resume/extract` | Upload a resume file (PDF, DOCX, text; max 5 MB) and get its text back, for a one-off interview |
| GET | `/api/interviews/{id}` | Interview, its stage, sessions, live session, recent activity |
| PUT | `/api/interviews/{id}/setup` | Update JD, resume, criteria (withdraws plan approval) |
| POST | `/api/interviews/{id}/plan/generate` | Generate the plan |
| PUT | `/api/interviews/{id}/plan` | Save an edited plan (validated) |
| POST | `/api/interviews/{id}/plan/approve` | Approve the plan |
| POST | `/api/interviews/{id}/launch` | `{meeting_link, consent_attested, scheduled_at?, mode?, candidate_meeting_name?, questions_in_chat?, format?}`: start now or schedule. `mode`: `ai` or `analyst` |
| POST | `/api/interviews/{id}/unschedule` | Cancel a scheduled start |
| POST | `/api/interviews/{id}/cancel`, `/reopen` | Cancel (`{notify_candidate?}`; unschedules and withdraws the booking link) or reopen |
| PUT | `/api/interviews/{id}/options` | How the interview runs: `mode`, `format` (`voice`, `video`, `proctored`), `require_camera`, `require_screen_share`, `questions_in_chat`, `candidate_meeting_name` |
| POST / DELETE | `/api/interviews/{id}/booking` | `{meeting_link, slots, expires_at?}`: create a self-scheduling link / withdraw it |
| GET | `/api/calendar/events` | Upcoming Meet / Zoom / Teams meetings from the calendar connected in MeetStream (read only), with any problem MeetStream reports reading it |
| POST / GET | `/api/calendar/resync`, `/api/calendar/status` | Ask MeetStream to fetch the calendar again / when it last did |
| POST | `/api/interviews/{id}/decision` | `{decision: advance\|hold\|reject, note}` |
| DELETE | `/api/interviews/{id}` | Delete the interview and its data; erase MeetStream recordings |
| GET | `/api/sessions/{id}` | Transcript, notes, report, live snapshot |
| GET | `/api/sessions/{id}/events` | Server-sent events while live |
| POST | `/api/sessions/{id}/end` | End early (the bot leaves; evaluation runs) |
| POST | `/api/sessions/{id}/evaluate` | Re-run the evaluation on a finished session (e.g. after adding an LLM key) |
| GET | `/api/sessions/{id}/report` | Report JSON |
| GET | `/api/audit` | Audit log |
| GET | `/api/settings` | What's configured, grouped, without secret values |
| GET / PUT | `/api/config` | Editable settings with their source (saved, `.env`, default) / save changes (admin; all or nothing) |
| DELETE | `/api/config/{key}` | Go back to the `.env` value (admin) |
| POST | `/api/config/test-ai`, `/api/config/access-token/regenerate`, `/revoke` | Test an AI provider; manage the access token (admin) |
| POST | `/api/settings/check/{public_url\|meetstream\|email}` | Run a check (email: `{to}`); admin only |
| POST | `/webhooks/meetstream/{token}/{events\|transcript}` | MeetStream callbacks (per-session token) |
| POST | `/webhooks/mia/{token}/{next_question\|follow_up\|end_interview}` | MIA custom functions (bearer + `X-Bot-ID`) |

</details>

## 🗂️ Project structure

```
app/
  server.py            FastAPI routes, candidate pages, webhooks, MIA tool endpoints
  security.py          sign-in, access control, the public-address boundary
  store.py             persistence (SQLite or PostgreSQL), schema migrations, audit log
  recruiting.py        applications, screening, invitations     gateway.py   candidate links and codes
  planning.py          plan building and validation             evaluation.py  scoring with evidence checks
  providers.py         which AI provider covers which part of the interview
  booking.py           self-scheduling and room bookings        ats/          ATS connectors
  llm/                 Claude, Gemini, OpenAI-compatible, Ollama, offline template; prompts
  meeting/             platforms.py (Meet / Zoom / Teams rules), meetstream.py (bot lifecycle), calendar.py
  interview/           agent.py (MIA config), runner.py (live interview, call monitoring), manager.py (sessions, scheduler)
  web/                 dashboard and candidate pages (vanilla HTML/JS/CSS, no build step)
desktop/               Electron shell, PyInstaller spec, launch check
docs/                  setup, deployment, architecture, screenshots
examples/              a demo company careers site
tests/                 pytest, no network (SQLite; PostgreSQL in CI), Playwright dashboard tests
```

## 🧪 Running tests

```bash
.venv/bin/python -m pytest -q        # Windows: .venv\Scripts\python -m pytest -q
```

500+ tests, no network, a throwaway SQLite database (set `TEST_DATABASE_URL` to an empty PostgreSQL database to run them there too): platform rules, the store and migrations, plans, screening and evaluation, the MeetStream bot and webhooks, the live interview runner (consent gate, question order, follow-up limits, call monitoring, every way an interview ends), the candidate gateway, careers intake, ATS connectors, access control, the full recruiter flow over HTTP, and the dashboard in a real browser (Playwright).

## 🔁 CI/CD

- **Every push and pull request** ([`ci.yml`](.github/workflows/ci.yml)): the test suite on **SQLite and PostgreSQL**, `pip-audit`, a syntax check of the dashboard scripts, the Windows installer built and launched, and the Docker image built and probed.
- **Every `v*` tag** ([`release.yml`](.github/workflows/release.yml)): the backend frozen with PyInstaller on each system's own runner, installers for **Windows, macOS (Apple silicon and Intel) and Linux** built, each launched once, and attached to a GitHub release. It can also be run by hand from the Actions tab to get the installers without publishing.
- **On demand** ([`b8-installer.yml`](.github/workflows/b8-installer.yml)): install, upgrade and uninstall on a clean Windows runner.

## 🗺️ Roadmap

- [ ] Code-signed Windows and macOS builds
- [ ] Desktop auto-update
- [ ] More ATS connectors
- [ ] Languages other than English

## 🩺 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Public address check: *reachable, but it isn't this app* | The tunnel leads to another site (e.g. your careers site) | Point a tunnel at Recruence itself (`127.0.0.1:8765` for the desktop app) and use its address |
| The AI interviewer doesn't speak; "voice agent reported an error" | A voice provider's account is out of credit or its key is wrong | Check that provider's key and credit in MeetStream → Integrations |
| The AI interviewer stops: *429 RESOURCE_EXHAUSTED* | Gemini's free tier ran out | A key with billing, or another provider for the conversation |
| A bot can't be created: *403 Low credits* | MeetStream credit ran out | Top up in MeetStream → Billing |
| Calendar picker: *MeetStream can no longer use its sign-in* | MeetStream's access to your calendar expired | Reconnect the calendar in MeetStream, then **Resync calendar** |
| Invitation *not delivered* | No working Public URL or email | Fix it in Settings, then **Send again** on the role |
| The bot waits in the lobby | Nobody admitted it | Admit "AI Interviewer" from the meeting's lobby |

More: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## ❓ FAQ

**Does candidate data leave my machine?** Only to the services you choose: MeetStream for the call and its recording (deleted after `MEETSTREAM_RETENTION_HOURS`) and your AI provider. With Ollama, plans and evaluations stay local; the live voice always runs in MeetStream's cloud.
**Do I need MeetStream?** For interviews in meetings, yes. Roles, applications, screening and reports work without it.
**Which AI models?** Gemini, Claude, OpenAI or compatible, xAI and Ollama for plans and evaluations; the voice agent takes what MeetStream supports ([Choosing your providers](#-choosing-your-providers)).
**Can a team share it?** Yes: one PostgreSQL database, workspaces with joining codes and roles.
**Can several interviews run at once?** Yes. Each interview is its own session, with its own bot, transcript and report. On a prepaid MeetStream account, up to 5 bots can be started per minute (50 per hour).
**Is it free?** MIT licensed, no telemetry. You pay only MeetStream and the AI providers you choose.

## 📚 Documentation

[Interview setup](docs/INTERVIEW_SETUP.md) · [Desktop app](docs/ELECTRON.md) · [Deployment](docs/DEPLOYMENT.md) · [Cloud backend](docs/CLOUD.md) · [ATS and careers sites](docs/ATS_INTEGRATIONS.md) · [Demo careers site](docs/DEMO_SITE.md) · [Architecture](docs/ARCHITECTURE.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) · [Security](SECURITY.md)

## 🤝 Contributing

Issues and pull requests are welcome: start with [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately: [SECURITY.md](SECURITY.md).

## ⚖️ Responsible use

- Tell candidates in advance that an AI conducts the interview, that it is recorded and transcribed, and how to get a human interviewer instead. The invitation says so, the app asks you to confirm it before a recruiter-launched interview, and the agent asks for consent again at the start.
- If the candidate declines, nothing is evaluated, their words are not kept, and MeetStream is asked to erase the recording.
- A person reviews every report. The AI recommendation is not a decision.
- Using AI in hiring is regulated in many places (for example the EU AI Act and New York City's rules on automated employment decision tools): get legal advice before using it with real candidates.
- Treat candidate data as sensitive: use accounts or an access token, keep the dashboard off the public internet, keep retention short.

## 📄 License

[MIT](LICENSE). Built on [MeetStream](https://meetstream.ai), FastAPI, SQLAlchemy, pypdf, python-docx and Electron.
