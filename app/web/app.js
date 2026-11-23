"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
let health = null;
let user = null; // set when signed in with an account or through the identity proxy (role decides what's shown)
let authInfo = null; // /api/auth/status
let meInfo = null; // /api/me: the account and its workspaces
let source = null; // EventSource of the open live session
// Workspace roles as people see them: the owner, admins who manage members, and members who recruit.
const ROLE_NAMES = { owner: "Owner", admin: "Admin", recruiter: "Member" };
// In the desktop app the page extends into the window's title bar (desktop/main.js): a draggable strip that also
// holds the workspace switcher and the account menu, next to the window buttons Windows draws.
if (window.miaDesktop) {
  document.body.classList.add("desktop");
  $(".titlebar").append($("#userchip"));
}

// Where an interview stands (app/lifecycle.py), shown instead of the bare status.
const STAGE_LABEL = {
  draft: "Draft", ready: "Ready", invitation_sent: "Invitation sent", pending_candidate: "Pending candidate", scheduled: "Scheduled",
  in_progress: "In progress", evaluation_pending: "Evaluation pending", report_ready: "Report ready", expired: "Expired",
  cancelled: "Cancelled", failed: "Failed",
};
const STATE_LABEL = {
  created: "starting", joining: "joining (2-3 min)", awaiting_consent: "waiting for consent",
  questioning: "interview in progress", leaving: "leaving the call", transcribing: "transcribing the recording (up to 5 min)…", evaluating: "evaluating…",
  completed: "completed", failed: "failed",
};
const REC_LABEL = {
  strong_yes: "Strong yes", yes: "Yes", no: "No", strong_no: "Strong no", insufficient_evidence: "Insufficient evidence",
};
const VERDICT_LABEL = { meets: "Meets", does_not_meet: "Does not meet", not_enough_evidence: "Not enough evidence" };
const TYPE_LABEL = { behavioral: "Behavioural", technical: "Technical", screening: "Screening", mixed: "Mixed" };

// ---------- helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "value") node.value = v;
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function add(node, ...children) {
  node.append(...children.flat().filter((c) => c != null && c !== false));
  return node;
}

class ApiError extends Error {}
// Calls whose 401 means a wrong password, token or code: the form shows it, rather than a sign-in screen.
const AUTH_FORMS = new Set(["/api/auth/login", "/api/setup/unlock", "/api/auth/account-login", "/api/me/password", "/api/connection/unlock"]);

async function api(path, opts = {}) {
  if (setupPreview) {
    const simulated = previewApi(path, opts);
    if (simulated !== undefined) return simulated;
  }
  const init = { headers: { "Content-Type": "application/json" }, ...opts };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const res = await fetch(path, init);
  // A 401 from the sign-in or setup-code call is a wrong token or code, shown by the caller; anywhere else it means "not signed in".
  if (res.status === 401 && !AUTH_FORMS.has(path)) { showSignin(); throw new ApiError("Please sign in"); }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = Array.isArray(body.detail) ? body.detail.map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ") : body.detail || detail;
    } catch { /* not JSON */ }
    if (detail && typeof detail === "object") {  // a settings save: {errors: {field: message}}
      const err = new ApiError(Object.values(detail.errors || {}).join("; ") || "Not saved");
      err.errors = detail.errors || {};
      throw err;
    }
    throw new ApiError(detail);
  }
  return res.json();
}

function fmtTime(iso) {
  if (!iso) return "Not set";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// An empty table cell: muted words, or a short mark with a spoken label for narrow number columns.
function empty(text, spoken = null) {
  return el("span", { class: "muted small", "aria-label": spoken }, text);
}

function pill(value, label) { return el("span", { class: `pill ${value}` }, label || value); }
// Sample records (app/demo.py) carry this wherever they're listed, so they're never taken for real candidates.
function demoTag(on) { return on ? el("span", { class: "pill demo", title: "Demo data: fictional, can't be run or emailed" }, "Demo") : null; }

function notice(kind, ...text) { return el("p", { class: `notice ${kind}` }, ...text); }

function busy(button, label) {
  const old = [...button.childNodes];
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = label;
  return () => { button.disabled = false; button.removeAttribute("aria-busy"); button.replaceChildren(...old); };
}

// Line icons, drawn inline so nothing is fetched.
const ICONS = {
  plus: "M12 5v14M5 12h14",
  role: "M3 9.5A2.5 2.5 0 0 1 5.5 7h13A2.5 2.5 0 0 1 21 9.5v8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5zM8 7V5.5A2.5 2.5 0 0 1 10.5 3h3A2.5 2.5 0 0 1 16 5.5V7M3 13h18",
  users: "M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20M10 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7M20 20v-1.5a3.5 3.5 0 0 0-2.5-3.35M15.5 4.15a3.5 3.5 0 0 1 0 6.7",
  check: "M20 6 9 17l-5-5",
  database: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  cloud: "M7 18.5a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 17.8 8.6a4.5 4.5 0 0 1-.3 9.9z",
  user: "M20 21v-1.5A4.5 4.5 0 0 0 15.5 15h-7A4.5 4.5 0 0 0 4 19.5V21M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8",
  swap: "M7 4 3 8l4 4M3 8h14M17 20l4-4-4-4M21 16H7",
  calendar: "M4 7.5A2.5 2.5 0 0 1 6.5 5h11A2.5 2.5 0 0 1 20 7.5v11a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 18.5zM4 10h16M8 3v4M16 3v4",
  live: "M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4M7.8 16.2a6 6 0 0 1 0-8.4M16.2 7.8a6 6 0 0 1 0 8.4M4.9 19.1a10 10 0 0 1 0-14.2M19.1 4.9a10 10 0 0 1 0 14.2",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14M20 20l-3.5-3.5",
  alert: "M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0",
  doc: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M9 13h6M9 17h4",
  video: "M3 7.5A2.5 2.5 0 0 1 5.5 5h8A2.5 2.5 0 0 1 16 7.5v9a2.5 2.5 0 0 1-2.5 2.5h-8A2.5 2.5 0 0 1 3 16.5zM16 10l5-3v10l-5-3",
  chat: "M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12",
  team: "M14 8h5a2 2 0 0 1 2 2v5a3 3 0 0 1-3 3h-1M3 7h11v9a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3zM6 10.5h5M8.5 10.5V16M17.5 5.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3",
  sliders: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M14 4v4M8 10v4M18 16v4",
  chart: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  spark: "M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8",
  mail: "M4 6h16a1 1 0 0 1 1 1v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a1 1 0 0 1 1-1M3 7l9 6 9-6",
  shield: "M12 21s-8-3.5-8-10V5l8-2 8 2v6c0 6.5-8 10-8 10",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18M12 7v5l3 2",
  chevron: "M6 9l6 6 6-6",
  logout: "M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H3",
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.9");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name] || ICONS.spark);
  svg.append(path);
  return svg;
}

function back(href, label) { return el("a", { class: "back", href }, "← ", label); }

// The top of every page: an optional back link, eyebrow, title, subtitle, and actions or a status on the right.
function pageHead({ backTo = null, eyebrow = null, title, sub = null, right = null }) {
  return el("div", { class: "stack" },
    backTo,
    el("div", { class: "page-head" },
      el("div", {}, eyebrow ? el("p", { class: "eyebrow" }, eyebrow) : null, el("h2", {}, title), sub ? el("p", { class: "sub" }, sub) : null),
      right));
}

function sectionTitle(name, title, extra = null) {
  return el("div", { class: "section-title" }, el("span", { class: "ico" }, icon(name)), el("h3", {}, title), extra);
}

function emptyState(name, ...children) {
  return el("div", { class: "panel empty" }, el("div", { class: "empty-ico" }, icon(name)), ...children);
}

// A table row that opens a page, by mouse or keyboard.
function linkRow(href, ...cells) {
  const go = () => { location.hash = href; };
  return el("tr", { class: "click", tabindex: "0", onclick: go, onkeydown: (e) => { if (e.key === "Enter") go(); } }, ...cells);
}

function initials(name) {
  return (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
}

// A glass confirmation dialog in place of the browser's confirm(); resolves true when confirmed.
function confirmDialog(message, { title = "Are you sure?", confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false } = {}) {
  return new Promise((resolve) => {
    const ok = el("button", { class: `btn ${danger ? "danger" : "primary"}`, type: "button", value: "ok" }, confirmLabel);
    const cancel = el("button", { class: "btn", type: "button" }, cancelLabel);
    const dlg = el("dialog", { class: `modal${danger ? " danger" : ""}`, "aria-labelledby": "dlg-title" },
      el("h2", { id: "dlg-title" }, el("span", { class: "dlg-ico" }, icon(danger ? "alert" : "spark")), title),
      typeof message === "string" ? el("p", {}, message) : message,
      el("div", { class: "row" }, cancel, ok));
    let answer = false;
    ok.addEventListener("click", () => { answer = true; dlg.close(); });
    cancel.addEventListener("click", () => dlg.close());
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); }); // a click on the backdrop
    dlg.addEventListener("close", () => { dlg.remove(); resolve(answer); });
    document.body.append(dlg);
    dlg.showModal();
    (danger ? cancel : ok).focus();
  });
}

// A short message in the corner, in place of the browser's alert().
function toast(message, kind = "bad", ms = 6000) {
  const close = el("button", { type: "button", "aria-label": "Dismiss" }, "×");
  const t = el("div", { class: `toast ${kind}`, role: kind === "bad" ? "alert" : "status" }, el("span", { class: "grow" }, message), close);
  const remove = () => { t.classList.add("leaving"); setTimeout(() => t.remove(), 200); };
  close.addEventListener("click", remove);
  $("#toasts").append(t);
  if (ms) setTimeout(remove, ms);
}

// ---------- mobile navigation drawer ----------

function setNav(open) {
  document.body.classList.toggle("nav-open", open);
  $("#scrim").hidden = !open;
  $("#menu-btn").setAttribute("aria-expanded", String(open));
}
$("#menu-btn").addEventListener("click", () => setNav(!document.body.classList.contains("nav-open")));
$("#scrim").addEventListener("click", () => setNav(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && document.body.classList.contains("nav-open")) setNav(false); });

// ---------- sidebar: a compact icon bar, names in tooltips ----------

// The tip lives in <body>, outside the frame's rounded clip, so it's never cut off. Shown wherever the labels are
// hidden (everywhere but the phone's drawer).
const navTip = $("#navtip");
function labelsHidden() {
  const label = $(".mainnav .nav-label");
  return label && getComputedStyle(label).display === "none";
}
function showNavTip(target) {
  if (!labelsHidden() || document.body.classList.contains("nav-open")) return;
  const r = target.getBoundingClientRect();
  navTip.textContent = target.dataset.tip;
  navTip.style.left = `${Math.round(r.right + 10)}px`;
  navTip.style.top = `${Math.round(r.top + r.height / 2)}px`;
  navTip.hidden = false;
}
function hideNavTip() { navTip.hidden = true; }
for (const item of document.querySelectorAll(".sidebar [data-tip]")) {
  item.addEventListener("mouseenter", () => showNavTip(item));
  item.addEventListener("focus", () => showNavTip(item));
  item.addEventListener("mouseleave", hideNavTip);
  item.addEventListener("blur", hideNavTip);
  item.addEventListener("click", hideNavTip);
}
$("#sidebar").addEventListener("scroll", hideNavTip, { passive: true });

function platformHint(link) {
  try {
    const host = new URL(link).hostname.toLowerCase();
    if (host === "meet.google.com") return "Google Meet";
    if (host === "zoom.us" || host.endsWith(".zoom.us")) return "Zoom";
    if (host === "teams.microsoft.com" || host === "teams.live.com") return "Microsoft Teams";
  } catch { /* not a URL yet */ }
  return "";
}

function closeSource() { if (source) { source.close(); source = null; } }

// ---------- sign-in ----------

// Signing in is always the account page; an access token (APP_ACCESS_TOKEN) stays for scripts and the API.
function showSignin() { renderLogin(); }

// ---------- header ----------

// The account menu, top right: who is signed in and the actions that work for them. Nothing here is
// for show: there is no profile page, so there is no profile link; sign-out exists only for the access
// token (an SSO session ends at the organisation's sign-in, and without a token nobody signs in).
function renderUser(auth) {
  const wrap = $("#userchip");
  const u = auth.user;
  if (!auth.signed_in) {
    wrap.replaceChildren(el("button", { class: "userchip", type: "button", onclick: showSignin },
      el("span", { class: "avatar", "aria-hidden": "true" }, "?"), el("span", { class: "who" }, el("strong", {}, "Sign in"))));
    return;
  }
  const via = u ? u.via : "local";
  const role = u ? u.role : "admin";
  const roleName = role[0].toUpperCase() + role.slice(1);
  const account = via === "account";
  const wsRole = account ? u.workspace_role : null;
  const name = account ? u.name : via === "proxy" ? u.name : roleName;
  const how = account ? `${u.email}${wsRole ? ` · ${ROLE_NAMES[wsRole] || wsRole}` : ""}`
    : via === "proxy" ? `${roleName} · Signed in through your organisation`
      : via === "token" ? "Signed in with the access token"
        : "Open on this computer, no sign-in set up";

  const items = [];
  const item = (label, iconName, action) => {
    const node = action.href
      ? el("a", { class: "menu-item", role: "menuitem", href: action.href, tabindex: "-1" }, icon(iconName), label)
      : el("button", { class: "menu-item", role: "menuitem", type: "button", tabindex: "-1", onclick: action.onclick }, icon(iconName), label);
    items.push(node);
    return node;
  };
  const signOut = async () => {
    close();
    try { await api("/api/auth/logout", { method: "POST" }); } catch { /* signed out either way */ }
    location.hash = "#/";
    location.reload();
  };
  const menu = el("div", { class: "usermenu", id: "usermenu", role: "menu", "aria-label": "Account", hidden: true },
    el("div", { class: "menu-head" }, el("strong", {}, name), el("span", { class: "small muted" }, how)),
    el("div", { class: "menu-group" },
      account ? item("Your account", "user", { href: "#/account" }) : null,
      item("Settings", "sliders", { href: "#/settings" }),
      (account && ["owner", "admin"].includes(wsRole)) || (!account && meInfo && meInfo.workspace_id) ? item("Workspace and members", "users", { href: "#/settings?tab=workspace" }) : null),
    account || via === "token" ? el("div", { class: "menu-group" }, item("Sign out", "logout", { onclick: signOut })) : null);

  const chip = el("button", { class: "userchip", type: "button", "aria-haspopup": "menu", "aria-expanded": "false", "aria-controls": "usermenu",
    "aria-label": `Account: ${name}` },
  el("span", { class: "avatar", "aria-hidden": "true" }, account || via === "proxy" ? initials(name.split("@")[0].replace(/[._-]+/g, " ")) : roleName[0]),
  el("span", { class: "who" }, el("strong", {}, name)),
  el("span", { class: "chev", "aria-hidden": "true" }, icon("chevron")));

  function open(focusLast = false) {
    menu.hidden = false;
    chip.setAttribute("aria-expanded", "true");
    (focusLast ? items[items.length - 1] : items[0]).focus();
  }
  function close(refocus = false) {
    if (menu.hidden) return;
    menu.hidden = true;
    chip.setAttribute("aria-expanded", "false");
    if (refocus) chip.focus();
  }
  chip.addEventListener("click", () => (menu.hidden ? open() : close()));
  chip.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); open(e.key === "ArrowUp"); }
  });
  menu.addEventListener("keydown", (e) => {
    const n = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); items[(n + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(n - 1 + items.length) % items.length].focus(); }
    else if (e.key === "Home") { e.preventDefault(); items[0].focus(); }
    else if (e.key === "End") { e.preventDefault(); items[items.length - 1].focus(); }
    else if (e.key === "Escape") { e.preventDefault(); close(true); }
    else if (e.key === "Tab") close();
  });
  menu.addEventListener("click", (e) => { if (e.target.closest("a.menu-item")) close(); });
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) close(); });
  window.addEventListener("hashchange", () => close());
  wrap.replaceChildren(...[workspaceSwitcher(), chip, menu].filter(Boolean));
}

// The workspace switcher, next to the account: changes which workspace's roles, candidates and reports are shown.
// It never changes the database (that's "Switch database…" in the account menu).
function workspaceSwitcher() {
  const approved = meInfo ? (meInfo.workspaces || []).filter((w) => w.status === "approved") : [];
  if (!approved.length) return null;
  const current = approved.find((w) => w.id === meInfo.workspace_id) || approved[0];
  const items = [];
  const choose = async (w) => {
    if (w.id === current.id) { close(true); return; }
    try {
      await api("/api/workspaces/switch", { method: "POST", body: { workspace_id: w.id } });
      location.hash = "#/";
      location.reload(); // nothing from the previous workspace stays on screen or in memory
    } catch (err) { toast(err.message); }
  };
  const option = (w) => {
    const on = w.id === current.id;
    const b = el("button", { class: `menu-item${on ? " current" : ""}`, role: "menuitemradio", "aria-checked": String(on), type: "button", tabindex: "-1",
      onclick: () => choose(w) }, el("span", { class: "ws-badge", "aria-hidden": "true" }, initials(w.name)), el("span", { class: "grow" }, w.name), on ? icon("check") : null);
    items.push(b);
    return b;
  };
  const manage = el("a", { class: "menu-item", role: "menuitem", href: "#/account", tabindex: "-1" }, icon("plus"), "Create or join a workspace");
  const menu = el("div", { class: "usermenu ws-menu", id: "wsmenu", role: "menu", "aria-label": "Workspaces", hidden: true },
    el("div", { class: "menu-group first" }, approved.map(option)), el("div", { class: "menu-group" }, manage));
  items.push(manage);
  const button = el("button", { class: "ws-switch", type: "button", "aria-haspopup": "menu", "aria-expanded": "false", "aria-controls": "wsmenu",
    "aria-label": `Workspace: ${current.name}` }, icon("users"), el("span", { class: "ws-name" }, current.name), el("span", { class: "chev", "aria-hidden": "true" }, icon("chevron")));
  const wrap = el("div", { class: "ws-wrap" }, button, menu);
  function open(focusLast = false) {
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    (focusLast ? items[items.length - 1] : items.find((x) => x.classList.contains("current")) || items[0]).focus();
  }
  function close(refocus = false) {
    if (menu.hidden) return;
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (refocus) button.focus();
  }
  button.addEventListener("click", () => (menu.hidden ? open() : close()));
  button.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); open(e.key === "ArrowUp"); } });
  menu.addEventListener("keydown", (e) => {
    const n = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); items[(n + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(n - 1 + items.length) % items.length].focus(); }
    else if (e.key === "Escape") { e.preventDefault(); close(true); }
    else if (e.key === "Tab") close();
  });
  manage.addEventListener("click", () => close());
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) close(); });
  window.addEventListener("hashchange", () => close());
  return wrap;
}

// ---------- page search (top bar): only on pages with something to search ----------

const pageSearch = $("#page-search");
const pageSearchInput = $("#global-search");
let pageSearchHandler = null;
let pageSearchTimer;

function setPageSearch({ placeholder, value = "", onSearch }) {
  pageSearchInput.placeholder = placeholder;
  pageSearchInput.setAttribute("aria-label", placeholder);
  pageSearchInput.value = value;
  pageSearchHandler = onSearch;
  pageSearch.hidden = false;
}

function clearPageSearch() {
  clearTimeout(pageSearchTimer);
  pageSearchHandler = null;
  pageSearchInput.value = "";
  pageSearch.hidden = true;
}

pageSearchInput.addEventListener("input", () => {
  clearTimeout(pageSearchTimer);
  const value = pageSearchInput.value;
  pageSearchTimer = setTimeout(() => { if (pageSearchHandler) pageSearchHandler(value); }, 200);
});
pageSearchInput.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && pageSearchInput.value) { pageSearchInput.value = ""; if (pageSearchHandler) pageSearchHandler(""); }
});

// ---------- dashboard ----------

// A small area chart of daily counts, drawn as inline SVG. The gradient it fills with is defined once per page.
function sparkline(values, label) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  const w = 120, h = 46, pad = 3;
  const peak = Math.max(1, ...values);
  const step = w / Math.max(1, values.length - 1);
  const pts = values.map((v, n) => [n * step, h - pad - (v / peak) * (h - pad * 2)]);
  const line = pts.map(([x, y], n) => `${n ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "spark");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${label}: ${values.join(", ")}`);
  if (!document.getElementById("spark-fill")) {
    const defs = document.createElementNS(NS, "svg");
    defs.setAttribute("width", "0"); defs.setAttribute("height", "0"); defs.setAttribute("aria-hidden", "true");
    defs.style.position = "absolute";
    defs.innerHTML = '<defs><linearGradient id="spark-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#559BEE" stop-opacity=".45"/><stop offset="1" stop-color="#559BEE" stop-opacity="0"/></linearGradient></defs>';
    document.body.append(defs);
  }
  const area = document.createElementNS(NS, "path");
  area.setAttribute("class", "spark-area");
  area.setAttribute("d", `${line} L${w},${h} L0,${h} Z`);
  const path = document.createElementNS(NS, "path");
  path.setAttribute("class", "spark-line");
  path.setAttribute("d", line);
  svg.append(area, path);
  return svg;
}

// Counts and recent activity across every interview, for the top of the dashboard.
function dashboardOverview(all) {
  const by = (s) => all.filter((i) => i.status === s).length;
  const undecided = all.filter((i) => i.status === "completed" && !i.decision).length;
  const days = 14;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  // Interviews per day over the last two weeks, by when each was last updated (the list has no history of
  // status changes, so a "completed" line counts completed interviews by the day they were last touched).
  const perDay = (items) => {
    const counts = Array(days).fill(0);
    for (const i of items) {
      if (!i.updated_at) continue;
      const d = new Date(i.updated_at); d.setHours(0, 0, 0, 0);
      const back = Math.round((today - d) / 86400000);
      if (back >= 0 && back < days) counts[days - 1 - back] += 1;
    }
    return counts;
  };
  const stat = (kind, name, label, value, note, series) => el("div", { class: `panel stat ${kind}` },
    el("div", { class: "stat-top" }, el("span", { class: "stat-label" }, label), el("span", { class: "stat-ico" }, icon(name))),
    el("div", { class: "stat-body" },
      el("div", {}, el("span", { class: "stat-value" }, String(value)), el("span", { class: "stat-note block" }, note)),
      series.some((v) => v > 0) ? sparkline(series, `${label}: last updated per day, last ${days} days`) : null));  // no flat line when nothing happened
  const statusSeries = (s) => perDay(all.filter((i) => i.status === s));
  return [
    el("div", { class: "stat-grid" },
      stat("", "users", "Total interviews", all.length, `${by("draft") + by("ready")} not yet run`, perDay(all)),
      stat("", "check", "Completed", by("completed"), undecided ? `${undecided} awaiting your decision` : "All decided", statusSeries("completed")),
      stat("", "calendar", "Scheduled", by("scheduled"), by("scheduled") ? "Booked by candidates" : "None booked", statusSeries("scheduled")),
      stat("", "live", "In progress", by("in_progress"), by("failed") ? `${by("failed")} failed, can be relaunched` : by("in_progress") ? "Live in a meeting now" : "None live right now", statusSeries("in_progress"))),
    dashboardActivity(),
  ];
}

// What happened each day: interviews created, run, completed, and decisions recorded. Real events from
// /api/activity (UTC timestamps), grouped by the viewer's own calendar day.
const ACTIVITY_SERIES = [
  { key: "created", label: "Created", cls: "s-created" },
  { key: "run", label: "Run", cls: "s-run" },
  { key: "completed", label: "Completed", cls: "s-completed" },
  { key: "decided", label: "Decided", cls: "s-decided" },
];

function dashboardActivity() {
  let days = 14;
  const body = el("div", { class: "activity-body" });
  const totals = el("div", { class: "legend activity-legend" });
  const ranges = el("div", { class: "seg", role: "group", "aria-label": "Activity period" });
  const panel = el("section", { class: "panel stack activity" },
    el("div", { class: "spread" }, sectionTitle("chart", "Activity"), ranges), totals, body);
  const drawRanges = () => ranges.replaceChildren(...[14, 30, 90].map((n) => el("button", {
    type: "button", class: "seg-btn", "aria-pressed": String(n === days), onclick: () => { days = n; drawRanges(); load(); },
  }, `${n} days`)));

  async function load() {
    body.replaceChildren(el("div", { class: "chart-skeleton", "aria-hidden": "true" }, Array.from({ length: 14 }, () => el("span"))));
    totals.replaceChildren();
    let data;
    try { data = await api(`/api/activity?days=${days}`); } catch (err) { body.replaceChildren(notice("bad", `Couldn't load the activity: ${err.message}`)); return; }
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const buckets = Array.from({ length: days }, (_, n) => {
      const d = new Date(today); d.setDate(d.getDate() - (days - 1 - n));
      return { d, created: 0, run: 0, completed: 0, decided: 0 };
    });
    for (const { key } of ACTIVITY_SERIES) {
      for (const ts of data[key]) {
        const d = new Date(ts); d.setHours(0, 0, 0, 0);
        const back = Math.round((today - d) / 86400000);
        if (back >= 0 && back < days) buckets[days - 1 - back][key] += 1;
      }
    }
    const sums = Object.fromEntries(ACTIVITY_SERIES.map(({ key }) => [key, buckets.reduce((n, b) => n + b[key], 0)]));
    totals.replaceChildren(...ACTIVITY_SERIES.map(({ key, label, cls }) => el("span", {}, el("i", { class: cls }), `${label} `, el("strong", {}, String(sums[key])))));
    if (!Object.values(sums).some(Boolean)) {
      body.replaceChildren(el("div", { class: "chart-empty" }, el("p", {}, `Nothing happened in the last ${days} days.`),
        el("p", { class: "muted small" }, "Interviews you create, run and decide on appear here day by day.")));
      return;
    }
    body.replaceChildren(activityChart(buckets));
  }
  drawRanges();
  load();
  return panel;
}

function activityChart(buckets) {
  const peak = Math.max(1, ...buckets.flatMap((b) => ACTIVITY_SERIES.map(({ key }) => b[key])));
  // A round top for the axis: 1, 2, 5, 10, 20, 50...
  const top = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000].find((n) => n >= peak) || peak;
  const dayFmt = (d) => d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const step = Math.ceil(buckets.length / 10); // about ten date labels whatever the period
  const tip = el("div", { class: "chart-tip", role: "status", "aria-live": "polite", hidden: true });
  let active = -1;
  const cols = buckets.map((b, n) => el("div", { class: "a-col", "data-n": String(n), onmouseenter: () => show(n) },
    el("div", { class: "a-bars" }, ACTIVITY_SERIES.map(({ key, cls }) =>
      el("span", { class: `a-bar ${cls}${b[key] ? "" : " zero"}`, style: `height:${(b[key] / top) * 100}%` }))),
    el("span", { class: "a-label" }, n % step === (buckets.length - 1) % step ? dayFmt(b.d) : "")));
  const plot = el("div", { class: "a-plot" }, cols);
  const grid = el("div", { class: "a-grid", "aria-hidden": "true" },
    [top, top / 2, 0].map((v) => el("div", {}, el("span", {}, String(Math.round(v * 10) / 10)))));
  const chart = el("div", {
    class: "a-chart", tabindex: "0", role: "group",
    "aria-label": "Activity per day. Use the left and right arrow keys to read each day.",
    onmouseleave: () => show(-1),
    onkeydown: (e) => {
      if (e.key === "ArrowRight") { e.preventDefault(); show(Math.min(buckets.length - 1, active + 1)); }
      if (e.key === "ArrowLeft") { e.preventDefault(); show(Math.max(0, active < 0 ? buckets.length - 1 : active - 1)); }
      if (e.key === "Escape") show(-1);
    },
    onblur: () => show(-1),
  }, grid, plot, tip);

  function show(n) {
    active = n;
    cols.forEach((c, k) => c.classList.toggle("on", k === n));
    if (n < 0) { tip.hidden = true; return; }
    const b = buckets[n];
    tip.replaceChildren(el("strong", {}, b.d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })),
      ...ACTIVITY_SERIES.map(({ key, label, cls }) => el("div", {}, el("i", { class: cls }), `${label}: ${b[key]}`)));
    tip.hidden = false;
    const col = cols[n];
    const x = col.offsetLeft + col.offsetWidth / 2;
    const flip = x > plot.offsetWidth * 0.7;
    tip.style.left = `${x + plot.offsetLeft}px`;
    tip.classList.toggle("flip", flip);
  }
  return chart;
}

// The dashboard: the overview and activity, then hiring by role (the role page holds each role's candidates).
async function renderDashboard() {
  const [everything, insights] = await Promise.all([api("/api/interviews"), api("/api/insights/roles")]);
  view.replaceChildren(el("div", { class: "stack" },
    pageHead({ title: "Dashboard",
      right: el("a", { class: "btn primary", href: "#/roles/new" }, icon("plus"), "New role") }),
    ...dashboardOverview(everything),
    roleInsights(insights)));
}

function roleInsights(rows) {
  const panel = el("section", { class: "panel stack" }, el("div", { class: "spread" }, sectionTitle("role", "Hiring by role"), el("a", { class: "small", href: "#/roles" }, "All roles")));
  if (!rows.length) {
    panel.append(el("p", { class: "muted flush" }, "No roles yet. ", el("a", { href: "#/roles/new" }, "Create a role"), "."));
    return panel;
  }
  const n = (v) => (v ? String(v) : empty("0", "None"));
  panel.append(el("div", { class: "table-wrap" }, el("table", { class: "list" },
    el("thead", {}, el("tr", {}, ["Role", "Applied", "To review", "Invited", "Interviewed", "Passed", "Top candidate"].map((h, i) =>
      el("th", { class: [1, 3].includes(i) ? "hide-sm num" : i > 0 && i < 6 ? "num" : null }, h)))),
    el("tbody", {}, rows.map((r) => linkRow(`#/r/${r.id}`,
      el("td", {}, el("strong", {}, r.role_title, " ", demoTag(r.demo)), el("div", { class: "muted small" }, r.demo ? "Demo role" : r.open ? "Accepting applications" : r.published ? "Applications closed" : "Not published")),
      el("td", { class: "num hide-sm" }, n(r.summary.applied)),
      el("td", { class: "num" }, r.summary.to_review ? el("strong", {}, String(r.summary.to_review)) : n(0)),
      el("td", { class: "num hide-sm" }, n(r.summary.invited)),
      el("td", { class: "num" }, n(r.summary.completed)),
      el("td", { class: "num" }, n(r.summary.passed)),
      el("td", {}, r.top ? el("span", {}, `${r.top.candidate_name} · ${r.top.score}`) : empty("-", "No evaluated candidate"))))))));
  return panel;
}

// ---------- setup form (new + edit) ----------

function competenciesToText(list) {
  return (list || []).map((c) => (c.description ? `${c.name}: ${c.description}` : c.name)).join("\n");
}

function textToCompetencies(text) {
  return text.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
    const i = l.indexOf(":");
    return i > 0 ? { name: l.slice(0, i).trim(), description: l.slice(i + 1).trim() } : { name: l, description: "" };
  });
}

// A resume box with file upload. The server extracts the text; only the text is kept, never the file.
function resumeField(text) {
  const resume = el("textarea", { id: "f-resume", rows: 8, placeholder: "Paste the resume text, or upload a PDF, Word (.docx) or text file" }, text || "");
  const fileStatus = el("span", { class: "hint" });
  const file = el("input", { type: "file", accept: ".pdf,.docx,.txt,.md,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,text/markdown", onchange: async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    fileStatus.textContent = `Reading ${f.name}…`;
    // The server extracts the text; only the text is kept, never the file.
    const body = new FormData();
    body.append("file", f);
    try {
      const res = await fetch("/api/resume/extract", { method: "POST", body });
      const out = await res.json().catch(() => ({}));
      if (res.status === 401) { showSignin(); throw new Error("Please sign in"); }
      if (!res.ok) throw new Error(out.detail || res.statusText);
      resume.value = out.text;
      fileStatus.textContent = `Read ${out.chars.toLocaleString()} characters from ${f.name}` + (out.truncated ? " (cut to the 60,000-character limit)" : "") + ". Check the text below.";
    } catch (err) {
      fileStatus.textContent = `Couldn't read ${f.name}: ${err.message}`;
    }
    e.target.value = "";
  } });
  return { resume, field: el("label", { class: "field" }, "Resume", resume, el("span", { class: "row" }, file, fileStatus)) };
}

// mode "all": a standalone interview. "role": the job only. "candidate": the candidate only (a role's candidate).
function setupForm(initial, submitLabel, onSubmit, mode = "all") {
  const s = initial || { interview_type: "mixed", question_count: 4, duration_minutes: (health && health.default_duration_minutes) || 20 };
  const withRole = mode !== "candidate";
  const withCandidate = mode !== "role";
  const { resume, field: resumeBox } = resumeField(s.resume_text);
  const error = notice("bad"); error.hidden = true;
  const submit = el("button", { class: "btn primary", type: "submit" }, submitLabel);
  const candidateFields = withCandidate ? [
    el("div", { class: "form-grid" },
      el("label", { class: "field" }, "Candidate name", el("input", { type: "text", id: "f-name", required: true, maxlength: 200, value: s.candidate_name || "" })),
      el("label", { class: "field" }, el("span", {}, "Candidate email ", el("span", { class: "hint" }, "(optional; used for the invitation email)")),
        el("input", { type: "email", id: "f-email", maxlength: 320, value: s.candidate_email || "" })),
    ),
    resumeBox,
  ] : [];
  const roleFields = withRole ? [
    el("div", { class: "form-grid" },
      el("label", { class: "field" }, "Role title", el("input", { type: "text", id: "f-role", required: true, maxlength: 200, value: s.role_title || "" })),
      el("label", { class: "field" }, "Level", el("input", { type: "text", id: "f-level", maxlength: 100, placeholder: "e.g. Senior", value: s.level || "" })),
      el("label", { class: "field" }, "Interview type",
        el("select", { id: "f-type" }, Object.entries(TYPE_LABEL).map(([v, l]) => el("option", { value: v, selected: v === s.interview_type }, l)))),
    ),
    el("label", { class: "field" }, "Job description",
      el("textarea", { id: "f-jd", rows: 8, required: true, placeholder: "Paste the job description" }, s.job_description || "")),
  ] : [];
  const criteriaFields = withRole ? [
    el("label", { class: "field" },
      el("span", {}, "Evaluation criteria ", el("span", { class: "hint" }, "one competency per line, as “Name: what good looks like”. Leave empty to let the AI propose them.")),
      el("textarea", { id: "f-comps", rows: 4, placeholder: "Problem solving: breaks problems down and weighs trade-offs\nCommunication: explains ideas clearly" }, competenciesToText(s.competencies))),
    el("label", { class: "field" },
      el("span", {}, "Must-have requirements ", el("span", { class: "hint" }, "optional, one per line, up to 10. Each is judged Meets / Does not meet / Not enough evidence, only from what the candidate says. Keep them job-related.")),
      el("textarea", { id: "f-reqs", rows: 3, placeholder: "3+ years running Python services in production\nHas led an incident response as on-call lead" }, (s.requirements || []).join("\n"))),
    el("div", { class: "form-grid" },
      el("label", { class: "field" }, "Planned questions", el("input", { type: "number", id: "f-count", min: 1, max: 10, value: s.question_count })),
      el("label", { class: "field" }, "Length (minutes)", el("input", { type: "number", id: "f-duration", min: 5, max: 90, value: s.duration_minutes })),
    ),
  ] : [];
  const section = (title, fields) => (fields.length ? el("div", { class: "form-section stack" }, el("h3", {}, title), fields) : null);
  const form = el("form", { class: "panel stack" },
    section("The job", roleFields), section("The candidate", candidateFields), section("Evaluation", criteriaFields),
    error, el("div", { class: "form-actions" }, submit));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    const body = {};
    if (withRole) Object.assign(body, {
      role_title: $("#f-role", form).value.trim(),
      level: $("#f-level", form).value.trim(),
      interview_type: $("#f-type", form).value,
      job_description: $("#f-jd", form).value.trim(),
      competencies: textToCompetencies($("#f-comps", form).value).map((c, n) => ({ ...c, rubric: (s.competencies || [])[n]?.rubric || "" })),
      requirements: $("#f-reqs", form).value.split("\n").map((l) => l.trim()).filter(Boolean),
      question_count: Number($("#f-count", form).value),
      duration_minutes: Number($("#f-duration", form).value),
    });
    if (withCandidate) Object.assign(body, {
      candidate_name: $("#f-name", form).value.trim(),
      candidate_email: $("#f-email", form).value.trim(),
      resume_text: resume.value.trim(),
    });
    const done = busy(submit, "Saving…");
    try {
      await onSubmit(body, (label) => { submit.textContent = label; });
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      done();
    }
  });
  return form;
}

// ---------- roles: one approved question set for every candidate ----------

async function renderRoles() {
  const roles = await api("/api/roles");
  const head = pageHead({ title: "Roles",
    right: el("a", { class: "btn primary", href: "#/roles/new" }, icon("plus"), "New role") });
  if (!roles.length) {
    view.replaceChildren(el("div", { class: "stack" }, head,
      emptyState("role", el("p", {}, "No roles yet."),
        el("p", { class: "muted small" }, "Interviewing several people for the same job? Create a role, approve its questions, then add each candidate."),
        el("a", { class: "btn primary gap-top", href: "#/roles/new" }, "Create the first role"))));
    return;
  }
  const none = el("div", { hidden: true }, emptyState("search", el("p", {}, "No roles match your search.")));
  const rows = roles.map((r) => linkRow(`#/r/${r.id}`,
          el("td", {}, el("div", { class: "person" }, el("span", { class: "avatar", "aria-hidden": "true" }, icon("role")),
            el("div", {}, el("strong", {}, r.role_title, " ", demoTag(r.demo)), r.level ? el("div", { class: "muted small" }, r.level) : null))),
          el("td", { class: "hide-sm" }, TYPE_LABEL[r.interview_type] || r.interview_type),
          el("td", {}, r.questions ? `${r.questions} ` : empty("None yet"), r.questions ? pill(r.approved ? "ready" : "draft", r.approved ? "approved" : "draft") : null),
          el("td", {}, String(r.candidates)),
          el("td", { class: "hide-sm muted small" }, fmtTime(r.updated_at))));
  const table = el("div", { class: "panel table-wrap" },
    el("table", { class: "list" },
      el("thead", {}, el("tr", {}, ["Role", "Type", "Questions", "Candidates", "Updated"].map((h, n) => el("th", { class: [1, 4].includes(n) ? "hide-sm" : null }, h)))),
      el("tbody", {}, rows)));
  setPageSearch({ placeholder: "Search roles", onSearch: (q) => {
    const needle = q.trim().toLowerCase();
    let shown = 0;
    roles.forEach((r, n) => {
      const hit = !needle || `${r.role_title} ${r.level || ""}`.toLowerCase().includes(needle);
      rows[n].hidden = !hit;
      shown += hit ? 1 : 0;
    });
    table.hidden = shown === 0;
    none.hidden = shown !== 0;
  } });
  view.replaceChildren(el("div", { class: "stack" }, head, table, none));
}

function renderNewRole() {
  view.replaceChildren(el("div", { class: "stack" },
    pageHead({ backTo: back("#/roles", "Roles"), title: "Create a role" }),
    setupForm(null, "Create role and generate questions", async (body, setLabel) => {
      const created = await api("/api/roles", { method: "POST", body });
      setLabel("Generating questions…");
      try { await api(`/api/roles/${created.id}/plan/generate`, { method: "POST" }); } catch { /* shown on the next page */ }
      location.hash = `#/r/${created.id}/setup?step=questions`;
    }, "role")));
}

