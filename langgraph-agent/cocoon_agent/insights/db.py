"""PostgreSQL store for insights. The schema is created idempotently at startup.

psycopg's sync pool runs in worker threads (asyncio.to_thread): psycopg's async mode cannot run on the
Windows Proactor event loop that uvicorn uses, and this way no database call ever blocks the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .settings import InsightsSettings

log = logging.getLogger("cocoon_agent.insights.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS insights_users (
    employee_id      TEXT PRIMARY KEY,
    machine_id       TEXT NOT NULL,
    machine_model    TEXT,
    machine_category TEXT,
    display_name     TEXT,
    onboarded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per backend session. No foreign key to insights_users: a voice session can start before the
-- employee finishes onboarding, and its history must not be lost.
CREATE TABLE IF NOT EXISTS insights_sessions (
    session_id           TEXT PRIMARY KEY,
    employee_id          TEXT NOT NULL,
    machine_id           TEXT NOT NULL,
    room_name            TEXT,
    participant_identity TEXT,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_insights_sessions_employee ON insights_sessions (employee_id, started_at DESC);

-- One row per backend turn (what the employee asked and what they were told). Retries of the same turn_id
-- update the same row.
CREATE TABLE IF NOT EXISTS insights_interactions (
    session_id   TEXT NOT NULL,
    turn_id      TEXT NOT NULL,
    employee_id  TEXT NOT NULL,
    machine_id   TEXT NOT NULL,
    question     TEXT NOT NULL,
    source       TEXT,
    status       TEXT NOT NULL,
    reply        TEXT,
    action_types TEXT[] NOT NULL DEFAULT '{}',
    llm_mode     TEXT,
    asked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (session_id, turn_id)
);
CREATE INDEX IF NOT EXISTS ix_insights_interactions_employee ON insights_interactions (employee_id, asked_at DESC);

CREATE TABLE IF NOT EXISTS insights_reports (
    report_id    BIGSERIAL PRIMARY KEY,
    employee_id  TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    llm_mode     TEXT NOT NULL,
    report       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_insights_reports_employee ON insights_reports (employee_id, generated_at DESC);
"""


class _Conn:
    """Async facade over one pooled sync connection: every call runs in a worker thread."""

    def __init__(self, conn):
        self._conn = conn

    async def execute(self, query: str, params: Any = None) -> "_Cursor":
        cur = await asyncio.to_thread(self._conn.execute, query, params)
        return _Cursor(cur)


class _Cursor:
    def __init__(self, cur):
        self._cur = cur

    async def fetchone(self):
        return await asyncio.to_thread(self._cur.fetchone)

    async def fetchall(self):
        return await asyncio.to_thread(self._cur.fetchall)


class _Pool:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    @asynccontextmanager
    async def connection(self):
        conn = await asyncio.to_thread(self._pool.getconn, 5)
        try:
            yield _Conn(conn)
        finally:
            await asyncio.to_thread(self._pool.putconn, conn)

    async def close(self) -> None:
        await asyncio.to_thread(self._pool.close)


