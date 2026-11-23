// The only things the desktop app adds to its pages: a few fixed actions, each checked again in main.js
// (the caller must be the backend's own page or one of the app's local pages). No file system, no Node.
"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("miaDesktop", {
  openLogs: () => ipcRenderer.invoke("mia:open-logs"),
  openDataFolder: () => ipcRenderer.invoke("mia:open-data"),
  retry: () => ipcRenderer.invoke("mia:retry"),
  quit: () => ipcRenderer.invoke("mia:quit"),
  info: () => ipcRenderer.invoke("mia:info"),
  setServer: (url) => ipcRenderer.invoke("mia:set-server", String(url || "")),
  chooseLocal: () => ipcRenderer.invoke("mia:choose-local"),
});
