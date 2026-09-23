"""Post deterministic SIMULATED telemetry scenarios to a running backend (prototype, not machine data).

    python scripts/simulate_telemetry.py --room <livekit-room> --identity <participant> [--scenario seatbelt]
    python scripts/simulate_telemetry.py --session-id ses_...

The session is resolved with the same client_session_key the voice worker uses
(lk:<room>:<identity>), so operator/machine must match what the worker sent.
Works against the real backend or livekit-voice's mock backend (--base-url).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _http import client, ensure_session  # noqa: E402

# (engine_on, seatbelt_fastened, idle_seconds) per step
SCENARIOS = {
    # belted -> unbelted x3 samples (ONE announcement) -> belted (clear) -> unbelted again (new episode)
    "seatbelt": [(True, True, 0), (True, False, 0), (True, False, 5), (True, False, 10), (True, True, 12),
                 (True, False, 20)],
    "seatbelt-start": [(True, True, 0), (True, False, 0), (True, False, 5)],
    "seatbelt-clear": [(True, True, 0)],
    "engine-off": [(False, False, 0)],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url")
    ap.add_argument("--session-id")
    ap.add_argument("--room")
    ap.add_argument("--identity")
    ap.add_argument("--operator", help="defaults to --identity (the worker's default)")
    ap.add_argument("--machine", default="cat-320-demo")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="seatbelt")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between samples")
    ap.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"),
                    help="event_id prefix; reuse it to replay the exact same events idempotently")
    args = ap.parse_args()
    c = client(args.base_url)
    sid = args.session_id
    if not sid:
        if not (args.room and args.identity):
            ap.error("pass --session-id, or --room and --identity")
        sid = ensure_session(c, args.room, args.identity, args.operator or args.identity, args.machine)
    start = datetime.now(timezone.utc)
    steps = SCENARIOS[args.scenario]
    print(f"SIMULATED telemetry -> session {sid}, scenario {args.scenario}, run {args.run_id}")
    for i, (engine, belt, idle) in enumerate(steps):
        body = {
            "event_id": f"sim-{args.run_id}-{args.scenario}-{i}",
            "observed_at": (start + timedelta(seconds=i)).isoformat(),
            "simulated": True,
            "readings": {"engine_on": engine, "seatbelt_fastened": belt, "idle_seconds": idle},
        }
        r = c.post(f"/v1/sessions/{sid}/telemetry", json=body)
        r.raise_for_status()
        out = r.json()
        print(f"  #{i} engine_on={engine} seatbelt={belt} -> opened={out['alerts_opened']} "
              f"cleared={out['alerts_cleared']} announcements={out['announcements_created']}"
              f"{' (duplicate)' if out['duplicate'] else ''}")
        if i < len(steps) - 1:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
