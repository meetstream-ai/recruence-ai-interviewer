"use strict";
// The first-launch choice (desktop/main.js): the user's own cloud server, or this computer. Text is set as text only.
const server = document.getElementById("server");
const result = document.getElementById("result");
const connect = document.getElementById("connect");
const local = document.getElementById("local");

document.getElementById("cloud").addEventListener("submit", async (e) => {
  e.preventDefault();
  const value = server.value.trim();
  if (!value) { result.className = "result bad"; result.textContent = "Enter your Recruence server's https:// address."; return; }
  connect.disabled = true;
  result.className = "result";
  result.textContent = "Checking the server…";
  const r = await window.miaDesktop.setServer(value);
  if (!r || !r.ok) {
    result.className = "result bad";
    result.textContent = (r && r.message) || "Couldn't connect.";
    connect.disabled = false;
  }
});

local.addEventListener("click", async () => {
  local.disabled = true;
  await window.miaDesktop.chooseLocal();
});

server.focus();