class InsightsStore:
    def __init__(self, pool: _Pool):
        self._pool = pool

    @classmethod
    async def open(cls, settings: InsightsSettings) -> "InsightsStore":
        def _open() -> ConnectionPool:
            pool = ConnectionPool(settings.conninfo(), min_size=1, max_size=settings.pool_max_size, open=False,
                                  kwargs={"row_factory": dict_row, "autocommit": True})
            pool.open(wait=True, timeout=10)
            return pool

        store = cls(_Pool(await asyncio.to_thread(_open)))
        await store.init_schema()
        return store

    async def close(self) -> None:
        await self._pool.close()

    async def init_schema(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def ping(self) -> bool:
        async with self._pool.connection() as conn:
            await conn.execute("SELECT 1")
        return True

    # ------------------------------------------------------------------ onboarding

    async def upsert_user(self, employee_id: str, machine_id: str, machine_model: str | None,
                          machine_category: str | None, display_name: str | None) -> tuple[dict[str, Any], bool]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO insights_users (employee_id, machine_id, machine_model, machine_category, display_name)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (employee_id) DO UPDATE SET
                       machine_id = EXCLUDED.machine_id, machine_model = EXCLUDED.machine_model,
                       machine_category = EXCLUDED.machine_category,
                       display_name = COALESCE(EXCLUDED.display_name, insights_users.display_name),
                       updated_at = now()
                   RETURNING *, (xmax = 0) AS created""",
                (employee_id, machine_id, machine_model, machine_category, display_name))
            row = await cur.fetchone()
        created = bool(row.pop("created"))
        return row, created

    async def get_user(self, employee_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM insights_users WHERE employee_id = %s", (employee_id,))
            return await cur.fetchone()

    # ------------------------------------------------------------------ capture

    async def record_session(self, session_id: str, employee_id: str, machine_id: str, room_name: str | None,
                             participant_identity: str | None, started_at: datetime | None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """INSERT INTO insights_sessions (session_id, employee_id, machine_id, room_name,
                                                  participant_identity, started_at, last_activity_at)
                   VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()), now())
                   ON CONFLICT (session_id) DO UPDATE SET last_activity_at = now()""",
                (session_id, employee_id, machine_id, room_name, participant_identity, started_at))

    async def session_owner(self, session_id: str) -> tuple[str, str] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT employee_id, machine_id FROM insights_sessions WHERE session_id = %s",
                                     (session_id,))
            row = await cur.fetchone()
        return (row["employee_id"], row["machine_id"]) if row else None

    async def record_turn(self, *, session_id: str, turn_id: str, employee_id: str, machine_id: str, question: str,
                          source: str | None, status: str, reply: str | None, action_types: list[str],
                          llm_mode: str | None, asked_at: datetime | None, completed_at: datetime | None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """INSERT INTO insights_interactions (session_id, turn_id, employee_id, machine_id, question, source,
                                                      status, reply, action_types, llm_mode, asked_at, completed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), %s)
                   ON CONFLICT (session_id, turn_id) DO UPDATE SET
                       status = EXCLUDED.status,
                       reply = COALESCE(EXCLUDED.reply, insights_interactions.reply),
                       action_types = CASE WHEN cardinality(EXCLUDED.action_types) > 0
                                           THEN EXCLUDED.action_types ELSE insights_interactions.action_types END,
                       llm_mode = COALESCE(EXCLUDED.llm_mode, insights_interactions.llm_mode),
                       completed_at = COALESCE(EXCLUDED.completed_at, insights_interactions.completed_at)""",
                (session_id, turn_id, employee_id, machine_id, question, source, status, reply, action_types,
                 llm_mode, asked_at, completed_at))
            await conn.execute("UPDATE insights_sessions SET last_activity_at = now() WHERE session_id = %s",
                               (session_id,))

    async def update_turn_result(self, *, session_id: str, turn_id: str, status: str, reply: str | None,
                                 action_types: list[str], llm_mode: str | None, completed_at: datetime | None) -> None:
        """A polled result for a turn that was captured earlier (202 then GET). Unknown turns are ignored."""
        async with self._pool.connection() as conn:
            await conn.execute(
                """UPDATE insights_interactions SET status = %s, reply = COALESCE(%s, reply),
                       action_types = CASE WHEN cardinality(%s::text[]) > 0 THEN %s::text[] ELSE action_types END,
                       llm_mode = COALESCE(%s, llm_mode), completed_at = COALESCE(%s, completed_at)
                   WHERE session_id = %s AND turn_id = %s""",
                (status, reply, action_types, action_types, llm_mode, completed_at, session_id, turn_id))

    # ------------------------------------------------------------------ history / analytics

    async def list_sessions(self, employee_id: str, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT s.*, COUNT(i.turn_id) AS questions
                   FROM insights_sessions s LEFT JOIN insights_interactions i ON i.session_id = s.session_id
                   WHERE s.employee_id = %s
                   GROUP BY s.session_id ORDER BY s.started_at DESC LIMIT %s""", (employee_id, limit))
            return await cur.fetchall()

    async def list_interactions(self, employee_id: str, limit: int = 100,
                                session_id: str | None = None) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """SELECT session_id, turn_id, machine_id, question, source, status, reply, action_types, llm_mode,
                          asked_at, completed_at
                   FROM insights_interactions
                   WHERE employee_id = %s AND (%s::text IS NULL OR session_id = %s)
                   ORDER BY asked_at DESC LIMIT %s""", (employee_id, session_id, session_id, limit))
            return await cur.fetchall()

    async def save_report(self, employee_id: str, llm_mode: str, report: dict[str, Any]) -> int:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO insights_reports (employee_id, llm_mode, report) VALUES (%s, %s, %s) RETURNING report_id",
                (employee_id, llm_mode, Jsonb(report)))
            row = await cur.fetchone()
        return int(row["report_id"])