const ROLE_TABS = [["candidates", "Candidates"], ["report", "Role report"], ["setup", "Setup"], ["job", "Job"]];

async function renderRole(id, tab = "candidates") {
  const [data, pipe] = await Promise.all([api(`/api/roles/${id}`), api(`/api/roles/${id}/pipeline`)]);
  const r = data.role;
  const approved = Boolean(r.plan_approved_at);
  const reload = (next = current) => renderRole(id, next);
  if (tab === "hiring" || tab === "questions") tab = "setup";  // older links
  let current = ROLE_TABS.some(([k]) => k === tab) ? tab : "candidates";
  const s = pipe.summary;
  const body = el("div", { class: "stack" });
  const tabs = el("div", { class: "seg", role: "tablist", "aria-label": "Role sections" });
  const draw = () => {
    tabs.replaceChildren(...ROLE_TABS.map(([k, label]) => el("button", { type: "button", class: "seg-btn", role: "tab",
      "aria-selected": String(k === current), "aria-pressed": String(k === current),
      onclick: () => { current = k; history.replaceState(null, "", `#/r/${id}?tab=${k}`); draw(); } }, label)));
    body.replaceChildren(
      current === "candidates" ? pipelinePanel(r, pipe, reload)
        : current === "report" ? roleReport(r, pipe.ranking)
          : current === "setup" ? roleSetupTab(r, pipe, reload)
            : el("div", { class: "stack" }, roleSetupPanel(r), roleDangerPanel(r, data.candidates.length + s.applied)));
  };
  draw();
  const status = r.demo ? demoTag(true) : pipe.open ? pill("ready", "Accepting applications") : pill("draft", r.hiring.published ? "Applications closed" : "Not published");
  const metric = (label, value) => el("div", { class: "metric" }, el("b", {}, String(value)), label);
  view.replaceChildren(el("div", { class: "stack" },
    pageHead({ backTo: back("#/roles", "Roles"), title: r.setup.role_title + (r.setup.level ? ` · ${r.setup.level}` : ""),
      sub: `${TYPE_LABEL[r.setup.interview_type]} · ${r.plan ? r.plan.questions.length : r.setup.question_count} questions · ~${r.setup.duration_minutes} min · pass mark ${r.hiring.passing_score}/100`,
      right: el("div", { class: "row" }, status, approved ? null : pill("draft", "questions not approved")) }),
    el("section", { class: "panel" }, el("div", { class: "metrics pipeline-metrics" },
      metric("applied", s.applied), metric("to review", s.to_review), metric("invited", s.invited), metric("awaiting interview", s.awaiting),
      metric("interviewed", s.completed), metric("passed", s.passed), metric("did not pass", s.failed), s.incomplete ? metric("need more evidence", s.incomplete) : null)),
    !r.hiring.published && !r.demo ? el("div", { class: "notice info spread" },
      el("span", {}, el("strong", {}, approved ? "Not published yet. " : "Set up the interview process. "),
        approved ? "Review the setup and publish to start taking applications." : "Review and approve the questions, then choose how candidates are invited and interviewed."),
      el("a", { class: "btn small primary", href: `#/r/${id}/setup?step=${approved ? "review" : "questions"}` }, approved ? "Review and publish" : "Continue setup")) : null,
    tabs, body));
}

function copyButton(text, label) {
  const b = el("button", { class: "btn", type: "button", onclick: async () => {
    try { await navigator.clipboard.writeText(text); toast("Link copied.", "good", 2500); } catch { prompt("Copy the link:", text); }
  } }, label);
  return b;
}

const PIPELINE_FILTERS = [
  ["all", "All", () => true],
  ["review", "To review", (c) => ["needs_review", "not_eligible", "eligible", "screening"].includes(c.stage)],
  ["invited", "Invited", (c) => ["invited", "invitation_expired", "scheduled", "in_progress", "ready", "interview_failed"].includes(c.stage)],
  ["interviewed", "Interviewed", (c) => ["passed", "failed", "incomplete", "evaluation_pending", "evaluation_failed"].includes(c.stage)],
  ["passed", "Passed", (c) => c.stage === "passed"],
  ["failed", "Did not pass", (c) => c.stage === "failed"],
];
const STAGE_KIND = { passed: "ready", failed: "no", not_eligible: "no", invitation_expired: "warn", evaluation_failed: "failed",
  interview_failed: "failed", needs_review: "scheduled", screening: "scheduled", in_progress: "in_progress", evaluation_pending: "scheduled", incomplete: "scheduled",
  invited: "invitation_sent", eligible: "ready", ready: "draft", scheduled: "scheduled", cancelled: "cancelled" };

function pipelinePanel(r, pipe, reload) {
  const panel = el("section", { class: "panel stack" });
  if (!pipe.candidates.length) {
    panel.append(emptyState("users", el("p", {}, r.hiring.published ? "No applications yet." : "No candidates yet."),
      r.plan_approved_at ? null : el("p", { class: "muted small" }, "Approve the questions first.")), addCandidatePanel(r, Boolean(r.plan_approved_at)));
    return panel;
  }
  let filter = "all";
  const filters = el("div", { class: "seg", role: "group", "aria-label": "Filter candidates" });
  const tableBox = el("div", { class: "table-wrap" });
  const action = (c) => {
    if (c.application_id && !c.interview_id) return el("button", { class: "btn small", type: "button", onclick: (e) => { e.stopPropagation(); applicationDialog(c.application_id, reload); } }, "Review");
    if (c.stage === "invited" && c.invitation_undelivered) {
      const b = el("button", { class: "btn small", type: "button", onclick: async (e) => {
        e.stopPropagation();
        const done = busy(b, "Sending…");
        try { await api(`/api/interviews/${c.interview_id}/invite`, { method: "POST", body: {} }); toast("Invitation sent.", "good"); reload(); }
        catch (err) { toast(err.message, "bad", 9000); done(); }
      } }, "Send again");
      return b;
    }
    if (["invitation_expired", "interview_failed"].includes(c.stage)) {
      return el("button", { class: "btn small", type: "button", onclick: async (e) => {
        e.stopPropagation();
        try { await api(`/api/interviews/${c.interview_id}/invitation/resend`, { method: "POST", body: {} }); toast("New link sent.", "good"); reload(); } catch (err) { toast(err.message); }
      } }, "Send new link");
    }
    return null;
  };
  const target = (c) => (c.rank != null && c.session_id ? `#/s/${c.session_id}` : c.interview_id ? `#/i/${c.interview_id}` : null);
  const drawTable = () => {
    const test = PIPELINE_FILTERS.find(([k]) => k === filter)[2];
    const rows = pipe.candidates.filter(test);
    filters.replaceChildren(...PIPELINE_FILTERS.map(([k, label, f]) => el("button", { type: "button", class: "seg-btn", "aria-pressed": String(k === filter),
      onclick: () => { filter = k; drawTable(); } }, `${label} ${pipe.candidates.filter(f).length}`)));
    tableBox.replaceChildren(rows.length ? el("table", { class: "list" },
      el("thead", {}, el("tr", {}, ["Rank", "Candidate", "Screening", "Interview score", "Result", "Stage", ""].map((h, n) => el("th", { class: [0, 3].includes(n) ? "hide-sm" : null }, h)))),
      el("tbody", {}, rows.map((c) => {
        const cells = [
          el("td", { class: "num hide-sm" }, c.rank != null ? el("span", { class: "rank" }, `#${c.rank}`) : empty("-", "Not ranked")),
          el("td", {}, el("div", { class: "person" }, el("span", { class: "avatar", "aria-hidden": "true" }, initials(c.candidate_name)),
            el("div", {}, el("strong", {}, c.candidate_name, " ", demoTag(c.demo)), c.applied_at ? el("div", { class: "muted small" }, `Applied ${fmtTime(c.applied_at)}`) : null))),
          el("td", { class: "num" }, c.screening_failed ? pill("failed", "Failed") : c.screening_score != null ? `${c.screening_score}` : empty("-", "Not screened")),
          el("td", { class: "num hide-sm" }, c.score != null ? el("strong", {}, `${c.score}`) : empty("-", "No score")),
          el("td", {}, c.passed == null ? empty("-", c.stage === "incomplete" ? "Not every competency was assessed" : "No result") : pill(c.passed ? "ready" : "no", c.passed ? "Pass" : "Fail")),
          el("td", {}, c.stage === "invited" && c.invitation_undelivered ? pill("warn", "Invitation not delivered") : pill(STAGE_KIND[c.stage] || "draft", pipe.stages[c.stage] || c.stage),
            c.stage === "invited" && c.invitation_undelivered && c.invitation_email ? el("div", { class: "muted small clamp-2", title: c.invitation_email }, c.invitation_email.replace(/^failed: /, ""))
              : c.stage === "invited" && c.invitation_expires_at ? el("div", { class: "muted small" }, `Until ${fmtTime(c.invitation_expires_at)}`) : null,
            c.reason && !c.rank && c.stage.startsWith("evaluation") ? el("div", { class: "muted small clamp-2", title: c.reason }, c.reason) : null),
          el("td", { class: "actions" }, action(c)),
        ];
        const href = target(c);
        if (href) return linkRow(href, ...cells);
        const row = el("tr", { class: "clickable", tabindex: "0", onclick: () => applicationDialog(c.application_id, reload),
          onkeydown: (e) => { if (e.key === "Enter") applicationDialog(c.application_id, reload); } }, ...cells);
        return row;
      }))) : el("p", { class: "muted flush" }, "No candidates in this group."));
  };
  drawTable();
  panel.append(el("div", { class: "spread" }, filters,
    el("details", { class: "inline-add" }, el("summary", { class: "btn small" }, icon("plus"), "Add a candidate"), addCandidatePanel(r, Boolean(r.plan_approved_at)))),
  tableBox);
  return panel;
}

const VERDICT_KIND = { met: "ready", not_met: "no", unclear: "scheduled" };
const VERDICT_TEXT = { met: "Met", not_met: "Not met", unclear: "Unclear" };
const SCREEN_LABEL = { eligible: "Eligible", needs_review: "Needs review", not_eligible: "Not eligible", screening: "Screening", invited: "Invited", withdrawn: "Withdrawn" };

async function applicationDialog(applicationId, reload) {
  let data;
  try { data = await api(`/api/applications/${applicationId}`); } catch (err) { toast(err.message); return; }
  const a = data.application;
  const c = a.candidate;
  const sc = a.screening;
  const crit = Object.fromEntries(data.criteria.map((x) => [x.id, x]));
  const close = el("button", { class: "btn", type: "button" }, "Close");
  const act = (action, label, kind = "") => {
    const b = el("button", { class: `btn ${kind}`, type: "button", onclick: async () => {
      const done = busy(b, "…");
      try {
        const r = await api(`/api/applications/${applicationId}/decision`, { method: "POST", body: { action } });
        dlg.close();
        if (action === "invite" && r.invitation_email && r.invitation_email !== "sent") {
          toast(`Invited, but the email with the link wasn't delivered: ${r.invitation_email} It's tried again in the background, or use Send again.`, "warn", 12000);
        } else toast({ invite: "Invited.", reject: "Marked not eligible.", withdraw: "Marked as withdrawn." }[action] || "Screened again.", "good");
        reload();
      } catch (err) { toast(err.message); done(); }
    } }, label);
    return b;
  };
  const facts = [["Email", c.email], ["Phone", c.phone], ["Location", c.location], ["Current role", c.current_role],
    ["Experience", c.years_experience != null ? `${c.years_experience} years` : ""], ["Education", c.education], ["Links", c.links]].filter(([, v]) => v);
  const dlg = el("dialog", { class: "modal wide", "aria-labelledby": "app-title" },
    el("div", { class: "spread" }, el("h2", { id: "app-title" }, c.full_name), pill(VERDICT_KIND[{ eligible: "met", not_eligible: "not_met" }[a.status]] || "scheduled", SCREEN_LABEL[a.status] || a.status)),
    el("dl", { class: "facts" }, facts.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])),
    sc ? el("div", { class: "stack" },
      el("div", { class: "spread" }, el("h3", { class: "flush" }, "AI screening"),
        sc.error ? pill("failed", "Screening failed") : sc.score != null ? el("span", { class: "row" }, el("strong", {}, `${sc.score}/100`),
          ...Object.entries(sc.breakdown || {}).map(([k, v]) => el("span", { class: "muted small" }, `${k} ${v}`))) : null),
      el("p", { class: "muted small flush" }, "A recommendation from the application's own words. You make the decision."),
      sc.reasons.length ? el("p", { class: "flush" }, sc.reasons.join(" ")) : null,
      (sc.missing || []).length ? el("p", { class: "small flush" }, el("strong", {}, "Not shown in the application: "), sc.missing.join("; ")) : null,
      sc.results.length ? el("ul", { class: "plain screening" }, sc.results.map((x) => el("li", {},
        el("div", { class: "spread" }, el("span", {}, (crit[x.criterion_id] || {}).text || x.criterion_id,
          crit[x.criterion_id] && !crit[x.criterion_id].mandatory ? el("span", { class: "muted small" }, " · preferred") : null),
        pill(VERDICT_KIND[x.verdict], VERDICT_TEXT[x.verdict])),
        x.evidence ? el("p", { class: "small flush" }, `“${x.evidence}”`) : null,
        x.reasoning ? el("p", { class: "muted small flush" }, x.reasoning) : null))) : null,
      sc.summary ? el("p", { class: "small" }, sc.summary) : null,
      el("p", { class: "muted small flush" }, `Screened by ${sc.screened_by} · ${fmtTime(sc.screened_at)}`, sc.error ? ` · ${sc.error}` : ""),
      a.reviewed_by ? el("p", { class: "muted small flush" }, `Reviewed by ${a.reviewed_by}${a.review_note ? `: ${a.review_note}` : ""}`) : null)
      : el("p", { class: "muted" }, "Being screened…"),
    c.cover_note ? el("div", {}, el("h3", {}, "Note from the candidate"), el("p", { class: "jd" }, c.cover_note)) : null,
    (a.answers || []).length ? el("div", { class: "stack" }, el("h3", { class: "flush" }, "Application questions"),
      el("dl", { class: "facts" }, a.answers.flatMap((x) => [el("dt", {}, x.question), el("dd", {}, x.answer || empty("-", "Not answered"))]))) : null,
    sourceFacts(a),
    notesBox(a, applicationId),
    el("details", {}, el("summary", {}, `Resume${a.resume_filename ? ` (${a.resume_filename})` : ""}`),
      // The original file, when it was kept (applications from 0.13.5 on; audit M1). A download, never shown inline.
      data.resume_file ? el("p", { class: "flush" }, el("a", { class: "btn small", href: `/api/applications/${applicationId}/resume`,
        download: "" }, icon("doc"), "Download the original file")) : null,
      el("div", { class: "jd resume-text" }, a.resume_text || "No readable text.")),
    el("div", { class: "row end" }, close,
      a.interview_id ? el("a", { class: "btn primary", href: `#/i/${a.interview_id}` }, "Open interview") : [
        act("rescreen", "Screen again"),
        a.status !== "withdrawn" ? act("withdraw", "Withdrawn") : null,
        a.status !== "not_eligible" ? act("reject", "Not eligible", "danger") : null,
        act("invite", "Invite to interview", "primary")]));
  close.addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
}

const SOURCE_LABEL = { apply_page: "Recruence application page (former)", careers_site: "Careers website", recruiter: "Added by a recruiter", ats: "External ATS" };
const SYNC_KIND = { synced: "ready", failed: "failed", pending: "scheduled", not_needed: "draft" };
const SYNC_LABEL = { synced: "Result sent to the ATS", failed: "Sending to the ATS failed", pending: "Sending to the ATS", not_needed: "Nothing to send yet" };

function sourceFacts(a) {
  const x = a.external;
  const rows = [["Source", SOURCE_LABEL[a.source] || a.source]];
  if (x) {
    rows.push(["ATS", x.provider], ["ATS application id", x.application_id], ["ATS candidate id", x.candidate_id], ["ATS job id", x.job_id]);
  }
  return el("div", { class: "stack" }, el("h3", { class: "flush" }, "Source"),
    el("dl", { class: "facts" }, rows.filter(([, v]) => v).flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]),
      x && x.url && /^https:\/\//i.test(x.url) ? [el("dt", {}, "In the ATS"), el("dd", {}, el("a", { href: x.url, target: "_blank", rel: "noopener noreferrer" }, "Open the record"))] : null,
      a.sync ? [el("dt", {}, "Sync"), el("dd", {}, pill(SYNC_KIND[a.sync.status] || "draft", SYNC_LABEL[a.sync.status] || a.sync.status),
        a.sync.message ? el("span", { class: "muted small" }, ` ${a.sync.message}`) : null, a.sync.at ? el("span", { class: "muted small" }, ` · ${fmtTime(a.sync.at)}`) : null)] : null));
}

function notesBox(a, applicationId) {
  const list = el("ul", { class: "plain notes" });
  const draw = (notes) => list.replaceChildren(...(notes.length ? notes.slice().reverse().map((n) => el("li", {},
    el("p", { class: "flush" }, n.text), el("p", { class: "muted small flush" }, `${n.by || "recruiter"} · ${fmtTime(n.at)}`))) : [el("li", { class: "muted small" }, "No notes yet.")]));
  draw(a.notes || []);
  if (a.demo) return el("div", { class: "stack" }, el("h3", { class: "flush" }, "Recruiter notes"), list);
  const text = el("textarea", { rows: 2, maxlength: 4000, placeholder: "A note for the hiring team (not shown to the candidate)", "aria-label": "New note" });
  const add = el("button", { class: "btn small self-start", type: "button", onclick: async () => {
    if (!text.value.trim()) return;
    const done = busy(add, "Saving…");
    try { const r = await api(`/api/applications/${applicationId}/notes`, { method: "POST", body: { text: text.value.trim() } }); text.value = ""; draw(r.application.notes); done(); } catch (err) { toast(err.message); done(); }
  } }, icon("plus"), "Add note");
  return el("div", { class: "stack" }, el("h3", { class: "flush" }, "Recruiter notes"), list, text, add);
}

// Candidates apply on the company's own website; Recruence only gives it the job's API address.
function careersConnection(c) {
  const row = (label, value) => el("label", { class: "field" }, label,
    el("div", { class: "copy-row" }, el("input", { type: "text", readonly: true, value, "aria-label": label }), copyButton(value, "Copy")));
  return el("div", { class: "stack" },
    el("h3", { class: "flush" }, "Your careers website"),
    el("p", { class: "muted small flush" }, "Your careers site reads this job and posts applications to the address below."),
    row("Application API", c.api_url), row("Job key", c.job_key),
    el("p", { class: "muted small flush" }, c.allowed_websites.length
      ? `Websites whose visitors' browsers may send applications here: ${c.allowed_websites.join(", ")}.`
      : "If visitors' browsers send applications straight here, allow your site's address in Settings → MeetStream. A careers site whose own server sends them, to this job or to the one address in Settings → Recruitment sources, needs nothing more."));
}

// Under every field that takes a meeting link: how to make a Google Meet link that keeps working for interviews.
// Facts from Google's help (read 2026-11-10): Calendar meeting codes expire once unused for 60 days with no future
// event; meet.google.com codes 365 days after last use; with a personal Google account only the meeting's creator can
// admit people who knock.
const MEET_HELP = { start: "https://support.google.com/meet/answer/9302870", codes: "https://support.google.com/meet/answer/10710509" };

function meetGuide(kind = "room") {
  const ext = (href, text) => el("a", { href, target: "_blank", rel: "noopener noreferrer" }, text);
  return el("details", { class: "meet-guide" },
    el("summary", {}, "How to make a Google Meet link for interviews"),
    el("ol", { class: "small stack-tight" },
      kind === "room"
        ? el("li", {}, el("strong", {}, "One link for the whole hiring period. "),
          "In Google Calendar, create an event such as “Interviews: ", el("em", {}, "role"), "”, click ", el("strong", {}, "Add Google Meet video conferencing"),
          ", and set it to repeat (daily or on weekdays) until hiring ends. A Calendar meeting link keeps working while it has upcoming events.")
        : el("li", {}, el("strong", {}, "A link for this candidate. "),
          "In Google Calendar, create an event at the interview time and click ", el("strong", {}, "Add Google Meet video conferencing"),
          ". Or on meet.google.com: ", el("strong", {}, "New meeting → Create a meeting for later"), "."),
      el("li", {}, "Copy the meeting link (meet.google.com/xxx-xxxx-xxx) and paste it here. Don't use a link from a meeting you made up or one you were sent for something else."),
      el("li", {}, el("strong", {}, "Who lets people in. "), "The candidate and the AI interviewer join as guests. In the meeting's ",
        el("strong", {}, "Host controls"), ", set ", el("strong", {}, "Meeting access"), " to ", el("strong", {}, "Open"),
        " if your account offers it, so no one has to knock. Otherwise someone has to be in the call to admit them: with a personal Google account, only the person who created the meeting can."),
      el("li", {}, "Test it: open the link in a private window and check it doesn't say the meeting code expired.")),
    el("p", { class: "muted small flush" }, "Google's help: ", ext(MEET_HELP.start, "start or schedule a Meet meeting, and host controls"), " · ",
      ext(MEET_HELP.codes, "how long meeting codes last"), "."));
}

// ---------- a role's interview process, set up one decision at a time ----------
//
// Questions → Screening (automatic, or manual approval) → Interview mode (AI-led or human-led) → Settings for those
// choices → Review and publish. Each step saves when you continue; the role page's Setup tab summarises the result
// and links back to each step.

const ROLE_STEPS = [["questions", "Questions"], ["assignment", "Screening"], ["mode", "Interview mode"], ["settings", "Settings"], ["review", "Review"]];
const ROLE_FORMATS = { voice: "Voice", video: "Video", proctored: "Proctored" };

// The choices, in words, for the review step and the role's Setup tab.
function roleChoices(r) {
  const h = r.hiring;
  const o = h.options || {};
  const human = o.mode === "analyst";
  return {
    questions: r.plan ? `${r.plan.questions.length} question${r.plan.questions.length === 1 ? "" : "s"}, ${r.plan_approved_at ? "approved" : "not approved yet"}` : "Not generated yet",
    assignment: h.auto_invite
      ? `Automatic: Recruence's AI reviews each resume and eligible applicants are invited as soon as they're screened${h.screening_threshold ? `, if they score ${h.screening_threshold}/100 or more` : ""}. The rest wait for you.`
      : "Manual: Recruence's AI suggests, and you approve each application before anyone is invited.",
    mode: human ? "Human-led: a member of your team interviews; a silent assistant transcribes and the AI summarises."
      : `AI-led: the AI interviewer asks the questions by voice. Format: ${ROLE_FORMATS[o.format || "voice"]}.`,
    settings: [
      h.meeting_link ? `${human ? "Meeting link" : "Interview room"}: ${h.meeting_link}` : `${human ? "Meeting link" : "Interview room"}: not set`,
      human ? `Interview links stay valid for ${h.invitation_days} days` : `${h.invitation_days} days to take the interview`,
      `Pass mark ${h.passing_score}/100`,
      `${(h.criteria || []).length} eligibility requirement${(h.criteria || []).length === 1 ? "" : "s"}`,
      `${(h.questions || []).length} application question${(h.questions || []).length === 1 ? "" : "s"}`,
      h.application_deadline ? `Applications close ${fmtTime(h.application_deadline)}` : "No application deadline",
    ],
  };
}

// The setup at a glance, with a way back to each step.
function roleSetupSummary(r, { edit = true } = {}) {
  const c = roleChoices(r);
  const row = (step, label, value) => el("div", { class: "setup-row" },
    el("span", {}, el("strong", {}, label), Array.isArray(value)
      ? el("ul", { class: "plain small muted flush" }, value.map((v) => el("li", {}, v)))
      : el("span", { class: "muted small block" }, value)),
    edit && !r.demo ? el("a", { class: "btn small", href: `#/r/${r.id}/setup?step=${step}` }, "Edit") : null);
  return el("div", { class: "setup-summary" },
    row("questions", "Questions", c.questions), row("assignment", "Screening", c.assignment),
    row("mode", "Interview mode", c.mode), row("settings", "Settings", c.settings));
}

async function renderRoleSetup(id, step = "questions") {
  const [data, pipe] = await Promise.all([api(`/api/roles/${id}`), api(`/api/roles/${id}/pipeline`)]);
  const r = data.role;
  const h = r.hiring;
  const approved = Boolean(r.plan_approved_at);
  if (!ROLE_STEPS.some(([k]) => k === step)) step = "questions";
  if (!approved && step !== "questions") step = "questions";  // the rest builds on approved questions
  const index = ROLE_STEPS.findIndex(([k]) => k === step);
  const go = (k) => { location.hash = `#/r/${id}/setup?step=${k}`; };
  const error = notice("bad"); error.hidden = true;
  const save = async (button, body, next) => {
    error.hidden = true;
    const done = busy(button, "Saving…");
    try { await api(`/api/roles/${id}/hiring`, { method: "PUT", body }); go(next); }
    catch (err) { error.textContent = err.message; error.hidden = false; done(); }
  };
  const backBtn = index > 0 ? el("button", { class: "btn", type: "button", onclick: () => go(ROLE_STEPS[index - 1][0]) }, "Back")
    : el("a", { class: "btn", href: `#/r/${id}` }, "Back to the role");
  const stepper = el("ol", { class: "setup-steps five", "aria-label": "Interview setup steps" }, ROLE_STEPS.map(([k, label], n) => {
    const state = k === step ? "current" : n < index || (approved && k === "questions") ? "done" : "";
    return el("li", { class: state, "aria-current": k === step ? "step" : null },
      el("button", { type: "button", disabled: !approved && k !== "questions", onclick: () => go(k) },
        el("span", { class: "dot" }, state === "done" ? icon("check") : String(n + 1)), el("span", { class: "label" }, label)));
  }));
  const card = (title, lead, ...children) => el("section", { class: "panel stack" },
    el("div", {}, el("h2", { class: "flush" }, title), lead ? el("p", { class: "muted small flush-top" }, lead) : null), ...children);
  const choice = (name, value, checked, title, text) => {
    const input = el("input", { type: "radio", name, value, checked });
    return { input, node: el("label", { class: "mode-card" }, input, el("span", {}, el("strong", {}, title), el("span", { class: "muted small" }, text))) };
  };
  let body;

  if (step === "questions") {
    const next = el("button", { class: "btn primary", type: "button", disabled: !approved, onclick: () => go("assignment") }, "Continue");
    body = [
      planPanel(r, false, { base: `/api/roles/${id}`, reload: () => renderRoleSetup(id, "questions"), title: "Review the interview questions" }),
      approved ? null : el("p", { class: "muted small flush" }, "Edit, add or remove questions, save, then approve them to continue. Every candidate for this role gets these questions."),
      setupFooter(backBtn, next)];
  } else if (step === "assignment") {
    const review = h.resume_review || (h.auto_invite ? "auto" : "manual");
    const auto = choice("review", "auto", review === "auto", "Automatic",
      "Recruence's AI reviews each resume against the requirements. Eligible applicants are invited straight away; the rest wait for you.");
    const manual = choice("review", "manual", review === "manual", "Manual approval",
      "Recruence's AI reviews each resume as a suggestion, and you approve each application before anyone is invited.");
    const threshold = el("input", { type: "number", min: 0, max: 100, value: h.screening_threshold || 0, "aria-label": "Screening score needed" });
    const thresholdBox = el("label", { class: "field" }, el("span", {}, "Screening score needed ", el("span", { class: "hint" }, "(0-100; below it, the application waits for you. 0: eligibility alone decides)")), threshold);
    const sync = () => { thresholdBox.hidden = manual.input.checked; };
    [auto.input, manual.input].forEach((x) => x.addEventListener("change", sync));
    sync();
    const next = el("button", { class: "btn primary", type: "button" }, "Continue");
    next.addEventListener("click", () => {
      const chosen = manual.input.checked ? "manual" : "auto";
      save(next, { resume_review: chosen, screening_threshold: Number(threshold.value) || 0 }, "mode");
    });
    body = [card("How are candidates reviewed?", "Choose who decides which applicants are invited to the interview.",
      el("div", { class: "mode-cards" }, auto.node, manual.node), thresholdBox, error), setupFooter(backBtn, next)];
  } else if (step === "mode") {
    const o = h.options || {};
    const ai = choice("mode", "ai", o.mode !== "analyst", "AI-led", "The AI interviewer asks the approved questions and follow-ups by voice. Candidates start it from their link whenever suits them, within the time allowed.");
    const human = choice("mode", "analyst", o.mode === "analyst", "Human-led", "A member of your team interviews at a time you arrange. A silent assistant records and transcribes the call, and the AI summarises it against the same questions.");
    const formats = Object.fromEntries(Object.entries(ROLE_FORMATS).map(([k, label]) => [k, choice("format", k, (o.format || "voice") === k, label, FORMAT_INFO[k][1])]));
    const formatBox = el("div", { class: "field" }, el("span", {}, "Format"), el("div", { class: "mode-cards three" }, Object.values(formats).map((f) => f.node)));
    const sync = () => {
      const isHuman = human.input.checked;
      if (isHuman && formats.proctored.input.checked) formats.video.input.checked = true;  // proctoring needs the AI to ask
      formats.proctored.input.disabled = isHuman;
      formats.proctored.node.classList.toggle("disabled", isHuman);
    };
    const inChat = el("input", { type: "checkbox", checked: o.questions_in_chat !== false });
    const chatBox = el("label", { class: "check" }, inChat,
      el("span", {}, el("strong", {}, "Show each question in the meeting chat"), " while the AI asks it: the same words, numbered, so the candidate can read what they hear. ",
        el("span", { class: "muted" }, "Google Meet and Teams; not on Zoom, where MeetStream repeats chat messages.")));
    const syncChat = () => { chatBox.hidden = human.input.checked; };
    [ai.input, human.input].forEach((x) => x.addEventListener("change", () => { sync(); syncChat(); }));
    sync();
    syncChat();
    const next = el("button", { class: "btn primary", type: "button" }, "Continue");
    next.addEventListener("click", () => save(next, { options: {
      mode: human.input.checked ? "analyst" : "ai",
      format: Object.keys(formats).find((k) => formats[k].input.checked) || "voice",
      questions_in_chat: !human.input.checked && inChat.checked } }, "settings"));
    body = [card("Who leads the interview?", null, el("div", { class: "mode-cards" }, ai.node, human.node), formatBox, chatBox, error), setupFooter(backBtn, next)];
  } else if (step === "settings") {
    const human = (h.options || {}).mode === "analyst";
    const room = el("input", { type: "url", value: h.meeting_link, placeholder: "https://meet.google.com/abc-defg-hij", "aria-label": "Meeting link" });
    const days = el("input", { type: "number", min: 1, max: 60, value: h.invitation_days, "aria-label": "Days" });
    const pass = el("input", { type: "number", min: 0, max: 100, value: h.passing_score, "aria-label": "Pass mark out of 100" });
    const deadline = timePicker({ label: "Application deadline", optional: true, emptyText: "No deadline." });
    if (h.application_deadline) deadline.set(h.application_deadline);
    const rows = (items, make, max) => {
      const list = el("div", { class: "stack criteria" });
      const add = (x) => { const row = make(x, () => row.remove()); list.append(row); return row; };
      items.forEach(add);
      return { list, add, read: () => [...list.children].map((row) => row.read()).filter((x) => x.text), max };
    };
    const reqs = rows(h.criteria || [], (cr = { text: "", mandatory: true, kind: "skill" }, remove) => {
      const text = el("input", { type: "text", value: cr.text, maxlength: 300, placeholder: "e.g. Degree in computer science or equivalent", "aria-label": "Requirement" });
      const kind = el("select", { "aria-label": "Mandatory or preferred" }, el("option", { value: "1", selected: cr.mandatory }, "Mandatory"), el("option", { value: "0", selected: !cr.mandatory }, "Preferred"));
      const area = el("select", { "aria-label": "Kind of requirement" }, [["skill", "Skill"], ["experience", "Experience"], ["education", "Education"], ["other", "Other"]]
        .map(([v, l]) => el("option", { value: v, selected: (cr.kind || "other") === v }, l)));
      const row = el("div", { class: "criterion" }, text, area, kind, el("button", { class: "btn small", type: "button", onclick: remove }, "Remove"));
      row.read = () => ({ text: text.value.trim(), mandatory: kind.value === "1", kind: area.value });
      return row;
    });
    const asks = rows(h.questions || [], (q = { text: "", required: false }, remove) => {
      const text = el("input", { type: "text", value: q.text, maxlength: 300, placeholder: "e.g. Are you authorised to work in Germany?", "aria-label": "Application question" });
      const req = el("select", { "aria-label": "Required or optional" }, el("option", { value: "1", selected: q.required }, "Required"), el("option", { value: "0", selected: !q.required }, "Optional"));
      const row = el("div", { class: "criterion" }, text, req, el("button", { class: "btn small", type: "button", onclick: remove }, "Remove"));
      row.read = () => ({ text: text.value.trim(), required: req.value === "1" });
      return row;
    });
    const job = h.job || {};
    const jobInput = (key, label, placeholder) => { const i = el("input", { type: "text", value: job[key] || "", maxlength: key === "location" ? 200 : 60, placeholder }); i.dataset.key = key; return el("label", { class: "field" }, label, i); };
    const jobFields = [jobInput("employment_type", "Employment type", "Full-time"), jobInput("location", "Location", "Chennai, India"), jobInput("work_arrangement", "Work arrangement", "Hybrid")];
    const responsibilities = el("textarea", { rows: 3, maxlength: 4000, placeholder: "What the person will do, one point per line" }, job.responsibilities || "");
    const next = el("button", { class: "btn primary", type: "button" }, "Continue");
    next.addEventListener("click", () => {
      const d = deadline.value();
      if (d.error) { error.textContent = d.error; error.hidden = false; return; }
      const jobBody = { responsibilities: responsibilities.value.trim() };
      jobFields.forEach((f) => { const i = f.querySelector("input"); jobBody[i.dataset.key] = i.value.trim(); });
      save(next, { meeting_link: room.value.trim(), invitation_days: Number(days.value), passing_score: Number(pass.value),
        criteria: reqs.read(), questions: asks.read(), job: jobBody,
        ...(d.at ? { application_deadline: d.at.toISOString() } : { clear_deadline: true }) }, "review");
    });
    body = [
      card("The interview", human ? "Your interview room: the candidate's link shows it at the time you arrange. You can set another link for one candidate on their interview page."
        : "One meeting room that candidates join; the AI interviewer joins them there. On Google Meet, guests must be able to join without the host.",
        el("div", { class: "form-grid" },
          el("label", { class: "field" }, human ? "Meeting link" : "Interview room", room),
          el("label", { class: "field" }, human ? "Days the interview link stays valid" : "Days to take the interview", days),
          el("label", { class: "field" }, el("span", {}, "Pass mark ", el("span", { class: "hint" }, "(out of 100)")), pass)),
        meetGuide("room")),
      card("Applications", "What the careers site asks, and what the screening checks.",
        el("div", { class: "field" }, el("span", {}, "Eligibility requirements"), reqs.list,
          el("button", { class: "btn small self-start", type: "button", onclick: () => reqs.add().querySelector("input").focus() }, icon("plus"), "Add a requirement")),
        el("div", { class: "field" }, el("span", {}, "Application questions ", el("span", { class: "hint" }, "(asked on the application form; up to 10)")), asks.list,
          el("button", { class: "btn small self-start", type: "button", onclick: () => { if (asks.list.children.length < 10) asks.add().querySelector("input").focus(); } }, icon("plus"), "Add a question")),
        el("div", { class: "field" }, el("span", {}, "Application deadline"), deadline.node),
        el("details", {}, el("summary", {}, "Job details shown with the job"),
          el("div", { class: "stack gap-top" }, el("div", { class: "form-grid" }, ...jobFields), el("label", { class: "field" }, "Responsibilities", responsibilities)))),
      error, setupFooter(backBtn, next)];
  } else {
    const missing = [!h.meeting_link && "a meeting link (Settings)"].filter(Boolean);
    const publish = el("button", { class: "btn primary", type: "button", disabled: missing.length > 0 }, h.published ? "Done" : "Confirm and publish");
    publish.addEventListener("click", async () => {
      if (h.published) { location.hash = `#/r/${id}`; return; }
      error.hidden = true;
      // The Public URL must be reachable for candidates' invitation links: say so before publishing, not after the
      // first invitation silently isn't sent. (Only admins may run the check; for others it's skipped.)
      const checking = busy(publish, "Checking…");
      const url = await api("/api/settings/check/public_url", { method: "POST", body: {} }).catch(() => null);
      checking();
      if (url && url.ok === false && !(await confirmDialog(`Your Public URL isn't working: ${url.message}. People can still apply, but `
        + "their interview invitations won't be emailed until it works (Settings → MeetStream). Publish anyway?",
        { title: "Public URL not reachable", confirmLabel: "Publish anyway" }))) return;
      const done = busy(publish, "Publishing…");
      try {
        await api(`/api/roles/${id}/hiring`, { method: "PUT", body: { published: true } });
        toast("Published. Your careers site can now send applications for this role.", "good");
        location.hash = `#/r/${id}?tab=setup`;
      } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
    });
    const draft = h.published ? null : el("a", { class: "btn", href: `#/r/${id}` }, "Keep as draft");
    body = [
      card("Review and confirm", h.published ? "This role is published. Changes you made are already saved." : "Check each choice. Publishing lets your careers site send applications for this role.",
        roleSetupSummary(r),
        missing.length ? notice("warn", `Before publishing, add ${missing.join(" and ")}.`) : null,
        pipe.careers ? careersConnection(pipe.careers) : null,
        error),
      setupFooter(backBtn, draft, publish)];
  }

  view.replaceChildren(el("div", { class: "stack" },
    pageHead({ backTo: back(`#/r/${id}`, r.setup.role_title), title: "Set up the interview process",
      sub: `${r.setup.role_title} · step ${index + 1} of ${ROLE_STEPS.length}: ${ROLE_STEPS[index][1]}` }),
    stepper, ...body.filter(Boolean)));
}

// The role page's Setup tab: the choices at a glance, publishing, and the careers site connection.
function roleSetupTab(r, pipe, reload) {
  const h = r.hiring;
  const approved = Boolean(r.plan_approved_at);
  const send = async (button, body, message) => {
    const done = busy(button, "Saving…");
    try { await api(`/api/roles/${r.id}/hiring`, { method: "PUT", body }); toast(message, "good"); reload("setup"); } catch (err) { toast(err.message); done(); }
  };
  const publish = approved && !r.demo ? el("button", { class: `btn ${h.published ? "" : "primary"}`, type: "button" }, h.published ? "Unpublish" : "Publish") : null;
  if (publish) publish.addEventListener("click", () => send(publish, { published: !h.published }, h.published ? "Closed: your careers site can't send applications for it now." : "Published. Your careers site can now send applications for it."));
  const pause = h.published && !r.demo ? el("button", { class: "btn", type: "button" }, h.paused ? "Reopen applications" : "Pause applications") : null;
  if (pause) pause.addEventListener("click", () => send(pause, { paused: !h.paused }, h.paused ? "Applications reopened." : "Applications paused."));
  return el("div", { class: "stack" },
    el("section", { class: "panel stack" },
      el("div", { class: "spread" }, sectionTitle("sliders", "Interview process"),
        r.demo ? null : el("a", { class: "btn small", href: `#/r/${r.id}/setup?step=${approved ? "review" : "questions"}` }, "Walk through the setup")),
      roleSetupSummary(r),
      h.published && h.paused ? notice("warn", "Applications are paused: the job is shown but applications aren't accepted.") : null,
      el("div", { class: "form-actions" }, pause, publish)),
    pipe.careers ? el("section", { class: "panel stack" }, careersConnection(pipe.careers)) : null);
}

