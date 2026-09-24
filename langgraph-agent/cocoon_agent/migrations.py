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


STRUCTURED_INCIDENTS: tuple[str, ...] = (
    # Structured report fields. Existing rows were saved immediately as operator reports (the default origin); their
    # new fields stay NULL (not stated), nothing is back-filled.
    """ALTER TABLE incidents ADD COLUMN origin TEXT NOT NULL DEFAULT 'operator_reported'
    CHECK (origin IN ('operator_reported', 'auto_draft'))""",
    "ALTER TABLE incidents ADD COLUMN severity TEXT CHECK (severity IN ('low', 'medium', 'high', 'critical'))",
    "ALTER TABLE incidents ADD COLUMN severity_basis TEXT",
    "ALTER TABLE incidents ADD COLUMN site_id TEXT",
    "ALTER TABLE incidents ADD COLUMN site_zone_id TEXT",
    "ALTER TABLE incidents ADD COLUMN zone_basis TEXT",
    "ALTER TABLE incidents ADD COLUMN location_text TEXT",
    "ALTER TABLE incidents ADD COLUMN occurred_at TEXT",
    "ALTER TABLE incidents ADD COLUMN occurred_basis TEXT",
    "ALTER TABLE incidents ADD COLUMN episode_id TEXT",
    "ALTER TABLE incidents ADD COLUMN draft_id TEXT",
    "ALTER TABLE incidents ADD COLUMN confirmed_at TEXT",
    # Confirming a draft creates its incident exactly once.
    "CREATE UNIQUE INDEX one_incident_per_draft ON incidents(draft_id) WHERE draft_id IS NOT NULL",
    # Unconfirmed drafts live apart from reports and have their own numbering; a real incident ID is allocated only
    # when a draft is confirmed.
    """CREATE TABLE incident_drafts (
    draft_number INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    operator_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('auto_draft')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed', 'dismissed')),
    description TEXT NOT NULL,
    severity TEXT CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    severity_basis TEXT,
    site_id TEXT,
    site_zone_id TEXT,
    zone_basis TEXT,
    location_text TEXT,
    occurred_at TEXT,
    occurred_basis TEXT,
    episode_id TEXT UNIQUE,
    version INTEGER NOT NULL DEFAULT 1,
    incident_id TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    dismissed_at TEXT
)""",
    # The routing decision of a turn, saved once, so a retried turn repeats the same plan without a new model call.
    "ALTER TABLE turns ADD COLUMN route_json TEXT",
    # A request for supervisor review. Pending until a supervisor decision route exists (Batch D).
    """CREATE TABLE approval_requests (
    approval_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    operator_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('incident_escalation')),
    incident_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    created_at TEXT NOT NULL,
    UNIQUE (kind, incident_id)
)""",
)


MACHINE_EPISODES: tuple[str, ...] = (
    # Episode details saved when an alert opens (policy, reason, action, evidence) and its links. Existing alerts
    # were announced when they opened, so `announced` defaults to 1; their other new columns stay NULL (unknown).
    "ALTER TABLE alerts ADD COLUMN policy_version TEXT",
    "ALTER TABLE alerts ADD COLUMN source_status TEXT",
    "ALTER TABLE alerts ADD COLUMN reason TEXT",
    "ALTER TABLE alerts ADD COLUMN recommended_action TEXT",
    "ALTER TABLE alerts ADD COLUMN evidence_json TEXT",
    "ALTER TABLE alerts ADD COLUMN correlated_alert_id TEXT",
    "ALTER TABLE alerts ADD COLUMN draft_incident_id TEXT",
    "ALTER TABLE alerts ADD COLUMN announced INTEGER NOT NULL DEFAULT 1",
    # Latest applied observation per session: observation clock for durations, receipt clock for freshness.
    """CREATE TABLE machine_state (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    event_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    readings_json TEXT NOT NULL,
    idle_since TEXT
)""",
    "ALTER TABLE telemetry_events ADD COLUMN provenance_json TEXT",
)


