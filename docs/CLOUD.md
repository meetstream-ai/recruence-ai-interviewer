# Recruence in the cloud, with the desktop app

Recruence stays a Windows desktop app for recruiters, but its backend can run on a server. Candidates then apply, get their interview links, take their interviews and get evaluated while the recruiter's computer is switched off.

## How it fits together

```
 Recruiter's PC                     Cloud host (always on)                         Outside
┌──────────────────────┐   HTTPS   ┌──────────────────────────────────────┐
│ Recruence desktop    │ ────────► │ HTTPS proxy (Caddy / Cloudflare /    │ ◄── candidates (links, booking, interview)
│ (Electron window,    │           │ your load balancer)                  │ ◄── MeetStream (bot events, MIA tool calls)
│  cloud mode)         │           │   └─► Recruence backend (Docker)     │ ◄── careers sites (job list, applications)
└──────────────────────┘           │        REMOTE_DASHBOARD=true         │
                                   └──────────────┬───────────────────────┘
                                                  ▼
                                        Neon PostgreSQL (all data)
```

| Part | What it does | Where it runs |
|---|---|---|
| Desktop app (`desktop/main.js`) | The recruiter's window: the same dashboard, job, candidate, interview and settings pages as before | Recruiter's PC. **Cloud mode:** shows the cloud backend's dashboard and starts nothing locally. **Local mode:** starts the backend on the PC, as before |
| Backend (`app/server.py`) | Dashboard pages and API, careers API, application intake and screening, interview links, the interview scheduler and live interviews, MeetStream webhooks and MIA tools, evaluation, email, retention | Cloud host, one copy |
| Database | Roles, candidates, interviews, transcripts, reports, accounts, saved settings | Neon (`DATABASE_URL`) |

Nothing a candidate or MeetStream needs depends on the desktop app. The desktop app is only a window onto the backend: closing it, or turning the PC off, changes nothing on the server.

### What changed

- **Remote dashboard (`REMOTE_DASHBOARD=true`).** Normally the backend refuses its dashboard to any request that came through a proxy or tunnel, so a public address exposes only the webhooks and candidate pages. With this on, recruiters' **accounts** (email and password, HttpOnly session cookie) sign in through the HTTPS proxy in front of the backend. The setup wizard and the open "this computer" mode are still never reachable through it. Cookies are `Secure` when the proxy says the visitor used HTTPS, and sign-in rate limits count the visitor's own address, from `X-Forwarded-For` or `CF-Connecting-IP`, believed only from `TRUSTED_PROXIES`.
- **The backend refuses to start without what a cloud host needs:** `DATABASE_URL` must be PostgreSQL, `SETTINGS_ENCRYPTION_KEY` must be set, and `PUBLIC_BASE_URL` must be `https://`. It never falls back to a local SQLite file.
- **`INSTALLATION_ID`** keeps the installation's id fixed on a host without a persistent disk.
- **Desktop cloud mode.** If a server address is set, the desktop app loads `https://<server>/`. The address comes from `RECRUENCE_SERVER_URL`, or `server` in `%APPDATA%\Recruence\desktop.json`, chosen in **Settings → System → Desktop app connection** or on the startup error page. The window keeps its protections: navigation and permissions are limited to that origin, and the address must be https (http only for `localhost` in development).
- **Deployment files:** `deploy/cloud/docker-compose.yml` (the existing image, plus optional Caddy or Cloudflare Tunnel) and `deploy/cloud/.env.example`, plus `desktop/.env.example`.

The data model, migrations and every existing API are unchanged. Candidates' interview links and booking links keep working because they're stored in the database. A running local backend works exactly as before.

### Limits

- **Run one copy of the backend.** Live interviews, the scheduler and background jobs run in the backend process. Two copies on one database would both launch bots, so don't scale it out.
- **Don't run a local backend against the same database while the cloud one runs.** In cloud mode the desktop app never starts one. Switching a PC back to "this computer" while the server runs on the same Neon database would start a second scheduler.
- **The first account of a brand-new database** must be created on the server itself (SSH port forward to `127.0.0.1:8000`) or from a desktop app in local mode. The remote dashboard never allows that. An existing database (yours) already has its accounts.

## Environment variables

### Cloud backend (`deploy/cloud/.env`)

| Variable | Required | What it is |
|---|---|---|
| `DATABASE_URL` | yes | Neon connection string (`postgresql://…`, pooled host, `sslmode=require`) |
| `SETTINGS_ENCRYPTION_KEY` | yes | Decrypts the API keys saved from Settings. **The same key the desktop app used:** the contents of `%APPDATA%\Recruence\data\settings.key` |
| `PUBLIC_BASE_URL` | yes | The backend's `https://` address. Candidate links, MeetStream webhooks and the careers address use it |
| `REMOTE_DASHBOARD` | set by the compose file | `true` |
| `INSTALLATION_ID` | recommended | 32 hex characters, fixed |
| `TRUSTED_PROXIES` | if your proxy isn't on a private network | Addresses or CIDR ranges of the proxy |
| `DOMAIN` / `TUNNEL_TOKEN` | for the Caddy / tunnel profile | The domain, or the named tunnel's token |
| `PUBLIC_CORS_ORIGINS` | only for browser-posted forms | Careers sites whose visitors' browsers post to a role's application API |
| Everything else | optional | MeetStream key, AI keys, SMTP and so on (main `.env.example`), or set from Settings once signed in |

