"use strict";
// The error page's text comes from the query string main.js builds; shown as text only, never as HTML.
const q = new URLSearchParams(location.search);
document.getElementById("message").textContent = q.get("message") || "Recruence couldn't start";
document.getElementById("log").textContent = q.get("log") || "(nothing logged)";
document.getElementById("logs").textContent = q.get("logs") || "the logs folder";
document.getElementById("retry").addEventListener("click", () => window.miaDesktop.retry());
document.getElementById("open-logs").addEventListener("click", () => window.miaDesktop.openLogs());
document.getElementById("quit").addEventListener("click", () => window.miaDesktop.quit());

// Where the app connects: a cloud server, or this computer (empty).
const server = document.getElementById("server");
const result = document.getElementById("connect-result");
window.miaDesktop.info().then((info) => {
  if (!info) return;
  server.value = info.server || "";
  if (info.serverFromEnvironment) {
    server.disabled = true;
    document.getElementById("connect-go").disabled = true;
    document.getElementById("connect-note").textContent = "The server is set by RECRUENCE_SERVER_URL for this app.";
  }
});
document.getElementById("connect").addEventListener("submit", async (e) => {
  e.preventDefault();
  result.className = "note";
  result.textContent = server.value.trim() ? "Checking the server…" : "Switching to this computer…";
  const r = await window.miaDesktop.setServer(server.value.trim());
  if (!r || !r.ok) { result.className = "note bad"; result.textContent = (r && r.message) || "Couldn't connect."; }
});