function roleReport(r, data) {
  const comps = r.plan ? r.plan.competencies : [];
  const panel = el("section", { class: "panel stack role-report" });
  if (!data.ranked.length) {
    panel.append(el("p", { class: "muted flush" }, "No evaluated interviews yet."), unrankedList(data.unranked));
    return panel;
  }
  const mark = data.passing_score;
  const bars = el("div", { class: "score-bars" }, data.ranked.map((c) => el("div", { class: "score-bar" },
    el("span", { class: "who" }, `#${c.rank} ${c.candidate_name}`),
    el("div", { class: "bar", role: "img", "aria-label": `${c.score} out of 100` }, el("span", { class: c.passed === false ? "below" : "", style: `width:${c.score}%` }), el("i", { class: "mark", style: `left:${mark}%`, title: `Pass mark ${mark}` })),
    el("strong", { class: "num" }, String(c.score)))));
  const compTable = el("div", { class: "table-wrap" }, el("table", { class: "list compare" },
    el("thead", {}, el("tr", {}, ["Candidate", ...comps.map((c) => c.name), "Must-haves met", "AI recommendation"].map((h) => el("th", {}, h)))),
    el("tbody", {}, data.ranked.map((c) => linkRow(`#/s/${c.session_id}`,
      el("td", {}, el("strong", {}, c.candidate_name)),
      comps.map((cp) => {
        const top = (data.competencies.find((x) => x.id === cp.id) || {}).top;
        const v = c.scores[cp.id];
        return el("td", { class: `num${v != null && v === top ? " best" : ""}` }, v != null ? `${v}/5` : empty("-", "No score"));
      }),
      el("td", { class: "num" }, data.requirements.length ? `${c.meets}/${data.requirements.length}` : empty("-", "None set")),
      el("td", {}, el("span", { class: `rec ${c.recommendation} small` }, REC_LABEL[c.recommendation])))))));
  panel.append(
    el("div", { class: "spread" }, el("h3", { class: "flush" }, "Overall score"), el("span", { class: "muted small" }, `Pass mark ${mark}/100 · ${data.passed.length} pass · ${data.failed.length} did not pass${data.incomplete.length ? ` · ${data.incomplete.length} need more evidence` : ""}`)),
    bars,
    el("h3", { class: "flush" }, "Competencies"), compTable,
    el("h3", { class: "flush" }, "Candidates"),
    el("div", { class: "grid2" }, data.ranked.map((c) => el("section", { class: "panel flat stack" },
      el("div", { class: "spread" }, el("a", { href: `#/s/${c.session_id}` }, el("strong", {}, `#${c.rank} ${c.candidate_name}`)),
        el("span", { class: "row" }, el("span", { class: "score-num" }, `${c.score}`), c.passed == null ? pill("scheduled", "Needs more evidence") : pill(c.passed ? "ready" : "no", c.passed ? "Pass" : "Fail"))),
      c.summary ? el("p", { class: "small flush" }, c.summary) : null,
      c.strengths.length ? el("div", {}, el("span", { class: "small muted" }, "Strengths"), el("ul", { class: "plain small" }, c.strengths.map((x) => el("li", {}, x)))) : null,
      c.concerns.length ? el("div", {}, el("span", { class: "small muted" }, "Weaknesses"), el("ul", { class: "plain small" }, c.concerns.map((x) => el("li", {}, x)))) : null))),
    data.further_assessment.length ? el("div", {}, el("h3", {}, "Needs further assessment"),
      el("ul", { class: "plain small" }, data.further_assessment.map((f) => el("li", {}, el("strong", {}, f.candidate_name), " · ",
        [f.requirements.length ? `not shown: ${f.requirements.join("; ")}` : null, f.competencies.length ? `no score for ${f.competencies.join(", ")}` : null, f.low_confidence ? "low evaluation confidence" : null].filter(Boolean).join(" · "))))) : null,
    unrankedList(data.unranked),
    el("p", { class: "muted small flush" }, data.method));
  return panel;
}

function unrankedList(unranked) {
  if (!unranked.length) return null;
  return el("div", {}, el("h3", { class: "small" }, "Not ranked yet"),
    el("ul", { class: "plain small" }, unranked.map((u) => el("li", {}, el("a", { href: `#/i/${u.interview_id}` }, u.candidate_name), el("span", { class: "muted" }, ` · ${u.reason}`)))));
}

function addCandidatePanel(r, approved) {
  const panel = el("section", { class: "panel stack" }, sectionTitle("plus", "Add a candidate"));
  if (!approved) {
    panel.append(el("p", { class: "muted" }, "Approve the questions first, so every candidate gets the same interview."));
    return panel;
  }
  const name = el("input", { type: "text", required: true, maxlength: 200, "aria-label": "Candidate name" });
  const email = el("input", { type: "email", maxlength: 320, "aria-label": "Candidate email" });
  const { resume, field } = resumeField("");
  const error = notice("bad"); error.hidden = true;
  const submit = el("button", { class: "btn primary", type: "submit" }, "Add candidate");
  const form = el("form", { class: "stack" },
    el("div", { class: "form-grid" },
      el("label", { class: "field" }, "Candidate name", name),
      el("label", { class: "field" }, el("span", {}, "Candidate email ", el("span", { class: "hint" }, "(optional; used for the invitation email)")), email)),
    field, error, el("div", { class: "row" }, submit));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    const done = busy(submit, "Adding…");
    try {
      const created = await api(`/api/roles/${r.id}/candidates`, { method: "POST",
        body: { candidate_name: name.value.trim(), candidate_email: email.value.trim(), resume_text: resume.value.trim() } });
      location.hash = `#/i/${created.id}`;
    } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
  });
  panel.append(form);
  return panel;
}

function roleSetupPanel(r) {
  return el("details", { class: "panel" }, el("summary", {}, el("strong", {}, "Job description and criteria")),
    el("p", { class: "muted small" }, "Saving keeps the questions but withdraws their approval; regenerate them to use the new details. Candidates already added keep their questions."),
    setupForm(r.setup, "Save role", async (body) => {
      await api(`/api/roles/${r.id}/setup`, { method: "PUT", body });
      renderRole(r.id);
    }, "role"));
}

function roleDangerPanel(r, candidates) {
  const del = el("button", { class: "btn danger", disabled: candidates > 0, onclick: async () => {
    if (!(await confirmDialog(`Delete the role "${r.setup.role_title}"? This cannot be undone.`, { title: "Delete role", confirmLabel: "Delete role", danger: true }))) return;
    const done = busy(del, "Deleting…");
    try { await api(`/api/roles/${r.id}`, { method: "DELETE" }); toast("Role deleted.", "good"); location.hash = "#/roles"; } catch (err) { toast(err.message); done(); }
  } }, icon("trash"), "Delete role");
  return el("section", { class: "panel spread" },
    el("div", {}, sectionTitle("trash", "Delete"),
      el("p", { class: "muted small" }, candidates ? "Delete the role's candidate interviews first; their data is deleted from each interview's page." : "Deletes the role and its questions.")),
    del);
}

// ---------- settings: what's configured, and checks ----------

// The settings building blocks, shared by the Settings page and the first-run wizard, so both edit the same
// configuration the same way. `redraw` is called after a save changes the configuration.
// Read when an interview starts (app/interview/agent.py), so a change reaches the next interview, not one in progress.
const NEXT_INTERVIEW_KEYS = ["mia_", "max_follow_ups_per_question", "chat_disclosure", "postcall_transcript", "bot_name"];
const EMAIL_KEYS = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "smtp_from", "smtp_starttls", "smtp_ssl", "mail_reply_to"];

// A setting's help, shortened to its first sentence: the rest stays one hover away.
function brief(text) {
  if (!text) return "";
  const cut = text.search(/(?<!\b(?:e\.g|i\.e|etc|vs))[.!?]\s/);
  return cut < 0 ? text : text.slice(0, cut + 1);
}

function makeKit(initialCfg, isAdmin) {
  let cfg = initialCfg;
  let draw = () => {};
  const OPTION_LABELS = {
    mia_mode: { pipeline: "Separate parts (speech-to-text, conversation, voice)", realtime: "One realtime model hears, thinks and speaks (Gemini Live)" },
    mia_realtime_provider: { google: "Google Gemini Live", openai: "OpenAI realtime", xai: "xAI realtime" },
  };
  function fieldRow(f, changes) {
    const id = `cfg-${f.key}`;
    let input;
    const mark = (value) => { changes[f.key] = value; };
    if (f.kind === "bool") {
      input = el("input", { type: "checkbox", id, checked: Boolean(f.value), disabled: !isAdmin, onchange: (e) => mark(e.target.checked) });
    } else if (f.kind === "select") {
      input = el("select", { id, disabled: !isAdmin, onchange: (e) => mark(e.target.value) },
        f.options.map((o) => el("option", { value: o, selected: o === f.value },
          (OPTION_LABELS[f.key] || {})[o] || o || (f.key.startsWith("mia_") ? "From the AI provider" : "None"))));
    } else if (f.kind === "secret") {
      input = el("input", { type: "password", id, autocomplete: "new-password", disabled: !isAdmin,
        placeholder: f.set ? "Set (type to replace)" : "Not set", oninput: (e) => { if (e.target.value) mark(e.target.value); else delete changes[f.key]; } });
    } else {
      const type = f.kind === "int" || f.kind === "float" ? "number" : f.kind === "url" || f.kind === "https" ? "url" : "text";
      input = el("input", { type, id, disabled: !isAdmin, step: f.kind === "float" ? "0.1" : null, min: f.min, max: f.max,
        value: Array.isArray(f.value) ? f.value.join(", ") : f.value ?? "",
        oninput: (e) => mark(f.kind === "int" ? Number(e.target.value) : f.kind === "float" ? Number(e.target.value) : e.target.value) });
      if (f.key === "ollama_model") input.setAttribute("list", "ollama-models");
    }
    // Only what's worth knowing: where a value comes from when it isn't the default, and when a change takes effect
    // when that isn't straight away.
    if (f.source === "server") input.disabled = true;  // a cloud backend's own address (R-08): changed on the server
    const source = f.source === "saved" ? "Saved" : f.source === "env" ? `From .env (${f.env})`
      : f.source === "server" ? `Set on the server (${f.env})` : null;
    const when = f.restart_pending ? "Waiting for restart" : !f.live ? "After restart"
      : NEXT_INTERVIEW_KEYS.some((k) => f.key.startsWith(k)) ? "From the next interview" : null;
    const meta = el("span", { class: "cfg-meta" },
      when ? el("span", { class: `cfg-tag ${f.live ? "live" : "restart"}` }, when) : null,
      source ? el("span", {}, source) : null,
      f.source === "saved" && isAdmin ? el("button", { class: "link small", type: "button", onclick: async () => {
        if (!(await confirmDialog(`Forget the saved value of “${f.label}” and use ${f.env} from .env (or the default)?`, { title: "Reset setting", confirmLabel: "Reset" }))) return;
        try { cfg = await api(`/api/config/${f.key}`, { method: "DELETE" }); toast(`${f.label}: reset.`, "good", 3000); health = await api("/api/health"); draw(); } catch (err) { toast(err.message); }
      } }, "Reset") : null,
      f.kind === "secret" && f.set && isAdmin ? el("button", { class: "link small", type: "button", onclick: () => { mark(""); input.value = ""; input.placeholder = "Will be cleared when you save"; } }, "Clear") : null);
    const error = el("span", { class: "cfg-error", id: `${id}-error`, hidden: true });
    input.setAttribute("aria-describedby", `${id}-error`);
    const short = brief(f.help);
    const hint = short ? el("span", { class: "hint block", title: short !== f.help ? f.help : null }, short) : null;
    if (f.kind === "bool") {
      return el("div", { class: "cfg-row", "data-key": f.key }, el("label", { class: "check" }, input, el("span", {}, el("strong", {}, f.label), hint)), meta, error);
    }
    return el("div", { class: "cfg-row", "data-key": f.key },
      el("label", { class: "field", for: id }, el("span", {}, f.label, hint), input), meta, error);
  }

  const allFields = () => cfg.sections.flatMap((s) => s.fields);
  const fieldOf = (key) => allFields().find((f) => f.key === key);

  // What the page's fields for these settings hold now, saved or not, so a check tests what was typed without
  // saving it or clearing the form. An empty password field stands for the saved password.
  function formValues(keys) {
    const out = {};
    for (const key of keys) {
      const input = document.getElementById(`cfg-${key}`);
      const f = fieldOf(key);
      if (!input || !f) continue;
      out[key] = f.kind === "bool" ? input.checked : f.kind === "int" ? Number(input.value) : input.value;
    }
    return out;
  }

  // Saves `changes` (all or nothing); errors are shown next to their fields inside `form`.
  async function saveChanges(changes, form, status, save) {
    form.querySelectorAll(".cfg-error").forEach((x) => { x.hidden = true; });
    status.classList.remove("bad-text");
    if (!Object.keys(changes).length) { status.textContent = "Nothing changed."; return false; }
    const done = busy(save, "Saving…");
    try {
      let data, res = { ok: true, status: 200 };
      if (setupPreview) {
        try { data = previewApi("/api/config", { method: "PUT", body: { changes } }); } catch (e) { res = { ok: false }; data = { detail: { errors: e.errors || {} } }; if (!e.errors) throw e; }
      } else {
        res = await fetch("/api/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ changes }) });
        data = await res.json();
      }
      if (res.status === 401) { showSignin(); throw new Error("Please sign in"); }
      if (!res.ok) {
        const errors = (data.detail && data.detail.errors) || {};
        for (const [key, msg] of Object.entries(errors)) {
          const slot = form.querySelector(`[data-key="${key}"] .cfg-error`);
          if (slot) { slot.textContent = `${fieldOf(key)?.label || key} ${msg}.`; slot.hidden = false; }
        }
        throw new Error(Object.keys(errors).length ? "Fix the highlighted settings." : data.detail || res.statusText);
      }
      cfg = data;
      if (setupPreview) { toast("Simulated save: nothing was stored.", "good"); draw(); return true; }
      toast(data.restart_required.length ? `Saved. ${data.restart_required.length} change${data.restart_required.length === 1 ? "" : "s"} apply after a restart.` : "Saved and applied.", "good");
      health = await api("/api/health");
      draw();
      return true;
    } catch (err) { status.textContent = err.message; status.classList.add("bad-text"); done(); return false; }
  }

  function sectionForm(section) {
    const changes = {};
    const rows = section.fields.map((f) => fieldRow(f, changes));
    const status = el("p", { class: "small flush", "aria-live": "polite" });
    const save = el("button", { class: "btn primary", type: "submit", disabled: !isAdmin }, "Save changes");
    const form = el("form", { class: "panel stack", novalidate: true }, sectionTitle("sliders", section.title), ...rows,
      el("div", { class: "form-actions" }, save, status, isAdmin ? null : el("span", { class: "muted small" }, "Only admins can change settings.")));
    form.addEventListener("submit", (e) => { e.preventDefault(); saveChanges(changes, form, status, save); });
    return form;
  }

  // AI provider: pick one, configure only its fields, test it, and use it for plans, evaluations and the interview.
  const PROVIDERS = [
    { id: "gemini", name: "Google Gemini", keys: ["gemini_api_key", "gemini_model"], test: ["gemini_api_key", "gemini_model"] },
    { id: "claude", name: "Anthropic Claude", keys: ["anthropic_api_key", "claude_model"], test: ["anthropic_api_key", "claude_model"] },
    { id: "openai", name: "OpenAI or compatible", keys: ["openai_api_key", "openai_model", "openai_base_url"], test: ["openai_api_key", "openai_model", "openai_base_url"] },
    { id: "xai", name: "xAI (Grok)", keys: ["xai_api_key", "xai_model"], test: ["xai_api_key", "xai_model"] },
    { id: "ollama", name: "Ollama (local)", keys: ["ollama_base_url", "ollama_model"], test: [null, "ollama_model", "ollama_base_url"] },
  ];
  let openProvider = null;

  function providerConfigured(pv) {
    if (pv.id === "ollama") return Boolean(fieldOf("ollama_model")?.value);
    if (pv.id === "openai") {
      // A key, or a server on this machine or network (self-hosted servers usually need none).
      const base = fieldOf("openai_base_url")?.value || "";
      return Boolean(fieldOf("openai_api_key")?.set || /^https?:\/\/(localhost|127\.|10\.|192\.168\.|\[::1\])/.test(base));
    }
    return Boolean(fieldOf(pv.keys[0])?.set);
  }

  // What a provider covers (cfg.providers, app/providers.py). Voice parts are never assumed from LLM support.
  const capsOf = (id) => (cfg.providers || []).find((p) => p.id === id) || { plans: id !== "offline", conversation: false, stt: false, tts: false };
  const CAP_LABELS = [["plans", "Plans, screening, evaluation"], ["conversation", "Interview conversation"], ["stt", "Speech-to-text"], ["tts", "Voice"]];
  // ✓ seen working; ○ supported, not yet tried here; ! listed but reported not to work; + needs another provider.
  // The note (hover, and the coverage panel) says what was seen.
  const EVIDENCE_MARK = { verified: ["yes", "✓ ", "seen working"], documented: ["untried", "○ ", "supported, not yet tried in a live interview"],
                          reported_broken: ["warn", "! ", "listed, but reported not to work"],
                          realtime: ["untried", "◐ ", "only when Gemini runs the whole interview (Gemini Live)"] };
  const capChips = (caps) => el("span", { class: "cap-chips" }, CAP_LABELS.map(([k, label]) => {
    const ev = (caps.evidence || {})[k];
    const [cls, mark, word] = caps[k] ? (EVIDENCE_MARK[ev && ev.level] || EVIDENCE_MARK.documented)
      : ev && ev.level === "realtime" ? EVIDENCE_MARK.realtime : ["no", "+ ", "needs another provider"];
    return el("span", { class: `cap ${cls}`, title: `${label}: ${word}.${ev ? ` ${ev.note}` : ""}` }, mark, label);
  }));

  // compact: without the heading and the Automatic / No AI links (the wizard has its own).
  function providersView({ compact = false } = {}) {
    const inUse = fieldOf("llm_provider").value;
    const active = health.llm.is_ai ? health.llm.name : null;
    const v = cfg.voice || {};
    const cards = el("div", { class: "provider-grid", role: "list" }, PROVIDERS.map((pv) => {
      const configured = providerConfigured(pv);
      const model = fieldOf(pv.keys.find((k) => k.endsWith("_model")))?.value;
      return el("button", { type: "button", class: `provider-card${openProvider === pv.id ? " open" : ""}`, role: "listitem", "aria-expanded": String(openProvider === pv.id),
        onclick: () => { openProvider = openProvider === pv.id ? null : pv.id; draw(); } },
      el("span", { class: "spread" }, el("strong", {}, pv.name),
        inUse === pv.id || v.primary === pv.id ? pill("ready", "In use") : configured ? pill("draft", "Ready") : null),
      el("span", { class: "small muted" }, configured ? (model || "Configured") : "Not configured"),
      capChips(capsOf(pv.id)));
    }));
    const parts = [el("section", { class: "panel stack" },
      compact ? null : el("div", { class: "spread" }, sectionTitle("spark", "AI provider"),
        el("span", { class: "small muted" }, active ? `In use: ${active}` : inUse === "offline" ? "No AI: template plans" : "No provider in use")),
      compact ? null : el("p", { class: "muted small flush" }, "Writes plans, screens resumes and evaluates interviews."),
      cards,
      compact ? null : el("div", { class: "row small" },
        [["auto", "Automatic", "Use automatic choice"], ["offline", "No AI (template plans)", "Use no AI"]].map(([id, label, action]) => inUse === id
          ? el("span", { class: "muted" }, `${label} in use`)
          : el("button", { class: "link small", type: "button", disabled: !isAdmin, onclick: async () => {
            try { cfg = await api("/api/config", { method: "PUT", body: { changes: { llm_provider: id } } }); health = await api("/api/health"); toast("Saved and applied.", "good"); draw(); } catch (err) { toast(err.message); }
          } }, action))))];
    const pv = PROVIDERS.find((x) => x.id === openProvider);
    if (pv) parts.push(providerForm(pv, inUse));
    if (cfg.voice) parts.push(coveragePanel());  // the wizard shows it too: what this provider covers
    return parts;
  }

  // The interview's parts as the next interview will run them, and a fallback for any the provider leaves open.
  function coveragePanel() {
    const v = cfg.voice || {};
    // Switch between separate parts and one realtime model (MeetStream's realtime mode: Gemini Live).
    const setMode = async (mode, button) => {
      const done = busy(button, "Saving…");
      try {
        const changes = mode === "realtime" ? { mia_mode: "realtime", mia_realtime_provider: "google" } : { mia_mode: "pipeline" };
        cfg = await api("/api/config", { method: "PUT", body: { changes } });
        health = await api("/api/health");
        toast(mode === "realtime" ? "Gemini Live runs the next interview." : "The next interview uses separate parts.", "good");
        draw();
      } catch (err) { done(); toast(err.message); }
    };
    const rtCaps = (capsOf(v.primary).realtime) || ((cfg.providers || []).find((p) => p.realtime) || {}).realtime;
    if (v.mode === "realtime") {
      const rt = v.realtime || {};
      const back = el("button", { class: "btn", type: "button", disabled: !isAdmin, onclick: () => setMode("pipeline", back) }, "Use separate parts instead");
      return el("section", { class: "panel stack" }, sectionTitle("live", "The interview"),
        el("p", { class: "small flush" }, el("strong", {}, rt.provider === "google" ? "Gemini for everything (Gemini Live). " : `One realtime model (${rt.provider}). `),
          "One realtime model hears the candidate, runs the conversation and speaks. The separate speech-to-text, conversation and voice settings aren't used."),
        v.ready ? el("ul", { class: "plain stack-tight" },
          [["Conversation", rt.model], ["Speech-to-text", rt.model], ["Voice", rt.voice]].map(([label, val]) => el("li", { class: "spread" },
            el("span", {}, el("strong", {}, label), " ", el("span", { class: "muted" }, `${rt.provider} · ${val || "MeetStream's default"} (realtime)`)),
            pill("draft", rt.provider === "google" ? "Covered by Gemini Live" : "Covered by the realtime model"))))
          : notice("warn", v.problem),
        rt.provider === "google" ? el("p", { class: "small muted flush" }, "The plans, screening and evaluations still use your AI provider.") : null,
        v.meetstream_keys && v.meetstream_keys.length ? notice("info", `Add the ${v.meetstream_keys.join(", ")} in MeetStream → Integrations.`) : null,
        el("p", { class: "small flush" }, "Voice and model: ", el("a", { href: "#/settings?tab=voice" }, "AI interviewer → Voice"), "."),
        el("div", { class: "row" }, back));
    }
    const parts = v.parts || {};
    const who = (p) => p.source === "provider" ? "from the AI provider" : p.source === "override" ? "set separately" : "";
    // What was seen working, for a part that comes from the AI provider (app/providers.py EVIDENCE).
    const evidence = (capsOf(v.primary).evidence || {});
    const PART_KEY = { Conversation: "conversation", "Speech-to-text": "stt", Voice: "tts" };
    const row = (label, p, extra) => {
      const ev = p.source === "provider" ? evidence[PART_KEY[label]] : null;
      const covered = p.provider && !(label === "Voice" && !p.model);
      return el("li", { class: "stack-tight" },
        el("span", { class: "spread" },
          el("span", {}, el("strong", {}, label), " ", p.provider ? el("span", { class: "muted" }, `${p.provider}${p.model ? ` · ${p.model}` : ""} (${who(p)})`) : null),
          !covered ? pill("failed", "Needs a provider") : ev && ev.level === "reported_broken" ? pill("hold", "Reported not to work")
            : ev && ev.level === "documented" ? pill("draft", "Not yet tried") : pill("ready", "Covered"), extra),
        ev && covered && ev.level !== "verified" ? el("span", { class: "small muted" }, ev.note) : null);
    };
    const gaps = [];
    if (!(parts.stt && parts.stt.provider)) gaps.push(["mia_stt_provider", "mia_stt_model"]);
    if (!(parts.llm && parts.llm.provider)) gaps.push(["mia_llm_provider", "mia_llm_model"]);
    if (!(parts.tts && parts.tts.provider && parts.tts.model)) gaps.push(["mia_tts_provider", "mia_voice"]);
    const live = rtCaps && v.primary === "gemini"
      ? (() => {
        const b = el("button", { class: "btn", type: "button", disabled: !isAdmin, onclick: () => setMode("realtime", b) }, "Use Gemini for everything (Gemini Live)");
        return el("div", { class: "stack-tight" },
          el("p", { class: "small flush" }, "MeetStream's separate speech-to-text and voice have no Google option, so Gemini hears and speaks only as Gemini Live: "
            + "one realtime model for the whole interview. Or keep Gemini for the conversation and set speech-to-text and voice below (for example Deepgram and ElevenLabs)."),
          el("div", { class: "row" }, b));
      })()
      : null;
    const section = el("section", { class: "panel stack" }, sectionTitle("live", "The interview"),
      el("ul", { class: "plain stack-tight" }, row("Conversation", parts.llm || {}), row("Speech-to-text", parts.stt || {}), row("Voice", parts.tts || {})),
      v.ready && v.meetstream_keys && v.meetstream_keys.length
        ? notice("info", `Also add the ${v.meetstream_keys.join(", ")} in MeetStream → Integrations.`)
        : null,
      live);
    if (gaps.length && !(cfg.providers || []).some((p) => p.id === v.primary)) {
      section.append(notice("info", "Choose an AI provider above first: it covers whatever it can, and only what's left needs another provider."));
    } else if (gaps.length) {
      const changes = {};
      const status = el("p", { class: "small flush", "aria-live": "polite" });
      const save = el("button", { class: "btn primary", type: "submit", disabled: !isAdmin }, "Save");
      const form = el("form", { class: "stack", novalidate: true },
        notice("warn", v.problem || "Some of the interview has no provider yet."),
        el("p", { class: "small flush" }, "Choose another provider just for what's missing (its key goes in MeetStream too):"),
        ...gaps.flat().map((k) => fieldRow(fieldOf(k), changes)),
        el("div", { class: "form-actions" }, save, status));
      form.addEventListener("submit", (e) => { e.preventDefault(); saveChanges(changes, form, status, save); });
      section.append(form);
    }
    return section;
  }

  function providerForm(pv, inUse) {
    const changes = {};
    const rows = pv.keys.map((k) => fieldRow(fieldOf(k), changes));
    const status = el("p", { class: "small flush", "aria-live": "polite" });
    const out = el("p", { class: "small flush", "aria-live": "polite" });
    const models = el("datalist", { id: "ollama-models" });
    const value = (key) => (key ? document.getElementById(`cfg-${key}`)?.value.trim() || "" : "");
    const save = el("button", { class: "btn", type: "submit", disabled: !isAdmin }, "Save");
    const use = el("button", { class: "btn primary", type: "button", disabled: !isAdmin || inUse === pv.id }, inUse === pv.id ? "In use" : "Save and use");
    const test = el("button", { class: "btn", type: "button", onclick: async () => {
      out.textContent = ""; const done = busy(test, "Testing…");
      const [k, m, b] = pv.test;
      try {
        const r = await api("/api/config/test-ai", { method: "POST", body: { provider: pv.id, api_key: value(k), model: value(m), base_url: value(b) } });
        out.replaceChildren(pill(r.ok ? "ready" : "failed", r.ok ? "OK" : "Problem"), ` ${r.message}`);
        if (pv.id === "ollama" && r.models.length) models.replaceChildren(...r.models.map((x) => el("option", { value: x })));
      } catch (err) { out.replaceChildren(pill("failed", "Problem"), ` ${err.message}`); }
      done();
    } }, "Test connection");
    const form = el("form", { class: "panel stack", novalidate: true }, el("h3", { class: "flush" }, pv.name), ...rows, models,
      el("div", { class: "form-actions" }, use, save, test, status), out);
    form.addEventListener("submit", (e) => { e.preventDefault(); saveChanges(changes, form, status, save); });
    use.addEventListener("click", () => saveChanges({ ...changes, llm_provider: pv.id }, form, status, use));
    return form;
  }

  // The AI interviewer: compact widgets showing the values the next interview will use; one opens at a time to edit.
  // Each value says whether someone chose it (saved here or set on the server) or it is the built-in default; a voice
  // provider nobody chose shows as Not configured, with the default that would be used.
  const chosenVal = (key, text) => {
    const f = fieldOf(key);
    return f && f.source === "default" ? `${text} (built-in default)` : text;
  };
  const partText = (p) => !p || !p.provider ? "needs a provider (Settings → AI provider)"
    : `${p.provider}${p.model ? ` · ${p.model}` : ""}${p.source === "provider" ? " (from the AI provider)" : " (set separately)"}`;
  const INTERVIEWER_WIDGETS = [
    { id: "voice", title: "Voice",
      get keys() { return (cfg.voice || {}).mode === "realtime" ? ["mia_mode", "mia_realtime_provider", "mia_realtime_voice", "mia_realtime_model", "mia_realtime_inline_plan"] : ["mia_mode", "mia_voice"]; },
      summary: () => {
        const v = cfg.voice || {};
        if (v.mode === "realtime") {
          const rt = v.realtime || {};
          return v.ready ? [`One realtime model: ${rt.provider} · ${rt.model || "MeetStream's default"}`, `Voice: ${rt.voice}`,
            rt.provider === "google" ? "Hears, thinks and speaks (Gemini Live)" : "Hears, thinks and speaks"] : [v.problem];
        }
        const parts = v.parts || {};
        return [`Voice: ${partText(parts.tts)}`, `Conversation: ${partText(parts.llm)}`, `Speech-to-text: ${partText(parts.stt)}`];
      } },
    { id: "turns", title: "Turn-taking", keys: ["mia_min_endpointing_delay", "mia_max_endpointing_delay", "mia_interrupt_min_seconds", "mia_interrupt_min_words", "mia_vad_min_silence_ms"],
      summary: (v) => [chosenVal("mia_min_endpointing_delay", `Waits ${v.mia_min_endpointing_delay}-${v.mia_max_endpointing_delay} s before replying`),
        v.mia_interrupt_min_seconds || v.mia_interrupt_min_words ? chosenVal("mia_interrupt_min_seconds", `Interruptions after ${v.mia_interrupt_min_seconds} s / ${v.mia_interrupt_min_words} words`)
          : "Interruption thresholds: off (MeetStream's own)",
        v.mia_vad_min_silence_ms ? chosenVal("mia_vad_min_silence_ms", `Speech ends after ${v.mia_vad_min_silence_ms} ms of silence`) : "End of speech: MeetStream's own setting"] },
    { id: "structure", title: "Interview structure", keys: ["max_follow_ups_per_question", "default_duration_minutes"],
      summary: (v) => [chosenVal("max_follow_ups_per_question", `Up to ${v.max_follow_ups_per_question} follow-ups per question`), "A question's own follow-up limit in its plan wins",
        chosenVal("default_duration_minutes", `New roles and interviews start at ${v.default_duration_minutes} min`)] },
    { id: "candidate", title: "Candidate experience", keys: ["bot_name", "chat_disclosure", "mia_language"],
      summary: (v) => [chosenVal("bot_name", `Joins as “${v.bot_name}”`), chosenVal("chat_disclosure", v.chat_disclosure ? "Notice posted in meeting chat" : "No chat notice"),
        chosenVal("mia_language", `Language: ${v.mia_language}`)] },
    { id: "transcript", title: "Transcript", keys: ["postcall_transcript"],
      summary: (v) => [chosenVal("postcall_transcript", v.postcall_transcript ? "Rebuilt from the recording after the call" : "Live transcript only")] },
    { id: "advanced", title: "Advanced: separate providers", keys: ["mia_stt_provider", "mia_stt_model", "mia_llm_provider", "mia_llm_model", "mia_tts_provider"],
      summary: () => { const p = (cfg.voice || {}).parts || {}; const sep = ["stt", "llm", "tts"].filter((k) => p[k] && p[k].source === "override");
        return [sep.length ? `Set separately: ${sep.map((k) => ({ stt: "speech-to-text", llm: "conversation", tts: "voice" })[k]).join(", ")}` : "None: everything comes from the AI provider",
          "Only to use a different provider for one part of the interview."]; } },
  ];
  // Which of a widget's values were chosen: all, some, or none (built-in defaults only).
  const widgetSource = (w) => {
    const v = cfg.voice || {};
    if (w.id === "voice") return v.ready ? ["ready", "Ready"] : ["failed", "Needs a provider"];
    if (w.id === "advanced") return Object.values(v.parts || {}).some((p) => p.source === "override") ? ["ready", "In use"] : ["draft", "Not used"];
    const srcs = w.keys.map((k) => fieldOf(k)?.source || "default");
    if (srcs.every((s) => s === "default")) return ["draft", "Built-in defaults"];
    if (srcs.some((s) => s === "saved")) return ["ready", srcs.every((s) => s === "saved") ? "Saved here" : "Partly saved here"];
    return ["ready", "Set on the server"];
  };
  let openWidget = null;

  // The voice: a list of the voice provider's voices where MeetStream documents them, else an id from your account.
  // The realtime model's voice, from its provider's documented voices (cfg.providers[].realtime).
  function realtimeVoiceRow(changes) {
    const v = cfg.voice || {};
    const rt = ((cfg.providers || []).map((p) => p.realtime).find((r) => r && r.provider === (v.realtime || {}).provider)) || null;
    if (!rt || !rt.voices.length) return fieldRow(fieldOf("mia_realtime_voice"), changes);
    const current = (v.realtime || {}).voice || rt.default_voice;
    const select = el("select", { id: "cfg-mia_realtime_voice", disabled: !isAdmin, onchange: (e) => { changes.mia_realtime_voice = e.target.value; } },
      rt.voices.map((x) => el("option", { value: x, selected: x === current }, x === rt.default_voice ? `${x} (default)` : x)));
    return el("label", { class: "field" }, el("span", {}, `Voice (${rt.provider} realtime)`), select);
  }

  function voiceRow(changes) {
    const f = fieldOf("mia_voice");
    const tts = ((cfg.voice || {}).parts || {}).tts || {};
    const voices = ((cfg.tts_providers || {})[tts.provider] || {}).voices || [];
    if (!tts.provider) return el("p", { class: "muted small flush" }, "Choose a voice provider first (Settings → AI provider).");
    if (!voices.length) return fieldRow(f, changes);
    const current = f.value || tts.model;
    const select = el("select", { id: "cfg-mia_voice", disabled: !isAdmin, onchange: (e) => { changes.mia_voice = e.target.value; } },
      voices.map((x) => el("option", { value: x, selected: x === current }, x)));
    return el("label", { class: "field" }, el("span", {}, `Voice (${tts.provider})`), select);
  }

  function interviewerView() {
    const values = Object.fromEntries(allFields().map((f) => [f.key, f.value]));
    const grid = el("div", { class: "widget-grid" });
    for (const w of INTERVIEWER_WIDGETS) {
      const open = openWidget === w.id;
      const pending = w.keys.some((k) => fieldOf(k)?.restart_pending);
      const card = el("section", { class: `panel widget${open ? " open" : ""}` },
        el("div", { class: "spread" }, el("span", { class: "row" }, el("h3", { class: "flush" }, w.title), pill(...widgetSource(w))),
          el("button", { class: "btn small", type: "button", "aria-expanded": String(open), onclick: () => { openWidget = open ? null : w.id; draw(); } }, open ? "Close" : "Edit")),
        el("ul", { class: "plain small widget-values" }, w.summary(values).map((s) => el("li", {}, s))),
        pending ? pill("scheduled", "Waiting for restart") : null);
      if (open) {
        const changes = {};
        const status = el("p", { class: "small flush", "aria-live": "polite" });
        const save = el("button", { class: "btn primary", type: "submit", disabled: !isAdmin }, "Save");
        const form = el("form", { class: "stack", novalidate: true }, ...w.keys.map((k) => k === "mia_voice" ? voiceRow(changes) : k === "mia_realtime_voice" ? realtimeVoiceRow(changes) : fieldRow(fieldOf(k), changes)),
          el("div", { class: "form-actions" }, save, status));
        form.addEventListener("submit", (e) => { e.preventDefault(); saveChanges(changes, form, status, save); });
        card.append(form);
      }
      grid.append(card);
    }
    const v = cfg.voice || {};
    const head = el("section", { class: "panel stack" },
      sectionTitle("live", "What the next interview uses"),
      v.mode === "realtime" && v.ready
        ? notice("info", `One realtime model${v.realtime.provider === "google" ? " (Gemini Live)" : ""}: ${v.realtime.provider} · ${v.realtime.model || "MeetStream's default"}, voice ${v.realtime.voice}, hears, thinks and speaks. `,
          "The separate speech-to-text, conversation and voice settings aren't used in this mode.")
        : !v.ready
          ? notice("warn", v.problem || "The interview isn't fully set up.", " ", el("a", { href: "#/settings?tab=ai" }, "Set up the AI provider"))
          : el("p", { class: "small flush" }, v.label),
      el("ul", { class: "plain small muted stack-tight" },
        el("li", {}, "These settings are for the whole installation: every workspace's interviews use them."),
        el("li", {}, "Changes apply from the next interview."),
        el("li", {}, `Voice provider keys live in MeetStream → Integrations${v.meetstream_keys && v.meetstream_keys.length ? `: ${v.meetstream_keys.join(", ")}` : ""}.`)));
    return [head, grid];
  }

  // Connection checks; `only` limits them to one tab's (e.g. ["meetstream", "public_url"]).
  function integrationChecks(only = ["public_url", "meetstream", "database", "email"]) {
    const result = el("p", { class: "small flush", "aria-live": "polite" });
    const check = (name, label, bodyFn) => {
      const b = el("button", { class: "btn small", type: "button", onclick: async () => {
        result.textContent = ""; const done = busy(b, "Checking…");
        try {
          const r = await api(`/api/settings/check/${name}`, { method: "POST", body: bodyFn ? bodyFn() : {} });
          result.replaceChildren(pill(r.ok ? "ready" : "failed", r.ok ? "OK" : "Problem"), ` ${r.message}`);
        } catch (err) { result.replaceChildren(pill("failed", "Problem"), ` ${err.message}`); }
        done();
      } }, label);
      return b;
    };
    const testTo = el("input", { type: "email", class: "inline", placeholder: "you@example.com", "aria-label": "Send a test email to" });
    return el("section", { class: "panel stack" }, sectionTitle("shield", "Checks"),
      el("div", { class: "row" },
        only.includes("public_url") ? check("public_url", "Public address reaches this app", () => ({ values: formValues(["public_base_url"]) })) : null,
        only.includes("meetstream") ? check("meetstream", "MeetStream key", () => ({ values: formValues(["meetstream_api_key"]) })) : null,
        only.includes("database") ? check("database", "Database") : null,
        only.includes("email") ? el("span", { class: "row" }, testTo, check("email", "Send test email", () => ({ to: testTo.value.trim(), values: formValues(EMAIL_KEYS) }))) : null),
      result);
  }

  // People sign in with accounts. An access token (APP_ACCESS_TOKEN) is only for scripts calling the API; if one
  // from an earlier version of setup is still saved here, it can be revoked, so it never stays valid unnoticed.
  function accessPanel() {
    const t = cfg.access_token;
    if (!t.set) return null;
    const revoke = el("button", { class: "btn danger", type: "button", disabled: !isAdmin || t.source !== "saved", onclick: async () => {
      if (!(await confirmDialog("Revoke the saved access token? Scripts using it stop working; people keep signing in with their accounts.", { title: "Revoke access token", confirmLabel: "Revoke", danger: true }))) return;
      try { await api("/api/config/access-token/revoke", { method: "POST" }); toast("Access token revoked.", "good"); cfg = await api("/api/config"); draw(); } catch (err) { toast(err.message, "bad", 9000); }
    } }, "Revoke");
    return el("section", { class: "panel stack" }, sectionTitle("shield", "API access token"),
      el("p", { class: "muted small flush" }, t.source === "env"
        ? "APP_ACCESS_TOKEN is set in the environment: scripts can call the API with it (Authorization: Bearer). Remove it there to turn it off."
        : "An access token from an earlier setup is still active. People sign in with accounts now; only scripts need a token. Revoke it unless a script uses it."),
      t.source === "saved" ? el("div", { class: "row" }, revoke) : null);
  }

  // The database: what's in use, and connecting a hosted PostgreSQL that the app uses from its next start.
  function dataPanel() {
    const panel = el("section", { class: "panel stack" }, sectionTitle("doc", "Database"), el("p", { class: "muted flush" }, "Loading…"));
    api("/api/database").then((db) => drawDatabase(panel, db)).catch((err) => panel.replaceChildren(sectionTitle("doc", "Database"), notice("bad", err.message)));
    return panel;
  }

  function drawDatabase(panel, db) {
    const source = { env: "set by DATABASE_URL in .env", saved: "saved here", local: "local file", given: "" }[db.source];
    const head = [sectionTitle("doc", "Database"),
      el("p", { class: "flush" }, el("strong", {}, db.current), source ? el("span", { class: "muted small" }, ` · ${source}`) : null),
      db.problem ? notice("bad", db.problem) : null,
      db.restart_required ? notice("warn", db.saved ? `Saved: ${db.saved}. Restart the app to switch to it.` : "The saved connection was removed. Restart the app to go back to the local database.") : null,
      el("p", { class: "muted small flush" }, `Saved keys and passwords are encrypted with ${cfg.encryption_key}.`)];
    if (db.env_locked || !isAdmin) { panel.replaceChildren(...head.filter(Boolean)); return; }
    const url = el("input", { type: "password", autocomplete: "off", placeholder: "postgresql://user:password@host:5432/database", "aria-label": "PostgreSQL connection URL" });
    const result = el("div", { "aria-live": "polite" });
    const choice = el("div", { class: "stack" });
    let tested = null;
    const radio = (value, label, checked) => el("label", { class: "check" }, el("input", { type: "radio", name: "db-data", value, checked }), el("span", {}, label));
    const test = el("button", { class: "btn", type: "button", onclick: async () => {
      tested = null; save.disabled = true; choice.replaceChildren(); result.replaceChildren();
      const done = busy(test, "Testing…");
      try {
        const r = await api("/api/database/test", { method: "POST", body: { url: url.value.trim() } });
        result.replaceChildren(notice(r.ok ? "good" : "bad", r.message));
        if (r.ok) {
          tested = r;
          save.disabled = false;
          choice.replaceChildren(...(r.has_data
            ? [notice("warn", `It already holds Recruence data (${Object.entries(r.rows).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(", ")}).`),
              radio("use_existing", "Use the data already in that database (this install's current data is not copied)", true)]
            : [radio("copy", `Copy this install's data into it (${db.current})`, true), radio("empty", "Start empty", false)]));
        }
      } catch (err) { result.replaceChildren(notice("bad", err.message)); }
      done();
    } }, "Test connection");
    const save = el("button", { class: "btn primary", type: "button", disabled: true, onclick: async () => {
      const data = (panel.querySelector('input[name="db-data"]:checked') || {}).value || "copy";
      const what = data === "copy" ? "This install's data is copied there now." : data === "empty" ? "The app will start with no data there." : "The app will use the data already there.";
      if (!(await confirmDialog(`Use ${tested.database} from the next restart? ${what} Until you restart, the app keeps using ${db.current}.`, { title: "Change the database", confirmLabel: "Save" }))) return;
      const done = busy(save, data === "copy" ? "Copying…" : "Saving…");
      try {
        const r = await api("/api/database", { method: "PUT", body: { url: url.value.trim(), data, replace: Boolean(db.saved) } });
        toast(r.copied ? `Saved. Copied ${Object.values(r.copied).reduce((a, b) => a + b, 0)} rows. Restart to switch.` : "Saved. Restart to switch.", "good", 9000);
        drawDatabase(panel, r);
      } catch (err) { result.replaceChildren(notice("bad", err.message)); done(); }
    } }, "Save");
    const forget = db.saved ? el("button", { class: "btn danger", type: "button", onclick: async () => {
      if (!(await confirmDialog("Remove the saved connection? From the next restart the app uses the local database. Nothing is deleted from either database.", { title: "Remove database connection", confirmLabel: "Remove", danger: true }))) return;
      try { drawDatabase(panel, await api("/api/database", { method: "DELETE" })); toast("Removed. Restart to switch back.", "good"); } catch (err) { toast(err.message); }
    } }, "Remove saved connection") : null;
    panel.replaceChildren(...head.filter(Boolean),
      el("label", { class: "field" }, el("span", {}, db.saved ? "Replace with another PostgreSQL database" : "Connect a hosted PostgreSQL database"), url),
      result, choice,
      el("div", { class: "form-actions" }, test, save, forget));
  }

  return {
    get cfg() { return cfg; },
    set cfg(value) { cfg = value; },
    set redraw(fn) { draw = fn; },
    isAdmin, fieldRow, allFields, fieldOf, formValues, saveChanges, sectionForm, providersView, providerForm, providerConfigured,
    interviewerView, integrationChecks, accessPanel, dataPanel, PROVIDERS,
    get openProvider() { return openProvider; },
    set openProvider(v) { openProvider = v; },
  };
}

async function renderSettings() {
  const installAdmin = !user || (user.via === "account" ? user.is_admin : user.role === "admin");
  // Installation settings (hosts, keys set or not) are only for its admins; others see their workspace.
  const kit = makeKit(installAdmin ? await api("/api/config") : { sections: [], restart_required: [], problems: [] }, installAdmin);
  const wsTab = meInfo && meInfo.workspace_id ? [{ id: "workspace", title: "Workspace" }] : [];
  const sections = [...wsTab, { id: "sources", title: "Recruitment sources" }, ...(installAdmin ? kit.cfg.sections : [])];
  const tabs = el("div", { class: "seg settings-tabs", role: "tablist", "aria-label": "Settings sections" });
  const body = el("div", { class: "stack" });
  const restartNote = el("div");
  let current = (location.hash.split("?tab=")[1] || "general");

  const drawRestart = () => restartNote.replaceChildren(kit.cfg.restart_required.length
    ? notice("warn", `Saved, waiting for a restart: ${kit.cfg.restart_required.join(", ")}. Restart the app for these to take effect.`) : "");
  const drawTabs = () => tabs.replaceChildren(...sections.map((s) => el("button", {
    type: "button", class: "seg-btn", role: "tab", "aria-selected": String(s.id === current), "aria-pressed": String(s.id === current),
    onclick: () => { current = s.id; history.replaceState(null, "", `#/settings?tab=${s.id}`); draw(); },
  }, s.title)));

  // One field: an input of the right kind, where its value comes from, and whether it applies now.
  const statusBody = el("div", { class: "stack" });
  const drawStatus = () => statusBody.replaceChildren(...[
    el("div", { class: "row" },
      health.meetstream.ready ? pill("ready", "MeetStream configured") : pill("scheduled", "MeetStream not configured"),
      health.llm.is_ai ? pill("ready", `AI model: ${health.llm.name}`) : pill("failed", "No AI model: template plans only")),
    health.meetstream.ready ? null : el("p", { class: "muted small flush" }, `${health.meetstream.reason}.`),
    kit.cfg.problems.length ? notice("bad", `Some saved settings couldn't be read and were skipped: ${kit.cfg.problems.join("; ")}`) : null,
    authInfo && authInfo.database_problem ? notice("warn", authInfo.database_problem) : null].filter(Boolean));

  function draw() {
    drawTabs();
    drawRestart();
    drawStatus();
    const listed = sections.find((s) => s.id === current) || sections[0];
    const section = kit.cfg.sections.find((s) => s.id === listed.id) || listed; // the latest values: a save replaces kit.cfg
    if (!section) { body.replaceChildren(notice("info", "Ask your workspace owner for anything you need changed here.")); return; }
    current = section.id;
    let parts;
    if (section.id === "workspace") { parts = [workspacePanel()]; body.replaceChildren(...parts); return; }
    if (section.id === "sources") { body.replaceChildren(sourcesPanel()); return; }
    if (section.id === "ai") parts = kit.providersView();
    else if (section.id === "voice") parts = kit.interviewerView();
    else {
      parts = [kit.sectionForm(section)];
      if (section.id === "access") parts.unshift(accountsPanel(), kit.accessPanel());
      if (section.id === "database") parts.unshift(connectionsPanel(), ownershipPanel(), diagnosticsPanel(), kit.dataPanel());
      if (section.id === "meetstream") parts.unshift(readinessPanel(), kit.integrationChecks(["meetstream", "public_url"]));
      if (section.id === "email") parts.unshift(kit.integrationChecks(["email"]));
      if (section.id === "system") parts.unshift(...(window.miaDesktop ? [desktopConnectionPanel()] : []), demoPanel(), setupPreviewPanel(), kit.integrationChecks());
    }
    body.replaceChildren(...parts.filter(Boolean));
  }

  view.replaceChildren(el("div", { class: "stack" },
    pageHead({ title: "Settings" }),
    el("section", { class: "panel stack" }, sectionTitle("live", "Status"), statusBody),
    restartNote, tabs, body));
  kit.redraw = draw;
  draw();
}

// Who this database belongs to and which installations have used it. Read only: an unknown account is disabled from
// Settings → Access → Accounts; nothing here deletes anything.
function ownershipPanel() {
  const panel = el("section", { class: "panel stack" }, sectionTitle("shield", "Who uses this database"), el("p", { class: "muted small flush" }, "Loading…"));
  api("/api/database/ownership").then((o) => {
    const others = o.installations.filter((i) => !i.this_one);
    panel.replaceChildren(sectionTitle("shield", "Who uses this database"),
      el("dl", { class: "facts" },
        el("dt", {}, "Owner"), el("dd", {}, o.owner ? `${o.owner.name} (${o.owner.email})` : "No account yet"),
        el("dt", {}, "Set up from"), el("dd", {}, o.owner_installation_is_this_one == null ? "Not recorded yet (recorded when an administrator signs in)"
          : o.owner_installation_is_this_one ? "This installation" : "Another installation")),
      el("p", { class: "muted small flush" }, "Accounts from another installation can only be created with a workspace's joining code. Knowing the connection string isn't enough."),
      el("h3", { class: "flush" }, `Installations (${o.installations.length})`),
      el("ul", { class: "plain small stack-tight" }, o.installations.map((i) => el("li", {}, el("strong", {}, i.label || "Unnamed"), i.this_one ? " · this one" : "",
        el("span", { class: "muted" }, ` · first ${fmtTime(i.first_seen)}, last ${fmtTime(i.last_seen)}`)))),
      others.length ? notice("info", `${others.length} other installation${others.length === 1 ? "" : "s"} use${others.length === 1 ? "s" : ""} this database. That's expected for a team; if you don't recognise one, change the database password at your provider and review the accounts below.`) : null,
      o.accounts_without_workspace.length ? el("div", { class: "stack" },
        el("h3", { class: "flush" }, `Accounts in no team's workspace (${o.accounts_without_workspace.length})`),
        el("ul", { class: "plain small stack-tight" }, o.accounts_without_workspace.map((u) => el("li", {}, `${u.name} (${u.email})`,
          el("span", { class: "muted" }, ` · created ${fmtTime(u.created_at)}${u.admin ? " · administrator" : ""}${u.disabled ? " · disabled" : ""}`)))),
        el("p", { class: "muted small flush" }, "They have only their own workspace and can't see any other's data. Disable any you don't recognise in Settings → Access → Accounts.")) : null);
  }).catch((err) => panel.replaceChildren(sectionTitle("shield", "Who uses this database"), notice("bad", err.message)));
  return panel;
}

// Demo data loaded by an earlier version (app/demo.py): shown only while some is installed, so it can be removed.
// Never touches real records.
function demoPanel() {
  const panel = el("section", { class: "panel stack", hidden: true });
  const draw = (s) => {
    panel.hidden = !s.installed;
    if (!s.installed) { panel.replaceChildren(); return; }
    const remove = el("button", { class: "btn danger", type: "button", onclick: async () => {
      if (!(await confirmDialog("Remove all demo roles, candidates, interviews and reports? Your own data isn't touched.", { title: "Remove demo data", confirmLabel: "Remove", danger: true }))) return;
      const done = busy(remove, "Removing…");
      try { draw(await api("/api/demo", { method: "DELETE" })); toast("Demo data removed.", "good"); } catch (err) { toast(err.message); done(); }
    } }, "Remove demo data");
    panel.replaceChildren(sectionTitle("users", "Demo data"),
      el("p", { class: "muted small flush" }, `Installed: ${s.roles} roles, ${s.interviews} interviews, ${s.applications} applications. Fictional people, marked Demo; they can't be run or emailed.`),
      el("div", { class: "row" }, remove));
  };
  api("/api/demo").then(draw).catch(() => {});
  return panel;
}

// The desktop app only (desktop/main.js): where it connects. A cloud server keeps candidates, interviews and webhooks
// working when this computer is off (docs/CLOUD.md); "this computer" runs the backend locally, as before.
function desktopConnectionPanel() {
  const panel = el("section", { class: "panel stack" }, sectionTitle("cloud", "Desktop app connection"));
  window.miaDesktop.info().then((info) => {
    if (!info) return;
    const input = el("input", { type: "url", value: info.server || "", placeholder: "https://recruence.example.com", class: "inline grow",
      "aria-label": "Recruence server address", disabled: info.serverFromEnvironment });
    const result = el("p", { class: "small flush", "aria-live": "polite" });
    const go = el("button", { class: "btn", type: "button", disabled: info.serverFromEnvironment, onclick: async () => {
      const done = busy(go, "Checking…");
      const r = await window.miaDesktop.setServer(input.value.trim());
      if (!r || !r.ok) { result.className = "small flush warn-text"; result.textContent = (r && r.message) || "Couldn't connect."; done(); }
    } }, info.server ? "Change server" : "Connect to a server");
    const local = info.server && !info.serverFromEnvironment ? el("button", { class: "link small", type: "button", onclick: async () => {
      if (!(await confirmDialog("Run Recruence on this computer instead? Candidates' interviews then need this computer on.", { title: "Use this computer", confirmLabel: "Use this computer" }))) return;
      await window.miaDesktop.setServer("");
    } }, "Use this computer instead") : null;
    panel.replaceChildren(sectionTitle("cloud", "Desktop app connection"),
      el("p", { class: "small flush" }, info.mode === "cloud"
        ? ["Connected to the Recruence server at ", el("strong", {}, info.server), ". Candidates can apply and take interviews while this computer is off."]
        : "Recruence runs on this computer: interviews need it switched on, with a tunnel to it. Connect to a Recruence server in the cloud to keep it running without this computer."),
      info.serverFromEnvironment ? el("p", { class: "muted small flush" }, "Set by RECRUENCE_SERVER_URL for this app.") : null,
      el("div", { class: "row" }, input, go), local, result);
  }).catch(() => {});
  return panel;
}

function setupPreviewPanel() {
  return el("section", { class: "panel spread" },
    el("div", {}, sectionTitle("spark", "First-run setup"), el("p", { class: "muted small flush" }, "Walk through the setup wizard. Nothing is saved and every check is simulated.")),
    el("a", { class: "btn", href: "#/setup-preview" }, "Preview setup"));
}

// ---------- setup preview: the real wizard over a practice state in the browser ----------
// Every call the wizard makes to /api/setup, /api/config, the connection tests and the database test is answered
// here instead of the server. Nothing is saved, nothing leaves the browser, and every check result says "Simulated".
// Type "bad" or "fail" into a key, address or connection string to see the failure states.

let setupPreview = null;

function startSetupPreview(realCfg) {
  const cfg = structuredClone(realCfg);
  for (const s of cfg.sections) {
    for (const f of s.fields) {
      f.source = "default";
      f.restart_pending = false;
      if (f.kind === "secret") f.set = false;
      if (["public_base_url", "smtp_host", "smtp_from", "smtp_username", "mail_reply_to", "ollama_model"].includes(f.key)) f.value = "";
      if (f.key === "llm_provider") f.value = "auto";
    }
  }
  cfg.restart_required = [];
  cfg.problems = [];
  setupPreview = { cfg, state: { mode: null, admin_name: "", admin_email: "", skipped: [], checks: {}, access: null, ai_offline: false, database: null, aiName: null, careersSite: "", careersKey: false } };
}

function previewField(key) { return setupPreview.cfg.sections.flatMap((s) => s.fields).find((f) => f.key === key); }
const SIMULATED = "Simulated: ";
const looksBad = (v) => /bad|fail|wrong/i.test(String(v || ""));

function previewStatus() {
  const st = setupPreview.state;
  const val = (k) => previewField(k);
  const skipped = new Set(st.skipped);
  const mark = (name, done, failed = false) => (done ? "done" : failed ? "failed" : skipped.has(name) ? "skipped" : "incomplete");
  const ai = st.checks.ai, ms = st.checks.meetstream, url = st.checks.public_url, mail = st.checks.email;
  const msKey = val("meetstream_api_key").set;
  const mailOn = Boolean(val("smtp_host").value && val("smtp_from").value);
  return {
    completed: false, signed_in: true, code_required: false, preview: true,
    mode: st.mode, admin_name: st.admin_name, admin_email: st.admin_email,
    order: SETUP_STEPS.map(([k]) => k), optional: ["email", "meetstream", "careers"],
    steps: {
      welcome: { status: mark("welcome", Boolean(st.mode)), mode: st.mode },
      access: { status: mark("access", Boolean(st.access)), configured: Boolean(st.access), method: st.access, token_source: null, local_request: true, proxy: false,
        accounts: st.access === "account", signed_in_account: st.access === "account" },
      database: { status: "done", current: "SQLite, data/interviewer.db (example)", backend: "sqlite", source: "local", saved: null, restart_required: false,
        env_locked: false, problem: "", pending: st.database },
      ai: { status: mark("ai", Boolean(st.aiName) || st.ai_offline, ai && !ai.ok && !st.aiName), configured: Boolean(st.aiName), name: st.aiName,
        provider: val("llm_provider").value, offline_chosen: st.ai_offline && !st.aiName, check: ai || null },
      meetstream: { status: mark("meetstream", msKey && ms && ms.ok && url && url.ok, (ms && !ms.ok) || (url && !url.ok)), key_set: msKey,
        key_source: "default", public_base_url: val("public_base_url").value, url_source: "default", check: ms || null, url_check: url || null },
      email: { status: mark("email", mailOn && mail && mail.ok, mail && !mail.ok), configured: mailOn, from: val("smtp_from").value, host_source: "default", check: mail || null },
      careers: { status: mark("careers", Boolean(st.careersSite)), site: st.careersSite },
    },
  };
}

// Answers the wizard's calls while the preview runs. Returns undefined for anything it doesn't handle.
function previewApi(path, opts = {}) {
  if (!setupPreview) return undefined;
  const st = setupPreview.state;
  const body = typeof opts.body === "string" ? JSON.parse(opts.body) : opts.body || {};
  const method = (opts.method || "GET").toUpperCase();
  const check = (name, ok, message, extra = {}) => { st.checks[name] = { ok, message: SIMULATED + message, at: new Date().toISOString(), ...extra }; return st.checks[name]; };
  if (path === "/api/setup/status") return previewStatus();
  if (path === "/api/config" && method === "GET") return setupPreview.cfg;
  // The careers website step: the workspace isn't changed, and the address shown is an example.
  if (path === "/api/workspaces/current" || path === "/api/workspaces/current/careers-key") {
    if (path === "/api/workspaces/current" && method === "PUT") { st.careersSite = body.careers_site || st.careersSite; st.careersNewJobs = body.careers_new_jobs || st.careersNewJobs; }
    if (path.endsWith("careers-key") && method !== "GET") st.careersKey = method === "POST";
    const key = st.careersKey ? "example-key" : "";
    return { careers_site: st.careersSite, careers_new_jobs: st.careersNewJobs || "manual", careers_path: key ? `/api/public/careers/${key}` : "", careers_url: "" };
  }
  if (path === "/api/setup/state") {
    const { skip, unskip, ...rest } = body;
    Object.assign(st, rest);
    if (skip) st.skipped = [...new Set([...st.skipped, skip])];
    if (unskip) st.skipped = st.skipped.filter((x) => x !== unskip);
    return previewStatus();
  }
  if (path === "/api/auth/register") {
    if (looksBad(body.email) || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(body.email || ""))) throw new ApiError(`${SIMULATED}enter a valid email address.`);
    if (!String(body.name || "").trim()) throw new ApiError(`${SIMULATED}enter your name.`);
    st.access = "account";
    st.admin_name = body.name; st.admin_email = body.email;
    return { signed_in: true, simulated: true };
  }
  if (path === "/api/setup/access") {
    st.access = body.method;
    return { ...previewStatus(), token: body.method === "token" ? "example-token-shown-once-in-a-real-setup" : null };
  }
  if (path === "/api/database/test") {
    const url = String(body.url || "");
    if (!/^postgres(ql)?:\/\//.test(url)) return { ok: false, message: `${SIMULATED}that isn't a PostgreSQL URL (postgresql://user:password@host:5432/database)` };
    if (looksBad(url)) return { ok: false, message: `${SIMULATED}couldn't connect: connection timeout expired` };
    const hasData = /existing|full/i.test(url);
    return { ok: true, message: `${SIMULATED}connected to PostgreSQL at example-host:5432 (no connection was made).`, database: "PostgreSQL at example-host:5432/mia", has_data: hasData, rows: hasData ? { interviews: 12 } : {},
      state: hasData ? "mia" : "empty", summary: hasData ? "Existing Recruence installation found (12 interviews). Its data is kept as it is." : "Empty database: ready for a new Recruence setup. Recruence creates its tables when you confirm setup." };
  }
  if (path === "/api/setup/database") {
    if (body.choice === "current") st.database = null;
    else if (looksBad(body.url)) throw new ApiError(`${SIMULATED}couldn't connect: connection timeout expired`);
    else st.database = { database: "PostgreSQL at example-host:5432/mia", data: body.data || "copy" };
    return previewStatus();
  }
  if (path === "/api/config/test-ai") {
    const key = body.api_key || "";
    const name = { gemini: "Gemini", claude: "Claude", openai: "OpenAI", ollama: "Ollama" }[body.provider];
    if (!key && ["gemini", "claude"].includes(body.provider)) return check("ai", false, "add the API key first.", { reason: "missing_key", provider: body.provider, models: [] });
    if (looksBad(key) || looksBad(body.base_url)) return check("ai", false, `${name} rejected the key (401).`, { reason: "invalid_key", provider: body.provider, models: [] });
    return { ...check("ai", true, `connected to ${name}; 12 example models (no request was sent).`, { reason: "", provider: body.provider }), models: [body.model || "example-model"] };
  }
  if (path === "/api/config" && method === "PUT") {
    const errors = {};
    for (const [key, value] of Object.entries(body.changes || {})) {
      const f = previewField(key);
      if (!f) continue;
      if (key === "public_base_url" && value && !/^https:\/\//.test(value)) { errors[key] = "must be an https:// address"; continue; }
      if (f.kind === "secret") { f.set = Boolean(value); f.bad = /bad/i.test(value); } else f.value = value;  // only "set" is kept, as on the server
      f.source = "saved";
      if (key === "llm_provider") st.aiName = value === "offline" || value === "auto" ? null : `${value}:${previewField(`${value === "claude" ? "claude" : value}_model`)?.value || "example-model"}`;
    }
    if (Object.keys(errors).length) { const e = new ApiError("Fix the highlighted settings."); e.errors = errors; throw e; }
    return setupPreview.cfg;
  }
  if (path === "/api/settings/check/meetstream") {
    const set = previewField("meetstream_api_key").set;
    if (!set) return check("meetstream", false, "add the MeetStream API key first.");
    return previewField("meetstream_api_key").bad ? check("meetstream", false, "MeetStream refused the API key ([403] Forbidden).")
      : check("meetstream", true, "MeetStream accepted the API key (no request was sent).");
  }
  if (path === "/api/settings/check/public_url") {
    const url = previewField("public_base_url").value;
    if (!url) return check("public_url", false, "PUBLIC_BASE_URL isn't set.");
    if (looksBad(url)) return check("public_url", false, `${url} doesn't resolve.`);
    return check("public_url", true, `${url} reaches this app (no request was sent).`);
  }
  if (path === "/api/settings/check/email") {
    if (!previewField("smtp_host").value) return check("email", false, "email isn't set up: add the SMTP server and From address.");
    if (looksBad(previewField("smtp_host").value) || !body.to) return check("email", false, "the SMTP server refused the login.");
    return check("email", true, `test email to ${body.to} (nothing was sent).`);
  }
  if (path === "/api/setup/complete") {
    // The same rule as the server's: a deployment type, access and an AI choice before finishing.
    const s = previewStatus().steps;
    const missing = [!st.mode && "Deployment type", s.access.status !== "done" && "Access", s.ai.status !== "done" && "AI provider"].filter(Boolean);
    if (missing.length) throw new ApiError(`${SIMULATED}finish these steps first: ${missing.join(", ")}`);
    return { completed: true, restart_required: Boolean(st.database), preview: true };
  }
  return undefined;
}

// ---------- first-run setup (app/setup.py): a full-page wizard over the same settings as the Settings page ----------

let setupPending = false;
const SETUP_STEPS = [
  ["welcome", "Welcome"], ["database", "Database"], ["access", "Account"], ["ai", "AI provider"],
  ["meetstream", "MeetStream"], ["email", "Email"], ["careers", "Careers site"], ["review", "Review"],
];
let setupStep = null;
let setupProvider = null; // the provider open on the AI step, kept across the step's redraws

function enterSetupMode(on) {
  document.body.classList.toggle("setup-mode", on);
  $("#setup-root").hidden = !on;
}

// Who may run setup: a browser away from the server enters the code from the server log first.
function renderSetupCode(root) {
  const code = el("input", { type: "text", inputmode: "text", autocomplete: "one-time-code", maxlength: 12, placeholder: "ABCD-1234", "aria-label": "Setup code" });
  const error = notice("bad"); error.hidden = true;
  const go = el("button", { class: "btn primary block", type: "submit" }, "Continue");
  const form = el("form", { class: "setup-card narrow stack" },
    el("h1", {}, "Set up Recruence"),
    el("p", { class: "muted flush" }, "Enter the setup code the server printed when it started: in the terminal running it, or with Docker:"),
    el("pre", { class: "setup-cmd" }, "docker compose logs app | grep \"setup code\""),
    el("label", { class: "field" }, "Setup code", code), error, go);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    const done = busy(go, "Checking…");
    try { await api("/api/setup/unlock", { method: "POST", body: { code: code.value } }); await renderSetup(); } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
  });
  root.replaceChildren(el("div", { class: "setup-page" }, el("header", { class: "setup-top" }, el("span", { class: "wordmark" }, "Recruence")), form));
  code.focus();
}