OPERATOR_CONTEXT: tuple[str, ...] = (
    # An operator's stated reason for idling, linked to the idle episode it explains. It never clears an episode.
    """CREATE TABLE idle_reasons (
    reason_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    operator_id TEXT NOT NULL,
    alert_id TEXT,
    reason_text TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, source_turn_id)
)""",
    # One briefing per shift, whatever the number of sessions or restarts.
    """CREATE TABLE shift_briefings (
    shift_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    event_id TEXT NOT NULL,
    speech TEXT NOT NULL,
    created_at TEXT NOT NULL
)""",
    # Versioned lesson text (only where authored; NULL = title/summary only, as before).
    "ALTER TABLE lessons ADD COLUMN version TEXT",
    "ALTER TABLE lessons ADD COLUMN content_text TEXT",
    "ALTER TABLE lessons ADD COLUMN content_status TEXT",
    # Behaviour-triggered assignment: the episode that caused it, and the link from the episode.
    "ALTER TABLE training_assignments ADD COLUMN source_episode_id TEXT",
    "ALTER TABLE alerts ADD COLUMN training_assignment_id TEXT",
)


_DRAFT_V5_COLUMNS = ("draft_number, draft_id, session_id, operator_id, machine_id, origin, status, description, severity,"
                     " severity_basis, site_id, site_zone_id, zone_basis, location_text, occurred_at, occurred_basis,"
                     " episode_id, version, incident_id, created_at, confirmed_at, dismissed_at")

INCIDENT_CAPTURE: tuple[str, ...] = (
    # How the occurrence time was derived: the operator's original phrase and the persisted reference instant it was
    # interpreted against (so a retry can never move it). Existing rows keep NULL (their basis is unchanged).
    "ALTER TABLE incidents ADD COLUMN occurred_expression TEXT",
    "ALTER TABLE incidents ADD COLUMN occurred_reference_at TEXT",
    # An operator's report that still misses a fact (severity or time) is kept as a draft while it is clarified.
    # SQLite cannot widen a CHECK constraint in place, so the draft table is rebuilt: every row is copied with its
    # original draft_number (explicit values keep the AUTOINCREMENT sequence), nothing is renumbered.
    """CREATE TABLE incident_drafts_v8 (
    draft_number INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    operator_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('auto_draft', 'operator_report')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed', 'dismissed')),
    description TEXT NOT NULL,
    severity TEXT CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    severity_basis TEXT,
    site_id TEXT,
    site_zone_id TEXT,
    zone_basis TEXT,
    location_text TEXT,
    occurred_at TEXT,
    occurred_basis TEXT,
    episode_id TEXT UNIQUE,
    version INTEGER NOT NULL DEFAULT 1,
    incident_id TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    dismissed_at TEXT,
    occurred_expression TEXT,
    occurred_reference_at TEXT,
    source_turn_id TEXT,
    notify_supervisor INTEGER NOT NULL DEFAULT 0 CHECK (notify_supervisor IN (0, 1))
)""",
    f"INSERT INTO incident_drafts_v8({_DRAFT_V5_COLUMNS}) SELECT {_DRAFT_V5_COLUMNS} FROM incident_drafts",
    # Carry the old AUTOINCREMENT high-water mark too, so a number is never handed out twice.
    "INSERT INTO sqlite_sequence(name, seq) SELECT 'incident_drafts_v8', seq FROM sqlite_sequence"
    " WHERE name = 'incident_drafts' AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'incident_drafts_v8')",
    "UPDATE sqlite_sequence SET seq = MAX(seq, (SELECT seq FROM sqlite_sequence WHERE name = 'incident_drafts'))"
    " WHERE name = 'incident_drafts_v8' AND EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'incident_drafts')",
    "DROP TABLE incident_drafts",
    "ALTER TABLE incident_drafts_v8 RENAME TO incident_drafts",
    # One operator draft per reporting turn (the command log already makes the write exactly-once).
    "CREATE UNIQUE INDEX one_draft_per_report_turn ON incident_drafts(session_id, source_turn_id)"
    " WHERE source_turn_id IS NOT NULL",
)


