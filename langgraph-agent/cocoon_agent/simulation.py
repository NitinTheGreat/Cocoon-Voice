"""Simulated machine observations for replay against the telemetry endpoint (prototype data, not a real machine).

Two sources, both labelled in `provenance`:
- `synthetic_scenario`: small second-level change-event sequences (engine, belt, operating state, speed). The dataset
  has minute-level history only, so belt-before-motion ordering is generated here, not inferred from aggregates.
- `dataset_replay`: rows of the pinned dataset's `history_minutes.csv` for the chosen machine, re-timed onto the
  chosen simulation start while keeping their spacing.

Event IDs are stable for a given run ID, so re-sending a run is idempotent. The rules never see scenario names.
"""

from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

GENERATOR = "cocoon_agent.simulation.v1"

# (offset seconds, engine_on, seatbelt_fastened, operating_state, speed_kph)
Step = tuple[int, bool, bool, str, float]

SCENARIOS: dict[str, list[Step]] = {
    # Detailed Cat 320 sequence: the belt is unfastened while idling (warning before any motion), idling continues
    # past the demo idle limits, then the machine moves still unbelted, and finally the belt is fastened.
    "belt_idle": [
        (0, False, True, "off", 0.0),
        (5, True, True, "idle", 0.0),
        (15, True, False, "idle", 0.0),     # belt unfastened with the engine running -> seatbelt episode
        (20, True, False, "idle", 0.0),     # repeated sample: same episode, no new announcement
        (45, True, False, "idle", 0.0),
        (80, True, False, "idle", 0.0),     # idle and unbelted for 75 s -> correlated episode, not re-announced
        (200, True, False, "idle", 0.0),
        (320, True, False, "idle", 0.0),    # idling 315 s of observation time -> prolonged idle episode
        (330, True, False, "travel", 3.0),  # first motion: the belt warning was already raised
        (340, True, True, "travel", 3.0),   # belt fastened -> seatbelt episode clears
        (360, True, True, "working", 0.0),
    ],
    # Clear and retrigger: two distinct belt violations are two episodes.
    "belt_retrigger": [
        (0, True, True, "working", 0.0),
        (5, True, False, "working", 0.0),
        (10, True, False, "working", 0.0),
        (15, True, True, "working", 0.0),
        (20, True, False, "working", 0.0),
    ],
    # Small selection/isolation check used for every other asset.
    "selection_check": [
        (0, True, True, "idle", 0.0),
        (5, True, False, "idle", 0.0),
        (10, True, True, "idle", 0.0),
    ],
}


def _event(run_id: str, machine_id: str, scenario: str, i: int, at: datetime, readings: dict[str, Any],
           origin: str, record_ref: str | None = None) -> dict[str, Any]:
    return {
        "event_id": f"sim-{run_id}-{machine_id}-{scenario}-{i:03d}",
        "observed_at": at.isoformat(),
        "simulated": True,
        "readings": readings,
        "provenance": {"origin": origin, "generator": GENERATOR, "record_ref": record_ref},
    }


def scenario_events(machine_id: str, scenario: str, start: datetime, run_id: str, seed: int = 0) -> list[dict]:
    """Deterministic for (scenario, start, run_id, seed). The seed adds sub-second jitter to observation times."""
    rng = random.Random(f"{seed}:{machine_id}:{scenario}")
    events = []
    idle_since: int | None = None
    for i, (offset, engine, belt, state, speed) in enumerate(SCENARIOS[scenario]):
        at = start + timedelta(seconds=offset, milliseconds=rng.randint(0, 400))
        idle = engine and state == "idle"
        idle_since = (offset if idle_since is None else idle_since) if idle else None
        readings = {"engine_on": engine, "seatbelt_fastened": belt, "operating_state": state, "speed_kph": speed,
                    "idle_seconds": offset - idle_since if idle_since is not None else 0}
        events.append(_event(run_id, machine_id, scenario, i, at, readings, "synthetic_scenario"))
    return events


