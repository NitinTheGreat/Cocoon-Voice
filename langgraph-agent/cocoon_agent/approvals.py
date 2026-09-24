"""Durable supervisor approvals (D2): scoped list/detail, idempotent decisions, and application kept separate.

Lifecycle: `pending` -> `approved` | `rejected` | `expired` | `cancelled`. A decision is recorded in one transaction
(decision_id, actor, reason, payload hash, version + 1). An approval then applies its exact stored payload in a SECOND
transaction (`application_status` pending -> applied | failed_stale_inputs | failed); a crash between the two leaves
`approved` + `pending`, which `apply_pending` resumes at startup and from the worker without re-running any utterance.
Rejection, expiry and cancellation never apply anything and create no notification.

Visibility: a supervisor sees requests of its granted sites only (legacy rows without a trusted site are
`ineligible_no_site` and visible to no supervisor); an operator sees their own; everything else is 404.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .api import schemas as s
from .auth import Principal
from .store import InvalidTransition, NotFound, Store, VersionConflict, iso, parse_dt, utcnow


class DecisionConflict(Exception):
    """A different decision already exists (another decision_id, or the same one with another body)."""


class ApprovalExpired(Exception):
    pass


class PayloadMismatch(Exception):
    def __init__(self, current: str):
        super().__init__("payload_sha256 does not match the stored proposal")
        self.current = current


def payload_sha256(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _body_hash(req: s.ApprovalDecisionRequest) -> str:
    return hashlib.sha256(req.model_dump_json().encode("utf-8")).hexdigest()


def create_notification(c: sqlite3.Connection, *, site_id: str, kind: str, source_type: str, source_id: str,
                        operator_id: str | None, summary: str, now: datetime, priority: str = "normal",
                        policy_id: str | None = None) -> tuple[str, bool]:
    """One in-app record per (kind, source) — a retry or a second worker finds the existing one. Creating it is
    not delivery and not reading."""
    row = c.execute("SELECT notification_id FROM supervisor_notifications WHERE kind = ? AND source_id = ?",
                    (kind, source_id)).fetchone()
    if row is not None:
        return row["notification_id"], False
    notification_id = "NTF-" + hashlib.sha256(f"{kind}|{source_id}".encode()).hexdigest()[:12]
    c.execute("INSERT INTO supervisor_notifications(notification_id, site_id, kind, source_type, source_id,"
              " operator_id, priority, policy_id, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (notification_id, site_id, kind, source_type, source_id, operator_id, priority, policy_id, summary,
               iso(now)))
    Store.feed_event(c, site_id, "notification.created", "notification", notification_id, now)
    return notification_id, True


def notification_item(r: sqlite3.Row) -> s.SupervisorNotificationItem:
    return s.SupervisorNotificationItem(
        notification_id=r["notification_id"], site_id=r["site_id"], kind=r["kind"], priority=r["priority"],
        policy_id=r["policy_id"], source_type=r["source_type"], source_id=r["source_id"], operator_id=r["operator_id"],
        summary=r["summary"], status=r["status"], created_at=parse_dt(r["created_at"]),
        presented_at=parse_dt(r["presented_at"]), acknowledged_at=parse_dt(r["acknowledged_at"]))


def approval_view(c: sqlite3.Connection, r: sqlite3.Row) -> s.ApprovalView:
    payload = json.loads(r["payload_json"])
    machine = c.execute("SELECT machine_id FROM sessions WHERE session_id = ?", (r["session_id"],)).fetchone()
    escalation = schedule = None
    if r["kind"] == "schedule_change":
        schedule = s.ScheduleProposalView.model_validate(payload["proposal"])
    else:
        details = payload.get("details") or {}
        escalation = s.EscalationView(incident_id=payload.get("incident_id"), alert_id=payload.get("alert_id"),
                                      rule_family=details.get("family"),
                                      episode_count=len(details["episode_ids"]) if details.get("episode_ids") else None)
    decision = None
    if r["decision_id"]:
        decision = s.ApprovalDecisionView(decision_id=r["decision_id"], decision=r["decision"],
                                          decided_by=r["decided_by"], decided_at=parse_dt(r["decided_at"]),
                                          reason=r["decision_reason"])
    return s.ApprovalView(
        approval_id=r["approval_id"], kind=r["kind"], action_type=r["action_type"], status=r["status"],
        eligibility=r["eligibility"], site_id=r["site_id"], operator_id=r["operator_id"],
        machine_id=machine["machine_id"] if machine else None, proposer=r["proposer"],
        created_at=parse_dt(r["created_at"]), expires_at=parse_dt(r["expires_at"]), version=r["version"],
        payload_sha256=payload_sha256(r["payload_json"]), escalation=escalation, schedule=schedule,
        decision=decision, application=s.ApprovalApplicationView(status=r["application_status"],
                                                                 reason=r["application_reason"],
                                                                 applied_at=parse_dt(r["applied_at"])))


_ESCALATION_TEXT = {"repeated_violations": "Repeated safety-rule episodes", "incident_escalation": "Incident review"}


class Approvals:
    def __init__(self, store: Store, replanner=None, clock: Callable[[], datetime] = utcnow):
        self.store, self.replanner, self.clock = store, replanner, clock

    # ------------------------------------------------------------------ visibility

    @staticmethod
    def _visible(c: sqlite3.Connection, principal: Principal, sites: set[str], approval_id: str) -> sqlite3.Row | None:
        row = c.execute("SELECT * FROM approval_requests WHERE approval_id = ?", (approval_id,)).fetchone()
        if row is None or row["payload_json"] is None:
            return None
        if principal.kind == "supervisor":
            return row if row["eligibility"] == "eligible" and row["site_id"] in sites else None
        if principal.kind == "operator":
            owner = c.execute("SELECT binding_status FROM sessions WHERE session_id = ?", (row["session_id"],)).fetchone()
            ok = row["operator_id"] == principal.operator_id and owner and owner["binding_status"] == "catalog_verified"
            return row if ok else None
        return None

    def list(self, principal: Principal, sites: set[str], status: str | None, limit: int,
             cursor: str | None) -> s.ApprovalPageView:
        self.expire_due()
        offset = int(cursor) if cursor and cursor.isdigit() else 0
        where, args = ["payload_json IS NOT NULL"], []
        if principal.kind == "supervisor":
            if not sites:
                return s.ApprovalPageView(items=[])
            where.append(f"eligibility = 'eligible' AND site_id IN ({','.join('?' * len(sites))})")
            args += sorted(sites)
        else:
            where.append("operator_id = ? AND session_id IN (SELECT session_id FROM sessions WHERE binding_status ="
                         " 'catalog_verified')")
            args.append(principal.operator_id)
        if status:
            where.append("status = ?")
            args.append(status)
        with self.store._lock:
            c = self.store._conn
            rows = c.execute(f"SELECT * FROM approval_requests WHERE {' AND '.join(where)} ORDER BY created_at DESC,"
                             f" approval_id LIMIT ? OFFSET ?", (*args, limit + 1, offset)).fetchall()
            items = [approval_view(c, r) for r in rows[:limit]]
        return s.ApprovalPageView(items=items, next_cursor=str(offset + limit) if len(rows) > limit else None)

    def get(self, principal: Principal, sites: set[str], approval_id: str) -> s.ApprovalView:
        self.expire_due()
        with self.store._lock:
            row = self._visible(self.store._conn, principal, sites, approval_id)
            if row is None:
                raise NotFound("approval not found")
            return approval_view(self.store._conn, row)

    # ------------------------------------------------------------------ transitions

    def expire_due(self, now: datetime | None = None) -> list[str]:
        now = now or self.clock()
        with self.store._tx() as c:
            rows = c.execute("SELECT approval_id, site_id FROM approval_requests WHERE status = 'pending' AND"
                             " expires_at IS NOT NULL AND expires_at <= ?", (iso(now),)).fetchall()
            for r in rows:
                c.execute("UPDATE approval_requests SET status = 'expired', version = version + 1, updated_at = ?"
                          " WHERE approval_id = ?", (iso(now), r["approval_id"]))
                if r["site_id"]:
                    Store.feed_event(c, r["site_id"], "approval.changed", "approval", r["approval_id"], now)
            return [r["approval_id"] for r in rows]

    def decide(self, principal: Principal, sites: set[str], approval_id: str,
               req: s.ApprovalDecisionRequest) -> s.ApprovalDecisionResult:
        """Record one decision (exactly once per request). Identical retry → saved result; any other decision on a
        decided request → DecisionConflict; stale version or payload hash → VersionConflict/PayloadMismatch."""
        now = self.clock()
        self.expire_due(now)
        body = _body_hash(req)
        with self.store._tx() as c:
            row = self._visible(c, principal, sites, approval_id)
            if row is None:
                raise NotFound("approval not found")
            if row["decision_id"] is not None:
                if row["decision_id"] == req.decision_id and row["decision_hash"] == body:
                    return s.ApprovalDecisionResult(approval=approval_view(c, row), decision_recorded=False)
                raise DecisionConflict("this request was already decided" if row["decision_id"] != req.decision_id
                                       else "decision_id was already used with a different decision")
            if row["status"] == "expired":
                raise ApprovalExpired("the request expired before a decision")
            if row["status"] != "pending":
                raise DecisionConflict(f"the request is {row['status']}")
            if row["version"] != req.expected_version:
                raise VersionConflict(row["version"])
            current = payload_sha256(row["payload_json"])
            if current != req.payload_sha256:
                raise PayloadMismatch(current)
            approve = req.decision == "approve"
            c.execute("UPDATE approval_requests SET status = ?, decision_id = ?, decision = ?, decision_hash = ?,"
                      " decided_by = ?, decided_at = ?, decision_reason = ?, version = version + 1,"
                      " application_status = ?, updated_at = ? WHERE approval_id = ?",
                      ("approved" if approve else "rejected", req.decision_id, req.decision, body,
                       principal.subject_id, iso(now), req.reason, "pending" if approve else "not_applicable",
                       iso(now), approval_id))
            Store.feed_event(c, row["site_id"], "approval.changed", "approval", approval_id, now)
        if approve:
            self.apply_pending(approval_id)
        with self.store._lock:
            row = self.store._conn.execute("SELECT * FROM approval_requests WHERE approval_id = ?",
                                           (approval_id,)).fetchone()
            return s.ApprovalDecisionResult(approval=approval_view(self.store._conn, row), decision_recorded=True)

    def apply_pending(self, approval_id: str | None = None) -> list[str]:
        """Apply approved requests whose application is still pending (after a decision, at startup, from the
        worker). Each runs in its own transaction; the row condition makes it apply at most once."""
        done = []
        ids = [approval_id] if approval_id else [r[0] for r in self.store._all(
            "SELECT approval_id FROM approval_requests WHERE status = 'approved' AND application_status = 'pending'"
            " ORDER BY decided_at")]
        for aid in ids:
            now = self.clock()
            with self.store._tx() as c:
                row = c.execute("SELECT * FROM approval_requests WHERE approval_id = ? AND status = 'approved' AND"
                                " application_status = 'pending'", (aid,)).fetchone()
                if row is None:
                    continue
                c.execute("SAVEPOINT apply_approval")
                try:
                    status, reason = self._apply(c, row, now)
                    c.execute("RELEASE apply_approval")
                except Exception as exc:  # the decision stays; the effect is rolled back and reported
                    c.execute("ROLLBACK TO apply_approval")
                    c.execute("RELEASE apply_approval")
                    status, reason = "failed", f"application error ({type(exc).__name__})"
                c.execute("UPDATE approval_requests SET application_status = ?, application_reason = ?, applied_at = ?,"
                          " updated_at = ? WHERE approval_id = ?",
                          (status, reason, iso(now) if status == "applied" else None, iso(now), aid))
                Store.feed_event(c, row["site_id"], "approval.changed", "approval", aid, now)
                done.append(aid)
        return done

    def _apply(self, c: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> tuple[str, str | None]:
        if row["action_type"] == "apply_schedule_change":
            if self.replanner is None:
                return "failed", "re-planning is not enabled"
            return self.replanner.apply(c, row, json.loads(row["payload_json"]), now)
        machine = c.execute("SELECT machine_id FROM sessions WHERE session_id = ?", (row["session_id"],)).fetchone()
        what = _ESCALATION_TEXT.get(row["kind"], "Review")
        ref = row["incident_id"] or row["alert_id"]
        summary = f"{what} approved for operator {row['operator_id']} on {machine['machine_id']} ({ref})."
        create_notification(c, site_id=row["site_id"], kind="escalation_approved", source_type="approval",
                            source_id=row["approval_id"], operator_id=row["operator_id"], summary=summary, now=now)
        return "applied", None

    # ------------------------------------------------------------------ notifications

    def notification_receipt(self, sites: set[str], notification_id: str, status: str,
                             principal_id: str) -> s.SupervisorNotificationItem:
        """created -> presented -> acknowledged, reported by the supervisor client; never backwards."""
        now = self.clock()
        with self.store._tx() as c:
            row = c.execute("SELECT * FROM supervisor_notifications WHERE notification_id = ?",
                            (notification_id,)).fetchone()
            if row is None or row["site_id"] not in sites:
                raise NotFound("notification not found")
            rank = {"created": 0, "presented": 1, "acknowledged": 2}
            if rank[status] > rank[row["status"]]:
                if status == "presented":
                    c.execute("UPDATE supervisor_notifications SET status = 'presented', presented_at = ? WHERE"
                              " notification_id = ?", (iso(now), notification_id))
                else:
                    c.execute("UPDATE supervisor_notifications SET status = 'acknowledged', acknowledged_at = ?,"
                              " acknowledged_by = ?, presented_at = COALESCE(presented_at, ?) WHERE notification_id = ?",
                              (iso(now), principal_id, iso(now), notification_id))
                Store.feed_event(c, row["site_id"], "notification.created", "notification", notification_id, now)
            elif status == row["status"]:
                pass  # identical report: unchanged
            else:
                raise InvalidTransition(row["status"], f"notification is already {row['status']}")
            return notification_item(c.execute("SELECT * FROM supervisor_notifications WHERE notification_id = ?",
                                               (notification_id,)).fetchone())


def row_payload(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["payload_json"])