SITE_CONDITIONS: tuple[str, ...] = (
    # Trusted site coordinates for weather lookups (server fixture only; never from a request). NULL = no location.
    "ALTER TABLE sites ADD COLUMN latitude REAL",
    "ALTER TABLE sites ADD COLUMN longitude REAL",
    "ALTER TABLE sites ADD COLUMN location_basis TEXT",
    # A weather value set exactly as used by a saved check or episode (evidence never changes afterwards).
    """CREATE TABLE weather_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('open_meteo', 'fixture')),
    kind TEXT NOT NULL,
    record_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
)""",
    # One working-conditions check (task start, or an in-task re-check) with its findings and weather evidence.
    """CREATE TABLE condition_checks (
    check_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    task_id TEXT,
    purpose TEXT NOT NULL CHECK (purpose IN ('task_start', 'in_task')),
    level TEXT NOT NULL,
    coverage TEXT NOT NULL,
    check_json TEXT NOT NULL,
    weather_record_id TEXT,
    policy_version TEXT NOT NULL,
    data_time TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
    created_at TEXT NOT NULL
)""",
    # The check that let a task start (kept even if the weather or policy changes later).
    "ALTER TABLE task_assignments ADD COLUMN start_check_id TEXT",
    # Family-specific evidence of an episode (e.g. the condition check behind a working-conditions warning), saved
    # once when it opens. NULL for belt/idle episodes, whose evidence is in evidence_json.
    "ALTER TABLE alerts ADD COLUMN details_json TEXT",
)


HAZARD_RULES: tuple[str, ...] = (
    # One active episode per rule AND subject (e.g. one proximity episode per detected entity). Existing rows get the
    # empty subject, so the old one-per-rule behaviour of belt/idle episodes is unchanged.
    "ALTER TABLE alerts ADD COLUMN subject_key TEXT NOT NULL DEFAULT ''",
    "DROP INDEX one_active_episode_per_rule",
    "CREATE UNIQUE INDEX one_active_episode_per_subject ON alerts(session_id, rule_id, subject_key)"
    " WHERE status = 'active'",
    # Current level of a graded episode (warning/danger, acknowledge/block) and why it ended.
    "ALTER TABLE alerts ADD COLUMN level TEXT",
    "ALTER TABLE alerts ADD COLUMN last_seen_at TEXT",
    "ALTER TABLE alerts ADD COLUMN cleared_reason TEXT",
    # Later changes of a published episode get their own evidence and identity; the opening evidence never changes.
    """CREATE TABLE alert_updates (
    update_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES alerts(alert_id),
    level TEXT NOT NULL,
    previous_level TEXT,
    observed_at TEXT NOT NULL,
    event_id TEXT NOT NULL,
    details_json TEXT NOT NULL,
    announcement_event_id TEXT,
    created_at TEXT NOT NULL
)""",
    "CREATE INDEX alert_updates_by_alert ON alert_updates(alert_id, observed_at)",
    # Rule state carried between observations (fuel window accumulator, last evaluated motion sample, coverage).
    "ALTER TABLE machine_state ADD COLUMN rule_state_json TEXT",
    # A repeat-violation trigger creates a pending supervisor review linked to its episode (no incident involved), so
    # the review table is rebuilt to widen its kind and make incident_id optional. Rows are copied unchanged.
    """CREATE TABLE approval_requests_v10 (
    approval_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    operator_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('incident_escalation', 'repeated_violations')),
    incident_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    created_at TEXT NOT NULL,
    alert_id TEXT,
    details_json TEXT,
    UNIQUE (kind, incident_id),
    UNIQUE (kind, alert_id),
    CHECK (incident_id IS NOT NULL OR alert_id IS NOT NULL)
)""",
    "INSERT INTO approval_requests_v10(approval_id, session_id, operator_id, kind, incident_id, status, created_at)"
    " SELECT approval_id, session_id, operator_id, kind, incident_id, status, created_at FROM approval_requests",
    "DROP TABLE approval_requests",
    "ALTER TABLE approval_requests_v10 RENAME TO approval_requests",
)


