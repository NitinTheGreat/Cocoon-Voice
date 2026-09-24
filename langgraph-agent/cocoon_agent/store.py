"""SQLite persistence owned exclusively by langgraph-agent.

Side effects are made idempotent with database uniqueness constraints keyed on
stable client IDs (turn_id, event_id), not with an in-memory cache.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .api import schemas as s
from .catalog import Catalog, CatalogError
from .incident_time import interpret, offset_timezone
from .migrations import migrate
from .weather import Site

if TYPE_CHECKING:
    from .rules import SafetyPolicy

SEED_TASKS = [
    ("T-101", "Pre-start walkaround inspection", "Check tracks, hydraulic lines and fluid levels before starting the excavator.", "high", 10),
    ("T-102", "Move the spoil pile in bay 3", "Load the spoil from bay 3 and dump it at the north stockpile.", "normal", 20),
    ("T-103", "Grade the access road at gate 2", "Level the ruts on the access road between gate 2 and the site office.", "normal", 30),
]

# Versioned demo text for L1, attached only where the row has no text yet (never overwrites authored content).
LESSON_CONTENT = {
    "L1": ("L1.demo.1", "demo_authored_unreviewed", (
        "Seatbelt and rollover protection basics. "
        "One: the rollover protective structure only protects you if you stay inside it, and the seatbelt is what "
        "keeps you there. "
        "Two: fasten the belt before you start the engine, and keep it fastened while the engine runs, including "
        "while you wait or idle. "
        "Three: never unbuckle to lean out or reach for something while the machine can move. Stop, lower the "
        "attachment and park first. "
        "Four: if the machine starts to tip, stay in the seat, hold on and brace; do not try to jump. "
        "Five: report a damaged or missing belt before operating. "
        "This demo lesson was written for the prototype and has not been reviewed by a trainer."
    )),
}

SEED_LESSONS = [
    ("L1", "Seatbelt and rollover protection basics", "Why the seatbelt and ROPS work together, and when to buckle up.", 5),
    ("L2", "Pre-start walkaround inspection", "A step-by-step walkaround before starting heavy equipment.", 8),
    ("L3", "Responding to a hydraulic leak", "How to stop safely, isolate the machine and report a hydraulic leak.", 6),
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Conflict(Exception):
    """A stable ID was reused with a different payload."""


class NotFound(Exception):
    """The record does not exist in the caller's scope (never reveals whether it exists elsewhere)."""


class VersionConflict(Exception):
    def __init__(self, current_version: int):
        super().__init__(f"stale version; current version is {current_version}")
        self.current_version = current_version


class ConditionsGate(Exception):
    """A task start stopped by the working-conditions check (block, or a finding that needs acknowledgement)."""

    def __init__(self, reason: str, check: s.ConditionCheck | None, task: s.AssignedTask):
        super().__init__(reason)
        self.reason, self.check, self.task = reason, check, task


class InvalidTransition(Exception):
    def __init__(self, current_status: str, message: str, missing: list[str] | None = None):
        super().__init__(message)
        self.current_status = current_status
        self.missing = missing or []


@dataclass(frozen=True)
class OccurrenceTime:
    """How a report's occurrence time was established (see incident_time.py and schemas.OccurredBasis)."""

    occurred_at: datetime | None
    basis: str
    expression: str | None
    reference_at: datetime | None

    @staticmethod
    def of_report(reference: datetime, expression: str | None = None) -> "OccurrenceTime":
        return OccurrenceTime(reference, "time_of_report", expression, reference)


@dataclass(frozen=True)
class NewSessionBinding:
    """What admission established for a new session. Site/shift are None unless a trusted binding matched."""

    dataset_manifest_sha256: str
    context_status: str  # 'unavailable' | 'trusted_binding'
    site_id: str | None = None
    shift_id: str | None = None
    context_source: str | None = None


@dataclass(frozen=True)
class RuleOutcome:
    """One evaluation of a C-family rule for the current sample: held True (condition present), False (observed
    absent) or None (cannot be evaluated: missing, stale or not applicable input; neither opens nor clears).
    `details` is the family-specific evidence saved once when the episode opens."""

    rule_id: str
    family: str
    alert_type: str
    severity: str
    message: str
    reason: str
    recommended_action: str
    start_speech: str
    clear_speech: str | None
    policy_version: str
    source_status: str
    held: bool | None
    explanation: str
    details: dict[str, Any]
    subject_key: str = ""
    priority: str = "high"
    level: str | None = None  # graded episodes: a higher level later is an update of the same episode
    escalate_speech: str | None = None  # announced (once per episode) when the level rises
    clear_reason: str = "observed_clear"
    instant: bool = False  # a one-off event: opened and closed by the same observation
    touch: bool = False  # record this observation as the latest sighting (proximity tracks)
    on_open: Callable[[sqlite3.Connection, str], dict[str, Any]] | None = None  # links created with the episode


LEVEL_RANK = {"warning": 1, "danger": 2, "advisory": 1, "acknowledge": 2, "block": 3}


@dataclass
class TurnRow:
    session_id: str
    turn_id: str
    request_hash: str
    status: str
    attempts: int
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: datetime
    completed_at: datetime | None


