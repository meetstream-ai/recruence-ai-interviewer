"use strict";

// A candidate's own interview link: /i/<token> (or /interview/<token>). They confirm it's them with a code sent to
// the email address they applied with, read the notice and start inside the interview's time window; the AI
// interviewer then joins the meeting and so do they. Only the device that entered the code gets the meeting link.

const view = document.getElementById("view");
const token = location.pathname.split("/").filter(Boolean)[1] || "";
const api = `/api/public/interview/${encodeURIComponent(token)}`;

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
    const err = new Error(typeof data.detail === "string" ? data.detail : "Something went wrong. Please try again.");
    err.status = res.status;
    throw err;
  }
  return data;
}

const fullFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" });

function card(...children) { return el("section", { class: "panel stack" }, ...children); }
let company = "";
function header(title, sub) {
  return el("div", { class: "book-head" }, el("div", {},
    company ? el("p", { class: "eyebrow" }, company) : null, el("h1", {}, title), sub ? el("p", { class: "sub" }, sub) : null));
}
function message(title, text) { view.replaceChildren(header(title), card(el("p", {}, text))); }

function aboutList(v) {
  const contact = v.contact ? `contact ${v.contact}` : "reply to the email that brought you here";
  if (v.mode === "analyst") {  // human-led: a person from the hiring team interviews; a silent assistant transcribes
    return el("ul", { class: "plain small" },
      el("li", {}, `A video call on ${v.platform}, about ${v.duration_minutes} minutes, with a member of the hiring team.`),
      el("li", {}, `A note-taking assistant joins as “${v.bot_name}”. It doesn't speak; it records and transcribes the call so the team can review it, and an AI summarises the transcript. A person makes the decision.`),
      el("li", {}, `If you need an accommodation, ${contact}.`),
      el("li", {}, "Join from a quiet place, with headphones if you can."));
  }
  const asked = Math.max(1, (v.question_count || 0));
  const needs = [v.require_camera ? "keep your camera on" : null, v.require_screen_share ? "share your entire screen when asked" : null].filter(Boolean);
  return el("ul", { class: "plain small" },
    el("li", {}, `${v.format === "voice" ? "An audio call" : "A video call"} on ${v.platform}, about ${v.duration_minutes} minutes${v.format === "voice" ? " (your camera can stay off)" : ""}. An AI interviewer starts by asking you to introduce yourself, then asks ${asked} questions and may ask short follow-ups. It joins as “${v.bot_name}”.`),
    v.format === "video" ? el("li", {}, "Join with your camera on: the video is recorded with the call.") : null,
    v.format === "proctored" && needs.length ? el("li", {}, `This is a proctored interview: join from a computer and be ready to ${needs.join(" and ")}. The times your screen is shared are logged; what's on it isn't analysed.`) : null,
    el("li", {}, "The call is recorded and transcribed. A person on the hiring team reviews it; the AI does not make the decision."),
    el("li", {}, "At the start the AI asks whether you agree to continue. You can say no, or ask to stop at any time."),
    el("li", {}, `If you would prefer a person to interview you, or need an accommodation, ${contact}. Choosing this does not count against you.`),
    el("li", {}, "Join from a quiet place, with headphones if you can."));
}

function renderJoin(v) {
  const scheduledJoin = v.state === "ready_to_join";
  view.replaceChildren(
    header(scheduledJoin ? "Your interview" : "Your interview has started", v.role_title),
    card(
      el("p", {}, v.mode === "analyst"
        ? `Join this call at the agreed time. Your interviewer and a note-taking assistant (“${v.bot_name}”) join it too; if you're asked to admit them, please do.`
        : scheduledJoin
        ? `Join this call. The AI interviewer (“${v.bot_name}”) joins at the start time; if you're asked to admit it, please do.`
        : `Join this call now. The AI interviewer (“${v.bot_name}”) joins it within about 2 minutes; if you're asked to admit it, please do.`),
      el("p", {}, el("a", { class: "btn primary block", href: v.meeting_link, target: "_blank", rel: "noopener noreferrer" }, "Join the call")),
      el("p", { class: "small muted break" }, v.meeting_link)),
    card(el("h2", {}, "About the interview"), aboutList(v)));
}

// A check of the microphone and camera before starting: in this browser only, nothing is recorded or sent.
// The interview itself runs in the video call, which asks for them again.
function deviceCheck() {
  const video = el("video", { class: "device-preview", autoplay: true, muted: true, playsinline: true, hidden: true });
  const meter = el("div", { class: "mic-meter", hidden: true, role: "img", "aria-label": "Microphone level" }, el("span", {}));
  const status = el("p", { class: "small muted", role: "status" });
  let stream = null;
  let stopLevel = () => {};
  const stop = () => { stopLevel(); if (stream) stream.getTracks().forEach((tr) => tr.stop()); stream = null; video.hidden = meter.hidden = true; button.textContent = "Check my microphone and camera"; };
  const button = el("button", { class: "btn block", type: "button", onclick: async () => {
    if (stream) { stop(); status.textContent = ""; return; }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
      video.srcObject = stream;
      video.hidden = meter.hidden = false;
      const ctx = new AudioContext();
      const analyser = ctx.createAnalyser();
      ctx.createMediaStreamSource(stream).connect(analyser);
      const data = new Uint8Array(analyser.fftSize);
      let frame;
      const tick = () => {
        analyser.getByteTimeDomainData(data);
        const peak = Math.max(...data.map((x) => Math.abs(x - 128))) / 128;
        meter.firstChild.style.width = `${Math.min(100, Math.round(peak * 220))}%`;
        frame = requestAnimationFrame(tick);
      };
      tick();
      stopLevel = () => { cancelAnimationFrame(frame); ctx.close(); };
      status.textContent = "If you can see yourself and the bar moves when you speak, you're ready. Nothing here is recorded.";
      button.textContent = "Stop the check";
    } catch (err) {
      stop();
      status.textContent = err && err.name === "NotAllowedError" ? "The browser wasn't allowed to use them. Allow the microphone and camera for this page and try again."
        : "No microphone or camera was found. You can still join the call from another device.";
    }
  } }, "Check my microphone and camera");
  return el("div", { class: "stack" }, button, video, meter, status);
}

