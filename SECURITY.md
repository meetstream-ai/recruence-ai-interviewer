# Security

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this repository. Don't open a public issue. Include what you found, how to reproduce it, and the version or commit. We aim to reply within a week.

## Trust model

Recruence handles candidates' personal data (resumes, applications, interview recordings and transcripts). One install serves one organisation; whoever controls the server controls the data.

| Surface | Who reaches it | Protection |
|---|---|---|
| Dashboard and `/api/*` | Recruiters | `APP_ACCESS_TOKEN` (cookie holds a hash, or `Authorization: Bearer`), or a trusted SSO proxy with viewer / recruiter / admin roles. Without either, only the server's own machine is answered. Requests arriving through a proxy or tunnel are refused unless they come from the trusted proxy |
| First-run setup | The installer | Until setup is complete: the server's own machine, a signed-in admin, or a browser that entered the one-time code printed in the server log. Never through the public URL |
| `/webhooks/*` | MeetStream | Random per-session path token; the bot id must match; MIA tool calls also need a per-session bearer token |
| `/api/public/apply/<job key>` | Your careers website (browsers only from allowed origins) | Unguessable job key, rate limiting on applications, only published open roles |
| `/interview/<token>`, `/book/<token>` and their `/api/public/*` | Candidates | Unguessable tokens (`secrets.token_urlsafe(24)`), expiry, one interview start per link, rate limiting on applications. Candidates see only their own first name, the role and the notice |
| Everything else | Nobody publicly | Keep the dashboard off the public internet unless it sits behind your authenticated proxy (see `docs/DEPLOYMENT.md`) |

## Data at rest

- API keys and passwords saved from Settings, and a hosted database URL, are encrypted with Fernet. The key is `SETTINGS_ENCRYPTION_KEY` or `DATA_DIR/settings.key`; back it up apart from the database.
- `.env` holds keys in plain text: keep it readable only by the account that runs the app.
- The server log contains candidates' names (as transcript speakers) but not what they said.
- Retention: finished interviews and unused applications are deleted after `DATA_RETENTION_DAYS`; MeetStream recordings after `MEETSTREAM_RETENTION_HOURS`. Deleting a candidate deletes their application and asks MeetStream to erase recordings.

## Hardening checklist

- [ ] Set `APP_ACCESS_TOKEN` (the setup wizard creates one) or an SSO proxy with `PROXY_AUTH_SECRET`.
- [ ] Expose only the webhook and candidate paths publicly (`docs/DEPLOYMENT.md`); the app listens on `127.0.0.1`.
- [ ] Use HTTPS for `PUBLIC_BASE_URL`.
- [ ] Back up the database and `settings.key` separately; restrict access to `DATA_DIR`.
- [ ] Use paid AI keys for real candidates (free tiers may use content for training).
- [ ] Set retention periods your policy allows; rotate the access token when people leave.
- [ ] Rotate any key that has been pasted into chat, tickets or logs.