async function renderSetupPreview() {
  startSetupPreview(await api("/api/config"));
  setupStep = "welcome";
  setupProvider = null;
  await renderSetup("welcome");
}

function endSetupPreview(message) {
  setupPreview = null;
  setupStep = null;
  enterSetupMode(false);
  if (message) toast(message, "good", 6000);
  location.hash = "#/";
  route();
}

async function renderSetup(goTo = null) {
  const root = $("#setup-root");
  enterSetupMode(true);
  const status = await api("/api/setup/status");
  if (status.completed) { setupPending = false; enterSetupMode(false); location.hash = "#/"; route(); return; }
  if (!status.signed_in) {
    if (status.code_required) { renderSetupCode(root); return; }
    enterSetupMode(false);
    // The connected database already has accounts: its administrator signs in, then setup carries on.
    renderLogin(status.sign_in_required ? "This database already has accounts. Sign in with an administrator account to finish setting up." : null);
    return;
  }
  const kit = makeKit(await api("/api/config"), true);
  const steps = status.steps;
  const order = SETUP_STEPS.map(([k]) => k);
  const firstOpen = order.find((k) => k !== "review" && steps[k] && steps[k].status === "incomplete") || "review";
  setupStep = goTo || setupStep || (status.mode ? firstOpen : "welcome");
  const index = order.indexOf(setupStep);
  const go = (k) => renderSetup(k);
  const next = () => go(order[Math.min(index + 1, order.length - 1)]);
  const back = index > 0 ? el("button", { class: "btn", type: "button", onclick: () => go(order[index - 1]) }, "Back") : null;

  const stepper = el("ol", { class: "setup-steps", "aria-label": "Setup steps" }, SETUP_STEPS.map(([k, label], n) => {
    const s = k === "review" ? "" : (steps[k] || {}).status;
    const state = k === setupStep ? "current" : s === "done" ? "done" : s === "failed" ? "failed" : s === "skipped" ? "skipped" : "";
    return el("li", { class: state, "aria-current": k === setupStep ? "step" : null },
      el("button", { type: "button", onclick: () => go(k), disabled: !status.preview && !status.mode && k !== "welcome" },
        el("span", { class: "dot" }, state === "done" ? icon("check") : String(n + 1)), el("span", { class: "label" }, label)));
  }));

  const body = SETUP_RENDER[setupStep](status, kit, { go, next, back, refresh: () => renderSetup(setupStep) });
  const banner = status.preview ? el("div", { class: "setup-preview-banner", role: "status" },
    el("span", {}, el("strong", {}, "Preview. "), "Nothing is saved and every check is simulated. Type “bad” into a field to see failures."),
    el("button", { class: "btn small", type: "button", onclick: () => endSetupPreview() }, "Exit preview")) : null;
  root.replaceChildren(el("div", { class: "setup-page" },
    el("header", { class: "setup-top" }, el("span", { class: "wordmark" }, "Recruence"),
      el("span", { class: "muted small" }, `Step ${index + 1} of ${order.length}`)),
    banner, stepper, body));
  const first = body.querySelector("input:not([type=hidden]), select, button.primary");
  if (first && setupStep !== "review") first.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

// "I already have an account" (from the setup's Welcome step): connect the database the account lives in, sign in,
// and the installation uses that database as it is (its settings come with it). Two steps on one page.
function renderExistingAccount() {
  const root = $("#setup-root");
  enterSetupMode(true);
  const error = notice("bad"); error.hidden = true;
  const fail = (err) => { error.textContent = err.message; error.hidden = false; };
  const url = el("input", { type: "password", autocomplete: "off", placeholder: "postgresql://user:password@host:5432/database", "aria-label": "Connection string" });
  const email = el("input", { type: "email", autocomplete: "username", maxlength: 320 });
  const password = el("input", { type: "password", autocomplete: "current-password", maxlength: 200 });
  const connected = el("div", { hidden: true });
  const signInPart = el("div", { class: "stack", hidden: true },
    el("h2", {}, "2. Sign in"),
    el("div", { class: "form-grid" }, el("label", { class: "field" }, "Email", email), el("label", { class: "field" }, "Password", password)));
  const connect = el("button", { class: "btn primary", type: "button", onclick: async () => {
    error.hidden = true;
    if (!url.value.trim()) { fail(new Error("Paste the connection string of the database your account is in.")); return; }
    const done = busy(connect, "Checking…");
    try {
      // Only a check: nothing is created or changed until the person confirms.
      const t = await api("/api/database/test", { method: "POST", body: { url: url.value.trim() } });
      if (!t.ok) throw new Error(t.message);
      if (t.state !== "mia") { offerSetup(t); done(); return; }
      const r = await api("/api/setup/database", { method: "POST", body: { choice: "postgresql", url: url.value.trim(), data: "use_existing" } });
      if (!r.has_accounts) { offerSetup({ ...t, noAccounts: true }); done(); return; }
      connected.replaceChildren(stateRow("Existing Recruence installation", r.steps.database.pending ? r.steps.database.pending.database : t.database, ["ready", "Data kept"]));
      connected.hidden = false;
      urlPart.hidden = true;
      signInPart.hidden = false;
      signIn.hidden = false;
      connect.hidden = true;
      email.focus();
    } catch (err) { fail(err); done(); }
  } }, "Connect");
  const signIn = el("button", { class: "btn primary", type: "button", hidden: true, onclick: async () => {
    error.hidden = true;
    const done = busy(signIn, "Signing in…");
    try {
      await api("/api/auth/account-login", { method: "POST", body: { email: email.value, password: password.value } });
      await api("/api/setup/existing-account", { method: "POST" });
      setupPending = false;
      await enterApp();
    } catch (err) { fail(err); done(); }
  } }, "Sign in");
  const urlPart = el("div", { class: "stack" },
    el("h2", {}, "1. Your database"),
    el("label", { class: "field" }, el("span", {}, "Connection string ", el("span", { class: "hint" }, "Neon, Supabase (Session pooler), Railway, RDS or your own PostgreSQL")), url),
    el("p", { class: "muted small flush" }, "It's checked first. An existing Recruence installation is used as it is; an empty database can be set up. Nothing in it is ever dropped, cleared or overwritten."));
  const back = el("button", { class: "btn", type: "button", onclick: () => renderSetup("welcome") }, "Back");
  // No account to sign in to: an empty database (expected for a first setup) or an installation without accounts.
  const setupPart = el("div", { class: "stack", hidden: true });
  function offerSetup(t) {
    const start = el("button", { class: "btn primary", type: "button", onclick: async () => {
      error.hidden = true;
      const done = busy(start, "Setting up…");
      try {
        await api("/api/setup/database", { method: "POST", body: { choice: "postgresql", url: url.value.trim(), data: t.state === "mia" ? "use_existing" : "copy" } });
        toast(t.state === "mia" ? "Connected. Its data is kept; create the first account next." : "Recruence's tables were created in the database. Continue the setup.", "good", 7000);
        setupStep = null;
        renderSetup();
      } catch (err) { fail(err); done(); }
    } }, t.state === "mia" ? "Continue setup with this installation" : "Set up Recruence in this database");
    setupPart.replaceChildren(
      stateRow(t.state === "mia" ? "Existing Recruence installation" : "Ready for a new setup", t.database, t.state === "mia" ? ["ready", "Data kept"] : ["ready", "Empty"]),
      notice("info", t.noAccounts ? `${t.summary} It has no accounts yet, so there's nothing to sign in to: continue the setup to create the first one.`
        : `${t.summary} There are no accounts in it yet, so there's nothing to sign in to. Set Recruence up in it instead: you'll create the first account.`),
      el("div", { class: "row" }, start));
    setupPart.hidden = false;
    urlPart.hidden = true;
    connect.hidden = true;
  }
  root.replaceChildren(el("div", { class: "setup-page" },
    el("header", { class: "setup-top" }, el("span", { class: "wordmark" }, (health && health.app_name) || "Recruence")),
    el("section", { class: "setup-card narrow stack" }, el("h1", {}, "Sign in with your account"),
      urlPart, connected, setupPart, signInPart, error, setupFooter(back, connect, signIn))));
  [url, email, password].forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") (signIn.hidden ? connect : signIn).click(); }));
  url.focus();
}

function setupCard(title, ...children) { return el("section", { class: "setup-card stack" }, el("h1", {}, title), ...children); }
function setupFooter(...buttons) { return el("div", { class: "setup-footer" }, ...buttons.filter(Boolean)); }
function stateRow(label, value, kind) {
  return el("div", { class: "setup-row" }, el("span", {}, label), el("span", { class: "row" }, value, kind ? pill(kind[0], kind[1]) : null));
}

const SETUP_RENDER = {
  welcome(status, kit, nav) {
    const mode = el("div", { class: "mode-cards", role: "radiogroup", "aria-label": "Deployment" },
      [["personal", "Personal", "Just me, on this computer or my own server."], ["team", "Team", "Shared by several recruiters, behind your own domain or proxy."]].map(([v, t, d]) =>
      el("label", { class: "mode-card" }, el("input", { type: "radio", name: "setup-mode", value: v, checked: status.mode === v }), el("span", {}, el("strong", {}, t), el("span", { class: "small muted" }, d)))));
    const error = notice("bad"); error.hidden = true;
    const cont = el("button", { class: "btn primary", type: "button", onclick: async () => {
      const chosen = (mode.querySelector("input:checked") || {}).value;
      if (!chosen) { error.textContent = "Choose how this install is used."; error.hidden = false; return; }
      const done = busy(cont, "…");
      try { await api("/api/setup/state", { method: "PUT", body: { mode: chosen } }); nav.next(); } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
    } }, "Continue");
    return setupCard("Welcome",
      el("img", { class: "setup-logo", src: "/static/logo.png", alt: "Recruence" }),
      el("p", { class: "muted flush" }, "Connect your database, AI provider, meetings and email."),
      el("div", { class: "field" }, el("span", {}, "How will this install be used?"), mode),
      status.preview ? null : el("div", { class: "existing-account" },
        el("span", {}, el("strong", {}, "Already have an account? "), "Connect the database it's in and sign in. Nothing else to set up."),
        el("button", { class: "btn", type: "button", onclick: () => renderExistingAccount() }, "I already have an account")),
      error, setupFooter(null, cont));
  },

  access(status, kit, nav) {
    const a = status.steps.access;
    const error = notice("bad"); error.hidden = true;
    const cont = el("button", { class: "btn primary", type: "button", disabled: a.status !== "done", onclick: nav.next }, "Continue");
    if (a.method === "proxy" || a.token_source === "env" || (a.method === "token" && !a.accounts)) {
      return setupCard("Account",
        a.method === "proxy" ? stateRow("Sign-in", "Identity proxy (PROXY_AUTH_SECRET)", ["ready", "Set"]) : stateRow("Access token", a.token_source === "env" ? "APP_ACCESS_TOKEN in the environment" : "Created", ["ready", "Set"]),
        setupFooter(nav.back, el("button", { class: "btn primary", type: "button", onclick: nav.next }, "Continue")));
    }
    if (a.accounts && a.signed_in_account) {
      return setupCard("Account", stateRow("Signed in", `${(authInfo && authInfo.user && authInfo.user.email) || "Administrator"}`, ["ready", "Owner"]),
        el("p", { class: "muted small flush" }, "Teammates join later with a code you approve."),
        setupFooter(nav.back, cont));
    }
    if (a.accounts) {
      // The connected database already has accounts: its administrator signs in.
      const email = el("input", { type: "email", autocomplete: "username", maxlength: 320 });
      const password = el("input", { type: "password", autocomplete: "current-password", maxlength: 200 });
      const signIn = el("button", { class: "btn primary self-start", type: "button", onclick: async () => {
        error.hidden = true;
        const done = busy(signIn, "Signing in…");
        try {
          await api("/api/auth/account-login", { method: "POST", body: { email: email.value, password: password.value } });
          await refreshAuth();
          nav.refresh();
        } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
      } }, "Sign in");
      return setupCard("Account", notice("info", "This database already has accounts. Sign in with an administrator account from it."),
        el("div", { class: "form-grid" }, el("label", { class: "field" }, "Email", email), el("label", { class: "field" }, "Password", password)),
        signIn, error, setupFooter(nav.back, cont));
    }
    const name = el("input", { type: "text", maxlength: 200, value: status.admin_name || "", autocomplete: "name" });
    const email = el("input", { type: "email", maxlength: 320, value: status.admin_email || "", autocomplete: "email" });
    const password = el("input", { type: "password", maxlength: 200, autocomplete: "new-password", minlength: 8 });
    const confirm = el("input", { type: "password", maxlength: 200, autocomplete: "new-password" });
    const wsName = el("input", { type: "text", maxlength: 120, placeholder: status.mode === "team" ? "Your company or team" : "My recruiting" });
    const create = el("button", { class: "btn primary self-start", type: "button", onclick: async () => {
      error.hidden = true;
      if (password.value.length < 8) { error.textContent = "Use at least 8 characters for the password."; error.hidden = false; return; }
      if (password.value !== confirm.value) { error.textContent = "The two passwords don't match."; error.hidden = false; return; }
      const done = busy(create, "Creating…");
      try {
        await api("/api/auth/register", { method: "POST", body: { name: name.value, email: email.value, password: password.value, workspace_name: wsName.value } });
        if (!setupPreview) await refreshAuth();
        nav.refresh();
      } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
    } }, "Create account");
    const others = a.local_request && status.mode === "personal" ? el("details", { class: "small" }, el("summary", {}, "Other ways to sign in"),
      el("div", { class: "stack gap-top" },
        el("p", { class: "muted flush" }, "On your own computer you can skip accounts."),
        a.local_request && status.mode === "personal" ? el("button", { class: "btn small self-start", type: "button", onclick: async () => {
          try { await api("/api/setup/access", { method: "POST", body: { method: "local_only" } }); nav.next(); } catch (err) { error.textContent = err.message; error.hidden = false; }
        } }, "No sign-in: only this computer can open it") : null)) : null;
    return setupCard("Create your account",
      el("p", { class: "muted flush small" }, "You'll own the first workspace and manage this install."),
      el("div", { class: "form-grid" },
        el("label", { class: "field" }, "Full name", name), el("label", { class: "field" }, "Email", email),
        el("label", { class: "field" }, "Password", password), el("label", { class: "field" }, "Confirm password", confirm),
        el("label", { class: "field" }, el("span", {}, "Workspace name ", el("span", { class: "hint" }, "(optional)")), wsName)),
      create, error, others, setupFooter(nav.back, cont));
  },

  database(status, kit, nav) {
    const d = status.steps.database;
    const error = notice("bad"); error.hidden = true;
    if (d.env_locked) {
      return setupCard("Database", stateRow("In use", d.current, ["draft", "DATABASE_URL"]),
        setupFooter(nav.back, el("button", { class: "btn primary", type: "button", onclick: nav.next }, "Continue")));
    }
    // A team shares one hosted database; the local SQLite file is for one person on this computer.
    const teamOnly = Boolean(d.team_needs_hosted) || (status.mode === "team" && d.backend === "sqlite");
    let choice = d.pending || teamOnly ? "postgresql" : "current";
    const url = el("input", { type: "password", autocomplete: "off", placeholder: "postgresql://user:password@host:5432/database", "aria-label": "PostgreSQL connection URL" });
    const useExisting = el("input", { type: "checkbox", id: "db-existing" });
    const existingBox = el("label", { class: "check", for: "db-existing", hidden: true }, useExisting, el("span", {}, "Use the Recruence data already in that database as it is. Nothing is copied, changed or deleted; its own accounts sign in."));
    const hosted = el("div", { class: "stack", hidden: choice !== "postgresql" },
      d.pending ? stateRow("Connected", d.pending.database, ["ready", d.pending.data === "use_existing" ? "Existing Recruence installation" : "New setup"]) : null,
      el("label", { class: "field" }, el("span", {}, d.pending ? "Connect a different database" : "Connection string"), url),
      existingBox);
    const cards = el("div", { class: "mode-cards", role: "radiogroup", "aria-label": "Where data is stored" },
      [...(teamOnly ? [] : [["current", d.backend === "sqlite" ? "Local (SQLite)" : "Current database", d.current]]),
        ["postgresql", "Hosted PostgreSQL", "Neon, Supabase, RDS or your own server"]].map(([v, t, sub]) =>
        el("label", { class: "mode-card" }, el("input", { type: "radio", name: "setup-db", value: v, checked: choice === v, onchange: () => { choice = v; hosted.hidden = v !== "postgresql"; } }),
          el("span", {}, el("strong", {}, t), el("span", { class: "small muted break" }, sub)))));
    const test = el("button", { class: "btn", type: "button", onclick: async () => {
      error.hidden = true;
      const done = busy(test, "Testing…");
      try {
        const r = await api("/api/database/test", { method: "POST", body: { url: url.value.trim() } });
        error.className = `notice ${r.ok ? "good" : "bad"}`; error.textContent = r.ok ? `${r.message} ${r.summary}` : r.message; error.hidden = false;
        existingBox.hidden = !(r.ok && r.state === "mia");
      } catch (err) { error.className = "notice bad"; error.textContent = err.message; error.hidden = false; }
      done();
    } }, "Test connection");
    hosted.append(el("div", { class: "row" }, test));
    const cont = el("button", { class: "btn primary", type: "button", onclick: async () => {
      error.hidden = true;
      const done = busy(cont, "Saving…");
      try {
        if (choice === "current") await api("/api/setup/database", { method: "POST", body: { choice: "current" } });
        else if (url.value.trim()) {
          const r = await api("/api/setup/database", { method: "POST", body: { choice: "postgresql", url: url.value.trim(), data: useExisting.checked ? "use_existing" : "copy" } });
          if (r.has_accounts) toast("Connected to the existing Recruence installation. It already has accounts: sign in with its administrator next.", "good", 8000);
          else if (r.database_state && r.database_state !== "mia") toast("Recruence's tables were created in the new database.", "good", 6000);
        }
        else if (!d.pending) throw new Error("Enter the connection string, or keep the local database.");
        nav.next();
      } catch (err) { error.className = "notice bad"; error.textContent = err.message; error.hidden = false; done(); }
    } }, "Continue");
    return setupCard("Database",
      teamOnly ? notice("info", "A team installation needs a hosted PostgreSQL database that every member reaches. The local database is only for one person on this computer: choose Personal on the Welcome page for that.") : null,
      cards, hosted, error, setupFooter(nav.back, cont));
  },

  ai(status, kit, nav) {
    const a = status.steps.ai;
    kit.openProvider = setupProvider;
    kit.redraw = () => { setupProvider = kit.openProvider; nav.refresh(); };
    const check = a.check;
    const line = a.configured ? notice("good", `In use: ${a.name}.`)
      : check && check.ok === false ? notice("bad", `${{ missing_key: "No API key", invalid_key: "Key rejected", model_unavailable: "Model not available", unreachable: "Unreachable" }[check.reason] || "Problem"}: ${check.message}`)
        : null;
    const offline = el("button", { class: "link small", type: "button", onclick: async () => {
      await api("/api/setup/state", { method: "PUT", body: { ai_offline: true } }); nav.next();
    } }, "Continue without AI (template plans, no evaluations)");
    const cont = el("button", { class: "btn primary", type: "button", disabled: a.status !== "done", onclick: nav.next }, "Continue");
    return setupCard("AI provider",
      el("p", { class: "muted flush small" }, "Writes plans, screens resumes and evaluates interviews."),
      ...kit.providersView({ compact: true }).map((n) => { n.classList.remove("panel"); return n; }),
      line, setupFooter(nav.back, a.configured ? null : offline, cont));
  },

  meetstream(status, kit, nav) {
    const m = status.steps.meetstream;
    const line = (c) => (c ? [pill(c.ok ? "ready" : "failed", c.ok ? "Works" : "Problem"), " ", c.message] : []);
    const cont = el("button", { class: "btn primary", type: "button", disabled: m.status !== "done", onclick: nav.next }, "Continue");
    // One field, one button: it saves the value, then checks what was saved. Continue follows the saved values only,
    // so nothing typed and checked can be lost by moving on without saving.
    const field = (key, check, label, attrs) => {
      const f = kit.fieldOf(key) || {};
      const secret = f.kind === "secret";
      const input = el("input", { id: `ms-${key}`, autocomplete: secret ? "new-password" : "off", ...attrs,
        type: secret ? "password" : "url", value: secret ? "" : f.value || "", placeholder: secret ? (f.set ? "Saved · type to replace" : "") : attrs.placeholder });
      const result = el("p", { class: "small flush check-line", "aria-live": "polite" }, ...line(check === "meetstream" ? m.check : m.url_check));
      const go = el("button", { class: "btn", type: "button", onclick: async () => {
        const value = input.value.trim();
        if (!value && !(secret ? f.set : f.value)) { result.replaceChildren(...line({ ok: false, message: `Enter the ${label.toLowerCase()} first.` })); input.focus(); return; }
        const done = busy(go, value ? "Saving…" : "Checking…");
        try {
          if (value && (secret || value !== f.value)) {
            await api("/api/config", { method: "PUT", body: { changes: { [key]: value } } });
            if (secret) { f.set = true; input.value = ""; input.placeholder = "Saved · type to replace"; } else f.value = value;
          }
          result.replaceChildren(...line(await api(`/api/settings/check/${check}`, { method: "POST", body: {} })));
          cont.disabled = (await api("/api/setup/status")).steps.meetstream.status !== "done";
        } catch (err) { result.replaceChildren(...line({ ok: false, message: err.message })); }
        done();
      } }, "Save and check");
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go.click(); } });
      return el("div", { class: "stack-tight" }, el("label", { class: "field", for: input.id }, label), el("div", { class: "field-action" }, input, go), result);
    };
    const skip = el("button", { class: "btn", type: "button", onclick: async () => {
      await api("/api/setup/state", { method: "PUT", body: { skip: "meetstream" } }); nav.next();
    } }, "Skip for now");
    return setupCard("MeetStream",
      el("p", { class: "muted flush small" }, "Brings the AI interviewer into Google Meet, Zoom and Teams."),
      field("meetstream_api_key", "meetstream", "API key", {}),
      field("public_base_url", "public_url", "Public URL", { placeholder: "https://interviews.example.com" }),
      el("p", { class: "muted flush small" }, "The address MeetStream and candidates use to reach this app: your domain or a tunnel."),
      setupFooter(nav.back, m.status === "done" ? null : skip, cont));
  },

  email(status, kit, nav) {
    const m = status.steps.email;
    kit.redraw = () => nav.refresh();
    const changes = {};
    const form = el("form", { class: "stack", novalidate: true });
    const saveStatus = el("p", { class: "small flush", "aria-live": "polite" });
    const save = el("button", { class: "btn", type: "submit" }, "Save");
    const keys = EMAIL_KEYS;
    form.append(el("div", { class: "form-grid" }, ...keys.slice(0, 5).map((k) => kit.fieldRow(kit.fieldOf(k), changes))),
      ...keys.slice(5).map((k) => kit.fieldRow(kit.fieldOf(k), changes)), el("div", { class: "row" }, save, saveStatus));
    form.addEventListener("submit", (e) => { e.preventDefault(); kit.saveChanges(changes, form, saveStatus, save); });
    const to = el("input", { type: "email", placeholder: "you@example.com", value: status.admin_email || "", "aria-label": "Send a test email to", class: "inline" });
    const line = (c) => (c ? [pill(c.ok ? "ready" : "failed", c.ok ? "OK" : "Problem"), ` ${c.message}`] : []);
    const result = el("p", { class: "small flush", "aria-live": "polite" }, ...line(m.check));
    const cont = el("button", { class: "btn primary", type: "button", disabled: m.status !== "done", onclick: nav.next }, "Continue");
    // The test uses what the fields hold now, saved or not, and leaves the form as it is.
    const send = el("button", { class: "btn small", type: "button", onclick: async () => {
      const done = busy(send, "Sending…");
      try {
        const r = await api("/api/settings/check/email", { method: "POST", body: { to: to.value.trim(), values: kit.formValues(EMAIL_KEYS) } });
        result.replaceChildren(...line(r));
        cont.disabled = (await api("/api/setup/status")).steps.email.status !== "done";
      } catch (err) { result.replaceChildren(...line({ ok: false, message: err.message })); }
      done();
    } }, "Send a test email");
    const skip = el("button", { class: "btn", type: "button", onclick: async () => {
      await api("/api/setup/state", { method: "PUT", body: { skip: "email" } }); nav.next();
    } }, "Skip for now");
    return setupCard("Email",
      el("p", { class: "muted flush small" }, "Sends invitations, codes and report notes. Gmail: smtp.gmail.com, port 587, STARTTLS, and an App Password."),
      form, el("div", { class: "row" }, to, send), result,
      setupFooter(nav.back, m.status === "done" ? null : skip, cont));
  },

  careers(status, kit, nav) {
    const c = status.steps.careers || { status: "incomplete" };
    const cont = el("button", { class: "btn primary", type: "button", disabled: c.status !== "done", onclick: nav.next }, "Continue");
    const skip = el("button", { class: "btn", type: "button", onclick: async () => {
      await api("/api/setup/state", { method: "PUT", body: { skip: "careers" } }); nav.next();
    } }, "Skip for now");
    const box = careersSiteBox(true, () => { cont.disabled = false; skip.hidden = true; });
    return setupCard("Careers website",
      el("p", { class: "muted flush small" }, "Candidates apply on your company's own careers website, and Recruence screens every application. "
        + "Tell Recruence how the site is set up to see how to connect it. You can change this later in Settings → Recruitment sources."),
      box, setupFooter(nav.back, c.status === "done" ? null : skip, cont));
  },

  review(status, kit, nav) {
    const s = status.steps;
    const KIND = { done: ["ready", "Configured"], skipped: ["draft", "Skipped"], failed: ["failed", "Failed"], incomplete: ["scheduled", "Incomplete"] };
    const row = (step, label, value) => el("div", { class: "setup-row" },
      el("span", {}, el("strong", {}, label), el("span", { class: "muted small block" }, value)),
      el("span", { class: "row" }, pill(...(step === "ai" && s.ai.offline_chosen ? ["draft", "No AI"] : KIND[s[step].status])),
        el("button", { class: "link small", type: "button", onclick: () => nav.go(step) }, "Edit")));
    const error = notice("bad"); error.hidden = true;
    const finish = el("button", { class: "btn primary", type: "button", onclick: async () => {
      error.hidden = true;
      const done = busy(finish, "Finishing…");
      try {
        const r = await api("/api/setup/complete", { method: "POST" });
        if (r.preview) { endSetupPreview("Preview finished. Nothing was saved."); return; }
        setupPending = false;
        enterSetupMode(false);
        health = await api("/api/health").catch(() => health);  // setup saved MeetStream, the Public URL, email...
        if (r.restart_required) toast("Setup complete. Restart the app to switch to the new database.", "warn", 15000);
        else toast("Setup complete.", "good");
        api("/api/auth/status").then(renderUser).catch(() => {});
        location.hash = "#/";
        route();
      } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
    } }, "Finish setup");
    const pending = s.database.pending;
    return setupCard("Review",
      el("div", { class: "setup-summary" },
        row("access", "Account", s.access.method === "proxy" ? "Identity proxy" : s.access.method === "account" ? `Owner: ${status.admin_email || "account created"}`
          : s.access.method === "token" ? "Access token" : s.access.method === "local_only" ? "This computer only" : "Not set"),
        row("database", "Database", pending ? `${s.database.current} → ${pending.database} at Finish` : s.database.current),
        row("ai", "AI provider", s.ai.name || (s.ai.offline_chosen ? "None (template plans)" : "Not set")),
        row("meetstream", "MeetStream", s.meetstream.key_set ? `API key set · ${s.meetstream.public_base_url || "no public URL"}` : "Not set: interviews can't run in meetings"),
        row("email", "Email", s.email.configured ? `From ${s.email.from}` : "Not set: invitations aren't emailed"),
        row("careers", "Careers website", s.careers && s.careers.site === "own" ? "Has its own job pages: forwards applications"
          : s.careers && s.careers.site === "recruence" ? "Lists the jobs set up in Recruence" : "Not set")),
      error, setupFooter(nav.back, finish));
  },
};

