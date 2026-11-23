# Recruitment sources and external ATS integrations

Recruence works as an applicant tracking system (ATS) by itself. It can also take candidates from another ATS (Greenhouse, Lever, or anything that sends webhooks) and send interview results back. In both cases every candidate goes through **the same pipeline**: screening, invitation, the AI interview, evaluation, the report and the recruiter's decision. There is no separate path for external candidates.

> **Where to find them.** External ATS integrations are under Settings → Recruitment sources → **Additional integrations (advanced)**, collapsed unless one is connected. The Generic connector works with anything that can send and receive signed JSON. Greenhouse and Lever need an organization account with the provider; until a connector has been validated with one, the dashboard labels it "Not tested – Organization account required" and never shows it as active, and the API reports `verified: false` with a `status_note` (`GET /api/integrations/providers`, `GET /api/integrations`). When you first connect an account, send one test candidate and check the sync log both ways.

## 1. Recruence as a standalone ATS

| What | Where |
| --- | --- |
| Jobs (roles): description, competencies, must-haves, approved interview questions | Roles → New role |
| Publishing, application deadline, pause, interview room, invitation days | The role's setup (Roles → the role → Setup), Settings step; publish and pause on its Setup tab |
| Eligibility requirements (mandatory / preferred) and screening threshold | The role's setup: Assignment (threshold) and Settings (requirements) steps |
| **Application questions** (up to 10, required or optional), returned by the application API for your careers site's form and read by the screening | The role's setup, Settings step → Application questions |
| Resume parsing (PDF, DOCX, text, up to 5 MB) | Careers website uploads, ATS resume files |
| AI screening with quoted evidence, score out of 100, recruiter override | Role pipeline → an application |
| Pipeline and statuses: screening, eligible, needs review, not eligible, invited, withdrawn; then the interview's own stages | Role pipeline, Interviews |
| Scheduling: candidate self-booking or the interview link with a deadline | The role's setup, interview page |
| Reports, ranking and comparison per role | Role → Report |
| **Recruiter notes** on an application (not shown to the candidate) | Application dialog → Recruiter notes |
| Decisions (advance / hold / reject) | Interview report |
| **Candidate search and filters** across all roles and sources: name, email, role, location; status, source, sync status. One person with several applications is shown once. | Candidates (sidebar) |

Native sources:

