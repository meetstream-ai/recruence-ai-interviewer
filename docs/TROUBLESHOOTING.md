# Troubleshooting

## Setup and access

| Symptom | Cause and fix |
|---|---|
| The dashboard asks for a setup code | You're not on the server's own machine (Docker counts as another machine). The code is in the log: `docker compose logs app \| grep "setup code"`, or the terminal running the app. It changes on every start until setup is complete |
| "Set APP_ACCESS_TOKEN to allow access from other machines" | Setup chose "this computer only", or no token is set. Create one in Settings → Access (on the server itself), or set `APP_ACCESS_TOKEN` and restart |
| Lost the access token | On the server itself, Settings → Access → Regenerate. Or set a new `APP_ACCESS_TOKEN` in `.env` and restart |
| The setup wizard doesn't appear | The install already has roles or interviews, or an access token and an AI provider in the environment; it counts as set up. Everything is in Settings |
| A saved setting has no effect | It's marked "After restart" (restart the app), or the same value is set in the environment and only `DATABASE_URL` takes precedence over Settings. Settings shows where each value comes from |

## Database

| Symptom | Cause and fix |
|---|---|
| "Couldn't connect: connection timeout expired" | The host or port isn't reachable from the server: check firewalls, the provider's IP allow-list, and that the port is 5432 (or the pooler's) |
| "this user can't create tables" | Give the user `CREATE` on the database or schema, or use the owner account |
| "Existing Recruence installation found" | That database belongs to another install (or this one, earlier). Confirm using it as it is, or choose another database |
| "Empty database: ready for a new Recruence setup" | Not an error: continue, and Recruence creates its tables there |
| "tables named like Recruence's … that aren't from a Recruence installation" | Another app uses that database. Recruence won't touch it: use an empty database for Recruence |
| The new database isn't used | A database chosen in Settings or setup applies at the next start: restart the app. `DATABASE_URL` in the environment always wins |
| "A saved secret can't be decrypted" | `settings.key` (or `SETTINGS_ENCRYPTION_KEY`) changed. Restore the original key, or re-enter the saved keys and passwords |

## AI provider

| Symptom | Cause and fix |
|---|---|
| "The provider rejected the key" | Wrong or revoked key, or the key's project lacks access to the API. Create a new key |
| "Connected, but the model isn't available" | The model name is wrong or not enabled for your key: pick one from the list the test returns |
| Plans are templates, evaluations missing | No AI provider in use (Settings → AI provider), or the provider failed: the report says why; re-run the evaluation from it |
| Gemini: 429 quota, or the interviewer goes silent mid-call | Free-tier daily limits (about 20 requests a day per model, roughly one interview). Enable billing, or switch the conversation model |

## MeetStream and live interviews

| Symptom | Cause and fix |
|---|---|
| "doesn't resolve" / "couldn't reach itself" for the public URL | The tunnel is stopped or `PUBLIC_BASE_URL` is out of date (quick tunnels change address on every start). Start the tunnel and update the URL. MeetStream answers 500 when it can't resolve the app's address |
| "answers, but not from this app" | The address points to another server or an old tunnel |
| The bot never joins | Google Meet needs the meeting to have started; admit "AI Interviewer" from the lobby; Zoom needs a Meeting SDK app in MeetStream |
| The AI is silent after its greeting | Its conversation model isn't answering (quota, overload or a closed model). Check the voice provider keys in your MeetStream dashboard and the model in Settings → AI interviewer |
| Echo in the transcript | The candidate isn't using headphones |

## Email

| Symptom | Cause and fix |
|---|---|
| "Email isn't set up" | Set the SMTP server and From address (Settings → Email), then send a test email |
| Authentication errors | Many providers need an app password or SMTP-specific credentials, and port 587 with STARTTLS or 465 with SSL |
| Invitations show "not emailed" | Email wasn't set up when they were sent: copy the link from the candidate's interview page, or send a new link after setting up email |
