// The demo company site: reads each job from Recruence's public application API and sends applications to it.
// Nothing about screening or interviews happens here; Recruence does all of it. Text from Recruence is shown as text only.
"use strict";

const cfg = window.DEMO_SITE || {};
const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

// A job is a role's application API address from Recruence (https://host/api/public/apply/<key>) or just its job key.
function parseJob(entry) {
  const text = String(entry || "").trim();
  const m = text.match(/^(https?:\/\/[^/]+)?\/?(?:api\/public\/apply\/)?([A-Za-z0-9_-]{16,64})\/?$/);
  if (!m) return null;
  return { base: (m[1] || cfg.miaUrl || "").replace(/\/$/, ""), token: m[2] };
}

const jobs = (cfg.jobs || []).map(parseJob).filter(Boolean);

async function fetchJob(job) {
  const res = await fetch(`${job.base}/api/public/apply/${job.token}`);
  if (!res.ok) throw new Error(res.status === 404 ? "Recruence doesn't know this job (copied wrong, or the role was unpublished)." : `Recruence answered ${res.status}`);
  return res.json();
}

function brand() {
  document.title = document.title.replace("Demo company", cfg.company || "Demo company");
  document.querySelectorAll("[data-company]").forEach((n) => { n.textContent = cfg.company || "Demo company"; });
  document.querySelectorAll("[data-tagline]").forEach((n) => { n.textContent = cfg.tagline || ""; });
}

function facts(r) {
  const j = r.job || {};
  return [j.employment_type, j.work_arrangement, j.location].filter(Boolean).join(" · ");
}

function problem(err) {
  const hint = /Failed to fetch|NetworkError|TypeError/i.test(String(err))
    ? `Can't reach Recruence at ${cfg.miaUrl}. Is it running, and is this site's address (${location.origin}) listed in Recruence's Settings → MeetStream → "Websites that may submit applications"?`
    : err.message;
  return el("p", { class: "problem" }, hint);
}

async function renderCareers() {
  const list = $("#jobs");
  if (!jobs.length) {
    list.replaceChildren(el("p", { class: "muted" }, "No openings configured yet. Add each role's application API address from Recruence to config.js (see the comments there)."));
    return;
  }
  list.replaceChildren(el("p", { class: "muted" }, "Loading openings…"));
  const cards = await Promise.all(jobs.map(async (job) => {
    try {
      const r = await fetchJob(job);
      return el("a", { class: "job-card", href: `job.html?job=${encodeURIComponent(job.base ? `${job.base}/api/public/apply/${job.token}` : job.token)}` },
        el("h3", {}, r.role_title, r.level ? ` · ${r.level}` : ""),
        facts(r) ? el("p", { class: "muted" }, facts(r)) : null,
        el("p", {}, r.job_description.slice(0, 180), r.job_description.length > 180 ? "…" : ""),
        el("span", { class: r.open ? "tag open" : "tag closed" }, r.open ? "Open" : (r.closed_reason || "Closed")));
    } catch (err) { return el("div", { class: "job-card" }, problem(err)); }
  }));
  list.replaceChildren(...cards);
}

async function renderJob() {
  const job = parseJob(new URLSearchParams(location.search).get("job"));
  const main = $("#job");
  if (!job) { main.replaceChildren(el("p", { class: "problem" }, "No job selected."), el("a", { href: "careers.html" }, "All openings")); return; }
  let r;
  try { r = await fetchJob(job); } catch (err) { main.replaceChildren(problem(err)); return; }
  document.title = `${r.role_title} · ${cfg.company}`;
  const req = (r.requirements || []).filter((q) => q.mandatory), nice = (r.requirements || []).filter((q) => !q.mandatory);
  const form = applyForm(job, r);
  main.replaceChildren(...[
    el("a", { class: "back", href: "careers.html" }, "← All openings"),
    el("h1", {}, r.role_title, r.level ? ` · ${r.level}` : ""),
    facts(r) ? el("p", { class: "muted" }, facts(r)) : null,
    r.deadline ? el("p", { class: "muted" }, `Apply by ${new Date(r.deadline).toLocaleString()}`) : null,
    el("section", {}, el("h2", {}, "About the role"), el("p", { class: "pre" }, r.job_description)),
    r.job && r.job.responsibilities ? el("section", {}, el("h2", {}, "What you'll do"), el("p", { class: "pre" }, r.job.responsibilities)) : null,
    req.length ? el("section", {}, el("h2", {}, "Requirements"), el("ul", {}, req.map((q) => el("li", {}, q.text)))) : null,
    nice.length ? el("section", {}, el("h2", {}, "Nice to have"), el("ul", {}, nice.map((q) => el("li", {}, q.text)))) : null,
    el("section", { id: "apply" }, el("h2", {}, "Apply"), r.open ? form : el("p", { class: "problem" }, r.closed_reason || "This role isn't taking applications."))]
    .filter(Boolean));
}

function applyForm(job, r) {
  const field = (label, name, attrs = {}) => el("label", {}, label, attrs.textarea ? el("textarea", { name, rows: 4, ...attrs.input }) : el("input", { name, ...attrs.input }));
  const error = el("p", { class: "problem", hidden: true });
  const send = el("button", { type: "submit" }, "Submit application");
  const form = el("form", { class: "apply" },
    field("Full name", "full_name", { input: { required: true, maxlength: 200, autocomplete: "name" } }),
    field("Email", "email", { input: { type: "email", required: true, maxlength: 320, autocomplete: "email" } }),
    field("Phone (optional)", "phone", { input: { maxlength: 40, autocomplete: "tel" } }),
    field("Location (optional)", "location", { input: { maxlength: 200 } }),
    field("Current role (optional)", "current_role", { input: { maxlength: 200 } }),
    field("Years of experience (optional)", "years_experience", { input: { type: "number", min: 0, max: 60, step: "0.5" } }),
    field("Education (optional)", "education", { textarea: true, input: { maxlength: 1000 } }),
    field("LinkedIn or portfolio (optional)", "links", { input: { maxlength: 500 } }),
    el("label", {}, "Resume (PDF, Word or text, up to 5 MB)", el("input", { name: "resume", type: "file", accept: ".pdf,.docx,.txt,.md" })),
    ...(r.questions || []).map((q) => field(q.required ? q.text : `${q.text} (optional)`, `q_${q.id}`, { textarea: true, input: { maxlength: 4000, required: q.required } })),
    field("Anything else you'd like us to know (optional)", "cover_note", { textarea: true, input: { maxlength: 4000 } }),
    el("label", { class: "check" }, el("input", { type: "checkbox", name: "consent", value: "yes", required: true }),
      el("span", {}, "I understand my application is checked against the role's requirements, partly by AI, and reviewed by the hiring team. If I'm invited, the interview is with an AI interviewer and is recorded.")),
    error, send);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    send.disabled = true;
    send.textContent = "Sending…";
    try {
      const res = await fetch(`${job.base}/api/public/apply/${job.token}`, { method: "POST", body: new FormData(form) });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Recruence answered ${res.status}`);
      form.replaceWith(el("div", { class: "done" }, el("h3", {}, "Thanks, your application is in."),
        el("p", {}, `We'll email you about the next step for ${r.role_title}. (Demo: it's now in Recruence's pipeline for screening.)`)));
    } catch (err) {
      error.textContent = problem(err).textContent;
      error.hidden = false;
      send.disabled = false;
      send.textContent = "Submit application";
    }
  });
  return form;
}

brand();
if ($("#jobs")) renderCareers();
if ($("#job")) renderJob();
