"""The backend as the desktop app (desktop/main.js) runs it: `python -m app.desktop`, or the frozen mia-backend.exe.

Electron chooses the data folder (the user's application data, never the install folder) and the port, starts
this as a child process and loads the dashboard once /api/health answers. Differences from the server:

- Listens on 127.0.0.1 only. MeetStream and candidates reach it through a tunnel to this port, if you set one up
  (docs/INTERVIEW_SETUP.md); without one, recruiting works locally but interviews can't run in meetings.
- Settings come from the dashboard (saved in the database) and, optionally, a `.env` file in the data folder.
  The project's own .env is not read: an installed app has none.
- Exits by itself when the Electron process that started it is gone, so a crash never leaves it running.

Environment (set by Electron): DATA_DIR, MIA_DESKTOP_PORT, MIA_PARENT_PID.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("app.desktop")


def _parent_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE, STILL_ACTIVE = 0x00100000, 259
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE | 0x1000, False, pid)  # | PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


STOP_FILE = ".stop"  # written by the desktop app to ask for a clean shutdown (bots leave their meetings first)


def _watch(server, pid: int, stop_file: Path) -> None:
    """Stop cleanly when asked (the stop file) or when the desktop app is gone; force it if that takes too long."""
    while True:
        time.sleep(1)
        asked = stop_file.exists()
        orphaned = bool(pid) and not _parent_alive(pid)
        if asked or orphaned:
            logger.warning("stopping the backend: %s", "the desktop app asked" if asked else f"the desktop app (pid {pid}) is gone")
            server.should_exit = True
            time.sleep(20)
            os._exit(0)


def main() -> None:
    data_dir = Path(os.environ.get("DATA_DIR") or Path.home() / ".mia-interviewer")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        print(f"Recruence can't write to its data folder {data_dir}: {e}", file=sys.stderr)
        sys.exit(3)
    os.environ["DATA_DIR"] = str(data_dir)

    from dotenv import load_dotenv

    load_dotenv(data_dir / ".env")  # optional; values saved in Settings take precedence over it

    port = int(os.environ.get("MIA_DESKTOP_PORT") or 8765)
    parent = int(os.environ.get("MIA_PARENT_PID") or 0)
    stop_file = data_dir / STOP_FILE
    stop_file.unlink(missing_ok=True)

    import uvicorn

    from app.server import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, access_log=False,
                                           log_level=os.environ.get("LOG_LEVEL", "info").lower()))
    threading.Thread(target=_watch, args=(server, parent, stop_file), daemon=True, name="desktop-watch").start()
    server.run()
    stop_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