TASK_ESTIMATES: tuple[str, ...] = (
    # Versioned duration estimates, saved once per task + estimator version + input snapshot.
    """CREATE TABLE task_estimates (
    estimate_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    estimator_version TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    inputs_sha256 TEXT NOT NULL,
    method TEXT NOT NULL,
    predicted_minutes REAL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, config_sha256, inputs_sha256)
)""",
    # The estimate in force when a task started (kept when weather or the estimator changes later).
    "ALTER TABLE task_assignments ADD COLUMN start_estimate_id TEXT",
    # Synthetic fixture ground condition (estimator input); NULL = not stated.
    "ALTER TABLE task_assignments ADD COLUMN ground_condition TEXT",
)


CORE_LMS: tuple[str, ...] = (
    # One pinned copy of each lesson version (text steps, assessment and answer key), so progress and attempts keep
    # the exact version they started on.
    """CREATE TABLE lesson_versions (
    lesson_id TEXT NOT NULL REFERENCES lessons(lesson_id),
    version TEXT NOT NULL,
    curriculum_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    content_json TEXT NOT NULL,
    review_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (lesson_id, version)
)""",
    # Per learner (catalog operator of a verified session) and lesson version.
    """CREATE TABLE lesson_progress (
    learner_id TEXT NOT NULL,
    lesson_id TEXT NOT NULL,
    lesson_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'paused', 'deferred', 'awaiting_assessment', 'completed')),
    current_step INTEGER NOT NULL DEFAULT 0,
    steps_seen INTEGER NOT NULL DEFAULT 0,
    deferred_until TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    last_session_id TEXT,
    PRIMARY KEY (learner_id, lesson_id, lesson_version),
    FOREIGN KEY (lesson_id, lesson_version) REFERENCES lesson_versions(lesson_id, version)
)""",
    """CREATE TABLE quiz_attempts (
    attempt_id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    lesson_id TEXT NOT NULL,
    lesson_version TEXT NOT NULL,
    quiz_id TEXT NOT NULL,
    quiz_version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('quiz', 'scenario')),
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'passed', 'failed')),
    current_index INTEGER NOT NULL DEFAULT 0,
    current_node TEXT,
    correct INTEGER NOT NULL DEFAULT 0,
    answered INTEGER NOT NULL DEFAULT 0,
    total INTEGER,
    score REAL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    session_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (learner_id, lesson_id, lesson_version, attempt_number),
    FOREIGN KEY (lesson_id, lesson_version) REFERENCES lesson_versions(lesson_id, version)
)""",
    "CREATE UNIQUE INDEX one_active_attempt_per_lesson ON quiz_attempts(learner_id, lesson_id) WHERE status = 'active'",
    """CREATE TABLE quiz_answers (
    attempt_id TEXT NOT NULL REFERENCES quiz_attempts(attempt_id),
    question_id TEXT NOT NULL,
    choice_id TEXT NOT NULL,
    correct INTEGER NOT NULL CHECK (correct IN (0, 1)),
    answered_at TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (attempt_id, question_id)
)""",
    """CREATE TABLE learner_levels (
    learner_id TEXT PRIMARY KEY,
    level TEXT NOT NULL CHECK (level IN ('beginner', 'intermediate', 'expert')),
    criteria_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE level_history (
    entry_id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    level TEXT NOT NULL,
    previous_level TEXT,
    criteria_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    achieved_at TEXT NOT NULL
)""",
    # Assignment lifecycle beyond "assigned": completion (by a passed assessment only), deferral and the one-time
    # coaching prompt for an episode-linked lesson.
    "ALTER TABLE training_assignments ADD COLUMN completed_at TEXT",
    "ALTER TABLE training_assignments ADD COLUMN deferred_until TEXT",
    "ALTER TABLE training_assignments ADD COLUMN coaching_prompted_at TEXT",
)


