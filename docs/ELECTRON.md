# Recruence desktop app (Windows, macOS, Linux)

The desktop app is the same Recruence you'd run with Docker or `uvicorn`: the same backend, dashboard, onboarding, accounts, workspaces and recruiting. Electron adds a window and runs the backend for you. Nothing is rebuilt or duplicated.

## How it works

```
Recruence (Electron main process, desktop/main.js)
 ├─ starts  resources/backend/mia-backend/mia-backend[.exe]   (app/desktop.py, frozen with PyInstaller: Python and
 │          every dependency included, nothing to install)
 │          listens on 127.0.0.1:8765 only (another free port if 8765 is taken)
 ├─ waits for /api/health, showing a loading screen; shows an error page with the log if it fails
 └─ window: the dashboard from the backend (context isolation, sandbox, no Node in pages, navigation limited
            to the backend, external links in your browser, microphone/camera and clipboard only)
```

- **One instance only.** Opening Recruence again focuses the running window, so a second backend never starts.
- **Shutting down.** On quit, the backend is asked to stop cleanly: a live interview's bot leaves its meeting first. It's forced to stop after 25 seconds if needed. If the window process dies, the backend notices within seconds and stops itself, so nothing is left running.
- **The preload bridge** gives pages five actions and nothing else: open the logs folder, open the data folder, try again, quit, and show version and folders. Each is checked again in the main process. Pages get no file system or OS access, and database credentials never reach a page.

## Where your data lives

| What | Where |
| --- | --- |
| Local database (SQLite), settings key, saved database connections (encrypted), setup progress | `%APPDATA%\Recruence\data` |
| Optional settings file (`.env` format, same names as `.env.example`) | `%APPDATA%\Recruence\data\.env` |
| Backend log (rotated at 5 MB) | `%APPDATA%\Recruence\logs\backend.log` |
| Window preferences (`{"port": 8765}` to choose the port) | `%APPDATA%\Recruence\desktop.json` |
| The app itself | `%LOCALAPPDATA%\Programs\Recruence` (or the folder chosen at install) |

- **Installed before the rename (as MIA Interviewer)?** Your data stays in `%APPDATA%\MIA Interviewer` and the app keeps using it there; nothing is moved or copied. An update installs into the folder the earlier version used. Environment variables keep their `MIA_` names.

- **Resumes** are stored as extracted text in the connected database. Uploaded files aren't kept.
- **Transcripts, notes and reports** are also in the database.
- **Interview recordings** stay at MeetStream (kept for `MEETSTREAM_RETENTION_HOURS`); the app doesn't download them.
- **Updates and uninstalling** keep everything above. The uninstaller asks whether to delete it, and the default is No. A PostgreSQL server's data is never touched by either.
- **Secrets:** saved keys and connection strings are encrypted with `settings.key` in the data folder. Keep that folder private, and back up `settings.key` together with the database.

## Install

1. Run `Recruence-Setup-<version>.exe`. It installs for your Windows user only, so no administrator rights are needed. You can choose the folder.
2. The installer adds a desktop shortcut and a Start menu entry, and starts Recruence when it finishes.
3. The installer isn't code-signed, so Windows SmartScreen may say "Windows protected your PC". Choose **More info → Run anyway**.

To update, run a newer installer over the installed app; your data stays. To uninstall, use **Settings → Apps → Recruence → Uninstall**.

## First run: database first

The first window is Recruence's normal setup:

