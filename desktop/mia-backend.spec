# PyInstaller spec: the backend as one folder (desktop/build/backend/mia-backend), bundled into the installer.
# Build it on each operating system for that system: a frozen backend runs only where it was built.
# Build: npm run build:backend  (from the repository root; see docs/ELECTRON.md)
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = SPECPATH + "/.."
datas = [(ROOT + "/app/web", "app/web")]
binaries = []
hidden = []
hidden += collect_submodules("uvicorn")  # protocols, loops and lifespan are imported by name
hidden += collect_submodules("app")
hidden += ["sqlalchemy.dialects.sqlite", "sqlalchemy.dialects.postgresql", "sqlalchemy.dialects.postgresql.psycopg"]
for package in ("psycopg", "psycopg_binary"):  # the PostgreSQL driver and its bundled libpq
    d, b, h = collect_all(package)
    datas += d; binaries += b; hidden += h
datas += collect_data_files("docx")  # python-docx's default template
datas += collect_data_files("tzdata")  # time zones (Windows has no IANA database)
datas += collect_data_files("certifi")

a = Analysis([ROOT + "/app/desktop.py"], pathex=[ROOT], binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=["pytest", "_pytest", "tkinter", "PyInstaller"], noarchive=False)
pyz = PYZ(a.pure)
import sys
# The .ico is for Windows; on macOS and Linux the app's icon comes from the Electron package, not the backend.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="mia-backend", console=True,
          icon=ROOT + "/app/web/favicon.ico" if sys.platform == "win32" else None)
coll = COLLECT(exe, a.binaries, a.datas, name="mia-backend")