def dataset_events(dataset_root: Path, machine_id: str, start: datetime, run_id: str, day: str | None = None,
                   limit: int = 30) -> list[dict]:
    """Minute rows of the pinned dataset for one machine (first `day` if not given), re-timed onto `start`."""
    path = dataset_root / "data" / "generated" / "history_minutes.csv"
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["machine_id"] != machine_id or (day and row["day"] != day):
                continue
            day = day or row["day"]
            rows.append(row)
            if len(rows) >= limit:
                break
    if not rows:
        raise ValueError(f"no dataset rows for {machine_id} on {day or 'any day'}")
    first = datetime.fromisoformat(rows[0]["observed_at"].replace("Z", "+00:00"))
    events = []
    for i, row in enumerate(rows):
        at = start + (datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")) - first)
        readings = {"engine_on": row["engine_on"] == "true", "seatbelt_fastened": row["seatbelt_fastened"] == "true",
                    "operating_state": row["operating_state"] or None,
                    "speed_kph": float(row["speed_kph"]) if row["speed_kph"] else None,
                    "idle_seconds": min(int(float(row["consecutive_idle_seconds"] or 0)), 86_400)}
        events.append(_event(run_id, machine_id, "dataset", i, at, readings, "dataset_replay", row["record_id"]))
    return events


# ---------------------------------------------------------------------------------------------- Batch C hazards
# Small synthetic scenarios for the C2 rule families, each with its normal and clearing path. Readings carry the
# extra fields the rules need (proximity scans, 100 ms speed windows, tilt, cumulative fuel/cycle meters); units:
# metres, degrees, m/s, litres. Nothing here is a real detector, IMU or fuel meter.

def _base(state: str = "working", belt: bool = True, speed: float = 0.0) -> dict[str, Any]:
    return {"engine_on": True, "seatbelt_fastened": belt, "operating_state": state, "speed_kph": speed,
            "idle_seconds": 0}


def _person(entity: str, distance: float, bearing: float = 180.0, quality: str = "good") -> dict[str, Any]:
    return {"entity_id": entity, "entity_type": "person", "distance_m": distance, "bearing_deg": bearing,
            "quality": quality, "source": "synthetic_scenario"}


def _window(at: datetime, speeds: list[float], step_ms: int = 100) -> dict[str, Any]:
    first = at - timedelta(milliseconds=step_ms * (len(speeds) - 1))
    return {"samples": [{"t": (first + timedelta(milliseconds=step_ms * i)).isoformat(), "speed_mps": v}
                        for i, v in enumerate(speeds)], "source": "synthetic_scenario"}


def _hazard_steps(scenario: str, start: datetime) -> list[tuple[int, dict[str, Any]]]:
    at = lambda s: start + timedelta(seconds=s)  # noqa: E731
    if scenario == "proximity_approach":  # warning -> danger (one episode) -> retreat -> observed clear
        return [(0, {**_base(), "proximity": []}),
                (2, {**_base(), "proximity": [_person("P1", 11.0)]}),
                (4, {**_base(), "proximity": [_person("P1", 8.0)]}),
                (6, {**_base(), "proximity": [_person("P1", 4.0)]}),
                (8, {**_base(), "proximity": [_person("P1", 4.5)]}),
                (10, {**_base(), "proximity": [_person("P1", 9.0)]}),
                (12, {**_base(), "proximity": [_person("P1", 15.0)]})]
    if scenario == "proximity_lost":  # a detection that disappears: lost, never "left", expires after the policy time
        return [(0, {**_base(), "proximity": [_person("P2", 7.0, 90.0)]}),
                (5, {**_base(), "proximity": []}),
                (30, _base()),                      # no detector data at all: unknown
                (61, {**_base(), "proximity": []})]
    if scenario == "sudden_stop":  # smooth, then a hard stop, then a gappy window (unknown), then a repeat window
        return [(0, {**_base("travel", speed=10.8), "motion": _window(at(0), [3.0, 3.0, 2.95, 2.9])}),
                (1, {**_base("travel", speed=6.1), "motion": _window(at(1), [2.9, 2.5, 2.1, 1.7])}),
                (2, {**_base("travel", speed=6.1), "motion": _window(at(2), [1.7, 1.5, 1.2], step_ms=800)}),
                (3, {**_base("travel", speed=6.1), "motion": _window(at(1), [2.9, 2.5, 2.1, 1.7])})]
    if scenario == "slope":  # over the model limit, inside the hysteresis band, then clear; idle on a slope = n/a
        return [(0, {**_base("travel"), "pitch_deg": 10.0, "roll_deg": 2.0}),
                (5, {**_base("travel"), "pitch_deg": 17.0, "roll_deg": 2.0}),
                (10, {**_base("travel"), "pitch_deg": 14.0, "roll_deg": 2.0}),
                (15, {**_base("travel"), "pitch_deg": 12.0, "roll_deg": 2.0}),
                (20, {**_base("idle"), "grade_pct": 40.0})]
    if scenario in ("fuel_high", "fuel_normal"):  # 11 one-minute samples of cumulative meters during a task
        per_cycle = 0.9 if scenario == "fuel_high" else 0.45
        return [(60 * i, {**_base(), "fuel_meter_l": round(1200.0 + per_cycle * 2 * i, 3),
                          "load_cycles_total": 5000 + 2 * i}) for i in range(12)]
    if scenario == "fuel_reset":  # the meter goes backwards: the window is discarded, nothing is compared
        return [(0, {**_base(), "fuel_meter_l": 1200.0, "load_cycles_total": 5000}),
                (60, {**_base(), "fuel_meter_l": 1201.8, "load_cycles_total": 5002}),
                (120, {**_base(), "fuel_meter_l": 3.6, "load_cycles_total": 4})]
    if scenario == "repeat_belt":  # three distinct belt violations within the repeat window
        steps = []
        for k in range(3):
            steps += [(k * 120, _base()), (k * 120 + 10, _base(belt=False)), (k * 120 + 40, _base())]
        return steps
    if scenario == "normal_operation":  # everything within limits and fully observed: nothing should open
        return [(i * 5, {**_base("working", speed=3.6), "proximity": [_person("P9", 30.0)], "pitch_deg": 4.0,
                         "roll_deg": 1.0, "motion": _window(at(i * 5), [1.0, 1.05, 1.1, 1.15])}) for i in range(4)]
    raise KeyError(scenario)


HAZARD_SCENARIOS = ("proximity_approach", "proximity_lost", "sudden_stop", "slope", "fuel_high", "fuel_normal",
                    "fuel_reset", "repeat_belt", "normal_operation")


def hazard_events(machine_id: str, scenario: str, start: datetime, run_id: str) -> list[dict]:
    """Deterministic for (scenario, start, run_id): stable event IDs make a re-send idempotent."""
    return [_event(run_id, machine_id, scenario, i, start + timedelta(seconds=offset), readings, "synthetic_scenario")
            for i, (offset, readings) in enumerate(_hazard_steps(scenario, start))]
