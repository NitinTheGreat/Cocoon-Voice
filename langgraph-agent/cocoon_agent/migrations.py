"""Ordered, versioned SQLite migrations for cocoon.db (not for the LangGraph checkpoint database).

Authoritative version source: the `schema_migrations` ledger (current version = its highest row, rows must be
1..N). Each migration runs with its ledger row inside ONE explicit `BEGIN IMMEDIATE` transaction on a connection
in autocommit mode (isolation_level=None). `executescript()` is never used: it COMMITs any pending transaction
first, which would break atomicity. SQLite DDL is transactional, so a failed migration leaves the previous
version and all rows intact.

The pre-I02a schema had no version. A database whose tables and columns exactly match that baseline is adopted
as version 1 without rewriting anything. Any other unversioned layout, a newer version, or a gap in the ledger is
refused without changes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

BASELINE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    client_session_key TEXT NOT NULL UNIQUE,
    room_name TEXT NOT NULL,
    participant_identity TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 0,
    last_observed_at TEXT,
    created_at TEXT NOT NULL
)""",
    """CREATE TABLE turns (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    turn_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 1,
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (session_id, turn_id)
)""",
    """CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    details TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    sort_order INTEGER NOT NULL
)""",
    """CREATE TABLE lessons (
    lesson_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL
)""",
    """CREATE TABLE incidents (
    incident_number INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    operator_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    description TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, source_turn_id)
)""",
    """CREATE TABLE training_assignments (
    assignment_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL,
    lesson_id TEXT NOT NULL REFERENCES lessons(lesson_id),
    session_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    status TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    UNIQUE (operator_id, lesson_id)
)""",
    """CREATE TABLE alerts (
    alert_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    rule_id TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'cleared')),
    message TEXT NOT NULL,
    explanation TEXT NOT NULL,
    trigger_readings_json TEXT NOT NULL,
    opened_by_event_id TEXT NOT NULL,
    cleared_by_event_id TEXT,
    started_at TEXT NOT NULL,
    cleared_at TEXT
)""",
    """CREATE UNIQUE INDEX one_active_episode_per_rule
    ON alerts(session_id, rule_id) WHERE status = 'active'""",
    """CREATE TABLE telemetry_events (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    event_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    readings_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (session_id, event_id)
)""",
    """CREATE TABLE announcements (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    sequence INTEGER NOT NULL,
    type TEXT NOT NULL,
    priority TEXT NOT NULL,
    speech TEXT NOT NULL,
    alert_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    UNIQUE (session_id, sequence),
    UNIQUE (alert_id, type)
)""",
    """CREATE TABLE deliveries (
    event_id TEXT NOT NULL REFERENCES announcements(event_id),
    consumer_id TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (event_id, consumer_id)
)""",
)

LEDGER_STATEMENT = """CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('applied', 'adopted_existing')),
    applied_at TEXT NOT NULL
)"""

CATALOG_BOUND_SESSIONS: tuple[str, ...] = (
    # Immutable, append-only record of each verified catalog snapshot a session was bound to.
    """CREATE TABLE catalog_versions (
    manifest_sha256 TEXT PRIMARY KEY CHECK (length(manifest_sha256) = 64),
    manifest_schema_version TEXT NOT NULL,
    dataset_origin TEXT NOT NULL,
    generator_seed INTEGER,
    machines_sha256 TEXT NOT NULL,
    operators_sha256 TEXT NOT NULL,
    machine_count INTEGER NOT NULL,
    operator_count INTEGER NOT NULL,
    first_registered_at TEXT NOT NULL
)""",
    """CREATE TABLE catalog_version_machines (
    manifest_sha256 TEXT NOT NULL REFERENCES catalog_versions(manifest_sha256),
    machine_id TEXT NOT NULL,
    model TEXT NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (manifest_sha256, machine_id)
)""",
    """CREATE TABLE catalog_version_operators (
    manifest_sha256 TEXT NOT NULL REFERENCES catalog_versions(manifest_sha256),
    operator_id TEXT NOT NULL,
    PRIMARY KEY (manifest_sha256, operator_id)
)""",
    *(f"""CREATE TRIGGER {table}_immutable_{op.lower()} BEFORE {op} ON {table}
    BEGIN SELECT RAISE(ABORT, 'catalog snapshots are immutable'); END"""
      for table in ("catalog_versions", "catalog_version_machines", "catalog_version_operators")
      for op in ("UPDATE", "DELETE")),
    # Additive session binding metadata. Existing rows become legacy_unverified with NULL provenance.
    "ALTER TABLE sessions ADD COLUMN dataset_manifest_sha256 TEXT REFERENCES catalog_versions(manifest_sha256)",
    "ALTER TABLE sessions ADD COLUMN site_id TEXT",
    "ALTER TABLE sessions ADD COLUMN shift_id TEXT",
    """ALTER TABLE sessions ADD COLUMN binding_status TEXT NOT NULL DEFAULT 'legacy_unverified'
    CHECK (binding_status IN ('legacy_unverified', 'catalog_verified'))""",
    """ALTER TABLE sessions ADD COLUMN context_status TEXT NOT NULL DEFAULT 'legacy_unverified'
    CHECK (context_status IN ('legacy_unverified', 'unavailable', 'trusted_binding'))""",
    "ALTER TABLE sessions ADD COLUMN context_source TEXT",
    # A stored association can never be silently rebound; only state_version/last_observed_at change.
    """CREATE TRIGGER sessions_association_immutable BEFORE UPDATE OF
    session_id, client_session_key, room_name, participant_identity, operator_id, machine_id, created_at,
    dataset_manifest_sha256, site_id, shift_id, binding_status, context_status, context_source ON sessions
    BEGIN SELECT RAISE(ABORT, 'session association is immutable'); END""",
)


