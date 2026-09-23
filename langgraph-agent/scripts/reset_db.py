"""Apply pending migrations and seed demo data (idempotent). --wipe deletes this service's SQLite files first.

    python scripts/reset_db.py [--wipe]

--wipe is destructive and irreversible; back up first with scripts/backup_db.py (see docs/MIGRATIONS.md).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cocoon_agent.config import get_settings  # noqa: E402
from cocoon_agent.store import Store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wipe", action="store_true", help="delete cocoon.db and checkpoints.db (stop the server first)")
    args = ap.parse_args()
    settings = get_settings()
    if args.wipe:
        for path in (settings.db_path, settings.checkpoint_path):
            for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                if p.exists():
                    p.unlink()
                    print(f"deleted {p}")
    store = Store(settings.db_path)
    applied = store.init_schema()
    store.seed_demo()
    print(f"schema version {store.schema_version()} ready ({', '.join(applied) or 'no migrations pending'}); "
          f"demo data seeded in {settings.db_path}")
    print(f"tasks={len(store.list_tasks())} lessons={len(store.list_lessons())}")
    store.close()


if __name__ == "__main__":
    main()