CONSENT_WELLBEING: tuple[str, ...] = (
    # Purpose-specific consent owned by the operator. Current state per purpose plus an append-only change log.
    """CREATE TABLE consent_state (
    operator_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 0
)""",
    """CREATE TABLE consent_current (
    operator_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('vitals_processing', 'risk_sharing_supervisor')),
    status TEXT NOT NULL CHECK (status IN ('granted', 'revoked')),
    notice_version TEXT NOT NULL,
    effective_at TEXT,
    revoked_at TEXT,
    is_synthetic INTEGER NOT NULL CHECK (is_synthetic IN (0, 1)),
    provenance TEXT NOT NULL,
    PRIMARY KEY (operator_id, purpose)
)""",
    """CREATE TABLE consent_changes (
    operator_id TEXT NOT NULL,
    change_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    purpose TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('grant', 'revoke')),
    notice_version TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    resulting_version INTEGER NOT NULL,
    cascaded TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (operator_id, change_id)
)""",
    # Raw private samples, kept only under an effective processing grant and only until expires_at (bounded
    # retention, purged by the maintenance worker and on revocation).
    """CREATE TABLE wellbeing_samples (
    operator_id TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    observed_at TEXT NOT NULL,
    heart_rate_bpm REAL,
    skin_temp_c REAL,
    window_seconds INTEGER NOT NULL,
    quality TEXT NOT NULL,
    source TEXT NOT NULL,
    received_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (operator_id, sample_id)
)""",
    "CREATE INDEX wellbeing_samples_by_time ON wellbeing_samples(operator_id, observed_at)",
    # Outcome of every sample request, WITHOUT values (idempotency and audit never retain raw inputs).
    """CREATE TABLE wellbeing_sample_outcomes (
    operator_id TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (operator_id, sample_id)
)""",
    # Operator-private advice episodes with their saved derived evidence and explanation.
    """CREATE TABLE wellbeing_advice (
    advice_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    rule_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('advisory', 'high')),
    status TEXT NOT NULL CHECK (status IN ('active', 'cleared', 'withdrawn')),
    evidence_json TEXT NOT NULL,
    explanation TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    announcement_event_id TEXT
)""",
    "CREATE UNIQUE INDEX one_active_advice_per_rule ON wellbeing_advice(operator_id, rule_id) WHERE status = 'active'",
    # Explicit breaks (engine-off, waiting or telemetry silence are never a break).
    """CREATE TABLE break_records (
    break_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    shift_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
)""",
    "CREATE UNIQUE INDEX one_open_break ON break_records(operator_id) WHERE ended_at IS NULL",
    # Supervisor change-feed outbox: references only (projected at read time under current scope and consent).
    """CREATE TABLE supervisor_feed (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    site_id TEXT NOT NULL,
    type TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    created_at TEXT NOT NULL
)""",
    "CREATE INDEX supervisor_feed_by_site ON supervisor_feed(site_id, sequence)",
)


