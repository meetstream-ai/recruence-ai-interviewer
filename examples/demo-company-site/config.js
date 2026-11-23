// Demo company website settings. TEST SITE ONLY: a stand-in for a real company's careers pages.
//
// 1. In Recruence: create a role, approve its questions, publish it (the role's setup → Review → Confirm and publish), and copy its Application API from its Setup tab.
// 2. Paste that address (or just the job key) into jobs below; add as many as you like.
// 3. In Recruence: Settings → MeetStream → "Websites that may submit applications": add this site's address
//    (http://127.0.0.1:8300 when you run `npm run demo:site`).
// 4. Open http://127.0.0.1:8300. Applications you send go into Recruence's real pipeline: screening, invitations, interviews.
window.DEMO_SITE = {
  company: "Lumen Labs",            // a made-up company; change it if you like
  tagline: "Tools that help teams hear each other.",
  // Where Recruence's backend answers from this browser. The desktop app: http://127.0.0.1:8765.
  // With a tunnel or a server, its https address (the same as Recruence's Public application URL).
  miaUrl: "http://127.0.0.1:8765",
  jobs: [
    // "https://interviews.example.com/api/public/apply/<job key>",
    // "<job key>",
  ],
};
