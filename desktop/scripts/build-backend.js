// Freeze the backend (app/desktop.py) into desktop/build/backend/mia-backend with PyInstaller (requirements-desktop.txt).
"use strict";
const { spawnSync } = require("node:child_process");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");
const run = (args) => spawnSync(process.execPath, [path.join(__dirname, "run-python.js"), ...args], { cwd: root, stdio: "inherit" }).status;
if (run(["-c", "import PyInstaller, psycopg"]) !== 0) {
  console.error("Install the build tools first: .venv\Scripts\pip install -r requirements.txt -r requirements-postgres.txt -r requirements-desktop.txt");
  process.exit(1);
}
process.exit(run(["-m", "PyInstaller", "desktop/mia-backend.spec", "--distpath", "desktop/build/backend",
  "--workpath", "desktop/build/work", "--noconfirm", "--log-level", "WARN"]));