class Store:
    def __init__(self, db_path: Path, busy_timeout_ms: int = 5000):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Autocommit mode: every write goes through an explicit BEGIN IMMEDIATE (see _tx and migrations.migrate).
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------------ lifecycle

    def init_schema(self) -> list[str]:
        """Apply pending versioned migrations (see migrations.py). Raises MigrationError; never wipes data."""
        with self._lock:
            return migrate(self._conn)

    def schema_version(self) -> int:
        return self._one("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")[0]

    def register_catalog(self, catalog: Catalog) -> None:
        """Record the verified snapshot once (append-only). A different identity set under the same hash is refused."""
        machines = {(m.machine_id, m.model, m.category) for m in catalog.machines.values()}
        with self._tx() as c:
            row = c.execute("SELECT 1 FROM catalog_versions WHERE manifest_sha256 = ?",
                            (catalog.manifest_sha256,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO catalog_versions(manifest_sha256, manifest_schema_version, dataset_origin,"
                    " generator_seed, machines_sha256, operators_sha256, machine_count, operator_count,"
                    " first_registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (catalog.manifest_sha256, catalog.manifest_schema_version, catalog.dataset_origin,
                     catalog.generator_seed, catalog.file_sha256["machines.csv"], catalog.file_sha256["operators.csv"],
                     len(machines), len(catalog.operators), iso(utcnow())),
                )
                c.executemany("INSERT INTO catalog_version_machines(manifest_sha256, machine_id, model, category)"
                              " VALUES (?, ?, ?, ?)", [(catalog.manifest_sha256, *m) for m in sorted(machines)])
                c.executemany("INSERT INTO catalog_version_operators(manifest_sha256, operator_id) VALUES (?, ?)",
                              [(catalog.manifest_sha256, o) for o in sorted(catalog.operators)])
                return
            stored_m = {tuple(r) for r in c.execute(
                "SELECT machine_id, model, category FROM catalog_version_machines WHERE manifest_sha256 = ?",
                (catalog.manifest_sha256,))}
            stored_o = {r[0] for r in c.execute(
                "SELECT operator_id FROM catalog_version_operators WHERE manifest_sha256 = ?",
                (catalog.manifest_sha256,))}
            if stored_m != machines or stored_o != set(catalog.operators):
                raise CatalogError("catalog_snapshot_conflict",
                                   "stored snapshot for this manifest hash differs from the loaded catalog")

    def seed_demo(self) -> None:
        """Idempotent: re-running never duplicates or overwrites demo rows."""
        with self._tx() as c:
            c.executemany(
                "INSERT OR IGNORE INTO tasks(task_id, title, details, priority, status, sort_order)"
                " VALUES (?, ?, ?, ?, 'pending', ?)",
                SEED_TASKS,
            )
            c.executemany(
                "INSERT OR IGNORE INTO lessons(lesson_id, title, summary, duration_minutes) VALUES (?, ?, ?, ?)",
                SEED_LESSONS,
            )
            has_content = any(r[1] == "content_text" for r in c.execute("PRAGMA table_info(lessons)"))
            for lesson_id, (version, status, text) in (LESSON_CONTENT.items() if has_content else ()):
                c.execute("UPDATE lessons SET version = ?, content_status = ?, content_text = ?"
                          " WHERE lesson_id = ? AND content_text IS NULL", (version, status, text, lesson_id))

    def ping(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1").fetchone()[0] == 1

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _tx(self):
        store = self

        class _Tx:
            def __enter__(self_inner):
                store._lock.acquire()
                store._conn.execute("BEGIN IMMEDIATE")
                return store._conn

            def __exit__(self_inner, exc_type, exc, tb):
                try:
                    store._conn.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    store._lock.release()
                return False

        return _Tx()

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ sessions

    def get_or_create_session(
        self, req: s.SessionCreateRequest, admit: Callable[[s.SessionCreateRequest], "NewSessionBinding"]
    ) -> tuple[s.Session, bool]:
        """Retrieve by client_session_key, else admit and insert, in ONE BEGIN IMMEDIATE transaction.

        An existing key is compared with its stored association first, so pre-upgrade sessions stay retrievable
        even when their IDs are no longer admissible. Only a new key goes through `admit` (catalog and binding
        checks), which raises to abort the transaction with nothing written. The UNIQUE client_session_key and
        the IMMEDIATE lock serialise concurrent creators across connections and processes."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM sessions WHERE client_session_key = ?", (req.client_session_key,)).fetchone()
            if row:
                session = _session(row)
                for field in ("room_name", "participant_identity", "operator_id", "machine_id"):
                    if getattr(session, field) != getattr(req, field):
                        raise Conflict(f"client_session_key already bound to a different {field}")
                for field in ("site_id", "shift_id"):  # omitted = not asserted; supplied must equal the stored value
                    supplied = getattr(req, field)
                    if supplied is not None and supplied != getattr(session, field):
                        raise Conflict(f"client_session_key already bound to a different {field}")
                return session, False
            binding = admit(req)
            session_id = "ses_" + uuid.uuid4().hex
            c.execute(
                "INSERT INTO sessions(session_id, client_session_key, room_name, participant_identity, operator_id,"
                " machine_id, created_at, dataset_manifest_sha256, site_id, shift_id, binding_status,"
                " context_status, context_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'catalog_verified', ?, ?)",
                (session_id, req.client_session_key, req.room_name, req.participant_identity, req.operator_id,
                 req.machine_id, iso(utcnow()), binding.dataset_manifest_sha256, binding.site_id, binding.shift_id,
                 binding.context_status, binding.context_source),
            )
            row = c.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            return _session(row), True

    def get_session(self, session_id: str) -> s.Session | None:
        row = self._one("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        return _session(row) if row else None

    def bump_state_version(self, session_id: str) -> int:
        with self._tx() as c:
            c.execute("UPDATE sessions SET state_version = state_version + 1 WHERE session_id = ?", (session_id,))
            return c.execute("SELECT state_version FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]

    def state_version(self, session_id: str) -> int:
        return self._one("SELECT state_version FROM sessions WHERE session_id = ?", (session_id,))[0]

    # ------------------------------------------------------------------ turns

    def get_turn(self, session_id: str, turn_id: str) -> TurnRow | None:
        row = self._one("SELECT * FROM turns WHERE session_id = ? AND turn_id = ?", (session_id, turn_id))
        return _turn(row) if row else None

    def claim_turn(self, session_id: str, turn_id: str, request_hash: str) -> tuple[TurnRow, bool]:
        """Insert a processing row. Returns (row, inserted). Raises Conflict on a payload mismatch."""
        now = iso(utcnow())
        with self._tx() as c:
            row = c.execute("SELECT * FROM turns WHERE session_id = ? AND turn_id = ?", (session_id, turn_id)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO turns(session_id, turn_id, request_hash, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'processing', ?, ?)",
                    (session_id, turn_id, request_hash, now, now),
                )
                row = c.execute("SELECT * FROM turns WHERE session_id = ? AND turn_id = ?", (session_id, turn_id)).fetchone()
                return _turn(row), True
            if row["request_hash"] != request_hash:
                raise Conflict("turn_id was already used with a different text or source")
            return _turn(row), False

    def restart_turn(self, session_id: str, turn_id: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE turns SET status = 'processing', attempts = attempts + 1, error_json = NULL, updated_at = ?"
                " WHERE session_id = ? AND turn_id = ?",
                (iso(utcnow()), session_id, turn_id),
            )

    def complete_turn(self, session_id: str, turn_id: str, result: dict[str, Any]) -> None:
        now = iso(utcnow())
        with self._tx() as c:
            c.execute(
                "UPDATE turns SET status = 'completed', result_json = ?, updated_at = ?, completed_at = ?"
                " WHERE session_id = ? AND turn_id = ?",
                (json.dumps(result), now, now, session_id, turn_id),
            )

    def save_turn_route(self, session_id: str, turn_id: str, route: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute("UPDATE turns SET route_json = ? WHERE session_id = ? AND turn_id = ? AND route_json IS NULL",
                      (json.dumps(route), session_id, turn_id))

    def turn_route(self, session_id: str, turn_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT route_json FROM turns WHERE session_id = ? AND turn_id = ?", (session_id, turn_id))
        return json.loads(row["route_json"]) if row and row["route_json"] else None

    def fail_turn(self, session_id: str, turn_id: str, error: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE turns SET status = 'failed', error_json = ?, updated_at = ? WHERE session_id = ? AND turn_id = ?",
                (json.dumps(error), iso(utcnow()), session_id, turn_id),
            )

    # ------------------------------------------------------------------ tasks / lessons

    def list_tasks(self) -> list[s.Task]:
        return [_task(r) for r in self._all("SELECT * FROM tasks ORDER BY sort_order")]

    def next_task(self) -> s.Task | None:
        row = self._one("SELECT * FROM tasks WHERE status != 'done' ORDER BY sort_order LIMIT 1")
        return _task(row) if row else None

    def list_lessons(self) -> list[s.Lesson]:
        return [_lesson(r) for r in self._all("SELECT * FROM lessons ORDER BY lesson_id")]

    def get_lesson(self, lesson_id: str) -> s.Lesson | None:
        row = self._one("SELECT * FROM lessons WHERE lesson_id = ?", (lesson_id,))
        return _lesson(row) if row else None

    # ------------------------------------------------------------------ demo site, shifts and assigned tasks

    _SEED_COLUMNS = {
        "sites": "site_id, name, timezone, utc_offset, fixture_version, provenance",
        "site_zones": "site_zone_id, site_id, name, zone_type, outdoor",
        "shifts": "shift_id, site_id, operator_id, machine_id, service_date, start_at, end_at, catalog_manifest_sha256,"
                  " fixture_version",
        "task_assignments": "task_id, shift_id, operator_id, machine_id, site_zone_id, scheduled_order,"
                            " scheduled_start_at, task_type, title, details, work_quantity, work_unit, weather_json,"
                            " duration_minutes, duration_source, updated_at, provenance",
    }
    # Columns compared when a seeded row already exists (lifecycle/updated_at may legitimately have changed).
    _SEED_COMPARE = {"sites": 4, "site_zones": 5, "shifts": 8, "task_assignments": 15}

    def seed_demo_site(self, rows: dict[str, list[tuple[str, tuple]]]) -> dict[str, Any]:
        """Insert missing rows; keep identical existing rows; refuse (whole seed rolls back) on differing rows."""
        report: dict[str, Any] = {"inserted": {}, "reused": {}}
        with self._tx() as c:
            for table in ("sites", "site_zones", "shifts", "task_assignments"):
                cols = self._SEED_COLUMNS[table]
                n = self._SEED_COMPARE[table]
                inserted = reused = 0
                for key_col, values in rows.get(table, []):
                    existing = c.execute(f"SELECT {cols} FROM {table} WHERE {key_col} = ?", (values[0],)).fetchone()
                    if existing is None:
                        marks = ", ".join("?" * len(values))
                        c.execute(f"INSERT INTO {table}({cols}) VALUES ({marks})", values)
                        inserted += 1
                    elif tuple(existing)[:n] != tuple(values)[:n]:
                        raise Conflict(f"{table} row {values[0]} already exists with different content")
                    else:
                        reused += 1
                report["inserted"][table], report["reused"][table] = inserted, reused
        return report

    def get_shift(self, shift_id: str) -> s.ShiftInfo | None:
        row = self._one("SELECT sh.*, si.name AS site_name, si.timezone, si.utc_offset FROM shifts sh"
                        " JOIN sites si USING (site_id)"
                        " WHERE sh.shift_id = ?", (shift_id,))
        return _shift(row) if row else None

    def list_assigned_tasks(self, shift_id: str) -> list[s.AssignedTask]:
        rows = self._all(_TASK_SQL + " WHERE t.shift_id = ? ORDER BY t.scheduled_order", (shift_id,))
        return [_assigned_task(r) for r in rows]

    def get_command(self, scope: str, command_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM command_log WHERE scope = ? AND command_id = ?", (scope, command_id))
        return dict(row) if row else None

    def commands_for_turn(self, session_id: str, turn_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT * FROM command_log WHERE session_id = ? AND turn_id = ?"
                                           " ORDER BY state_version, command_id", (session_id, turn_id))]

    def action_records(self, session_id: str, turn_id: str) -> list[s.ActionRecord]:
        return [s.ActionRecord(action_id=r["command_id"], kind=r["kind"], outcome=r["outcome"],
                               record_type=r["record_type"], record_id=r["record_id"], summary=r["summary"],
                               state_version=r["state_version"], created_at=parse_dt(r["created_at"]))
                for r in self.commands_for_turn(session_id, turn_id)]

    def run_command(self, *, scope: str, command_id: str, kind: str, fingerprint: str, session_id: str,
                    turn_id: str | None, mutate: Callable[[sqlite3.Connection], dict[str, Any]]) -> tuple[dict, bool]:
        """Execute one domain write exactly once per (scope, command_id), in one short transaction.

        The mutation and its command_log row commit together. An identical retry returns the saved result
        (duplicate=True); a different payload under the same id raises Conflict. `mutate` raises NotFound /
        VersionConflict / InvalidTransition to reject without side effects."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM command_log WHERE scope = ? AND command_id = ?", (scope, command_id)).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    raise Conflict("command_id was already used with a different payload")
                return json.loads(row["result_json"]), True
            outcome = mutate(c)
            c.execute("UPDATE sessions SET state_version = state_version + 1 WHERE session_id = ?", (session_id,))
            version = c.execute("SELECT state_version FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]
            result = {**outcome, "command_id": command_id, "kind": kind, "state_version": version,
                      "created_at": iso(utcnow())}
            c.execute("INSERT INTO command_log(scope, command_id, kind, fingerprint, session_id, turn_id, outcome,"
                      " record_type, record_id, summary, state_version, result_json, created_at)"
                      " VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)",
                      (scope, command_id, kind, fingerprint, session_id, turn_id, outcome.get("record_type"),
                       outcome.get("record_id"), outcome["summary"], version, json.dumps(result, default=str),
                       result["created_at"]))
            return result, False

    @staticmethod
    def save_condition_check(c: sqlite3.Connection, session: s.Session, check: s.ConditionCheck, purpose: str) -> str:
        """Persist a check and the exact weather values it used (the snapshot row is written once)."""
        check_id = "CHK-" + uuid.uuid4().hex[:12]
        check = check.model_copy(update={"check_id": check_id})
        record_id = None
        if check.weather is not None:
            record_id = check.weather.record_id
            c.execute("INSERT OR IGNORE INTO weather_records(record_id, site_id, provider, kind, record_json,"
                      " retrieved_at) VALUES (?, ?, ?, ?, ?, ?)",
                      (record_id, check.weather.site_id, check.weather.provider, check.weather.kind,
                       check.weather.model_dump_json(), iso(check.weather.retrieved_at)))
        c.execute("INSERT INTO condition_checks(check_id, session_id, task_id, purpose, level, coverage, check_json,"
                  " weather_record_id, policy_version, data_time, acknowledged, created_at)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (check_id, session.session_id, check.task_id, purpose, check.level, check.coverage,
                   check.model_dump_json(), record_id, check.policy_version, iso(check.data_time),
                   int(check.acknowledged), iso(utcnow())))
        return check_id

    def get_site(self, site_id: str | None) -> "Site | None":
        if not site_id:
            return None
        row = self._one("SELECT site_id, utc_offset, latitude, longitude FROM sites WHERE site_id = ?", (site_id,))
        return Site(row["site_id"], row["utc_offset"], row["latitude"], row["longitude"]) if row else None

    def located_sites(self) -> "list[Site]":
        return [Site(r["site_id"], r["utc_offset"], r["latitude"], r["longitude"])
                for r in self._all("SELECT * FROM sites WHERE latitude IS NOT NULL AND longitude IS NOT NULL")]

    def set_site_location(self, site_id: str, latitude: float, longitude: float, basis: str) -> bool:
        """Fill the trusted coordinates once (never overwrites a different stored location)."""
        with self._tx() as c:
            row = c.execute("SELECT latitude, longitude FROM sites WHERE site_id = ?", (site_id,)).fetchone()
            if row is None:
                return False
            if row["latitude"] is None:
                c.execute("UPDATE sites SET latitude = ?, longitude = ?, location_basis = ? WHERE site_id = ?",
                          (latitude, longitude, basis, site_id))
                return True
            if (row["latitude"], row["longitude"]) != (latitude, longitude):
                raise Conflict(f"site {site_id} already has a different trusted location")
            return False

    def in_progress_task(self, shift_id: str | None) -> s.AssignedTask | None:
        if not shift_id:
            return None
        row = self._one(_TASK_SQL + " WHERE t.shift_id = ? AND t.status = 'in_progress' ORDER BY t.scheduled_order"
                        " LIMIT 1", (shift_id,))
        return _assigned_task(row) if row else None

    def data_clock(self, session_id: str) -> datetime | None:
        """The session's data time: the newest applied observation (None before any telemetry)."""
        row = self._one("SELECT observed_at FROM machine_state WHERE session_id = ?", (session_id,))
        return parse_dt(row["observed_at"]) if row else None

    @staticmethod
    def task_transition(session: s.Session, kind: str, task_id: str | None, expected_version: int | None,
                        check_for: "Callable[[s.AssignedTask], tuple[s.ConditionCheck | None, str]] | None" = None,
                        acknowledged: bool = False) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        """Mutation for task.start / task.complete, scoped to the session's trusted shift.

        task.start runs the working-conditions gate on the task actually selected (`check_for` returns the check and
        proceed/acknowledge/block): a block refuses, an unacknowledged finding refuses (ConditionsGate), otherwise the
        check is saved with the start in this transaction and never rewritten later."""
        needed = {"task.start": "scheduled", "task.complete": "in_progress"}[kind]

        def mutate(c: sqlite3.Connection) -> dict[str, Any]:
            if not session.shift_id:
                raise NotFound("this session has no assigned shift")
            if task_id:
                row = c.execute(_TASK_SQL + " WHERE t.task_id = ? AND t.shift_id = ?",
                                (task_id, session.shift_id)).fetchone()
            else:  # voice: "start the next task" / "I've finished"
                row = c.execute(_TASK_SQL + " WHERE t.shift_id = ? AND t.status = ? ORDER BY t.scheduled_order LIMIT 1",
                                (session.shift_id, needed)).fetchone()
            if row is None or row["operator_id"] != session.operator_id or row["machine_id"] != session.machine_id:
                raise NotFound("no such task in this session's shift" if task_id else f"no {needed} task in this shift")
            if expected_version is not None and row["version"] != expected_version:
                raise VersionConflict(row["version"])
            if row["status"] != needed:
                raise InvalidTransition(row["status"], f"task is {row['status']}; {kind} needs a {needed} task")
            now = iso(utcnow())
            check_id = None
            if kind == "task.start" and check_for is not None:
                check, gate = check_for(_assigned_task(row))
                if gate == "block" or (gate == "acknowledge" and not acknowledged):
                    raise ConditionsGate("conditions_block" if gate == "block" else "conditions_need_acknowledgement",
                                         check, _assigned_task(row))
                if check is not None:
                    check_id = Store.save_condition_check(
                        c, session, check.model_copy(update={"acknowledged": gate == "acknowledge"}), "task_start")
            if kind == "task.start":
                c.execute("UPDATE task_assignments SET status = 'in_progress', version = version + 1, started_at = ?,"
                          " updated_at = ?, start_check_id = ? WHERE task_id = ?", (now, now, check_id, row["task_id"]))
            else:
                c.execute("UPDATE task_assignments SET status = 'completed', version = version + 1, completed_at = ?,"
                          " updated_at = ? WHERE task_id = ?", (now, now, row["task_id"]))
            task = _assigned_task(c.execute(_TASK_SQL + " WHERE t.task_id = ?", (row["task_id"],)).fetchone())
            verb = "Started" if kind == "task.start" else "Completed"
            out = {"record_type": "task", "record_id": task.task_id, "summary": f"{verb} {task.title}.",
                   "task": task.model_dump(mode="json")}
            if task.start_check is not None and kind == "task.start":
                out["conditions"] = task.start_check.model_dump(mode="json")
            return out

        return mutate

    # ------------------------------------------------------------------ incidents

    def create_incident(self, session: s.Session, description: str, source_turn_id: str) -> tuple[s.Incident, bool]:
        with self._tx() as c:
            existing = c.execute(
                "SELECT * FROM incidents WHERE session_id = ? AND source_turn_id = ?", (session.session_id, source_turn_id)
            ).fetchone()
            if existing:
                return _incident(existing), False
            cur = c.execute(
                "INSERT INTO incidents(session_id, operator_id, machine_id, description, source_turn_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session.session_id, session.operator_id, session.machine_id, description, source_turn_id, iso(utcnow())),
            )
            number = cur.lastrowid
            c.execute("UPDATE incidents SET incident_id = ? WHERE incident_number = ?", (f"INC-{number:04d}", number))
            row = c.execute("SELECT * FROM incidents WHERE incident_number = ?", (number,)).fetchone()
            return _incident(row), True

    def list_incidents(self, session_id: str) -> list[s.Incident]:
        rows = self._all("SELECT * FROM incidents WHERE session_id = ? ORDER BY incident_number", (session_id,))
        return [_incident(r) for r in rows]

    def list_drafts(self, session_id: str, status: str = "draft") -> list[s.IncidentDraft]:
        rows = self._all("SELECT * FROM incident_drafts WHERE session_id = ? AND status = ? ORDER BY draft_number",
                         (session_id, status))
        return [_draft(r) for r in rows]

    def list_pending_approvals(self, session_id: str) -> list[s.ApprovalRequest]:
        rows = self._all("SELECT * FROM approval_requests WHERE session_id = ? AND status = 'pending'"
                         " ORDER BY created_at", (session_id,))
        return [_approval(r) for r in rows]

    @staticmethod
    def incident_report(session: s.Session, turn_id: str, description: str, severity: str | None,
                        location_text: str | None, *, severity_basis: str | None = None,
                        when: OccurrenceTime | None = None) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        """Mutation for an operator's complete report: a new confirmed incident per turn (matching text never merges
        two intentional reports). Identity comes from the session; "where" is the stated place, else the active
        task's zone, each with its basis; "when" is the interpreted phrase, else the (labelled) time of the report;
        severity only as stated (a level, or explicitly unknown)."""

        def mutate(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute("SELECT * FROM incidents WHERE session_id = ? AND source_turn_id = ?",
                            (session.session_id, turn_id)).fetchone()
            reused = row is not None  # written before the command log existed (pre-v5 retry)
            if row is None:
                now = utcnow()
                t = when or OccurrenceTime.of_report(now)
                zone, zone_basis = _resolve_zone(c, session, location_text)
                basis = severity_basis or ("reported" if severity else None)
                number = c.execute(
                    "INSERT INTO incidents(session_id, operator_id, machine_id, description, source_turn_id, created_at,"
                    " origin, severity, severity_basis, site_id, site_zone_id, zone_basis, location_text,"
                    " occurred_at, occurred_basis, occurred_expression, occurred_reference_at, confirmed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'operator_reported', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session.session_id, session.operator_id, session.machine_id, description, turn_id, iso(now),
                     severity, basis, session.site_id, zone, zone_basis, location_text, iso(t.occurred_at), t.basis,
                     t.expression, iso(t.reference_at), iso(now)),
                ).lastrowid
                c.execute("UPDATE incidents SET incident_id = ? WHERE incident_number = ?", (f"INC-{number:04d}", number))
                row = c.execute("SELECT * FROM incidents WHERE incident_number = ?", (number,)).fetchone()
            inc = _incident(row)
            return {"record_type": "incident", "record_id": inc.incident_id, "reused": reused,
                    "summary": f"Logged incident {inc.incident_number}.", "incident": inc.model_dump(mode="json")}

        return mutate

    @staticmethod
    def operator_draft(session: s.Session, turn_id: str, description: str, severity: str | None,
                       severity_basis: str | None, location_text: str | None, when: OccurrenceTime,
                       notify_supervisor: bool) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        """Mutation for an operator's report that still misses a fact: it is saved as a draft (own DRF numbering, no
        incident ID yet) so nothing is lost while the operator is asked. One draft per reporting turn."""

        def mutate(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute("SELECT * FROM incident_drafts WHERE session_id = ? AND source_turn_id = ?",
                            (session.session_id, turn_id)).fetchone()
            if row is None:
                zone, zone_basis = _resolve_zone(c, session, location_text)
                number = c.execute(
                    "INSERT INTO incident_drafts(session_id, operator_id, machine_id, origin, description, severity,"
                    " severity_basis, site_id, site_zone_id, zone_basis, location_text, occurred_at, occurred_basis,"
                    " occurred_expression, occurred_reference_at, source_turn_id, notify_supervisor, created_at)"
                    " VALUES (?, ?, ?, 'operator_report', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session.session_id, session.operator_id, session.machine_id, description, severity,
                     severity_basis, session.site_id, zone, zone_basis, location_text, iso(when.occurred_at),
                     when.basis, when.expression, iso(when.reference_at), turn_id, int(notify_supervisor),
                     iso(utcnow())),
                ).lastrowid
                c.execute("UPDATE incident_drafts SET draft_id = ? WHERE draft_number = ?", (f"DRF-{number:04d}", number))
                row = c.execute("SELECT * FROM incident_drafts WHERE draft_number = ?", (number,)).fetchone()
            draft = _draft(row)
            return {"record_type": "incident_draft", "record_id": draft.draft_id,
                    "summary": f"Saved report as draft {draft.draft_number} (missing: {', '.join(draft.missing)}).",
                    "draft": draft.model_dump(mode="json")}

        return mutate

    @staticmethod
    def insert_auto_draft(c: sqlite3.Connection, session: s.Session, episode_id: str, description: str,
                          severity: str, occurred_at: datetime) -> str:
        """An automatic draft for one alert episode (at most one per episode). Not an incident until confirmed."""
        row = c.execute("SELECT draft_id FROM incident_drafts WHERE episode_id = ?", (episode_id,)).fetchone()
        if row is not None:
            return row["draft_id"]
        zone, zone_basis = _resolve_zone(c, session, None)
        number = c.execute(
            "INSERT INTO incident_drafts(session_id, operator_id, machine_id, origin, description, severity,"
            " severity_basis, site_id, site_zone_id, zone_basis, occurred_at, occurred_basis, episode_id, created_at)"
            " VALUES (?, ?, ?, 'auto_draft', ?, ?, 'rule_default', ?, ?, ?, ?, 'observation_time', ?, ?)",
            (session.session_id, session.operator_id, session.machine_id, description, severity, session.site_id,
             zone, zone_basis, iso(occurred_at), episode_id, iso(utcnow())),
        ).lastrowid
        draft_id = f"DRF-{number:04d}"
        c.execute("UPDATE incident_drafts SET draft_id = ? WHERE draft_number = ?", (draft_id, number))
        return draft_id

    @staticmethod
    def incident_transition(session: s.Session, kind: str, draft_id: str, expected_version: int | None,
                            edits: dict[str, Any] | None = None) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        """Mutation for incident.edit / incident.confirm / incident.dismiss on this session's open drafts. Confirming
        allocates the incident (and its real ID) exactly once and needs every fact stated (severity as a level or
        explicitly unknown, a usable occurrence time); a confirmed or dismissed draft is final. A draft saved with
        `notify_supervisor` gets its pending supervisor-review request in the same transaction as the confirmation.

        Edits: description, severity, severity_unknown, location_text, and the occurrence time as `when` (already
        interpreted by the graph), `occurred_expression` (interpreted here against this first execution's time and
        the trusted site offset) or an exact `occurred_at`."""

        def mutate(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute("SELECT * FROM incident_drafts WHERE draft_id = ? AND session_id = ?",
                            (draft_id, session.session_id)).fetchone()
            if row is None:
                raise NotFound("no such incident draft in this session")
            if expected_version is not None and row["version"] != expected_version:
                raise VersionConflict(row["version"])
            if row["status"] != "draft":
                raise InvalidTransition(row["status"], f"draft is {row['status']}; {kind} needs an open draft")
            now = iso(utcnow())
            incident = approval = None
            if kind == "incident.confirm":
                missing = _draft_missing(row)
                if missing:
                    raise InvalidTransition("draft", f"the draft still needs: {', '.join(missing)}", missing)
                number = c.execute(
                    "INSERT INTO incidents(session_id, operator_id, machine_id, description, source_turn_id, created_at,"
                    " origin, severity, severity_basis, site_id, site_zone_id, zone_basis, location_text, occurred_at,"
                    " occurred_basis, occurred_expression, occurred_reference_at, episode_id, draft_id, confirmed_at)"
                    " SELECT session_id, operator_id, machine_id, description, 'draft:' || draft_id, ?,"
                    " CASE origin WHEN 'operator_report' THEN 'operator_reported' ELSE origin END,"
                    " severity, severity_basis, site_id, site_zone_id, zone_basis, location_text, occurred_at,"
                    " occurred_basis, occurred_expression, occurred_reference_at, episode_id, draft_id, ?"
                    " FROM incident_drafts WHERE draft_id = ?",
                    (now, now, draft_id)).lastrowid
                incident_id = f"INC-{number:04d}"
                c.execute("UPDATE incidents SET incident_id = ? WHERE incident_number = ?", (incident_id, number))
                c.execute("UPDATE incident_drafts SET status = 'confirmed', incident_id = ?, confirmed_at = ?,"
                          " version = version + 1 WHERE draft_id = ?", (incident_id, now, draft_id))
                incident = _incident(c.execute("SELECT * FROM incidents WHERE incident_number = ?",
                                               (number,)).fetchone())
                summary = f"Confirmed draft {row['draft_number']} as incident {number}."
                if row["notify_supervisor"]:
                    approval = Store.escalation_request(session, incident_id)(c)["approval"]
                    summary += " Supervisor review requested (pending)."
            elif kind == "incident.dismiss":
                c.execute("UPDATE incident_drafts SET status = 'dismissed', dismissed_at = ?, version = version + 1"
                          " WHERE draft_id = ?", (now, draft_id))
                summary = f"Dismissed draft {row['draft_number']}."
            else:
                sets, args = _draft_edits(c, session, edits or {})
                if not sets:
                    raise InvalidTransition(row["status"], "incident.edit needs at least one field to change")
                c.execute(f"UPDATE incident_drafts SET {', '.join(sets)}, version = version + 1 WHERE draft_id = ?",
                          (*args, draft_id))
                summary = f"Edited draft {row['draft_number']}."
            draft = _draft(c.execute("SELECT * FROM incident_drafts WHERE draft_id = ?", (draft_id,)).fetchone())
            out = {"record_type": "incident" if incident else "incident_draft",
                   "record_id": incident.incident_id if incident else draft_id, "summary": summary,
                   "draft": draft.model_dump(mode="json")}
            if incident is not None:
                out["incident"] = incident.model_dump(mode="json")
            if approval is not None:
                out["approval"] = approval
            return out

        return mutate

    @staticmethod
    def escalation_request(session: s.Session, incident_id: str) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        """A pending supervisor-review request linked to one incident (one per incident). Not a notification."""

        def mutate(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute("SELECT * FROM approval_requests WHERE kind = 'incident_escalation' AND incident_id = ?",
                            (incident_id,)).fetchone()
            if row is None:
                approval_id = "APR-" + uuid.uuid4().hex[:12]
                c.execute("INSERT INTO approval_requests(approval_id, session_id, operator_id, kind, incident_id,"
                          " created_at) VALUES (?, ?, ?, 'incident_escalation', ?, ?)",
                          (approval_id, session.session_id, session.operator_id, incident_id, iso(utcnow())))
                row = c.execute("SELECT * FROM approval_requests WHERE approval_id = ?", (approval_id,)).fetchone()
            approval = _approval(row)
            return {"record_type": "approval_request", "record_id": approval.approval_id,
                    "summary": f"Requested supervisor review of {incident_id} (pending).",
                    "approval": approval.model_dump(mode="json")}

        return mutate

    # ------------------------------------------------------------------ training

    # Training assignments are keyed by operator_id, a string that a pre-upgrade (legacy) session may also have used.
    # An assignment is visible only when its owning session has the SAME binding class as the reading session:
    # catalog_verified sessions see records of catalog_verified sessions of the same operator, and legacy sessions
    # see only legacy records (the pre-I02b behaviour among legacy sessions). A free-text name match therefore
    # never merges records across the two classes in either direction.
    _SAME_CLASS_OWNER = (" AND ta.session_id IN (SELECT session_id FROM sessions"
                         " WHERE binding_status = ? AND operator_id = ta.operator_id)")

    def assign_training(self, session: s.Session, lesson_id: str,
                        source_turn_id: str) -> tuple[s.TrainingAssignment | None, bool]:
        """Returns (assignment, created). (None, False) when a record of the other binding class already holds this
        operator/lesson pair: it is withheld, not handed to the caller."""
        with self._tx() as c:
            existing = c.execute(
                "SELECT * FROM training_assignments WHERE operator_id = ? AND lesson_id = ?", (session.operator_id, lesson_id)
            ).fetchone()
            if existing is not None:
                owner = c.execute("SELECT binding_status FROM sessions WHERE session_id = ?",
                                  (existing["session_id"],)).fetchone()
                if owner is None or owner["binding_status"] != session.binding_status:
                    return None, False
            if existing is None:
                c.execute(
                    "INSERT INTO training_assignments(assignment_id, operator_id, lesson_id, session_id, source_turn_id,"
                    " status, assigned_at) VALUES (?, ?, ?, ?, ?, 'assigned', ?)",
                    ("TA-" + uuid.uuid4().hex[:10], session.operator_id, lesson_id, session.session_id, source_turn_id,
                     iso(utcnow())),
                )
            row = c.execute(
                "SELECT ta.*, l.title AS lesson_title FROM training_assignments ta JOIN lessons l USING (lesson_id)"
                " WHERE ta.operator_id = ? AND ta.lesson_id = ?", (session.operator_id, lesson_id)
            ).fetchone()
            created = existing is None or existing["source_turn_id"] == source_turn_id
            return _assignment(row), created

    @staticmethod
    def _episode_assignment(c: sqlite3.Connection, session: s.Session, lesson_id: str, episode_id: str) -> str | None:
        """Assign the policy's lesson for a qualifying episode, once while it is outstanding: an existing assignment of
        the same lesson (same binding class) is linked instead of creating another. A record held by the other binding
        class is never linked or exposed (returns None)."""
        if c.execute("SELECT 1 FROM lessons WHERE lesson_id = ?", (lesson_id,)).fetchone() is None:
            return None
        row = c.execute("SELECT ta.assignment_id, s.binding_status FROM training_assignments ta"
                        " LEFT JOIN sessions s ON s.session_id = ta.session_id"
                        " WHERE ta.operator_id = ? AND ta.lesson_id = ?", (session.operator_id, lesson_id)).fetchone()
        if row is not None:
            return row["assignment_id"] if row["binding_status"] == session.binding_status else None
        assignment_id = "TA-" + uuid.uuid4().hex[:10]
        c.execute("INSERT INTO training_assignments(assignment_id, operator_id, lesson_id, session_id, source_turn_id,"
                  " status, assigned_at, source_episode_id) VALUES (?, ?, ?, ?, ?, 'assigned', ?, ?)",
                  (assignment_id, session.operator_id, lesson_id, session.session_id, f"episode:{episode_id}",
                   iso(utcnow()), episode_id))
        return assignment_id

    def list_assignments(self, session: s.Session) -> list[s.TrainingAssignment]:
        rows = self._all(
            "SELECT ta.*, l.title AS lesson_title FROM training_assignments ta JOIN lessons l USING (lesson_id)"
            f" WHERE ta.operator_id = ?{self._SAME_CLASS_OWNER} ORDER BY ta.assigned_at",
            (session.operator_id, session.binding_status),
        )
        return [_assignment(r) for r in rows]

    # ------------------------------------------------------------------ principals and actor tokens

    def issue_actor_token(self, *, kind: str, principal_id: str, operator_id: str | None,
                          operator_catalog_sha256: str | None, display_name: str | None, token_sha256: str,
                          scopes: tuple[str, ...], issued_at: str, expires_at: str) -> dict[str, Any]:
        """Create the principal if new (never widen or rebind an existing one) and store one token digest."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM principals WHERE principal_id = ?", (principal_id,)).fetchone()
            if row is None:
                if kind == "operator" and c.execute(
                        "SELECT 1 FROM principals WHERE kind = 'operator' AND operator_id = ?", (operator_id,)).fetchone():
                    raise Conflict("this operator already has a principal under a different principal_id")
                c.execute("INSERT INTO principals(principal_id, kind, operator_id, operator_catalog_sha256,"
                          " display_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (principal_id, kind, operator_id, operator_catalog_sha256, display_name, issued_at))
            elif row["kind"] != kind or row["operator_id"] != operator_id:
                raise Conflict("principal_id already exists with a different role or operator")
            token_id = "tok_" + uuid.uuid4().hex[:16]
            c.execute("INSERT INTO actor_tokens(token_id, principal_id, token_sha256, scopes, issued_at, expires_at)"
                      " VALUES (?, ?, ?, ?, ?, ?)",
                      (token_id, principal_id, token_sha256, " ".join(scopes), issued_at, expires_at))
            return _token_meta(c.execute(_TOKEN_META_SQL + " WHERE t.token_id = ?", (token_id,)).fetchone())

    def resolve_actor_token(self, token_sha256: str) -> dict[str, Any] | None:
        """Metadata for a stored digest (including revoked/expired ones; the caller decides)."""
        row = self._one(_TOKEN_META_SQL + " WHERE t.token_sha256 = ?", (token_sha256,))
        return _token_meta(row) if row else None

    def get_actor_token(self, token_id: str) -> dict[str, Any] | None:
        row = self._one(_TOKEN_META_SQL + " WHERE t.token_id = ?", (token_id,))
        return _token_meta(row) if row else None

    def list_actor_tokens(self, principal_id: str | None = None) -> list[dict[str, Any]]:
        if principal_id is None:
            rows = self._all(_TOKEN_META_SQL + " ORDER BY t.issued_at")
        else:
            rows = self._all(_TOKEN_META_SQL + " WHERE t.principal_id = ? ORDER BY t.issued_at", (principal_id,))
        return [_token_meta(r) for r in rows]

    def revoke_actor_token(self, token_id: str, revoked_at: str,
                           reason: str | None) -> tuple[dict[str, Any] | None, bool]:
        """Idempotent. Returns (metadata, changed); (None, False) for an unknown token_id."""
        with self._tx() as c:
            row = c.execute("SELECT revoked_at FROM actor_tokens WHERE token_id = ?", (token_id,)).fetchone()
            if row is None:
                return None, False
            changed = row["revoked_at"] is None
            if changed:
                c.execute("UPDATE actor_tokens SET revoked_at = ?, revoke_reason = ? WHERE token_id = ?",
                          (revoked_at, reason, token_id))
            return _token_meta(c.execute(_TOKEN_META_SQL + " WHERE t.token_id = ?", (token_id,)).fetchone()), changed

    def owned_verified_sessions(self, operator_id: str) -> list[s.Session]:
        rows = self._all("SELECT * FROM sessions WHERE binding_status = 'catalog_verified' AND operator_id = ?"
                         " ORDER BY created_at", (operator_id,))
        return [_session(r) for r in rows]

    # ------------------------------------------------------------------ alerts

    def active_alerts(self, session_id: str) -> list[s.Alert]:
        rows = self._all("SELECT * FROM alerts WHERE session_id = ? AND status = 'active' ORDER BY started_at", (session_id,))
        return self._with_updates([_alert(r) for r in rows])

    def latest_alert(self, session_id: str) -> s.Alert | None:
        row = self._one(
            "SELECT * FROM alerts WHERE session_id = ? ORDER BY (status = 'active') DESC, started_at DESC LIMIT 1",
            (session_id,),
        )
        return self._with_updates([_alert(row)])[0] if row else None

    def _with_updates(self, alerts: list[s.Alert]) -> list[s.Alert]:
        """Attach each episode's later updates (level changes with their own evidence)."""
        if not alerts:
            return alerts
        marks = ", ".join("?" * len(alerts))
        rows = self._all(f"SELECT * FROM alert_updates WHERE alert_id IN ({marks}) ORDER BY observed_at",
                         tuple(a.alert_id for a in alerts))
        by_alert: dict[str, list[s.AlertUpdate]] = {}
        for r in rows:
            by_alert.setdefault(r["alert_id"], []).append(s.AlertUpdate(
                update_id=r["update_id"], level=r["level"], previous_level=r["previous_level"],
                observed_at=parse_dt(r["observed_at"]), event_id=r["event_id"], details=json.loads(r["details_json"]),
                announcement_event_id=r["announcement_event_id"]))
        return [a.model_copy(update={"updates": by_alert.get(a.alert_id, [])}) for a in alerts]

    def rule_coverage(self, session_id: str) -> list[s.RuleCoverage]:
        row = self._one("SELECT rule_state_json FROM machine_state WHERE session_id = ?", (session_id,))
        if row is None or not row["rule_state_json"]:
            return []
        return [s.RuleCoverage(**c) for c in json.loads(row["rule_state_json"]).get("coverage", [])]

    def announced_alerts(self, session_id: str) -> list[tuple[s.Alert, str, datetime]]:
        """Alerts that were announced, with the announcement's event_id and time, newest announcement first."""
        rows = self._all("SELECT a.*, n.event_id AS ann_event_id, n.created_at AS ann_created_at FROM alerts a"
                         " JOIN announcements n ON n.alert_id = a.alert_id AND n.type = 'alert_started'"
                         " WHERE a.session_id = ? ORDER BY n.sequence DESC", (session_id,))
        alerts = self._with_updates([_alert(r) for r in rows])
        return [(a, r["ann_event_id"], parse_dt(r["ann_created_at"])) for a, r in zip(alerts, rows)]

    def linked_alerts(self, session_id: str) -> list[tuple[s.Alert, str, datetime]]:
        """Correlated (combined-condition) episodes that were not announced themselves, paired with the announcement
        of the parent episode that covered them, newest first."""
        rows = self._all("SELECT a.*, n.event_id AS ann_event_id, n.created_at AS ann_created_at FROM alerts a"
                         " JOIN announcements n ON n.alert_id = a.correlated_alert_id AND n.type = 'alert_started'"
                         " WHERE a.session_id = ? AND a.announced = 0 ORDER BY a.started_at DESC", (session_id,))
        return [(_alert(r), r["ann_event_id"], parse_dt(r["ann_created_at"])) for r in rows]

    def related_alerts(self, alert_id: str) -> list[s.Alert]:
        """Episodes linked to this one: its correlated children, and the parent if it is itself a child."""
        rows = self._all("SELECT * FROM alerts WHERE correlated_alert_id = ? OR alert_id ="
                         " (SELECT correlated_alert_id FROM alerts WHERE alert_id = ?) ORDER BY started_at",
                         (alert_id, alert_id))
        return [_alert(r) for r in rows if r["alert_id"] != alert_id]

    def get_draft(self, session_id: str, draft_id: str) -> s.IncidentDraft | None:
        row = self._one("SELECT * FROM incident_drafts WHERE session_id = ? AND draft_id = ?", (session_id, draft_id))
        return _draft(row) if row else None

    def site_offset(self, session: s.Session) -> timezone | None:
        with self._lock:
            return session_offset(self._conn, session)

    def deliveries(self, event_id: str) -> list[s.DeliveryRecord]:
        return [_delivery(d) for d in self._all("SELECT * FROM deliveries WHERE event_id = ? ORDER BY consumer_id",
                                                (event_id,))]

    def previous_turn_at(self, session_id: str, turn_id: str) -> datetime | None:
        row = self._one("SELECT MAX(created_at) FROM turns WHERE session_id = ? AND turn_id != ? AND created_at <="
                        " (SELECT created_at FROM turns WHERE session_id = ? AND turn_id = ?)",
                        (session_id, turn_id, session_id, turn_id))
        return parse_dt(row[0]) if row and row[0] else None

    @staticmethod
    def idle_reason(session: s.Session, turn_id: str, reason_text: str) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        """Record the operator's reason against the active idle episode (if any). Never clears an episode."""

        def mutate(c: sqlite3.Connection) -> dict[str, Any]:
            idle = c.execute("SELECT alert_id FROM alerts WHERE session_id = ? AND status = 'active' AND alert_type IN"
                             " ('prolonged_idle', 'idle_unbelted') ORDER BY alert_type = 'prolonged_idle' DESC,"
                             " started_at DESC LIMIT 1", (session.session_id,)).fetchone()
            reason_id = "IDR-" + uuid.uuid4().hex[:12]
            now = iso(utcnow())
            c.execute("INSERT INTO idle_reasons(reason_id, session_id, operator_id, alert_id, reason_text,"
                      " source_turn_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (reason_id, session.session_id, session.operator_id, idle["alert_id"] if idle else None,
                       reason_text, turn_id, now))
            belt = c.execute("SELECT 1 FROM alerts WHERE session_id = ? AND status = 'active'"
                             " AND alert_type = 'seatbelt_unfastened'", (session.session_id,)).fetchone()
            return {"record_type": "idle_reason", "record_id": reason_id,
                    "summary": f"Recorded idle reason: {reason_text[:80]}.", "reason_id": reason_id,
                    "reason_text": reason_text, "alert_id": idle["alert_id"] if idle else None,
                    "belt_warning_active": belt is not None}

        return mutate

    def list_idle_reasons(self, session_id: str) -> list[s.IdleReason]:
        rows = self._all("SELECT * FROM idle_reasons WHERE session_id = ? ORDER BY created_at", (session_id,))
        return [s.IdleReason(reason_id=r["reason_id"], reason_text=r["reason_text"], alert_id=r["alert_id"],
                             created_at=parse_dt(r["created_at"])) for r in rows]

    def ensure_shift_briefing(self, session: s.Session, speech: str, ttl: timedelta) -> tuple[s.ShiftBriefing, bool]:
        """One briefing per shift: the first bound session publishes it; later sessions and restarts reuse it."""
        now = utcnow()
        with self._tx() as c:
            row = c.execute("SELECT * FROM shift_briefings WHERE shift_id = ?", (session.shift_id,)).fetchone()
            created = row is None
            if created:
                seq = c.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM announcements WHERE session_id = ?",
                                (session.session_id,)).fetchone()[0]
                event_id = f"ann_briefing_{session.shift_id}"
                c.execute("INSERT INTO announcements(event_id, session_id, sequence, type, priority, speech, alert_id,"
                          " created_at, expires_at) VALUES (?, ?, ?, 'shift_briefing', 'normal', ?, NULL, ?, ?)",
                          (event_id, session.session_id, seq, speech, iso(now), iso(now + ttl)))
                c.execute("INSERT INTO shift_briefings(shift_id, session_id, event_id, speech, created_at)"
                          " VALUES (?, ?, ?, ?, ?)", (session.shift_id, session.session_id, event_id, speech, iso(now)))
                c.execute("UPDATE sessions SET state_version = state_version + 1 WHERE session_id = ?",
                          (session.session_id,))
                row = c.execute("SELECT * FROM shift_briefings WHERE shift_id = ?", (session.shift_id,)).fetchone()
            return s.ShiftBriefing(shift_id=row["shift_id"], event_id=row["event_id"], speech=row["speech"],
                                   created_at=parse_dt(row["created_at"])), created

    def get_shift_briefing(self, shift_id: str) -> s.ShiftBriefing | None:
        row = self._one("SELECT * FROM shift_briefings WHERE shift_id = ?", (shift_id,))
        return s.ShiftBriefing(shift_id=row["shift_id"], event_id=row["event_id"], speech=row["speech"],
                               created_at=parse_dt(row["created_at"])) if row else None

    def get_telemetry(self, session_id: str, event_id: str) -> tuple[str, dict[str, Any]] | None:
        row = self._one(
            "SELECT request_hash, result_json FROM telemetry_events WHERE session_id = ? AND event_id = ?",
            (session_id, event_id),
        )
        return (row["request_hash"], json.loads(row["result_json"])) if row else None

    def machine_state(self, session_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM machine_state WHERE session_id = ?", (session_id,))
        return dict(row) if row else None

    def apply_observation(self, session: s.Session, req: s.TelemetryRequest, request_hash: str, policy: "SafetyPolicy",
                          machine_category: str | None, announcement_ttl: timedelta,
                          requires_engine_on: bool, outcomes: "list[RuleOutcome] | None" = None,
                          in_task_check: "s.ConditionCheck | None" = None,
                          evaluate_in_tx: "Callable[[sqlite3.Connection, dict], tuple[list[RuleOutcome], dict]] | None"
                          = None,
                          repeat_in_tx: "Callable[[sqlite3.Connection], list[RuleOutcome]] | None" = None) -> dict[str, Any]:
        """Record one sample and apply every rule's episode transition, the linked automatic draft and the
        announcement in ONE short transaction (no model call inside).

        Ordering uses the observation clock: a sample older than the newest applied one is `late`, and one with the
        same observation time but different readings is `conflicting`; both are recorded but change nothing.
        Idle streaks are measured between observation timestamps, never against the wall clock."""
        from .rules import condition, idle_observation

        now = utcnow()
        session_id = session.session_id
        opened: list[str] = []
        cleared: list[str] = []
        updated: list[str] = []
        announced: list[str] = []
        drafts: list[str] = []
        readings_json = req.readings.model_dump_json()
        with self._tx() as c:
            prev = c.execute("SELECT * FROM machine_state WHERE session_id = ?", (session_id,)).fetchone()
            srow = c.execute("SELECT last_observed_at FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            last_observed = parse_dt(srow["last_observed_at"])
            ignored = None
            if last_observed is not None and req.observed_at < last_observed:
                ignored = "late"
            elif (last_observed is not None and req.observed_at == last_observed and prev is not None
                  and json.loads(prev["readings_json"]) != json.loads(readings_json)):
                ignored = "conflicting"
            changed = False
            if ignored is None:
                idle = idle_observation(req.readings)
                prev_idle_since = parse_dt(prev["idle_since"]) if prev is not None else None
                idle_since = (prev_idle_since or req.observed_at) if idle else None
                idle_seconds = int((req.observed_at - idle_since).total_seconds()) if idle_since else 0
                c.execute("UPDATE sessions SET last_observed_at = ? WHERE session_id = ?",
                          (iso(req.observed_at), session_id))
                c.execute("INSERT INTO machine_state(session_id, event_id, observed_at, received_at, readings_json,"
                          " idle_since) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET"
                          " event_id = excluded.event_id, observed_at = excluded.observed_at,"
                          " received_at = excluded.received_at, readings_json = excluded.readings_json,"
                          " idle_since = excluded.idle_since",
                          (session_id, req.event_id, iso(req.observed_at), iso(now), readings_json, iso(idle_since)))
                for rule in policy.rules:
                    if not rule.applies_to(machine_category):
                        continue
                    held = condition(rule, req.readings, idle, idle_seconds, requires_engine_on=requires_engine_on)
                    active = c.execute("SELECT * FROM alerts WHERE session_id = ? AND rule_id = ? AND status = 'active'",
                                       (session_id, rule.rule_id)).fetchone()
                    if held and active is None:
                        alert_id = "ALR-" + uuid.uuid4().hex[:12]
                        correlated = None
                        if rule.family == "idle_unbelted":
                            belt = c.execute("SELECT a.alert_id FROM alerts a WHERE a.session_id = ? AND a.status ="
                                             " 'active' AND a.alert_type = 'seatbelt_unfastened'",
                                             (session_id,)).fetchone()
                            correlated = belt["alert_id"] if belt else None
                        evidence = s.AlertEvidence(
                            event_id=req.event_id, observed_at=req.observed_at, readings=req.readings,
                            idle_since=idle_since if rule.family != "seatbelt_engine_on" else None,
                            idle_seconds_observed=idle_seconds if rule.family != "seatbelt_engine_on" else None,
                            machine_category=machine_category,
                            applicability="category_listed" if rule.applies_to_categories else "all_categories")
                        c.execute(
                            "INSERT INTO alerts(alert_id, session_id, rule_id, alert_type, severity, status, message,"
                            " explanation, trigger_readings_json, opened_by_event_id, started_at, policy_version,"
                            " source_status, reason, recommended_action, evidence_json, correlated_alert_id, announced)"
                            " VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (alert_id, session_id, rule.rule_id, rule.alert_type, rule.severity, rule.message,
                             rule.explanation(req.observed_at, idle_since, idle_seconds), readings_json, req.event_id,
                             iso(req.observed_at), policy.policy_version, policy.source_status, rule.reason,
                             rule.recommended_action, evidence.model_dump_json(), correlated, int(correlated is None)),
                        )
                        opened.append(alert_id)
                        if correlated is None:  # one physical situation is announced (and drafted) once
                            if rule.draft_incident:
                                draft_id = Store.insert_auto_draft(
                                    c, session, alert_id, f"{rule.reason.rstrip('.')} (automatic draft from simulated telemetry)",
                                    rule.draft_severity or "medium", req.observed_at)
                                c.execute("UPDATE alerts SET draft_incident_id = ? WHERE alert_id = ?",
                                          (draft_id, alert_id))
                                drafts.append(draft_id)
                            if rule.training_lesson_id:
                                assignment_id = Store._episode_assignment(c, session, rule.training_lesson_id, alert_id)
                                if assignment_id:
                                    c.execute("UPDATE alerts SET training_assignment_id = ? WHERE alert_id = ?",
                                              (assignment_id, alert_id))
                            announced.append(self._announce(c, session_id, alert_id, "alert_started", "high",
                                                            rule.start_speech, now, announcement_ttl))
                        changed = True
                    elif held is False and active is not None:
                        c.execute("UPDATE alerts SET status = 'cleared', cleared_at = ?, cleared_by_event_id = ?"
                                  " WHERE alert_id = ?", (iso(req.observed_at), req.event_id, active["alert_id"]))
                        cleared.append(active["alert_id"])
                        if rule.clear_speech and active["announced"]:
                            announced.append(self._announce(c, session_id, active["alert_id"], "alert_cleared", "low",
                                                            rule.clear_speech, now, announcement_ttl))
                        changed = True
                rule_state = json.loads(prev["rule_state_json"]) if prev is not None and "rule_state_json" in \
                    prev.keys() and prev["rule_state_json"] else {}
                extra = list(outcomes or ())
                if evaluate_in_tx is not None:
                    more, rule_state = evaluate_in_tx(c, rule_state)
                    extra += more
                    c.execute("UPDATE machine_state SET rule_state_json = ? WHERE session_id = ?",
                              (json.dumps(rule_state, default=str), session_id))

                def apply(items: list[RuleOutcome]) -> bool:
                    moved = False
                    for o in items:
                        for kind, alert_id, event_id in self._rule_outcome(c, session, req, o, now, announcement_ttl,
                                                                           in_task_check):
                            {"opened": opened, "cleared": cleared, "updated": updated}[kind].append(alert_id)
                            if event_id:
                                announced.append(event_id)
                            moved = True
                    return moved

                changed = apply(extra) or changed
                if repeat_in_tx is not None:  # counts the episodes this very sample may have opened
                    changed = apply(repeat_in_tx(c)) or changed
            if changed:
                c.execute("UPDATE sessions SET state_version = state_version + 1 WHERE session_id = ?", (session_id,))
            version = c.execute("SELECT state_version FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]
            result = {
                "session_id": session_id,
                "event_id": req.event_id,
                "stale": ignored is not None,
                "ignored_reason": ignored,
                "alerts_opened": opened,
                "alerts_cleared": cleared,
                "alerts_updated": updated,
                "announcements_created": announced,
                "drafts_created": drafts,
                "state_version": version,
            }
            c.execute(
                "INSERT INTO telemetry_events(session_id, event_id, request_hash, observed_at, readings_json,"
                " result_json, received_at, provenance_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, req.event_id, request_hash, iso(req.observed_at), readings_json, json.dumps(result),
                 iso(now), req.provenance.model_dump_json() if req.provenance else None),
            )
            return result

    def _rule_outcome(self, c: sqlite3.Connection, session: s.Session, req: s.TelemetryRequest, o: RuleOutcome,
                      now: datetime, ttl: timedelta,
                      in_task_check: "s.ConditionCheck | None") -> list[tuple[str, str, str | None]]:
        """Open, update or close one C-family episode inside the observation transaction. The opening evidence is
        saved once; a later level change is an `alert_updates` row with its own evidence (announced once per episode
        when it rises); a close records why it closed."""
        sid = session.session_id
        at = iso(req.observed_at)
        active = c.execute("SELECT * FROM alerts WHERE session_id = ? AND rule_id = ? AND subject_key = ?"
                           " AND status = 'active'", (sid, o.rule_id, o.subject_key)).fetchone()
        if o.held and active is None:
            alert_id = "ALR-" + uuid.uuid4().hex[:12]
            details = dict(o.details)
            if o.family == "working_conditions" and in_task_check is not None:
                details["check_id"] = Store.save_condition_check(c, session, in_task_check, "in_task")
            evidence = s.AlertEvidence(event_id=req.event_id, observed_at=req.observed_at, readings=req.readings)
            c.execute(
                "INSERT INTO alerts(alert_id, session_id, rule_id, alert_type, severity, status, message, explanation,"
                " trigger_readings_json, opened_by_event_id, started_at, policy_version, source_status, reason,"
                " recommended_action, evidence_json, announced, details_json, subject_key, level, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (alert_id, sid, o.rule_id, o.alert_type, o.severity, o.message, o.explanation,
                 req.readings.model_dump_json(), req.event_id, at, o.policy_version, o.source_status, o.reason,
                 o.recommended_action, evidence.model_dump_json(), json.dumps(details, default=str), o.subject_key,
                 o.level, at))
            if o.on_open is not None:
                details.update(o.on_open(c, alert_id))
                c.execute("UPDATE alerts SET details_json = ? WHERE alert_id = ?",
                          (json.dumps(details, default=str), alert_id))
            event_id = self._announce(c, sid, alert_id, "alert_started", o.priority, o.start_speech, now, ttl)
            steps = [("opened", alert_id, event_id)]
            if o.instant:
                c.execute("UPDATE alerts SET status = 'cleared', cleared_at = ?, cleared_by_event_id = ?,"
                          " cleared_reason = 'instant_event' WHERE alert_id = ?", (at, req.event_id, alert_id))
            return steps
        if o.held and active is not None:
            if o.touch:
                c.execute("UPDATE alerts SET last_seen_at = ? WHERE alert_id = ?", (at, active["alert_id"]))
            if not o.level or o.level == active["level"]:
                return []
            rising = LEVEL_RANK.get(o.level, 0) > LEVEL_RANK.get(active["level"] or "", 0)
            update_id = "UPD-" + uuid.uuid4().hex[:12]
            event_id = None
            already = c.execute("SELECT 1 FROM announcements WHERE alert_id = ? AND type = 'alert_escalated'",
                                (active["alert_id"],)).fetchone()
            if rising and o.escalate_speech and already is None:
                event_id = self._announce(c, sid, active["alert_id"], "alert_escalated", "critical" if
                                          o.severity == "critical" else "high", o.escalate_speech, now, ttl)
            c.execute("INSERT INTO alert_updates(update_id, alert_id, level, previous_level, observed_at, event_id,"
                      " details_json, announcement_event_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (update_id, active["alert_id"], o.level, active["level"], at, req.event_id,
                       json.dumps(o.details, default=str), event_id, iso(now)))
            c.execute("UPDATE alerts SET level = ?, severity = CASE WHEN ? THEN ? ELSE severity END WHERE alert_id = ?",
                      (o.level, int(rising), o.severity, active["alert_id"]))
            return [("updated", active["alert_id"], event_id)]
        if o.held is False and active is not None:
            c.execute("UPDATE alerts SET status = 'cleared', cleared_at = ?, cleared_by_event_id = ?, cleared_reason = ?"
                      " WHERE alert_id = ?", (at, req.event_id, o.clear_reason, active["alert_id"]))
            event_id = None
            if o.clear_speech:
                event_id = self._announce(c, sid, active["alert_id"], "alert_cleared", "low", o.clear_speech, now, ttl)
            return [("cleared", active["alert_id"], event_id)]
        return []

    @staticmethod
    def _announce(c: sqlite3.Connection, session_id: str, alert_id: str, kind: str, priority: str, speech: str,
                  now: datetime, ttl: timedelta) -> str:
        seq = c.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM announcements WHERE session_id = ?",
                        (session_id,)).fetchone()[0]
        event_id = f"ann_{alert_id}_{ {'alert_started': 'start', 'alert_escalated': 'escalated'}.get(kind, 'clear')}"
        c.execute(
            "INSERT INTO announcements(event_id, session_id, sequence, type, priority, speech, alert_id, created_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, session_id, seq, kind, priority, speech, alert_id, iso(now), iso(now + ttl)),
        )
        return event_id

    # ------------------------------------------------------------------ announcements

    def list_events(self, session_id: str, after: int, limit: int) -> tuple[list[s.Announcement], bool]:
        rows = self._all(
            "SELECT * FROM announcements WHERE session_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
            (session_id, after, limit + 1),
        )
        has_more = len(rows) > limit
        events = []
        for r in rows[:limit]:
            deliveries = [_delivery(d) for d in self._all(
                "SELECT * FROM deliveries WHERE event_id = ? ORDER BY consumer_id", (r["event_id"],))]
            events.append(s.Announcement(
                event_id=r["event_id"], sequence=r["sequence"], type=r["type"], priority=r["priority"],
                speech=r["speech"], alert_id=r["alert_id"], created_at=parse_dt(r["created_at"]),
                expires_at=parse_dt(r["expires_at"]), deliveries=deliveries,
            ))
        return events, has_more

    def announcement_exists(self, session_id: str, event_id: str) -> bool:
        return self._one("SELECT 1 FROM announcements WHERE session_id = ? AND event_id = ?", (session_id, event_id)) is not None

    def record_delivery(self, event_id: str, report: s.DeliveryReport) -> s.DeliveryRecord:
        with self._tx() as c:
            c.execute(
                "INSERT INTO deliveries(event_id, consumer_id, status, detail, recorded_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(event_id, consumer_id) DO UPDATE SET status = excluded.status,"
                " detail = excluded.detail, recorded_at = excluded.recorded_at",
                (event_id, report.consumer_id, report.status, report.detail, iso(utcnow())),
            )
            row = c.execute("SELECT * FROM deliveries WHERE event_id = ? AND consumer_id = ?",
                            (event_id, report.consumer_id)).fetchone()
            return _delivery(row)


