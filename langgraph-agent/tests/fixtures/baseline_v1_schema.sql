-- Verbatim SCHEMA of langgraph-agent/cocoon_agent/store.py at commit 6bcbf88 (unversioned v1 baseline).
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    client_session_key TEXT NOT NULL UNIQUE,
    room_name TEXT NOT NULL,
    participant_identity TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 0,
    last_observed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
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
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    details TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    sort_order INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons (
    lesson_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_number INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    operator_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    description TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, source_turn_id)
);
CREATE TABLE IF NOT EXISTS training_assignments (
    assignment_id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL,
    lesson_id TEXT NOT NULL REFERENCES lessons(lesson_id),
    session_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    status TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    UNIQUE (operator_id, lesson_id)
);
CREATE TABLE IF NOT EXISTS alerts (
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
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_episode_per_rule
    ON alerts(session_id, rule_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS telemetry_events (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    event_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    readings_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (session_id, event_id)
);
CREATE TABLE IF NOT EXISTS announcements (
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
);
CREATE TABLE IF NOT EXISTS deliveries (
    event_id TEXT NOT NULL REFERENCES announcements(event_id),
    consumer_id TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (event_id, consumer_id)
);
