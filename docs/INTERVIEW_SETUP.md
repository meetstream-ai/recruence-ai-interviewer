# Interview automation setup guide

How to get from a fresh Recruence install to a real AI interview in Google Meet, Zoom or Teams, and how each piece works. Everything here is what the code does today. Anything not yet seen in a live call is marked **unverified**.

In the app, **Settings → MeetStream → Interview readiness** shows what's set up, tested or missing.

## What you need

| Service | Why | Required | Cost |
| --- | --- | --- | --- |
| [MeetStream](https://app.meetstream.ai) | Puts the AI interviewer (a bot with a voice agent, "MIA") in the meeting; live transcript; recording | Yes, for interviews in meetings | Billed by MeetStream per bot time; check your plan |
| Voice providers, with keys saved **in MeetStream** | The voice agent's speech-to-text, conversation model and voice | Yes | Each provider bills you. Free tiers run out fast: Gemini's free tier allowed about 20 requests a day for one model in our tests, which ended an interview midway |
| An AI provider, with keys saved **in Recruence** | Interview plans, screening, evaluation reports | Yes, for screening and evaluation (without one: template questions only, nothing screened or evaluated) | Gemini, Claude and OpenAI bill per use; Ollama runs on your own computer |
| A public HTTPS address to Recruence | MeetStream's callbacks; candidates' application and interview links | Yes, for interviews in meetings | A Cloudflare tunnel is free; or your own domain and server |
| SMTP (any provider) | Invitation, booking, cancellation and report-ready emails | No: without it, copy links by hand | Usually free for low volumes |
| A meeting room link | Where interviews happen (a Zoom meeting's `/j/` join link in your Zoom SDK app's account, a Meet link guests can join without the host, a Teams meeting) | Yes | Your existing meeting account |

## 1. MeetStream

**Credentials.**
1. Create an account at [app.meetstream.ai](https://app.meetstream.ai) and create an API key there.
2. In Recruence: **Settings → MeetStream → MeetStream API key** (or `MEETSTREAM_API_KEY` in `.env`). Mandatory.
3. Click **MeetStream key** under Checks. It makes one real call and says whether the key works.

**Voice provider keys.** These are saved in the MeetStream dashboard (Configure → Integrations), not in Recruence. Recruence tells MeetStream which providers to use (section 3). Creating the voice agent fails with "400" until the matching keys are there.

**Meeting rooms.** Recruence doesn't create meetings. You give it a link, and MeetStream's bot joins that link:
- **A role's interview room:** the role's setup → Settings → Interview room. One reusable link that every candidate of the role uses. Only one interview at a time can use a room; Recruence refuses a second start while one is running.
- **A one-off interview:** the meeting link on the interview's launch form, or its self-scheduling page.

On Google Meet, the meeting must let guests join without the host admitting them. Otherwise someone has to admit the bot.

**How the interviewer joins.**
1. When an interview starts (you click Launch, a booked time arrives, or a candidate presses Start on their link), Recruence creates a MIA voice agent configuration with the interview's plan.
2. Recruence asks MeetStream for a bot with that agent and the meeting link.
3. The bot asks to join. On Meet that took 2–3 minutes in our tests; on Zoom about 6 seconds.
4. The callbacks go to `https://<public address>/webhooks/meetstream/<random token>/events` and `/transcript`. The token is new for each session, and every payload's bot id must match.

**How completion is detected.** MeetStream sends the bot's ending event: stopped, kicked, denied or failed. The agent also ends the interview through Recruence's tool endpoint (`/webhooks/mia/<token>/<tool>`) when the questions are done. Recruence then:
- builds the transcript (`POSTCALL_TRANSCRIPT`, on by default, which rebuilds it from the recording);
- evaluates it with your AI provider;
- saves the report and emails the hiring team.

**Recordings.** MeetStream records each call and keeps it for `MEETSTREAM_RETENTION_HOURS`. Video is recorded only for the Video and Proctored formats. Recruence doesn't download recordings; open them in the MeetStream dashboard. The transcript and report are stored in Recruence.

**Webhooks.** There's nothing to register in the MeetStream dashboard: Recruence sends the callback URLs with each bot. You just need the public address (section 6).

**Testing.** Run the **MeetStream key** check, then the **Public address reaches this app** check. Then run one short interview with yourself as the candidate (section 5).

## 2. AI provider (plans, screening, evaluation, and the interview's voice where it can)

Set it in **Settings → AI provider**: choose the provider, add its key and model, **Test connection** (it lists the models), save. The same provider also runs the interview's conversation, speech-to-text and voice wherever MeetStream supports it; the page shows what it covers and offers another provider only for what's missing (README, "One provider for the whole interview").

| Provider | Key from | Settings (`.env` names) | Notes |
| --- | --- | --- | --- |
| Google Gemini | [aistudio.google.com](https://aistudio.google.com) → API keys | `LLM_PROVIDER=gemini`, `GEMINI_API_KEY`, `GEMINI_MODEL` | In our tests, `gemini-3.6-flash` and `gemini-3.5-flash-lite` answered and `gemini-3.8-flash` timed out. The free tier has low daily limits and may use your data to improve Google's products, so use a paid key for real candidates |
| Anthropic Claude | [console.anthropic.com](https://console.anthropic.com) | `LLM_PROVIDER=claude`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` | |
| OpenAI or compatible | [platform.openai.com](https://platform.openai.com) | `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` (for compatible servers) | Covers the whole interview (speech-to-text and voice too) |
| xAI (Grok) | [console.x.ai](https://console.x.ai) | `LLM_PROVIDER=xai`, `XAI_API_KEY`, `XAI_MODEL` | Covers the whole interview. Plans through xAI's OpenAI-compatible API |
| Ollama (local) | [ollama.com](https://ollama.com), then `ollama pull <model>` | `LLM_PROVIDER=ollama`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | Runs locally. A 3B model was too weak in our tests (no usable plans or quotes); use 7B or more |

`LLM_PROVIDER=auto` uses the first provider that has a key. With no provider, plans come from a template and nothing is screened or evaluated.

**How questions are generated.** When you click **Generate** on a role, your AI provider writes the plan from:
- the job description, level and interview type (technical, behavioural, screening or mixed);
- the number of questions and the duration;
- the competencies and must-have requirements you listed.

The plan has competencies, questions with "what a strong answer covers", and the requirements. You review and approve it; every candidate of the role gets the same approved questions. During the interview the voice agent asks them in order and may ask short follow-ups (`MAX_FOLLOW_UPS_PER_QUESTION`).

**Common errors.**
- "The provider rejected the key (401)" means a wrong or revoked key.
- "Connected, but the model … isn't available here" means the model name is wrong for that key.
- A timeout means the model is overloaded. Try another model; Recruence also falls back on its own for Gemini.

## 3. Voice and transcription (the AI interviewer's voice)

The interviewer is MeetStream's MIA voice agent: speech-to-text, a conversation model, then text-to-speech. Recruence configures it and MeetStream runs it, using the provider keys saved in the MeetStream dashboard.

| Setting (Settings → AI interviewer) | `.env` | Options in Recruence |
| --- | --- | --- |
| Speech-to-text provider, model | `MIA_STT_PROVIDER`, `MIA_STT_MODEL` | openai, deepgram, assemblyai, xai |
| Conversation model provider, model | `MIA_LLM_PROVIDER`, `MIA_LLM_MODEL` | xai, openai, google, anthropic. MeetStream lists anthropic, but it has been reported not to work in practice. Only xai and google have been used live |
| Voice provider, voice | `MIA_TTS_PROVIDER`, `MIA_VOICE` | xai, openai, elevenlabs. The voice name or id comes from that provider |
| Turn-taking | `MIA_MIN_ENDPOINTING_DELAY`, `MIA_MAX_ENDPOINTING_DELAY`, `MIA_INTERRUPT_MIN_SECONDS`, `MIA_INTERRUPT_MIN_WORDS`, `MIA_VAD_MIN_SILENCE_MS` | 1.5–6 s before replying worked without cutting candidates off (Teams test) |
| Mode | `MIA_MODE`, or Settings → AI interviewer → Voice (Settings → AI provider → "Use Gemini for everything") | `pipeline` (recommended) or `realtime` (Gemini Live: Gemini alone hears, thinks and speaks; worked poorly in tests) |
| Language | `MIA_LANGUAGE` | English only for now |

**Configurations that worked live:**
- Deepgram `nova-3`, then Google `gemini-3.6-flash`, then ElevenLabs.
- xAI throughout, while that account had credit.

**Limitations:**
- If the conversation model is overloaded, the agent goes quiet (MeetStream doesn't retry). The dashboard shows the error. Asking "could you repeat the question?" or relaunching usually helps.
- Candidates need headphones; otherwise the agent's own voice gets transcribed as theirs.
- Not every AI provider in section 2 can be a voice provider. Only the ones listed here can.

## 4. What a recruiter configures

| Setting | Where | Affects |
| --- | --- | --- |
| Interview type (technical / behavioural / screening / mixed), level, number of questions, duration, competencies, must-have requirements | Role → Job / Questions | The AI interviewer (plan and pacing) |
| Interview format: voice, video (camera recorded), proctored (camera and screen sharing asked for) | The role's setup (Interview mode step), or the launch form | The AI interviewer and the recording |
| Questions in meeting chat | Launch form | The AI interviewer (not on Zoom) |
| Interview passing score (out of 100) | The role's setup, Settings step | The workflow: the ranking shows pass or fail. The interview itself is unchanged |
| Screening requirements (mandatory or preferred; skill, experience, education), screening threshold | The role's setup: Settings (requirements) and Assignment (threshold) steps | The workflow: who is invited automatically and who you review |
| Invite eligible applicants automatically | The role's setup, Assignment step (Automatic) | The workflow |
| Days to take the interview (invitation expiry) | The role's setup, Settings step | The workflow: the link stops working afterwards; resend from the interview |
| Attempts | Fixed: 3 starts per invitation | The workflow |
| Application deadline, pause and reopen applications | The role's setup (deadline); the role's Setup tab (pause, reopen) | The workflow |
| Recording retention, data retention | Settings → Database (`MEETSTREAM_RETENTION_HOURS`, `DATA_RETENTION_DAYS`) | Privacy |
| Consent | Always on: the candidate confirms the notice on their link, and the agent asks for consent first. A "no" ends the interview with nothing kept | The AI interviewer |
| Difficulty | Through the level (e.g. Junior, Senior) and the competencies; there's no separate difficulty or technical/behavioural ratio setting | The AI interviewer |

## 5. Your first real interview, step by step

Do this as yourself, with a second device or browser profile as the "candidate".

| # | Do | Expect | If not |
| --- | --- | --- | --- |
| 1 | Settings → MeetStream → Interview readiness | Everything required is Ready (voice says "Test in a call") | Each line links to where it's set |
| 2 | Roles → New role: title, description, type, 3 questions, 10 minutes | The role is created | |
| 3 | Questions → Generate, review, Approve | A plan from your AI provider (Source: the model's name) | "template + heuristic" means the AI provider failed. Check its test |
| 4 | The role's setup: interview room link, one mandatory requirement, threshold 0, auto-invite on, Publish | "Your careers website" shows the role's Application API | Publishing needs approved questions and a room |
| 5 | On your careers website (or the demo site, docs/DEMO_SITE.md), apply for the job with your second email and a short text resume | "Application received" on the website | The website's address must be in Settings → MeetStream → Websites that may submit applications |
| 6 | Wait about 30 s | In the role's Candidates tab: screening score, then Invited | "Needs review": a requirement wasn't shown with a quote. Use Review → Invite |
| 7 | Candidate inbox | An invitation with the interview link and deadline | No email: SMTP isn't set up; copy the link from the interview page |
| 8 | Open the link, check microphone and camera, tick the notice, press Start | A "Join the call" button with the room link | "Couldn't start right now": the public address or MeetStream check failed (Settings) |
| 9 | Join the call | "AI Interviewer" asks to join or joins (Meet: up to 3 minutes). It greets you and asks for consent | No bot: check the log for "MeetStream refused…" or a webhook error; check the voice keys in MeetStream Integrations |
| 10 | Answer the questions | Questions in order, short follow-ups | Silent agent: the conversation model is overloaded or out of quota (dashboard shows the error) |
| 11 | The agent closes the interview | The bot leaves; the interview shows Completed | Ends early: see the interview's activity log |
| 12 | Wait for the report (up to about 5 minutes while the recording is transcribed) | Transcript, scores out of 100, recommendation, evidence quotes | "Not evaluated": the AI provider failed. Use **Re-run evaluation** later |
| 13 | Video or Proctored format | The recording is in the MeetStream dashboard; proctored reports list screen-share events | Recording links aren't shown in Recruence |
| 14 | Your inbox (workspace's "report notes go to") | "Interview report ready: …" with the score and a link | Nobody listed: Settings → Workspace |
| 15 | Role → Candidates / Role report | The candidate ranked, pass or fail against the passing score | |

## 6. Public address {#public-address}

MeetStream and candidates must reach Recruence over HTTPS. Localhost can't be reached from the internet.

**Option A: a quick tunnel (testing).**
```bash
cloudflared tunnel --url http://localhost:8765     # desktop app (8000 for Docker or uvicorn)
```
Copy the `https://….trycloudflare.com` address into **Settings → MeetStream → Public application URL**, then run **Public address reaches this app**. A quick tunnel's address changes every time it starts, so update the setting each time.

**Option B: a named tunnel or your own domain (lasting).** Expose only `/webhooks/`, the candidate pages and their scripts. [DEPLOYMENT.md](DEPLOYMENT.md#exposing-only-the-webhooks-and-the-candidate-pages) has the exact ingress rules. Recruence refuses dashboard requests that come through a proxy or tunnel anyway.

**The desktop app as the recruiter's client.** Keep Recruence running on your computer with the tunnel pointed at port 8765, or run Recruence on a server (Docker) and use the dashboard from a browser. The desktop app always runs its own backend; it doesn't connect to a remote one.

Which features need the public address:
- Without it, these work: setup, accounts, workspaces, roles, AI plans, screening (when applications arrive), reports and email sending.
- With it, these also work: MeetStream callbacks (joining, transcript, end of call), candidates opening application, interview and booking links, and starting interviews.

## 7. Configuration checklist

- [ ] MeetStream account and API key (Settings → MeetStream); **MeetStream key** check passes
- [ ] Voice provider keys saved in MeetStream → Integrations, with credit
- [ ] AI provider in Settings → AI provider; **Test connection** passes
- [ ] Public address set; **Public address reaches this app** check passes
- [ ] SMTP set (optional); **Send test email** passes
- [ ] Report recipients set (Settings → Workspace)
- [ ] A meeting room link guests can join without the host
- [ ] One test interview with yourself completed and its report received

## 8. Errors and fixes

| Error | Cause | Fix |
| --- | --- | --- |
| "MeetStream refused the API key or is unreachable: [403]" | Wrong key | Create a new key in MeetStream |
| "Could not create the MIA voice agent … 400" | No key in MeetStream Integrations for the configured voice providers | Add the key there, or change the MIA_* providers |
| "PUBLIC_BASE_URL … doesn't resolve" | Tunnel not running, or the address is out of date | Start the tunnel; update the setting |
| `stt_error … 403` in the log | The speech-to-text provider account is out of credit or its key was revoked | Top up, or switch provider |
| The agent goes silent | The conversation model is overloaded or out of quota | Ask the candidate to say "could you repeat that?", or relaunch with another model |
| The bot waits in the lobby | Meet requires the host to admit guests | Admit it, or change the meeting's access |
| "Not evaluated" in the report | The AI provider failed during evaluation | Re-run evaluation once the provider works |