# ---------------------------------------------------------------------- row mappers

_TASK_SQL = ("SELECT t.*, z.name AS zone_name, z.outdoor AS zone_outdoor, si.utc_offset, cc.check_json AS start_check_json"
             " FROM task_assignments t"
             " JOIN site_zones z ON z.site_zone_id = t.site_zone_id"
             " JOIN shifts sh ON sh.shift_id = t.shift_id JOIN sites si ON si.site_id = sh.site_id"
             " LEFT JOIN condition_checks cc ON cc.check_id = t.start_check_id")


def _local_hhmm(when: datetime, utc_offset: str) -> str:
    sign = 1 if utc_offset[0] == "+" else -1
    local = when.astimezone(timezone.utc) + sign * timedelta(hours=int(utc_offset[1:3]), minutes=int(utc_offset[4:6]))
    return local.strftime("%H:%M")


def _assigned_task(r: sqlite3.Row) -> s.AssignedTask:
    weather = json.loads(r["weather_json"])
    return s.AssignedTask(
        task_id=r["task_id"], shift_id=r["shift_id"], machine_id=r["machine_id"], site_zone_id=r["site_zone_id"],
        zone_name=r["zone_name"], scheduled_order=r["scheduled_order"],
        scheduled_start_at=parse_dt(r["scheduled_start_at"]),
        scheduled_start_local=_local_hhmm(parse_dt(r["scheduled_start_at"]), r["utc_offset"]),
        task_type=r["task_type"], title=r["title"],
        details=r["details"], work_quantity=r["work_quantity"], work_unit=r["work_unit"], status=r["status"],
        version=r["version"], started_at=parse_dt(r["started_at"]), completed_at=parse_dt(r["completed_at"]),
        weather=s.TaskConditions(source=weather["source"], summary=weather.get("summary"),
                                 temperature_c=weather.get("temperature_c")),
        duration=s.TaskDuration(minutes=r["duration_minutes"], source=r["duration_source"]),
        outdoor=bool(r["zone_outdoor"]),
        start_check=s.ConditionCheck.model_validate_json(r["start_check_json"]) if r["start_check_json"] else None,
    )


