"use strict";

// The candidate's self-scheduling page: /book/<token>. No sign-in; the token in the address is the key.
// Times arrive in UTC and are shown in the candidate's own time zone.

const view = document.getElementById("view");
const token = location.pathname.split("/").filter(Boolean)[1] || "";
const api = `/api/public/booking/${encodeURIComponent(token)}`;
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "your time zone";

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

async function call(path, body) {
  const res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let data = {};
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? "Please check your choice and try again." : data.detail || res.statusText;
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return data;
}

const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long" });
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const fullFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" });

function card(...children) { return el("section", { class: "panel stack" }, ...children); }
function notice(kind, text) { return el("p", { class: `notice ${kind}` }, text); }

function header(b, title, sub) {
  return el("div", { class: "book-head" },
    el("img", { class: "mark", src: "/static/logo.png", alt: "", width: "46", height: "46" }),
    el("div", {}, el("p", { class: "eyebrow" }, b ? `${b.role_title} · interview` : "Interview"), el("h1", {}, title), sub ? el("p", { class: "sub" }, sub) : null));
}

function formatItem(b) {
  if (b.format === "video") return el("li", {}, "Please join with your camera on: the video is recorded with the call.");
  if (b.format !== "proctored") return null;
  const needs = [b.require_camera ? "keep your camera on" : null, b.require_screen_share ? "share your entire screen when asked" : null].filter(Boolean);
  return needs.length ? el("li", {}, `This is a proctored interview. Join from a computer and be ready to ${needs.join(" and ")}. The times your screen is shared are logged for the hiring team; what's on it isn't analysed.`) : null;
}

function aboutList(b) {
  const contact = b.contact ? `contact ${b.contact}` : "reply to the email that brought you here";
  if (b.mode === "analyst") {
    return el("ul", { class: "plain small" },
      el("li", {}, `It's a video call on ${b.platform}, about ${b.duration_minutes} minutes, with a member of the hiring team.`),
      formatItem(b),
      el("li", {}, `A note-taking assistant joins as “${b.bot_name}”. It doesn't speak; it transcribes the call so the team can review it, and an AI summarises the transcript. A person makes the decision.`),
      el("li", {}, `If you'd rather the call wasn't transcribed, or need an accommodation, ${contact}. Choosing this does not count against you.`),
      el("li", {}, "Join from a quiet place, ideally with headphones."));
  }
  return el("ul", { class: "plain small" },
    el("li", {}, `It's ${b.format === "voice" ? "an audio call" : "a video call"} on ${b.platform}, about ${b.duration_minutes} minutes${b.format === "voice" ? " (your camera can stay off)" : ""}. An AI interviewer asks ${b.question_count} questions about your experience and may ask short follow-ups. It joins as “${b.bot_name}”.`),
    formatItem(b),
    el("li", {}, "The call is recorded and transcribed. A person on the hiring team reviews it; the AI does not make the decision."),
    el("li", {}, "At the start the AI asks whether you agree to continue. You can say no, or ask to stop at any time."),
    el("li", {}, `If you would prefer a person to interview you, or need an accommodation, ${contact}. Choosing this does not count against you.`),
    el("li", {}, "Join from a quiet place and use headphones, so the AI's voice doesn't echo into your microphone."));
}

function slotPicker(slots, current) {
  const byDay = new Map();
  for (const iso of slots) {
    const d = new Date(iso);
    const key = dayFmt.format(d);
    if (!byDay.has(key)) byDay.set(key, []);
    byDay.get(key).push(iso);
  }
  return el("fieldset", { class: "slots" },
    el("legend", {}, `Available times (${zone})`),
    [...byDay.entries()].map(([day, times]) => el("div", { class: "slot-day" },
      el("p", { class: "slot-date" }, day),
      el("div", { class: "slot-row" }, times.map((iso) => el("label", { class: "slot" },
        el("input", { type: "radio", name: "slot", value: iso, checked: iso === current }),
        el("span", {}, timeFmt.format(new Date(iso)))))))));
}

