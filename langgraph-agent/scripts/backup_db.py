"""Back up this service's SQLite files consistently (safe while the server runs).

    python scripts/backup_db.py [--dest DIR]

Writes <DIR>/<UTC timestamp>/cocoon.db and checkpoints.db using the SQLite backup API. Default DIR is
<COCOON_DATA_DIR>/backups (git-ignored with the rest of data/). Restore procedure: docs/MIGRATIONS.md.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cocoon_agent.backup import backup_sqlite  # noqa: E402
from cocoon_agent.config import get_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, help="backup root directory (default: <data dir>/backups)")
    args = ap.parse_args()
    settings = get_settings()
    root = args.dest or settings.data_dir / "backups"
    target = root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    copied = 0
    for path in (settings.db_path, settings.checkpoint_path):
        if path.exists():
            backup_sqlite(path, target / path.name)
            copied += 1
            print(f"backed up {path.name} -> {target / path.name}")
    if not copied:
        print(f"nothing to back up in {settings.data_dir}")
        return 1
    db = target / settings.db_path.name
    if db.exists():
        conn = sqlite3.connect(db)
        try:
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        except sqlite3.OperationalError:
            version = "unversioned (pre-I02a)"
        finally:
            conn.close()
        print(f"cocoon.db schema version in backup: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
