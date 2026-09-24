"""Batch C required-features demo over HTTP (mock LLM mode, fixture weather, SIMULATED machine data).

    python scripts/demo_required_features.py                    # spawns an isolated backend in data/demo_c
    python scripts/demo_required_features.py --run-id c2        # a fresh session in the same demo database
    python scripts/demo_required_features.py --base-url http://127.0.0.1:8010   # an already running backend

Reuses demo_operator.py (session/turn/telemetry helpers and the isolated, seeded backend). Sequence on the Cat 320 /
OP_DEMO_1_1 shift, on a simulation clock pinned to the site-local service date:
  assigned tasks with conditions and estimates -> working-condition check needing acknowledgement -> start anyway ->
  proximity warning then danger update -> saved explanation -> repeated belt violations -> pending escalation +
  assigned lesson -> coaching prompt when parked -> lesson steps, media metadata and file -> quiz answers and stored
  progress -> other rule families (sudden stop, slope, fuel per cycle, worsening conditions) -> incidents with
  severity/relative time -> task completion by tap -> the frozen estimator evaluation.

Same --run-id again replays saved results (stable IDs). Reading media metadata or downloading the file is recorded,
but it is neither playback nor lesson completion; audio and phone playback are not part of this demo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent))

from demo_operator import MACHINE, OPERATOR, SERVICE_DIR, Demo, client, spawn  # noqa: E402

from cocoon_agent.demo_site import load_fixture, site_today  # noqa: E402
from cocoon_agent.simulation import hazard_events  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def local(day: str, hhmm: str, seconds: int = 0) -> datetime:
    d = datetime.fromisoformat(day)
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST) + timedelta(seconds=seconds)


def reading(at: datetime, event_id: str, engine=True, belt=True, state="working", **extra) -> dict:
    return {"event_id": event_id, "observed_at": at.isoformat(), "simulated": True,
            "readings": {"engine_on": engine, "seatbelt_fastened": belt, "idle_seconds": 0, "operating_state": state,
                         **extra},
            "provenance": {"origin": "synthetic_scenario", "generator": "demo_required_features"}}


class CDemo(Demo):
    def replay_c(self, label: str, events: list[dict]) -> list[dict]:
        outs = []
        for e in events:
            out = self.call(f"telemetry {label}", "POST", f"/v1/sessions/{self.sid}/telemetry", e)
            outs.append(out)
            moved = [f"opened {len(out['alerts_opened'])}" if out["alerts_opened"] else "",
                     f"updated {len(out.get('alerts_updated', []))}" if out.get("alerts_updated") else "",
                     f"cleared {len(out['alerts_cleared'])}" if out["alerts_cleared"] else ""]
            if any(moved) or out["duplicate"]:
                print(f"  obs {e['observed_at'][11:19]} {' '.join(x for x in moved if x)}"
                      f"{' (duplicate)' if out['duplicate'] else ''}")
        return outs

    def run_c(self, day: str) -> None:
        print(f"\n== session ({MACHINE} / {OPERATOR}), simulation clock on {day} (site-local)")
        session = self.call("create session", "POST", "/v1/sessions", {
            "client_session_key": f"lk:democ-{self.run}:{OPERATOR}", "room_name": f"democ-{self.run}",
            "participant_identity": OPERATOR, "operator_id": OPERATOR, "machine_id": MACHINE})
        self.sid, self.cursor = session["session_id"], 0
        if not session["shift_id"]:
            raise SystemExit("session is not bound to a seeded shift")
        self.events("briefing")
        obs = lambda label, *events: self.replay_c(label, list(events))  # noqa: E731

        print("\n== 1 assigned tasks with conditions and estimates")
        obs("data clock", reading(local(day, "11:30"), f"{self.run}-clock"))
        state = self.call("state", "GET", f"/v1/sessions/{self.sid}/state")
        for t in state["assigned_tasks"]:
            est = t["start_estimate"] or t["estimate"] or {}
            cond = t["start_check"] or t["conditions"] or {}
            print(f"  {t['title']}: {t['status']}, conditions {cond.get('level')} ({cond.get('coverage')}), "
                  f"estimate {est.get('predicted_minutes')} min [{est.get('method')}]")
        self.say(1, "What are my tasks today?")
        self.say(2, "How long will my next task take?")

        print("\n== 2 working-condition check before the task")
        self.say(3, "What are the conditions like?")
        self.say(4, "Start the next task")
        self.say(5, "Start anyway")

        print("\n== 3 proximity warning, then a danger update in the same episode")
        obs("proximity", *hazard_events(MACHINE, "proximity_approach", local(day, "11:35"), self.run))
        self.events("proximity")
        self.say(6, "Why did you warn me about the person behind me?")

        print("\n== 4 repeated seatbelt violations -> pending escalation + assigned lesson")
        obs("repeat belt", *hazard_events(MACHINE, "repeat_belt", local(day, "11:37"), self.run))
        self.events("repeat")
        state = self.call("state", "GET", f"/v1/sessions/{self.sid}/state")
        print(f"  pending approvals: {[(a['kind'], a['status']) for a in state['pending_approvals']]}")
        print(f"  assignments: {[(a['lesson_id'], a['status'], a['source_episode_id']) for a in state['training_assignments']]}")

        print("\n== 5 parked with no warning -> coaching prompt; lesson, media and quiz")
        obs("parked", reading(local(day, "11:48"), f"{self.run}-parked", engine=False, state="off"))
        self.events("coaching")
        lesson = self.call("lesson L1", "GET", f"/v1/sessions/{self.sid}/lessons/L1")
        media = lesson["media"][0]
        r = self.c.get(media["content_ref"])
        ok = hashlib.sha256(r.content).hexdigest() == media["checksum_sha256"]
        self.log.append({"step": "media file", "request": {"method": "GET", "path": media["content_ref"]},
                         "response": {"status": r.status_code, "bytes": len(r.content), "sha256_matches": ok,
                                      "content_type": r.headers.get("content-type")}})
        print(f"  media {media['asset_id']}: {media['mime_type']} {media['duration_seconds']} s, {len(r.content)} "
              f"bytes, checksum matches: {ok} (download is not playback or completion)")
        self.say(7, "Start my lesson")
        for i in range(5):
            self.say(8 + i, "next")
        self.say(13, "Quiz me")
        self.say(14, "yes")  # never an answer
        self.say(15, "b")
        self.say(16, "stay in the seat, hold on and brace")
        self.say(17, "a")
        self.say(18, "How am I progressing?")

        print("\n== 6 other rule families and worsening conditions")
        obs("sudden stop", *hazard_events(MACHINE, "sudden_stop", local(day, "11:50"), self.run))
        obs("slope", *hazard_events(MACHINE, "slope", local(day, "11:51"), self.run))
        obs("fuel per cycle", *hazard_events(MACHINE, "fuel_high", local(day, "11:52"), self.run))
        self.events("rules")
        self.say(19, "Why did you warn me about the weather?")

        print("\n== 7 incidents: missing severity is asked for; relative time is interpreted")
        self.say(20, "Log an incident: the bucket clipped the fence ten minutes ago, high severity")
        self.say(21, "Report an incident: a hydraulic hose is weeping by the north pit")
        self.say(22, "medium")

        print("\n== 8 complete the task with a tap (the start estimate is kept)")
        state = self.call("state", "GET", f"/v1/sessions/{self.sid}/state")
        task = state["assigned_tasks"][0]  # the task started in step 2 (a replay sends the identical command)
        expected = task["version"] if task["status"] == "in_progress" else task["version"] - 1
        done = self.call("complete task", "POST", f"/v1/sessions/{self.sid}/commands", {
            "command_id": f"{self.run}-complete", "kind": "task.complete", "expected_version": expected,
            "payload": {"task_id": task["task_id"]}})
        print(f"  {done['summary']} duplicate={done['duplicate']}; started under conditions "
              f"{done['task']['start_check']['level']} (acknowledged {done['task']['start_check']['acknowledged']}), "
              f"start estimate {done['task']['start_estimate']['predicted_minutes']} min")

        final = self.call("final state", "GET", f"/v1/sessions/{self.sid}/state")
        print("\n== final state")
        print(f"  tasks: {[(t['title'], t['status']) for t in final['assigned_tasks']]}")
        print(f"  incidents: {[(i['incident_id'], i['severity'], i['severity_basis'], i['occurred_basis']) for i in final['incidents']]}")
        print(f"  active alerts: {[a['alert_type'] for a in final['active_alerts']]}")
        print(f"  learning: level {final['learning']['level']}, completed "
              f"{[x['lesson_id'] for x in final['learning']['lessons'] if x['status'] == 'completed']}")
        print(f"  rule coverage: {[(c['rule_id'], c['status']) for c in final['rule_coverage']]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url")
    ap.add_argument("--data-dir", type=Path, default=SERVICE_DIR / "data" / "demo_c")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--run-id", default="c1")
    args = ap.parse_args()
    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    day = site_today(load_fixture())
    run_file = data_dir / f"demo_run_{args.run_id}.json"
    if run_file.is_file():  # a replay keeps the original service date, so every request is byte-identical
        day = json.loads(run_file.read_text(encoding="utf-8"))["service_date"]
    run_file.write_text(json.dumps({"service_date": day}), encoding="utf-8")
    proc = None if args.base_url else spawn(data_dir, args.port, {"COCOON_WEATHER_MODE": "fixture"})
    try:
        demo = CDemo(client(args.base_url or f"http://127.0.0.1:{args.port}"), args.run_id,
                     data_dir / f"demo_transcript_{args.run_id}.json")
        try:
            demo.run_c(day)
        finally:
            demo.save()
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=15)
    print("\n== frozen estimator evaluation on the five provided rows")
    import importlib.util
    spec = importlib.util.spec_from_file_location("evaluate_estimator", SCRIPTS / "evaluate_estimator.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.evaluate()
    print(f"  baseline MAE {report['baseline']['mae_min']} min, estimator MAE {report['estimator']['mae_min']} min "
          f"(n={report['count']}, {report['audit']['label']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
