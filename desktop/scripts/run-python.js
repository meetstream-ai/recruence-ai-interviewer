// Run a Python script with the repository's virtual environment (or MIA_PYTHON), passing the arguments through.
"use strict";
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");
const candidates = [process.env.MIA_PYTHON, path.join(root, ".venv", "Scripts", "python.exe"), path.join(root, ".venv", "bin", "python")];
const python = candidates.find((p) => p && fs.existsSync(p));
if (!python) {
  console.error("No Python found: create .venv (python -m venv .venv; .venv\Scripts\pip install -r requirements.txt -r requirements-postgres.txt) or set MIA_PYTHON.");
  process.exit(1);
}
const result = spawnSync(python, process.argv.slice(2), { cwd: root, stdio: "inherit" });
process.exit(result.status ?? 1);