// ---------- interview detail ----------

async function renderInterview(id) {
  // Fresh each time: whether live meetings can run depends on settings saved since the page loaded (setup, Settings).
  const [data, fresh] = await Promise.all([api(`/api/interviews/${id}`), api("/api/health").catch(() => null)]);
  if (fresh) health = fresh;
  const i = data.interview;
  const locked = ["scheduled", "in_progress"].includes(i.status);
  const parts = [
    pageHead({ backTo: i.role_id ? back(`#/r/${i.role_id}`, `${i.setup.role_title} (role)`) : back("#/", "Dashboard"),
      eyebrow: i.role_id ? "Role candidate" : "One-off interview", title: `${i.setup.candidate_name} · ${i.setup.role_title}`,
      sub: `${TYPE_LABEL[i.setup.interview_type]} interview · ${i.plan ? i.plan.questions.length : i.setup.question_count} questions · ~${i.setup.duration_minutes} min`,
      right: el("div", { class: "row" }, demoTag(i.demo), data.stage === "invitation_sent" && data.invitation_undelivered ? pill("warn", "Invitation not delivered") : pill(data.stage, STAGE_LABEL[data.stage])) }),
    progressSteps(i, data),
  ];
  if (i.cancelled_at) parts.push(cancelledPanel(i));
  else if (data.stage === "expired") parts.push(notice("warn", "The candidate didn't choose a time before the booking link expired. Withdraw it and offer new times, or cancel the interview."));
  if (i.last_error) parts.push(notice("bad", i.last_error));
  if (data.live_session_id) {
    parts.push(el("div", { class: "panel spread" },
      el("div", { class: "row" }, el("span", { class: "live-dot", "aria-hidden": "true" }),
        el("strong", {}, "A session is live")),
      el("a", { class: "btn primary", href: `#/s/${data.live_session_id}` }, icon("live"), "Open live session")));
  }
  parts.push(planPanel(i, locked, { base: `/api/interviews/${i.id}`, reload: () => renderInterview(i.id), fromRole: i.role_id }));
  if (i.invitation) parts.push(interviewLinkPanel(i, data));
  if (!i.cancelled_at) {
    const launch = i.plan_approved_at ? launchPanel(i, locked) : null;
    parts.push(formatPanel(i, locked, (o) => { if (launch && launch.updateOptions) launch.updateOptions(o); }));
    if (launch) parts.push(launch, bookingPanel(i, data.booking_url), invitationPanel(i));
  }
  if (data.sessions.length) parts.push(sessionsPanel(data.sessions));
  if (i.status === "completed") parts.push(decisionPanel(i));
  parts.push(setupPanel(i, locked));
  if (data.meeting && !i.demo && !["completed", "in_progress"].includes(i.status)) parts.splice(1, 0, meetingPanel(i, data.meeting));
  const open = (i.flags || []).filter((f) => !f.resolved_at);
  if ((i.flags || []).length) {
    const review = el("button", { class: "btn small", type: "button", onclick: async () => {
      try { await api(`/api/interviews/${i.id}/flags/resolve`, { method: "POST" }); toast("Marked as reviewed.", "good", 2500); route(); } catch (err) { toast(err.message); }
    } }, "Mark as reviewed");
    const FLAG_TEXT = { second_device: "Second device", code_failures: "Wrong codes", agent_silent_start: "AI didn't start", recording_not_allowed: "Recording not allowed",
      question_stalled: "AI stuck on a question", candidate_asked_to_stop: "Candidate asked to stop" };
    const linkFlags = i.flags.some((f) => ["second_device", "code_failures"].includes(f.kind));
    parts.splice(1, 0, el("section", { class: "panel stack" },
      el("div", { class: "spread" }, sectionTitle("shield", linkFlags ? "Interview link activity" : "To review"), open.length ? review : pill("ready", "Reviewed")),
      el("ul", { class: "plain small stack-tight" }, i.flags.map((f) => el("li", {}, pill(f.resolved_at ? "draft" : "scheduled", FLAG_TEXT[f.kind] || f.kind), " ", f.detail,
        el("span", { class: "muted" }, ` · ${fmtTime(f.at)}${f.resolved_at ? ` · reviewed by ${f.resolved_by}` : ""}`)))),
      linkFlags ? el("p", { class: "muted small flush" }, "Signals, not proof: someone may simply have switched devices. Check with the candidate before deciding anything.") : null));
  }
  parts.push(dangerPanel(i, Boolean(data.live_session_id)));
  parts.push(activityPanel(data.audit));
  view.replaceChildren(el("div", { class: "stack" }, parts));
}

// Where the interview is in the workflow: set up, plan approved, launched, report ready, decided.
function progressSteps(i, data) {
  const steps = [
    ["Set up", true],
    ["Plan approved", Boolean(i.plan_approved_at)],
    ["Launched", ["scheduled", "in_progress", "completed"].includes(i.status) || data.sessions.length > 0],
    ["Report ready", i.status === "completed"],
    ["Decision", Boolean(i.decision)],
  ];
  const current = steps.findIndex(([, done]) => !done);
  return el("ol", { class: "panel steps", "aria-label": "Interview progress" }, steps.map(([label, done], n) =>
    el("li", { class: done ? "done" : n === current ? "current" : null, "aria-current": n === current ? "step" : null },
      el("span", { class: "dot" }, done ? icon("check") : String(n + 1)), label)));
}

// The plan editor, for an interview or a role. opts.base is its API path, opts.reload redraws the page;
// opts.fromRole marks a role's candidate interview, whose questions are the role's and read-only here.
function planPanel(i, locked, opts) {
  const { base, reload, fromRole = null, title = "Interview plan" } = opts;
  locked = locked || Boolean(fromRole);
  const panel = el("section", { class: "panel stack" });
  const regenerate = el("button", { class: "btn", disabled: locked, hidden: Boolean(fromRole), onclick: async () => {
    if (i.plan && !(await confirmDialog("Replace the current plan with a newly generated one? Your edits to the questions are lost.", { title: "Regenerate the plan", confirmLabel: "Regenerate" }))) return;
    const done = busy(regenerate, "Generating…");
    try {
      const res = await api(`${base}/plan/generate`, { method: "POST" });
      if (res.warning) toast(`The AI model failed, so a template plan was used instead:\n${res.warning}`, "warn", 12000);
      reload();
    } catch (err) { toast(err.message); done(); }
  } }, icon("spark"), i.plan ? "Regenerate" : "Generate plan");

  if (!i.plan) {
    panel.append(el("div", { class: "spread" }, sectionTitle("doc", title), regenerate),
      el("p", { class: "muted" }, "No questions yet. Generate them from the job description and criteria."));
    return panel;
  }
  const plan = structuredClone(i.plan);
  const approved = Boolean(i.plan_approved_at);
  let dirty = false;
  const save = el("button", { class: "btn", disabled: true, hidden: Boolean(fromRole) }, "Save changes");
  const approve = el("button", { class: "btn primary", disabled: approved || locked, hidden: Boolean(fromRole) }, approved ? "Approved" : "Approve plan");
  const markDirty = () => { dirty = true; save.disabled = locked; approve.disabled = true; approve.textContent = "Save before approving"; };
  const questionsBox = el("div");

  const drawQuestions = () => {
    questionsBox.replaceChildren(...plan.questions.map((q, n) => {
      const text = el("textarea", { rows: 2, disabled: locked, oninput: (e) => { q.text = e.target.value; markDirty(); } }, q.text);
      const lookFor = el("textarea", { rows: 2, disabled: locked, placeholder: "One point per line",
        oninput: (e) => { q.look_for = e.target.value.split("\n").map((x) => x.trim()).filter(Boolean); markDirty(); } }, q.look_for.join("\n"));
      const maxFu = el("input", { type: "number", min: 0, max: 5, disabled: locked, placeholder: "default", value: q.max_follow_ups ?? "",
        oninput: (e) => { q.max_follow_ups = e.target.value === "" ? null : Number(e.target.value); markDirty(); } });
      const comps = el("div", { class: "comps" }, plan.competencies.map((c) => el("label", {},
        el("input", { type: "checkbox", disabled: locked, checked: q.competency_ids.includes(c.id), onchange: (e) => {
          q.competency_ids = e.target.checked ? [...q.competency_ids, c.id] : q.competency_ids.filter((x) => x !== c.id);
          markDirty();
        } }), c.name)));
      const move = (d) => () => { const j = n + d; [plan.questions[n], plan.questions[j]] = [plan.questions[j], plan.questions[n]]; markDirty(); drawQuestions(); };
      return el("div", { class: "q" },
        el("div", { class: "q-head" }, el("span", { class: "q-num", "data-n": String(n + 1) }, `Question ${n + 1}`),
          el("div", { class: "row" },
            el("button", { class: "btn small", disabled: locked || n === 0, onclick: move(-1), "aria-label": "Move up" }, "↑"),
            el("button", { class: "btn small", disabled: locked || n === plan.questions.length - 1, onclick: move(1), "aria-label": "Move down" }, "↓"),
            el("button", { class: "btn small danger", disabled: locked || plan.questions.length === 1, onclick: () => { plan.questions.splice(n, 1); markDirty(); drawQuestions(); } }, "Remove"))),
        el("label", { class: "field" }, "Asked word for word", text),
        el("div", { class: "form-grid" },
          el("label", { class: "field" }, el("span", {}, "Strong answer covers ", el("span", { class: "hint" }, "(private)")), lookFor),
          el("label", { class: "field" }, "Max follow-ups", maxFu)),
        comps);
    }));
  };
  drawQuestions();

  save.addEventListener("click", async () => {
    const done = busy(save, "Saving…");
    try { await api(`${base}/plan`, { method: "PUT", body: plan }); dirty = false; toast("Plan saved.", "good", 3000); reload(); }
    catch (err) { toast(err.message); done(); }
  });
  approve.addEventListener("click", async () => {
    if (dirty) return;
    const done = busy(approve, "Approving…");
    try { await api(`${base}/plan/approve`, { method: "POST" }); toast("Plan approved.", "good", 3000); reload(); }
    catch (err) { toast(err.message); done(); }
  });

  add(panel,
    el("div", { class: "spread" },
      el("div", {}, sectionTitle("doc", title), el("p", { class: "muted small flush-top" }, `Source: ${plan.source}`)),
      el("div", { class: "row" }, regenerate, save, approve)),
    fromRole ? notice("good", "The role's questions. ", el("a", { href: `#/r/${fromRole}` }, "Edit them on the role"), ".") : null,
    plan.source.includes("no AI") && !fromRole ? notice("warn", "Template plan: no AI model is configured, so the resume and job description were not analysed. Edit the questions before approving.") : null,
    approved && !fromRole ? notice("good", `Approved ${fmtTime(i.plan_approved_at)}. Editing the plan withdraws the approval.`) : null,
    plan.candidate_summary ? el("div", {}, el("h3", {}, "Candidate summary"), el("p", { class: "muted" }, plan.candidate_summary)) : null,
    (i.setup.requirements || []).length ? el("div", {}, el("h3", {}, "Must-have requirements"),
      el("ul", { class: "plain" }, i.setup.requirements.map((r) => el("li", {}, r)))) : null,
    el("div", {}, el("h3", {}, "Competencies"), el("div", { class: "chips" }, plan.competencies.map((c) =>
      el("div", { class: "chip" }, el("strong", {}, c.name), c.description ? el("span", { class: "muted" }, c.description) : null,
        c.rubric ? el("span", { class: "muted small" }, `Rubric: ${c.rubric}`) : null)))),
    el("div", {}, el("h3", {}, "Questions"),
      // Not part of the plan and not editable: every AI interview opens with it (app/planning.py INTRO_QUESTION).
      el("p", { class: "muted small flush" }, el("strong", {}, "First: "), "a self-introduction, with up to three follow-ups on what the candidate says."),
      questionsBox),
    locked ? null : el("button", { class: "btn small", onclick: () => {
      plan.questions.push({ id: `q${plan.questions.length + 1}`, text: "", competency_ids: [plan.competencies[0].id], look_for: [], max_follow_ups: null });
      markDirty(); drawQuestions();
    } }, icon("plus"), "Add question"),
  );
  return panel;
}

