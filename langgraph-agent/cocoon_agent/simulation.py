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
