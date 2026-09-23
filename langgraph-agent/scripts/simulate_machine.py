"""Replay SIMULATED machine observations for one catalog machine through the authenticated telemetry endpoint.

    python scripts/simulate_machine.py --machine EXC_DEMO_001 --scenario belt_idle --start 2026-09-24T07:40:00+05:30
    python scripts/simulate_machine.py --machine DOZ_DEMO_001 --scenario selection_check --pace 0
    python scripts/simulate_machine.py --machine LDR_DEMO_001 --scenario dataset --limit 20

Selects the machine's operator from the seeded demo fixture unless --operator is given, and resolves the session with
the voice worker's key format (lk:<room>:<identity>), so a seeded shift is bound automatically; or pass --session-id.
Observation times start at --start (the simulation clock, independent of wall-clock time); --pace only spaces the
HTTP posts. Re-running with the same --run-id re-sends identical event IDs, which the backend treats as duplicates.
Prototype data only: never use it to protect people on a real machine.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _http import client, ensure_session  # noqa: E402

from cocoon_agent.config import Settings  # noqa: E402
from cocoon_agent.demo_site import load_fixture  # noqa: E402
from cocoon_agent.simulation import SCENARIOS, dataset_events, scenario_events  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url")
    ap.add_argument("--machine", default="EXC_DEMO_001", help="catalog asset ID (any of the five demo machines)")
    ap.add_argument("--operator", help="catalog operator ID (default: the machine's operator in the demo fixture)")
    ap.add_argument("--session-id")
    ap.add_argument("--room", help="LiveKit room name (default demo-<machine>)")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS) + ["dataset"], default="belt_idle")
    ap.add_argument("--start", help="simulation start (ISO 8601 with offset); default: now")
    ap.add_argument("--pace", type=float, default=0.5, help="wall-clock seconds between posts (0 = as fast as possible)")
    ap.add_argument("--seed", type=int, default=0, help="jitter seed (deterministic)")
    ap.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"))
    ap.add_argument("--day", help="dataset scenario: dataset day (YYYY-MM-DD); default the machine's first day")
    ap.add_argument("--limit", type=int, default=30, help="dataset scenario: number of minute rows")
    args = ap.parse_args()

    operator = args.operator
    if not operator:
        pairs = {a["machine_id"]: a["operator_id"] for a in load_fixture()["assignments"]}
        if args.machine not in pairs:
            ap.error(f"{args.machine} is not in the demo fixture; pass --operator")
        operator = pairs[args.machine]
    start = datetime.fromisoformat(args.start) if args.start else datetime.now(timezone.utc).replace(microsecond=0)
    if start.tzinfo is None:
        ap.error("--start needs a UTC offset, e.g. 2026-09-24T07:40:00+05:30")

    c = client(args.base_url)
    sid = args.session_id or ensure_session(c, args.room or f"demo-{args.machine}", operator, operator, args.machine)
    if args.scenario == "dataset":
        events = dataset_events(Settings().dataset_root, args.machine, start, args.run_id, args.day, args.limit)
    else:
        events = scenario_events(args.machine, args.scenario, start, args.run_id, args.seed)
    print(f"SIMULATED observations -> session {sid} machine {args.machine} operator {operator} "
          f"scenario {args.scenario} run {args.run_id} start {start.isoformat()}")
    for i, body in enumerate(events):
        r = c.post(f"/v1/sessions/{sid}/telemetry", json=body)
        r.raise_for_status()
        out = r.json()
        rd = body["readings"]
        note = " (duplicate)" if out["duplicate"] else (f" (ignored: {out['ignored_reason']})" if out["stale"] else "")
        print(f"  {body['observed_at']} engine={rd['engine_on']} belt={rd['seatbelt_fastened']} "
              f"state={rd['operating_state']} speed={rd['speed_kph']} -> opened={out['alerts_opened']} "
              f"cleared={out['alerts_cleared']} drafts={out['drafts_created']} "
              f"announcements={out['announcements_created']}{note}")
        if args.pace and i < len(events) - 1:
            time.sleep(args.pace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
