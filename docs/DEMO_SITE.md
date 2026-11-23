# Demo company website (testing only)

`examples/demo-company-site/` is a small, made-up company website: a home page, a careers page, job pages and an application form with a resume upload. It shows how a real company's own site would hand applicants to Recruence. **It's for testing.** It's not part of Recruence, not served by Recruence, and the company in it isn't real.

Recruence has no application page of its own: candidates always apply on the company's website. The site reads each job from Recruence's application API (`GET /api/public/apply/<job key>`: the job, its requirements and application questions) and posts each application to it (`POST`, multipart form: `full_name`, `email`, optional `phone`, `location`, `current_role`, `years_experience`, `education`, `links`, `cover_note`, `q_<question id>` answers, `resume` file, and `consent=yes`). Recruence does everything after that: screening, invitations, interviews and reports.

## Try it

1. **In Recruence**, create a role and go through its setup (questions, assignment, interview mode, settings), then publish it on the Review step. On the role's Setup tab:
   - set an interview room and the requirements;
   - click **Publish**;
   - copy the **Application API** under "Your careers website".
2. **Allow the site:** Settings → MeetStream → **Websites that may submit applications** → `http://127.0.0.1:8300`. This lets that one address call the application API from the browser (CORS). Nothing else in Recruence is opened to it.
3. **Configure the site:** open `examples/demo-company-site/config.js`.
   - Paste the Application API address (or just the job key) into `jobs`.
   - Set `miaUrl` to where Recruence answers: `http://127.0.0.1:8765` for the desktop app, `http://127.0.0.1:8000` for a local server, or your public https address.
4. **Start it:** `npm run demo:site`, then open <http://127.0.0.1:8300> and apply as a test candidate. Use a synthetic name, and an email address you control.
5. **Back in Recruence:** the application shows up in the role's Candidates tab and is screened, and if it's eligible and the role invites automatically, an invitation is sent.

## Notes

- **Links in emails:** Recruence's emails link to Recruence's own interview page on its Public application URL, not to this site.
- **Real interviews** need that address to be reachable from the internet (docs/INTERVIEW_SETUP.md).
- **Limits still apply:** 10 applications an hour from one address, 200 an hour per role, one application per email per role.
- **A real company site** works the same way: add its address to the allowed websites and use each role's Application API. A site's server can also post to the API directly (no browser, no CORS needed).
