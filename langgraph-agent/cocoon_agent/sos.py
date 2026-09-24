"""Durable human-impact check-ins and SOS notifications (D3).

State machine (one episode per operator at a time; `sos_transitions` records every step, its server time and rule):

    queued ──eligible offer (voice played / screen presented)──> offered
    queued ──offer deadline, no eligible offer──> unreachable (no_live_channel | offer_not_confirmed) + notify
    offered ──response deadline, no answer──> unresolved_no_response (no_response_after_offer) + notify
    queued|offered ──"I'm okay"──> okay          queued|offered ──"I need help"──> help_requested + notify at once
    unreachable|unresolved ──late answer──> same state + late_response (+ a separate recovery notification)

Deadlines are fixed from the server clock when set and never moved by later reports. Every mutation first applies any
deadline that is already due (so at the exact deadline the deadline wins: an answer or offer received at or after it
is late), then the event. Transitions are conditional on the row version (an atomic claim), and notifications are
unique per (kind, episode), so concurrent workers or retries cannot notify twice. A candidate older than
`max_age_seconds` is kept as history and never opens a current emergency; impacts while an episode is open merge
into it without touching its deadlines.

Notifications are in-app records under the versioned site emergency policy (`policies/emergency_notification_v1.json`):
no policy/recipient gives a visible `blocked_*` status, never a pretended notification. Nothing leaves the app.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .api import schemas as s
from .approvals import create_notification
from .store import Conflict, InvalidInput, InvalidTransition, NotFound, Store, iso, parse_dt, utcnow

POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "emergency_notification_v1.json"
PROMPT = "Are you okay? Say I'm okay, or I need help."
OPEN = ("queued", "offered")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SitePolicy(_Strict):
    enabled: bool
    recipients: str
    basis: str


class ImpactPolicy(_Strict):
    min_peak_accel_g: float
    max_age_seconds: int
    future_skew_seconds: int
    basis: str


class TimerProfile(_Strict):
    offer_wait_seconds: int
    response_window_seconds: int
    basis: str


class EmergencyPolicy(_Strict):
    schema_id: str = Field(alias="schema")
    policy_id: str
    policy_version: str
    note: str
    sites: dict[str, SitePolicy]
    impact: ImpactPolicy
    timer_profiles: dict[str, TimerProfile]


def load_emergency_policy(path: Path | None = None) -> EmergencyPolicy:
    doc = json.loads((path or POLICY_PATH).read_text(encoding="utf-8"))
    if doc.get("schema") != "cocoon.emergency-policy.v1":
        raise ValueError("not a cocoon.emergency-policy.v1 file")
    return EmergencyPolicy.model_validate(doc)


def _hash(obj: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Sos:
    def __init__(self, store: Store, policy: EmergencyPolicy, profile: str = "standard",
                 clock: Callable[[], datetime] = utcnow):
        if profile not in policy.timer_profiles:
            raise ValueError(f"unknown SOS timer profile {profile!r}")
        self.store, self.policy, self.profile, self.clock = store, policy, profile, clock
        self.timers = policy.timer_profiles[profile]

    # ------------------------------------------------------------------ intake

    def ingest(self, session: s.Session, req: s.HumanImpactCandidate) -> s.ImpactResult:
        if session.binding_status != "catalog_verified":
            raise NotFound("impacts are accepted for verified operator sessions only")
        now = self.clock()
        if req.observed_at > now + timedelta(seconds=self.policy.impact.future_skew_seconds):
            raise InvalidInput("observed_at", "is in the future relative to the server clock")
        digest = _hash(req.model_dump(mode="json"))
        site_id = session.site_id if session.context_status == "trusted_binding" else None
        with self.store._tx() as c:
            saved = c.execute("SELECT * FROM human_impacts WHERE operator_id = ? AND source_event_id = ?",
                              (session.operator_id, req.source_event_id)).fetchone()
            if saved is not None:
                if saved["request_hash"] != digest:
                    raise Conflict("source_event_id was already used with a different candidate")
                result = s.ImpactResult.model_validate_json(saved["result_json"])
                episode = self._row(c, saved["episode_id"]) if saved["episode_id"] else None
                return result.model_copy(update={"status": "duplicate",
                                                 "episode": self.view(c, episode) if episode else None})
            age = (now - req.observed_at).total_seconds()
            episode_id, reason = None, None
            if age > self.policy.impact.max_age_seconds:
                disposition = "historical"
                reason = (f"captured {int(age)} s before receipt (limit {self.policy.impact.max_age_seconds} s): kept "
                          "as history, no current check-in")
            elif req.quality == "poor" or req.peak_accel_g < self.policy.impact.min_peak_accel_g:
                disposition, reason = "below_threshold", "poor quality or below the demo trigger threshold"
            else:
                open_row = c.execute("SELECT * FROM sos_episodes WHERE operator_id = ? AND state IN ('queued',"
                                     " 'offered')", (session.operator_id,)).fetchone()
                if open_row is not None:
                    open_row = self._advance(c, open_row, now)
                if open_row is not None and open_row["state"] in OPEN:
                    episode_id, disposition = open_row["episode_id"], "merged"
                    c.execute("UPDATE sos_episodes SET impact_count = impact_count + 1, last_impact_at = MAX("
                              "last_impact_at, ?), updated_at = ? WHERE episode_id = ?",
                              (iso(req.observed_at), iso(now), episode_id))
                    reason = "merged into the open check-in; its deadlines are unchanged"
                else:
                    episode_id, disposition = self._open(c, session, site_id, req, now), "opened"
            episode = self._row(c, episode_id) if episode_id else None
            result = s.ImpactResult(source_event_id=req.source_event_id, status=disposition, reason=reason,
                                    episode=self.view(c, episode) if episode else None)
            c.execute("INSERT INTO human_impacts(operator_id, source_event_id, request_hash, session_id, site_id,"
                      " device_id, observed_at, received_at, peak_accel_g, duration_ms, orientation_after, quality,"
                      " provenance, disposition, episode_id, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                      " ?, ?, ?, ?)",
                      (session.operator_id, req.source_event_id, digest, session.session_id, site_id, req.device_id,
                       iso(req.observed_at), iso(now), req.peak_accel_g, req.duration_ms, req.orientation_after,
                       req.quality, req.provenance, disposition, episode_id,
                       result.model_copy(update={"episode": None}).model_dump_json()))
            return result

    def _open(self, c: sqlite3.Connection, session: s.Session, site_id: str | None, req: s.HumanImpactCandidate,
              now: datetime) -> str:
        episode_id = "SOS-" + uuid.uuid4().hex[:12]
        event_id = f"ann_{episode_id}_checkin"
        ttl = timedelta(seconds=self.timers.offer_wait_seconds + self.timers.response_window_seconds)
        seq = c.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM announcements WHERE session_id = ?",
                        (session.session_id,)).fetchone()[0]
        c.execute("INSERT INTO sos_episodes(episode_id, operator_id, session_id, site_id, state, provenance,"
                  " timer_profile, opened_at, first_impact_at, last_impact_at, checkin_event_id, offer_deadline_at,"
                  " updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
                  (episode_id, session.operator_id, session.session_id, site_id, req.provenance, self.profile,
                   iso(now), iso(req.observed_at), iso(req.observed_at), event_id,
                   iso(now + timedelta(seconds=self.timers.offer_wait_seconds)), iso(now)))
        c.execute("INSERT INTO announcements(event_id, session_id, sequence, type, priority, speech, alert_id,"
                  " created_at, expires_at) VALUES (?, ?, ?, 'sos_checkin', 'critical', ?, NULL, ?, ?)",
                  (event_id, session.session_id, seq, PROMPT, iso(now), iso(now + ttl)))
        c.execute("UPDATE sessions SET state_version = state_version + 1 WHERE session_id = ?", (session.session_id,))
        self._log(c, episode_id, None, "queued", now, "impact_candidate_opened")
        if site_id:
            Store.feed_event(c, site_id, "sos.changed", "sos_episode", episode_id, now)
        return episode_id

    # ------------------------------------------------------------------ transitions

    @staticmethod
    def _row(c: sqlite3.Connection, episode_id: str) -> sqlite3.Row | None:
        return c.execute("SELECT * FROM sos_episodes WHERE episode_id = ?", (episode_id,)).fetchone()

    @staticmethod
    def _log(c: sqlite3.Connection, episode_id: str, before: str | None, after: str, now: datetime, rule: str) -> None:
        seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM sos_transitions WHERE episode_id = ?",
                        (episode_id,)).fetchone()[0]
        c.execute("INSERT INTO sos_transitions(episode_id, seq, from_state, to_state, at, rule) VALUES (?, ?, ?, ?, ?,"
                  " ?)", (episode_id, seq, before, after, iso(now), rule))

    def _move(self, c: sqlite3.Connection, row: sqlite3.Row, to_state: str, rule: str, now: datetime,
              **fields: Any) -> sqlite3.Row:
        """Conditional on the version read: a concurrent writer that already moved the row wins; this one fails."""
        sets = ", ".join(f"{k} = ?" for k in fields)
        n = c.execute(f"UPDATE sos_episodes SET state = ?, version = version + 1, updated_at = ?"
                      f"{', ' + sets if sets else ''} WHERE episode_id = ? AND version = ?",
                      (to_state, iso(now), *fields.values(), row["episode_id"], row["version"])).rowcount
        if n != 1:
            raise Conflict("the check-in changed concurrently")
        self._log(c, row["episode_id"], row["state"], to_state, now, rule)
        if row["site_id"]:
            Store.feed_event(c, row["site_id"], "sos.changed", "sos_episode", row["episode_id"], now)
        return self._row(c, row["episode_id"])

    def _live_channels(self, c: sqlite3.Connection, operator_id: str, now: datetime) -> bool:
        return c.execute("SELECT 1 FROM presence p JOIN sessions se USING (session_id) WHERE se.operator_id = ? AND"
                         " se.binding_status = 'catalog_verified' AND p.expires_at > ? AND p.connection != 'offline'"
                         " AND (p.voice_available = 1 OR p.screen_available = 1) LIMIT 1",
                         (operator_id, iso(now))).fetchone() is not None

    def _advance(self, c: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> sqlite3.Row:
        """Apply a deadline that is already due (at the deadline instant the deadline wins)."""
        if row["state"] == "queued" and now >= parse_dt(row["offer_deadline_at"]):
            reason = "offer_not_confirmed" if self._live_channels(c, row["operator_id"], now) else "no_live_channel"
            row = self._move(c, row, "unreachable", "offer_deadline_passed", now, outcome_reason=reason,
                             closed_at=iso(now))
            return self._notify(c, row, "sos_unreachable", now)
        if row["state"] == "offered" and now >= parse_dt(row["response_deadline_at"]):
            row = self._move(c, row, "unresolved_no_response", "response_deadline_passed", now,
                             outcome_reason="no_response_after_offer", closed_at=iso(now))
            return self._notify(c, row, "sos_no_response", now)
        return row

    def offer(self, c: sqlite3.Connection, event_id: str, channel: str, evidence: str, now: datetime) -> bool:
        """An eligible offer: reported voice playback or on-screen presentation of this check-in's event."""
        row = c.execute("SELECT * FROM sos_episodes WHERE checkin_event_id = ?", (event_id,)).fetchone()
        if row is None:
            return False
        row = self._advance(c, row, now)
        if row["state"] != "queued":
            return False  # already offered (never extended) or already decided (a late report changes nothing)
        self._move(c, row, "offered", f"eligible_offer:{channel}", now, offered_at=iso(now), offer_channel=channel,
                   offer_evidence=evidence,
                   response_deadline_at=iso(now + timedelta(seconds=self.timers.response_window_seconds)))
        return True

    def offer_after_report(self, event_id: str, channel: str, evidence: str) -> bool:
        with self.store._tx() as c:
            return self.offer(c, event_id, channel, evidence, self.clock())

    def respond_mutation(self, session: s.Session, checkin_id: str,
                         response: str) -> Callable[[sqlite3.Connection], dict[str, Any]]:
        def mutate(c: sqlite3.Connection) -> dict[str, Any]:
            now = self.clock()
            row = c.execute("SELECT * FROM sos_episodes WHERE episode_id = ? AND operator_id = ?",
                            (checkin_id, session.operator_id)).fetchone()
            if row is None:
                raise NotFound("no such check-in for this operator")
            row = self._advance(c, row, now)
            event = "response_recorded"
            if row["state"] in OPEN:
                if response == "okay":
                    row = self._move(c, row, "okay", "operator_answer_before_deadline", now, response="okay",
                                     responded_at=iso(now), outcome_reason="operator_okay", closed_at=iso(now))
                else:
                    row = self._move(c, row, "help_requested", "operator_answer_before_deadline", now,
                                     response="help", responded_at=iso(now), outcome_reason="help_requested",
                                     closed_at=iso(now))
                    row = self._notify(c, row, "sos_help", now)
            elif row["state"] in ("unreachable", "unresolved_no_response"):
                if row["late_response"] is not None:
                    raise InvalidTransition(row["state"], f"a late answer ({row['late_response']}) is already recorded")
                c.execute("UPDATE sos_episodes SET late_response = ?, late_response_at = ?, version = version + 1,"
                          " updated_at = ? WHERE episode_id = ?", (response, iso(now), iso(now), checkin_id))
                self._log(c, checkin_id, row["state"], row["state"], now, f"late_response:{response}")
                row = self._row(c, checkin_id)
                row = self._notify(c, row, "sos_recovered" if response == "okay" else "sos_help", now)
                if row["site_id"]:
                    Store.feed_event(c, row["site_id"], "sos.changed", "sos_episode", checkin_id, now)
                event = "late_response_recorded"
            else:
                raise InvalidTransition(row["state"], f"this check-in was already answered ({row['response']})")
            view = self.view(c, row)
            return {"record_type": "sos_episode", "record_id": checkin_id, "event": event,
                    "summary": f"Recorded '{response}' for check-in {checkin_id}.", "sos": view.model_dump(mode="json")}

        return mutate

    def _notify(self, c: sqlite3.Connection, row: sqlite3.Row, kind: str, now: datetime) -> sqlite3.Row:
        site_id = row["site_id"]
        site = self.policy.sites.get(site_id) if site_id else None
        if site is None or not site.enabled:
            status = "blocked_no_policy"
        elif c.execute("SELECT 1 FROM principal_site_grants g JOIN principals p USING (principal_id) WHERE"
                       " g.site_id = ? AND g.revoked_at IS NULL AND p.kind = 'supervisor' LIMIT 1",
                       (site_id,)).fetchone() is None:
            status = "blocked_no_recipient"
        else:
            status = "notified"
            create_notification(c, site_id=site_id, kind=kind, source_type="sos_episode", source_id=row["episode_id"],
                                operator_id=row["operator_id"], summary=self._summary(c, row, kind, now), now=now,
                                priority="normal" if kind == "sos_recovered" else "urgent",
                                policy_id=self.policy.policy_id)
        keep = row["notify_status"] == "notified" and status != "notified"
        if not keep:
            c.execute("UPDATE sos_episodes SET notify_status = ? WHERE episode_id = ?", (status, row["episode_id"]))
        self._log(c, row["episode_id"], row["state"], row["state"], now, f"notify:{kind}:{status}")
        return self._row(c, row["episode_id"])

    def _summary(self, c: sqlite3.Connection, row: sqlite3.Row, kind: str, now: datetime) -> str:
        session = c.execute("SELECT machine_id, shift_id FROM sessions WHERE session_id = ?",
                            (row["session_id"],)).fetchone()
        zone = c.execute("SELECT z.name FROM task_assignments t JOIN site_zones z USING (site_zone_id) WHERE"
                         " t.shift_id = ? AND t.status = 'in_progress' LIMIT 1", (session["shift_id"],)).fetchone() \
            if session and session["shift_id"] else None
        who = f"Operator {row['operator_id']} on {session['machine_id'] if session else 'an unknown machine'}"
        where = f", task zone {zone[0]}" if zone else ""
        source = f"{row['provenance']} impact source"
        return {
            "sos_help": f"URGENT: {who}{where} asked for help after a possible impact ({source}). Check on them.",
            "sos_no_response": f"URGENT: {who}{where} did not answer a check-in offered by {row['offer_channel']} "
                               f"after a possible impact ({source}). Not a confirmed injury.",
            "sos_unreachable": f"URGENT: a check-in could not be offered to {who}{where} after a possible impact "
                               f"({source}; {row['outcome_reason'].replace('_', ' ')}).",
            "sos_recovered": f"Update: {who} answered \"I'm okay\" at {now:%H:%M} UTC after the earlier alert.",
        }[kind]

    # ------------------------------------------------------------------ worker

    def run_due(self, now: datetime | None = None) -> list[str]:
        """Apply every due deadline without waiting for new input (startup recovery and the background worker)."""
        now = now or self.clock()
        due = [r[0] for r in self.store._all(
            "SELECT episode_id FROM sos_episodes WHERE (state = 'queued' AND offer_deadline_at <= ?) OR"
            " (state = 'offered' AND response_deadline_at <= ?) ORDER BY opened_at", (iso(now), iso(now)))]
        moved = []
        for episode_id in due:
            try:
                with self.store._tx() as c:
                    row = self._row(c, episode_id)
                    before = row["state"]
                    if self._advance(c, row, now)["state"] != before:
                        moved.append(episode_id)
            except Conflict:
                continue  # another writer claimed it first
        return moved

    # ------------------------------------------------------------------ views

    def view(self, c: sqlite3.Connection, row: sqlite3.Row) -> s.SosEpisodeView:
        notes = [r[0] for r in c.execute("SELECT notification_id FROM supervisor_notifications WHERE source_type ="
                                         " 'sos_episode' AND source_id = ? ORDER BY created_at", (row["episode_id"],))]
        return s.SosEpisodeView(
            episode_id=row["episode_id"], operator_id=row["operator_id"], site_id=row["site_id"], state=row["state"],
            version=row["version"], provenance=row["provenance"], opened_at=parse_dt(row["opened_at"]),
            first_impact_at=parse_dt(row["first_impact_at"]), impact_count=row["impact_count"],
            checkin=s.SosCheckIn(checkin_id=row["episode_id"], event_id=row["checkin_event_id"], prompt=PROMPT),
            offer_deadline_at=parse_dt(row["offer_deadline_at"]), offered_at=parse_dt(row["offered_at"]),
            offer_channel=row["offer_channel"], response_deadline_at=parse_dt(row["response_deadline_at"]),
            response=row["response"], responded_at=parse_dt(row["responded_at"]), outcome_reason=row["outcome_reason"],
            notify_status=row["notify_status"], notification_ids=notes, late_response=row["late_response"],
            late_response_at=parse_dt(row["late_response_at"]), timer_profile=row["timer_profile"])

    def current(self, operator_id: str) -> s.SosEpisodeView | None:
        with self.store._lock:
            c = self.store._conn
            row = c.execute("SELECT * FROM sos_episodes WHERE operator_id = ? ORDER BY (state IN ('queued', 'offered'))"
                            " DESC, opened_at DESC LIMIT 1", (operator_id,)).fetchone()
            return self.view(c, row) if row else None

    def answerable(self, operator_id: str) -> s.SosEpisodeView | None:
        """The check-in a voice answer refers to: the open one, else a notified one still without a late answer."""
        with self.store._lock:
            c = self.store._conn
            row = c.execute("SELECT * FROM sos_episodes WHERE operator_id = ? AND (state IN ('queued', 'offered') OR"
                            " (state IN ('unreachable', 'unresolved_no_response') AND late_response IS NULL))"
                            " ORDER BY (state IN ('queued', 'offered')) DESC, opened_at DESC LIMIT 1",
                            (operator_id,)).fetchone()
            return self.view(c, row) if row else None

    @staticmethod
    def supervisor_items(c: sqlite3.Connection, site_id: str, episode_id: str | None = None) -> list[s.SupervisorSosItem]:
        sql = ("SELECT e.*, se.machine_id, se.shift_id FROM sos_episodes e JOIN sessions se USING (session_id)"
               " WHERE e.site_id = ?")
        args: list[Any] = [site_id]
        if episode_id:
            sql += " AND e.episode_id = ?"
            args.append(episode_id)
        rows = c.execute(sql + " ORDER BY (e.state IN ('queued', 'offered')) DESC, e.opened_at DESC LIMIT 20",
                         args).fetchall()
        out = []
        for r in rows:
            zone = c.execute("SELECT site_zone_id FROM task_assignments WHERE shift_id = ? AND status = 'in_progress'"
                             " LIMIT 1", (r["shift_id"],)).fetchone() if r["shift_id"] else None
            out.append(s.SupervisorSosItem(
                episode_id=r["episode_id"], operator_id=r["operator_id"], machine_id=r["machine_id"],
                site_zone_id=zone[0] if zone else None, state=r["state"], outcome_reason=r["outcome_reason"],
                notify_status=r["notify_status"], opened_at=parse_dt(r["opened_at"]),
                responded_at=parse_dt(r["responded_at"]), late_response=r["late_response"],
                provenance=r["provenance"]))
        return out

    def feed_projection(self, c: sqlite3.Connection, episode_id: str, site_id: str) -> dict[str, Any]:
        items = self.supervisor_items(c, site_id, episode_id)
        return items[0].model_dump(mode="json") if items else {"episode_id": episode_id, "removed": True}
