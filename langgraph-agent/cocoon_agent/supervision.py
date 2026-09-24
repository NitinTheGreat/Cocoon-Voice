"""Site-scoped supervisor read models and the supervisor change feed (D2).

Scope: a supervisor principal whose token carries `supervise` reads and decides only at sites granted through the
admin CLI (`principal_site_grants`). Nothing is inferred from a query parameter, speech or room metadata: a
`site_id` the caller names must be one of its grants (else 403), and records of other sites are 404.

Projections are built from explicit fields (never the operator `/state`, alert details, draft evidence or free
text). Wellbeing appears only as `WellbeingRiskView`, recomputed from the operator's CURRENT consent on every read.

Feed: `supervisor_feed` rows are committed with the change they describe and hold only a reference
(type, ref_type, ref_id). Each replayed or live event is projected at send time under the reader's current grants
and the operator's current consent, so a revoked sharing grant also changes replayed events. A cursor is the
per-database `sequence`; events of other sites are skipped without affecting the cursor. Rows older than the
retention window are pruned; a cursor older than the oldest retained row gets 410 `replay_expired` (reload the
overview, whose `feed_cursor` continues the stream).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .api import schemas as s
from .approvals import approval_view, notification_item
from .auth import Principal
from .store import Store, iso, parse_dt, utcnow

FEED_SCHEMA = "cocoon.supervisor-feed.v1"
LIST_LIMIT = 50


class Supervision:
    def __init__(self, store: Store, wellbeing=None, clock: Callable[[], datetime] = utcnow,
                 feed_retention: timedelta = timedelta(hours=24)):
        self.store, self.wellbeing, self.clock, self.feed_retention = store, wellbeing, clock, feed_retention

    # ------------------------------------------------------------------ scope

    def sites(self, principal: Principal) -> set[str]:
        if principal.kind != "supervisor" or not principal.has_scope("supervise"):
            return set()
        return set(self.store.granted_sites(principal.subject_id))

    # ------------------------------------------------------------------ overview

    def overview(self, site_id: str) -> s.SupervisorSiteOverview:
        now = self.clock()
        with self.store._lock:
            c = self.store._conn
            cursor = c.execute("SELECT COALESCE(MAX(sequence), 0) FROM supervisor_feed WHERE site_id = ?",
                               (site_id,)).fetchone()[0]
            return s.SupervisorSiteOverview(
                site_id=site_id, generated_at=now, feed_cursor=cursor, tasks=self._tasks(c, site_id, None),
                alerts=self._alerts(c, site_id, None), incidents=self._incidents(c, site_id),
                approvals=[approval_view(c, r) for r in c.execute(
                    "SELECT * FROM approval_requests WHERE site_id = ? AND eligibility = 'eligible' AND payload_json IS"
                    " NOT NULL ORDER BY (status = 'pending') DESC, created_at DESC LIMIT ?", (site_id, LIST_LIMIT))],
                risks=self._risks(c, site_id, now),
                notifications=[notification_item(r) for r in c.execute(
                    "SELECT * FROM supervisor_notifications WHERE site_id = ? ORDER BY created_at DESC LIMIT ?",
                    (site_id, LIST_LIMIT))],
                sos=self.sos.supervisor_items(c, site_id) if self.sos is not None else [])

    @staticmethod
    def _tasks(c: sqlite3.Connection, site_id: str, shift_id: str | None) -> list[s.SupervisorTaskItem]:
        sql = ("SELECT t.*, z.name AS zone_name FROM task_assignments t JOIN shifts sh USING (shift_id)"
               " LEFT JOIN site_zones z ON z.site_zone_id = t.site_zone_id WHERE sh.site_id = ?")
        args: list[Any] = [site_id]
        if shift_id:
            sql += " AND t.shift_id = ?"
            args.append(shift_id)
        else:  # the site's latest service date only (bounded)
            sql += " AND sh.service_date = (SELECT MAX(service_date) FROM shifts WHERE site_id = ?)"
            args.append(site_id)
        rows = c.execute(sql + " ORDER BY t.shift_id, t.scheduled_order LIMIT ?", (*args, LIST_LIMIT * 2)).fetchall()
        return [s.SupervisorTaskItem(task_id=r["task_id"], operator_id=r["operator_id"], machine_id=r["machine_id"],
                                     shift_id=r["shift_id"], title=r["title"], zone_name=r["zone_name"],
                                     status=r["status"], scheduled_order=r["scheduled_order"],
                                     scheduled_start_at=parse_dt(r["scheduled_start_at"]), version=r["version"])
                for r in rows]

    @staticmethod
    def _alerts(c: sqlite3.Connection, site_id: str, session_id: str | None) -> list[s.SupervisorAlertItem]:
        sql = ("SELECT a.alert_id, a.alert_type, a.severity, a.level, a.status, a.started_at, a.cleared_at,"
               " se.operator_id, se.machine_id FROM alerts a JOIN sessions se USING (session_id)"
               " WHERE se.site_id = ? AND se.binding_status = 'catalog_verified'")
        args: list[Any] = [site_id]
        if session_id:
            sql += " AND a.session_id = ? AND a.status = 'active'"
            args.append(session_id)
        rows = c.execute(sql + " ORDER BY (a.status = 'active') DESC, a.started_at DESC LIMIT ?",
                         (*args, LIST_LIMIT)).fetchall()
        return [s.SupervisorAlertItem(alert_id=r["alert_id"], operator_id=r["operator_id"], machine_id=r["machine_id"],
                                      alert_type=r["alert_type"], severity=r["severity"], level=r["level"],
                                      status=r["status"], started_at=parse_dt(r["started_at"]),
                                      cleared_at=parse_dt(r["cleared_at"])) for r in rows]

    @staticmethod
    def _incidents(c: sqlite3.Connection, site_id: str) -> list[s.SupervisorIncidentItem]:
        rows = c.execute("SELECT incident_id, operator_id, machine_id, origin, severity, site_zone_id, occurred_at"
                         " FROM incidents WHERE site_id = ? AND incident_id IS NOT NULL ORDER BY incident_number DESC"
                         " LIMIT ?", (site_id, LIST_LIMIT)).fetchall()
        return [s.SupervisorIncidentItem(incident_id=r["incident_id"], operator_id=r["operator_id"],
                                         machine_id=r["machine_id"], origin=r["origin"], severity=r["severity"],
                                         site_zone_id=r["site_zone_id"], occurred_at=parse_dt(r["occurred_at"]))
                for r in rows]

    def _risks(self, c: sqlite3.Connection, site_id: str, now: datetime) -> list[s.WellbeingRiskView]:
        if self.wellbeing is None:
            return []
        operators = [r[0] for r in c.execute("SELECT DISTINCT operator_id FROM sessions WHERE site_id = ? AND"
                                             " binding_status = 'catalog_verified' ORDER BY operator_id", (site_id,))]
        return [self.wellbeing.risk(c, op, site_id, now) for op in operators]

    # ------------------------------------------------------------------ feed

    def feed_bounds(self) -> tuple[int, int]:
        """(pruned_through, newest sequence): a cursor below the first is 410, above the second 422."""
        seq = self.store._one("SELECT seq FROM sqlite_sequence WHERE name = 'supervisor_feed'")
        floor = self.store._one("SELECT value FROM feed_meta WHERE key = 'pruned_through'")
        return (int(floor[0]) if floor else 0), (seq[0] if seq else 0)

    def feed_page(self, site_id: str, after: int, limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        """Project up to `limit` events after the cursor. Returns (events, new cursor)."""
        now = self.clock()
        with self.store._lock:
            c = self.store._conn
            rows = c.execute("SELECT * FROM supervisor_feed WHERE site_id = ? AND sequence > ? ORDER BY sequence"
                             " LIMIT ?", (site_id, after, limit)).fetchall()
            events = [self.project(c, r, now) for r in rows]
        return events, (rows[-1]["sequence"] if rows else after)

    def project(self, c: sqlite3.Connection, r: sqlite3.Row, now: datetime) -> dict[str, Any]:
        site_id, ref = r["site_id"], r["ref_id"]
        data: dict[str, Any]
        if r["type"] == "risk.changed" and self.wellbeing is not None:
            data = self.wellbeing.risk(c, ref, site_id, now).model_dump(mode="json")
        elif r["type"] == "approval.changed":
            row = c.execute("SELECT * FROM approval_requests WHERE approval_id = ? AND site_id = ? AND eligibility ="
                            " 'eligible'", (ref, site_id)).fetchone()
            data = approval_view(c, row).model_dump(mode="json") if row else {"approval_id": ref, "removed": True}
        elif r["type"] == "notification.created":
            row = c.execute("SELECT * FROM supervisor_notifications WHERE notification_id = ? AND site_id = ?",
                            (ref, site_id)).fetchone()
            data = notification_item(row).model_dump(mode="json") if row else {"notification_id": ref,
                                                                               "removed": True}
        elif r["type"] == "tasks.changed":
            version = c.execute("SELECT schedule_version FROM shifts WHERE shift_id = ?", (ref,)).fetchone()
            data = {"shift_id": ref, "schedule_version": version[0] if version else None,
                    "tasks": [t.model_dump(mode="json") for t in self._tasks(c, site_id, ref)]}
        elif r["type"] == "alerts.changed":
            session = c.execute("SELECT operator_id, machine_id FROM sessions WHERE session_id = ? AND site_id = ?",
                                (ref, site_id)).fetchone()
            data = {"operator_id": session["operator_id"] if session else None,
                    "machine_id": session["machine_id"] if session else None,
                    "active_alerts": [a.model_dump(mode="json") for a in self._alerts(c, site_id, ref)]}
        elif r["type"] == "sos.changed" and self.sos is not None:
            data = self.sos.feed_projection(c, ref, site_id)
        else:
            data = {"removed": True}
        return {"schema_version": FEED_SCHEMA, "site_id": site_id, "event_id": r["event_id"],
                "sequence": r["sequence"], "type": r["type"], "created_at": r["created_at"], "data": data}

    sos = None  # cocoon_agent.sos.Sos, set at startup (emergency status projection)

    def prune_feed(self, now: datetime | None = None) -> int:
        """Delete feed rows older than the retention window; remember how far replay can still reach."""
        cutoff = (now or self.clock()) - self.feed_retention
        with self.store._tx() as c:
            last = c.execute("SELECT MAX(sequence) FROM supervisor_feed WHERE created_at < ?", (iso(cutoff),)).fetchone()[0]
            if last is None:
                return 0
            n = c.execute("DELETE FROM supervisor_feed WHERE sequence <= ?", (last,)).rowcount
            c.execute("INSERT INTO feed_meta(key, value) VALUES ('pruned_through', ?) ON CONFLICT(key) DO UPDATE SET"
                      " value = MAX(CAST(feed_meta.value AS INTEGER), CAST(excluded.value AS INTEGER))", (str(last),))
            return n