def _shift(r: sqlite3.Row) -> s.ShiftInfo:
    return s.ShiftInfo(shift_id=r["shift_id"], site_id=r["site_id"], site_name=r["site_name"], timezone=r["timezone"],
                       service_date=r["service_date"], start_at=parse_dt(r["start_at"]), end_at=parse_dt(r["end_at"]),
                       utc_offset=r["utc_offset"], source="synthetic_demo_fixture")


# Never selects token_sha256: token metadata leaving the store cannot carry the digest.
_TOKEN_META_SQL = (
    "SELECT t.token_id, t.principal_id, p.kind, p.operator_id, p.display_name, t.scopes, t.issued_at, t.expires_at,"
    " t.revoked_at, t.revoke_reason FROM actor_tokens t JOIN principals p USING (principal_id)"
)


def _token_meta(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "token_id": r["token_id"], "principal_id": r["principal_id"], "kind": r["kind"],
        "operator_id": r["operator_id"], "display_name": r["display_name"], "scopes": tuple(r["scopes"].split()),
        "issued_at": parse_dt(r["issued_at"]), "expires_at": parse_dt(r["expires_at"]),
        "revoked_at": parse_dt(r["revoked_at"]), "revoke_reason": r["revoke_reason"],
    }


def _session(r: sqlite3.Row) -> s.Session:
    return s.Session(
        session_id=r["session_id"], client_session_key=r["client_session_key"], room_name=r["room_name"],
        participant_identity=r["participant_identity"], operator_id=r["operator_id"], machine_id=r["machine_id"],
        state_version=r["state_version"], created_at=parse_dt(r["created_at"]),
        dataset_manifest_sha256=r["dataset_manifest_sha256"], site_id=r["site_id"], shift_id=r["shift_id"],
        binding_status=r["binding_status"], context_status=r["context_status"], context_source=r["context_source"],
    )


