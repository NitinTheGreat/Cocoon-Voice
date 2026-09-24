"""Complete Batch B operator demo over HTTP, with no voice worker or UI (mock LLM mode, SIMULATED machine data).

    python scripts/demo_operator.py                      # seeds + starts an isolated backend in data/demo_run
    python scripts/demo_operator.py --run-id demo2       # a fresh session in the same demo database
    python scripts/demo_operator.py --base-url http://127.0.0.1:8010   # use a backend you already started

Sequence: create a catalog-verified session for EXC_DEMO_001 / OP_DEMO_1_1 (bound to today's seeded shift) -> read the
shift briefing -> list and start tasks -> log an incident with a supervisor request -> replay the Cat 320 belt/idle
scenario -> poll the warning -> "Why did you warn me?" -> "I'm waiting for a truck" -> confirm the automatic draft ->
read the linked lesson -> finish the scenario -> complete the task with a tap command.

Every request uses stable IDs derived from --run-id, so re-running the same run replays saved results instead of
repeating actions. Requests and responses are written to <data dir>/demo_transcript_<run>.json; the bearer token is
never printed or stored. The spawned backend uses its own data directory and never touches data/cocoon.db.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _http import client  # noqa: E402

from cocoon_agent.simulation import scenario_events  # noqa: E402

MACHINE, OPERATOR = "EXC_DEMO_001", "OP_DEMO_1_1"


class Demo:
    def __init__(self, c: httpx.Client, run: str, transcript: Path):
        self.c, self.run, self.transcript, self.log = c, run, transcript, []

    def call(self, step: str, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(3):  # identical retries are safe: every write carries a stable ID
            try:
                r = self.c.request(method, path, json=body)
                break
            except httpx.TransportError:
                if attempt == 2:
                    raise
                time.sleep(1)
        out = r.json()
        self.log.append({"step": step, "request": {"method": method, "path": path, "body": body},
                         "response": {"status": r.status_code, "body": out}})
        if r.status_code >= 400:
            raise SystemExit(f"{step}: HTTP {r.status_code} {out}")
        return out

    def say(self, n: int, text: str) -> dict:
        out = self.call(f"turn {n}", "POST", f"/v1/sessions/{self.sid}/turns",
                        {"turn_id": f"{self.run}-t{n:02d}", "text": text, "source": "text"})
        kinds = [a["type"] for a in out["actions"]]
        print(f"  operator: {text}\n  cocoon:   {out['speech']}\n            branch={out.get('branch')} "
              f"actions={kinds} saved={[x['kind'] + ':' + str(x['record_id']) for x in out['action_records']]}")
        return out

    def events(self, label: str) -> list[dict]:
        page = self.call(f"poll {label}", "GET", f"/v1/sessions/{self.sid}/events?after={self.cursor}")
        self.cursor = page["next_cursor"]
        for e in page["events"]:
            print(f"  [announcement #{e['sequence']} {e['type']}] {e['speech']}")
        return page["events"]

    def replay(self, label: str, events: list[dict]) -> None:
        for e in events:
            out = self.call(f"telemetry {label}", "POST", f"/v1/sessions/{self.sid}/telemetry", e)
            rd = e["readings"]
            print(f"  obs {e['observed_at'][11:19]} engine={rd['engine_on']} belt={rd['seatbelt_fastened']} "
                  f"state={rd['operating_state']} -> opened={len(out['alerts_opened'])} cleared="
                  f"{len(out['alerts_cleared'])} drafts={out['drafts_created']}{' (duplicate)' if out['duplicate'] else ''}")

    def run_demo(self, sim_start: datetime) -> None:
        print(f"\n== session ({MACHINE} / {OPERATOR})")
        session = self.call("create session", "POST", "/v1/sessions", {
            "client_session_key": f"lk:demo-{self.run}:{OPERATOR}", "room_name": f"demo-{self.run}",
            "participant_identity": OPERATOR, "operator_id": OPERATOR, "machine_id": MACHINE})
        self.sid, self.cursor = session["session_id"], 0
        print(f"  {self.sid} shift={session['shift_id']} context={session['context_status']}")
        if not session["shift_id"]:
            raise SystemExit("session is not bound to a seeded shift: seed today's demo site and set "
                             "SESSION_BINDINGS_PATH (the spawned backend does both)")
        self.events("briefing")

        print("\n== 1-2 tasks")
        self.say(1, "What are my tasks today?")
        self.say(2, "Start the next task")

        print("\n== 3 incident")
        self.say(3, "Log an incident: hydraulic hose leaking near the stockpile yard, high severity, "
                    "and tell my supervisor")

        scenario = scenario_events(MACHINE, "belt_idle", sim_start, self.run)
        print("\n== 4-5 machine observations -> warning + automatic draft")
        self.replay("belt unfastened", scenario[:4])
        self.events("warning")

        print("\n== 6 explanation")
        self.say(4, "Why did you warn me?")
        self.replay("prolonged idling", scenario[4:8])
        self.events("idle")
        self.say(5, "I'm waiting for a truck")

        print("\n== 7 confirm the automatic draft")
        self.say(6, "Confirm the draft")

        print("\n== 8 linked training")
        state = self.call("state", "GET", f"/v1/sessions/{self.sid}/state")
        for a in state["training_assignments"]:
            print(f"  assignment {a['assignment_id']} {a['lesson_id']} {a['status']} from episode "
                  f"{a['source_episode_id']}")
        self.say(7, "Read my seatbelt lesson")

        print("\n== 9 finish the scenario and complete the task (tap command)")
        self.replay("moving, belt fastened", scenario[8:])
        self.events("cleared")
        state = self.call("state", "GET", f"/v1/sessions/{self.sid}/state")
        task = next(t for t in state["assigned_tasks"] if t["status"] in ("in_progress", "completed"))
        done = self.call("complete task", "POST", f"/v1/sessions/{self.sid}/commands", {
            "command_id": f"{self.run}-complete-1", "kind": "task.complete",
            "expected_version": 2, "payload": {"task_id": task["task_id"]}})
        print(f"  {done['summary']} duplicate={done['duplicate']} version={done['task']['version']}")

        final = self.call("final state", "GET", f"/v1/sessions/{self.sid}/state")
        print("\n== final state")
        print(f"  tasks: {[(t['title'], t['status']) for t in final['assigned_tasks']]}")
        print(f"  incidents: {[(i['incident_id'], i['origin']) for i in final['incidents']]}")
        print(f"  drafts open: {len(final['incident_drafts'])}; approvals pending: "
              f"{[a['approval_id'] for a in final['pending_approvals']]}")
        print(f"  active alerts: {[a['alert_type'] for a in final['active_alerts']]}; idle reasons: "
              f"{[r['reason_text'] for r in final['idle_reasons']]}")
        print(f"  training: {[(a['lesson_id'], a['status']) for a in final['training_assignments']]}; "
              f"machine_state: {final['machine_state']['status']}")

    def save(self) -> None:
        self.transcript.write_text(json.dumps(self.log, indent=2), encoding="utf-8")
        print(f"\ntranscript: {self.transcript}")


def spawn(data_dir: Path, port: int, extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    env = {**os.environ, "COCOON_DATA_DIR": str(data_dir), "COCOON_LLM_MODE": "mock", "COCOON_HOST": "127.0.0.1",
           "COCOON_PORT": str(port), "SESSION_BINDINGS_PATH": str(data_dir / "demo" / "session_bindings.json"),
           **(extra_env or {})}
    seed_args = [sys.executable, str(SERVICE_DIR / "scripts" / "seed_demo.py")]
    if not (SERVICE_DIR.parent / "Cocoon_Dataset_v1" / "data" / "generated" / "manifest.json").is_file() \
            and "DATASET_ROOT" not in (extra_env or {}) and "DATASET_ROOT" not in os.environ:
        # No dataset on this machine: a labelled synthetic stand-in catalog with the same IDs (never dataset data).
        from cocoon_agent.demo_site import load_fixture, write_stand_in_catalog
        root = data_dir / "stand_in_catalog"
        env.update(DATASET_ROOT=str(root), DATASET_MANIFEST_SHA256=write_stand_in_catalog(root, load_fixture()))
        seed_args.append("--allow-stand-in-catalog")
        print("Cocoon_Dataset_v1 not found: using a labelled synthetic stand-in catalog (IDs only)")
    seeded = subprocess.run(seed_args, env=env, capture_output=True, text=True)
    if seeded.returncode != 0:
        raise SystemExit(f"seeding failed: {seeded.stderr.strip()}")
    print(f"seeded isolated demo database in {data_dir}")
    log = (data_dir / "backend.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "cocoon_agent"], cwd=SERVICE_DIR, env=env, stdout=log,
                            stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=2).json().get("status") == "ready":
                print(f"backend ready on http://127.0.0.1:{port} (mock mode, log {data_dir / 'backend.log'})")
                return proc
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.5)
    proc.terminate()
    raise SystemExit("backend did not become ready; see backend.log")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", help="use an already running backend instead of spawning one")
    ap.add_argument("--data-dir", type=Path, default=SERVICE_DIR / "data" / "demo_run",
                    help="isolated data directory for the spawned backend")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--run-id", default="demo1", help="stable ID prefix; reuse it to replay saved results")
    ap.add_argument("--sim-start", help="observation clock start (ISO 8601 with offset); default: now - 370 s")
    args = ap.parse_args()
    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    # The observation clock of a run is fixed the first time, so a re-run sends byte-identical observations.
    run_file = data_dir / f"demo_run_{args.run_id}.json"
    if args.sim_start:
        sim_start = datetime.fromisoformat(args.sim_start)
    elif run_file.is_file():
        sim_start = datetime.fromisoformat(json.loads(run_file.read_text(encoding="utf-8"))["sim_start"])
    else:
        sim_start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=370)
    run_file.write_text(json.dumps({"sim_start": sim_start.isoformat()}), encoding="utf-8")
    # The B flow runs on the wall clock, so it keeps weather off by default (the C demo pins a simulation clock and
    # uses fixture weather); set COCOON_WEATHER_MODE to override.
    proc = None if args.base_url else spawn(data_dir, args.port,
                                            {"COCOON_WEATHER_MODE": os.environ.get("COCOON_WEATHER_MODE", "off")})
    try:
        demo = Demo(client(args.base_url or f"http://127.0.0.1:{args.port}"), args.run_id,
                    data_dir / f"demo_transcript_{args.run_id}.json")
        try:
            demo.run_demo(sim_start)
        finally:
            demo.save()
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