// A date and a time in 15-minute steps, in the viewer's own time zone (named on screen). value() gives a real
// instant (sent to the server as UTC) or an error to show: a past time, or a time that doesn't exist because
// the clocks change that night.
const TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
const HOUR_12 = ["h11", "h12"].includes(new Intl.DateTimeFormat(undefined, { hour: "numeric" }).resolvedOptions().hourCycle);
const FULL_TIME = { weekday: "short", day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" };

function timePicker({ label = "Time", optional = false, allowPast = false, emptyText = "" } = {}) {
  const p2 = (n) => String(n).padStart(2, "0");
  const today = new Date();
  const date = el("input", { type: "date", "aria-label": `${label}: date`, min: allowPast ? null : `${today.getFullYear()}-${p2(today.getMonth() + 1)}-${p2(today.getDate())}` });
  const hour = el("select", { "aria-label": `${label}: hour` },
    (HOUR_12 ? [12, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11] : Array.from({ length: 24 }, (_, h) => h)).map((h) =>
      el("option", { value: String(h), selected: h === 9 }, HOUR_12 ? String(h) : p2(h))));
  const minute = el("select", { "aria-label": `${label}: minutes` }, [0, 15, 30, 45].map((m) => el("option", { value: String(m) }, p2(m))));
  const ampm = HOUR_12 ? el("select", { "aria-label": `${label}: AM or PM` }, el("option", { value: "AM" }, "AM"), el("option", { value: "PM" }, "PM")) : null;
  const summary = el("p", { class: "tp-summary small", "aria-live": "polite" });
  const node = el("div", { class: "timepicker" },
    el("div", { class: "tp-row" }, date,
      el("div", { class: "tp-time" }, hour, el("span", { "aria-hidden": "true" }, ":"), minute, ampm),
      el("span", { class: "tp-zone", title: "Your time zone" }, TIME_ZONE)),
    summary);

  function value() {
    if (!date.value) return { at: null, error: optional ? null : `Choose a date for ${label.toLowerCase()}.` };
    const [y, mo, d] = date.value.split("-").map(Number);
    let h = Number(hour.value);
    if (HOUR_12) h = (h % 12) + (ampm.value === "PM" ? 12 : 0);
    const mi = Number(minute.value);
    const at = new Date(y, mo - 1, d, h, mi);
    if (at.getHours() !== h || at.getMinutes() !== mi) return { at: null, error: "That time doesn't exist on that date because the clocks change. Choose another time." };
    if (!allowPast && at <= new Date()) return { at: null, error: "That time has passed. Choose a time in the future." };
    return { at, error: null };
  }
  function refresh() {
    const { at, error } = value();
    summary.classList.toggle("bad", Boolean(error));
    summary.textContent = error || (at ? at.toLocaleString(undefined, FULL_TIME) : emptyText);
  }
  function set(when) {
    if (!when) { date.value = ""; refresh(); return; }
    const d = new Date(when);
    date.value = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
    const h = d.getHours();
    hour.value = String(HOUR_12 ? (h % 12 || 12) : h);
    if (ampm) ampm.value = h < 12 ? "AM" : "PM";
    const m = d.getMinutes();
    if (![0, 15, 30, 45].includes(m) && !minute.querySelector(`option[value="${m}"]`)) {
      minute.append(el("option", { value: String(m) }, p2(m))); // a calendar meeting at an odd minute keeps its time
    }
    minute.value = String(m);
    refresh();
  }
  [date, hour, minute, ampm].forEach((x) => x && x.addEventListener("change", refresh));
  refresh();
  return { node, value, set };
}

// "2026-11-03T10:00" in the browser's time zone, for a datetime-local input.
function localInputValue(iso) {
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

// Upcoming meetings from the calendar connected in MeetStream, loaded on request.
function calendarPicker(onPick) {
  // The connected calendar is the installation's (MeetStream's), not a workspace's: only its admins see it.
  if (user && user.via === "account" && !user.is_admin) return null;
  const status = el("span", { class: "muted small" });
  const synced = el("p", { class: "muted small flush", "aria-live": "polite" });
  const select = el("select", { "aria-label": "Upcoming meetings", hidden: true });
  // MeetStream answers from its own copy of the calendar; when that copy stops changing (its connection to Google or
  // Outlook broke), new meetings never arrive though every resync "succeeds" (R-10).
  const stale = notice("warn"); stale.hidden = true;
  // MeetStream also reports which connected calendars it couldn't read, e.g. a Google sign-in it can't refresh (R-14).
  const problems = notice("bad"); problems.hidden = true;
  const showProblems = (list) => {
    problems.hidden = !(list && list.length);
    if (list && list.length) problems.replaceChildren(...list.map((p) => el("p", { class: "flush" },
      el("strong", {}, `${p.provider} calendar${p.account ? ` (${p.account})` : ""}: `), p.advice,
      p.message ? el("span", { class: "muted small" }, ` MeetStream said: ${p.message}`) : null)));
  };
  const showStale = (s) => {
    stale.hidden = !s;
    if (s) stale.textContent = `MeetStream's copy of this calendar hasn't changed since ${fmtTime(s.since)} (${s.days} days). `
      + "If meetings were added since, they won't show here: reconnect the calendar in MeetStream, then resync.";
  };
  let meetings = [];
  const show = (res) => {
    showStale(res.stale);
    if ("problems" in res) showProblems(res.problems);
    meetings = res.events;
    if (!meetings.length) {
      select.hidden = true;
      status.textContent = "No upcoming Google Meet, Zoom or Teams meetings in the connected calendar.";
      return;
    }
    select.replaceChildren(el("option", { value: "" }, `Choose one of ${meetings.length} upcoming meetings`),
      ...meetings.map((m, n) => el("option", { value: String(n) }, `${fmtTime(m.start)} · ${m.title} · ${m.platform}`)));
    select.hidden = false;
    load.hidden = true;
    status.textContent = res.more ? "Showing the next few weeks." : "";
  };
  const showSync = (sync) => {
    if (sync && "stale" in sync) showStale(sync.stale);
    if (sync && "problems" in sync) showProblems(sync.problems);
    if (!sync) { synced.textContent = "Not resynced from this app yet. MeetStream also syncs on calendar changes and daily."; return; }
    synced.textContent = sync.ok
      ? `Last resynced ${fmtTime(sync.at)}.`
      : `The last resync failed (${fmtTime(sync.at)}): ${sync.message}${sync.last_ok_at ? ` Last successful: ${fmtTime(sync.last_ok_at)}.` : ""}`;
  };
  const load = el("button", { class: "btn small", type: "button", onclick: async () => {
    const done = busy(load, "Loading…");
    status.textContent = "";
    try { show(await api("/api/calendar/events")); } catch (err) { status.textContent = err.message; }
    done();
  } }, "Pick from your calendar");
  // Resync asks MeetStream to re-read the calendar from Google now. MeetStream only reads the calendar, so this
  // never creates or changes events and can't duplicate anything.
  const resync = el("button", { class: "btn small", type: "button", onclick: async () => {
    const done = busy(resync, "Syncing…");
    status.textContent = "";
    try {
      const res = await api("/api/calendar/resync", { method: "POST" });
      show(res);
      showSync(res.sync);
      toast(`Calendar resynced: ${res.events.length} upcoming meeting${res.events.length === 1 ? "" : "s"}.`, "good", 3000);
    } catch (err) {
      status.textContent = err.message;
      api("/api/calendar/status").then((r) => showSync(r.sync)).catch(() => {});
    }
    done();
  } }, "Resync calendar");
  api("/api/calendar/status").then((r) => showSync(r.sync)).catch(() => {});
  select.addEventListener("change", () => { if (select.value !== "") onPick(meetings[Number(select.value)]); });
  return el("div", { class: "field" },
    el("span", { class: "row" }, el("strong", { class: "small" }, "From your calendar"), load, resync, status),
    select,
    problems,
    stale,
    synced,
    el("p", { class: "muted small flush" }, "Fills in the link and start time. On Google Meet the host must have started the meeting by then, or the bot fails at once."));
}

function launchPanel(i, locked) {
  const panel = el("section", { class: "panel stack" }, sectionTitle("video", "Run the interview"));
  if (i.status === "scheduled") {
    const unschedule = el("button", { class: "btn", onclick: async () => {
      const done = busy(unschedule, "…");
      try { await api(`/api/interviews/${i.id}/unschedule`, { method: "POST" }); renderInterview(i.id); } catch (err) { toast(err.message); done(); }
    } }, "Unschedule");
    panel.append(el("div", { class: "notice info" }, `${(i.options || {}).mode === "analyst" ? "The meeting analyst" : "The AI interviewer"} will join ${i.platform} at `, el("strong", {}, fmtTime(i.scheduled_at)), " (while this server is running)."),
      el("p", { class: "muted small" }, i.meeting_link), unschedule);
    return panel;
  }
  if (locked) { panel.append(el("p", { class: "muted" }, "A session is in progress.")); return panel; }
  if (!health.meetstream.ready) {
    panel.append(notice("warn", `Live meetings are not configured: ${health.meetstream.reason}.`));
    return panel;
  }
  const link = el("input", { type: "url", placeholder: "https://meet.google.com/abc-defg-hij", value: i.meeting_link || "" });
  const hint = el("p", { class: "muted small flush" });
  // One card per supported platform; the one the link points to lights up.
  const cards = [["Google Meet", "video"], ["Zoom", "chat"], ["Microsoft Teams", "team"]].map(([name, ico]) => {
    const state = el("span", { class: "small" }, "Paste a link");
    const card = el("div", { class: "platform" }, el("span", { class: "p-ico" }, icon(ico)), el("div", {}, el("strong", {}, name), state));
    return { name, card, state };
  });
  const updateHint = () => {
    const p = platformHint(link.value.trim());
    hint.textContent = p ? `${p} detected.` : "Google Meet, Zoom or Microsoft Teams.";
    for (const c of cards) {
      c.card.classList.toggle("on", c.name === p);
      c.state.textContent = c.name === p ? "Detected from the link" : "Supported";
    }
  };
  link.addEventListener("input", updateHint);
  updateHint();
  const when = timePicker({ label: "Start time", optional: true, emptyText: "No time set: the bot is sent now." });
  const picker = calendarPicker((m) => {
    link.value = m.meeting_link;
    updateHint();
    // A meeting already under way starts now; otherwise at its start time.
    when.set(new Date(m.start) > new Date() ? m.start : null);
  });
  const consent = el("input", { type: "checkbox" });
  const consentText = el("span");
  const error = notice("bad"); error.hidden = true;
  const goLabel = el("span", {}, "Send AI interviewer");
  const go = el("button", { class: "btn primary" }, icon("live"), goLabel);

  // How it runs is set in the panel above (saved as soon as it changes); this only reflects it.
  let opts = { ...(i.options || {}) };
  const syncMode = () => {
    const analyst = opts.mode === "analyst";
    goLabel.textContent = analyst ? "Send meeting analyst" : "Send AI interviewer";
    consentText.textContent = consentWording(opts);
  };
  syncMode();
  panel.updateOptions = (o) => { opts = { ...o }; syncMode(); };
  go.addEventListener("click", async () => {
    error.hidden = true;
    const start = when.value();
    if (start.error) { error.textContent = start.error; error.hidden = false; return; }
    const body = { meeting_link: link.value.trim(), consent_attested: consent.checked, scheduled_at: start.at ? start.at.toISOString() : null,
      mode: opts.mode || "ai", candidate_meeting_name: opts.candidate_meeting_name || "", questions_in_chat: Boolean(opts.questions_in_chat),
      format: opts.format || "voice", require_camera: opts.require_camera !== false, require_screen_share: opts.require_screen_share !== false };
    const done = busy(go, "Starting…");
    try {
      const res = await api(`/api/interviews/${i.id}/launch`, { method: "POST", body });
      if (res.session_id) location.hash = `#/s/${res.session_id}`; else renderInterview(i.id);
    } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
  });
  panel.append(
    el("div", { class: "platforms", role: "group", "aria-label": "Supported meeting platforms" }, cards.map((c) => c.card)),
    picker,
    el("label", { class: "field" }, "Meeting link", link, hint),
    meetGuide("candidate"),
    el("div", { class: "field" }, el("span", {}, "Start at ", el("span", { class: "hint" }, "(optional; leave the date empty to start now)")), when.node),
    el("label", { class: "check" }, consent, consentText),
    el("p", { class: "notice info" }, `“${health.meetstream.bot_name}” asks to join within 2-3 minutes: admit it if asked. Ask the candidate to use headphones.`),
    error,
    el("div", { class: "form-actions" }, go),
  );
  return panel;
}

const FORMAT_INFO = {
  voice: ["Voice", "Audio only. No camera or screen sharing needed."],
  video: ["Video", "The candidate's camera is recorded with the call."],
  proctored: ["Proctored", "Camera on and the whole screen shared; screen-sharing activity is logged for you."],
};

// What the recruiter confirms the candidate was told, for this mode and format.
function consentWording(o) {
  const camera = o.format === "video" ? " and that their camera video will be recorded" : "";
  if (o.mode === "analyst") {
    return `The candidate has been told in advance that the interview will be recorded and transcribed${camera} and analysed by AI for the hiring team, and has agreed. The bot stays silent; you lead the interview and should confirm their agreement at the start.`;
  }
  const needs = [o.require_camera !== false ? "keep their camera on" : null, o.require_screen_share !== false ? "share their entire screen" : null].filter(Boolean);
  const proctored = o.format === "proctored" && needs.length
    ? `, that it is proctored (they will be asked to ${needs.join(" and ")}, and screen-sharing activity is logged)` : "";
  return `The candidate has been told in advance that an AI interviewer will conduct this interview, that it will be recorded and transcribed${camera}${proctored}, and how to ask for a human interviewer instead. The AI also asks for their consent at the start.`;
}

// How the interview runs, chosen before anything is sent: it shapes the invitation, the booking page and a
// scheduled start. Saved as soon as it changes.
function formatPanel(i, locked, onSaved) {
  let opts = { mode: "ai", format: "voice", require_camera: true, require_screen_share: true, questions_in_chat: true, candidate_meeting_name: "", ...(i.options || {}) };
  const status = el("span", { class: "small muted", role: "status" });
  const radio = (name, value, checked) => el("input", { type: "radio", name: `${name}-${i.id}`, value, checked, disabled: locked });
  const modeAi = radio("mode", "ai", opts.mode !== "analyst");
  const modeAnalyst = radio("mode", "analyst", opts.mode === "analyst");
  const formats = Object.fromEntries(Object.keys(FORMAT_INFO).map((f) => [f, radio("format", f, opts.format === f)]));
  const camera = el("input", { type: "checkbox", checked: opts.require_camera !== false, disabled: locked });
  const screen = el("input", { type: "checkbox", checked: opts.require_screen_share !== false, disabled: locked });
  const inChat = el("input", { type: "checkbox", checked: Boolean(opts.questions_in_chat), disabled: locked });
  const meetingName = el("input", { type: "text", maxlength: 200, value: opts.candidate_meeting_name || i.setup.candidate_name, disabled: locked, "aria-label": "Candidate's name in the meeting" });

  const proctorBox = el("div", { class: "subtle-box stack" },
    el("label", { class: "check" }, camera, el("span", {}, el("strong", {}, "Camera required. "), "The AI asks the candidate to turn it on before the first question.")),
    el("label", { class: "check" }, screen, el("span", {}, el("strong", {}, "Screen sharing required. "), "The AI asks for the entire screen to be shared and waits until the candidate says it's shared.")),
    el("p", { class: "muted small flush" }, "What you get: when the screen was shared, for how long, and any gaps, from MeetStream's recording (Meet, Teams and Zoom), plus the camera video. ",
      "What it can't do: check the camera is on during the call, read what's on the screen, or tell whether another device or person helped. A gap in sharing is shown as a technical event, never as misconduct. Not yet tried in a live call."));
  const analystOnly = el("label", { class: "field" }, el("span", {}, "Candidate's name in the meeting ",
    el("span", { class: "hint" }, "(to tell their words from the interviewer's in the transcript)")), meetingName);
  const aiOnly = el("label", { class: "check" }, inChat, el("span", {}, el("strong", {}, "Post each question in the meeting chat "),
    "while the AI asks it, numbered. Not on Zoom, where MeetStream repeats chat messages."));

  const read = () => ({
    mode: modeAnalyst.checked ? "analyst" : "ai",
    format: Object.keys(formats).find((f) => formats[f].checked) || "voice",
    require_camera: camera.checked, require_screen_share: screen.checked,
    questions_in_chat: inChat.checked, candidate_meeting_name: meetingName.value.trim(),
  });
  const sync = () => {
    const analyst = modeAnalyst.checked;
    if (analyst && formats.proctored.checked) formats.video.checked = true; // proctoring needs the AI to ask
    formats.proctored.disabled = locked || analyst;
    formats.proctored.closest(".mode-card").classList.toggle("disabled", analyst);
    proctorBox.hidden = !formats.proctored.checked;
    analystOnly.hidden = !analyst;
    aiOnly.hidden = analyst;
  };
  let timer;
  const save = () => {
    sync();
    clearTimeout(timer);
    status.textContent = "Saving…";
    timer = setTimeout(async () => {
      try {
        const saved = await api(`/api/interviews/${i.id}/options`, { method: "PUT", body: read() });
        opts = saved.options;
        i.options = saved.options;
        status.textContent = "Saved";
        onSaved(opts);
      } catch (err) { status.textContent = ""; toast(err.message); }
    }, 300);
  };
  for (const input of [modeAi, modeAnalyst, ...Object.values(formats), camera, screen, inChat]) input.addEventListener("change", save);
  meetingName.addEventListener("input", save);

  const card = (input, title, text) => el("label", { class: "mode-card" }, input, el("span", {}, el("strong", {}, title), el("span", { class: "small muted" }, text)));
  const panel = el("section", { class: "panel stack" },
    el("div", { class: "spread" }, sectionTitle("sliders", "How the interview runs"), status),
    locked ? notice("info", "Unschedule the interview to change how it runs.") : null,
    el("div", { class: "field" }, el("span", {}, "Who runs it"),
      el("div", { class: "mode-cards", role: "radiogroup", "aria-label": "Who runs the interview" },
        card(modeAi, "AI interviewer", "The AI interviewer asks the approved questions and follow-ups by voice."),
        card(modeAnalyst, "Meeting Analyst", "You interview; a silent bot transcribes, then the AI evaluates."))),
    el("div", { class: "field" }, el("span", {}, "Format"),
      el("div", { class: "mode-cards three", role: "radiogroup", "aria-label": "Interview format" },
        Object.entries(FORMAT_INFO).map(([f, [title, text]]) => card(formats[f], title, text)))),
    proctorBox, analystOnly, aiOnly);
  sync();
  return panel;
}

// The written notice before the call: preview the invitation, then send it (or copy it into your own email).
function invitationPanel(i) {
  const emailOn = health.email && health.email.enabled;
  const link = el("input", { type: "url", value: i.meeting_link || "", placeholder: "https://meet.google.com/abc-defg-hij", "aria-label": "Meeting link for the invitation" });
  const when = timePicker({ label: "Interview time", optional: true, emptyText: "No time in the email: it says \u201cat the agreed time\u201d." });
  if (i.scheduled_at) when.set(i.scheduled_at);
  const out = el("div", { class: "stack" });
  const status = el("p", { class: "muted small" });
  const whenText = () => { const { at } = when.value(); return at ? at.toLocaleString(undefined, FULL_TIME) : ""; };
  const body = () => ({ meeting_link: link.value.trim(), when_text: whenText() });
  const preview = el("button", { class: "btn", type: "button", onclick: async () => {
    status.textContent = "";
    const done = busy(preview, "Preparing…");
    try {
      const p = await api(`/api/interviews/${i.id}/invite/preview`, { method: "POST", body: body() });
      const text = el("textarea", { rows: 14, readonly: true, "aria-label": "Invitation text" }, p.body);
      const copy = el("button", { class: "btn small", type: "button", onclick: async () => {
        try { await navigator.clipboard.writeText(`Subject: ${p.subject}\n\n${p.body}`); copy.textContent = "Copied"; } catch { text.select(); }
      } }, "Copy text");
      const send = el("button", { class: "btn primary", type: "button", disabled: !p.can_send, onclick: async () => {
        if (!(await confirmDialog(`Send this invitation to ${p.to.join(", ")}?`, { title: "Send invitation", confirmLabel: "Send" }))) return;
        const sending = busy(send, "Sending…");
        try {
          await api(`/api/interviews/${i.id}/invite`, { method: "POST", body: body() });
          send.removeAttribute("aria-busy"); send.textContent = "Sent"; status.textContent = `Sent to ${p.to.join(", ")}.`; toast(`Invitation sent to ${p.to.join(", ")}.`, "good");
        } catch (err) { status.textContent = err.message; sending(); }
      } }, icon("mail"), "Send invitation");
      out.replaceChildren(
        el("p", { class: "small" }, el("strong", {}, "To: "), p.to.length ? p.to.join(", ") : "None yet (add the candidate's email in their details below)"),
        el("p", { class: "small" }, el("strong", {}, "Subject: "), p.subject),
        text, el("div", { class: "row" }, send, copy));
    } catch (err) { status.textContent = err.message; }
    done();
  } }, "Preview invitation");
  return el("details", { class: "panel" }, el("summary", {}, el("span", { class: "section-title" }, el("span", { class: "ico" }, icon("mail")), el("strong", {}, "Invite the candidate"))),
    el("div", { class: "stack" },
      el("p", { class: "muted small" }, "The email tells the candidate an AI conducts the interview, that it's recorded and transcribed and reviewed by a person, and how to ask for a human interviewer or an accommodation instead.",
        emailOn ? "" : " Email isn't set up (SMTP_* in .env), so copy the text into your own email."),
      i.booking && i.status !== "scheduled"
        ? notice("good", "The candidate has a self-scheduling link, so the email asks them to choose a time there instead of giving one.")
        : el("div", { class: "stack" }, el("div", { class: "form-grid" }, el("label", { class: "field" }, "Meeting link", link), el("label", { class: "field" }, "Interview time", when)), meetGuide("candidate")),
      el("div", { class: "row" }, preview), status, out));
}

// Self-scheduling: offer a meeting link and some times; the candidate picks one on a public page.
function bookingPanel(i, url) {
  const title = el("span", { class: "section-title" }, el("span", { class: "ico" }, icon("calendar")), el("strong", {}, "Let the candidate choose a time"));
  const details = el("details", { class: "panel", open: Boolean(i.booking) }, el("summary", {}, title));
  const body = el("div", { class: "stack" });
  details.append(body);
  const fmt = (iso) => new Date(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  if (["in_progress", "completed"].includes(i.status)) {
    body.append(el("p", { class: "muted small" }, "This interview has run, so there's nothing to schedule."));
    return details;
  }
  if (i.booking) {
    const b = i.booking;
    const booked = b.booked_at && i.status === "scheduled";
    const deadline = b.expires_at || b.slots.reduce((a, s) => (new Date(s) > new Date(a) ? s : a));
    const expired = !b.booked_at && new Date(deadline) <= new Date();
    const field = el("input", { type: "text", readonly: true, value: url, "aria-label": "Booking link" });
    const copy = el("button", { class: "btn", type: "button", onclick: async () => {
      try { await navigator.clipboard.writeText(url); toast("Booking link copied.", "good", 2500); } catch { field.select(); }
    } }, "Copy");
    const withdraw = el("button", { class: "btn danger", type: "button", onclick: async () => {
      if (!(await confirmDialog("Withdraw this link? The candidate won't be able to use it any more. A time they already booked stays booked; unschedule it separately.", { title: "Withdraw the booking link", confirmLabel: "Withdraw", danger: true }))) return;
      const done = busy(withdraw, "Withdrawing…");
      try { await api(`/api/interviews/${i.id}/booking`, { method: "DELETE" }); renderInterview(i.id); } catch (err) { toast(err.message); done(); }
    } }, "Withdraw link");
    body.append(
      booked ? notice("good", `Booked by the candidate for ${fmt(i.scheduled_at)}. The ${i.options && i.options.mode === "analyst" ? "note-taking bot" : "AI interviewer"} joins then.`)
        : expired ? notice("bad", `Expired ${fmt(deadline)} without a booking. Withdraw this link and create a new one to offer other times.`)
        : notice("warn", `Not booked yet. The candidate can choose until ${fmt(deadline)}. Send them the link, or the invitation below, which includes it.`),
      el("div", { class: "copy-row" }, field, copy),
      el("p", { class: "muted small flush" }, `${b.platform}: `, el("span", { class: "break" }, b.meeting_link)),
      el("div", {}, el("strong", { class: "small" }, "Times offered"),
        el("ul", { class: "plain small" }, b.slots.map((t) => el("li", {}, fmt(t), booked && new Date(t).getTime() === new Date(i.scheduled_at).getTime() ? " · booked" : "")))),
      el("p", { class: "muted small flush" }, "The candidate can change or cancel up to 30 minutes before. To offer different times, withdraw this link and create a new one."),
      el("div", { class: "row" }, withdraw));
    return details;
  }
  if (i.status === "scheduled") {
    body.append(el("p", { class: "muted small" }, "A time is already set. Unschedule it first to let the candidate choose."));
    return details;
  }
  if (!health.meetstream.ready) {
    body.append(notice("warn", `Live meetings are not configured: ${health.meetstream.reason}.`));
    return details;
  }
  const link = el("input", { type: "url", placeholder: "https://zoom.us/j/1234567890", value: i.meeting_link || "", "aria-label": "Meeting link for every offered time" });
  const slotList = el("div", { class: "stack" });
  const pickers = [];
  const addSlot = () => {
    const tp = timePicker({ label: "Offered time", optional: true });
    const row = el("div", { class: "slot-edit" }, tp.node,
      el("button", { class: "btn small", type: "button", "aria-label": "Remove this time", onclick: () => { row.remove(); pickers.splice(pickers.indexOf(tp), 1); } }, "Remove"));
    pickers.push(tp);
    slotList.append(row);
    return tp.node.querySelector("input");
  };
  for (let n = 0; n < 3; n++) addSlot();
  const expiry = timePicker({ label: "Link expires", optional: true });
  const error = notice("bad", ""); error.hidden = true;
  const create = el("button", { class: "btn primary", type: "button" }, icon("calendar"), "Create booking link");
  create.addEventListener("click", async () => {
    error.hidden = true;
    const picked = pickers.map((tp) => tp.value());
    const bad = picked.find((x) => x.error);
    if (bad) { error.textContent = bad.error; error.hidden = false; return; }
    const slots = picked.filter((x) => x.at).map((x) => x.at.toISOString());
    if (!slots.length) { error.textContent = "Add at least one time."; error.hidden = false; return; }
    const until = expiry.value();
    if (until.error) { error.textContent = until.error; error.hidden = false; return; }
    const done = busy(create, "Creating…");
    try {
      await api(`/api/interviews/${i.id}/booking`, { method: "POST", body: { meeting_link: link.value.trim(), slots, expires_at: until.at ? until.at.toISOString() : null } });
      toast("Booking link created. Send it to the candidate.", "good");
      renderInterview(i.id);
    } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
  });
  body.append(
    el("p", { class: "muted small flush" }, "The candidate gets a private link, reads how the AI interview works, and picks one of your times; the AI interviewer then joins at that time. Use one meeting link for every time, such as a recurring Zoom meeting's /j/ join link. On Google Meet someone must start the meeting before the bot joins."),
    el("label", { class: "field" }, "Meeting link", link),
    el("div", { class: "field" }, el("span", {}, "Times to offer ", el("span", { class: "hint" }, "(your time zone; the candidate sees them in theirs)")), slotList,
      el("button", { class: "btn small self-start", type: "button", onclick: () => addSlot().focus() }, icon("plus"), "Add a time")),
    el("div", { class: "field" }, el("span", {}, "Choose by ", el("span", { class: "hint" }, "(optional; if empty, until the last offered time)")), expiry.node),
    error,
    el("div", { class: "form-actions" }, create));
  return details;
}

function sessionsPanel(sessions) {
  return el("section", { class: "panel stack" }, sectionTitle("clock", "Sessions"),
    el("div", { class: "table-wrap" }, el("table", { class: "list" },
      el("thead", {}, el("tr", {}, ["Started", "Platform", "State", "Ended because", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, sessions.map((s) => el("tr", {},
        el("td", {}, fmtTime(s.created_at)), el("td", {}, s.platform), el("td", {}, pill(s.state, STATE_LABEL[s.state] || s.state)),
        el("td", { class: "muted small" }, (s.end_reason || "not recorded").replaceAll("_", " ")),
        el("td", {}, el("a", { class: "btn small", href: `#/s/${s.id}` }, s.state === "completed" ? "Report" : "Open"))))))));
}

function decisionPanel(i) {
  const note = el("textarea", { rows: 2, placeholder: "Reason for the decision (optional)" }, i.decision_note || "");
  const choose = (d) => async () => {
    try { await api(`/api/interviews/${i.id}/decision`, { method: "POST", body: { decision: d, note: note.value } }); toast(`Decision recorded: ${d}.`, "good", 3000); renderInterview(i.id); }
    catch (err) { toast(err.message); }
  };
  return el("section", { class: "panel stack" },
    el("div", { class: "spread" }, sectionTitle("check", "Your decision"), i.decision ? el("span", { class: "row" }, pill(i.decision), el("span", { class: "muted small" }, fmtTime(i.decided_at))) : null),
    el("p", { class: "muted small" }, "The AI recommendation is advisory. Review the transcript and evidence before deciding."),
    note,
    el("div", { class: "row" },
      el("button", { class: "btn good", onclick: choose("advance") }, "Advance"),
      el("button", { class: "btn hold", onclick: choose("hold") }, "Hold"),
      el("button", { class: "btn danger", onclick: choose("reject") }, "Reject")));
}

function setupPanel(i, locked) {
  const details = el("details", { class: "panel" }, el("summary", {}, el("strong", {}, "Job description, resume and criteria")));
  if (locked) {
    details.append(el("p", { class: "muted small" }, "Locked while the interview is scheduled or in progress."));
    return details;
  }
  if (i.role_id) {
    // A role's candidate: only the candidate's details are edited here; the job is the role's.
    details.querySelector("summary strong").textContent = "Candidate details and resume";
    details.append(setupForm(i.setup, "Save candidate details", async (body) => {
      await api(`/api/interviews/${i.id}/setup`, { method: "PUT", body: { ...i.setup, ...body } });
      renderInterview(i.id);
    }, "candidate"));
    return details;
  }
  details.append(el("p", { class: "muted small" }, "Saving changes keeps the current plan but withdraws its approval. Regenerate the plan to use the new details."),
    setupForm(i.setup, "Save details", async (body) => {
      await api(`/api/interviews/${i.id}/setup`, { method: "PUT", body });
      renderInterview(i.id);
    }));
  return details;
}

// A candidate who applied takes the interview through their own link, before its deadline.
function interviewLinkPanel(i, data) {
  const inv = i.invitation;
  const expired = new Date(inv.expires_at) <= new Date();
  const done = i.status === "completed";
  const state = data.live_session_id ? "In progress" : done ? "Completed" : expired ? "Expired" : inv.started_at ? "Started" : inv.opened_at ? "Opened, not started" : "Not opened";
  const email = inv.email_sent_at ? `Emailed ${fmtTime(inv.email_sent_at)}` : inv.email_error ? `Not emailed: ${inv.email_error}` : "Not emailed yet";
  const copy = el("button", { class: "btn", type: "button", onclick: async () => {
    try { const r = await api(`/api/interviews/${i.id}/invitation`); await navigator.clipboard.writeText(r.url); toast("Link copied.", "good", 2500); } catch (err) { toast(err.message); }
  } }, "Copy link");
  const resend = el("button", { class: "btn", type: "button", disabled: Boolean(data.live_session_id), onclick: async () => {
    if (done && !(await confirmDialog("Allow another attempt? The candidate gets a new link; the earlier interview and its report are kept.", { title: "Reopen the interview", confirmLabel: "Reopen" }))) return;
    const finish = busy(resend, "Sending…");
    try { await api(`/api/interviews/${i.id}/invitation/resend`, { method: "POST", body: { reopen: done } }); toast("New link sent. The old one no longer works.", "good"); renderInterview(i.id); } catch (err) { toast(err.message); finish(); }
  } }, done ? "Allow another attempt" : "Send a new link");
  return el("section", { class: "panel stack" },
    el("div", { class: "spread" }, sectionTitle("mail", "Interview link"), pill(expired && !done ? "failed" : done ? "ready" : "invitation_sent", state)),
    el("dl", { class: "facts" },
      el("dt", {}, "Valid until"), el("dd", {}, fmtTime(inv.expires_at)),
      el("dt", {}, "Email"), el("dd", {}, email),
      el("dt", {}, "Attempts"), el("dd", {}, String(inv.attempts))),
    el("div", { class: "row" }, copy, resend));
}

function cancelledPanel(i) {
  const reopen = el("button", { class: "btn", type: "button", onclick: async () => {
    const done = busy(reopen, "Reopening…");
    try { await api(`/api/interviews/${i.id}/reopen`, { method: "POST" }); toast("Interview reopened.", "good", 2500); renderInterview(i.id); } catch (err) { toast(err.message); done(); }
  } }, "Reopen");
  return el("section", { class: "panel spread" },
    el("div", {}, el("strong", {}, "Cancelled"),
      el("p", { class: "muted small flush" }, `Cancelled ${fmtTime(i.cancelled_at)}. Reopen it to launch, offer times or send an invitation again.`)),
    reopen);
}

function cancelButton(i) {
  const cancel = el("button", { class: "btn", type: "button", onclick: async () => {
    const canMail = Boolean(i.setup.candidate_email && health.email && health.email.enabled);
    const notify = el("input", { type: "checkbox", id: "notify-cancel", checked: canMail && Boolean(i.invited_at || i.booking), disabled: !canMail });
    const why = !i.setup.candidate_email ? " (no email address on file)" : !canMail ? " (email isn't set up)" : "";
    const ok = await confirmDialog(el("div", { class: "stack" },
      el("p", { class: "flush" }, "Cancel this interview? A scheduled time is unscheduled and the self-scheduling link stops working. You can reopen it later."),
      el("label", { class: "check", for: "notify-cancel" }, notify, el("span", {}, `Email the candidate that it's cancelled${why}`))),
    { title: "Cancel the interview", confirmLabel: "Cancel interview", cancelLabel: "Keep it", danger: true });
    if (!ok) return;
    const done = busy(cancel, "Cancelling…");
    try {
      await api(`/api/interviews/${i.id}/cancel`, { method: "POST", body: { notify_candidate: notify.checked } });
      toast(notify.checked ? "Interview cancelled. The candidate is being emailed." : "Interview cancelled.", "good");
      renderInterview(i.id);
    } catch (err) { toast(err.message); done(); }
  } }, "Cancel interview");
  return cancel;
}

// Where the interview takes place (app/meetings.py): the role's shared room, or a meeting made for this candidate.
// Candidates never get this link by email: their interview link shows it after the emailed code.
function meetingPanel(i, m) {
  const url = el("input", { type: "url", maxlength: 2000, placeholder: "https://meet.google.com/abc-defg-hij", value: m.own ? m.own.url : "", "aria-label": "This candidate's meeting link" });
  const save = el("button", { class: "btn small primary", type: "button", onclick: async () => {
    const done = busy(save, "Saving…");
    try { await api(`/api/interviews/${i.id}/meeting`, { method: "PUT", body: { url: url.value.trim() } }); toast("This candidate has their own meeting now.", "good"); route(); } catch (err) { toast(err.message); done(); }
  } }, m.own ? "Update" : "Use for this candidate");
  const back = m.own && i.role_id ? el("button", { class: "btn small", type: "button", onclick: async () => {
    try { await api(`/api/interviews/${i.id}/meeting`, { method: "DELETE" }); toast("Back to the role's room.", "good"); route(); } catch (err) { toast(err.message); }
  } }, "Use the role's room") : null;
  const level = m.capture ? { verified: "ready", "partly verified": "scheduled", "not verified": "failed" }[m.capture.level] || "draft" : null;
  return el("section", { class: "panel stack" }, sectionTitle("video", "Meeting"),
    el("p", { class: "small flush" }, m.own ? `This candidate's own ${m.own.platform} meeting.` : m.shared_room ? `The role's shared ${m.platform} room: one interview at a time.` : m.link ? `${m.platform} meeting.` : "No meeting yet."),
    m.capture ? el("p", { class: "small flush" }, pill(level, `Capture: ${m.capture.level}`), " ", el("span", { class: "muted" }, m.capture.note)) : null,
    el("div", { class: "copy-row" }, url, save), back, meetGuide("candidate"),
    el("p", { class: "muted small flush" }, "A meeting for each candidate keeps a shared link from letting someone else in. Turn on the platform's waiting room or lobby. Candidates only see the meeting through their interview link, after the emailed code, so changing it needs no new email."));
}

function dangerPanel(i, live) {
  const del = el("button", { class: "btn danger", disabled: live, onclick: async () => {
    if (!(await confirmDialog(`Delete this interview and all of ${i.setup.candidate_name}'s data (resume, transcripts, reports)? MeetStream will be asked to erase the recordings. This cannot be undone.`,
      { title: "Delete candidate data", confirmLabel: "Delete everything", danger: true }))) return;
    const done = busy(del, "Deleting…");
    try { await api(`/api/interviews/${i.id}`, { method: "DELETE" }); toast("Interview and candidate data deleted.", "good"); location.hash = i.role_id ? `#/r/${i.role_id}` : "#/"; } catch (err) { toast(err.message); done(); }
  } }, icon("trash"), "Delete interview and candidate data");
  return el("section", { class: "panel spread" },
    el("div", {}, sectionTitle("shield", "Candidate data"),
      el("p", { class: "muted small" }, health.retention_days ? `Finished interviews are deleted automatically after ${health.retention_days} days.` : "Finished interviews are kept until you delete them.")),
    el("div", { class: "row" }, !i.cancelled_at && !["in_progress", "completed"].includes(i.status) ? cancelButton(i) : null, del));
}

function activityPanel(events) {
  if (!events.length) return null;
  return el("details", { class: "panel" }, el("summary", {}, el("strong", {}, "Activity")),
    el("ul", { class: "plain small" }, events.map((e) => el("li", {},
      el("span", { class: "muted" }, fmtTime(e.ts)), ` ${e.action.replaceAll("_", " ")}`, e.detail ? el("span", { class: "muted" }, ` · ${e.detail}`) : null,
      e.actor ? el("span", { class: "muted" }, ` · by ${e.actor}`) : null))));
}

// ---------- session ----------

async function renderSession(sid) {
  const data = await api(`/api/sessions/${sid}`);
  if (data.report && !data.live) { renderReport(data.report, data.interview_id); return; }
  if (!data.live) { renderStoredSession(data); return; }

  const transcript = el("ol", { class: "transcript" });
  const notes = el("ul", { class: "notes" }, el("li", { class: "muted" }, "Notes appear as each question closes."));
  const errors = el("div", { class: "stack" });
  const joinNote = notice("warn", "Starting the bot. MeetStream usually takes 2-3 minutes before it asks to join the meeting; then admit it from the lobby if asked.");
  joinNote.hidden = true;
  const state = el("span", { class: "pill" }, "connecting");
  const stat = { q: el("span", { class: "v" }, "Waiting"), fu: el("span", { class: "v" }, "0"), t: el("span", { class: "v" }, "0:00") };
  const end = el("button", { class: "btn danger block", onclick: async () => {
    if (!(await confirmDialog("End the interview now? The AI leaves the call and the evaluation starts.", { title: "End interview", confirmLabel: "End interview", danger: true }))) return;
    end.disabled = true;
    try { await api(`/api/sessions/${sid}/end`, { method: "POST" }); } catch (err) { toast(err.message); end.disabled = false; }
  } }, "End interview");
  const conn = el("span", { class: "conn" }, "Connecting…");
  const waiting = el("li", { class: "muted small transcript-wait" }, "Waiting for the first line of the transcript…");
  transcript.append(waiting);

  view.replaceChildren(el("div", { class: "stack" },
    pageHead({ backTo: back(`#/i/${data.interview_id}`, "Interview"), eyebrow: "Live monitoring",
      title: el("span", { class: "row" }, el("span", { class: "live-dot", "aria-hidden": "true" }), "Live interview"),
      right: el("div", { class: "row" }, conn, state) }),
    el("div", { class: "layout" },
      el("section", { class: "panel stack" },
        el("div", { class: "spread" }, sectionTitle("chat", "Transcript"),
          el("div", { class: "legend", "aria-hidden": "true" },
            el("span", {}, el("i", { class: "key-ai" }), "AI interviewer"),
            el("span", {}, el("i", { class: "key-candidate" }), "Candidate"),
            el("span", {}, el("i", { class: "key-system" }), "System"))),
        el("p", { class: "muted small" }, "The AI interviewer speaks in the call. Planned questions and notes come from this app; candidate lines come from MeetStream's live transcription and can contain recognition errors. The AI's follow-up questions are spoken but not transcribed."),
        joinNote,
        transcript),
      el("aside", { class: "stack aside-sticky" },
        el("div", { class: "panel stack" },
          el("div", { class: "stats" },
            el("div", {}, el("span", { class: "k" }, "Question"), stat.q),
            el("div", {}, el("span", { class: "k" }, "Follow-ups"), stat.fu),
            el("div", {}, el("span", { class: "k" }, "Elapsed"), stat.t)),
          end),
        el("div", { class: "panel stack" }, sectionTitle("doc", "Interviewer notes"), notes),
        errors))));

  let t0 = Date.now();
  const timer = setInterval(() => {
    const s = Math.max(0, Math.floor((Date.now() - t0) / 1000));
    stat.t.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }, 1000);

  closeSource();
  source = new EventSource(`/api/sessions/${sid}/events`);
  const setConn = (cls, text) => { conn.className = `conn ${cls}`; conn.textContent = text; };
  source.onopen = () => setConn("on", "Connected");
  // EventSource reconnects by itself; say so rather than fail silently.
  source.onerror = () => setConn("off", "Reconnecting…");
  source.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.type === "utterance") {
      waiting.remove();
      transcript.append(lineEl(ev.utterance));
      transcript.scrollTop = transcript.scrollHeight;
    } else if (ev.type === "note") {
      notes.querySelector(".muted")?.remove();
      notes.prepend(el("li", {}, el("strong", { class: "small" }, ev.note.question_id), ` ${ev.note.note}`,
        ev.note.follow_ups ? el("span", { class: "muted small" }, ` (${ev.note.follow_ups} follow-up${ev.note.follow_ups > 1 ? "s" : ""})`) : null));
    } else if (ev.type === "state") {
      state.className = `pill ${ev.state}`;
      state.textContent = STATE_LABEL[ev.state] || ev.state;
      joinNote.hidden = ev.state !== "joining";
      stat.q.textContent = ev.mode === "analyst" ? "Listening" : ev.question_total ? `${ev.question_number}/${ev.question_total}` : "Waiting";
      stat.fu.textContent = String(ev.follow_ups);
      t0 = Date.now() - ev.elapsed_seconds * 1000;
      end.disabled = ["leaving", "transcribing", "evaluating", "completed", "failed"].includes(ev.state);
      if (ev.state === "failed") { clearInterval(timer); closeSource(); setConn("", "Session ended"); }
    } else if (ev.type === "error") {
      errors.append(notice("bad", ev.message));
    } else if (ev.type === "report") {
      clearInterval(timer);
      closeSource();
      renderReport(ev.report, data.interview_id);
    }
  };
}

function lineEl(u) {
  const who = u.role === "system" ? "System" : u.speaker;
  return el("li", { class: `line ${u.role}` }, el("div", { class: "who" }, who + (u.question_id ? ` · ${u.question_id}` : "")), el("div", {}, u.text));
}

// A report transcript line: anchored, and tagged when it is evidence for a score or requirement.
function reportLineEl(u, n, marks) {
  const li = lineEl(u);
  li.id = `line-${n}`;
  if (marks && marks.length) {
    li.classList.add("key");
    li.append(el("div", { class: "tags" }, [...new Set(marks.map((m) => m.label))].map((label) => el("span", { class: "tag" }, label))));
  }
  return li;
}

function talkPanel(t) {
  if (!t || t.candidate_share == null) return null;
  const pct = Math.round(t.candidate_share * 100);
  const note = t.basis === "recording"
    ? "Counted in words from the transcript rebuilt from the recording."
    : "Counted in words from the live transcript. The AI's follow-up questions aren't in it, so the candidate's share reads high.";
  return el("section", { class: "panel stack" }, sectionTitle("users", "Talk time"),
    el("div", { class: "spread" }, el("span", {}, `Candidate ${pct}%`), el("span", { class: "muted" }, `Interviewer ${100 - pct}%`)),
    el("div", { class: "bar", role: "img", "aria-label": `Candidate spoke ${pct}% of the words` }, el("span", { style: `width:${pct}%` })),
    el("p", { class: "muted small" }, `${t.candidate_words} candidate words, ${t.interviewer_words} interviewer words. ${note}`),
    t.per_question.length ? el("ul", { class: "plain small" }, t.per_question.map((q) => el("li", {}, el("strong", {}, q.question_id), ` ${q.candidate_words} words`)),
      t.unassigned_candidate_words ? el("li", { class: "muted" }, `${t.unassigned_candidate_words} words not tied to a question`) : null) : null);
}

function showLine(n) {
  const line = document.getElementById(`line-${n}`);
  if (!line) return;
  line.closest("details")?.setAttribute("open", "");
  // An instant jump: a smooth scroll started right after opening the transcript fell short of the line.
  line.scrollIntoView({ block: "center" });
  line.classList.remove("flash");
  void line.offsetWidth; // restart the animation
  line.classList.add("flash");
}

// How each proctoring entry is known, from most to least direct. The recruiter sees this next to every entry.
const PROCTOR_SOURCE = {
  meetstream_recording: ["From MeetStream's recording", "good"],
  meetstream_live: ["Reported by MeetStream during the call", "good"],
  app: ["Recorded by this app", "info"],
  candidate: ["Said by the candidate, not verified", "warn"],
};

function proctoringPanel(p, startedAt) {
  const t0 = new Date(startedAt).getTime();
  const when = (e) => {
    if (!e.at) return e.offset_seconds != null ? `+${Math.round(e.offset_seconds / 60)} min` : "";
    const mins = Math.max(0, Math.round((new Date(e.at).getTime() - t0) / 60000));
    return `${new Date(e.at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })} · ${mins} min in`;
  };
  const span = (s) => (s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);
  const metric = (label, value) => el("div", { class: "metric" }, el("b", {}, value), label);
  const required = [p.require_camera ? "camera" : null, p.require_screen_share ? "screen sharing" : null].filter(Boolean);
  return el("section", { class: "panel stack" }, sectionTitle("shield", "Proctoring"),
    el("p", { class: "muted small flush" }, `Required: ${required.length ? required.join(" and ") : "nothing"}. `,
      p.consent_given ? "The candidate agreed at the start." : "The candidate didn't agree, so nothing was recorded.",
      " These are records of what happened, not judgements: a gap in screen sharing is a technical event, not evidence of misconduct."),
    el("div", { class: "metrics" },
      metric("screen shared", p.screen_share_seconds != null ? span(p.screen_share_seconds) : "Unknown"),
      metric("interruptions", p.interruptions != null ? String(p.interruptions) : "Unknown")),
    p.events.length
      ? el("ol", { class: "plain proctor-timeline" }, p.events.map((e) => {
        const [label, kind] = PROCTOR_SOURCE[e.source] || [e.source, "info"];
        return el("li", {}, el("span", { class: "muted small" }, when(e)),
          el("div", {}, el("p", { class: "flush" }, e.text), el("span", { class: `badge ${kind} small` }, label)));
      }))
      : el("p", { class: "muted small flush" }, "Nothing was recorded."),
    p.unverified.length ? el("div", {}, el("strong", { class: "small" }, "Couldn't be checked"),
      el("ul", { class: "plain small muted" }, p.unverified.map((u) => el("li", {}, u)))) : null);
}

// Meeting Analyst: what the person interviewing actually asked, taken from their lines in the transcript.
function interviewerQuestionsPanel(transcript) {
  const asked = transcript.map((u, n) => ({ u, n })).filter(({ u }) => u.role === "interviewer" && u.text.includes("?"));
  return el("section", { class: "panel stack" }, sectionTitle("chat", "Questions the interviewer asked"),
    asked.length
      ? el("ol", { class: "plain moments" }, asked.map(({ u, n }) => el("li", {},
        el("button", { class: "link", type: "button", onclick: () => showLine(n) }, u.text), el("span", { class: "muted small" }, ` · ${u.speaker}`))))
      : el("p", { class: "muted small flush" }, "No questions were recognised in the interviewer's lines."));
}

function momentsPanel(moments) {
  if (!moments || !moments.length) return null;
  return el("section", { class: "panel stack" }, sectionTitle("spark", "Key moments"),
    el("ul", { class: "plain moments" }, moments.map((m) => el("li", {},
      el("button", { class: "link", type: "button", onclick: () => showLine(m.line) }, `“${m.quote}”`),
      el("span", { class: "muted small" }, ` · ${m.label}`)))));
}

function renderStoredSession(data) {
  view.replaceChildren(el("div", { class: "stack" },
    pageHead({ backTo: back(`#/i/${data.interview_id}`, "Interview"), eyebrow: "Session", title: "Session record",
      sub: data.session.end_reason ? `Ended: ${data.session.end_reason.replaceAll("_", " ")}` : null,
      right: pill(data.session.state, STATE_LABEL[data.session.state]) }),
    el("section", { class: "panel stack" }, sectionTitle("chat", "Transcript"),
      data.transcript.length ? el("ol", { class: "transcript" }, data.transcript.map(lineEl)) : el("p", { class: "muted" }, "Nothing was recorded."))));
}

// ---------- report ----------

function list(items, empty = "None noted.") {
  return items && items.length ? el("ul", { class: "plain" }, items.map((x) => el("li", {}, x))) : el("p", { class: "muted" }, empty);
}

function renderReport(r, interviewId) {
  const ev = r.evaluation;
  const mins = Math.floor(r.duration_seconds / 60);
  const parts = [
    pageHead({ backTo: back(`#/i/${interviewId}`, "Interview"), eyebrow: r.mode === "analyst" ? "Evaluation report · Meeting Analyst" : "Evaluation report",
      title: el("span", {}, `${r.candidate_name} · ${r.role_title} `, demoTag((r.generated_by || "").startsWith("Demo data"))),
      sub: `${r.platform} · ${fmtTime(r.started_at)} · ended: ${r.end_reason.replaceAll("_", " ")}`,
      right: el("div", { class: "row" },
        r.consent_given && health.llm.is_ai ? (() => {
          const again = el("button", { class: "btn", onclick: async () => {
            if (!(await confirmDialog(`Run the evaluation again with ${health.llm.name}? It replaces the current scores.`, { title: "Re-run evaluation", confirmLabel: "Re-run" }))) return;
            const done = busy(again, "Evaluating…");
            try { renderReport(await api(`/api/sessions/${r.session_id}/evaluate`, { method: "POST" }), interviewId); toast("Evaluation updated.", "good", 3000); }
            catch (err) { toast(err.message); done(); }
          } }, icon("spark"), "Re-run evaluation");
          return again;
        })() : null,
        el("button", { class: "btn", onclick: () => window.print(), title: "Print, or choose “Save as PDF” as the printer" }, "Print / Save as PDF"),
        el("button", { class: "btn", onclick: () => download(r) }, "Download JSON"),
        el("a", { class: "btn primary", href: `#/i/${interviewId}` }, icon("check"), "Record decision")) }),
    el("p", { class: "print-only muted small" }, `Printed ${fmtTime(new Date().toISOString())} from Recruence. Session ${r.session_id}.`),
  ];
  const metric = (label, value) => el("div", { class: "metric" }, el("b", {}, value), label);
  const callMetrics = () => [metric("minutes", String(mins)), metric("questions asked", `${r.questions_asked}/${r.questions_planned}`), metric("follow-ups", String(r.follow_ups_asked))];
  if (!ev) {
    parts.push(el("section", { class: "panel stack" }, sectionTitle("alert", "No evaluation"), notice("warn", r.evaluation_error || "Unknown reason"),
      el("div", { class: "metrics" }, callMetrics())));
  } else {
    // The ring shows the mean of the competency scores (1-5) as a score out of 100, the same figure the role's ranking
    // and pass mark use (app/roles.py: score_100). The recommendation is the model's own.
    const scores = ev.competency_scores.map((c) => c.score).filter((s) => s != null);
    const avg = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null;
    const score100 = avg == null ? null : Math.round((Math.round(avg * 100) / 100) * 20);
    const reqs = ev.requirement_results || [];
    const met = reqs.filter((q) => q.verdict === "meets").length;
    parts.push(el("section", { class: "panel hero" },
      avg == null ? null : el("div", { class: "score-ring", style: `--p:${score100}`, role: "img", "aria-label": `Score ${score100} out of 100 (average competency score ${avg.toFixed(1)} out of 5)` },
        el("div", {}, el("div", {}, el("strong", {}, String(score100)), el("span", {}, `of 100 · ${avg.toFixed(1)}/5`)))),
      el("div", { class: "stack", style: avg == null ? "grid-column:1/-1" : null },
        el("div", { class: "row" }, el("span", { class: `rec big ${ev.recommendation}` }, REC_LABEL[ev.recommendation]),
          el("span", { class: "badge" }, `confidence: ${ev.confidence}`), el("span", { class: "muted small" }, r.generated_by)),
        el("p", { class: "summary" }, ev.summary),
        el("div", { class: "metrics" }, callMetrics(),
          reqs.length ? metric("requirements met", `${met}/${reqs.length}`) : null,
          scores.length ? metric("competencies scored", `${scores.length}/${ev.competency_scores.length}`) : null))));
    if (scores.length < ev.competency_scores.length) {
      parts.push(notice("warn", "Not every competency was assessed. The score covers the assessed ones only, so there's no pass or fail yet."));
    }
    if (reqs.length) {
      parts.push(el("section", { class: "panel stack" }, sectionTitle("shield", "Must-have requirements"),
        el("p", { class: "muted small flush" }, "A verdict needs the candidate's own words. Without them it stays “Not enough evidence”."),
        el("div", {}, reqs.map((q) => el("div", { class: "comp" },
          el("div", { class: "spread" }, el("strong", {}, q.requirement), pill(q.verdict, VERDICT_LABEL[q.verdict])),
          el("p", {}, q.rationale),
          q.evidence.length ? el("ul", { class: "evidence" }, q.evidence.map((x) => el("li", {}, `“${x}”`))) : null)))));
    }
    parts.push(el("section", { class: "panel stack" }, sectionTitle("chart", "Competencies"),
      el("div", {}, ev.competency_scores.map((c) => el("div", { class: "comp" },
        el("div", { class: "spread" }, el("strong", {}, c.competency_name), c.score == null ? el("span", { class: "muted small" }, "Not assessed") : el("span", { class: "score-num" }, `${c.score}/5`)),
        c.score == null ? null : el("div", { class: "bar", role: "img", "aria-label": `${c.score} out of 5` }, el("span", { style: `width:${c.score * 20}%` })),
        el("p", {}, c.rationale),
        c.evidence.length ? el("ul", { class: "evidence" }, c.evidence.map((q) => el("li", {}, `“${q}”`))) : el("p", { class: "muted small" }, "No verbatim evidence."))))));
    parts.push(el("div", { class: "grid2" },
      el("section", { class: "panel card-good" }, el("h3", {}, "Strengths"), list(ev.strengths)),
      el("section", { class: "panel card-warn" }, el("h3", {}, "Areas for improvement"), list(ev.areas_for_improvement)),
      el("section", { class: "panel card-info" }, el("h3", {}, "Focus for the next round"), list(ev.suggested_next_round_focus))));
  }
  if (r.mode === "analyst") parts.push(interviewerQuestionsPanel(r.transcript));
  if (r.proctoring) parts.push(proctoringPanel(r.proctoring, r.started_at));
  parts.push(momentsPanel(r.key_moments), talkPanel(r.talk));
  if (r.notes.length) {
    parts.push(el("section", { class: "panel stack" }, sectionTitle("doc", "Interviewer notes"),
      el("ul", { class: "notes" }, r.notes.filter((n) => n.note).map((n) => el("li", {}, el("strong", { class: "small" }, n.question_id), ` ${n.note}`)))));
  }
  const source = r.transcript_source === "recording"
    ? "Rebuilt from the call recording, including the AI's follow-up questions. Speakers are inferred, so a few words at a turn boundary may be credited to the wrong speaker."
    : "From MeetStream's live transcript, which carries only human speech: the AI's spoken follow-up questions are not included.";
  parts.push(el("details", { class: "panel", open: !ev },
    el("summary", {}, el("span", { class: "section-title" }, el("span", { class: "ico" }, icon("chat")), el("strong", {}, `Transcript (${r.transcript.length} lines)`))),
    el("p", { class: "muted small" }, source),
    el("ol", { class: "transcript gap-top" }, r.transcript.map((u, n) =>
      reportLineEl(u, n, (r.key_moments || []).filter((m) => m.line === n))))));
  parts.push(el("p", { class: "disclaimer" }, r.disclaimer));
  view.replaceChildren(el("div", { class: "stack" }, parts));
}

function download(r) {
  const blob = new Blob([JSON.stringify(r, null, 2)], { type: "application/json" });
  const a = el("a", { href: URL.createObjectURL(blob), download: `interview-report-${r.session_id}.json` });
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---------- routing ----------

// The shape of a page while it loads: a header and a few panels.
function pageSkeleton() {
  return el("div", { class: "stack skeleton", "aria-busy": "true", "aria-label": "Loading" },
    el("div", { class: "sk sk-title" }), el("div", { class: "sk sk-line" }),
    el("div", { class: "sk-row" }, el("div", { class: "sk sk-card" }), el("div", { class: "sk sk-card" }), el("div", { class: "sk sk-card" })),
    el("div", { class: "sk sk-panel" }));
}

// Everyone who applied, from every source, as people: search and filter, then open an application.
// Candidates, by role: the roles with how many applied and how many wait for a decision, then the chosen role's
// applicants, each with where their application stands. Clicking one opens the review (the same as on the role page).
const NEEDS_DECISION = ["needs_review", "eligible"];

async function renderCandidates() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  let everyone;
  try { everyone = await api("/api/candidates"); } catch (err) { view.replaceChildren(notice("bad", err.message)); return; }
  // Per role: applications, and those waiting for the recruiter (to review, or eligible and not invited).
  const counts = Object.fromEntries(everyone.roles.map((r) => [r.id, { applied: 0, waiting: 0 }]));
  for (const person of everyone.candidates) for (const a of person.applications) {
    const c = counts[a.role_id] || (counts[a.role_id] = { applied: 0, waiting: 0 });
    c.applied += 1;
    if (NEEDS_DECISION.includes(a.status)) c.waiting += 1;
  }
  const roles = everyone.roles.slice().sort((a, b) => (counts[b.id].waiting - counts[a.id].waiting)
    || (counts[b.id].applied - counts[a.id].applied) || a.role_title.localeCompare(b.role_title));
  const head = pageHead({ title: "Candidates", sub: "Applicants by role. Choose a role to see who applied and where each application stands." });
  if (!roles.length) {
    view.replaceChildren(el("div", { class: "stack" }, head,
      emptyState("users", el("p", {}, "No roles yet"), el("p", { class: "muted small" }, "Applications are filed under the role they're for. Create a role and publish it first."),
        el("a", { class: "btn primary gap-top", href: "#/roles/new" }, "Create a role"))));
    return;
  }
  let roleId = roles.some((r) => r.id === params.get("role")) ? params.get("role") : roles[0].id;

  const q = el("input", { type: "search", placeholder: "Search name, email, location", value: params.get("q") || "", "aria-label": "Search this role's applicants" });
  const select = (name, label, options) => el("select", { "aria-label": label, "data-filter": name },
    el("option", { value: "" }, label), options.map(([v, l]) => el("option", { value: v, selected: params.get(name) === v }, l)));
  const status = select("status", "Any status", Object.entries(SCREEN_LABEL));
  const source = select("source", "Any source", Object.entries(SOURCE_LABEL));
  const sync = select("sync", "Any ATS sync", Object.entries(SYNC_LABEL));
  const picker = el("nav", { class: "role-picker", "aria-label": "Roles" });
  const detail = el("section", { class: "panel stack" });
  const results = el("div", { class: "stack" });
  const title = el("div", { class: "spread" });
  let timer = null;

  const drawPicker = () => picker.replaceChildren(...roles.map((r) => {
    const c = counts[r.id];
    return el("button", { type: "button", class: "role-pick", "aria-current": r.id === roleId ? "true" : null,
      onclick: () => { roleId = r.id; drawPicker(); load(); } },
      el("strong", {}, r.role_title),
      el("span", { class: "muted small" }, c.applied ? `${c.applied} applied` : "No applications yet",
        c.waiting ? el("span", { class: "role-pick-waiting" }, ` · ${c.waiting} to decide`) : null));
  }));

  async function load() {
    const qs = new URLSearchParams({ role: roleId });
    if (q.value.trim()) qs.set("q", q.value.trim());
    [status, source, sync].forEach((s) => { if (s.value) qs.set(s.dataset.filter, s.value); });
    history.replaceState(null, "", `#/candidates?${qs}`);
    const role = roles.find((r) => r.id === roleId);
    const c = counts[roleId];
    title.replaceChildren(el("div", {}, el("h2", { class: "flush" }, role.role_title),
      el("p", { class: "muted small flush-top" }, `${c.applied} applied${c.waiting ? ` · ${c.waiting} waiting for your decision` : ""}`)),
      el("a", { class: "btn small", href: `#/r/${roleId}` }, "Open the role"));
    let data;
    try { data = await api(`/api/candidates?${qs}`); } catch (err) { results.replaceChildren(notice("bad", err.message)); return; }
    const rows = data.candidates.flatMap((p) => p.applications.filter((a) => a.role_id === roleId).map((a) => ({ p, a })));
    if (!rows.length) {
      const filtered = q.value.trim() || status.value || source.value || sync.value;
      results.replaceChildren(el("p", { class: "muted flush" }, filtered ? "No applicant matches these filters." : "No one has applied for this role yet."));
      return;
    }
    // Those waiting for a decision first, then the newest.
    rows.sort((x, y) => (NEEDS_DECISION.includes(y.a.status) - NEEDS_DECISION.includes(x.a.status)) || y.a.created_at.localeCompare(x.a.created_at));
    const open = (id) => applicationDialog(id, load);
    const headers = ["Candidate", "Status", "Screening", "Source", "Applied", ""];
    results.replaceChildren(el("div", { class: "table-wrap" }, el("table", { class: "list" },
      el("thead", {}, el("tr", {}, headers.map((h, n) => el("th", { class: [3, 4].includes(n) ? "hide-sm" : n === 2 ? "num" : null }, h)))),
      el("tbody", {}, rows.map(({ p, a }) => el("tr", { class: "clickable", tabindex: "0", onclick: () => open(a.id), onkeydown: (e) => { if (e.key === "Enter") open(a.id); } },
        el("td", {}, el("strong", {}, p.full_name), el("div", { class: "muted small" }, p.email),
          p.current_role || p.location ? el("div", { class: "muted small" }, [p.current_role, p.location].filter(Boolean).join(" · ")) : null),
        el("td", {}, a.invitation_undelivered ? pill("warn", "Invited, not delivered")
            : pill(VERDICT_KIND[{ eligible: "met", not_eligible: "not_met" }[a.status]] || "scheduled", SCREEN_LABEL[a.status] || a.status),
          a.sync && a.sync.status !== "not_needed" ? el("div", {}, pill(SYNC_KIND[a.sync.status], SYNC_LABEL[a.sync.status])) : null),
        el("td", { class: "num" }, a.score != null ? `${a.score}/100` : empty("-", "Not screened")),
        el("td", { class: "hide-sm small" }, SOURCE_LABEL[a.source] || a.source, a.external ? ` (${a.external.provider})` : ""),
        el("td", { class: "hide-sm small muted" }, fmtTime(a.created_at)),
        el("td", { class: "actions" }, a.interview_id
          ? el("a", { class: "btn small", href: `#/i/${a.interview_id}`, onclick: (e) => e.stopPropagation() }, "Interview")
          : el("button", { class: "btn small", type: "button", onclick: (e) => { e.stopPropagation(); open(a.id); } }, NEEDS_DECISION.includes(a.status) ? "Review" : "Open"))))))));
  }
  q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
  [status, source, sync].forEach((s) => s.addEventListener("change", load));
  drawPicker();
  detail.append(title, el("div", { class: "row filters" }, q, status, source, sync), results);
  view.replaceChildren(el("div", { class: "stack" }, head, el("div", { class: "cand-layout" }, picker, detail)));
  await load();
}

// The Interviews page: every interview in the workspace in one list, whether a recruiter created it or an eligible
// application was invited automatically (the same interviews and API as everywhere else; app/lifecycle.py state()).
const BOARD_LABEL = { pending: "Pending", scheduled: "Scheduled", upcoming: "Upcoming", in_progress: "In progress",
  completed: "Completed", cancelled: "Cancelled", expired: "Expired", failed: "Failed" };
const BOARD_KIND = { pending: "scheduled", scheduled: "draft", upcoming: "invitation_sent", in_progress: "in_progress",
  completed: "ready", cancelled: "cancelled", expired: "expired", failed: "failed" };
const ASSIGNED_LABEL = { automatic: "Automatic", manual: "Manual", "": "Not recorded" };

async function renderInterviews() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const all = await api("/api/interviews");
  const want = { state: params.get("state") || "", role: params.get("role") || "", assigned: params.get("assigned") || "",
                 platform: params.get("platform") || "", q: params.get("q") || "" };
  // Grouped by role unless the list was asked for (?list=1): each role's interviews together, one-off interviews last.
  let grouped = params.get("list") !== "1";
  const roles = [...new Map(all.filter((x) => x.role_id).map((x) => [x.role_id, x.role_title])).entries()].sort((a, b) => a[1].localeCompare(b[1]));
  const platforms = [...new Set(all.map((x) => x.platform).filter(Boolean))].sort();
  const select = (name, label, options) => {
    const s = el("select", { "aria-label": label }, el("option", { value: "" }, label),
      options.map(([v, l]) => el("option", { value: v, selected: want[name] === v }, l)));
    s.addEventListener("change", () => { want[name] = s.value; draw(); });
    return s;
  };
  const q = el("input", { type: "search", placeholder: "Search candidate or role", value: want.q, "aria-label": "Search interviews" });
  let timer = null;
  q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { want.q = q.value.trim(); draw(); }, 200); });
  const tabs = el("div", { class: "row tabs-row", role: "tablist", "aria-label": "Status" });
  const results = el("div", { class: "stack" });

  const matches = (x, ignoreState = false) => (ignoreState || !want.state || x.state === want.state)
    && (!want.role || x.role_id === want.role) && (!want.platform || x.platform === want.platform)
    && (!want.assigned || x.assigned === (want.assigned === "unknown" ? "" : want.assigned))
    && (!want.q || `${x.candidate_name} ${x.role_title}`.toLowerCase().includes(want.q.toLowerCase()));

  const when = (x) => x.scheduled_at ? fmtTime(x.scheduled_at)
    : x.booking_pending ? empty("Candidate choosing a time", "The candidate hasn't chosen a time yet")
    : x.state === "pending" ? empty("Candidate starts from their link", "No fixed time: the candidate starts from their link")
    : empty("-", "No time set");
  const meeting = (x) => x.meeting_link
    ? el("span", { class: "stack-tight" }, el("span", {}, x.platform || "Meeting"),
      el("a", { class: "small", href: x.meeting_link, target: "_blank", rel: "noopener noreferrer", onclick: (e) => e.stopPropagation(),
        title: x.meeting_link }, "Open meeting link"))
    : empty("Not set", "No meeting link yet");

  const groupToggle = el("button", { class: "btn small", type: "button", onclick: () => { grouped = !grouped; draw(); } });

  const table = (rows, showRole) => el("div", { class: "table-wrap" }, el("table", { class: "list" },
    el("thead", {}, el("tr", {}, ["Candidate", "When", "Assigned", "Meeting", "Status"].map((h, n) => el("th", { class: [2].includes(n) ? "hide-sm" : null }, h)))),
    el("tbody", {}, rows.map((x) => linkRow(`#/i/${x.id}`,
      el("td", {}, el("strong", {}, x.candidate_name || "Unnamed candidate", " ", demoTag(x.demo)), showRole ? el("div", { class: "muted small" }, x.role_title) : null),
      el("td", {}, when(x)),
      el("td", { class: "hide-sm" }, ASSIGNED_LABEL[x.assigned] ?? x.assigned),
      el("td", {}, meeting(x)),
      el("td", {}, pill(BOARD_KIND[x.state] || "draft", BOARD_LABEL[x.state] || x.state),
        x.invitation_undelivered && x.stage === "invitation_sent" ? el("div", { class: "small warn-text" }, "Invitation not delivered")
          : x.stage && STAGE_LABEL[x.stage] && STAGE_LABEL[x.stage] !== BOARD_LABEL[x.state] ? el("div", { class: "muted small" }, STAGE_LABEL[x.stage]) : null))))));

  // One section per role (alphabetical), with its count and a link to the role; one-off interviews (no role) last.
  function byRole(rows) {
    const groups = new Map();
    for (const x of rows) {
      const key = x.role_id || "";
      if (!groups.has(key)) groups.set(key, { title: x.role_id ? x.role_title : "One-off interviews", rows: [] });
      groups.get(key).rows.push(x);
    }
    const ordered = [...groups.entries()].sort(([a, ga], [b, gb]) => (a ? 0 : 1) - (b ? 0 : 1) || ga.title.localeCompare(gb.title));
    return el("div", { class: "stack" }, ordered.map(([id, g]) => el("section", { class: "stack-tight" },
      el("div", { class: "spread" },
        el("h3", { class: "flush" }, g.title, el("span", { class: "muted small" }, ` · ${g.rows.length} interview${g.rows.length === 1 ? "" : "s"}`)),
        id ? el("a", { class: "small", href: `#/r/${id}` }, "Open role") : null),
      table(g.rows, false))));
  }

  function draw() {
    const qs = new URLSearchParams(Object.entries({ ...want, list: grouped ? "" : "1" }).filter(([, v]) => v));
    groupToggle.textContent = grouped ? "Show as one list" : "Group by role";
    history.replaceState(null, "", `#/interviews${qs.toString() ? `?${qs}` : ""}`);
    const counts = {};
    for (const x of all) if (matches(x, true)) counts[x.state] = (counts[x.state] || 0) + 1;
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const tab = (id, label, n) => el("button", { class: `state-tab${want.state === id ? " on" : ""}`, type: "button", role: "tab",
      "aria-selected": String(want.state === id), onclick: () => { want.state = id; draw(); } }, label, el("span", { class: "state-count" }, String(n)));
    tabs.replaceChildren(tab("", "All", total), ...Object.entries(BOARD_LABEL)
      .filter(([id]) => !["expired", "failed"].includes(id) || counts[id] || want.state === id)
      .map(([id, label]) => tab(id, label, counts[id] || 0)));
    const rows = all.filter((x) => matches(x));
    if (!all.length) {
      results.replaceChildren(emptyState("users", el("p", {}, "No interviews yet"),
        el("p", { class: "muted small" }, "An interview appears here when a candidate is invited: automatically when an eligible application arrives (if the role invites automatically), or when you invite or add a candidate on a role."),
        el("a", { class: "btn primary gap-top", href: "#/roles" }, "Go to roles")));
      return;
    }
    if (!rows.length) { results.replaceChildren(el("p", { class: "muted" }, "No interviews match these filters.")); return; }
    // One role chosen: a single list is the same thing, without a heading.
    results.replaceChildren(grouped && !want.role ? byRole(rows) : table(rows, true));
  }

  view.replaceChildren(el("div", { class: "stack" },
    pageHead({ title: "Interviews", sub: "Every interview in this workspace." }),
    el("section", { class: "panel stack" },
      tabs,
      el("div", { class: "row filters" }, q, select("role", "Any role", roles),
        select("assigned", "Manual or automatic", [["manual", "Manual"], ["automatic", "Automatic"], ["unknown", "Not recorded"]]),
        select("platform", "Any platform", platforms.map((p) => [p, p])), groupToggle),
      results)));
  draw();
}

