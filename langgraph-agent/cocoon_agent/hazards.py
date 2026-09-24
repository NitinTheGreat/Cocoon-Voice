"""PROTOTYPE hazard and behaviour rules on SIMULATED telemetry (Batch C2): proximity, sudden starts/stops, steep
slopes, fuel per load cycle and repeated violations, driven by policies/hazard_rules_v1.json.

Every threshold is a labelled demo assumption, not a published or site limit. The rules read observations only
(never scenario names), run inside the observation transaction without any model call, and say `unknown` whenever
their input is missing, stale, too sparse or not configured for the machine: unknown is never treated as safe.

Per family:
- proximity: one episode per detected entity; opens at the warning distance, rises to danger within the same
  episode (announced once), clears only when the entity is observed beyond the clear distance with usable quality.
  A missing detection is not proof the entity left: the track becomes `lost`, and the episode ends as
  `expired_without_detection` after the policy's expiry, with a spoken reminder to look around.
- sudden starts/stops: instant events from consecutive timestamped speed samples (positive steps, bounded gaps);
  samples already evaluated are never counted again.
- steep slope: model-specific pitch/roll limits under working/travel context; no configured limit or no reading is
  unknown/not_configured; grade % is converted to degrees only when pitch is absent.
- fuel per cycle: cumulative meters accumulated over a covered window of the in-progress task; resets and gaps are
  explicit; zero cycles is idle fuel (left to the idle rules); only a configured machine/task baseline is compared.
- repeated violations: distinct episodes per family in an observation-time window; overlapping episodes of one family
  count once; each trigger creates one pending supervisor review and one coaching assignment.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .api import schemas as s
from .store import RuleOutcome, Store, iso, parse_dt, utcnow

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "policies" / "hazard_rules_v1.json"
SOURCE = "demo_assumption"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProximityPolicy(_Strict):
    rule_id: str
    entity_types: list[str]
    warning_distance_m: float
    danger_distance_m: float
    clear_distance_m: float
    track_lost_seconds: int
    expire_without_detection_seconds: int
    category_overrides: dict[str, dict[str, float]] = Field(default_factory=dict)
    basis: str
    notes: str

    def limits(self, category: str | None) -> tuple[float, float, float]:
        o = self.category_overrides.get(category or "", {})
        return (o.get("warning_distance_m", self.warning_distance_m), o.get("danger_distance_m", self.danger_distance_m),
                o.get("clear_distance_m", self.clear_distance_m))


class MotionPolicy(_Strict):
    start_rule_id: str
    stop_rule_id: str
    min_samples: int
    max_gap_ms: int
    start_accel_mps2: float
    stop_decel_mps2: float
    basis: str
    notes: str


class SlopePolicy(_Strict):
    rule_id: str
    operating_states: list[str]
    clear_margin_deg: float
    limits_by_category: dict[str, dict[str, float]]
    basis: str
    notes: str


class Baseline(_Strict):
    category: str
    task_type: str
    litres_per_cycle: float


class FuelPolicy(_Strict):
    rule_id: str
    min_window_seconds: int
    max_gap_seconds: int
    min_coverage: float
    min_cycles: int
    abnormal_ratio: float
    clear_ratio: float
    baselines: list[Baseline]
    basis: str
    notes: str

    def baseline(self, category: str | None, task_type: str | None) -> Baseline | None:
        return next((b for b in self.baselines if b.category == category and b.task_type == task_type), None)


class RepeatPolicy(_Strict):
    rule_id: str
    families: list[str]
    window_seconds: int
    threshold: int
    lesson_by_family: dict[str, str]
    basis: str
    notes: str


class HazardPolicy(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_id: Literal["cocoon.hazard-policy.v1"] = Field(alias="schema")
    policy_version: str
    note: str
    sources: list[dict[str, str]]
    proximity: ProximityPolicy
    sudden_motion: MotionPolicy
    slope: SlopePolicy
    fuel_per_cycle: FuelPolicy
    repeated_violations: RepeatPolicy


def load_hazard_policy(path: Path | None = None) -> HazardPolicy:
    return HazardPolicy.model_validate(json.loads((path or DEFAULT_PATH).read_text(encoding="utf-8")))


def direction(bearing: float) -> str:
    """Machine-relative bearing (0 = ahead, clockwise) as a spoken direction."""
    b = bearing % 360
    return "ahead of" if b < 45 or b >= 315 else "to the right of" if b < 135 else "behind" if b < 225 else \
        "to the left of"


def _ms(t: datetime) -> str:
    return t.strftime("%H:%M:%S.%f")[:-3]


def _coverage(rule_id: str, family: str, status: str, reason: str | None, at: datetime) -> dict[str, Any]:
    return {"rule_id": rule_id, "family": family, "status": status, "reason": reason, "observed_at": iso(at)}


class HazardEngine:
    def __init__(self, policy: HazardPolicy):
        self.policy = policy

    def _outcome(self, rule_id: str, family: str, alert_type: str, held: bool | None, **kw: Any) -> RuleOutcome:
        base = dict(rule_id=rule_id, family=family, alert_type=alert_type, severity="warning",
                    message="", reason="", recommended_action="", start_speech="", clear_speech=None,
                    policy_version=self.policy.policy_version, source_status=SOURCE, held=held, explanation="",
                    details={})
        base.update(kw)
        return RuleOutcome(**base)

    # ------------------------------------------------------------------ one observation

    def evaluate(self, c: sqlite3.Connection, session: s.Session, req: s.TelemetryRequest, category: str | None,
                 task: s.AssignedTask | None, state: dict[str, Any]) -> tuple[list[RuleOutcome], dict[str, Any]]:
        """All C2 families except repeat counting, for one applied observation. Returns outcomes and the new rule
        state (carried in machine_state.rule_state_json)."""
        outcomes: list[RuleOutcome] = []
        coverage: list[dict[str, Any]] = []
        new_state = dict(state)
        outcomes += self._proximity(c, session, req, category, coverage)
        motion, new_state["motion_last_t"] = self._motion(req, state.get("motion_last_t"), coverage)
        outcomes += motion
        outcomes += self._slope(req, category, coverage)
        fuel, new_state["fuel"] = self._fuel(req, category, task, state.get("fuel"), coverage)
        outcomes += fuel
        new_state["coverage"] = coverage
        return outcomes, new_state

    # ------------------------------------------------------------------ proximity

    def _proximity(self, c, session, req, category, coverage) -> list[RuleOutcome]:
        p = self.policy.proximity
        warn, danger, clear_at = p.limits(category)
        at = req.observed_at
        out: list[RuleOutcome] = []
        active = {r["subject_key"]: r for r in c.execute(
            "SELECT alert_id, subject_key, level, last_seen_at FROM alerts WHERE session_id = ? AND rule_id = ?"
            " AND status = 'active'", (session.session_id, p.rule_id))}
        scan = req.readings.proximity
        seen: set[str] = set()
        if scan is None:
            coverage.append(_coverage(p.rule_id, "proximity", "unknown", "no detector data in this observation", at))
        else:
            coverage.append(_coverage(p.rule_id, "proximity", "evaluated", None, at))
            for d in scan:
                if d.entity_type not in p.entity_types:
                    continue
                seen.add(d.entity_id)
                level = "danger" if d.distance_m <= danger else "warning" if d.distance_m <= warn else None
                where = f"{d.distance_m:g} metres {direction(d.bearing_deg)} the machine"
                what = "A person" if d.entity_type == "person" else "A vehicle"
                details = {"detection": d.model_dump(mode="json"), "thresholds_m": {"warning": warn, "danger": danger,
                           "clear": clear_at}, "machine_category": category}
                if level is not None:
                    out.append(self._outcome(
                        p.rule_id, "proximity", "proximity", True, subject_key=d.entity_id, level=level,
                        severity="critical" if level == "danger" else "warning",
                        priority="critical" if level == "danger" else "high",
                        message=f"{d.entity_type.capitalize()} within the {level} zone (simulated detection).",
                        reason=f"{what} was detected {where}, inside the demo {level} distance.",
                        recommended_action=("Stop all movement and make eye contact before continuing." if
                                            level == "danger" else "Slow down and locate them before moving."),
                        start_speech=(f"Danger: {what.lower()} {where}. Stop now." if level == "danger"
                                      else f"Caution: {what.lower()} {where}."),
                        escalate_speech=f"Danger: {what.lower()} is now {where}. Stop now.",
                        clear_speech=f"The {d.entity_type} is now clear of the machine.",
                        explanation=(f"At {at:%H:%M:%S} UTC a simulated detection ({d.quality} quality) put "
                                     f"{what.lower()} {where}; demo limits are {warn:g} m warning and {danger:g} m "
                                     "danger. This is a prototype rule on simulated data, not a validated detector."),
                        details=details, touch=True))
                elif d.distance_m >= clear_at and d.quality != "poor":
                    out.append(self._outcome(p.rule_id, "proximity", "proximity", False, subject_key=d.entity_id,
                                             clear_reason="observed_clear", details=details,
                                             clear_speech=f"The {d.entity_type} is now clear of the machine."))
                # between the warning and clear distances, or a poor-quality far reading: no change (hysteresis)
        lost = []
        for key, row in active.items():
            if key in seen:
                continue
            last = parse_dt(row["last_seen_at"])
            age = (at - last).total_seconds() if last is not None else 0
            if age >= p.track_lost_seconds:
                lost.append(f"{key} not detected for {age:.0f} s")
            if last is not None and age >= p.expire_without_detection_seconds:
                out.append(self._outcome(
                    p.rule_id, "proximity", "proximity", False, subject_key=key,
                    clear_reason="expired_without_detection",
                    clear_speech=("I've lost track of the person or vehicle near you. That doesn't mean they've gone: "
                                  "look around before you move.")))
        if lost:  # a lost track is not proof the entity left: say so in the coverage
            coverage[-1]["reason"] = "; ".join(filter(None, [coverage[-1]["reason"],
                                                               "lost (not proof they left): " + ", ".join(lost)]))
        return out

    # ------------------------------------------------------------------ sudden starts / stops

    def _motion(self, req, last_t: str | None, coverage) -> tuple[list[RuleOutcome], str | None]:
        m = self.policy.sudden_motion
        at = req.observed_at
        window = req.readings.motion
        if window is None:
            for rid in (m.start_rule_id, m.stop_rule_id):
                coverage.append(_coverage(rid, rid.split(".")[1], "unknown", "no high-rate motion samples", at))
            return [], last_t
        samples = sorted(window.samples, key=lambda x: x.t)
        prev_end = parse_dt(last_t) if last_t else None
        problem = None
        if any(b.t <= a.t for a, b in zip(window.samples, window.samples[1:])):
            problem = "samples are not in strictly increasing time order"
        elif len(samples) < m.min_samples:
            problem = f"fewer than {m.min_samples} samples"
        elif any((b.t - a.t).total_seconds() * 1000 > m.max_gap_ms for a, b in zip(samples, samples[1:])):
            problem = f"a gap longer than {m.max_gap_ms} ms between samples"
        if problem:
            for rid in (m.start_rule_id, m.stop_rule_id):
                coverage.append(_coverage(rid, rid.split(".")[1], "unknown", problem, at))
            return [], last_t
        steps = []
        for a, b in zip(samples, samples[1:]):
            if prev_end is not None and b.t <= prev_end:
                continue  # already evaluated in an earlier observation: never counted twice
            dt = (b.t - a.t).total_seconds()
            steps.append(((b.speed_mps - a.speed_mps) / dt, a, b))
        for rid in (m.start_rule_id, m.stop_rule_id):
            coverage.append(_coverage(rid, rid.split(".")[1], "evaluated", None if steps else
                                      "no new samples since the last observation", at))
        out: list[RuleOutcome] = []
        if steps:
            accel, a1, b1 = max(steps, key=lambda x: x[0])
            decel, a2, b2 = min(steps, key=lambda x: x[0])
            if accel >= m.start_accel_mps2:
                out.append(self._event(m.start_rule_id, "sudden_start", accel, a1, b1, samples,
                                       "That was a sudden start. Pull away smoothly unless it's an emergency."))
            if -decel >= m.stop_decel_mps2:
                out.append(self._event(m.stop_rule_id, "sudden_stop", -decel, a2, b2, samples,
                                       "That was a sudden stop. Brake smoothly unless it's an emergency."))
        return out, iso(samples[-1].t) if prev_end is None or samples[-1].t > prev_end else last_t

    def _event(self, rule_id, kind, value, a, b, samples, speech) -> RuleOutcome:
        m = self.policy.sudden_motion
        limit = m.start_accel_mps2 if kind == "sudden_start" else m.stop_decel_mps2
        word = "accelerated" if kind == "sudden_start" else "decelerated"
        return self._outcome(
            rule_id, kind, kind, True, subject_key=f"{b.t.isoformat()}", instant=True, priority="normal",
            message=f"{kind.replace('_', ' ').capitalize()} (simulated motion samples).",
            reason=f"The machine {word} at {value:.1f} m/s² between two samples {int((b.t - a.t).total_seconds() * 1000)}"
                   f" ms apart (demo limit {limit} m/s²).",
            recommended_action="Use smooth throttle and brake inputs unless avoiding a hazard.", start_speech=speech,
            explanation=(f"Between {_ms(a.t)} and {_ms(b.t)} UTC the simulated speed went from {a.speed_mps:g} to "
                         f"{b.speed_mps:g} m/s, {value:.1f} m/s² against a demo limit of {limit} m/s². This is a "
                         "prototype rule on simulated data."),
            details={"acceleration_mps2": round(value if kind == "sudden_start" else -value, 3),
                     "limit_mps2": limit, "from": a.model_dump(mode="json"), "to": b.model_dump(mode="json"),
                     "window_samples": len(samples), "max_gap_ms": m.max_gap_ms})

    # ------------------------------------------------------------------ steep slope

    def _slope(self, req, category, coverage) -> list[RuleOutcome]:
        p = self.policy.slope
        at = req.observed_at
        r = req.readings
        limits = p.limits_by_category.get(category or "")
        if limits is None:
            coverage.append(_coverage(p.rule_id, "steep_slope", "not_configured",
                                      f"no configured slope limit for {category or 'this machine'}", at))
            return []
        if not r.engine_on or r.operating_state not in p.operating_states:
            coverage.append(_coverage(p.rule_id, "steep_slope", "not_applicable",
                                      "machine not working or travelling", at))
            return []
        pitch, derived = r.pitch_deg, None
        if pitch is None and r.grade_pct is not None:
            pitch, derived = math.degrees(math.atan(r.grade_pct / 100)), "grade_pct"
        axes = {"pitch_deg": pitch, "roll_deg": r.roll_deg}
        if all(v is None for v in axes.values()):
            coverage.append(_coverage(p.rule_id, "steep_slope", "unknown", "no tilt reading", at))
            return []
        coverage.append(_coverage(p.rule_id, "steep_slope", "evaluated", None, at))
        over = {k: v for k, v in axes.items() if v is not None and k in limits and abs(v) > limits[k]}
        within = all(v is not None and abs(v) <= limits[k] - p.clear_margin_deg for k, v in axes.items() if k in limits)
        details = {"pitch_deg": round(pitch, 2) if pitch is not None else None, "roll_deg": r.roll_deg,
                   "pitch_derived_from": derived, "grade_pct": r.grade_pct, "limits_deg": limits,
                   "machine_category": category}
        if over:
            what = ", ".join(f"{'pitch' if k == 'pitch_deg' else 'side tilt'} {abs(v):.0f} degrees over the "
                             f"{limits[k]:g} degree demo limit" for k, v in over.items())
            return [self._outcome(
                p.rule_id, "steep_slope", "steep_slope", True, message="Steep slope (simulated tilt).",
                reason=f"Tilt beyond the demo limit for a {category}: {what}.",
                recommended_action="Reduce the angle: change your route or work across a gentler face.",
                start_speech=f"Steep slope: {what}. Reduce the angle or change your route.",
                clear_speech="Back within the slope limit.",
                explanation=(f"At {at:%H:%M:%S} UTC the simulated tilt showed {what}"
                             + (" (pitch derived from grade percent)" if derived else "")
                             + ". The limit is a demo assumption per machine model, not an OEM limit."),
                details=details)]
        if within:
            return [self._outcome(p.rule_id, "steep_slope", "steep_slope", False, details=details,
                                  clear_speech="Back within the slope limit.")]
        return []  # between the limit and the clearing margin, or a missing axis: no change

    # ------------------------------------------------------------------ fuel per load cycle

    def _fuel(self, req, category, task, acc: dict | None, coverage) -> tuple[list[RuleOutcome], dict | None]:
        p = self.policy.fuel_per_cycle
        at = req.observed_at
        r = req.readings
        if r.fuel_meter_l is None or r.load_cycles_total is None:
            coverage.append(_coverage(p.rule_id, "abnormal_fuel_per_cycle", "unknown", "no fuel/cycle meters", at))
            return [], acc
        base = p.baseline(category, task.task_type if task else None)
        if task is None or base is None:
            why = "no task in progress" if task is None else f"no eligible baseline for {category}/{task.task_type}"
            coverage.append(_coverage(p.rule_id, "abnormal_fuel_per_cycle", "not_applicable", why, at))
            return [], _fresh_window(at, r, task)
        if not acc or acc.get("task_id") != task.task_id:
            coverage.append(_coverage(p.rule_id, "abnormal_fuel_per_cycle", "evaluated", "window started", at))
            return [], _fresh_window(at, r, task)
        last_at = parse_dt(acc["last_at"])
        dt = (at - last_at).total_seconds()
        if r.fuel_meter_l < acc["last_fuel"] or r.load_cycles_total < acc["last_cycles"]:
            coverage.append(_coverage(p.rule_id, "abnormal_fuel_per_cycle", "unknown",
                                      "meter reset: window discarded", at))
            return [], _fresh_window(at, r, task, resets=acc.get("resets", 0) + 1)
        acc = dict(acc)
        if 0 < dt <= p.max_gap_seconds and r.engine_on:
            acc["fuel_l"] += r.fuel_meter_l - acc["last_fuel"]
            acc["cycles"] += r.load_cycles_total - acc["last_cycles"]
            acc["covered_s"] += dt
        elif dt > p.max_gap_seconds:
            acc["gaps"] = acc.get("gaps", 0) + 1  # the interval is excluded (fuel, cycles and coverage alike)
        acc.update(last_at=iso(at), last_fuel=r.fuel_meter_l, last_cycles=r.load_cycles_total)
        elapsed = (at - parse_dt(acc["start"])).total_seconds()
        if elapsed < p.min_window_seconds:
            coverage.append(_coverage(p.rule_id, "abnormal_fuel_per_cycle", "evaluated",
                                      f"window {elapsed:.0f}/{p.min_window_seconds} s", at))
            return [], acc
        covered = acc["covered_s"] / elapsed if elapsed else 0.0
        window = {"start": acc["start"], "end": iso(at), "covered_fraction": round(covered, 3),
                  "fuel_l": round(acc["fuel_l"], 3), "cycles": acc["cycles"], "gaps_excluded": acc.get("gaps", 0),
                  "task_id": task.task_id, "task_type": task.task_type, "machine_category": category,
                  "baseline_l_per_cycle": base.litres_per_cycle}
        nxt = _fresh_window(at, r, task)  # tumbling windows
        if covered < p.min_coverage:
            coverage.append(_coverage(p.rule_id, "abnormal_fuel_per_cycle", "unknown",
                                      f"window only {covered:.0%} covered", at))
            return [], nxt
        if acc["cycles"] == 0:
            coverage.append(_coverage(p.rule_id, "abnormal_fuel_per_cycle", "not_applicable",
                                      "zero load cycles: idle fuel, handled by the idle rules", at))
            return [], nxt
        if acc["cycles"] < p.min_cycles:
            coverage.append(_coverage(p.rule_id, "abnormal_fuel_per_cycle", "unknown",
                                      f"only {acc['cycles']} cycles in the window", at))
            return [], nxt
        per_cycle = acc["fuel_l"] / acc["cycles"]
        ratio = per_cycle / base.litres_per_cycle
        window.update(litres_per_cycle=round(per_cycle, 3), ratio=round(ratio, 2))
        coverage.append(_coverage(p.rule_id, "abnormal_fuel_per_cycle", "evaluated", None, at))
        held = True if ratio >= p.abnormal_ratio else False if ratio <= p.clear_ratio else None
        if held is None:
            return [], nxt
        return [self._outcome(
            p.rule_id, "abnormal_fuel_per_cycle", "abnormal_fuel_per_cycle", held, priority="normal",
            message="Fuel per load cycle above the demo baseline (simulated meters).",
            reason=f"{per_cycle:.2f} litres per cycle over {elapsed / 60:.0f} minutes, {ratio:.1f} times the demo "
                   f"baseline of {base.litres_per_cycle:g} for {task.task_type.replace('_', ' ')}.",
            recommended_action="Check for idling inside the cycle, over-revving or a restricted filter.",
            start_speech=(f"Fuel use is high: about {per_cycle:.2f} litres a cycle, {ratio:.1f} times the demo "
                          "baseline for this task. Check for idling in the cycle or over-revving."),
            clear_speech="Fuel use per cycle is back near the baseline.",
            explanation=(f"From {parse_dt(window['start']):%H:%M} to {at:%H:%M} UTC the simulated meters showed "
                         f"{window['fuel_l']} litres over {window['cycles']} cycles ({covered:.0%} of the window "
                         f"covered): {per_cycle:.2f} litres per cycle against a demo baseline of "
                         f"{base.litres_per_cycle:g} for this machine and task."),
            details=window)], nxt

    # ------------------------------------------------------------------ repeated violations

    def repeat_outcomes(self, c: sqlite3.Connection, session: s.Session, req: s.TelemetryRequest) -> list[RuleOutcome]:
        p = self.policy.repeated_violations
        at = req.observed_at
        since = at - timedelta(seconds=p.window_seconds)
        rows = c.execute("SELECT a.alert_id, a.started_at, a.cleared_at, a.rule_id, a.alert_type FROM alerts a"
                         " WHERE a.session_id = ? AND a.correlated_alert_id IS NULL AND a.started_at > ?"
                         " AND a.started_at <= ? ORDER BY a.started_at", (session.session_id, iso(since), iso(at)))
        by_family: dict[str, list[tuple[datetime, datetime, str]]] = {}
        for r in rows:
            family = _FAMILY_OF.get(r["alert_type"])
            if family in p.families:
                start = parse_dt(r["started_at"])
                end = parse_dt(r["cleared_at"]) or at
                by_family.setdefault(family, []).append((start, max(end, start), r["alert_id"]))
        active = {r["subject_key"] for r in c.execute(
            "SELECT subject_key FROM alerts WHERE session_id = ? AND rule_id = ? AND status = 'active'",
            (session.session_id, p.rule_id))}
        out = []
        for family in p.families:
            merged = _merge(by_family.get(family, []))
            n = len(merged)
            held = n >= p.threshold
            if not held and family not in active:
                continue
            lesson = p.lesson_by_family.get(family)
            name = _SPOKEN.get(family, family.replace("_", " "))
            ids = [m[2] for m in merged]
            out.append(self._outcome(
                p.rule_id, "repeated_violations", "repeated_violations", held, subject_key=family,
                message=f"Repeated {name} episodes (simulated).",
                reason=f"{n} distinct {name} episodes within {p.window_seconds // 60} minutes of observation time "
                       f"(demo threshold {p.threshold}).",
                recommended_action="Review the lesson assigned for this and talk it through with your supervisor.",
                start_speech=(f"That's {n} {name} warnings in the last {p.window_seconds // 60} minutes. I've asked "
                              "for a supervisor review, which is pending, and assigned a short coaching lesson."),
                clear_speech=None, priority="normal",
                explanation=(f"{n} distinct {name} episodes started between {since:%H:%M} and {at:%H:%M} UTC "
                             f"(overlapping episodes counted once; demo threshold {p.threshold} per "
                             f"{p.window_seconds // 60} minutes)."),
                details={"family": family, "count": n, "window_seconds": p.window_seconds, "threshold": p.threshold,
                         "episode_ids": ids, "lesson_id": lesson},
                on_open=_repeat_links(session, family, lesson, ids)))
        return out


_FAMILY_OF = {"seatbelt_unfastened": "seatbelt_engine_on", "proximity": "proximity", "sudden_start": "sudden_start",
              "sudden_stop": "sudden_stop", "steep_slope": "steep_slope"}
_SPOKEN = {"seatbelt_engine_on": "seatbelt", "proximity": "proximity", "sudden_start": "sudden start",
           "sudden_stop": "sudden stop", "steep_slope": "steep slope"}


def _merge(episodes: list[tuple[datetime, datetime, str]]) -> list[tuple[datetime, datetime, str]]:
    """Overlapping episodes of one family are one occurrence (e.g. two people detected together)."""
    merged: list[tuple[datetime, datetime, str]] = []
    for start, end, alert_id in sorted(episodes):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end), merged[-1][2])
        else:
            merged.append((start, end, alert_id))
    return merged


def _repeat_links(session: s.Session, family: str, lesson_id: str | None, episode_ids: list[str]):
    """On a new repeat trigger: one pending supervisor review and one coaching assignment, linked to the episode."""
    def hook(c: sqlite3.Connection, alert_id: str) -> dict[str, Any]:
        approval_id = "APR-" + uuid.uuid4().hex[:12]
        c.execute("INSERT INTO approval_requests(approval_id, session_id, operator_id, kind, alert_id, details_json,"
                  " created_at) VALUES (?, ?, ?, 'repeated_violations', ?, ?, ?)",
                  (approval_id, session.session_id, session.operator_id, alert_id,
                   json.dumps({"family": family, "episode_ids": episode_ids}), iso(utcnow())))
        assignment_id = Store._episode_assignment(c, session, lesson_id, alert_id) if lesson_id else None
        if assignment_id:
            c.execute("UPDATE alerts SET training_assignment_id = ? WHERE alert_id = ?", (assignment_id, alert_id))
        return {"approval_id": approval_id, "training_assignment_id": assignment_id}
    return hook


def _fresh_window(at: datetime, r: s.TelemetryReadings, task: s.AssignedTask | None,
                  resets: int = 0) -> dict[str, Any] | None:
    if task is None:
        return None
    return {"task_id": task.task_id, "start": iso(at), "last_at": iso(at), "last_fuel": r.fuel_meter_l,
            "last_cycles": r.load_cycles_total, "fuel_l": 0.0, "cycles": 0, "covered_s": 0.0, "gaps": 0,
            "resets": resets}
