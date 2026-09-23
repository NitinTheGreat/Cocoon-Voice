"""Create the insights PostgreSQL database (if missing) and its tables. Safe to run repeatedly.

    python scripts/insights_db_setup.py

Uses POSTGRES_* (or INSIGHTS_DATABASE_URL) from langgraph-agent/.env. If POSTGRES_PASSWORD is empty, the
password is asked with a hidden prompt and is not stored. Nothing secret is printed.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cocoon_agent.insights.db import SCHEMA  # noqa: E402
from cocoon_agent.insights.settings import InsightsSettings  # noqa: E402


def main() -> int:
    settings = InsightsSettings()
    if settings.database_url is None and settings.password is None:
        from pydantic import SecretStr

        settings.password = SecretStr(getpass.getpass(f"PostgreSQL password for {settings.user}@{settings.host}: "))
    target = settings.describe()
    try:
        if settings.database_url is None:
            # CREATE DATABASE cannot run inside a transaction; connect to the maintenance database first.
            with psycopg.connect(settings.conninfo(database="postgres"), autocommit=True) as conn:
                exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (settings.database,)).fetchone()
                if exists:
                    print(f"database {settings.database} exists")
                else:
                    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.database)))
                    print(f"created database {settings.database}")
        with psycopg.connect(settings.conninfo(), autocommit=True) as conn:
            conn.execute(SCHEMA)
            tables = [r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'insights_%' ORDER BY 1")]
        print(f"schema ready on {target}: {', '.join(tables)}")
        return 0
    except psycopg.OperationalError as exc:
        print(f"cannot connect to PostgreSQL {target}: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(main())