def _turn(r: sqlite3.Row) -> TurnRow:
    return TurnRow(
        session_id=r["session_id"], turn_id=r["turn_id"], request_hash=r["request_hash"], status=r["status"],
        attempts=r["attempts"], result=json.loads(r["result_json"]) if r["result_json"] else None,
        error=json.loads(r["error_json"]) if r["error_json"] else None, created_at=parse_dt(r["created_at"]),
        completed_at=parse_dt(r["completed_at"]),
    )


def _task(r: sqlite3.Row) -> s.Task:
    return s.Task(task_id=r["task_id"], title=r["title"], details=r["details"], priority=r["priority"], status=r["status"])


_INCIDENT_V5 = ("origin", "severity", "severity_basis", "site_id", "site_zone_id", "zone_basis", "location_text",
                "occurred_basis", "episode_id", "draft_id", "occurred_expression")


def _incident(r: sqlite3.Row) -> s.Incident:
    # Rows read before migration 5/8 (only during an upgrade) lack the structured columns: model defaults apply.
    extra = {k: r[k] for k in _INCIDENT_V5 if k in r.keys()}
    if "occurred_at" in r.keys():
        extra.update(occurred_at=parse_dt(r["occurred_at"]), confirmed_at=parse_dt(r["confirmed_at"]))
    if "occurred_reference_at" in r.keys():
        extra["occurred_reference_at"] = parse_dt(r["occurred_reference_at"])
    return s.Incident(
        incident_id=r["incident_id"], incident_number=r["incident_number"], session_id=r["session_id"],
        operator_id=r["operator_id"], machine_id=r["machine_id"], description=r["description"],
        source_turn_id=r["source_turn_id"], created_at=parse_dt(r["created_at"]), **extra,
    )


