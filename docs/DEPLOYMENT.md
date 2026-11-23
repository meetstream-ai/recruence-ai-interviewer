# Deployment notes

How to run Recruence somewhere other than a laptop with a quick tunnel. For the container itself see [README → Self-hosting with Docker](../README.md#self-hosting-with-docker).

## The shape of a deployment

The app has two kinds of traffic, and they must be exposed differently:

| Traffic | Who sends it | Exposure |
|---|---|---|
| `/webhooks/*` | MeetStream (bot events, live transcript, MIA tool calls) | **Public HTTPS**, at the address in `PUBLIC_BASE_URL`. Protected by per-session secrets in the path and a bearer token for MIA tools |
| `/book/*`, `/api/public/booking/*`, and the page's assets (`/static/styles.css`, `book.js`, `logo.png`, `favicon.png`, `apple-touch-icon.png`) | Candidates choosing their interview time (self-scheduling) | **Public HTTPS**, same address. Protected by each booking's unguessable token; shows only the candidate's first name, the role and the offered times |
| Everything else (dashboard, `/api/*`) | Recruiters | **Not public.** Needs `APP_ACCESS_TOKEN` |

`app/security.py` enforces this: any request that arrives with proxy headers (`X-Forwarded-For`, `CF-Connecting-IP`, `Forwarded`, …) can reach only `/webhooks/*` and the booking page's paths. The one exception is your own sign-in proxy, which is trusted when it sends the shared secret ([below](#for-a-team-sign-in-through-your-identity-proxy-recommended)).

## Requirements

- **One process, one worker.** Live interview state is held in memory. Don't run several uvicorn workers or replicas; a second worker would not know about sessions the first one started. The Docker image already runs a single worker.
- **The server must stay up during interviews and at scheduled start times.** The scheduler runs in-process. A session that was live when the server stopped is marked failed on restart, and its bot is removed.
- **A stable public address.** A quick tunnel's URL changes on every start, and MeetStream only learns `PUBLIC_BASE_URL` when a session launches. Use a named tunnel or your own domain.
- **Outbound HTTPS** to `api.meetstream.ai` and to your LLM provider (Gemini or Anthropic).

## Exposing only the webhooks and the candidate pages

### Option A: named Cloudflare tunnel

A named tunnel keeps one hostname. Limit its ingress to the webhooks and the candidate pages (booking, application, interview link), so the dashboard can't be reached through it even by mistake:

```yaml
# ~/.cloudflared/config.yml
tunnel: <tunnel-id>
credentials-file: /path/to/<tunnel-id>.json
ingress:
  - hostname: interviews.example.com
    path: ^/(webhooks/|book/|apply/|interview/|api/public/(booking|apply|interview)/|static/(styles\.css|book\.js|apply\.js|interview\.js|logo\.png|favicon\.png|favicon\.ico|apple-touch-icon\.png)$)
    service: http://localhost:8000
  - service: http_status:404
```

Then set `PUBLIC_BASE_URL=https://interviews.example.com` and restart the app.

### Option B: your own domain with a reverse proxy

Forward only `/webhooks/*` and the candidate pages' paths, and answer everything else with 404. With Caddy (it obtains the TLS certificate itself):

```
interviews.example.com {
    @public path /webhooks/* /book/* /apply/* /interview/* /api/public/booking/* /api/public/apply/* /api/public/interview/* /static/styles.css /static/book.js /static/apply.js /static/interview.js /static/logo.png /static/favicon.png /static/favicon.ico /static/apple-touch-icon.png
    handle @public {
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        respond 404
    }
}
```

nginx works the same way: `location` blocks for those paths with `proxy_pass http://127.0.0.1:8000;` and `return 404` everywhere else. If you use neither self-scheduling nor public applications, forwarding `/webhooks/*` alone is enough. The application form accepts at most 10 applications an hour from one address (the tunnel's `CF-Connecting-IP`).

In both options the app should listen on `127.0.0.1` only. The compose file binds `127.0.0.1:8000`.

## Giving recruiters the dashboard

### For a team: sign in through your identity proxy (recommended)

Put an SSO proxy (for example [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) with Google Workspace, Microsoft Entra ID or Okta) in front of the dashboard. The app then knows each user's email and role, and every audit entry names who did it. The app never sees passwords.

The app trusts a proxied request only when it carries the shared secret. Everything else that arrives through a proxy can still reach only `/webhooks/*` and the booking page.

| Setting | Meaning |
|---|---|
| `PROXY_AUTH_SECRET` | Long random value. The proxy sends it in `X-MIA-Proxy-Secret` on every dashboard request. Empty: no proxy is trusted |
| `PROXY_USER_HEADER` | Header with the signed-in user's email (default `X-Forwarded-Email`) |
| `PROXY_GROUPS_HEADER` | Header with their groups, comma-separated (default `X-Forwarded-Groups`; empty: groups not used) |
| `PROXY_ADMIN_GROUPS`, `PROXY_RECRUITER_GROUPS`, `PROXY_VIEWER_GROUPS` | Comma-separated group names for each role. The highest matching role wins |
| `PROXY_DEFAULT_ROLE` | Role for users in none of those groups: `recruiter` (default), `viewer`, `admin`, or `none` to refuse them |

| Role | Can |
|---|---|
| viewer | Read interviews, live sessions and reports. Nothing that changes anything |
| recruiter | Everything a viewer can, plus create, plan, launch, end, re-evaluate and decide |
| admin | Everything, including deleting an interview with its data and reading the full audit log |

The proxy **must replace** any identity headers the browser sends, so nobody can claim to be someone else. It also needs to let `/webhooks/*` through without sign-in, for MeetStream.

Example with Caddy and oauth2-proxy (run with `--set-xauthrequest` on `127.0.0.1:4180`):

```
interviews.example.com {
    handle /webhooks/* {
        reverse_proxy 127.0.0.1:8000
    }
    handle /oauth2/* {
        reverse_proxy 127.0.0.1:4180
    }
    handle {
        request_header -X-Auth-Request-Email
        request_header -X-Auth-Request-Groups
        forward_auth 127.0.0.1:4180 {
            uri /oauth2/auth
            copy_headers X-Auth-Request-Email X-Auth-Request-Groups
            @signin status 401
            handle_response @signin {
                redir * /oauth2/sign_in?rd={scheme}://{host}{uri}
            }
        }
        reverse_proxy 127.0.0.1:8000 {
            header_up X-MIA-Proxy-Secret {env.MIA_PROXY_SECRET}
        }
    }
}
```

With that proxy, set `PROXY_USER_HEADER=X-Auth-Request-Email` and `PROXY_GROUPS_HEADER=X-Auth-Request-Groups`. Set the same secret in `PROXY_AUTH_SECRET` (app) and `MIA_PROXY_SECRET` (Caddy's environment).

The dashboard shows the signed-in user and role in its header. `APP_ACCESS_TOKEN` still works for direct requests, as an admin, for break-glass access.

### Without a proxy: direct connection

Recruiters need a connection that doesn't pass through a proxy:

- **SSH port forward** (simplest): `ssh -L 8000:127.0.0.1:8000 user@server`, then open http://localhost:8000 and sign in with `APP_ACCESS_TOKEN`.
- **A private network such as a VPN**: bind the app to the server's private address instead of `127.0.0.1` (for example `--host 10.0.0.5`, or change the compose port mapping to that address). Requests must arrive directly, without a proxy in between, and `APP_ACCESS_TOKEN` is required because they don't come from localhost.

Then everyone shares the one access token, as an admin, and the audit log can't tell them apart. Give the token only to people who may see every interview, and rotate it when someone leaves (change `APP_ACCESS_TOKEN` and restart; existing sign-ins stop working).

## First run

On a new install the dashboard opens the setup wizard. From the server's own machine it starts straight away; from anywhere else (Docker's port mapping, another machine) it asks for the one-time **setup code** printed in the log when the app starts:

```bash
docker compose logs app | grep "setup code"
```

The code works until setup is complete, and never through the public URL (proxied requests are refused). Progress is kept in `DATA_DIR/setup.json`, so a restart resumes where you left off. An install whose database already holds roles or interviews, or that has an access token (or SSO proxy) and an AI provider in its environment, is treated as set up and never shows the wizard.

## Secrets

- `.env` holds `MEETSTREAM_API_KEY`, the LLM key and `APP_ACCESS_TOKEN`. Keep it readable only by the account that runs the app. It's never copied into the Docker image.
- The voice-agent provider keys (xAI, Google, …) are stored in the MeetStream dashboard (Configure → Integrations), not here. Keep each provider account in credit, with a spending limit you're comfortable with: an exhausted account makes the voice agent fail with a 403 agent error, which Recruence reports and handles.
- Rotate every key before making the repository public, and whenever one has been pasted somewhere it shouldn't be.
- **Settings saved from the dashboard** (Settings, admins only) are stored in the database and override `.env` until reset. Keys and passwords among them are encrypted with `SETTINGS_ENCRYPTION_KEY`, or, when that is empty, with a key generated once at `DATA_DIR/settings.key`. Back that key up **apart from** the database backups: a database backup alone doesn't expose the saved secrets, and without the key they have to be entered again. On several machines sharing a PostgreSQL database, set the same `SETTINGS_ENCRYPTION_KEY` on each.

## Sending reports to your ATS

The app has no built-in adapter for a particular ATS. Instead it can POST each saved report, and each recruiter decision, to an endpoint you run: an ATS integration, an automation tool (Zapier, Make, n8n) or a small script that writes to your ATS's API.

| Setting | Meaning |
|---|---|
| `REPORT_WEBHOOK_URL` | Where to send. https only (plain http only to localhost) |
| `REPORT_WEBHOOK_SECRET` | At least 16 characters; signs every delivery |
| `REPORT_WEBHOOK_TRANSCRIPT` | `true` to include the transcript. Off by default: the scorecard, notes and ids are usually what an ATS needs |

Each delivery is JSON: `{"event", "delivery_id", "sent_at", "data"}`.
- **`report.completed`:** the report (scores, evidence quotes, strengths, areas for improvement, notes, end reason, `evaluated`). It's sent again after a re-evaluation. If the candidate didn't consent, `data` holds only the ids, role, candidate name, `consent_given: false` and the end reason.
- **`decision.recorded`:** the recruiter's advance / hold / reject decision and note.

The headers are `X-MIA-Event`, `X-MIA-Delivery` (the same id on retries, so you can de-duplicate), `X-MIA-Timestamp` and `X-MIA-Signature`. Check the signature before trusting a delivery, and reject old timestamps:

```python
import hashlib, hmac, time

def verify(secret: str, headers: dict, body: bytes, max_age: int = 300) -> bool:
    ts = headers["X-MIA-Timestamp"]
    expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers["X-MIA-Signature"]) and abs(time.time() - int(ts)) <= max_age
```

Answer with any 2xx. Server errors, timeouts and 429 are retried twice; other 4xx responses aren't. Delivery never delays or blocks saving the report. Each outcome is in the audit log as `report_exported` or `report_export_failed`, without content. `/api/health` shows whether the export is enabled, or why not.

## Data, backups and logs

- **Back up together:** the database (or `pg_dump`), and separately `DATA_DIR/settings.key` (or `SETTINGS_ENCRYPTION_KEY`), `DATA_DIR/database.json` (a hosted database chosen in Settings, encrypted with that key) and `DATA_DIR/setup.json`. To restore, put them back in `DATA_DIR` and start the app.
- **Database.** By default all data is one SQLite file: `DATA_DIR/interviewer.db` (`/data/interviewer.db` in Docker, in the `mia-data` volume). Back it up with the app stopped, or with `sqlite3 interviewer.db ".backup backup.db"` while it runs. Copying the file by hand while the app has it open misses recent writes: they are in `interviewer.db-wal` until SQLite checkpoints them.
- **PostgreSQL** instead: set `DATABASE_URL=postgresql://user:password@host:5432/dbname` (outside Docker, `pip install -r requirements-postgres.txt`; the image already has the driver). The tables are created on first start, and later versions upgrade them on start (`schema_version` table). Docker Compose has an optional `postgres` service: set `POSTGRES_PASSWORD` and `DATABASE_URL=postgresql://mia:<password>@postgres:5432/mia`, then `docker compose --profile postgres up -d --build`. Back it up with `pg_dump`.
- **Moving from SQLite to PostgreSQL:** stop the app, run `python scripts/copy_db.py --from data/interviewer.db --to postgresql://…` (the target must be empty; the SQLite file isn't changed), set `DATABASE_URL`, start the app. Settings → Checks → Database confirms the connection.
- **Still one app instance per install,** with either database: live interviews, the scheduler and the live page keep state in the running process, and MeetStream's webhooks must reach that process.
- **A hosted PostgreSQL from Settings** (Settings → Data and privacy → Database, admins): the connection is tested (reachable, can create tables, already holds Recruence data or not), then either this install's data is copied into the empty database or, if it already holds Recruence data, the admin confirms using that data instead. It is saved encrypted in `DATA_DIR/database.json` and used **from the next start**; the app never switches databases while it runs. `DATABASE_URL` in `.env` always wins and locks this setting. Remove the saved connection to go back to the local database at the next start (nothing is deleted from either). Any PostgreSQL works (Supabase, Neon, RDS, Cloud SQL, your own server); `pip install -r requirements-postgres.txt` outside Docker.
- **Upgrading to this version** needs no manual migration: the new tables (`app_state`, `app_settings`) are created on start, and the new interview fields (link expiry, invited, cancelled) are optional fields in the stored JSON.
- `DATA_RETENTION_DAYS` purges finished interviews; `MEETSTREAM_RETENTION_HOURS` sets how long MeetStream keeps recordings. Keep both as short as your process allows.
- **The server log contains personal data:** the candidate's name as the transcript speaker, and session and bot ids. It doesn't contain what anyone said. Rotate and expire logs like other candidate data.
- Use paid LLM keys for real candidates. Gemini's free tier may use submitted content to improve Google's products.

## Updating

```bash
git pull
docker compose up -d --build
```

Avoid restarting while an interview is live or due to start; that session will be marked failed. The database schema is created on start-up; there are no migrations to run by hand.

## Checklist

- [ ] `APP_ACCESS_TOKEN` set (generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
- [ ] `PUBLIC_BASE_URL` is a stable HTTPS address, and only `/webhooks/*` (plus the booking paths, if you use self-scheduling) is forwarded to the app
- [ ] The app listens on `127.0.0.1` (or a private address), never directly on the internet
- [ ] One worker; the server stays up during interviews and scheduled starts
- [ ] Provider keys saved in MeetStream Integrations, with credit
- [ ] Backups of `interviewer.db`; retention and log expiry set
- [ ] Legal review of AI in hiring for your jurisdictions (the EU AI Act, New York City's AEDT rules and similar)