### Desktop app (`desktop/.env.example`)

| Variable | What it is |
|---|---|
| `RECRUENCE_SERVER_URL` | The cloud backend's `https://` address. Set: cloud mode, and the address can't be changed in the app. Empty: whatever `desktop.json` says, else local mode |
| `MIA_USERDATA` | Another data folder (tests, a second profile) |

## Local development

Nothing changes for working on Recruence locally:

```bash
.venv/Scripts/python -m uvicorn app.server:create_app --factory --port 8000
```

```bash
npm run dev:electron
```

The desktop app starts its own backend unless a server is set. To try the desktop app against a backend running separately on this computer, start the backend first, then:

```bash
RECRUENCE_SERVER_URL=http://127.0.0.1:8000 npm run dev:electron
```

## Deploying the cloud backend

Any Linux host with Docker works (a small VPS is enough). On it:

1. Copy the repository (or just `Dockerfile`, `app/`, `scripts/`, `requirements*.txt` and `deploy/cloud/`).
2. Create `deploy/cloud/.env` from `deploy/cloud/.env.example` and fill in `DATABASE_URL`, `SETTINGS_ENCRYPTION_KEY` (copied from the PC's `settings.key`), `PUBLIC_BASE_URL` and `INSTALLATION_ID`.
3. Choose how HTTPS reaches it:
   - **Your domain with Caddy:** point the domain's DNS record at the host, open ports 80 and 443, set `DOMAIN`, then:
     ```bash
     docker compose -f deploy/cloud/docker-compose.yml --profile caddy up -d --build
     ```
   - **A named Cloudflare Tunnel** (no open ports): in Cloudflare Zero Trust create a tunnel with a public hostname whose service is `http://app:8000`, set `TUNNEL_TOKEN`, then:
     ```bash
     docker compose -f deploy/cloud/docker-compose.yml --profile tunnel up -d --build
     ```
   - **Your own proxy or load balancer:** start only the app (`docker compose -f deploy/cloud/docker-compose.yml up -d --build`) and send `https://your-domain` to port 8000. Set `TRUSTED_PROXIES` if the proxy isn't on this host or a private network. The proxy must send `X-Forwarded-Proto`. If it forwards plain http instead of redirecting it, Recruence redirects page loads to `PUBLIC_BASE_URL` and refuses anything that sends data (sign-in, applications) with 403, so nothing sensitive is accepted over http.
4. Check it: `https://your-domain/api/health` answers `{"ok": true, …}`, and `https://your-domain/api/health/ready` answers `{"ok": true, "database": {"ok": true, …}}`. Point uptime monitors at `/api/health/ready`: it answers 503 while the database is unreachable (the container's health check uses it too), whereas `/api/health` only says the process is running. The log (`docker compose -f deploy/cloud/docker-compose.yml logs app`) says "Remote dashboard: accounts sign in through the HTTPS proxy".
5. In the dashboard (sign in from a browser or the desktop app), **Settings → MeetStream → Public URL** must be the same `https://` address. MeetStream's callbacks go there.

### Bring your own cloud: by provider

A new desktop install asks how to run Recruence: **Bring your own cloud** (recommended) or **Run on this computer** (`desktop/welcome.html`). The cloud option needs the user's own server and database:

| Provider | Server | Database |
|---|---|---|
| AWS | Lightsail or EC2 with Docker and `deploy/cloud`, or App Runner with one instance | Neon, or Amazon RDS for PostgreSQL |
| Google Cloud | Compute Engine with Docker, or Cloud Run with min and max instances 1 and CPU always allocated | Neon, or Cloud SQL for PostgreSQL |
| Azure | A Linux VM with Docker, or Container Apps with 1 replica (no scale to zero) | Neon, or Azure Database for PostgreSQL |
| DigitalOcean | A Droplet with Docker, or App Platform with 1 container | Neon, or DigitalOcean Managed PostgreSQL |
| Hetzner | A Cloud server with Docker and the `caddy` profile | Neon, or PostgreSQL on the same server |
| Railway | Deploy from the repository's `Dockerfile`, 1 replica; see [Railway](#railway) | Railway's PostgreSQL, or Neon |

On any of them: one copy, always on, `REMOTE_DASHBOARD=true`, the four required variables, and HTTPS on a permanent address. Then paste that address into the app's first screen, or into Settings → System later.

A container platform (Render, Railway, Fly.io, Cloud Run with minimum instances 1, …) works too. Use the repository's `Dockerfile`, set the variables above plus `REMOTE_DASHBOARD=true`, run exactly one instance that never scales to zero (an interview's callbacks must find it awake), and set `TRUSTED_PROXIES` to the platform's proxy range if it isn't private.

### Railway

Railway works well for the cloud backend:

- **Service:** deploy from the repository's `Dockerfile` (GitHub, or `railway up` from a clean copy). One replica.
- **Address:** Settings → Networking → **Generate Domain**, target port **8000**. Railway redirects http to https itself.
- **Database:** add Railway's PostgreSQL and set `DATABASE_URL=${{Postgres.DATABASE_URL}}`, or use Neon's pooled string.
- **Variables:** `REMOTE_DASHBOARD=true`, `SETTINGS_ENCRYPTION_KEY`, `INSTALLATION_ID`, `PUBLIC_BASE_URL` (the generated `https://…up.railway.app` address or your own domain), and **`TRUSTED_PROXIES=100.64.0.0/10,127.0.0.0/8,::1/128`**. Railway's proxy connects from `100.64.0.0/10`. Without that setting, Recruence doesn't trust its `X-Forwarded-Proto`: no HSTS, the session cookie isn't `Secure`, and visitors' addresses aren't seen.
- **No volume.** With PostgreSQL, everything lives in the database, and a restart keeps data and sign-ins. A Railway volume is mounted owned by root, which the image's unprivileged user can't write to. Don't run the container as root to get around this.
- **The first account:** create it on the server itself, e.g. `railway ssh` and a request to `http://127.0.0.1:8000/api/auth/register`. Through the public address, registration is closed.

### Neon

- Run the backend **in the same region as the Neon project** (shown in Neon's console). From another continent, each request waits on several cross-region round trips: about 1 s per page in the 13 November test.
- Use the existing project: `DATABASE_URL` is its **pooled** connection string. Nothing to migrate; the backend checks and updates the schema itself at start, the same as the desktop app.
- Stop running interviews from a PC in local mode before switching to the cloud backend, so only one copy uses the database.
- Rotate the Neon password if it was ever shared, then update `DATABASE_URL` and restart.
- `SETTINGS_ENCRYPTION_KEY` must be the key the settings were saved with. If it isn't, the saved keys are skipped, and the log names them at start ("Saved settings couldn't be read and were skipped: …").

### Moving from the desktop's local backend

1. On the PC, quit Recruence and copy the contents of `%APPDATA%\Recruence\data\settings.key` into the server's `SETTINGS_ENCRYPTION_KEY`. Keep it secret; it's what decrypts the saved API keys.
2. Deploy the backend as above, pointing `DATABASE_URL` at the same Neon database.
3. In the desktop app: **Settings → System → Desktop app connection**, enter `https://your-domain` and click **Connect to a server**. The app reconnects and you sign in with your usual account.
4. Update the careers sites' forward addresses that pointed at the PC's tunnel (below). The **Public URL** needs no change: on a cloud backend it's always `PUBLIC_BASE_URL`, shown in Settings as "Set on the server". A Public URL the desktop saved in the database is ignored, and the log says so at start.

## Careers sites

The careers address (Settings → Recruitment sources) is shown with the backend's Public URL once it's set: `https://your-domain/api/public/careers/<key>`. The key doesn't change.

- **MeetStream Careers** (`D:\MeetStream Careers Website`): set `APPLICATION_FORWARD_URL` to that address. It reads the published jobs from it and posts applications with each job's `ref`.
- **A site with its own job pages** (like the Larkspur Labs test site): set its forward address (`RECRUENCE_CAREERS_URL`) to the same address. It posts each application with the job's title (`role`), and Recruence files it under the published role with that title.

The address carries a secret key: keep it on the careers site's server. No CORS setting is needed for a server that forwards applications.

## Building the desktop app

Unchanged, and one installer serves both modes:

```bash
npm run package:win
```

The installer is written to `desktop/dist/Recruence-Setup-<version>.exe`. To ship a build that always uses your server, set `RECRUENCE_SERVER_URL` for the users (for example with a machine-wide environment variable), or let each recruiter enter the address once in Settings → System.

## Security checklist

- HTTPS end to end at the proxy; the backend's port 8000 is bound to `127.0.0.1` (compose) or reachable only from the proxy.
- `SETTINGS_ENCRYPTION_KEY`, `DATABASE_URL`, `TUNNEL_TOKEN` and the provider keys live only in the server's `.env` or the platform's secret store, never in the repository or the desktop app.
- Recruiters sign in with their accounts. New accounts on an existing database need a joining code from a workspace.
- Candidate pages and webhooks stay protected by their unguessable tokens and signatures, as before.
- Back up the encryption key separately from Neon's backups.
