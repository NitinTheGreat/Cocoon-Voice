"""Forecast-backed task reorder proposals (D2), applied only after a supervisor approval.

Smallest deterministic algorithm: the scheduled (movable) tasks of one shift are permuted (at most 6 tasks) over the
shift's existing time slots (the movable tasks' current start times, in order; a task starts at its slot or when the
previous one ends, never before the data clock or the expected end of the task in progress, so the current order
reproduces the current schedule), and each candidate is evaluated over every task's WHOLE interval (start, every 15 minutes, end) against the cached
forecast with C's working-conditions policy. A candidate is feasible only if every task ends inside the shift, keeps
its trusted dependencies and time windows, and every lookup is usable (unknown weather is never read as clear).
Scores are [blocks, acknowledgements, advisories] over tasks; a proposal is made only for a strictly better score,
otherwise the result explains why there is none. In-progress/completed tasks, operator and machine never change.

Creating a proposal changes nothing. At application the proposal is revalidated in the applying transaction: same
schedule version, same task versions and statuses, same forecast values for the calculation (identical cached
content is the same forecast; any change needs a new proposal). Then orders and start times are updated atomically,
the shift's schedule_version rises, the operator gets one `schedule_changed` announcement and the feed one
`tasks.changed` reference. Start checks and start estimates of started tasks are untouched.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .api import schemas as s
from .incident_time import offset_timezone
from .store import _TASK_SQL, Store, _assigned_task, canonical_json, iso, utcnow
from .weather import evaluate, rank

CONSTRAINTS_PATH = Path(__file__).resolve().parent.parent / "demo" / "task_constraints_v1.json"
PROPOSAL_TTL = timedelta(minutes=60)
STEP = timedelta(minutes=15)
MAX_MOVABLE = 6


class TaskConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine_id: str
    task_type: str
    after_task_types: list[str] = Field(default_factory=list)
    not_before_local: str | None = None
    not_after_local: str | None = None
    reason: str


class ConstraintSet(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_id: str = Field(alias="schema")
    version: str
    provenance: str
    constraints: list[TaskConstraint]


def load_constraints(path: Path | None = None) -> ConstraintSet:
    doc = json.loads((path or CONSTRAINTS_PATH).read_text(encoding="utf-8"))
    if doc.get("schema") != "cocoon.task-constraints.v1":
        raise ValueError("not a cocoon.task-constraints.v1 file")
    return ConstraintSet.model_validate(doc)


class Replanner:
    def __init__(self, store: Store, conditions, planner, constraints: ConstraintSet | None = None,
                 clock: Callable[[], datetime] = utcnow):
        self.store, self.conditions, self.planner, self.clock = store, conditions, planner, clock
        self.constraints = constraints or load_constraints()

    # ------------------------------------------------------------------ evaluation

    def _level(self, site, task: s.AssignedTask, start: datetime, minutes: float, now: datetime,
               seen: dict[str, Any]) -> str:
        end = start + timedelta(minutes=minutes)
        times, t = [], start
        while t < end:
            times.append(t)
            t += STEP
        times.append(end - timedelta(seconds=1))
        worst = "clear"
        for at in times:
            snap, coverage, reason = self.conditions.weather.lookup(site, at, now)
            check = evaluate(self.conditions.policy, snap, coverage, data_time=at, now=now, task_id=task.task_id,
                             task_type=task.task_type, outdoor=task.outdoor, reason=reason)
            if check.level == "not_applicable":
                continue
            if check.level == "unknown":
                return "unknown"
            if snap is not None:
                seen[snap.record_id] = snap
            if rank(check.level) > rank(worst):
                worst = check.level
        return worst

    @staticmethod
    def _score(levels: list[str]) -> list[int]:
        return [levels.count("block"), levels.count("acknowledge"), levels.count("advisory")]

    @staticmethod
    def _fingerprint(seen: dict[str, Any]) -> tuple[str, list[str], s.WeatherSnapshot | None]:
        values = [snap.model_dump(mode="json", exclude={"retrieved_at"}) for _, snap in sorted(seen.items())]
        digest = hashlib.sha256(canonical_json(values).encode()).hexdigest()
        first = next(iter(sorted(seen.items())), (None, None))[1]
        return digest, sorted(seen), first

    def _constraint(self, task: s.AssignedTask) -> TaskConstraint | None:
        return next((k for k in self.constraints.constraints
                     if k.machine_id == task.machine_id and k.task_type == task.task_type), None)

    @staticmethod
    def _local(shift: s.ShiftInfo, hhmm: str) -> datetime:
        day = datetime.fromisoformat(shift.service_date)
        h, m = (int(x) for x in hhmm.split(":"))
        return datetime(day.year, day.month, day.day, h, m, tzinfo=offset_timezone(shift.utc_offset))

    def _plan_inputs(self, shift_id: str):
        shift = self.store.get_shift(shift_id)
        if shift is None:
            return None, None, None, "no such shift"
        session = self.store._one("SELECT * FROM sessions WHERE shift_id = ? AND binding_status = 'catalog_verified'"
                                  " AND context_status = 'trusted_binding' ORDER BY created_at DESC LIMIT 1",
                                  (shift_id,))
        if session is None:
            return shift, None, None, "no verified session is bound to this shift"
        session = self.store.get_session(session["session_id"])
        tasks = [self.planner.enrich(session, t) for t in self.store.list_assigned_tasks(shift_id)]
        return shift, session, tasks, None

    def propose(self, shift_id: str) -> s.ScheduleProposalResult:
        from .approvals import approval_view

        now = self.clock()
        shift, session, tasks, problem = self._plan_inputs(shift_id)
        if problem:
            return s.ScheduleProposalResult(status="no_proposal", reason=problem)
        movable = [t for t in tasks if t.status == "scheduled"]
        if len(movable) < 2:
            return s.ScheduleProposalResult(status="no_proposal", reason="fewer than two tasks can still be moved")
        if len(movable) > MAX_MOVABLE:
            return s.ScheduleProposalResult(status="no_proposal", reason=f"more than {MAX_MOVABLE} movable tasks")
        minutes: dict[str, float] = {}
        for t in movable:
            m = t.estimate.predicted_minutes if t.estimate and t.estimate.predicted_minutes else t.duration.minutes
            if not m:
                return s.ScheduleProposalResult(status="no_proposal", reason=f"no duration estimate for {t.title}")
            minutes[t.task_id] = float(m)
        site = self.store.get_site(shift.site_id)
        seen: dict[str, Any] = {}
        before_levels = [self._level(site, t, t.scheduled_start_at, minutes[t.task_id], now, seen) for t in movable]
        before_items = [self._item(t, t.scheduled_order, t.scheduled_order, t.scheduled_start_at, t.scheduled_start_at,
                                   minutes[t.task_id], lvl, lvl, "unchanged") for t, lvl in zip(movable, before_levels)]
        if "unknown" in before_levels:
            return s.ScheduleProposalResult(status="no_proposal", evaluated=before_items,
                                            reason="the forecast does not cover the current schedule; nothing to "
                                                   "compare against")
        before = self._score(before_levels)
        anchor = self.conditions.data_time(session)
        running = next((t for t in tasks if t.status == "in_progress"), None)
        if running is not None and running.started_at is not None:
            est = running.start_estimate or running.estimate
            if est and est.predicted_minutes:
                anchor = max(anchor, running.started_at + timedelta(minutes=est.predicted_minutes))
        slots = sorted(t.scheduled_order for t in movable)
        times = sorted(t.scheduled_start_at for t in movable)
        best = None
        for perm in itertools.permutations(movable):
            placed = self._pack(shift, perm, minutes, times, anchor)
            if placed is None:
                continue
            trial: dict[str, Any] = {}
            levels = [self._level(site, t, start, minutes[t.task_id], now, trial) for t, start in placed]
            if "unknown" in levels:
                continue
            score = self._score(levels)
            moved = sum(1 for (t, _), slot in zip(placed, slots) if t.scheduled_order != slot)
            key = (score, moved, [t.task_id for t, _ in placed])
            if best is None or key < best[0]:
                best = (key, placed, levels, trial)
        if best is None or best[0][0] >= before:
            return s.ScheduleProposalResult(
                status="no_proposal", evaluated=before_items,
                reason="no feasible order (shift end, dependencies, time windows, forecast coverage) avoids more "
                       "adverse conditions than the current one")
        _, placed, levels, trial = best
        seen.update(trial)
        forecast_sha, record_ids, first = self._fingerprint(seen)
        by_id = {t.task_id: (t, lvl) for t, lvl in zip(movable, before_levels)}
        changes = []
        for (t, start), slot, lvl in zip(placed, slots, levels):
            old_lvl = by_id[t.task_id][1]
            if slot == t.scheduled_order and start == t.scheduled_start_at:
                reason = "unchanged"
            elif rank(lvl) < rank(old_lvl):
                reason = f"forecast over the whole task improves from {old_lvl} to {lvl}"
            else:
                reason = f"moved to make room; forecast over the task is {lvl}"
            changes.append(self._item(t, t.scheduled_order, slot, t.scheduled_start_at, start, minutes[t.task_id],
                                      old_lvl, lvl, reason))
        estimator_sha = next((t.estimate.config_sha256 for t in movable if t.estimate), None)
        proposal = s.ScheduleProposalView(
            shift_id=shift_id, schedule_version=self._schedule_version(shift_id),
            policy_version=self.conditions.policy.policy_version, estimator_config_sha256=estimator_sha,
            forecast=s.ForecastReference(provider=first.provider, kind=first.kind, record_ids=record_ids,
                                         content_sha256=forecast_sha, retrieved_at=first.retrieved_at),
            before_score=before, after_score=best[0][0], changes=changes, constraints_version=self.constraints.version)
        payload = {"kind": "schedule_change", "proposal": proposal.model_dump(mode="json"),
                   "task_versions": {t.task_id: t.version for t in movable},
                   "estimate_ids": {t.task_id: t.estimate.estimate_id for t in movable if t.estimate}}
        dedup = hashlib.sha256(canonical_json([shift_id, proposal.schedule_version, [c.task_id for c in changes],
                                               forecast_sha]).encode()).hexdigest()
        with self.store._tx() as c:
            existing = c.execute("SELECT * FROM approval_requests WHERE dedup_key = ? AND status = 'pending'",
                                 (dedup,)).fetchone()
            if existing is not None:  # unchanged inputs: the pending proposal stands (no churn)
                return s.ScheduleProposalResult(status="duplicate", approval=approval_view(c, existing))
            for old in c.execute("SELECT approval_id, site_id FROM approval_requests WHERE kind = 'schedule_change'"
                                 " AND status = 'pending' AND json_extract(payload_json, '$.proposal.shift_id') = ?",
                                 (shift_id,)).fetchall():
                c.execute("UPDATE approval_requests SET status = 'cancelled', version = version + 1, updated_at = ?,"
                          " application_reason = 'superseded by a newer proposal' WHERE approval_id = ?",
                          (iso(now), old["approval_id"]))
                if old["site_id"]:
                    Store.feed_event(c, old["site_id"], "approval.changed", "approval", old["approval_id"], now)
            approval_id = Store.insert_approval(
                c, session, kind="schedule_change", action_type="apply_schedule_change", payload=payload,
                proposer="planner:weather_reorder", ttl=PROPOSAL_TTL, dedup_key=dedup,
                resource_versions={"schedule_version": proposal.schedule_version, **payload["task_versions"]},
                evidence_refs={"forecast_records": record_ids, "estimates": payload["estimate_ids"]}, now=now)
            row = c.execute("SELECT * FROM approval_requests WHERE approval_id = ?", (approval_id,)).fetchone()
            return s.ScheduleProposalResult(status="proposed", approval=approval_view(c, row))

    def _schedule_version(self, shift_id: str) -> int:
        return self.store._one("SELECT schedule_version FROM shifts WHERE shift_id = ?", (shift_id,))[0]

    def _pack(self, shift: s.ShiftInfo, order, minutes: dict[str, float], times: list[datetime],
              anchor: datetime) -> list[tuple[s.AssignedTask, datetime]] | None:
        placed, cursor, done = [], anchor, set()
        for t, slot in zip(order, times):
            k = self._constraint(t)
            start = max(cursor, slot)
            if k is not None:
                if any(dep not in done for dep in k.after_task_types
                       if any(x.task_type == dep for x in order)):
                    return None
                if k.not_before_local:
                    start = max(start, self._local(shift, k.not_before_local))
                if k.not_after_local and start > self._local(shift, k.not_after_local):
                    return None
            end = start + timedelta(minutes=minutes[t.task_id])
            if end > shift.end_at:
                return None
            placed.append((t, start))
            done.add(t.task_type)
            cursor = end
        return placed

    @staticmethod
    def _item(t: s.AssignedTask, before_order: int, after_order: int, before_start: datetime, after_start: datetime,
              minutes: float, before_level: str, after_level: str, reason: str) -> s.ScheduleChangeItem:
        return s.ScheduleChangeItem(task_id=t.task_id, title=t.title, before_order=before_order,
                                    after_order=after_order, before_start_at=before_start, after_start_at=after_start,
                                    duration_minutes=round(minutes, 1), before_level=before_level,
                                    after_level=after_level, reason=reason)

    # ------------------------------------------------------------------ application

    def apply(self, c: sqlite3.Connection, row: sqlite3.Row, payload: dict[str, Any],
              now: datetime) -> tuple[str, str | None]:
        """Revalidate, then apply the exact approved schedule inside the caller's transaction."""
        proposal = s.ScheduleProposalView.model_validate(payload["proposal"])
        shift_row = c.execute("SELECT schedule_version FROM shifts WHERE shift_id = ?", (proposal.shift_id,)).fetchone()
        if shift_row is None or shift_row["schedule_version"] != proposal.schedule_version:
            return "failed_stale_inputs", "the shift schedule changed since the proposal"
        tasks = {}
        for change in proposal.changes:
            r = c.execute(_TASK_SQL + " WHERE t.task_id = ?", (change.task_id,)).fetchone()
            if r is None or r["status"] != "scheduled" or r["version"] != payload["task_versions"][change.task_id]:
                return "failed_stale_inputs", "a task started or changed since the proposal"
            tasks[change.task_id] = _assigned_task(r)
        site = self.store.get_site(c.execute("SELECT site_id FROM shifts WHERE shift_id = ?",
                                             (proposal.shift_id,)).fetchone()["site_id"])
        seen: dict[str, Any] = {}
        for change in proposal.changes:
            for start in {change.before_start_at, change.after_start_at}:
                if self._level(site, tasks[change.task_id], start, change.duration_minutes, now, seen) == "unknown":
                    return "failed_stale_inputs", "the forecast for the proposal is no longer available"
        if self._fingerprint(seen)[0] != proposal.forecast.content_sha256:
            return "failed_stale_inputs", "the forecast changed since the proposal; request a new proposal"
        moving = [ch for ch in proposal.changes if ch.before_order != ch.after_order
                  or ch.before_start_at != ch.after_start_at]
        for ch in moving:  # two phases: UNIQUE(shift_id, scheduled_order) holds between statements
            c.execute("UPDATE task_assignments SET scheduled_order = ? WHERE task_id = ?",
                      (-1000 - ch.after_order, ch.task_id))
        for ch in moving:
            c.execute("UPDATE task_assignments SET scheduled_order = ?, scheduled_start_at = ?, version = version + 1,"
                      " updated_at = ? WHERE task_id = ?", (ch.after_order, iso(ch.after_start_at), iso(now),
                                                            ch.task_id))
        c.execute("UPDATE shifts SET schedule_version = schedule_version + 1 WHERE shift_id = ?", (proposal.shift_id,))
        order = sorted(proposal.changes, key=lambda ch: ch.after_order)
        tz = offset_timezone(c.execute("SELECT si.utc_offset FROM shifts sh JOIN sites si USING (site_id) WHERE"
                                       " sh.shift_id = ?", (proposal.shift_id,)).fetchone()[0])
        speech = ("Your supervisor approved a new task order because of the forecast: " +
                  ", then ".join(f"{ch.title} at {ch.after_start_at.astimezone(tz):%H:%M}" for ch in order) + ".")
        sessions = c.execute("SELECT session_id FROM sessions WHERE shift_id = ? AND binding_status = 'catalog_verified'"
                             " ORDER BY created_at DESC LIMIT 1", (proposal.shift_id,)).fetchall()
        for srow in sessions:
            sid = srow["session_id"]
            seq = c.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM announcements WHERE session_id = ?",
                            (sid,)).fetchone()[0]
            c.execute("INSERT INTO announcements(event_id, session_id, sequence, type, priority, speech, alert_id,"
                      " created_at, expires_at) VALUES (?, ?, ?, 'schedule_changed', 'normal', ?, NULL, ?, ?)",
                      (f"ann_{row['approval_id']}_schedule", sid, seq, speech, iso(now),
                       iso(now + timedelta(hours=1))))
            c.execute("UPDATE sessions SET state_version = state_version + 1 WHERE session_id = ?", (sid,))
        Store.feed_event(c, row["site_id"], "tasks.changed", "shift", proposal.shift_id, now)
        return "applied", None
