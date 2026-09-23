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
from typing import Any, Callable

from .api import schemas as s
from .catalog import Catalog, CatalogError
from .migrations import migrate

SEED_TASKS = [
    ("T-101", "Pre-start walkaround inspection", "Check tracks, hydraulic lines and fluid levels before starting the excavator.", "high", 10),
    ("T-102", "Move the spoil pile in bay 3", "Load the spoil from bay 3 and dump it at the north stockpile.", "normal", 20),
    ("T-103", "Grade the access road at gate 2", "Level the ruts on the access road between gate 2 and the site office.", "normal", 30),
]

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


class InvalidTransition(Exception):
    def __init__(self, current_status: str, message: str):
        super().__init__(message)
        self.current_status = current_status


@dataclass(frozen=True)
class NewSessionBinding:
    """What admission established for a new session. Site/shift are None unless a trusted binding matched."""

    dataset_manifest_sha256: str
    context_status: str  # 'unavailable' | 'trusted_binding'
    site_id: str | None = None
    shift_id: str | None = None
    context_source: str | None = None


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
        return [s.Lesson(**dict(r)) for r in self._all("SELECT * FROM lessons ORDER BY lesson_id")]

    def get_lesson(self, lesson_id: str) -> s.Lesson | None:
        row = self._one("SELECT * FROM lessons WHERE lesson_id = ?", (lesson_id,))
        return s.Lesson(**dict(row)) if row else None

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
    def task_transition(session: s.Session, kind: str, task_id: str | None,
                        expected_version: int | None) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        """Mutation for task.start / task.complete, scoped to the session's trusted shift."""
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
            if kind == "task.start":
                c.execute("UPDATE task_assignments SET status = 'in_progress', version = version + 1, started_at = ?,"
                          " updated_at = ? WHERE task_id = ?", (now, now, row["task_id"]))
            else:
                c.execute("UPDATE task_assignments SET status = 'completed', version = version + 1, completed_at = ?,"
                          " updated_at = ? WHERE task_id = ?", (now, now, row["task_id"]))
            task = _assigned_task(c.execute(_TASK_SQL + " WHERE t.task_id = ?", (row["task_id"],)).fetchone())
            verb = "Started" if kind == "task.start" else "Completed"
            return {"record_type": "task", "record_id": task.task_id, "summary": f"{verb} {task.title}.",
                    "task": task.model_dump(mode="json")}

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

    def list_incidents(self, session_id: str, status: str = "confirmed") -> list[s.Incident]:
        rows = self._all("SELECT * FROM incidents WHERE session_id = ? AND status = ? ORDER BY incident_number",
                         (session_id, status))
        return [_incident(r) for r in rows]

    def get_incident(self, session_id: str, incident_id: str) -> s.Incident | None:
        row = self._one("SELECT * FROM incidents WHERE session_id = ? AND incident_id = ?", (session_id, incident_id))
        return _incident(row) if row else None

    def list_pending_approvals(self, session_id: str) -> list[s.ApprovalRequest]:
        rows = self._all("SELECT * FROM approval_requests WHERE session_id = ? AND status = 'pending'"
                         " ORDER BY created_at", (session_id,))
        return [_approval(r) for r in rows]

    @staticmethod
    def incident_report(session: s.Session, turn_id: str, description: str, severity: str | None,
                        location_text: str | None) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        """Mutation for an operator's report: a new confirmed incident per turn (matching text never merges two
        intentional reports). Identity comes from the session; "where" is the stated place, else the active task's
        zone, each with its basis; "when" is the time of the report unless stated otherwise; severity only if stated."""

        def mutate(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute("SELECT * FROM incidents WHERE session_id = ? AND source_turn_id = ?",
                            (session.session_id, turn_id)).fetchone()
            reused = row is not None  # written before the command log existed (pre-v5 retry)
            if row is None:
                now = iso(utcnow())
                zone, zone_basis = _resolve_zone(c, session, location_text)
                number = c.execute(
                    "INSERT INTO incidents(session_id, operator_id, machine_id, description, source_turn_id, created_at,"
                    " status, origin, severity, severity_basis, site_id, site_zone_id, zone_basis, location_text,"
                    " occurred_at, occurred_basis, confirmed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'confirmed', 'operator_reported', ?, ?, ?, ?, ?, ?, ?, 'time_of_report', ?)",
                    (session.session_id, session.operator_id, session.machine_id, description, turn_id, now, severity,
                     "reported" if severity else None, session.site_id, zone, zone_basis, location_text, now, now),
                ).lastrowid
                c.execute("UPDATE incidents SET incident_id = ? WHERE incident_number = ?", (f"INC-{number:04d}", number))
                row = c.execute("SELECT * FROM incidents WHERE incident_number = ?", (number,)).fetchone()
            inc = _incident(row)
            return {"record_type": "incident", "record_id": inc.incident_id, "reused": reused,
                    "summary": f"Logged incident {inc.incident_number}.", "incident": inc.model_dump(mode="json")}

        return mutate

    @staticmethod
    def insert_auto_draft(c: sqlite3.Connection, session: s.Session, episode_id: str, description: str,
                          severity: str, occurred_at: datetime) -> str:
        """An automatic draft for one alert episode (at most one per episode). Not a confirmed incident."""
        row = c.execute("SELECT incident_id FROM incidents WHERE episode_id = ?", (episode_id,)).fetchone()
        if row is not None:
            return row["incident_id"]
        zone, zone_basis = _resolve_zone(c, session, None)
        number = c.execute(
            "INSERT INTO incidents(session_id, operator_id, machine_id, description, source_turn_id, created_at, status,"
            " origin, severity, severity_basis, site_id, site_zone_id, zone_basis, occurred_at, occurred_basis,"
            " episode_id) VALUES (?, ?, ?, ?, ?, ?, 'draft', 'auto_draft', ?, 'rule_default', ?, ?, ?, ?,"
            " 'observation_time', ?)",
            (session.session_id, session.operator_id, session.machine_id, description, f"episode:{episode_id}",
             iso(utcnow()), severity, session.site_id, zone, zone_basis, iso(occurred_at), episode_id),
        ).lastrowid
        incident_id = f"INC-{number:04d}"
        c.execute("UPDATE incidents SET incident_id = ? WHERE incident_number = ?", (incident_id, number))
        return incident_id

    @staticmethod
    def incident_transition(session: s.Session, kind: str, incident_id: str, expected_version: int | None,
                            edits: dict[str, Any] | None = None) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        """Mutation for incident.edit / incident.confirm / incident.dismiss. Only this session's drafts change;
        a confirmed or dismissed report is final. The incident keeps its ID when confirmed."""

        def mutate(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute("SELECT * FROM incidents WHERE incident_id = ? AND session_id = ?",
                            (incident_id, session.session_id)).fetchone()
            if row is None:
                raise NotFound("no such incident in this session")
            if expected_version is not None and row["version"] != expected_version:
                raise VersionConflict(row["version"])
            if row["status"] != "draft":
                raise InvalidTransition(row["status"], f"incident is {row['status']}; {kind} needs a draft")
            now = iso(utcnow())
            if kind == "incident.confirm":
                c.execute("UPDATE incidents SET status = 'confirmed', confirmed_at = ?, version = version + 1"
                          " WHERE incident_id = ?", (now, incident_id))
                verb = "Confirmed"
            elif kind == "incident.dismiss":
                c.execute("UPDATE incidents SET status = 'dismissed', dismissed_at = ?, version = version + 1"
                          " WHERE incident_id = ?", (now, incident_id))
                verb = "Dismissed"
            else:
                e = edits or {}
                sets, args = [], []
                if e.get("description"):
                    sets.append("description = ?"), args.append(e["description"])
                if e.get("severity"):
                    sets += ["severity = ?", "severity_basis = 'reported'"]
                    args.append(e["severity"])
                if e.get("location_text"):
                    zone, basis = _resolve_zone(c, session, e["location_text"])
                    sets += ["location_text = ?", "site_zone_id = ?", "zone_basis = ?"]
                    args += [e["location_text"], zone if basis == "reported" else row["site_zone_id"],
                             basis if basis == "reported" else row["zone_basis"]]
                if not sets:
                    raise InvalidTransition(row["status"], "incident.edit needs at least one field to change")
                c.execute(f"UPDATE incidents SET {', '.join(sets)}, version = version + 1 WHERE incident_id = ?",
                          (*args, incident_id))
                verb = "Edited"
            inc = _incident(c.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone())
            return {"record_type": "incident", "record_id": incident_id,
                    "summary": f"{verb} incident {inc.incident_number}.", "incident": inc.model_dump(mode="json")}

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
        return [_alert(r) for r in rows]

    def latest_alert(self, session_id: str) -> s.Alert | None:
        row = self._one(
            "SELECT * FROM alerts WHERE session_id = ? ORDER BY (status = 'active') DESC, started_at DESC LIMIT 1",
            (session_id,),
        )
        return _alert(row) if row else None

    def get_telemetry(self, session_id: str, event_id: str) -> tuple[str, dict[str, Any]] | None:
        row = self._one(
            "SELECT request_hash, result_json FROM telemetry_events WHERE session_id = ? AND event_id = ?",
            (session_id, event_id),
        )
        return (row["request_hash"], json.loads(row["result_json"])) if row else None

    def apply_telemetry(
        self,
        session_id: str,
        req: s.TelemetryRequest,
        request_hash: str,
        condition_active: bool,
        rule: "AlertTemplate",
        announcement_ttl: timedelta,
    ) -> dict[str, Any]:
        """Record one sample and apply the alert episode transition atomically."""
        now = utcnow()
        opened: list[str] = []
        cleared: list[str] = []
        announced: list[str] = []
        with self._tx() as c:
            srow = c.execute("SELECT last_observed_at FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            last_observed = parse_dt(srow["last_observed_at"])
            stale = last_observed is not None and req.observed_at < last_observed
            changed = False
            if not stale:
                c.execute("UPDATE sessions SET last_observed_at = ? WHERE session_id = ?", (iso(req.observed_at), session_id))
                active = c.execute(
                    "SELECT * FROM alerts WHERE session_id = ? AND rule_id = ? AND status = 'active'",
                    (session_id, rule.rule_id),
                ).fetchone()
                if condition_active and active is None:
                    alert_id = "ALR-" + uuid.uuid4().hex[:12]
                    c.execute(
                        "INSERT INTO alerts(alert_id, session_id, rule_id, alert_type, severity, status, message,"
                        " explanation, trigger_readings_json, opened_by_event_id, started_at)"
                        " VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
                        (alert_id, session_id, rule.rule_id, rule.alert_type, rule.severity, rule.message,
                         rule.explanation(req), req.readings.model_dump_json(), req.event_id, iso(req.observed_at)),
                    )
                    opened.append(alert_id)
                    announced.append(self._announce(c, session_id, alert_id, "alert_started", "high",
                                                    rule.start_speech, now, announcement_ttl))
                    changed = True
                elif not condition_active and active is not None:
                    c.execute(
                        "UPDATE alerts SET status = 'cleared', cleared_at = ?, cleared_by_event_id = ? WHERE alert_id = ?",
                        (iso(req.observed_at), req.event_id, active["alert_id"]),
                    )
                    cleared.append(active["alert_id"])
                    announced.append(self._announce(c, session_id, active["alert_id"], "alert_cleared", "low",
                                                    rule.clear_speech, now, announcement_ttl))
                    changed = True
            if changed:
                c.execute("UPDATE sessions SET state_version = state_version + 1 WHERE session_id = ?", (session_id,))
            version = c.execute("SELECT state_version FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]
            result = {
                "session_id": session_id,
                "event_id": req.event_id,
                "stale": stale,
                "alerts_opened": opened,
                "alerts_cleared": cleared,
                "announcements_created": announced,
                "state_version": version,
            }
            c.execute(
                "INSERT INTO telemetry_events(session_id, event_id, request_hash, observed_at, readings_json,"
                " result_json, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, req.event_id, request_hash, iso(req.observed_at), req.readings.model_dump_json(),
                 json.dumps(result), iso(now)),
            )
            return result

    @staticmethod
    def _announce(c: sqlite3.Connection, session_id: str, alert_id: str, kind: str, priority: str, speech: str,
                  now: datetime, ttl: timedelta) -> str:
        seq = c.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM announcements WHERE session_id = ?",
                        (session_id,)).fetchone()[0]
        event_id = f"ann_{alert_id}_{'start' if kind == 'alert_started' else 'clear'}"
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


@dataclass(frozen=True)
class AlertTemplate:
    rule_id: str
    alert_type: str
    severity: str
    message: str
    start_speech: str
    clear_speech: str

    def explanation(self, req: s.TelemetryRequest) -> str:
        at = req.observed_at.strftime("%H:%M:%S UTC")
        return (
            f"At {at} the simulated telemetry showed the engine running while the seatbelt was unfastened. "
            "This is a prototype rule on simulated data, not validated machine safety logic."
        )


# ---------------------------------------------------------------------- row mappers

_TASK_SQL = ("SELECT t.*, z.name AS zone_name, si.utc_offset FROM task_assignments t"
             " JOIN site_zones z ON z.site_zone_id = t.site_zone_id"
             " JOIN shifts sh ON sh.shift_id = t.shift_id JOIN sites si ON si.site_id = sh.site_id")


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


_INCIDENT_V5 = ("status", "origin", "severity", "severity_basis", "site_id", "site_zone_id", "zone_basis",
                "location_text", "occurred_basis", "episode_id", "version")


def _incident(r: sqlite3.Row) -> s.Incident:
    # Rows read before migration 5 (only during an upgrade) lack the structured columns: model defaults apply.
    extra = {k: r[k] for k in _INCIDENT_V5 if k in r.keys()}
    if "occurred_at" in r.keys():
        extra.update(occurred_at=parse_dt(r["occurred_at"]), confirmed_at=parse_dt(r["confirmed_at"]))
    return s.Incident(
        incident_id=r["incident_id"], incident_number=r["incident_number"], session_id=r["session_id"],
        operator_id=r["operator_id"], machine_id=r["machine_id"], description=r["description"],
        source_turn_id=r["source_turn_id"], created_at=parse_dt(r["created_at"]), **extra,
    )


def _approval(r: sqlite3.Row) -> s.ApprovalRequest:
    return s.ApprovalRequest(approval_id=r["approval_id"], kind=r["kind"], incident_id=r["incident_id"],
                             status=r["status"], created_at=parse_dt(r["created_at"]))


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
    )


def _alert(r: sqlite3.Row) -> s.Alert:
    return s.Alert(
        alert_id=r["alert_id"], rule_id=r["rule_id"], alert_type=r["alert_type"], severity=r["severity"],
        status=r["status"], message=r["message"], explanation=r["explanation"], simulated=True,
        trigger_readings=s.TelemetryReadings.model_validate_json(r["trigger_readings_json"]),
        started_at=parse_dt(r["started_at"]), cleared_at=parse_dt(r["cleared_at"]),
    )


def _delivery(r: sqlite3.Row) -> s.DeliveryRecord:
    return s.DeliveryRecord(
        event_id=r["event_id"], consumer_id=r["consumer_id"], status=r["status"], detail=r["detail"],
        recorded_at=parse_dt(r["recorded_at"]),
    )