// The company's careers website (Settings → Recruitment sources, and the setup wizard's Careers website step): how it's
// set up, what connecting it takes, and the careers address. onChange(kind) runs after the choice is saved.
function careersSiteBox(canManage, onChange = null) {
  // The company's careers website. First asked how it's set up, since that decides what it needs from Recruence:
  // - "recruence": the site lists the jobs set up here (title, description, requirements, questions) from the careers
  //   address, and posts each application back to it with the job's ref;
  // - "own": the site keeps its own job pages and form, and its server only forwards each application and resume to
  //   the careers address, naming the job by its title. Recruence screens it against the role's requirements here.
  const careersBox = el("div", { class: "stack" });
  async function saveNewJobs(choice) {
    try { drawCareers(await api("/api/workspaces/current", { method: "PUT", body: { careers_new_jobs: choice } })); }
    catch (err) { toast(err.message); }
  }
  async function saveSiteKind(kind) {
    try { const ws = await api("/api/workspaces/current", { method: "PUT", body: { careers_site: kind } }); drawCareers(ws); if (onChange) onChange(kind); }
    catch (err) { toast(err.message); }
  }
  function drawCareers(ws) {
    if (!ws || !("careers_path" in ws)) { careersBox.replaceChildren(); return; }
    const url = ws.careers_url || (ws.careers_path ? `${location.origin}${ws.careers_path}` : "");
    const shown = url || "https://<your Recruence public address>/api/public/careers/<key>";
    const kind = ws.careers_site || "";
    const make = el("button", { class: "btn small", type: "button", onclick: async () => {
      if (url && !(await confirmDialog("Create a new address? The current one stops working, so update your careers site.", { title: "New careers address", confirmLabel: "Create" }))) return;
      try { drawCareers(await api("/api/workspaces/current/careers-key", { method: "POST" })); toast("Careers address created.", "good"); } catch (err) { toast(err.message); }
    } }, url ? "Replace address" : "Create address");
    const off = url ? el("button", { class: "link small", type: "button", onclick: async () => {
      if (!(await confirmDialog("Turn off the careers address? Applications sent to it are refused until you create a new one.", { title: "Turn off", confirmLabel: "Turn off", danger: true }))) return;
      try { drawCareers(await api("/api/workspaces/current/careers-key", { method: "DELETE" })); } catch (err) { toast(err.message); }
    } }, "Turn off") : null;
    const addressRow = el("div", { class: "stack-tight" },
      el("p", { class: "small flush" }, el("strong", {}, "Careers address")),
      url ? el("div", { class: "copy-row" }, el("input", { type: "text", readonly: true, value: url, "aria-label": "Careers address", onfocus: (e) => e.target.select() }), copyButton(url, "Copy")) : null,
      url && !ws.careers_url ? el("p", { class: "muted small flush" }, "This is this computer's own address: it works for a careers site running on the same computer. Set the Public URL (Settings → MeetStream) for a site elsewhere.") : null,
      el("div", { class: "row" }, make, off));

    const choices = [
      ["recruence", "Recruence lists the jobs", "The site shows the jobs, requirements and application questions you set up here, and candidates apply on it. Change a job here and the site follows."],
      ["own", "My website has its own job pages", "The site keeps its own job descriptions and application form. It only sends each application and resume to Recruence, which screens it against the role's requirements here."],
    ];
    const question = el("div", { class: "mode-cards", role: "radiogroup", "aria-label": "How your careers website is set up" },
      choices.map(([v, title, desc]) => el("label", { class: "mode-card" },
        el("input", { type: "radio", name: "careers-site", value: v, checked: kind === v, disabled: !canManage, onchange: () => saveSiteKind(v) }),
        el("span", {}, el("strong", {}, title), el("span", { class: "small muted" }, desc)))));

    const step = (n, ...body) => el("li", {}, ...body);
    const fields = (...names) => names.flatMap((n, i) => [i ? ", " : "", el("code", {}, n)]);
    const curl = (lines) => el("pre", { class: "setup-cmd" }, lines.join(" \\\n  "));
    let steps = null;
    if (kind === "recruence") {
      steps = el("div", { class: "stack-tight" },
        el("ol", { class: "plain stack-tight small" },
          step(1, "Set up each role and publish it on its Review step. Only published roles are listed."),
          step(2, "Create the careers address below and give it to whoever runs your site. Keep it on the site's server: anyone who has it can apply."),
          step(3, "The site's server reads the job list from the address (", el("code", {}, "GET"), ", JSON): each job's ", ...fields("ref", "title", "job_description", "requirements", "questions"), " and whether it's ", el("code", {}, "open"), "."),
          step(4, "It posts each application to the same address (", el("code", {}, "POST"), ", multipart/form-data) with the job's ", el("code", {}, "job_ref"), ", ", ...fields("name", "email", "phone", "linkedin", "message"), ", the answers as ", el("code", {}, "q_<question id>"), " and the ", el("code", {}, "resume"), " file."),
          step(5, "The answer says what happened: 201 received (screened in the background), 404 the job isn't published any more, 409 already applied or the job is closed, 422 a field or a required answer is missing.")),
        curl([`curl ${shown}`]));
    } else if (kind === "own") {
      const newJobs = ws.careers_new_jobs || "manual";
      const jobChoice = el("div", { class: "mode-cards", role: "radiogroup", "aria-label": "New jobs on your site" },
        [["manual", "I add each role myself", "Before a job goes live on your site, create its role here with the same title and publish it. Applications for a title Recruence doesn't have are refused."],
         ["draft", "Add new jobs automatically", "The first application for a new job title creates a draft role with that title. Applications are kept; add the requirements and publish the role, and they're screened then."]]
          .map(([v, title, desc]) => el("label", { class: "mode-card" },
            el("input", { type: "radio", name: "careers-new-jobs", value: v, checked: newJobs === v, disabled: !canManage, onchange: () => saveNewJobs(v) }),
            el("span", {}, el("strong", {}, title), el("span", { class: "small muted" }, desc)))));
      steps = el("div", { class: "stack-tight" },
        el("p", { class: "small flush" }, el("strong", {}, "When you add a new job to your site")),
        jobChoice,
        el("ol", { class: "plain stack-tight small" },
          newJobs === "draft"
            ? step(1, "Nothing to do in advance: a new job title on your site becomes a draft role here with its first application. Open the role, set its requirements, approve the questions and publish it; the waiting applications are screened then.")
            : step(1, "Create each role here with the same title as on your site, set its requirements, and publish it on its Review step. Recruence doesn't show it anywhere: publishing only lets it take applications."),
          step(2, "Create the careers address below and give it to whoever runs your site. Keep it on the site's server: anyone who has it can apply."),
          step(3, "Where your site's server receives an application today, also send it to the address (", el("code", {}, "POST"), ", multipart/form-data): ",
            el("code", {}, "role"), " (the job's title, as it is here), ", ...fields("name", "email"), ", optionally ", ...fields("phone", "linkedin", "portfolio", "message"), ", and the ", el("code", {}, "resume"), " file (PDF or Word, up to 5 MB).",
            newJobs === "draft" ? [" Also send ", el("code", {}, "job_description"), " (the job's text from your site): a new draft role starts from it."] : null),
          step(4, "Your site keeps its own notice and consent box; Recruence records that the site collected consent."),
          step(5, newJobs === "draft"
            ? "The answer says what happened: 201 received (screened in the background, or kept until a new role is published), 409 already applied or the role is closed, 422 a field is missing."
            : "The answer says what happened: 201 received (screened in the background), 404 no published role with that title, 409 already applied or the role is closed, 422 a field is missing.")),
        curl([`curl ${shown}`, `-F role="Senior Backend Engineer"`, `-F name="Ada Lovelace"`, `-F email=ada@example.com`, `-F resume=@resume.pdf`]),
        el("p", { class: "muted small flush" }, "A form that posts straight from the visitor's browser can use a role's own application API instead (the role's Setup tab); allow your site's address in Settings → MeetStream for that."));
    }
    careersBox.replaceChildren(...[
      el("p", { class: "small flush" }, el("strong", {}, "How is your careers website set up?")),
      question,
      kind ? null : el("p", { class: "muted small flush" }, "Choose one to see how to connect it."),
      steps,
      kind ? addressRow : null].filter(Boolean));
  }
  api("/api/workspaces/current").then(drawCareers).catch((err) => careersBox.replaceChildren(notice("bad", err.message)));
  return careersBox;
}

// Settings → Recruitment sources: Recruence's own ways in, and external ATS integrations (app/ats).
const UNTESTED_ATS = "Not tested – Organization account required";

function sourcesPanel() {
  const wrap = el("div", { class: "stack" });
  const canManage = !user || user.role === "admin";
  let providers = [];
  let info = { integrations: [], stages: [], fields: [] };

  const careersBox = careersSiteBox(canManage);
  const native = el("section", { class: "panel stack" }, sectionTitle("role", "Recruence's own sources"),
    el("ul", { class: "plain stack-tight" },
      el("li", {}, el("strong", {}, "Your careers website. "), "Candidates apply on your own site, which sends their applications here. How you connect it depends on how the site is set up: choose below."),
      el("li", {}, el("strong", {}, "Added by a recruiter. "), "Candidates added to a role by hand.")),
    careersBox,
    el("p", { class: "muted small flush" }, "Every source goes through the same screening, invitation, interview and report."));

  async function load() {
    try {
      [providers, info] = await Promise.all([api("/api/integrations/providers"), api("/api/integrations")]);
    } catch (err) { wrap.replaceChildren(native, notice("bad", err.message)); return; }
    const untested = providers.filter((p) => !p.verified).map((p) => p.label);
    const list = el("details", { class: "panel stack advanced-integrations", open: info.integrations.length > 0 },
      el("summary", {}, el("strong", {}, "Additional integrations (advanced)"),
        el("span", { class: "muted small" }, " · optional: connect an applicant tracking system")),
      el("div", { class: "stack" },
        el("div", { class: "spread" }, sectionTitle("swap", "External ATS integrations"),
          canManage ? el("button", { class: "btn small", type: "button", onclick: () => editor(null) }, icon("plus"), "Connect an ATS") : null),
        el("p", { class: "muted small flush" }, "ATS candidates are screened and interviewed here; results go back to the ATS."),
        untested.length ? notice("warn", `${untested.join(" and ")}: ${UNTESTED_ATS}. They haven't been validated against a real account yet, so don't rely on them for live hiring.`) : null,
        info.base_url ? null : notice("warn", "Set Recruence's public address (Settings → MeetStream) to get webhook URLs an ATS can reach."),
        info.integrations.length ? null : el("p", { class: "muted flush" }, "No ATS connected."),
        ...info.integrations.map(card)));
    wrap.replaceChildren(native, list);
  }

  const health = (h, label) => h ? el("span", { class: "small" }, pill(h.ok ? "ready" : "failed", `${label}: ${h.ok ? "OK" : "failed"}`),
    el("span", { class: "muted" }, ` ${h.message} · ${fmtTime(h.at)}`)) : null;

  function card(it) {
    const toggle = el("button", { class: "btn small", type: "button", onclick: async () => {
      const done = busy(toggle, "…");
      try { await api(`/api/integrations/${it.id}`, { method: "PUT", body: { enabled: !it.enabled } }); load(); } catch (err) { toast(err.message); done(); }
    } }, it.enabled ? "Pause" : "Enable");
    const test = el("button", { class: "btn small", type: "button", onclick: async () => {
      const done = busy(test, "Testing…");
      try { const r = await api(`/api/integrations/${it.id}/test`, { method: "POST" }); toast(r.message, r.ok ? "good" : "bad"); load(); } catch (err) { toast(err.message); done(); }
    } }, "Test connection");
    const remove = el("button", { class: "btn small danger", type: "button", onclick: async () => {
      if (!(await confirmDialog(`Disconnect ${it.name}? Its log is deleted. Candidates it sent stay in Recruence with their ATS ids; their results aren't sent any more.`, { title: "Disconnect ATS", confirmLabel: "Disconnect", danger: true }))) return;
      try { await api(`/api/integrations/${it.id}`, { method: "DELETE" }); toast("Disconnected.", "good"); load(); } catch (err) { toast(err.message); }
    } }, "Disconnect");
    const missing = Object.entries(it.secrets).filter(([, set]) => !set).map(([k]) => k.replace(/_/g, " "));
    return el("section", { class: "panel flat stack" },
      el("div", { class: "spread" }, el("div", {}, el("strong", {}, it.name), el("span", { class: "muted small" }, ` · ${it.provider_label}`)),
        it.verified === false ? pill("warn", "Not tested") : pill(it.enabled ? "ready" : "draft", it.enabled ? "Active" : "Paused")),
      it.verified === false ? notice("warn", `${it.status_note || UNTESTED_ATS}. This connection hasn't been validated with a real ${it.provider_label} account.`) : null,
      it.webhook_url ? el("div", { class: "copy-row" }, el("input", { type: "text", readonly: true, value: it.webhook_url, "aria-label": "Webhook URL" }), copyButton(it.webhook_url, "Copy")) : el("p", { class: "muted small flush" }, `Webhook path: ${it.webhook_path}`),
      missing.length ? notice("warn", `Not set yet: ${missing.join(", ")}.`) : null,
      el("div", { class: "stack-tight" }, health(it.health.last_inbound, "Last webhook"), health(it.health.last_test, "Last test")),
      el("p", { class: "muted small flush" }, `Screening: ${(it.settings.screening || "mia") === "mia" ? "Recruence screens every candidate" : "the ATS's decision is trusted"} · ${Object.keys(it.settings.role_map || {}).length} job(s) mapped to roles`),
      el("div", { class: "row" }, canManage ? el("button", { class: "btn small", type: "button", onclick: () => editor(it) }, "Edit") : null,
        canManage ? test : null, el("button", { class: "btn small", type: "button", onclick: () => logDialog(it) }, "Sync log"),
        canManage ? toggle : null, canManage ? remove : null));
  }

  async function editor(it) {
    let roleList = [];
    try { roleList = await api("/api/roles"); } catch { roleList = []; }
    const creating = !it;
    const provider = el("select", { "aria-label": "Provider", disabled: !creating }, providers.map((p) => el("option", { value: p.provider, selected: it && it.provider === p.provider },
      p.verified ? p.label : `${p.label} (${p.status_note || UNTESTED_ATS})`)));
    const name = el("input", { type: "text", maxlength: 80, value: it ? it.name : "", placeholder: "e.g. Greenhouse (production)" });
    const fieldsBox = el("div", { class: "stack" });
    const error = notice("bad"); error.hidden = true;
    let inputs = {};
    const LABELS = { webhook_secret: "Webhook secret / signature token", api_key: "API key", on_behalf_of_user_id: "Post notes as user id",
      perform_as_user_id: "Post notes as user id", stage_name: "Only act at stage (name)", stage_id: "Only act at stage (id)",
      api_base: "API base URL (leave empty for the default)", callback_url: "Callback URL for results (https)", field_map: "Field mapping (JSON: Recruence field → dotted path)" };

    function drawFields() {
      const p = providers.find((x) => x.provider === provider.value);
      const s = (it && it.settings) || {};
      inputs = {};
      const secretInputs = p.secret_fields.map((k) => {
        const set = it && it.secrets[k];
        inputs[`secret:${k}`] = el("input", { type: "password", autocomplete: "new-password", placeholder: set ? "Saved. Leave empty to keep it" : "Not set" });
        return el("label", { class: "field" }, LABELS[k] || k, inputs[`secret:${k}`]);
      });
      const settingInputs = p.setting_fields.map((k) => {
        if (k === "field_map") {
          inputs[k] = el("textarea", { rows: 4, placeholder: '{"email": "applicant.contact.email"}' }, s.field_map ? JSON.stringify(s.field_map, null, 2) : "");
        } else inputs[k] = el("input", { type: "text", value: s[k] || "", maxlength: 500 });
        return el("label", { class: "field" }, LABELS[k] || k, inputs[k]);
      });
      inputs.screening = el("select", { "aria-label": "Screening" }, el("option", { value: "mia", selected: (s.screening || "mia") === "mia" }, "Recruence screens every candidate (recommended)"),
        el("option", { value: "trust_ats", selected: s.screening === "trust_ats" }, "Trust the ATS's screening (candidates it sends as eligible go straight to invitation)"));
      const roleRows = el("div", { class: "stack criteria" });
      const addRoleRow = (ext = "", roleId = "") => {
        const e = el("input", { type: "text", value: ext, maxlength: 100, placeholder: "ATS job / posting id", "aria-label": "ATS job id" });
        const r = el("select", { "aria-label": "Recruence role" }, roleList.map((x) => el("option", { value: x.id, selected: x.id === roleId }, x.role_title)));
        const row = el("div", { class: "criterion" }, e, r, el("button", { class: "btn small", type: "button", onclick: () => row.remove() }, "Remove"));
        row.read = () => [e.value.trim(), r.value];
        roleRows.append(row);
      };
      Object.entries(s.role_map || {}).forEach(([k, v]) => addRoleRow(k, v));
      inputs.roleRows = roleRows;
      inputs.status = Object.fromEntries(info.stages.map((st) => [st, el("input", { type: "text", maxlength: 100, value: (s.status_map || {})[st] || "", placeholder: st.replace(/_/g, " ") })]));
      fieldsBox.replaceChildren(
        el("dl", { class: "facts small" }, Object.entries(p.capabilities).flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])),
        el("div", { class: "form-grid" }, ...secretInputs),
        el("p", { class: "muted small flush" }, "Credentials are stored encrypted and never shown again."),
        el("div", { class: "form-grid" }, ...settingInputs),
        el("label", { class: "field" }, "Screening", inputs.screening),
        el("div", { class: "field" }, el("span", {}, "Jobs → roles ", el("span", { class: "hint" }, "(an ATS job without a mapping becomes a new draft role)")), roleRows,
          roleList.length ? el("button", { class: "btn small self-start", type: "button", onclick: () => addRoleRow() }, icon("plus"), "Map a job") : el("p", { class: "muted small flush" }, "No roles yet.")),
        el("details", {}, el("summary", {}, "Result labels sent to the ATS"),
          el("div", { class: "form-grid gap-top" }, ...Object.entries(inputs.status).map(([st, i]) => el("label", { class: "field" }, st.replace(/_/g, " "), i)))));
    }
    provider.addEventListener("change", drawFields);
    drawFields();
    const save = el("button", { class: "btn primary", type: "button" }, creating ? "Connect" : "Save");
    const cancel = el("button", { class: "btn", type: "button", onclick: () => dlg.close() }, "Cancel");
    save.addEventListener("click", async () => {
      error.hidden = true;
      const p = providers.find((x) => x.provider === provider.value);
      const settings = { screening: inputs.screening.value, role_map: Object.fromEntries([...inputs.roleRows.children].map((r) => r.read()).filter(([k]) => k)),
        status_map: Object.fromEntries(Object.entries(inputs.status).map(([k, i]) => [k, i.value.trim()]).filter(([, v]) => v)) };
      for (const k of p.setting_fields) {
        if (k === "field_map") {
          const raw = inputs[k].value.trim();
          try { settings.field_map = raw ? JSON.parse(raw) : {}; } catch { error.textContent = "The field mapping isn't valid JSON."; error.hidden = false; return; }
        } else settings[k] = inputs[k].value.trim();
      }
      const secrets = Object.fromEntries(p.secret_fields.map((k) => [k, inputs[`secret:${k}`].value]).filter(([, v]) => v));
      const done = busy(save, "Saving…");
      try {
        await api(creating ? "/api/integrations" : `/api/integrations/${it.id}`, { method: creating ? "POST" : "PUT", body: { provider: provider.value, name: name.value.trim(), settings, secrets } });
        dlg.close(); toast(creating ? "Connected. Add the webhook URL in the ATS, then test the connection." : "Saved.", "good"); load();
      } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
    });
    const dlg = el("dialog", { class: "modal wide", "aria-labelledby": "ats-title" },
      el("h2", { id: "ats-title" }, creating ? "Connect an ATS" : `Edit ${it.name}`),
      el("div", { class: "form-grid" }, el("label", { class: "field" }, "Provider", provider), el("label", { class: "field" }, "Name", name)),
      fieldsBox, error, el("div", { class: "row end" }, cancel, save));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
  }

  async function logDialog(it) {
    const body = el("div", { class: "stack" });
    const retry = el("button", { class: "btn", type: "button" }, "Retry failed results");
    const close = el("button", { class: "btn", type: "button", onclick: () => dlg.close() }, "Close");
    const draw = async () => {
      let data;
      try { data = await api(`/api/integrations/${it.id}/events`); } catch (err) { body.replaceChildren(notice("bad", err.message)); return; }
      body.replaceChildren(data.events.length ? el("div", { class: "table-wrap" }, el("table", { class: "list" },
        el("thead", {}, el("tr", {}, ["When", "Direction", "Event", "Status", "Details"].map((h) => el("th", {}, h)))),
        el("tbody", {}, data.events.map((e) => el("tr", {}, el("td", { class: "small" }, fmtTime(e.ts)), el("td", {}, e.direction === "in" ? "In" : "Out"),
          el("td", { class: "small" }, e.kind), el("td", {}, pill({ ok: "ready", failed: "failed", rejected: "failed", pending: "scheduled" }[e.status] || "draft", e.status)),
          el("td", { class: "small" }, e.message, e.attempts > 1 ? el("span", { class: "muted" }, ` (${e.attempts} attempts)`) : null)))))) : el("p", { class: "muted" }, "Nothing yet."));
    };
    retry.addEventListener("click", async () => {
      const done = busy(retry, "Retrying…");
      try { const r = await api(`/api/integrations/${it.id}/retry`, { method: "POST" }); toast(`${r.sent} sent.`, "good"); await draw(); } catch (err) { toast(err.message); }
      done();
    });
    const dlg = el("dialog", { class: "modal wide", "aria-labelledby": "log-title" }, el("h2", { id: "log-title" }, `Sync log · ${it.name}`), body,
      el("div", { class: "row end" }, close, canManage ? retry : null));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
    await draw();
  }

  // External ATS integrations sit in a collapsed "Additional integrations (advanced)" section, after Recruence's own sources.
  // Greenhouse and Lever haven't been validated against an organisation's account: the server marks them unverified, and
  // they're labelled "Not tested" and never shown as ready (owner's decision, 14 November 2026).
  wrap.replaceChildren(native);
  load();
  return wrap;
}

