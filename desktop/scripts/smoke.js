// Launch check: starts the app (the installed one with --installed, the unpacked build with --unpacked, otherwise
// development), waits for the backend and the dashboard, reads the page, quits, and checks nothing is left running.
// The data folder is the normal one (on Windows %APPDATA%\Recruence, or an earlier install's %APPDATA%\MIA Interviewer)
// unless MIA_SMOKE_USERDATA is set. --installed is Windows only; --unpacked works on Windows, macOS and Linux.
"use strict";
const { spawn, execSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");
const mode = process.argv.includes("--installed") ? "installed" : process.argv.includes("--unpacked") ? "unpacked" : "dev";
const win = process.platform === "win32";
// Installed (Windows): the default folder, or the folder an install from before the rename used (an update keeps it).
const installed = () => ["Recruence", "MIA Interviewer"].map((dir) => path.join(process.env.LOCALAPPDATA || "", "Programs", dir, "Recruence.exe"))
  .find((p) => fs.existsSync(p)) || path.join(process.env.LOCALAPPDATA || "", "Programs", "Recruence", "Recruence.exe");
// The unpacked build electron-builder leaves in desktop/dist, for this operating system.
function unpacked() {
  const dist = path.join(root, "desktop", "dist");
  if (win) return path.join(dist, "win-unpacked", "Recruence.exe");
  if (process.platform === "darwin") {
    const dir = fs.readdirSync(dist).find((d) => d.startsWith("mac") && fs.existsSync(path.join(dist, d, "Recruence.app")));
    return path.join(dist, dir || "mac", "Recruence.app", "Contents", "MacOS", "Recruence");
  }
  return path.join(dist, "linux-unpacked", "recruence");
}
if (mode === "installed" && !win) { console.error("--installed is for Windows; use --unpacked on macOS and Linux"); process.exit(1); }
const exe = mode === "installed" ? installed() : mode === "unpacked" ? unpacked() : require(path.join(root, "node_modules", "electron"));
const args = mode === "dev" ? [root] : [];
const result = path.join(os.tmpdir(), `mia-smoke-${Date.now()}.json`);

if (!fs.existsSync(exe)) { console.error(`Not found: ${exe}`); process.exit(1); }
const started = Date.now();
const child = spawn(exe, args, { env: { ...process.env, MIA_SMOKE_TEST: result }, stdio: "ignore" });
const timer = setTimeout(() => { console.error("Timed out after 150 s"); child.kill(); process.exit(1); }, 150_000);
child.on("exit", () => {
  clearTimeout(timer);
  const seconds = ((Date.now() - started) / 1000).toFixed(1);
  let out;
  try { out = JSON.parse(fs.readFileSync(result, "utf8")); } catch { console.error("The app wrote no result"); process.exit(1); }
  setTimeout(() => {
    // This launch's own backend (its process id), so another Recruence running on the same computer doesn't count.
    const alive = (pid) => {
      if (!pid) return false;
      if (win) {
        try { return execSync(`tasklist /FI "PID eq ${Number(pid)}" /NH`, { encoding: "utf8" }).includes(String(pid)); } catch { return false; }
      }
      try { process.kill(pid, 0); return true; } catch { return false; }
    };
    const orphan = alive(out.backendPid);
    console.log(JSON.stringify({ mode, seconds: Number(seconds), orphanedBackend: orphan, ...out }, null, 2));
    process.exit(out.ok && !orphan && !out.page.nodeInPage ? 0 : 1);
  }, 3000);
});
