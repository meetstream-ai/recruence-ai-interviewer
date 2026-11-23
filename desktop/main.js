// Recruence desktop (Windows, macOS, Linux): shows the dashboard in a locked-down window, from one of two backends:
//
// - Cloud (a server address is set: RECRUENCE_SERVER_URL, or "server" in desktop.json, or chosen on the error page or
//   Settings → System): the dashboard of the hosted backend (docs/CLOUD.md). Nothing runs on this computer, so candidates,
//   interviews and webhooks keep working when it's off. No local backend is ever started in this mode.
// - Local (no server address): the existing backend (app/desktop.py, frozen as mia-backend in the installed app) as a
//   child process on 127.0.0.1, as before. Interviews then need this computer on and a tunnel to it.
//
// No second UI or onboarding: everything the window shows comes from the backend, exactly as in a browser.
//
// - One instance at a time; a second launch focuses the first window.
// - Data lives in the user's application data folder, never the install folder, so it survives updates and uninstalls:
//   Windows %APPDATA%\Recruence\data (earlier installs: %APPDATA%\MIA Interviewer\data), macOS
//   ~/Library/Application Support/Recruence/data, Linux ~/.config/Recruence/data.
// - The backend is asked to stop cleanly on quit (a stop file), then forced if it doesn't; it also stops by
//   itself if this process disappears.
// - The window: context isolation, sandbox, no Node in pages, navigation limited to the backend, external links
//   in the system browser, only microphone/camera and clipboard permissions for the backend's own pages.

"use strict";

const { app, BrowserWindow, Menu, dialog, ipcMain, session, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const net = require("node:net");
const path = require("node:path");
const { loadDashboard } = require("./dashboard");

const PREFERRED_PORT = 8765; // fixed when free, so a tunnel for MeetStream can point at it (docs/INTERVIEW_SETUP.md)
const READY_TIMEOUT_MS = 90_000;
const STOP_TIMEOUT_MS = 25_000;

const repoRoot = path.resolve(__dirname, "..");
// MIA_USERDATA: another data folder (tests, or a second profile); otherwise %APPDATA%\Recruence.
// Installs from before the rename keep their data where it is: %APPDATA%\MIA Interviewer stays in use (never moved
// or copied) as long as it holds data and the new folder doesn't.
const FORMER_DATA_FOLDER = "MIA Interviewer";
if (process.env.MIA_USERDATA) {
  app.setPath("userData", path.resolve(process.env.MIA_USERDATA));
} else {
  const former = path.join(app.getPath("appData"), FORMER_DATA_FOLDER);
  if (fs.existsSync(path.join(former, "data")) && !fs.existsSync(path.join(app.getPath("userData"), "data"))) {
    app.setPath("userData", former);
  }
}
const userData = app.getPath("userData");
const dataDir = path.join(userData, "data");
const logsDir = path.join(userData, "logs");
const configPath = path.join(userData, "desktop.json");
const smoke = process.env.MIA_SMOKE_TEST ? path.resolve(process.env.MIA_SMOKE_TEST) : null; // automated launch check

let backend = null;
let backendLog = null;
let recentLines = [];
let origin = null;
let win = null;
let quitting = false;
let ready = false;

// ---------- single instance ----------

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
  });
  app.whenReady().then(start).catch((err) => fatal(`Recruence couldn't start: ${err.message}`));
}

// ---------- configuration and logs ----------

function readConfig() {
  try { return JSON.parse(fs.readFileSync(configPath, "utf8")); } catch { return {}; }
}

function writeConfig(patch) {
  const next = { ...readConfig(), ...patch };
  for (const [k, v] of Object.entries(next)) if (v === null || v === "") delete next[k];
  fs.writeFileSync(configPath, JSON.stringify(next, null, 2));
}