function renderNotFound() {
  view.replaceChildren(emptyState("search", el("p", {}, "This page doesn't exist"),
    el("p", { class: "muted small" }, "The link may be out of date, or the interview or role was deleted."),
    el("div", { class: "row center gap-top" }, el("a", { class: "btn primary", href: "#/" }, "Go to the dashboard"), el("a", { class: "btn", href: "#/roles" }, "Roles"))));
}

function markNav(hash) {
  const section = /^#\/(roles|r\/)/.test(hash) ? "roles" : hash.startsWith("#/settings") ? "settings" : hash.startsWith("#/candidates") ? "candidates"
    : hash.startsWith("#/interviews") ? "interviews"
    : hash === "#/" || hash.startsWith("#/?") ? "dashboard" : /^#\/(i|s)\//.test(hash) ? "roles" : "";
  document.querySelectorAll(".mainnav a").forEach((a) => {
    if (a.dataset.nav === section) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
}

async function route() {
  closeSource();
  if (setupPreview && location.hash !== "#/setup-preview") { setupPreview = null; setupStep = null; enterSetupMode(false); }
  if (setupPending) { await renderSetup(); return; }
  const hash = location.hash || "#/";
  markNav(hash);
  setNav(false);
  // Only show the spinner if the page takes a moment, so quick pages don't flicker.
  const before = view.firstChild;
  clearPageSearch();
  const slow = setTimeout(() => { if (view.firstChild === before) view.replaceChildren(pageSkeleton()); }, 250);
  try {
    let m;
    if (hash === "#/account") await renderAccount();
    else if (hash === "#/new") { location.replace("#/"); return; }
    else if (hash === "#/interviews" || hash.startsWith("#/interviews?")) await renderInterviews();
    else if (hash === "#/settings" || hash.startsWith("#/settings?")) await renderSettings();
    else if (hash === "#/roles") await renderRoles();
    else if (hash === "#/candidates" || hash.startsWith("#/candidates?")) await renderCandidates();
    else if (hash === "#/roles/new") renderNewRole();
    else if ((m = hash.match(/^#\/r\/([a-f0-9]+)\/setup(?:\?step=(\w+))?$/))) await renderRoleSetup(m[1], m[2]);
    else if ((m = hash.match(/^#\/r\/([a-f0-9]+)(?:\?tab=(\w+))?$/))) await renderRole(m[1], m[2]);
    else if ((m = hash.match(/^#\/i\/([a-f0-9]+)$/))) await renderInterview(m[1]);
    else if ((m = hash.match(/^#\/s\/([a-f0-9]+)$/))) await renderSession(m[1]);
    else if (hash === "#/setup-preview") await renderSetupPreview();
    else if (hash === "#/" || hash.startsWith("#/?")) await renderDashboard();
    else renderNotFound();
  } catch (err) {
    if (err instanceof ApiError && err.message === "Please sign in") return;
    view.replaceChildren(emptyState("alert", el("p", {}, "Something went wrong"), notice("bad", err.message),
      el("a", { class: "btn gap-top", href: "#/" }, "Back to the dashboard")));
  } finally {
    clearTimeout(slow);
  }
  window.scrollTo(0, 0);
  $(".shell").scrollTo(0, 0); // on wide screens the workspace scrolls inside the frame
}

async function init() {
  health = await api("/api/health");
  // The application name from Settings > General.
  if (health.app_name) {
    document.title = health.app_name;
    document.querySelectorAll(".wordmark").forEach((w) => { w.textContent = health.app_name; });
    $(".sidebar .brand").setAttribute("aria-label", `${health.app_name}, home`);
  }
  window.addEventListener("hashchange", () => {
    if (location.hash.startsWith("#/reset/")) renderReset(location.hash.slice(8));
    else if (location.hash === "#/connect") renderConnect();
    else route();
  });
  if (location.hash.startsWith("#/reset/")) { renderReset(location.hash.slice(8)); return; }
  if (location.hash === "#/connect") { renderConnect(); return; }
  const setupState = await api("/api/setup/status");
  setupPending = !setupState.completed;
  await refreshAuth();
  if (!authInfo.signed_in && !(setupState.code_required && !authInfo.accounts)) { showSignin(); return; }
  if (authInfo.user && authInfo.user.via === "account" && !authInfo.user.workspace_id && !setupPending) { renderNoWorkspace(); return; }
  await route();
}

// ---------- accounts, workspaces and the database connection (app/workspace_api.py) ----------

async function refreshAuth() {
  authInfo = await api("/api/auth/status");
  const u = authInfo.user;
  user = u && (u.via === "account" || u.via === "proxy") ? u : null;
  meInfo = null;
  if (authInfo.signed_in && u) { try { meInfo = await api("/api/me"); } catch { meInfo = null; } }
  renderUser(authInfo);
}

// A full page outside the app shell (signing in, connecting a database, joining a workspace).
function authPage(...children) {
  enterSetupMode(true);
  const root = $("#setup-root");
  root.replaceChildren(el("div", { class: "setup-page auth-page" },
    el("header", { class: "setup-top" }, el("img", { class: "auth-logo", src: "/static/logo.png", alt: "" }),
      el("span", { class: "wordmark" }, (health && health.app_name) || "Recruence")),
    ...children.filter(Boolean)));
  const first = root.querySelector("input:not([type=hidden])");
  if (first) first.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

function authForm(title, intro, fields, submitLabel, onSubmit, after = []) {
  const error = notice("bad"); error.hidden = true;
  const go = el("button", { class: "btn primary block", type: "submit" }, submitLabel);
  const form = el("form", { class: "setup-card narrow stack" }, el("h1", {}, title), intro ? el("p", { class: "muted flush" }, intro) : null,
    ...fields, error, go, ...after);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    const done = busy(go, "…");
    try { await onSubmit(); } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
  });
  return form;
}

function labelled(label, input, hint = null) {
  return el("label", { class: "field" }, hint ? el("span", {}, `${label} `, el("span", { class: "hint" }, hint)) : label, input);
}

async function enterApp() {
  enterSetupMode(false);
  await refreshAuth();
  if (authInfo.user && authInfo.user.via === "account" && !authInfo.user.workspace_id) { renderNoWorkspace(); return; }
  if (!location.hash || location.hash === "#/connect" || location.hash.startsWith("#/reset/")) location.hash = "#/";
  await route();
}

function connectionLine() {
  return el("p", { class: "muted small flush center" }, icon("database"), " Signing in to the connected database. ",
    el("a", { href: "#/connect" }, "Use another database"));
}

function renderLogin(message = null) {
  const email = el("input", { type: "email", autocomplete: "username", required: true, maxlength: 320 });
  const password = el("input", { type: "password", autocomplete: "current-password", required: true, maxlength: 200 });
  const problem = authInfo && authInfo.database_problem
    ? notice("warn", authInfo.database_problem, " ", el("a", { href: "#/connect" }, "Reconnect")) : null;
  authPage(message ? notice("good", message) : null, problem, authForm("Sign in", null, [labelled("Email", email), labelled("Password", password)], "Sign in", async () => {
    await api("/api/auth/account-login", { method: "POST", body: { email: email.value, password: password.value } });
    await enterApp();
  }, [
    el("div", { class: "spread small" },
      el("button", { class: "link", type: "button", onclick: () => renderForgot() }, "Forgot your password?"),
      el("button", { class: "link", type: "button", onclick: () => renderRegister() }, "Create an account")),
    connectionLine(),
  ]));
}

function renderRegister(first = false) {
  const name = el("input", { type: "text", autocomplete: "name", required: true, maxlength: 200 });
  const email = el("input", { type: "email", autocomplete: "email", required: true, maxlength: 320 });
  const password = el("input", { type: "password", autocomplete: "new-password", required: true, minlength: 8, maxlength: 200 });
  const confirm = el("input", { type: "password", autocomplete: "new-password", required: true, maxlength: 200 });
  const wsName = el("input", { type: "text", maxlength: 120, placeholder: "Your company or team" });
  const joinCode = el("input", { type: "text", maxlength: 40, autocomplete: "off", placeholder: "ABCD-EFGH-IJKL" });
  authPage(authForm(first ? "Create the first account" : "Create an account",
    first ? "This database has no accounts yet. You'll administer it and own its first workspace."
      : "Any email address works, Gmail included. You get a workspace of your own; to join a team's, enter the joining code its owner gave you (a database set up on another computer only accepts new accounts with one).",
    [labelled("Full name", name), labelled("Email", email), labelled("Password", password, "(at least 8 characters)"), labelled("Confirm password", confirm),
      labelled("Your workspace's name", wsName, "(optional: every account gets a workspace of its own)"),
      first ? null : labelled("Joining code", joinCode, "(to also join a team's workspace)")].filter(Boolean),
    first ? "Create account" : "Create account", async () => {
      if (password.value !== confirm.value) throw new Error("The two passwords don't match.");
      const r = await api("/api/auth/register", { method: "POST", body: { name: name.value, email: email.value, password: password.value, workspace_name: wsName.value, join_code: joinCode.value.trim() } });
      if (r.join_requested) toast(`Account created. Your request to join ${r.join_requested} is waiting for the owner's approval.`, "good", 9000);
      await enterApp();
    }, first ? [connectionLine()] : [el("button", { class: "link small self-start", type: "button", onclick: () => renderLogin() }, "I already have an account"), connectionLine()]));
}

function renderForgot() {
  const email = el("input", { type: "email", autocomplete: "username", required: true, maxlength: 320 });
  authPage(authForm("Reset your password", "We'll email you a link to choose a new one.", [labelled("Email", email)], "Send the link", async () => {
    const r = await api("/api/auth/reset-request", { method: "POST", body: { email: email.value } });
    renderLogin(r.email_enabled ? "If that address has an account here, a reset link is on its way. It works for an hour."
      : "Email isn't set up on this server, so no link can be sent. Ask an administrator to help you back in.");
  }, [el("button", { class: "link small self-start", type: "button", onclick: () => renderLogin() }, "Back to sign in")]));
}

function renderReset(token) {
  const password = el("input", { type: "password", autocomplete: "new-password", required: true, minlength: 8, maxlength: 200 });
  const confirm = el("input", { type: "password", autocomplete: "new-password", required: true, maxlength: 200 });
  authPage(authForm("Choose a new password", null, [labelled("New password", password, "(at least 8 characters)"), labelled("Confirm password", confirm)], "Save and sign in", async () => {
    if (password.value !== confirm.value) throw new Error("The two passwords don't match.");
    await api("/api/auth/reset", { method: "POST", body: { token, password: password.value } });
    history.replaceState(null, "", "#/");
    await enterApp();
  }));
}

async function switchDatabase() {
  if (!(await confirmDialog("Switching the database signs everyone out of this one. You'll choose or enter another database, then sign in "
    + "with an account from it. Nothing is copied or deleted in either database.", { title: "Switch database", confirmLabel: "Sign out and continue" }))) return;
  try {
    await api("/api/connection/switch-start", { method: "POST" });
    history.replaceState(null, "", "#/connect");
    await renderConnect();
  } catch (err) { toast(err.message); }
}

// The connection screen: which database this server uses, saved connections, and connecting another one.
async function renderConnect(message = null) {
  const st = await api("/api/connection/status");
  if (!st.allowed) {
    // Only from another computer: on the server itself the connection screen opens straight away.
    const code = el("input", { type: "text", autocomplete: "one-time-code", maxlength: 12, placeholder: "ABCD-1234" });
    const ask = el("button", { class: "btn small self-start", type: "button", onclick: async () => {
      const r = await api("/api/connection/request-code", { method: "POST" });
      toast(r.logged ? "A code is in the server log now (valid 15 minutes)." : "A code was printed less than a minute ago; use that one.", "good", 6000);
    } }, "Print a code in the server log");
    const how = el("details", { class: "how" }, el("summary", {}, "Where do I get a code?"),
      el("div", { class: "stack-tight" }, ask, el("pre", { class: "setup-cmd" }, "docker compose logs app | grep \"connection code\"")));
    authPage(authForm("Change database", "From another computer, enter a one-time code from the server, or sign in as an administrator.",
      [labelled("Connection code", code), how], "Continue", async () => {
        await api("/api/connection/unlock", { method: "POST", body: { code: code.value } });
        await renderConnect();
      }, [el("button", { class: "link small self-start", type: "button", onclick: () => { history.replaceState(null, "", "#/"); renderLogin(); } }, "Back to sign in")]));
    return;
  }
  const error = notice("bad"); error.hidden = true;
  const connected = async (r) => {
    toast(`Connected to ${r.connected}.`, "good", 6000);
    history.replaceState(null, "", "#/");
    if (r.has_accounts) renderLogin("Connected. Sign in with an account from this database.");
    else renderRegister(true);
  };
  const list = el("div", { class: "stack" }, (st.profiles || []).map((pr) => el("div", { class: "setup-row" },
    el("span", {}, el("strong", {}, pr.name)),
    el("span", { class: "row" },
      pr.active ? pill("ready", "In use") : el("button", { class: "btn small", type: "button", onclick: async (e) => {
        const done = busy(e.currentTarget, "Connecting…");
        try { await connected(await api("/api/connection/connect", { method: "POST", body: { profile_id: pr.id } })); } catch (err) { error.textContent = err.message; error.hidden = false; done(); }
      } }, "Connect"),
      !pr.active && pr.id !== "local" ? el("button", { class: "btn small", type: "button", "aria-label": `Remove ${pr.name}`, onclick: async () => {
        if (!(await confirmDialog(`Forget the saved connection “${pr.name}”? The database itself isn't touched.`, { title: "Remove connection", confirmLabel: "Remove" }))) return;
        try { await api(`/api/connection/profiles/${pr.id}`, { method: "DELETE" }); renderConnect(); } catch (err) { toast(err.message); }
      } }, "Remove") : null))));
  const name = el("input", { type: "text", maxlength: 80, placeholder: "Company" });
  const url = el("input", { type: "password", autocomplete: "off", placeholder: "postgresql://user:password@host:5432/database" });
  const save = el("input", { type: "checkbox", id: "conn-save", checked: true });
  const existing = el("input", { type: "checkbox", id: "conn-existing" });
  const existingBox = el("label", { class: "check", for: "conn-existing", hidden: true }, existing, el("span", {}, "It already holds Recruence data: use it as it is. Nothing is copied, changed or deleted."));
  const result = el("p", { class: "notice", hidden: true });
  const test = el("button", { class: "btn", type: "button", onclick: async () => {
    const done = busy(test, "Testing…");
    try {
      const r = await api("/api/connection/test", { method: "POST", body: { url: url.value.trim() } });
      result.className = `notice ${r.ok ? "good" : "bad"}`;
      result.textContent = r.ok ? `${r.message} ${r.summary}` : r.message;
      result.hidden = false;
      existingBox.hidden = !(r.ok && r.has_mia_tables);
    } catch (err) { result.className = "notice bad"; result.textContent = err.message; result.hidden = false; }
    done();
  } }, "Test connection");
  const connect = el("button", { class: "btn primary", type: "button", onclick: async () => {
    error.hidden = true;
    const done = busy(connect, "Connecting…");
    try {
      await connected(await api("/api/connection/connect", { method: "POST", body: { url: url.value.trim(), name: name.value, save: save.checked, use_existing: existing.checked } }));
    } catch (err) {
      error.textContent = err.message; error.hidden = false;
      if (/already holds Recruence data/.test(err.message)) existingBox.hidden = false;
      done();
    }
  } }, "Connect");
  authPage(message ? notice("good", message) : null,
    el("section", { class: "setup-card stack" }, el("h1", {}, "Database connection"),
      el("p", { class: "muted flush small" }, "Each database has its own accounts and data. Nothing is copied or deleted."),
      st.problem ? notice("warn", st.problem) : null,
      stateRow("In use now", el("code", { class: "conn-target", title: st.current }, st.current), [st.source === "env" ? "draft" : "ready", st.source === "env" ? "From DATABASE_URL" : "Connected"]),
      st.env_locked ? notice("info", "DATABASE_URL in the server's environment decides the database. Remove it there to connect another one here.") : null,
      st.env_locked ? null : el("div", { class: "stack" }, el("h2", {}, "Saved connections"), list),
      st.env_locked ? null : el("div", { class: "stack" }, el("h2", {}, "Connect another database"),
        el("div", { class: "form-grid" }, labelled("Name", name), labelled("Connection string", url)),
        el("label", { class: "check", for: "conn-save" }, save, el("span", {}, "Remember this connection")),
        existingBox, result, el("div", { class: "row" }, test, connect)),
      error,
      el("div", { class: "setup-footer" }, st.has_accounts
        ? el("button", { class: "btn", type: "button", onclick: () => { history.replaceState(null, "", "#/"); renderLogin(); } }, "Sign in to the current database")
        : el("button", { class: "btn", type: "button", onclick: () => { history.replaceState(null, "", "#/"); renderRegister(true); } }, "Create the first account here"))));
}

// Signed in, but in no workspace yet: join one with a code (and wait for approval), or create one.
async function renderNoWorkspace() {
  meInfo = await api("/api/me");
  const approved = meInfo.workspaces.filter((w) => w.status === "approved");
  if (approved.length) {
    await api("/api/workspaces/switch", { method: "POST", body: { workspace_id: approved[0].id } });
    await enterApp();
    return;
  }
  authPage(workspaceChoices(true), el("div", { class: "row center" },
    el("button", { class: "link small", type: "button", onclick: async () => { await api("/api/auth/logout", { method: "POST" }); location.reload(); } }, "Sign out")));
}

function workspaceChoices(standalone = false) {
  const requests = (meInfo.workspaces || []).filter((w) => w.status !== "approved");
  const code = el("input", { type: "text", maxlength: 20, placeholder: "ABCD-EFGH-JKLM", autocomplete: "off" });
  const join = authForm("Join a workspace", "Enter the joining code from the workspace's owner. They approve your request before you see anything.",
    [labelled("Joining code", code)], "Ask to join", async () => {
      const r = await api("/api/workspaces/join", { method: "POST", body: { code: code.value } });
      toast(`Request sent to ${r.workspace.name}. You'll get in once the owner approves it.`, "good", 8000);
      meInfo = await api("/api/me");
      if (standalone) renderNoWorkspace(); else route();
    }, requests.length ? [el("div", { class: "stack" }, el("strong", { class: "small" }, "Your requests"),
      ...requests.map((w) => el("div", { class: "setup-row" }, el("span", {}, w.name),
        pill(w.status === "pending" ? "scheduled" : "failed", w.status === "pending" ? "Waiting for approval" : "Not approved"))),
      standalone ? el("button", { class: "btn small self-start", type: "button", onclick: () => renderNoWorkspace() }, "Check again") : null)] : []);
  const name = el("input", { type: "text", maxlength: 120, required: true });
  const desc = el("input", { type: "text", maxlength: 500 });
  const create = meInfo.user ? authForm("Create a workspace", "A separate space for another team or company: its own roles, candidates and members.",
    [labelled("Name", name), labelled("Description", desc, "(optional)")], "Create workspace", async () => {
      await api("/api/workspaces", { method: "POST", body: { name: name.value, description: desc.value } });
      location.hash = "#/";
      location.reload();
    }) : null;
  return el("div", { class: "auth-choices" }, join, create);
}

// Your account: name, password, and your workspaces.
async function renderAccount() {
  meInfo = await api("/api/me");
  const me = meInfo.user;
  if (me.via !== "account") {
    view.replaceChildren(el("div", { class: "stack" }, pageHead({ title: "Your account" }),
      notice("info", "You're signed in without an account (access token, identity proxy or this computer). Accounts are created on the sign-in screen.")));
    return;
  }
  const name = el("input", { type: "text", value: me.name, maxlength: 200 });
  const current = el("input", { type: "password", autocomplete: "current-password", maxlength: 200 });
  const next = el("input", { type: "password", autocomplete: "new-password", minlength: 8, maxlength: 200 });
  const confirm = el("input", { type: "password", autocomplete: "new-password", maxlength: 200 });
  const saveName = el("button", { class: "btn", type: "button", onclick: async () => {
    try { await api("/api/me", { method: "PUT", body: { name: name.value } }); toast("Saved.", "good", 2500); await refreshAuth(); } catch (err) { toast(err.message); }
  } }, "Save");
  const savePw = el("button", { class: "btn", type: "button", onclick: async () => {
    if (next.value !== confirm.value) { toast("The two new passwords don't match."); return; }
    try {
      await api("/api/me/password", { method: "POST", body: { current: current.value, new: next.value } });
      toast("Password changed. Other browsers were signed out.", "good", 5000);
      current.value = next.value = confirm.value = "";
    } catch (err) { toast(err.message); }
  } }, "Change password");
  const rows = meInfo.workspaces.map((w) => el("tr", {}, el("td", {}, el("strong", {}, w.name), w.id === meInfo.workspace_id ? pill("ready", "Current") : null),
    el("td", {}, w.role), el("td", {}, pill(w.status === "approved" ? "ready" : w.status === "pending" ? "scheduled" : "failed",
      { approved: "Member", pending: "Waiting for approval", rejected: "Not approved" }[w.status] || w.status))));
  view.replaceChildren(el("div", { class: "stack" }, pageHead({ title: "Your account" }),
    el("section", { class: "panel stack" }, sectionTitle("user", "Profile"),
      el("div", { class: "form-grid" }, labelled("Name", name), labelled("Email", el("input", { type: "email", value: me.email, readonly: true }))),
      el("div", { class: "row" }, saveName)),
    el("section", { class: "panel stack" }, sectionTitle("shield", "Password"),
      el("div", { class: "form-grid" }, labelled("Current password", current), labelled("New password", next, "(at least 8 characters)"), labelled("Confirm new password", confirm)),
      el("div", { class: "row" }, savePw)),
    el("section", { class: "panel stack" }, sectionTitle("users", "Your workspaces"),
      el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {}, ["Workspace", "Role", "Status"].map((h) => el("th", {}, h)))), el("tbody", {}, rows)))),
    workspaceChoices(false)));
}

// Settings → Workspace: details, who hears about reports, the joining code, and members with their requests.
function workspacePanel() {
  const holder = el("div", { class: "stack" }, el("p", { class: "loading" }, "Loading…"));
  const draw = async () => {
    let ws;
    try { ws = await api("/api/workspaces/current"); } catch (err) { holder.replaceChildren(notice("bad", err.message)); return; }
    const manage = ["owner", "admin"].includes(ws.role);
    const isOwner = ws.role === "owner";
    const name = el("input", { type: "text", value: ws.name, maxlength: 120, disabled: !manage });
    const desc = el("input", { type: "text", value: ws.description || "", maxlength: 500, disabled: !manage });
    const notify = el("input", { type: "text", value: (ws.notify_emails || []).join(", "), disabled: !manage, placeholder: "lead@example.com, hr@example.com" });
    const candNotices = el("input", { type: "checkbox", id: "ws-cand", checked: ws.candidate_notices, disabled: !manage });
    const company = el("input", { type: "text", value: ws.company_name || "", maxlength: 120, disabled: !manage, placeholder: "Lumen Labs" });
    const contact = el("input", { type: "text", value: ws.support_contact || "", maxlength: 200, disabled: !manage, placeholder: "careers@lumen.example" });
    const verify = el("select", { disabled: !manage, "aria-label": "Interview link verification" },
      el("option", { value: "email_code", selected: ws.gateway_verification !== "none" }, "A code emailed to the candidate's application address (recommended)"),
      el("option", { value: "none", selected: ws.gateway_verification === "none" }, "No code (the first device to start keeps the interview)"));
    const save = el("button", { class: "btn primary", type: "button", disabled: !manage, onclick: async () => {
      try {
        await api("/api/workspaces/current", { method: "PUT", body: { name: name.value, description: desc.value,
          notify_emails: notify.value.split(/[,\s]+/).filter(Boolean), candidate_notices: candNotices.checked,
          company_name: company.value.trim(), support_contact: contact.value.trim(), gateway_verification: verify.value } });
        toast("Saved.", "good", 2500);
        await refreshAuth();
      } catch (err) { toast(err.message); }
    } }, "Save");
    const parts = [el("section", { class: "panel stack" }, sectionTitle("users", "Workspace"),
      el("div", { class: "form-grid" }, labelled("Name", name), labelled("Description", desc, "(optional)")),
      labelled("Interview report notes go to", notify, "(comma-separated; also NOTIFY_EMAILS)"),
      el("label", { class: "check", for: "ws-cand" }, candNotices, el("span", {}, "Email candidates a short note when their interview is received (no scores or AI output)")),
      el("h3", { class: "flush" }, "What candidates see"),
      el("p", { class: "muted small flush" }, "Candidates see your company, never this product: on their interview page and in their emails."),
      el("div", { class: "form-grid" }, labelled("Company name", company), labelled("Contact for candidates", contact, "(an email or phone number)")),
      el("h3", { class: "flush" }, "Interview link security"),
      labelled("Before the interview opens", verify),
      el("p", { class: "muted small flush" }, "Each link works for one candidate on one device. Unusual activity is flagged for you, never rejected automatically."),
      el("div", { class: "row" }, save))];
    if (manage && ws.sharing_problem) {
      parts.push(el("section", { class: "panel stack" }, sectionTitle("plus", "Invite people"), notice("info", ws.sharing_problem)));
    } else if (manage) {
      const days = el("select", { "aria-label": "Code expires after" }, [["1", "1 day"], ["7", "7 days"], ["30", "30 days"], ["", "Never"]].map(([v, l]) => el("option", { value: v, selected: v === "7" }, l)));
      const makeCode = el("button", { class: "btn", type: "button", onclick: async () => {
        try { await api("/api/workspaces/current/code", { method: "POST", body: { days: days.value ? Number(days.value) : null } }); draw(); } catch (err) { toast(err.message); }
      } }, ws.join_code ? "New code" : "Create a joining code");
      parts.push(el("section", { class: "panel stack" }, sectionTitle("plus", "Joining code"),
        el("p", { class: "muted small flush" }, "Share it with people who should join. A code only sends you a request; nobody gets in until you approve them."),
        ws.join_code ? el("div", { class: "row" }, el("code", { class: "join-code" }, ws.join_code), copyButton(ws.join_code, "Copy"),
          el("span", { class: "muted small" }, ws.join_code_expires_at ? `Expires ${fmtTime(ws.join_code_expires_at)}` : "Doesn't expire"),
          el("button", { class: "btn small", type: "button", onclick: async () => { await api("/api/workspaces/current/code", { method: "DELETE" }); draw(); } }, "Turn off")) : null,
        el("div", { class: "row" }, days, makeCode)));
      const act = (label, fn, cls = "btn small") => el("button", { class: cls, type: "button", onclick: async () => { try { await fn(); draw(); } catch (err) { toast(err.message); } } }, label);
      const rows = ws.members.map((m) => {
        const mine = authInfo.user && m.id === authInfo.user.user_id;
        const actions = m.status === "pending"
          ? [act("Approve", () => api(`/api/workspaces/current/members/${m.id}/decision`, { method: "POST", body: { approve: true, role: "recruiter" } }), "btn small primary"),
            act("Reject", () => api(`/api/workspaces/current/members/${m.id}/decision`, { method: "POST", body: { approve: false } }))]
          : m.status === "approved" && m.role !== "owner" && !mine ? [
            isOwner ? el("select", { "aria-label": `Role of ${m.name}`, onchange: async (e) => {
              try { await api(`/api/workspaces/current/members/${m.id}`, { method: "PUT", body: { role: e.target.value } }); toast("Role changed.", "good", 2500); } catch (err) { toast(err.message); draw(); }
            } }, ["admin", "recruiter"].map((r) => el("option", { value: r, selected: r === m.role }, ROLE_NAMES[r]))) : null,
            act("Remove", async () => {
              if (await confirmDialog(`Remove ${m.name} from this workspace? They lose access at once.`, { title: "Remove member", confirmLabel: "Remove", danger: true })) {
                await api(`/api/workspaces/current/members/${m.id}`, { method: "DELETE" });
              }
            }),
            isOwner ? act("Make owner", async () => {
              if (await confirmDialog(`Make ${m.name} the owner? You become an admin.`, { title: "Transfer ownership", confirmLabel: "Transfer" })) {
                await api("/api/workspaces/current/transfer", { method: "POST", body: { user_id: m.id } });
                await refreshAuth();
              }
            }) : null]
            : m.status === "rejected" ? [act("Remove", () => api(`/api/workspaces/current/members/${m.id}`, { method: "DELETE" }))] : [];
        return el("tr", {}, el("td", {}, el("strong", {}, m.name), el("div", { class: "muted small" }, m.email)),
          el("td", {}, ROLE_NAMES[m.role] || m.role),
          el("td", {}, pill(m.status === "approved" ? "ready" : m.status === "pending" ? "scheduled" : "no",
            { approved: "Member", pending: "Wants to join", rejected: "Not approved" }[m.status])),
          el("td", {}, el("div", { class: "row" }, ...actions.filter(Boolean))));
      });
      const pending = ws.members.filter((m) => m.status === "pending").length;
      parts.push(el("section", { class: "panel stack" }, sectionTitle("users", "Members", pending ? pill("scheduled", `${pending} waiting`) : null),
        el("p", { class: "muted small flush" }, "Owner and admins manage the workspace and its members; recruiters work with roles, candidates and reports."),
        el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {}, ["Person", "Role", "Status", ""].map((h) => el("th", {}, h)))), el("tbody", {}, rows)))));
    }
    holder.replaceChildren(...parts);
  };
  draw();
  return holder;
}

// Settings → Access: everyone with an account in this database, for the installation's admins.
function accountsPanel() {
  const box = el("section", { class: "panel stack" }, sectionTitle("users", "Accounts"), el("p", { class: "loading" }, "Loading…"));
  const draw = async () => {
    let data;
    try { data = await api("/api/accounts"); } catch (err) { box.replaceChildren(sectionTitle("users", "Accounts"), notice("bad", err.message)); return; }
    const rule = (key, label) => {
      const input = el("input", { type: "checkbox", id: `rule-${key}`, checked: data[key], onchange: async () => {
        try { await api("/api/accounts/rules", { method: "PUT", body: { [key]: input.checked } }); toast("Saved.", "good", 2500); } catch (err) { toast(err.message); input.checked = !input.checked; }
      } });
      return el("label", { class: "check", for: `rule-${key}` }, input, el("span", {}, label));
    };
    const me = authInfo && authInfo.user ? authInfo.user.user_id : null;
    const rows = data.users.map((u) => {
      const temp = el("button", { class: "btn small", type: "button", disabled: u.id === me, onclick: async () => {
        if (!(await confirmDialog(`Set a temporary password for ${u.name}? Their current password stops working and they're signed out everywhere.`,
          { title: "Temporary password", confirmLabel: "Set it" }))) return;
        try {
          const r = await api(`/api/accounts/${u.id}/temporary-password`, { method: "POST" });
          box.querySelector(".temp-result").replaceChildren(el("div", { class: "notice good stack" },
            el("strong", {}, `Temporary password for ${u.email}. Shown once: give it to them privately.`),
            el("div", { class: "copy-row" }, el("input", { type: "text", readonly: true, value: r.password, "aria-label": "Temporary password" }), copyButton(r.password, "Copy")),
            el("span", { class: "small" }, "They sign in with it, then choose their own in Your account.")));
        } catch (err) { toast(err.message); }
      } }, "Temporary password");
      const admin = el("button", { class: "btn small", type: "button", onclick: async () => {
        try { await api(`/api/accounts/${u.id}/admin`, { method: "PUT", body: { is_admin: !u.is_admin } }); draw(); } catch (err) { toast(err.message); }
      } }, u.is_admin ? "Remove admin" : "Make admin");
      return el("tr", {}, el("td", {}, el("strong", {}, u.name), u.id === me ? pill("ready", "You") : null, el("div", { class: "muted small" }, u.email)),
        el("td", {}, u.is_admin ? pill("ready", "Installation admin") : empty("Member")),
        el("td", {}, fmtTime(u.created_at)), el("td", {}, el("div", { class: "row" }, temp, admin)));
    });
    box.replaceChildren(sectionTitle("users", "Accounts"),
      el("p", { class: "muted small flush" }, "Everyone with an account in this database."),
      rule("open_registration", "Anyone who can open this app can create an account (they see nothing until a workspace approves them)"),
      el("div", { class: "temp-result" }),
      el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {}, ["Person", "Installation", "Joined", ""].map((h) => el("th", {}, h)))), el("tbody", {}, rows))));
  };
  draw();
  return box;
}

// Settings → MeetStream: everything an interview in a meeting needs, and where each stands (GET /api/settings/readiness).
function readinessPanel() {
  const box = el("section", { class: "panel stack" }, sectionTitle("check", "Interview readiness"), el("p", { class: "loading" }, "Checking…"));
  const KIND = { ok: ["ready", "Ready"], unchecked: ["scheduled", "Set, not tested"], missing: ["failed", "Missing"], failed: ["failed", "Test failed"], manual: ["draft", "Test in a call"] };
  api("/api/settings/readiness").then((r) => {
    box.replaceChildren(...[sectionTitle("check", "Interview readiness", pill(r.ready ? "ready" : "failed", r.ready ? "Ready to run interviews" : "Not ready yet")),
      el("p", { class: "muted small flush" }, "Everything an interview in a meeting needs."),
      el("ul", { class: "plain readiness" }, r.items.map((i) => el("li", {},
        el("div", { class: "spread" }, el("span", {}, el("strong", {}, i.label), i.required ? null : el("span", { class: "muted small" }, " · optional")),
          pill(...(KIND[i.status] || ["draft", i.status]))),
        el("p", { class: "muted small flush break" }, i.detail),
        i.status !== "ok" && i.tab ? el("a", { class: "small", href: `#/settings?tab=${i.tab}`, onclick: () => setTimeout(route, 0) }, "Set it up") : null))),
      r.desktop ? notice("info", `Desktop app: Recruence listens on this computer only (port ${r.local_port}). For MeetStream and candidates to reach it, `
        + "point a tunnel (e.g. cloudflared) at that port and enter the tunnel's https address as the public address. Without one, everything but meeting interviews works locally.") : null].filter(Boolean));
  }).catch((err) => box.replaceChildren(sectionTitle("check", "Interview readiness"), notice("bad", err.message)));
  return box;
}

// Settings → Database: the connection's health, for installation admins (GET /api/database/diagnostics).
function diagnosticsPanel() {
  const out = el("div", { class: "stack" });
  const run = el("button", { class: "btn small self-start", type: "button", onclick: () => draw() }, "Run the check");
  const draw = async () => {
    const done = busy(run, "Checking…");
    try {
      const d = await api("/api/database/diagnostics");
      out.replaceChildren(el("div", { class: "stack" },
        stateRow("Type", d.backend === "postgresql" ? "PostgreSQL" : "SQLite"),
        stateRow("Database", `${d.database}${d.profile ? ` (“${d.profile}”)` : ""}`),
        stateRow("Connection", d.connected ? "Answering" : "Not answering", [d.connected ? "ready" : "failed", d.connected ? "OK" : "Problem"]),
        d.server_version ? stateRow("Server version", d.server_version) : null,
        d.schema_version != null ? stateRow("Schema", `version ${d.schema_version} of ${d.expected_schema}`, [d.schema_ok ? "ready" : "failed", d.schema_ok ? "Up to date" : "Needs attention"]) : null,
        d.missing_tables && d.missing_tables.length ? notice("bad", `Missing tables: ${d.missing_tables.join(", ")}`) : null,
        stateRow("Last successful check", d.last_ok_at ? fmtTime(d.last_ok_at) : "Never"),
        d.error ? notice("bad", d.error) : null));
    } catch (err) { out.replaceChildren(notice("bad", err.message)); }
    done();
  };
  draw();
  return el("section", { class: "panel stack" }, sectionTitle("database", "Diagnostics"),
    el("p", { class: "muted small flush" }, "Only for this installation's admins. Connection details and passwords are never shown."), out, run);
}

// Settings → Database: switching to another database, next to copying this one's data into a new one.
function connectionsPanel() {
  return el("section", { class: "panel stack" }, sectionTitle("database", "Connection"),
    el("p", { class: "muted small flush" }, "Everyone is signed out. Nothing is copied or deleted."),
    el("div", { class: "row" }, el("button", { class: "btn", type: "button", onclick: switchDatabase }, icon("swap"), "Switch database…")));
}

// Printing shows every collapsed section (the transcript, details), then puts them back as they were.
let openedForPrint = [];
window.addEventListener("beforeprint", () => {
  openedForPrint = [...view.querySelectorAll("details:not([open])")];
  openedForPrint.forEach((d) => { d.open = true; });
});
window.addEventListener("afterprint", () => { openedForPrint.forEach((d) => { d.open = false; }); openedForPrint = []; });

init().catch((e) => view.replaceChildren(emptyState("alert", el("p", {}, "The app couldn't load"), notice("bad", `Failed to load: ${e.message}`))));
