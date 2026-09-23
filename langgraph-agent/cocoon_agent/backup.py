"""Consistent SQLite backups through the SQLite Online Backup API (sqlite3.Connection.backup).

Copying cocoon.db alone while the server runs is NOT a backup: in WAL mode recent commits may live only in
cocoon.db-wal. The backup API reads a transactionally consistent snapshot, including WAL content, and writes a
self-contained database file (no -wal/-shm needed to restore it).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def backup_sqlite(source: Path, destination: Path) -> None:
    """Write a consistent copy of `source` to `destination` (which must not exist yet)."""
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        raise FileNotFoundError(source)
    # A normal connection (not mode=ro): a read-only open of a WAL database can fail without the -shm file.
    # backup() only reads from the source.
    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