- **Your careers website**: candidates apply on the company's own site, which posts to each published role's application API (`/api/public/apply/<job key>`, shown on the role's Setup tab). Allow the site's origin in `PUBLIC_CORS_ORIGINS` (Settings → MeetStream → websites that may submit applications). Source `careers_site`. See [DEMO_SITE.md](DEMO_SITE.md). Recruence hosts no application page of its own; older applications from its former page keep the source `apply_page`.
- **A careers website's own server, one address for every job** (Settings → Recruitment sources → careers address, `/api/public/careers/<key>`). Keep the address on the site's server, never in browser code.
  - `GET` lists the workspace's published jobs: `ref`, `title`, `level`, `open`, `closed_reason`, `summary`, `job` (responsibilities, employment type, location, work arrangement), `deadline`, `job_description`, `requirements` and `questions` (`id`, `text`, `required`). It never lists job keys or candidates.
  - `POST` takes one application as `multipart/form-data`: `job_ref` (the job's `ref` from the list; it routes the application to exactly that job), or else `role` (the title) or `job` (the job key); `name`, `email`, `phone`, `linkedin`, `portfolio`, `message`; answers as `q_<question id>`; and `resume`. It answers 201 `{"received": true, "role_title": ...}`; 409 for a duplicate or a closed or paused job; 404 for an unknown or unpublished job or a replaced address; 422 for a missing required answer, invalid details or an unreadable resume; 413 above 5 MB; 429 when the job gets too many applications in an hour. Source `careers_site`; the site collects the candidate's consent.
  - The MeetStream Careers site is built this way.
- **Added by a recruiter** on a role: source `recruiter`.

## 2. External ATS integrations

### Supported providers and what they really do

| | Greenhouse | Lever | Generic (signed JSON) |
| --- | --- | --- | --- |
| Inbound | Web hooks `new_candidate_application`, `candidate_stage_change` | Webhooks `applicationCreated`, `candidateStageChange` | `POST` with Recruence's JSON, or your shape with a field mapping |
| Signature | `Signature: sha256 <hex>`: HMAC-SHA256 of the body with the web hook's secret key | `signature` in the body: HMAC-SHA256 of `token + triggeredAt` with the webhook's signature token | `X-MIA-Signature: sha256=<hex>` of `"<X-MIA-Timestamp>.<body>"`; timestamps older than 5 minutes are refused |
| Candidate details | In the web hook | Fetched from the Lever API (webhooks carry ids only) | In the webhook |
| Resume | Downloaded from the attachment link in the web hook | Downloaded through the Lever API | Text in the webhook, or an `https` URL Recruence downloads |
| ATS screening decision | A stage hook: reaching the chosen stage means eligible | A stage hook (stage id) | `application.screening`: `eligible` / `not_eligible` |
| Results back | A note on the candidate (Harvest API, `On-Behalf-Of` a user you choose) | A note on the opportunity (`perform_as` a user you choose) | Signed `POST` to your callback URL |
| Moving the candidate's stage in the ATS | **Not automated** (needs provider stage ids; the note states the outcome) | **Not automated** | Up to your receiver |
| Credentials | Web hook secret key, Harvest API key (Basic auth) | Webhook signature token, API key (Basic auth) | Webhook secret (16+ characters), used both ways |

**Workday, SmartRecruiters, Ashby, Teamtailor and others** connect through the Generic adapter, via the ATS's own outgoing webhooks or integration tool, or middleware (Workday Studio / Integration Cloud, Zapier, Make, n8n) that maps their payload with a field mapping. There is no dedicated Workday adapter: Workday tenants differ too much to build one without a tenant to test against.

Greenhouse is moving the Harvest API to v3 with OAuth. The API base URL is a setting, but the v3 authentication is not implemented yet.

### Connecting one

Settings → **Recruitment sources** → **Connect an ATS** (workspace owners and admins; recruiters can see sources and logs but not change them).

1. Choose the provider and give it a name.
2. Enter the credentials. They are encrypted with the installation's key (the same box as other saved secrets) and never shown again; the screen only says whether each one is set. Leave a field empty to keep the saved value.
3. Copy the **webhook URL** (`<PUBLIC_BASE_URL>/webhooks/ats/<token>`) into the ATS. The token in the URL identifies the integration; the signature proves the delivery comes from the ATS. Recruence needs a public HTTPS address for the ATS to reach it.
4. Choose the screening rule (below), map jobs to roles, and optionally set result labels.
5. **Test connection** checks the credentials with the provider's cheapest API call (or posts a signed test event to a Generic callback URL).

Settings per provider: Greenhouse `on_behalf_of_user_id`, `stage_name`, `api_base`; Lever `perform_as_user_id`, `stage_id`, `api_base`; Generic `callback_url`, `field_map`. Callback and API URLs must be `https` and public (no localhost or private addresses).

### Screening: Recruence's or the ATS's

- **Recruence screens every candidate** (default, recommended): the application is screened against the role's requirements like any other, and invited automatically if eligible and the role allows it.
- **Trust the ATS's screening**: candidates the ATS marks eligible (Generic `application.screening`, or reaching the chosen stage in Greenhouse/Lever) skip Recruence's AI screening and are invited by Recruence directly; `not_eligible` ones are recorded as such. Use a stage hook ("only act at stage") so the ATS decides when a candidate gets the AI interview.

### Mapping

- **Jobs → roles.** An ATS job id mapped to a Recruence role sends its candidates to that role. A job Recruence hasn't seen becomes a new **draft** role (title and description from the ATS) and is mapped from then on. A draft role has no approved questions or interview room, so its candidates stay eligible with "Not invited: …" until a recruiter finishes the role and invites them.
- **Fields** (Generic only). Default paths: `event_id`, `application.id`, `candidate.id`, `job.id`, `job.title`, `job.description`, `candidate.name`, `candidate.email`, `candidate.phone`, `candidate.location`, `candidate.current_role`, `candidate.links`, `resume.text`, `resume.url`, `resume.filename`, `application.answers` (list of `{question, answer}`), `application.url`, `application.screening`. Override any of them with dotted paths, e.g. `{"email": "applicant.contact.email", "application_id": "ref"}`.
- **Result labels.** Recruence's result stages are `passed`, `not_passed`, `incomplete` (part of the interview wasn't assessed, so the score can't pass or fail anyone), `evaluation_failed`, `advance`, `hold`, `reject`. Give each the name your ATS uses (e.g. `passed` → "Advance to onsite"); the label goes in the note or callback as `mapped_status`.

### Generic payloads

In:

```json
{
  "event_id": "evt-123",
  "job": {"id": "J-1", "title": "Backend Engineer", "description": "..."},
  "application": {"id": "A-1", "url": "https://ats.example.com/a/A-1", "answers": [{"question": "Notice period?", "answer": "One month"}],
                  "screening": "eligible"},
  "candidate": {"id": "C-9", "name": "Ana Silva", "email": "ana@example.com", "phone": "", "location": "Lisbon"},
  "resume": {"text": "...", "url": "https://files.example.com/cv.pdf", "filename": "cv.pdf"}
}
```

Headers: `X-MIA-Timestamp: <unix seconds>`, `X-MIA-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>`.

Out (to `callback_url`, signed the same way, `X-MIA-Event: interview.result`):

```json
{"event": "interview.result", "delivery_id": "…",
 "application": {"integration_id": "…", "provider": "generic", "candidate_id": "C-9", "application_id": "A-1", "job_id": "J-1", "url": "…"},
 "result": {"candidate_name": "Ana Silva", "role_title": "Backend Engineer", "stage": "passed", "mapped_status": "Advance",
            "score": 80, "recommendation": "yes", "summary": "…", "report_url": "https://<mia>/#/s/<session>", "decision": ""}}
```

Verify the signature and the timestamp on your side before trusting it.

## 3. One pipeline: how it fits together

```
ATS webhook ──► /webhooks/ats/<token> ──► adapter.verify ─► adapter.parse ─► (seen this delivery? stop)
                                                                │
                                         adapter.complete (fetch details from the ATS API when needed)
                                                                │
                      role from the job mapping (or a new draft role) ─► application:
                        same ATS application seen before → that one; same email for the role → linked; else new (source "ats")
                                                                │
                       Recruence screening (process_application)  or  the ATS's decision ─► invite_application
                                                                │
                                  interview ─► evaluation ─► report ─► recruiter decision
                                                                │
                  ResultSyncSink / decision hook ─► adapter.push_result (background, retried) ─► sync status + log
```

- **Common interface:** `app/ats/base.py` (`Adapter`: `verify`, `parse`, `complete`, `push_result`, `test`; `InboundApplication`, `Result`).
- **Adapters:** `app/ats/greenhouse.py`, `lever.py`, `generic.py`; registry in `app/ats/__init__.py`. Adding a provider means one new adapter class.
- **Service:** `app/ats/service.py` (inbound and outbound kept apart). It never screens, invites or interviews by itself; it calls the same `process_application` and `invite_application` the application page uses.
- **Routes:** `app/ats/routes.py`.
- **Storage:** tables `integrations` (per workspace; credentials encrypted inside `data`) and `integration_events` (the log; a unique `dedupe_key` makes webhooks idempotent and results sent once). Applications carry `source`, `external` (the ATS's ids and link), `sync` (pending / synced / failed / not_needed, with the last message), `answers` and `notes`. Schema version 3 adds them; older databases are migrated on start.

### Duplicates and idempotency

- The same delivery (the provider's event id) is processed once; a repeat is logged as `duplicate` and answered `200` so the ATS stops retrying.
- The same ATS application in a later event is recognised by its external id and not added again.
- A person who already applied for the role (by email, on any source) is **linked** to the ATS application, not duplicated, and never screened or invited again; a recruiter's earlier decision stands.
- The same person may apply for **several roles**; the Candidates page groups their applications.
- Invitations are protected by the existing re-read-before-invite guard, so concurrent screening and a recruiter's click can't send two.

### Results back, retries and failures

- After a report is saved (and after a re-evaluation or a recruiter's decision) for an application that came from an ATS, the result is sent in the background. Each result (report evaluated, report not evaluated, each decision) is sent **once**.
- A failure that may pass (network error, HTTP 5xx, 408, 429) is retried 3 times with backoff, then again **hourly** (up to 8 attempts in all) and on **Retry failed results** in the sync log. A failure that won't pass by itself (401/403, a missing setting) is not retried until it's fixed and retried by hand.
- The application shows **Sync**: sent, failed (with the reason), or sending.
- **Isolation:** a provider that is down, misconfigured or slow only marks its own events and applications as failed. Saving reports, Recruence's own applications and other integrations carry on (tested). A paused integration's webhook answers `404` and takes nothing.

### Security

- Every inbound delivery must be signed; unsigned, wrongly signed or (Generic) replayed deliveries get `401` and are logged as `rejected` without storing the body.
- Webhook bodies are limited to 2 MB, resumes to 5 MB; resumes are fetched only from public `https` URLs, without following redirects.
- Integrations, their logs and their candidates belong to one workspace. Webhook tokens are random 32-character strings; another workspace can't list, read or change an integration.
- Log messages name the ATS application id, not the candidate.
- Credentials never leave the server.

## 4. Testing

`tests/test_ats.py`, all **mocked** (`httpx.MockTransport` standing in for the providers, webhooks signed with test secrets):

- native: application questions (required ones enforced), answers stored and read by the screening, notes, candidate search and filters, careers-site source;
- Greenhouse, Lever, Generic: signature checks (wrong, missing, replayed), parsing and mapping, details and resumes from the API, field mapping, invalid or missing fields;
- duplicate deliveries, the same ATS application again, a person already in Recruence, one person for two roles;
- trusting the ATS's screening, unmapped jobs creating draft roles;
- results: signed callback, Greenhouse note, failure → retry → synced, no double send, decisions, non-retryable errors;
- an ATS that is down while native applications keep working; native candidates never trigger a sync;
- encryption of credentials, setting validation, admin-only changes, workspace isolation.

To try it by hand without an ATS: connect a **Generic** integration, then post a signed payload (e.g. from a script using the format above, or a Zapier/Make webhook step) to its webhook URL. The demo careers site ([DEMO_SITE.md](DEMO_SITE.md)) covers the native careers-site path.

Before relying on Greenhouse or Lever: connect a sandbox account, use **Test connection**, create the web hook in the ATS, send one test candidate, and check the sync log both ways.

## 5. Limitations

- Greenhouse and Lever adapters are unverified against live accounts.
- Stage changes are not written back to the ATS (notes only).
- No Greenhouse Harvest v3 (OAuth) authentication yet.
- No polling or bulk import: candidates arrive by webhook only, from the moment the webhook is set up.
- Deleting a candidate in Recruence doesn't delete them in the ATS, and vice versa.
- An ATS job's later edits (title, description) don't update the Recruence role created from it.