def _draft(r: sqlite3.Row) -> s.IncidentDraft:
    keys = r.keys()
    extra = {}
    if "notify_supervisor" in keys:  # absent only while reading a pre-v8 row during an upgrade
        extra = dict(occurred_expression=r["occurred_expression"],
                     occurred_reference_at=parse_dt(r["occurred_reference_at"]),
                     notify_supervisor=bool(r["notify_supervisor"]))
    return s.IncidentDraft(
        draft_id=r["draft_id"], draft_number=r["draft_number"], session_id=r["session_id"],
        operator_id=r["operator_id"], machine_id=r["machine_id"], origin=r["origin"], status=r["status"],
        description=r["description"], severity=r["severity"], severity_basis=r["severity_basis"], site_id=r["site_id"],
        site_zone_id=r["site_zone_id"], zone_basis=r["zone_basis"], location_text=r["location_text"],
        occurred_at=parse_dt(r["occurred_at"]), occurred_basis=r["occurred_basis"], episode_id=r["episode_id"],
        version=r["version"], incident_id=r["incident_id"], created_at=parse_dt(r["created_at"]),
        confirmed_at=parse_dt(r["confirmed_at"]), dismissed_at=parse_dt(r["dismissed_at"]),
        missing=_draft_missing(r) if r["status"] == "draft" else [], **extra,
    )


