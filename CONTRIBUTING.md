# Contributing

Thanks for helping. Recruence is a self-hosted AI interviewing platform: FastAPI and SQLAlchemy on the server, a plain-JavaScript dashboard (no build step), MeetStream for meetings.

## Set up

Python 3.12+. Node is only needed for the syntax check below.

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # Windows: .venv\Scripts\python
.venv/bin/python -m uvicorn app.server:create_app --factory --port 8000
```

Open http://localhost:8000: the first-run setup guides you through access, database, AI provider, MeetStream and email. No `.env` is needed for development; `.env.example` documents every variable.

## Before opening a pull request

```bash
.venv/bin/python -m pytest -q                      # everything; no network, no paid APIs
TEST_DATABASE_URL=postgresql://... .venv/bin/python -m pytest -q   # the same suite on an empty PostgreSQL database
node --check app/web/app.js app/web/book.js app/web/interview.js
docker build -t recruence .               # if you changed the Dockerfile or dependencies
```

CI runs all of these on every pull request.

## Guidelines

- **Tests use fakes.** MeetStream, the AI providers and SMTP are replaced in tests (`tests/fakes.py`). Never add a test that needs a real key; say in the pull request what you verified live and how.
- **Untrusted input stays data.** Job descriptions, resumes, applications and anything a candidate says are wrapped and labelled in prompts and never followed as instructions.
- **No invented evidence.** Scores, verdicts and screening decisions need the candidate's own words; anything unclear goes to a person.
- **Schema changes** go through `app/store.py`: new tables are created on start; changes to existing tables need a numbered migration step (`_migrate_N`) and a test on both SQLite and PostgreSQL.
- **Secrets** never appear in API responses, logs, audit entries or test fixtures. Settings with secrets are stored encrypted (`app/secret_box.py`).
- **Public routes** (anything a candidate or MeetStream reaches) are listed in `app/security.py` and the tunnel rules in `docs/DEPLOYMENT.md`; keep them in step and behind an unguessable token.
- **UI text** is short, plain and specific. No marketing copy, no explanations that repeat what the screen shows.
- **Commits**: one change per commit, imperative subject ("Add …", "Fix …").

## Adding an AI provider

Implement `InterviewAI` (`app/llm/base.py`: `generate_plan`, `evaluate`, `screen`) with structured JSON output, add it to `build_ai` and `LLM_PROVIDERS`, add a connection test in `app/llm/probe.py`, its fields in `app/config_manager.py`, a card in the provider picker (`PROVIDERS` in `app/web/app.js`), and adapter tests with a mocked HTTP transport.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Please don't open public issues for vulnerabilities.
