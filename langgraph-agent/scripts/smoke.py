"""End-to-end HTTP smoke test against a RUNNING backend (no LiveKit, no audio).

    python scripts/smoke.py [--base-url http://127.0.0.1:8000]

Exercises every demo flow through the real HTTP API and exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _http import client, ensure_session  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url")
    args = ap.parse_args()
    run = uuid.uuid4().hex[:6]
    c = client(args.base_url)
    ready = c.get("/readyz").json()
    print(f"readyz: {ready}")
    sid = ensure_session(c, f"smoke-room-{run}", "smoke-operator", "smoke-operator", "cat-320-demo")
    print(f"session: {sid}")

    def say(tid: str, text: str) -> dict:
        r = c.post(f"/v1/sessions/{sid}/turns", json={"turn_id": f"{run}-{tid}", "text": text, "source": "text"})
        assert r.status_code == 200, r.text
        body = r.json()
        print(f"  > {text}\n  < {body['speech']}   {[a['type'] for a in body['actions']]}")
        return body

    t = say("1", "What's my next task?")
    assert t["actions"][0]["type"] == "next_task" and t["actions"][0]["task"]
    t = say("2", "I want to report an incident")
    assert t["actions"][0]["type"] == "information_requested"
    t = say("3", "The hydraulic hose on the boom is leaking")
    inc = t["actions"][0]["incident"]
    state = c.get(f"/v1/sessions/{sid}/state").json()
    assert inc["incident_id"] in [i["incident_id"] for i in state["incidents"]]
    again = c.post(f"/v1/sessions/{sid}/turns",
                   json={"turn_id": f"{run}-3", "text": "The hydraulic hose on the boom is leaking", "source": "text"})
    assert again.json() == t, "retry must return the stored result"
    assert len(c.get(f"/v1/sessions/{sid}/state").json()["incidents"]) == 1
    t = say("4", "Assign me the seatbelt lesson")
    assert t["actions"][0]["type"] == "training_assigned"

    now = datetime.now(timezone.utc)
    for i, belted in enumerate([True, False, False, False]):
        r = c.post(f"/v1/sessions/{sid}/telemetry", json={
            "event_id": f"{run}-tel-{i}", "observed_at": (now + timedelta(seconds=i)).isoformat(), "simulated": True,
            "readings": {"engine_on": True, "seatbelt_fastened": belted, "idle_seconds": 0}})
        assert r.status_code == 200, r.text
    events = c.get(f"/v1/sessions/{sid}/events", params={"after": 0}).json()["events"]
    assert [e["type"] for e in events] == ["alert_started"], events
    print(f"  announcement: {events[0]['speech']}")
    t = say("5", "Why did you warn me?")
    assert t["actions"][0]["alert"]["alert_id"] == events[0]["alert_id"]
    print(f"SMOKE OK (llm_mode={ready['llm_mode']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