def _draft_missing(r: sqlite3.Row) -> list[str]:
    """Facts a draft still needs before it can be confirmed (a rule default or 'unknown' counts as stated)."""
    missing = []
    if r["severity_basis"] is None:
        missing.append("severity")
    if r["occurred_at"] is None or r["occurred_basis"] in (None, "unresolved"):
        missing.append("occurred_time")
    return missing


class InvalidInput(Exception):
    """A request value that is well-formed but cannot be used (e.g. an uninterpretable time phrase). Changes nothing."""

    def __init__(self, field: str, issue: str):
        super().__init__(f"{field}: {issue}")
        self.field, self.issue = field, issue


def session_offset(c: sqlite3.Connection, session: s.Session) -> timezone | None:
    """Trusted site UTC offset of the session's seeded shift (None when unbound)."""
    if not session.shift_id:
        return None
    row = c.execute("SELECT si.utc_offset FROM shifts sh JOIN sites si USING (site_id) WHERE sh.shift_id = ?",
                    (session.shift_id,)).fetchone()
    return offset_timezone(row["utc_offset"]) if row else None


def _draft_edits(c: sqlite3.Connection, session: s.Session, e: dict[str, Any]) -> tuple[list[str], list[Any]]:
    sets: list[str] = []
    args: list[Any] = []
    if e.get("description"):
        sets.append("description = ?")
        args.append(e["description"])
    if e.get("severity") and e.get("severity_unknown"):
        raise InvalidInput("payload.severity", "give a severity level or severity_unknown, not both")
    if e.get("severity"):
        sets += ["severity = ?", "severity_basis = 'reported'"]
        args.append(e["severity"])
    elif e.get("severity_unknown"):
        sets += ["severity = NULL", "severity_basis = 'stated_unknown'"]
    if e.get("location_text"):
        zone, basis = _resolve_zone(c, session, e["location_text"])
        sets.append("location_text = ?")
        args.append(e["location_text"])
        if basis == "reported":
            sets += ["site_zone_id = ?", "zone_basis = 'reported'"]
            args.append(zone)
    when: OccurrenceTime | None = e.get("when")
    if when is None and e.get("occurred_at") is not None:
        at = e["occurred_at"]
        now = utcnow()
        if at > now + timedelta(minutes=5):
            raise InvalidInput("payload.occurred_at", "is in the future")
        when = OccurrenceTime(at.astimezone(timezone.utc), "operator_entered", None, now)
    elif when is None and e.get("occurred_expression"):
        now = utcnow()
        got = interpret(e["occurred_expression"], now, session_offset(c, session))
        if got.status != "resolved":
            raise InvalidInput("payload.occurred_expression", got.reason or "not a supported time expression")
        when = OccurrenceTime(got.occurred_at, got.basis, got.expression, now)
    if when is not None:
        sets += ["occurred_at = ?", "occurred_basis = ?", "occurred_expression = ?", "occurred_reference_at = ?"]
        args += [iso(when.occurred_at), when.basis, when.expression, iso(when.reference_at)]
    return sets, args