ACTOR_TOKENS: tuple[str, ...] = (
    # Server-owned actor identities. The service credential (COCOON_SERVICE_TOKEN) is never stored here.
    """CREATE TABLE principals (
    principal_id TEXT PRIMARY KEY CHECK (length(principal_id) BETWEEN 1 AND 128),
    kind TEXT NOT NULL CHECK (kind IN ('operator', 'supervisor')),
    operator_id TEXT,
    operator_catalog_sha256 TEXT REFERENCES catalog_versions(manifest_sha256),
    display_name TEXT CHECK (display_name IS NULL OR length(display_name) <= 128),
    created_at TEXT NOT NULL,
    CHECK ((kind = 'operator' AND operator_id IS NOT NULL AND operator_catalog_sha256 IS NOT NULL)
        OR (kind = 'supervisor' AND operator_id IS NULL AND operator_catalog_sha256 IS NULL))
)""",
    "CREATE UNIQUE INDEX one_principal_per_operator ON principals(operator_id) WHERE kind = 'operator'",
    *(f"""CREATE TRIGGER principals_immutable_{op.lower()} BEFORE {op} ON principals
    BEGIN SELECT RAISE(ABORT, 'principals are immutable'); END""" for op in ("UPDATE", "DELETE")),
    # Opaque bearer tokens: only a SHA-256 digest of the random token is stored, never the token itself.
    """CREATE TABLE actor_tokens (
    token_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    token_sha256 TEXT NOT NULL UNIQUE CHECK (length(token_sha256) = 64),
    scopes TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT CHECK (revoke_reason IS NULL OR length(revoke_reason) <= 200),
    CHECK (expires_at > issued_at)
)""",
    """CREATE TRIGGER actor_tokens_fixed BEFORE UPDATE OF
    token_id, principal_id, token_sha256, scopes, issued_at, expires_at ON actor_tokens
    BEGIN SELECT RAISE(ABORT, 'token records are immutable except for revocation'); END""",
    """CREATE TRIGGER actor_tokens_revoke_once BEFORE UPDATE OF revoked_at, revoke_reason ON actor_tokens
    WHEN OLD.revoked_at IS NOT NULL
    BEGIN SELECT RAISE(ABORT, 'a revoked token stays revoked'); END""",
    """CREATE TRIGGER actor_tokens_no_delete BEFORE DELETE ON actor_tokens
    BEGIN SELECT RAISE(ABORT, 'token records are kept for audit'); END""",
)


