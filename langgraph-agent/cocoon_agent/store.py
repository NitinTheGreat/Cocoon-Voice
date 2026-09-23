"""SQLite persistence owned exclusively by langgraph-agent.

Side effects are made idempotent with database uniqueness constraints keyed on
stable client IDs (turn_id, event_id), not with an in-memory cache.
"""

from __future__ import annotations

import json
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

    # ------------------------------------------------------------------ training

    def assign_training(self, session: s.Session, lesson_id: str, source_turn_id: str) -> tuple[s.TrainingAssignment, bool]:
        with self._tx() as c:
            existing = c.execute(
                "SELECT * FROM training_assignments WHERE operator_id = ? AND lesson_id = ?", (session.operator_id, lesson_id)
            ).fetchone()
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

    def list_assignments(self, operator_id: str) -> list[s.TrainingAssignment]:
        rows = self._all(
            "SELECT ta.*, l.title AS lesson_title FROM training_assignments ta JOIN lessons l USING (lesson_id)"
            " WHERE ta.operator_id = ? ORDER BY ta.assigned_at", (operator_id,)
        )
        return [_assignment(r) for r in rows]

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


def _incident(r: sqlite3.Row) -> s.Incident:
    return s.Incident(
        incident_id=r["incident_id"], incident_number=r["incident_number"], session_id=r["session_id"],
        operator_id=r["operator_id"], machine_id=r["machine_id"], description=r["description"],
        source_turn_id=r["source_turn_id"], created_at=parse_dt(r["created_at"]),
    )


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