def _approval(r: sqlite3.Row) -> s.ApprovalRequest:
    return s.ApprovalRequest(approval_id=r["approval_id"], kind=r["kind"], incident_id=r["incident_id"],
                             status=r["status"], created_at=parse_dt(r["created_at"]),
                             alert_id=r["alert_id"] if "alert_id" in r.keys() else None)


_ZONE_STOPWORDS = {"to", "the", "of", "road", "access", "north", "south", "east", "west", "yard", "strip"}


def _resolve_zone(c: sqlite3.Connection, session: s.Session,
                  location_text: str | None) -> tuple[str | None, str | None]:
    """Site zone for an incident: the one zone the operator named, else the zone of the task in progress."""
    if not session.site_id:
        return None, None
    if location_text:
        words = set(re.findall(r"[a-z]+", location_text.lower()))
        hits = [z["site_zone_id"] for z in c.execute("SELECT site_zone_id, name FROM site_zones WHERE site_id = ?",
                                                     (session.site_id,))
                if words & (set(z["name"].lower().split()) - _ZONE_STOPWORDS)]
        if len(hits) == 1:
            return hits[0], "reported"
    if session.shift_id:
        row = c.execute("SELECT site_zone_id FROM task_assignments WHERE shift_id = ? AND status = 'in_progress'"
                        " ORDER BY scheduled_order LIMIT 1", (session.shift_id,)).fetchone()
        if row is not None:
            return row["site_zone_id"], "active_task"
    return None, None


def _assignment(r: sqlite3.Row) -> s.TrainingAssignment:
    return s.TrainingAssignment(
        assignment_id=r["assignment_id"], lesson_id=r["lesson_id"], lesson_title=r["lesson_title"],
        operator_id=r["operator_id"], status=r["status"], assigned_at=parse_dt(r["assigned_at"]),
        source_episode_id=r["source_episode_id"] if "source_episode_id" in r.keys() else None,
    )


def _lesson(r: sqlite3.Row) -> s.Lesson:
    return s.Lesson(**{k: r[k] for k in r.keys() if k in s.Lesson.model_fields})


def _alert(r: sqlite3.Row) -> s.Alert:
    keys = r.keys()
    extra = {}
    if "evidence_json" in keys:  # absent only while reading a pre-v6 row during an upgrade
        extra = dict(policy_version=r["policy_version"], source_status=r["source_status"], reason=r["reason"],
                     recommended_action=r["recommended_action"], correlated_alert_id=r["correlated_alert_id"],
                     draft_incident_id=r["draft_incident_id"], announced=bool(r["announced"]),
                     training_assignment_id=r["training_assignment_id"] if "training_assignment_id" in keys else None,
                     evidence=s.AlertEvidence.model_validate_json(r["evidence_json"]) if r["evidence_json"] else None,
                     details=json.loads(r["details_json"]) if "details_json" in keys and r["details_json"] else None)
    if "subject_key" in keys:
        extra.update(subject_key=r["subject_key"], level=r["level"], cleared_reason=r["cleared_reason"])
    return s.Alert(
        alert_id=r["alert_id"], rule_id=r["rule_id"], alert_type=r["alert_type"], severity=r["severity"],
        status=r["status"], message=r["message"], explanation=r["explanation"], simulated=True,
        trigger_readings=s.TelemetryReadings.model_validate_json(r["trigger_readings_json"]),
        started_at=parse_dt(r["started_at"]), cleared_at=parse_dt(r["cleared_at"]), **extra,
    )


def _delivery(r: sqlite3.Row) -> s.DeliveryRecord:
    return s.DeliveryRecord(
        event_id=r["event_id"], consumer_id=r["consumer_id"], status=r["status"], detail=r["detail"],
        recorded_at=parse_dt(r["recorded_at"]),
    )
