"""Copy every table (roles, interviews, applications, sessions, reports, settings, audit log…) to another database.

For moving an install from SQLite to PostgreSQL. Stop the app first, so nothing changes mid-copy.

    python scripts/copy_db.py --from data/interviewer.db --to postgresql://mia:pass@localhost:5432/mia

Both ends are brought to the current schema first. The target must be empty; the source is not changed.
Then set DATABASE_URL to the target and start the app.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.dbconfig import DatabaseConfigError, copy_all  # noqa: E402
from app.store import describe  # noqa: E402


def copy(source: str, target: str) -> dict[str, int]:
    try:
        return copy_all(source, target)
    except DatabaseConfigError as e:
        raise SystemExit(str(e)) from None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="source", required=True, help="SQLite file path or database URL to copy from")
    ap.add_argument("--to", dest="target", required=True, help="database URL to copy to (must be empty)")
    args = ap.parse_args()
    counts = copy(args.source, args.target)
    print(f"Copied from {describe(args.source)} to {describe(args.target)}:")
    for name, n in counts.items():
        print(f"  {name}: {n}")


if __name__ == "__main__":
    main()