ASSIGNED_TASKS: tuple[str, ...] = (
    # Server-controlled demo site data (seeded by scripts/seed_demo.py from a tracked synthetic fixture).
    """CREATE TABLE sites (
    site_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    utc_offset TEXT NOT NULL,
    fixture_version TEXT NOT NULL,
    provenance TEXT NOT NULL
)""",
    """CREATE TABLE site_zones (
    site_zone_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    zone_type TEXT NOT NULL,
    outdoor INTEGER NOT NULL CHECK (outdoor IN (0, 1))
)""",
    """CREATE TABLE shifts (
    shift_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    operator_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    catalog_manifest_sha256 TEXT NOT NULL,
    fixture_version TEXT NOT NULL,
    UNIQUE (operator_id, machine_id, service_date)
)""",
    # Per-shift task assignments with a versioned lifecycle (the three shared seed tasks stay untouched).
    """CREATE TABLE task_assignments (
    task_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    operator_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    site_zone_id TEXT NOT NULL REFERENCES site_zones(site_zone_id),
    scheduled_order INTEGER NOT NULL,
    scheduled_start_at TEXT NOT NULL,
    task_type TEXT NOT NULL,
    title TEXT NOT NULL,
    details TEXT NOT NULL,
    work_quantity REAL,
    work_unit TEXT,
    weather_json TEXT NOT NULL,
    duration_minutes INTEGER,
    duration_source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'in_progress', 'completed')),
    version INTEGER NOT NULL DEFAULT 1,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    provenance TEXT NOT NULL,
    UNIQUE (shift_id, scheduled_order)
)""",
    # One row per committed domain command (tap or graph tool): identity, fingerprint, outcome and result.
    """CREATE TABLE command_log (
    scope TEXT NOT NULL,
    command_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    turn_id TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('completed', 'failed', 'unknown')),
    record_type TEXT,
    record_id TEXT,
    summary TEXT NOT NULL,
    state_version INTEGER,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, command_id)
)""",
    "CREATE INDEX command_log_by_turn ON command_log(session_id, turn_id)",
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline_v1", BASELINE_STATEMENTS),
    Migration(2, "catalog_bound_sessions", CATALOG_BOUND_SESSIONS),
    Migration(3, "actor_tokens", ACTOR_TOKENS),
    Migration(4, "assigned_tasks", ASSIGNED_TASKS),
)


class MigrationError(Exception):
    """The database was left at its previous version. `issue` is a short code; nothing was wiped."""

    def __init__(self, issue: str, message: str):
        super().__init__(message)
        self.issue = issue


def latest_version(migrations: tuple[Migration, ...] = MIGRATIONS) -> int:
    return migrations[-1].version


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return {r[0] for r in rows}


def _layout(conn: sqlite3.Connection) -> dict[str, tuple]:
    """Table → column names, plus named indexes. Used to recognise the unversioned v1 baseline exactly."""
    out: dict[str, tuple] = {}
    for table in sorted(_user_tables(conn)):
        out[table] = tuple(r[1] for r in conn.execute(f'PRAGMA table_info("{table}")'))
    indexes = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")
    out["<indexes>"] = tuple(sorted(r[0] for r in indexes))
    return out


def _baseline_layout() -> dict[str, tuple]:
    mem = sqlite3.connect(":memory:")
    try:
        for stmt in BASELINE_STATEMENTS:
            mem.execute(stmt)
        return _layout(mem)
    finally:
        mem.close()


def ledger_versions(conn: sqlite3.Connection) -> list[int] | None:
    if "schema_migrations" not in _user_tables(conn):
        return None
    return [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def migrate(conn: sqlite3.Connection, migrations: tuple[Migration, ...] = MIGRATIONS,
            now: Callable[[], str] = _utcnow) -> list[str]:
    """Bring cocoon.db to the latest version. Returns what was done, e.g. ['adopted:1', 'applied:2']."""
    if conn.isolation_level is not None:
        raise MigrationError("bad_connection", "migrations need an autocommit connection (isolation_level=None)")
    latest = latest_version(migrations)
    done: list[str] = []
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MigrationError("database_locked",
                                 "database is locked by another connection; no migration was applied") from exc
        try:
            versions = ledger_versions(conn)
            if versions is None:
                tables = _user_tables(conn)
                if not tables:
                    step, mode = migrations[0], "applied"
                    conn.execute(LEDGER_STATEMENT)
                    for stmt in step.statements:
                        conn.execute(stmt)
                elif _layout(conn) == _baseline_layout():
                    step, mode = migrations[0], "adopted_existing"
                    conn.execute(LEDGER_STATEMENT)
                else:
                    raise MigrationError("unknown_layout",
                                         f"unversioned database with an unrecognised layout (tables: {sorted(tables)})"
                                         "; refusing to modify it")
            else:
                if versions != list(range(1, len(versions) + 1)):
                    raise MigrationError("ledger_inconsistent", f"migration ledger is not contiguous: {versions}")
                current = versions[-1] if versions else 0
                if current > latest:
                    raise MigrationError("newer_schema",
                                         f"database schema version {current} is newer than supported {latest}")
                if current == latest:
                    conn.execute("ROLLBACK")
                    return done
                step, mode = migrations[current], "applied"
                for stmt in step.statements:
                    conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(version, name, mode, applied_at) VALUES (?, ?, ?, ?)",
                         (step.version, step.name, mode, now()))
            conn.execute("COMMIT")
            done.append(f"{'adopted' if mode == 'adopted_existing' else 'applied'}:{step.version}")
        except BaseException as exc:
            conn.execute("ROLLBACK")
            if isinstance(exc, MigrationError):
                raise
            if isinstance(exc, sqlite3.Error):
                raise MigrationError("migration_failed", f"migration failed and was rolled back: {exc}") from exc
            raise
