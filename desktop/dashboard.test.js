// node --test desktop/  (CI: Dashboard scripts). R-11: closing the window while the dashboard loads is quitting.
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const { loadDashboard } = require("./dashboard");

function fakeWindow({ fails = null, closeDuringLoad = false } = {}) {
  const w = { destroyed: false, loads: [] };
  w.isDestroyed = () => w.destroyed;
  w.loadURL = async (url) => {
    w.loads.push(url);
    if (closeDuringLoad) w.destroyed = true;
    if (fails) throw fails;
  };
  return w;
}
const aborted = Object.assign(new Error("ERR_ABORTED (-3) loading 'http://127.0.0.1:8765/'"), { code: "ERR_ABORTED" });

test("shows the dashboard", async () => {
  const w = fakeWindow();
  assert.strictEqual(await loadDashboard(w, "http://127.0.0.1:8765/", () => false), true);
  assert.deepStrictEqual(w.loads, ["http://127.0.0.1:8765/"]);
});

test("the window closed while the dashboard loaded: no error, nothing shown", async () => {
  const w = fakeWindow({ fails: aborted, closeDuringLoad: true });
  assert.strictEqual(await loadDashboard(w, "http://127.0.0.1:8765/", () => false), false);
});

test("the app started quitting while the dashboard loaded: no error", async () => {
  let quitting = false;
  const w = fakeWindow({ fails: aborted });
  w.loadURL = async () => { quitting = true; throw aborted; };
  assert.strictEqual(await loadDashboard(w, "http://127.0.0.1:8765/", () => quitting), false);
});

test("no window any more, or already quitting: doesn't try to load", async () => {
  assert.strictEqual(await loadDashboard(null, "http://x/", () => false), false);
  const closed = fakeWindow(); closed.destroyed = true;
  assert.strictEqual(await loadDashboard(closed, "http://x/", () => false), false);
  const open = fakeWindow();
  assert.strictEqual(await loadDashboard(open, "http://x/", () => true), false);
  assert.deepStrictEqual(open.loads, []);
});

test("a real failure with the window still open is still an error", async () => {
  const w = fakeWindow({ fails: new Error("ERR_CONNECTION_REFUSED") });
  await assert.rejects(loadDashboard(w, "http://127.0.0.1:8765/", () => false), /ERR_CONNECTION_REFUSED/);
});