1. **Welcome.**
2. **Database:** keep the local SQLite file, or paste a PostgreSQL connection string (Neon, Supabase, Railway, RDS, your own) and test it.
   - The test only checks: it connects, checks Recruence may create tables (in a transaction that is rolled back), and says what it found. Nothing is written.
   - **Empty database: ready for a new Recruence setup.** This is the normal case for a new Neon or Supabase project. Recruence creates its tables when you continue.
   - **No Recruence tables, but other tables:** also ready. Recruence adds its own tables and leaves the others untouched.
   - **Existing Recruence installation found:** used only if you confirm, then as it is. Missing tables or columns are added; nothing is dropped, cleared or overwritten.
   - Tables with Recruence's names that aren't from Recruence (e.g. another app's `users`) are refused, so they're never touched.
   - **I already have an account** with an empty database (or an installation with no accounts) offers **Set up Recruence in this database** instead of an error.
3. **Account:** your name, email (Gmail works) and password. The first account administers this installation and owns the first workspace.
4. **AI provider, MeetStream, Email, Review.**

If you close the app midway, setup resumes where you left off.

**Already have an account?** If your account is in a database you've used before (on another computer, or before reinstalling), choose **I already have an account** on the Welcome page:
1. Paste that database's connection string and click Connect.
2. Sign in with your email and password.

That's all: the database brings its own settings (AI provider, MeetStream, email) and workspaces with it. Nothing in it is changed.

- **Neon:** copy the connection string from the Neon console. Use the direct (non-pooled) one, and keep `?sslmode=require` at the end.
- **Supabase:** Project → Connect → copy the **Session pooler** string (host `aws-0-<region>.pooler.supabase.com`, port 5432, user `postgres.<project-ref>`) and fill in the database password.
  - The "Direct connection" host (`db.<project-ref>.supabase.co`) works over IPv6 only. On the many networks without IPv6 it fails with "failed to resolve host", and Recruence then says to use the Session pooler.
  - The transaction pooler (port 6543) isn't recommended.

### Amazon RDS for PostgreSQL and Aurora PostgreSQL

Both are standard PostgreSQL to Recruence: the same driver, migrations, connection test and pooling, with nothing AWS-specific to install.

```
postgresql://<user>:<password>@<instance>.<id>.<region>.rds.amazonaws.com:5432/<db>?sslmode=require
```

- **Aurora:** use the cluster (writer) endpoint, `<cluster>.cluster-<id>.<region>.rds.amazonaws.com`, not a reader endpoint. Recruence writes.
- **SSL:**
  - RDS for PostgreSQL 15 and newer refuses unencrypted connections by default, so keep `sslmode=require`.
  - To also check the server's certificate, download AWS's CA bundle (`global-bundle.pem` from the RDS documentation) and use `?sslmode=verify-full&sslrootcert=C:/path/to/global-bundle.pem`. The path is on the computer running Recruence.
- **Network:** the database's security group must let Recruence's computer or server reach port 5432. For a desktop app that usually means a publicly accessible instance restricted to your IP, or a VPN into the VPC.
- **RDS Proxy:** works. Recruence turns off server-side prepared statements, so sessions aren't pinned.
- **Not supported:** IAM database authentication (tokens that expire every 15 minutes). Use a database user with a password; keep it in Secrets Manager if you like, and paste it here.
- **The database user** needs to create tables in its schema the first time (Recruence checks this when you test the connection).
- **Connection:** RDS and Aurora use the same PostgreSQL connection and SSL handling as Neon and Supabase; use the instance's endpoint with `sslmode=require`.

## Switching databases and workspaces

- **Workspaces:** use the picker in the header. You stay signed in, and the database doesn't change.
- **Databases:** account menu → **Switch database…** (installation admins).
  1. You're signed out, and so is everyone else using this database.
  2. Pick a saved connection or enter a new one.
  3. Sign in with an account from that database, or create its first account if it has none.

  Nothing is copied, merged or deleted in either database, and every session in the old database ends. You can't switch while an interview is running.

## Developing

Requirements: Windows 10 or 11, Python 3.12, Node.js 20 or newer.

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-postgres.txt -r requirements-desktop.txt
npm install
npm run dev:electron
```

`npm run dev:electron` uses the repository's `.venv` and reads the project's `.env`. Set `MIA_USERDATA=<folder>` to use another data folder than `%APPDATA%\Recruence`.

| Command | What it does |
| --- | --- |
| `npm run dev:electron` | The desktop app with the backend from `.venv` (Python, not frozen) |
| `npm run build:backend` | Freeze the backend: `desktop/build/backend/mia-backend` (PyInstaller, `desktop/mia-backend.spec`) |
| `npm run build:electron` | Freeze the backend, then build the unpacked app: `desktop/dist/win-unpacked` |
| `npm run package:win` | Freeze the backend, then build the installer: `desktop/dist/Recruence-Setup-<version>.exe` |
| `npm run package:mac` | On a Mac: `desktop/dist/Recruence-<version>-macOS-<arch>.dmg` for that Mac's chip (add `-- --arm64` or `-- --x64`) |
| `npm run package:linux` | On Linux: `desktop/dist/Recruence-<version>-Linux-x86_64.AppImage` |
| `npm run test:smoke` | Launch check (`-- --unpacked` or `-- --installed` for the built app). It starts the app, waits for the dashboard, reads the page (title, no Node access, the bridge), quits, and checks no backend is left running |
| `npm run test:db -- neon` | Database check (see below) |

The dashboard is part of the backend, so any change under `app/` needs `npm run build:backend` before packaging. **Build each system's app on that system:** the frozen backend is a native program for the machine that built it, so a Windows machine can't build the macOS or Linux app (and an Apple silicon Mac's build won't run on an Intel Mac). The release workflow (`.github/workflows/release.yml`) builds all four on GitHub's runners: push a `v*` tag, or run it from the Actions tab. On Linux use `.venv/bin/python` wherever these docs say `.venv\Scripts\python`. Download caches can be kept on another drive with `npm_config_cache`, `ELECTRON_CACHE` and `ELECTRON_BUILDER_CACHE`.

## Testing a database (development)

Put the test connection strings in `.env.test.local` at the repository root. It's git-ignored, never bundled, and never printed:

```
MIA_TEST_NEON_DATABASE_URL=postgresql://<user>:<password>@<host>/<db>?sslmode=require
MIA_TEST_SUPABASE_DATABASE_URL=postgresql://postgres.<ref>:<password>@<host>:5432/postgres
```

```bash
npm run test:db -- neon                 # read-only: connects, reports version, SSL, encoding, extensions, schema
npm run test:db -- neon --init          # also creates or upgrades Recruence's tables (adds only), then a CRUD test
npm run test:db -- supabase --init
npm run test:db -- sqlite --init
npm run test:db -- MY_OTHER_URL_VARIABLE
```

- **The CRUD test** writes a temporary role and a key-value entry in a private test workspace, which no real workspace can see, then deletes exactly those.
- **Never done:** dropping tables, truncating, renaming or rewriting anything. Other applications' tables are listed and left alone, and Recruence uses only the connection's current schema.
- **Without `--init`,** nothing is changed.

In the app, **Settings → Database → Diagnostics** (installation admins only) shows the type, connection, server version, schema version, missing tables, the last successful check, and errors with the credentials removed.

## What works on the desktop, and what needs a public address

The backend listens on this computer only. MeetStream and candidates are on the internet, so:

| Works locally | Needs a public HTTPS address to this computer (a tunnel) or a server |
| --- | --- |
| Setup, accounts, workspaces, joining codes, database switching | MeetStream webhooks: the AI interviewer joining, live transcript, end of call |
| Roles, questions (AI plan generation), screening (AI), reports, decisions | Applications from your careers website (it calls `/api/public/apply/…`) |
| Sending email (SMTP is outgoing) | Candidate interview links (`/interview/…`) and self-scheduling (`/book/…`) |
| Reading reports of interviews already run | Starting an interview in a meeting |

To run real interviews from the desktop, point a tunnel at port 8765 and set its https address as the **Public application URL** (Settings → MeetStream). The steps are in [INTERVIEW_SETUP.md](INTERVIEW_SETUP.md#public-address). Requests that come through the tunnel can only reach the webhooks and candidate pages; the dashboard refuses them. Alternatively, run Recruence on a server (Docker, [DEPLOYMENT.md](DEPLOYMENT.md)) and use it from a browser.

## Troubleshooting

| What you see | Do this |
| --- | --- |
| "The Recruence backend couldn't start" | Use **Open the log folder** on the error page; `backend.log` says why |
| "The saved database … couldn't be reached when Recruence started" | Your saved database (e.g. Neon after a pause, or no internet) was down at start. Recruence opened on the local database instead; nothing was changed. When it's back, use **Reconnect** on the sign-in page (or **Use another database**) and connect the saved connection |
| Someone forgot their password and email isn't set up | An installation admin: Settings → Access → Accounts → **Temporary password**. It's shown once; they change it in Your account |
| "The Recruence backend didn't answer within 90 seconds" | Antivirus scanning the new backend is a common cause on first launch. Try again. If it persists, check the log |
| "port 8765 is in use" in the log | Another program has the port. Recruence uses another for that session, so a tunnel pointed at 8765 won't reach it. Free the port, or set `{"port": 8766}` in `desktop.json` and point the tunnel there |
| SmartScreen warning when installing | The installer isn't signed: More info → Run anyway |
| "Couldn't connect … SSL" with Neon or Supabase | Keep `?sslmode=require` in the connection string |
| "Existing Recruence installation found" | It's a Recruence database already. Confirm to use it as it is (sign in with its accounts), or use another one |
| "Empty database: ready for a new Recruence setup" | Expected for a new database: continue, and Recruence creates its tables |
| Locked out after switching | On the sign-in screen, use **Use another database**. In `/#/connect`, **Print a code in the server log**: the code is in `backend.log` |

## Security notes

- The dashboard is served on 127.0.0.1 only. Through a tunnel it refuses everything except the webhooks and candidate pages.
- Once an account exists, signing in is required even on this computer.
- Connection strings and API keys are encrypted at rest (`settings.key`). They're never sent to the window, and they aren't in the installer, the logs or the reports.
- The installer contains no keys or connection strings. Settings saved in the database stay with the database, so connecting another database means that database's settings apply.