function renderChoose(b, { changing = false } = {}) {
  const error = notice("bad", ""); error.hidden = true;
  if (!b.slots.length) {
    view.replaceChildren(header(b, `Hello ${b.first_name || "there"}`), card(
      notice("warn", "There are no times left to choose from. Please contact the hiring team for another time."),
      el("h2", {}, "About the interview"), aboutList(b)));
    return;
  }
  const ack = el("input", { type: "checkbox", id: "ack" });
  const submit = el("button", { class: "btn primary block", type: "submit" }, changing ? "Change to this time" : "Confirm this time");
  const form = el("form", { class: "stack" },
    slotPicker(b.slots, changing ? b.booked_at : null),
    el("label", { class: "check", for: "ack" }, ack, el("span", {}, b.mode === "analyst"
      ? "I have read how this interview works: it's transcribed, and a person reviews it."
      : "I have read how this AI interview works: it's recorded and transcribed, and a person reviews it.")),
    error,
    submit,
    changing ? el("button", { class: "btn block", type: "button", onclick: () => renderBooked(b) }, "Keep my current time") : null);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    const picked = form.querySelector("input[name=slot]:checked");
    if (!picked) { error.textContent = "Choose a time first."; error.hidden = false; return; }
    if (!ack.checked) { error.textContent = "Please confirm that you have read how the interview works."; error.hidden = false; ack.focus(); return; }
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    try {
      renderBooked(await call(api, { slot: picked.value, acknowledged: true }), true);
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
    }
  });
  view.replaceChildren(
    header(b, changing ? "Choose a new time" : `Hello ${b.first_name || "there"}`,
      changing ? null : `Please choose a time for your interview for the ${b.role_title} role.`),
    card(el("h2", {}, "About the interview"), aboutList(b)),
    card(el("h2", {}, "Pick a time"),
      b.expires_at && !changing ? el("p", { class: "small muted" }, `Please choose by ${fullFmt.format(new Date(b.expires_at))}. The link stops working after that.`) : null,
      form));
}

function renderBooked(b, justBooked = false) {
  const when = fullFmt.format(new Date(b.booked_at));
  const cancel = el("button", { class: "btn danger block", type: "button" }, "Cancel this time");
  const status = notice("bad", ""); status.hidden = true;
  cancel.addEventListener("click", async () => {
    if (cancel.dataset.armed !== "1") { cancel.dataset.armed = "1"; cancel.textContent = "Yes, cancel my interview time"; return; }
    cancel.disabled = true;
    try { renderChoose(await call(`${api}/cancel`, {})); } catch (err) { status.textContent = err.message; status.hidden = false; cancel.disabled = false; }
  });
  view.replaceChildren(
    header(b, justBooked ? "You're booked" : "Your interview time", null),
    card(
      el("div", { class: "booked-when" }, el("span", { class: "small muted" }, "Your interview"), el("strong", {}, when)),
      el("p", { class: "small" }, "At that time, join from your interview link. It emails you a one-time code first, then opens the call:"),
      b.interview_link ? el("p", {}, el("a", { href: b.interview_link, class: "break" }, b.interview_link))
        : el("p", { class: "small muted" }, "Your interview link is on its way by email."),
      el("div", { class: "row" },
        el("a", { class: "btn primary", href: `${api}/calendar.ics`, download: "interview.ics" }, "Add to calendar"),
        b.can_change ? el("button", { class: "btn", type: "button", onclick: () => renderChoose(b, { changing: true }) }, "Change time") : null),
      b.can_change ? cancel : el("p", { class: "muted small" }, "It's too close to the interview to change it here. Please contact the hiring team if you need to."),
      status),
    card(el("h2", {}, "About the interview"), aboutList(b)));
}

function renderMessage(title, text) {
  view.replaceChildren(header(null, title, null), card(el("p", {}, text)));
}

async function init() {
  try {
    const b = await call(api);
    if (b.state === "expired") renderMessage("This link has expired", "The time to choose an interview slot has passed. Please contact the hiring team if you'd still like to be interviewed.");
    else if (b.state === "closed") renderMessage("This interview is closed", "This interview has already taken place or is under way. Thank you.");
    else if (b.state === "booked") renderBooked(b);
    else renderChoose(b);
  } catch (err) {
    if (err.status === 404) renderMessage("This link isn't valid", "This booking link has expired or was withdrawn. Please contact the hiring team.");
    else renderMessage("Something went wrong", `${err.message} Please try again in a moment.`);
  }
}

init();