// Step one: a code to the address the candidate applied with, so the interview opens only for them.
function renderVerify(v) {
  const code = el("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, pattern: "[0-9]*", "aria-label": "Code", class: "code-input" });
  const error = el("p", { class: "notice bad", role: "alert", hidden: true });
  const info = el("p", { class: "small muted", role: "status" });
  const send = el("button", { class: "btn block", type: "button" }, "Email me a code");
  const check = el("button", { class: "btn primary block", type: "submit" }, "Continue");
  const fail = (err) => { error.textContent = err.message; error.hidden = false; };
  send.addEventListener("click", async () => {
    error.hidden = true; send.disabled = true;
    try {
      const r = await call(`${api}/code`, {});
      info.textContent = `We sent a 6-digit code to ${r.email}. It works for ${r.minutes} minutes.`;
      send.textContent = "Send a new code";
      code.focus();
    } catch (err) { fail(err); }
    send.disabled = false;
  });
  const form = el("form", { class: "stack" }, el("label", { class: "field" }, "Code from the email", code), error, check);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    if (!/^\d{6}$/.test(code.value.trim())) { fail(new Error("Enter the 6-digit code from the email.")); return; }
    check.disabled = true;
    try { await call(`${api}/verify`, { code: code.value.trim() }); init(); } catch (err) { fail(err); check.disabled = false; }
  });
  view.replaceChildren(
    header(`Hello ${v.first_name || "there"}`, v.role_title),
    card(el("p", {}, "To keep your interview yours, we'll email a one-time code to ", el("strong", {}, v.verification.email || "the address you applied with"), "."),
      v.verification.can_send ? send : notice("A code can't be sent for this interview. Please contact the hiring team."), info, form),
    card(el("h2", {}, "About the interview"), aboutList(v)));
}

function notice(text) { return el("p", { class: "notice warn" }, text); }

function renderStart(v) {
  const ack = el("input", { type: "checkbox", id: "ack" });
  const error = el("p", { class: "notice bad", role: "alert", hidden: true });
  const start = el("button", { class: "btn primary block", type: "submit" }, "Start my interview now");
  const form = el("form", { class: "stack" },
    el("label", { class: "check", for: "ack" }, ack, el("span", {}, "I have read how this AI interview works: it's recorded and transcribed, and a person reviews it.")),
    error, start);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    if (!ack.checked) { error.textContent = "Please confirm that you have read how the interview works."; error.hidden = false; ack.focus(); return; }
    start.disabled = true;
    start.setAttribute("aria-busy", "true");
    start.textContent = "Starting…";
    try {
      renderJoin(await call(`${api}/start`, { acknowledged: true }));
    } catch (err) {
      if (err.status === 410 || err.status === 404) { init(); return; }
      error.textContent = err.message;
      error.hidden = false;
      start.disabled = false;
      start.removeAttribute("aria-busy");
      start.textContent = "Start my interview now";
    }
  });
  view.replaceChildren(
    header(`Hello ${v.first_name || "there"}`, v.role_title),
    card(el("p", {}, v.opens_at ? "Start when you're ready, between " : "Start whenever you're ready, before ",
      v.opens_at ? [el("strong", {}, fullFmt.format(new Date(v.opens_at))), " and "] : null,
      el("strong", {}, fullFmt.format(new Date(v.closes_at || v.expires_at))), ". You can take it once, on this device."),
      form),
    card(el("h2", {}, "Before you start"), deviceCheck()),
    card(el("h2", {}, "About the interview"), aboutList(v)));
}

async function init() {
  try {
    const v = await call(api);
    company = v.company || "";
    if (company) document.title = `Your interview · ${company}`;
    if ((v.state === "in_progress" || v.state === "ready_to_join") && v.meeting_link) renderJoin(v);
    else if (v.state === "in_progress") message("Your interview is under way", "It's open on another device. Return to the video call there, or contact the hiring team.");
    else if (v.state === "done") message("Interview complete", "You have completed this interview. Thank you; the hiring team will be in touch.");
    else if (v.state === "expired") message("This link has expired", "The time to take this interview has passed. Please contact the hiring team.");
    else if (v.state === "closed") message("This link can't be used", "Please contact the hiring team.");
    else if (v.state === "awaiting_time") message("Your interview time is being arranged", "A member of the hiring team leads this interview. They'll email you its time; come back to this link then.");
    else if (v.state === "not_yet") message("Your interview isn't open yet", `You can start it from ${fullFmt.format(new Date(v.opens_at))}. Come back to this link then.`);
    else if (v.elsewhere && !v.verification.verified) message("This interview is open on another device", "Continue on the device where you entered the code, or contact the hiring team.");
    else if (v.verification.required && !v.verification.verified) renderVerify(v);
    else renderStart(v);
  } catch (err) {
    if (err.status === 404) message("This link isn't valid", "The interview link may be mistyped or replaced by a newer one. Please contact the hiring team.");
    else message("Something went wrong", `${err.message} Please try again in a moment.`);
  }
}

init();
