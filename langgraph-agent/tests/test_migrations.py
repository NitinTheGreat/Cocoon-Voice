"""Versioned cocoon.db migrations: fresh databases, upgrade of a populated v1 baseline, and refusal paths."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.backup import backup_sqlite
from cocoon_agent.migrations import (
    BASELINE_STATEMENTS, CATALOG_BOUND_SESSIONS, MIGRATIONS, Migration, MigrationError, latest_version, migrate,
)
from cocoon_agent.service import payload_hash
from cocoon_agent.store import Store

from .conftest import AUTH, FIXTURES, make_settings

BASELINE_SQL = (FIXTURES / "baseline_v1_schema.sql").read_text(encoding="utf-8")
LEGACY_TURN_RESULT = {"session_id": "ses_legacy", "turn_id": "t-old", "status": "completed", "speech": "Saved.",
                      "actions": [], "state_version": 1, "llm_mode": "mock", "error": None, "retry_after_ms": None,
                      "poll_url": None, "created_at": "2026-09-20T10:00:00+00:00",
                      "completed_at": "2026-09-20T10:00:01+00:00"}
LEGACY_TABLES = ("sessions", "turns", "tasks", "lessons", "incidents", "training_assignments", "alerts",
                 "telemetry_events", "announcements", "deliveries")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def make_populated_baseline(path: Path) -> None:
    """A pre-I02a database exactly as the v1 code created it, with one record in every table."""
    conn = sqlite3.connect(path)
    conn.executescript(BASELINE_SQL)  # test-only: builds the legacy layout the way v1 did
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO sessions VALUES ('ses_legacy', 'lk:old-room:operator-7', 'old-room', 'operator-7',"
                 " 'operator-7', 'cat-320-demo', 3, '2026-09-20T10:05:00+00:00', '2026-09-20T09:59:00+00:00')")
    conn.execute("INSERT INTO turns VALUES ('ses_legacy', 't-old', ?, 'completed', 1, ?, NULL,"
                 " '2026-09-20T10:00:00+00:00', '2026-09-20T10:00:01+00:00', '2026-09-20T10:00:01+00:00')",
                 (payload_hash({"text": "Log an incident: mirror cracked", "source": "voice"}),
                  json.dumps(LEGACY_TURN_RESULT)))
    conn.execute("INSERT INTO tasks VALUES ('T-101', 'Walkaround', 'Check tracks', 'high', 'pending', 10)")
    conn.execute("INSERT INTO lessons VALUES ('L1', 'Seatbelt basics', 'Why buckle up', 5)")
    conn.execute("INSERT INTO incidents(incident_id, session_id, operator_id, machine_id, description, source_turn_id,"
                 " created_at) VALUES ('INC-0001', 'ses_legacy', 'operator-7', 'cat-320-demo', 'mirror cracked',"
                 " 't-old', '2026-09-20T10:00:01+00:00')")
    conn.execute("INSERT INTO training_assignments VALUES ('TA-1', 'operator-7', 'L1', 'ses_legacy', 't-old',"
                 " 'assigned', '2026-09-20T10:00:02+00:00')")
    conn.execute("INSERT INTO alerts VALUES ('ALR-1', 'ses_legacy', 'prototype.seatbelt_unfastened.v1',"
                 " 'seatbelt_unfastened', 'warning', 'active', 'm', 'e', '{\"engine_on\": true,"
                 " \"seatbelt_fastened\": false, \"idle_seconds\": 0}', 'ev1', NULL, '2026-09-20T10:05:00+00:00', NULL)")
    conn.execute("INSERT INTO telemetry_events VALUES ('ses_legacy', 'ev1', 'h', '2026-09-20T10:05:00+00:00', '{}',"
                 " '{}', '2026-09-20T10:05:01+00:00')")
    conn.execute("INSERT INTO announcements VALUES ('ann_ALR-1_start', 'ses_legacy', 1, 'alert_started', 'high',"
                 " 'Warning', 'ALR-1', '2026-09-20T10:05:01+00:00', '2026-09-20T10:07:01+00:00')")
    conn.execute("INSERT INTO deliveries VALUES ('ann_ALR-1_start', 'voice-1', 'played', NULL,"
                 " '2026-09-20T10:05:03+00:00')")
    conn.commit()
    conn.close()


# Columns later migrations add to legacy tables (compared separately, never part of the legacy row check).
ADDED_COLUMNS = {
    "sessions": ("dataset_manifest_sha256", "site_id", "shift_id", "binding_status", "context_status",
                 "context_source"),
    "incidents": ("status", "origin", "severity", "severity_basis", "site_id", "site_zone_id", "zone_basis",
                  "location_text", "occurred_at", "occurred_basis", "episode_id", "version", "confirmed_at",
                  "dismissed_at"),
    "turns": ("route_json",),
}


def dump(path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(path)
    try:
        out = {}
        for table in LEGACY_TABLES:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            legacy_cols = [c for c in cols if c not in ADDED_COLUMNS.get(table, ())]
            out[table] = conn.execute(f"SELECT {', '.join(legacy_cols)} FROM {table} ORDER BY 1").fetchall()
        out["sqlite_sequence"] = conn.execute("SELECT * FROM sqlite_sequence").fetchall()
        return out
    finally:
        conn.close()


def ledger(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT version, name, mode FROM schema_migrations ORDER BY version").fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------------ fresh and repeated


def test_fresh_database_gets_every_migration_once(tmp_path):
    store = Store(tmp_path / "cocoon.db")
    assert store.init_schema() == [f"applied:{m.version}" for m in MIGRATIONS]
    assert store.init_schema() == []  # re-running startup changes nothing
    store.close()
    assert ledger(tmp_path / "cocoon.db") == [(m.version, m.name, "applied") for m in MIGRATIONS]
    assert latest_version() == len(MIGRATIONS)
    assert [m.name for m in MIGRATIONS[:4]] == ["baseline_v1", "catalog_bound_sessions", "actor_tokens",
                                               "assigned_tasks"]


# ------------------------------------------------------------------ upgrade of the real v1 baseline


def test_populated_v1_baseline_is_adopted_and_upgraded_without_losing_rows(tmp_path):
    db = tmp_path / "cocoon.db"
    make_populated_baseline(db)
    before = dump(db)
    store = Store(db)
    assert store.init_schema() == ["adopted:1"] + [f"applied:{m.version}" for m in MIGRATIONS[1:]]
    assert store.init_schema() == []
    # the next incident continues the AUTOINCREMENT sequence rather than restarting it
    session = store.get_session("ses_legacy")
    incident, created = store.create_incident(session, "second report", "t-new")
    store.close()
    assert created and incident.incident_number == 2

    after = dump(db)
    after["incidents"] = [r for r in after["incidents"] if r[0] == 1]
    after["sqlite_sequence"] = before["sqlite_sequence"]
    assert after == before  # every legacy row and value is unchanged
    assert ledger(db) == [(1, "baseline_v1", "adopted_existing")] + [
        (m.version, m.name, "applied") for m in MIGRATIONS[1:]]
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT dataset_manifest_sha256, site_id, shift_id, binding_status, context_status,"
                       " context_source FROM sessions WHERE session_id = 'ses_legacy'").fetchone()
    conn.close()
    assert row == (None, None, None, "legacy_unverified", "legacy_unverified", None)  # no fabricated provenance
    conn = sqlite3.connect(db)
    # a legacy report was saved immediately as a report: it stays a confirmed operator report, nothing invented
    assert conn.execute("SELECT status, origin, severity, site_zone_id, version FROM incidents"
                        " WHERE incident_number = 1").fetchone() == ("confirmed", "operator_reported", None, None, 1)
    conn.close()


def test_legacy_session_stays_retrievable_and_old_retries_replay(tmp_path):
    make_populated_baseline(tmp_path / "cocoon.db")
    with TestClient(create_app(make_settings(tmp_path))) as c:
        body = {"client_session_key": "lk:old-room:operator-7", "room_name": "old-room",
                "participant_identity": "operator-7", "operator_id": "operator-7", "machine_id": "cat-320-demo"}
        r = c.post("/v1/sessions", json=body, headers=AUTH)
        assert r.status_code == 200, r.text  # retrieval of the old association, even though its IDs are not catalog IDs
        session = r.json()
        assert session["session_id"] == "ses_legacy"
        assert session["binding_status"] == "legacy_unverified" and session["dataset_manifest_sha256"] is None
        assert session["context_status"] == "legacy_unverified" and session["site_id"] is None
        # the same free-text IDs cannot create a NEW session
        new = c.post("/v1/sessions", json={**body, "client_session_key": "lk:old-room:other"}, headers=AUTH)
        assert new.status_code == 422 and new.json()["error"]["code"] == "unknown_machine"
        # an identical retry of a pre-upgrade turn still replays its saved result (fingerprint semantics unchanged)
        retry = c.post("/v1/sessions/ses_legacy/turns", headers=AUTH,
                       json={"turn_id": "t-old", "text": "Log an incident: mirror cracked", "source": "voice"})
        assert retry.status_code == 200 and retry.json()["speech"] == "Saved."
        assert [i["incident_id"] for i in c.get("/v1/sessions/ses_legacy/state", headers=AUTH).json()["incidents"]] \
            == ["INC-0001"]


# ------------------------------------------------------------------ refusal paths


def test_failed_migration_rolls_back_and_keeps_the_previous_version(tmp_path):
    db = tmp_path / "cocoon.db"
    make_populated_baseline(db)
    before = dump(db)
    broken = (MIGRATIONS[0], Migration(2, "broken", ("CREATE TABLE partial_thing (x INTEGER)",
                                                      "ALTER TABLE sessions ADD COLUMN new_col TEXT",
                                                      "THIS IS NOT SQL")))
    conn = _connect(db)
    with pytest.raises(MigrationError) as err:
        migrate(conn, broken)
    assert err.value.issue == "migration_failed"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()
    assert "partial_thing" not in tables and "new_col" not in cols  # DDL rolled back with the failed step
    assert ledger(db) == [(1, "baseline_v1", "adopted_existing")]  # version did not advance past the good step
    assert dump(db) == before


def test_newer_schema_and_ledger_gaps_are_refused(tmp_path):
    db = tmp_path / "cocoon.db"
    Store(db).init_schema()
    conn = _connect(db)
    conn.execute("INSERT INTO schema_migrations VALUES (?, 'from_the_future', 'applied', 'x')", (latest_version() + 1,))
    with pytest.raises(MigrationError) as err:
        migrate(conn)
    assert err.value.issue == "newer_schema"
    conn.execute("DELETE FROM schema_migrations WHERE version = 2")
    with pytest.raises(MigrationError) as err:
        migrate(conn)
    assert err.value.issue == "ledger_inconsistent"
    conn.close()


def test_unknown_legacy_layout_is_not_touched(tmp_path):
    db = tmp_path / "cocoon.db"
    conn = _connect(db)
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, something_else TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('keep-me', 'x')")
    with pytest.raises(MigrationError) as err:
        migrate(conn)
    assert err.value.issue == "unknown_layout"
    assert conn.execute("SELECT * FROM sessions").fetchall() == [("keep-me", "x")]
    assert "schema_migrations" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()


def test_locked_database_fails_within_the_busy_timeout(tmp_path):
    db = tmp_path / "cocoon.db"
    make_populated_baseline(db)
    holder = _connect(db)
    holder.execute("BEGIN IMMEDIATE")
    try:
        store = Store(db, busy_timeout_ms=100)
        with pytest.raises(MigrationError) as err:
            store.init_schema()
        assert err.value.issue == "database_locked"
        store.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert "schema_migrations" not in {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}


def test_app_refuses_to_start_on_a_newer_schema(tmp_path):
    db = make_settings(tmp_path).db_path
    Store(db).init_schema()
    conn = _connect(db)
    conn.execute("INSERT INTO schema_migrations VALUES (99, 'future', 'applied', 'x')")
    conn.close()
    with pytest.raises(MigrationError):
        with TestClient(create_app(make_settings(tmp_path))):
            pass


def test_stored_associations_and_catalog_snapshots_are_immutable(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        sid = c.post("/v1/sessions", headers=AUTH, json={
            "client_session_key": "k", "room_name": "r", "participant_identity": "p", "operator_id": "OP_TEST_1",
            "machine_id": "EXC_DEMO_001"}).json()["session_id"]
    conn = _connect(make_settings(tmp_path).db_path)
    for sql in ("UPDATE sessions SET machine_id = 'DOZ_DEMO_001'", "UPDATE sessions SET site_id = 'S'",
                "UPDATE catalog_versions SET dataset_origin = 'x'", "DELETE FROM catalog_version_operators"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    conn.execute("UPDATE sessions SET state_version = state_version + 1 WHERE session_id = ?", (sid,))  # allowed
    conn.close()


def test_backup_api_copy_is_consistent_while_the_database_is_open(tmp_path):
    db = tmp_path / "cocoon.db"
    make_populated_baseline(db)
    store = Store(db)
    store.init_schema()
    live = _connect(db)  # an open WAL writer with a commit that may still sit in the -wal file
    live.execute("INSERT INTO tasks VALUES ('T-999', 'Late', 'Committed in WAL', 'normal', 'pending', 99)")
    backup = tmp_path / "backups" / "cocoon.db"
    backup_sqlite(db, backup)
    live.close()
    store.close()
    copy = sqlite3.connect(backup)
    assert copy.execute("SELECT COUNT(*) FROM tasks WHERE task_id = 'T-999'").fetchone()[0] == 1
    assert copy.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == latest_version()
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()
    with pytest.raises(FileExistsError):
        backup_sqlite(db, backup)


def test_baseline_statements_match_the_recorded_v1_schema():
    """The adoption check compares against BASELINE_STATEMENTS; they must equal the verbatim v1 SCHEMA."""
    def layout(statements_or_script):
        conn = sqlite3.connect(":memory:")
        if isinstance(statements_or_script, str):
            conn.executescript(statements_or_script)
        else:
            for stmt in statements_or_script:
                conn.execute(stmt)
        return sorted(conn.execute("SELECT type, name, tbl_name FROM sqlite_master").fetchall()), {
            t: conn.execute(f"PRAGMA table_info({t})").fetchall() for t in LEGACY_TABLES}
    assert layout(BASELINE_SQL) == layout(BASELINE_STATEMENTS)
    assert any("ALTER TABLE sessions" in s for s in CATALOG_BOUND_SESSIONS)