// The cloud backend's origin, or "" for the local backend. https only, except a backend on this computer (development).
function serverOrigin(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  let url;
  try { url = new URL(raw); } catch { throw new Error(`"${raw}" isn't a web address. Use the server's https:// address.`); }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) {
    throw new Error(`${url.origin} isn't https. Recruence connects to a server only over https:// (http:// only on this computer, for development).`);
  }
  return url.origin;
}

function configuredServer() {
  return serverOrigin(process.env.RECRUENCE_SERVER_URL || readConfig().server || "");
}

// A new install asks where the backend runs (desktop/welcome.html): the user's own cloud, or this computer. An install
// that already has data (an update, or a choice made before) carries on as it was; so does the automated launch check.
function needsChoice() {
  if (process.env.RECRUENCE_SERVER_URL || (smoke && !process.env.MIA_SMOKE_CHOICE)) return false;
  const config = readConfig();
  if (config.server || config.mode) return false;
  const existing = ["interviewer.db", "database.json", "settings.key", "setup.json"].some((f) => fs.existsSync(path.join(dataDir, f)));
  return !existing;
}

function ensureDirs() {
  for (const dir of [dataDir, logsDir]) {
    try { fs.mkdirSync(dir, { recursive: true }); fs.accessSync(dir, fs.constants.W_OK); } catch (err) {
      throw new Error(`The folder ${dir} can't be written (${err.code || err.message}). Check its permissions or free disk space.`);
    }
  }
}

function openLog() {
  const file = path.join(logsDir, "backend.log");
  try { if (fs.statSync(file).size > 5 * 1024 * 1024) fs.renameSync(file, path.join(logsDir, "backend.previous.log")); } catch { /* first run */ }
  backendLog = fs.createWriteStream(file, { flags: "a" });
  backendLog.write(`\n---- ${new Date().toISOString()} Recruence ${app.getVersion()} ----\n`);
}

function record(chunk) {
  const text = chunk.toString();
  if (backendLog) backendLog.write(text);
  recentLines = [...recentLines, ...text.split(/\r?\n/).filter(Boolean)].slice(-30);
}

// ---------- the backend ----------

function portFree(port) {
  return new Promise((resolve) => {
    const probe = net.createServer().once("error", () => resolve(false)).once("listening", () => probe.close(() => resolve(true)));
    probe.listen(port, "127.0.0.1");
  });
}

function anyFreePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer().once("error", reject).once("listening", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
    probe.listen(0, "127.0.0.1");
  });
}

async function choosePort() {
  const wanted = Number(readConfig().port) || PREFERRED_PORT;
  if (await portFree(wanted)) return wanted;
  const other = await anyFreePort();
  record(`[desktop] port ${wanted} is in use by another program; using ${other} for this session\n`);
  return other;
}

function backendCommand() {
  if (app.isPackaged) {
    const exe = path.join(process.resourcesPath, "backend", "mia-backend", process.platform === "win32" ? "mia-backend.exe" : "mia-backend");
    return { command: exe, args: [], cwd: path.dirname(exe) };
  }
  // Development: the repository's virtual environment (or MIA_PYTHON), running app/desktop.py.
  const python = process.env.MIA_PYTHON || (process.platform === "win32"
    ? path.join(repoRoot, ".venv", "Scripts", "python.exe") : path.join(repoRoot, ".venv", "bin", "python"));
  return { command: python, args: ["-m", "app.desktop"], cwd: repoRoot };
}

function startBackend(port) {
  const { command, args, cwd } = backendCommand();
  if (!fs.existsSync(command)) throw new Error(`The Recruence backend wasn't found at ${command}. Reinstall the app, or in development run npm run build:backend.`);
  try { fs.unlinkSync(path.join(dataDir, ".stop")); } catch { /* none */ }
  backend = spawn(command, args, {
    cwd,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, DATA_DIR: dataDir, MIA_DESKTOP_PORT: String(port), MIA_PARENT_PID: String(process.pid), PYTHONUNBUFFERED: "1" },
  });
  backend.stdout.on("data", record);
  backend.stderr.on("data", record);
  backend.once("exit", (code) => {
    record(`[desktop] backend exited with code ${code}\n`);
    backend = null;
    if (!quitting) showError(ready ? "The Recruence backend stopped unexpectedly." : "The Recruence backend couldn't start.");
  });
}