SUPERVISION_APPROVALS: tuple[str, ...] = (
    # Trusted supervisor scope, granted only by the local admin CLI (never inferred from speech, query or metadata).
    """CREATE TABLE principal_site_grants (
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    granted_at TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (principal_id, site_id)
)""",
    # approval_requests is rebuilt (like v10) to add schedule proposals and the cancelled state. Existing rows are
    # copied unchanged; their trusted site comes ONLY from their session's trusted binding (else they are
    # ineligible, never globally visible); their immutable payload is derived from the stored references.
    """CREATE TABLE approval_requests_v14 (
    approval_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    operator_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('incident_escalation', 'repeated_violations', 'schedule_change')),
    incident_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')),
    created_at TEXT NOT NULL,
    alert_id TEXT,
    details_json TEXT,
    site_id TEXT,
    eligibility TEXT NOT NULL DEFAULT 'eligible' CHECK (eligibility IN ('eligible', 'ineligible_no_site')),
    proposer TEXT NOT NULL DEFAULT 'legacy_request',
    action_type TEXT NOT NULL DEFAULT 'notify_supervisor'
        CHECK (action_type IN ('notify_supervisor', 'escalate_repeat_violation', 'apply_schedule_change')),
    payload_json TEXT,
    resource_versions_json TEXT,
    evidence_refs_json TEXT,
    dedup_key TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    decision_id TEXT,
    decision TEXT CHECK (decision IS NULL OR decision IN ('approve', 'reject')),
    decision_hash TEXT,
    decided_by TEXT,
    decided_at TEXT,
    decision_reason TEXT,
    application_status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (application_status IN ('not_started', 'pending', 'applied', 'failed_stale_inputs', 'failed',
                                      'not_applicable')),
    application_reason TEXT,
    applied_at TEXT,
    updated_at TEXT,
    UNIQUE (kind, incident_id),
    UNIQUE (kind, alert_id),
    CHECK (incident_id IS NOT NULL OR alert_id IS NOT NULL OR kind = 'schedule_change')
)""",
    "INSERT INTO approval_requests_v14(approval_id, session_id, operator_id, kind, incident_id, status, created_at,"
    " alert_id, details_json) SELECT approval_id, session_id, operator_id, kind, incident_id, status, created_at,"
    " alert_id, details_json FROM approval_requests",
    "DROP TABLE approval_requests",
    "ALTER TABLE approval_requests_v14 RENAME TO approval_requests",
    """UPDATE approval_requests SET
    site_id = (SELECT s.site_id FROM sessions s WHERE s.session_id = approval_requests.session_id
               AND s.binding_status = 'catalog_verified' AND s.context_status = 'trusted_binding'),
    action_type = CASE kind WHEN 'repeated_violations' THEN 'escalate_repeat_violation' ELSE 'notify_supervisor' END,
    payload_json = json_object('kind', kind, 'incident_id', incident_id, 'alert_id', alert_id,
                               'details', json(COALESCE(details_json, 'null'))),
    updated_at = created_at""",
    "UPDATE approval_requests SET eligibility = 'ineligible_no_site' WHERE site_id IS NULL",
    "CREATE UNIQUE INDEX one_pending_proposal ON approval_requests(dedup_key)"
    " WHERE status = 'pending' AND dedup_key IS NOT NULL",
    "CREATE INDEX approval_requests_by_site ON approval_requests(site_id, status, created_at)",
    """CREATE TRIGGER approval_payload_immutable BEFORE UPDATE OF payload_json, kind, action_type, site_id,
    operator_id, session_id ON approval_requests WHEN OLD.payload_json IS NOT NULL
    BEGIN SELECT RAISE(ABORT, 'an approval request payload and scope are immutable'); END""",
    # In-app supervisor notifications: application records only (no email/SMS/phone/dispatch). One per source.
    """CREATE TABLE supervisor_notifications (
    notification_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    operator_id TEXT,
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal', 'urgent')),
    policy_id TEXT,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'presented', 'acknowledged')),
    created_at TEXT NOT NULL,
    presented_at TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    UNIQUE (kind, source_id)
)""",
    "CREATE INDEX supervisor_notifications_by_site ON supervisor_notifications(site_id, created_at)",
    # Weather re-planning: the schedule of a shift is versioned as a whole.
    "ALTER TABLE shifts ADD COLUMN schedule_version INTEGER NOT NULL DEFAULT 1",
    # How far the supervisor feed was pruned (replay below this cursor is 410 replay_expired).
    "CREATE TABLE feed_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
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
    Migration(5, "structured_incidents", STRUCTURED_INCIDENTS),
    Migration(6, "machine_episodes", MACHINE_EPISODES),
    Migration(7, "operator_context", OPERATOR_CONTEXT),
    Migration(8, "incident_capture", INCIDENT_CAPTURE),
    Migration(9, "site_conditions", SITE_CONDITIONS),
    Migration(10, "hazard_rules", HAZARD_RULES),
    Migration(11, "task_estimates", TASK_ESTIMATES),
    Migration(12, "core_lms", CORE_LMS),
    Migration(13, "consent_wellbeing", CONSENT_WELLBEING),
    Migration(14, "supervision_approvals", SUPERVISION_APPROVALS),
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
