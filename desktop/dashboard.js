// Loading the dashboard into the desktop window (desktop/main.js), apart so it's testable without Electron
// (desktop/dashboard.test.js).
//
// Closing the window while the dashboard is still loading makes loadURL reject (the load is aborted). That's the person
// quitting, not a failure. Before this, the rejection reached start()'s catch, which showed "Recruence couldn't start"
// in a blocking error box: the quit already under way stalled behind it, with the backend still running (R-11, found
// by the B-8 installer test on a fresh Windows machine).
"use strict";

// true when the dashboard is showing; false when there's no window to show it in any more (closed, or quitting).
// Any other failure is thrown, as before.
async function loadDashboard(win, url, isQuitting) {
  const gone = () => !win || win.isDestroyed() || isQuitting();
  if (gone()) return false;
  try {
    await win.loadURL(url);
    return true;
  } catch (err) {
    if (gone()) return false;
    throw err;
  }
}

module.exports = { loadDashboard };