function health(url) {
  return new Promise((resolve) => {
    const client = url.startsWith("https:") ? https : http;
    const req = client.get(url, { timeout: 5000 }, (res) => {
      let body = "";
      res.on("data", (d) => { body += d; });
      res.on("end", () => { try { resolve(res.statusCode === 200 && JSON.parse(body).ok === true); } catch { resolve(false); } });
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

// The cloud backend: answering its health check within the timeout (a sleeping host can take a while to wake).
async function waitServer(timeoutMs = 45_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await health(`${origin}/api/health`)) return true;
    await new Promise((r) => setTimeout(r, 1500));
  }
  return false;
}

async function waitReady() {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!backend || quitting) return false;
    if (await health(`${origin}/api/health`)) return true;
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

function stopBackend() {
  return new Promise((resolve) => {
    if (!backend) { resolve(); return; }
    const child = backend;
    const timer = setTimeout(() => { try { child.kill(); } catch { /* gone */ } resolve(); }, STOP_TIMEOUT_MS);
    child.once("exit", () => { clearTimeout(timer); resolve(); });
    try { fs.writeFileSync(path.join(dataDir, ".stop"), "stop"); } catch { child.kill(); }
  });
}

// ---------- the window ----------

function sameOrigin(url) {
  try { return origin !== null && new URL(url).origin === origin; } catch { return false; }
}

function localPage(url) {
  return url.startsWith("file:") && path.normalize(decodeURIComponent(new URL(url).pathname.replace(/^\/([A-Za-z]:)/, "$1"))).startsWith(path.normalize(__dirname));
}

function openExternally(url) {
  if (/^https?:\/\//i.test(url) || /^mailto:/i.test(url)) shell.openExternal(url);
}

const TITLE_BAR_HEIGHT = 44;  // the pages' title bar strip (app/web/styles.css .titlebar, loading.html, error.html)

function createWindow() {
  win = new BrowserWindow({
    width: 1366, height: 880, minWidth: 960, minHeight: 640,
    title: "Recruence",
    icon: path.join(__dirname, "icon.png"),
    backgroundColor: "#EAF4FF",
    show: false,
    autoHideMenuBar: true,
    titleBarStyle: "hidden",
    titleBarOverlay: { color: "#F6FAFF", symbolColor: "#17324F", height: TITLE_BAR_HEIGHT },  // the strip's colour, so the buttons sit in it
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: true,
      webSecurity: true, allowRunningInsecureContent: false, devTools: !app.isPackaged },
  });
  win.once("ready-to-show", () => { if (!smoke) win.show(); });
  win.webContents.on("will-navigate", (event, url) => {
    if (!sameOrigin(url) && !localPage(url)) { event.preventDefault(); openExternally(url); }
  });
  win.webContents.on("will-attach-webview", (event) => event.preventDefault());
  win.webContents.setWindowOpenHandler(({ url }) => { openExternally(url); return { action: "deny" }; });
  win.on("closed", () => { win = null; });
  win.loadFile(path.join(__dirname, "loading.html"));
}

function lockDownSession() {
  const allowed = new Set(["media", "clipboard-sanitized-write", "fullscreen"]);
  session.defaultSession.setPermissionRequestHandler((wc, permission, callback, details) => {
    callback(allowed.has(permission) && sameOrigin(details.requestingUrl || wc.getURL()));
  });
  session.defaultSession.setPermissionCheckHandler((wc, permission, requestingOrigin) => allowed.has(permission) && requestingOrigin === origin);
}

function showError(message) {
  if (smoke) { finishSmoke({ ok: false, stage: "backend", message, log: recentLines.slice(-10) }); return; }
  if (quitting) { record(`[desktop] ${message}\n`); return; }  // never a new window while quitting (R-11)
  if (!win) createWindow();
  const query = new URLSearchParams({ message, log: recentLines.slice(-12).join("\n"), logs: logsDir });
  win.loadFile(path.join(__dirname, "error.html"), { search: query.toString() });
  win.show();
}

function fatal(message) {
  record(`[desktop] ${message}\n`);  // in backend.log too: a box on screen is lost once it's closed
  if (quitting) return;  // already on the way out: a blocking box here would stop the quit (R-11)
  dialog.showErrorBox("Recruence", message);
  app.quit();
}

// A promise nobody waits on that fails is logged, never left to stop the app with Electron's own error box (R-11).
process.on("unhandledRejection", (err) => record(`[desktop] unexpected error: ${err && err.stack ? err.stack : err}\n`));

// ---------- start and stop ----------

async function start() {
  Menu.setApplicationMenu(null);
  ensureDirs();
  openLog();
  registerIpc();
  createWindow();
  await launch();
}

// Start the backend (or reach the cloud one), wait for it, then show the dashboard (also after "Try again").
async function launch() {
  let server = "";
  try { server = configuredServer(); } catch (err) { record(`[desktop] ${err.message}\n`); showError(err.message); return; }
  if (!server && needsChoice()) {
    origin = null;
    win.loadFile(path.join(__dirname, "welcome.html"));
    if (smoke) chooseInSmoke();
    return;
  }
  if (server) {
    origin = server;
    lockDownSession();
    ready = false;
    record(`[desktop] connecting to the Recruence server at ${origin}\n`);
    if (!(await waitServer())) {
      showError(`Recruence couldn't reach its server at ${origin}. Check your internet connection, or that the server is running.`);
      return;
    }
    ready = true;
    record(`[desktop] connected to ${origin}\n`);
    if (await loadDashboard(win, `${origin}/`, () => quitting) && smoke) runSmoke();
    return;
  }
  const port = await choosePort();
  // The window can be closed at any moment while this runs: the app is then quitting, and must not start a backend that
  // nothing will stop, or carry on with one (R-11: the app stayed running with no window, or behind an error box).
  if (quitting) { app.quit(); return; }
  origin = `http://127.0.0.1:${port}`;
  lockDownSession();
  ready = false;
  try { startBackend(port); } catch (err) { record(`[desktop] ${err.message}\n`); showError(err.message); return; }
  if (!(await waitReady())) {
    if (quitting) { stopBackend().then(() => app.quit()); return; }
    if (backend) showError("The Recruence backend didn't answer within 90 seconds.");
    return;
  }
  if (quitting) { stopBackend().then(() => app.quit()); return; }
  ready = true;
  record(`[desktop] backend ready on ${origin}\n`);
  if (await loadDashboard(win, `${origin}/`, () => quitting) && smoke) runSmoke();
}

async function restart() {
  await stopBackend();
  recentLines = [];
  if (win) win.loadFile(path.join(__dirname, "loading.html"));
  await launch();
}

app.on("before-quit", (event) => {
  if (quitting) return;
  quitting = true;
  if (backend) {
    event.preventDefault();
    stopBackend().then(() => { if (backendLog) backendLog.end(); app.quit(); });
  }
});

app.on("window-all-closed", () => app.quit());

// ---------- the bridge (desktop/preload.js) ----------

function registerIpc() {
  const trusted = (event) => {
    const url = event.senderFrame ? event.senderFrame.url : "";
    return sameOrigin(url) || localPage(url);
  };
  ipcMain.handle("mia:open-logs", (event) => { if (trusted(event)) shell.openPath(logsDir); });
  ipcMain.handle("mia:open-data", (event) => { if (trusted(event)) shell.openPath(dataDir); });
  ipcMain.handle("mia:retry", (event) => { if (trusted(event)) restart(); });
  ipcMain.handle("mia:quit", (event) => { if (trusted(event)) app.quit(); });
  ipcMain.handle("mia:info", (event) => {
    if (!trusted(event)) return null;
    let server = "";
    try { server = configuredServer(); } catch { /* shown on launch */ }
    return { version: app.getVersion(), dataDir, logsDir, packaged: app.isPackaged, mode: server ? "cloud" : "local", server,
             serverFromEnvironment: Boolean(process.env.RECRUENCE_SERVER_URL) };
  });
  // The first-launch choice: run the backend on this computer (desktop/welcome.html only).
  ipcMain.handle("mia:choose-local", (event) => {
    const url = event.senderFrame ? event.senderFrame.url : "";
    if (!localPage(url)) return { ok: false, message: "Not allowed" };
    try { writeConfig({ mode: "local", server: null }); } catch (err) { return { ok: false, message: `Couldn't save the setting: ${err.message}` }; }
    restart();
    return { ok: true };
  });
  // Where the app connects: a cloud server (https) or this computer. Saved in desktop.json, then the app reconnects.
  ipcMain.handle("mia:set-server", async (event, value) => {
    if (!trusted(event)) return { ok: false, message: "Not allowed" };
    if (process.env.RECRUENCE_SERVER_URL) return { ok: false, message: "RECRUENCE_SERVER_URL is set for this app, so the server can't be changed here." };
    let server;
    try { server = serverOrigin(value); } catch (err) { return { ok: false, message: err.message }; }
    if (server && !(await health(`${server}/api/health`))) return { ok: false, message: `No Recruence server answered at ${server}.` };
    try { writeConfig({ server: server || null, mode: server ? "cloud" : "local" }); } catch (err) { return { ok: false, message: `Couldn't save the setting: ${err.message}` }; }
    restart();
    return { ok: true, server };
  });
}

// ---------- automated launch check (MIA_SMOKE_TEST=<result file>) ----------

// MIA_SMOKE_CHOICE=local or a server address: on a new install, read the choice page and choose as a user would.
let welcomeSeen = null;
function chooseInSmoke() {
  win.webContents.once("did-finish-load", async () => {
    try {
      const choice = process.env.MIA_SMOKE_CHOICE;
      welcomeSeen = await win.webContents.executeJavaScript(`({ title: document.querySelector("h1").textContent,
        options: [...document.querySelectorAll(".choice h2")].map((h) => h.textContent.trim()) })`);
      await win.webContents.executeJavaScript(choice === "local"
        ? `document.getElementById("local").click()`
        : `document.getElementById("server").value = ${JSON.stringify(choice)}; document.getElementById("connect").click()`);
    } catch (err) { finishSmoke({ ok: false, stage: "welcome", message: err.message }); }
  });
}

async function runSmoke() {
  try {
    const page = await win.webContents.executeJavaScript(`new Promise((resolve) => setTimeout(() => resolve({
      title: document.title, url: location.href,
      nodeInPage: typeof require !== "undefined" || typeof process !== "undefined",
      bridge: typeof window.miaDesktop === "object" ? Object.keys(window.miaDesktop).sort() : null,
      text: (document.querySelector("#setup-root:not([hidden])") || document.body).innerText.slice(0, 300),
    }), 2500))`);
    finishSmoke({ ok: true, origin, mode: backend ? "local" : "cloud", backendPid: backend ? backend.pid : null, page,
                  welcome: welcomeSeen, saved: readConfig() });
  } catch (err) {
    finishSmoke({ ok: false, stage: "page", message: err.message });
  }
}

function finishSmoke(result) {
  try { fs.writeFileSync(smoke, JSON.stringify({ ...result, version: app.getVersion(), packaged: app.isPackaged, dataDir }, null, 2)); } catch { /* best effort */ }
  app.quit();
}
