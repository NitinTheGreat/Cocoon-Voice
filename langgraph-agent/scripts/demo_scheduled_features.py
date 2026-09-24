"""Batch D scheduled-features demo over real HTTP (mock LLM, fixture weather, SIMULATED sensors, synthetic actors).

    python scripts/demo_scheduled_features.py                 # isolated backend in data/demo_d, run ID d1
    python scripts/demo_scheduled_features.py                 # same run ID again: stable requests replay
    python scripts/demo_scheduled_features.py --run-id d2     # a fresh run in the same demo database

Steps (operator 1 = OP_DEMO_1_1 on EXC_DEMO_001, operator 2 = OP_DEMO_2_1 on DOZ_DEMO_001, supervisor sup-north granted
SITE_DEMO_NORTH, sup-south granted only the synthetic SITE_DEMO_SOUTH):
  1 scoped actors, no implicit consent, no risk shared, foreign-site denial
  2 processing consent -> synthetic wellbeing samples -> one advice episode + operator explanation; supervisor sees
    no risk until sharing is granted; sharing revoked -> current and replayed feed reads show it unavailable
  3 repeated seatbelt episodes -> pending review -> scoped approval, one notification; identical retry and a
    contradictory decision
  4 forecast change -> explained no-proposal, proposal, rejection, stale approval (schedule intact), fresh approval
    applied once, operator announcement
  5 SOS: okay after voice offer, immediate help, offered-but-unanswered, unreachable, and a real backend restart with
    a pending deadline (accelerated demo timers, simulated impact source)
  6 offline client: cached snapshot, draft captured while "disconnected", upload twice, lost-response lookup,
    replacement session on another machine, original-machine record, stale task command, screen receipt
  7 same-ID replay: counts of consent changes, drafts, decisions, schedule applications and notifications unchanged

Tokens are written to private files under the demo data directory and never printed. The transcript
(<data dir>/demo_d_transcript_<run>.json) contains requests and responses without bearer tokens.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

SCRIPTS = Path(__file__).resolve().parent
SERVICE_DIR = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SERVICE_DIR))

from _http import client  # noqa: E402
from demo_operator import spawn  # noqa: E402

from cocoon_agent.config import Settings  # noqa: E402
from cocoon_agent.demo_site import load_fixture, site_today, write_stand_in_catalog  # noqa: E402
from cocoon_agent.simulation import hazard_events  # noqa: E402
from cocoon_agent.store import Store  # noqa: E402
from cocoon_agent.token_admin import main as token_admin  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SITE = "SITE_DEMO_NORTH"
OP1, M1, OP2, M2 = "OP_DEMO_1_1", "EXC_DEMO_001", "OP_DEMO_2_1", "DOZ_DEMO_001"
DEMO_DIR = SERVICE_DIR / "demo"
NOTICES = json.loads((SERVICE_DIR / "policies" / "consent_notices_v1.json").read_text(encoding="utf-8"))["notices"]


def local(day: str, hhmm: str, seconds: int = 0) -> datetime:
    d = datetime.fromisoformat(day)
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST) + timedelta(seconds=seconds)


def admin_settings(data_dir: Path) -> Settings:
    overrides = {"COCOON_DATA_DIR": str(data_dir)}
    if not (SERVICE_DIR.parent / "Cocoon_Dataset_v1" / "data" / "generated" / "manifest.json").is_file():
        root = data_dir / "stand_in_catalog"
        overrides.update(DATASET_ROOT=str(root), DATASET_MANIFEST_SHA256=write_stand_in_catalog(root, load_fixture()))
    return Settings(**overrides)


def token(settings: Settings, data_dir: Path, name: str, *argv: str) -> str:
    """Issue once per demo database through the real CLI; the token only ever lives in its private file."""
    path = data_dir / "tokens" / f"{name}.token"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        import io
        out, err = io.StringIO(), io.StringIO()
        if token_admin(["issue", *argv, "--ttl-hours", "72", "--out", str(path)], settings=settings, out=out,
                       err=err) != 0:
            raise SystemExit(f"token issue refused for {name}: {err.getvalue().strip()}")
    return path.read_text(encoding="ascii").strip()


def grant(settings: Settings, principal: str, site: str) -> None:
    import io
    if token_admin(["grant-site", "--principal-id", principal, "--site-id", site], settings=settings,
                   out=io.StringIO(), err=io.StringIO()) != 0:
        raise SystemExit(f"grant-site refused for {principal}")


class DDemo:
    def __init__(self, base: str, run: str, data_dir: Path, tokens: dict[str, str], state: dict):
        self.base, self.run, self.data_dir, self.state, self.log = base, run, data_dir, state, []
        self.tokens = tokens
        self.http = {who: client(base, t) for who, t in tokens.items()}
        self.checks: list[tuple[str, bool]] = []

    def call(self, who: str, step: str, method: str, path: str, body: dict | None = None,
             expect: int | tuple[int, ...] = 200) -> dict:
        for attempt in range(20):
            try:
                r = self.http[who].request(method, path, json=body)
                break
            except httpx.TransportError:
                if attempt == 19:
                    raise
                time.sleep(0.5)
        out = r.json() if r.content else {}
        self.log.append({"step": step, "as": who, "request": {"method": method, "path": path, "body": body},
                         "response": {"status": r.status_code, "body": out}})
        ok = r.status_code in ((expect,) if isinstance(expect, int) else expect)
        if not ok:
            raise SystemExit(f"{step}: HTTP {r.status_code} {json.dumps(out)[:400]}")
        return out

    def check(self, label: str, ok: bool) -> None:
        self.checks.append((label, ok))
        print(f"  [{'ok' if ok else 'FAILED'}] {label}")

    def say(self, who: str, sid: str, tid: str, text: str) -> dict:
        out = self.call(who, f"turn {tid}", "POST", f"/v1/sessions/{sid}/turns",
                        {"turn_id": f"{self.run}-{tid}", "text": text, "source": "voice"})
        print(f"  operator: {text}\n  cocoon:   {out['speech']}")
        return out

    def telemetry(self, sid: str, event_id: str, at: datetime, belt: bool = True, engine: bool = True) -> dict:
        return self.call("svc", "telemetry", "POST", f"/v1/sessions/{sid}/telemetry", {
            "event_id": f"{self.run}-{event_id}", "observed_at": at.isoformat(), "simulated": True,
            "readings": {"engine_on": engine, "seatbelt_fastened": belt, "idle_seconds": 0,
                         "operating_state": "working" if engine else "off"},
            "provenance": {"origin": "synthetic_scenario", "generator": "demo_scheduled_features"}})

    # ------------------------------------------------------------------ 1

    def actors(self) -> None:
        print("\n== 1 scoped synthetic actors")
        s1 = self.call("svc", "session op1", "POST", "/v1/sessions", {
            "client_session_key": f"lk:demod-{self.run}:op1", "room_name": f"demod-{self.run}-1",
            "participant_identity": OP1, "operator_id": OP1, "machine_id": M1}, expect=(200, 201))
        s2 = self.call("svc", "session op2", "POST", "/v1/sessions", {
            "client_session_key": f"lk:demod-{self.run}:op2", "room_name": f"demod-{self.run}-2",
            "participant_identity": OP2, "operator_id": OP2, "machine_id": M2}, expect=(200, 201))
        self.s1, self.s2 = s1, s2
        for who in ("sup", "south"):
            me = self.call(who, f"me {who}", "GET", "/v1/me")
            print(f"  {who}: principal={me['subject_id']} sites={me['site_ids']}")
        consents = self.call("op2", "consents", "GET", f"/v1/operators/{OP2}/consents")
        print(f"  {OP2} consent: {[(x['purpose'], x['status']) for x in consents['consents']]} "
              f"(version {consents['version']})")
        ov = self.call("sup", "overview", "GET", f"/v1/supervisor/overview?site_id={SITE}")
        risk = next((r for r in ov["risks"] if r["operator_id"] == OP2), None)
        print(f"  supervisor risk for {OP2}: {risk and (risk['risk_level'], risk['unavailable_reason'])}")
        denied = self.call("south", "foreign overview", "GET", f"/v1/supervisor/overview?site_id={SITE}", expect=403)
        self.check("foreign-site supervisor is denied (403)", denied["error"]["code"] == "forbidden")
        self.check("operator cannot read the supervisor overview",
                   self.call("op1", "op overview", "GET", f"/v1/supervisor/overview?site_id={SITE}",
                             expect=403)["error"]["code"] == "forbidden")

    # ------------------------------------------------------------------ 2

    def consent(self, cid: str, purpose: str, action: str, expected: int, expect=200) -> dict:
        return self.call("op2", f"consent {cid}", "POST", f"/v1/operators/{OP2}/consents", {
            "change_id": f"{self.run}-{cid}", "purpose": purpose, "action": action, "expected_version": expected,
            "notice_version": NOTICES[purpose]["notice_version"], "is_synthetic_demo_record": True}, expect=expect)

    def wellbeing(self, day: str) -> None:
        print("\n== 2 consent, private wellbeing input, advice and the supervisor projection")
        sid = self.s2["session_id"]
        self.telemetry(sid, "op2-clock", local(day, "09:00"))
        g = self.consent("c1", "vitals_processing", "grant", 0)
        print(f"  grant vitals_processing: applied={g['applied']} version={g['state']['version']}")
        opened = []
        for i in range(3):
            out = self.call("op2", "wellbeing sample", "POST", f"/v1/sessions/{sid}/wellbeing/samples", {
                "sample_id": f"{self.run}-hr{i}", "observed_at": local(day, "09:01", 30 * i).isoformat(),
                "heart_rate_bpm": 128.0 + i, "skin_temp_c": 35.2, "window_seconds": 60, "quality": "good",
                "source": "synthetic_wearable_fixture", "simulated": True})
            opened += out["advice_opened"]
            print(f"  sample {i}: {out['status']} retained={out['retained']} rules="
                  f"{[(r['factor'], r['status']) for r in out['rules']]}")
            self.check("sample response never echoes values", "128" not in json.dumps(out))
        view = self.call("op2", "wellbeing", "GET", f"/v1/sessions/{sid}/wellbeing")
        print(f"  advice: {[(a['advice_id'], a['level'], a['status']) for a in view['active_advice']]}")
        self.say("op2", sid, "wb-why", "Why did you suggest a break?")
        ov = self.call("sup", "overview processing only", "GET", f"/v1/supervisor/overview?site_id={SITE}")
        risk = next(r for r in ov["risks"] if r["operator_id"] == OP2)
        self.check("processing alone shares no risk", (risk["risk_level"], risk["unavailable_reason"])
                   in (("unavailable", "consent_not_granted"), ("unavailable", "consent_revoked")))
        self.consent("c2", "risk_sharing_supervisor", "grant", 1)
        risk = next(r for r in self.call("sup", "overview shared", "GET",
                                         f"/v1/supervisor/overview?site_id={SITE}")["risks"] if r["operator_id"] == OP2)
        print(f"  shared projection: {risk}")
        self.consent("c3", "risk_sharing_supervisor", "revoke", 2)
        ov = self.call("sup", "overview revoked", "GET", f"/v1/supervisor/overview?site_id={SITE}")
        risk = next(r for r in ov["risks"] if r["operator_id"] == OP2)
        self.check("after revocation the overview shows no risk", risk["unavailable_reason"] == "consent_revoked")
        events = self.feed(0, 2.0)
        risk_events = [e for e in events if e["type"] == "risk.changed" and e["data"].get("operator_id") == OP2]
        self.check(f"feed replay from 0 re-projects {len(risk_events)} risk events under the revocation",
                   bool(risk_events) and all(e["data"]["risk_level"] == "unavailable" for e in risk_events))
        text = json.dumps(ov) + json.dumps(events)
        self.check("no raw vitals, explanation or speech in supervisor payloads",
                   not any(x in text for x in ("128.0", "35.2", "heart_rate", "explanation", "speech")))

    def feed(self, after: int, seconds: float) -> list[dict]:
        """Read the supervisor SSE stream over the socket for a moment (replay after the cursor)."""
        events, current = [], {}
        with httpx.Client(base_url=self.base, headers={"Authorization": f"Bearer {self.tokens['sup']}"},
                          timeout=10) as http:
            with http.stream("GET", f"/v1/supervisor/events/stream?site_id={SITE}&after={after}") as r:
                deadline = time.monotonic() + seconds
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        current = json.loads(line[6:])
                    elif line == "" and current:
                        events.append(current)
                        current = {}
                    if time.monotonic() > deadline:
                        break
        self.log.append({"step": "supervisor feed", "as": "sup", "events": len(events)})
        return events

    # ------------------------------------------------------------------ 3

    def decide(self, approval: dict, decision_id: str, decision: str, expect=200, version=None) -> dict:
        return self.call("sup", f"decide {decision_id}", "POST", f"/v1/approvals/{approval['approval_id']}/decision", {
            "decision_id": f"{self.run}-{decision_id}", "decision": decision,
            "expected_version": version or approval["version"], "payload_sha256": approval["payload_sha256"]},
            expect=expect)

    def approvals(self, day: str) -> None:
        print("\n== 3 repeated violations -> scoped approval -> one in-app notification")
        sid = self.s2["session_id"]
        for e in hazard_events(M2, "repeat_belt", local(day, "10:00"), f"{self.run}-rb"):
            self.call("svc", "telemetry repeat", "POST", f"/v1/sessions/{sid}/telemetry", e)
        items = self.call("sup", "approvals", "GET", "/v1/approvals?limit=50")["items"]
        review = next(a for a in items if a["kind"] == "repeated_violations" and a["operator_id"] == OP2)
        if "review" not in self.state:
            self.state["review"] = {k: review[k] for k in ("approval_id", "version", "payload_sha256")}
        saved = self.state["review"]
        print(f"  review {review['approval_id']} status={review['status']} eligible={review['eligibility']} "
              f"family={review['escalation']['rule_family']} episodes={review['escalation']['episode_count']}")
        self.call("south", "foreign detail", "GET", f"/v1/approvals/{review['approval_id']}", expect=404)
        first = self.decide(saved, "rv-approve", "approve")
        print(f"  decision recorded={first['decision_recorded']} status={first['approval']['status']} "
              f"application={first['approval']['application']['status']}")
        retry = self.decide(saved, "rv-approve", "approve")
        self.check("identical decision retry is not recorded twice", retry["decision_recorded"] is False)
        contra = self.decide(saved, "rv-reject", "reject", expect=409, version=first["approval"]["version"])
        self.check("contradictory decision is refused (409 decision_conflict)",
                   contra["error"]["code"] == "decision_conflict")
        notes = [n for n in self.call("sup", "overview", "GET", f"/v1/supervisor/overview?site_id={SITE}")
                 ["notifications"] if n["source_id"] == review["approval_id"]]
        self.check("exactly one in-app notification for the approval (status created, not read)",
                   len(notes) == 1 and notes[0]["status"] in ("created", "presented", "acknowledged"))

    # ------------------------------------------------------------------ 4

    def weather(self, name: str) -> None:
        target = self.data_dir / "weather.json"
        shutil.copyfile(DEMO_DIR / name, target)
        print(f"  (forecast source now {name})")

    def propose(self, label: str) -> dict:
        out = self.call("svc", f"propose {label}", "POST", f"/v1/shifts/{self.s1['shift_id']}/schedule-proposals",
                        {"note": label})
        if out["status"] == "no_proposal":
            print(f"  {label}: no proposal ({out['reason']})")
        else:
            sched = out["approval"]["schedule"]
            print(f"  {label}: {out['status']} {out['approval']['approval_id']} score {sched['before_score']} -> "
                  f"{sched['after_score']}; " + "; ".join(
                      f"{c['title']} #{c['before_order']}->{c['after_order']} ({c['before_level']}->{c['after_level']})"
                      for c in sched["changes"]))
        return out

    def order(self) -> list[tuple]:
        state = self.call("op1", "state", "GET", f"/v1/sessions/{self.s1['session_id']}/state")
        return [(t["title"], t["scheduled_start_local"], t["version"]) for t in state["assigned_tasks"]]

    def replanning(self, day: str) -> None:
        print("\n== 4 forecast change -> reorder proposal -> reject / stale / approve once")
        self.telemetry(self.s1["session_id"], "op1-clock", local(day, "07:00"), engine=False)
        before = self.order()
        print(f"  schedule: {before}")
        if "proposals" not in self.state:  # first run only: forecast changes and new proposals
            self.weather("weather_fixture_replan_calm_v1.json")
            self.check("calm forecast: explained no-proposal", self.propose("calm")["status"] == "no_proposal")
            self.weather("weather_fixture_replan_gusty_v1.json")
            p1 = self.propose("gusty")["approval"]
            self.check("proposal changes nothing before approval", self.order() == before)
            self.state["proposals"] = {"p1": p1}
            self.decide(p1, "p1-reject", "reject")
            p2 = self.propose("fresh")["approval"]
            self.state["proposals"]["p2"] = p2
            self.weather("weather_fixture_replan_gusty_v2.json")
            stale = self.decide(p2, "p2-approve", "approve")["approval"]
            print(f"  P2 approved after the forecast changed: application={stale['application']['status']} "
                  f"({stale['application']['reason']})")
            self.check("stale proposal left the schedule intact", self.order() == before)
            p3 = self.propose("revalidated")["approval"]
            self.state["proposals"]["p3"] = p3
        props = self.state["proposals"]
        rejected = self.decide(props["p1"], "p1-reject", "reject")
        p2 = self.decide(props["p2"], "p2-approve", "approve")
        p3 = self.decide(props["p3"], "p3-approve", "approve")
        print(f"  P1 {rejected['approval']['status']}, P2 {p2['approval']['application']['status']}, "
              f"P3 {p3['approval']['application']['status']} (recorded now: "
              f"{[x['decision_recorded'] for x in (rejected, p2, p3)]})")
        after = self.order()
        print(f"  schedule now: {after}")
        events = self.call("op1", "events", "GET", f"/v1/sessions/{self.s1['session_id']}/events?limit=100")["events"]
        changed = [e for e in events if e["type"] == "schedule_changed"]
        for e in changed:
            print(f"  [announcement {e['type']}] {e['speech']}")
        self.check("exactly one schedule application and one operator announcement",
                   p3["approval"]["application"]["status"] == "applied" and len(changed) == 1)

    # ------------------------------------------------------------------ 5

    def impact(self, key: str) -> dict:
        at = self.state.setdefault("impacts", {}).setdefault(key, datetime.now(timezone.utc).isoformat())
        out = self.call("svc", f"impact {key}", "POST", f"/v1/sessions/{self.s2['session_id']}/impacts", {
            "source_event_id": f"{self.run}-{key}", "device_id": "demo-watch-2", "observed_at": at,
            "peak_accel_g": 4.1, "duration_ms": 160, "orientation_after": "prone", "quality": "good",
            "provenance": "simulated"})
        return out

    def wait_state(self, episode_id: str, seconds: float) -> dict:
        """Poll the supervisor's SOS view (every episode by ID) until the worker decides it; no input is sent."""
        deadline = time.monotonic() + seconds
        while True:
            ov = self.call("sup", "sos poll", "GET", f"/v1/supervisor/overview?site_id={SITE}")
            item = next(x for x in ov["sos"] if x["episode_id"] == episode_id)
            if item["state"] not in ("queued", "offered") or time.monotonic() > deadline:
                return item
            time.sleep(0.5)

    def sos(self, restart) -> None:
        print("\n== 5 SOS check-ins (simulated impact source, accelerated demo timers)")
        sid = self.s2["session_id"]
        a = self.impact("imp-okay")
        ep = a["episode"]
        print(f"  impact A: {a['status']} -> {ep['episode_id']} {ep['state']} (offer wait until "
              f"{ep['offer_deadline_at'][11:19]})")
        self.call("svc", "voice played", "POST", f"/v1/sessions/{sid}/events/{ep['checkin']['event_id']}/delivery",
                  {"consumer_id": "demo-voice-worker", "status": "played", "detail": "simulated playback report"})
        out = self.say("op2", sid, "sos-ok", "I'm okay")
        self.check("A: offered by (simulated) voice playback, answered okay, no notification",
                   out["actions"][0]["episode"]["state"] == "okay")
        b = self.impact("imp-help")["episode"]
        helped = self.call("op2", "sos help", "POST", f"/v1/sessions/{sid}/commands", {
            "command_id": f"{self.run}-sos-help", "kind": "sos.respond",
            "payload": {"checkin_id": b["episode_id"], "response": "help"}})
        self.check("B: help notifies at once", helped["sos"]["state"] == "help_requested"
                   and helped["sos"]["notify_status"] == "notified")
        c = self.impact("imp-silent")["episode"]
        self.call("op2", "screen presented", "POST", f"/v1/sessions/{sid}/events/{c['checkin']['event_id']}/presentation",
                  {"presentation_id": f"{self.run}-pres-c", "consumer_id": "demo-phone-2", "channel": "screen",
                   "status": "presented", "presented_at": self.state["impacts"]["imp-silent"]})
        final = self.wait_state(c["episode_id"], 12)
        self.check(f"C: offered on screen, no answer -> {final['state']} ({final['outcome_reason']})",
                   final["state"] == "unresolved_no_response")
        d = self.impact("imp-unreachable")["episode"]
        final = self.wait_state(d["episode_id"], 12)
        self.check(f"D: never offered -> {final['state']} ({final['outcome_reason']})", final["state"] == "unreachable")
        e = self.impact("imp-restart")["episode"]
        if e["state"] == "queued":
            print(f"  E {e['episode_id']} pending; stopping the backend before its deadline")
            restart(wait_until=datetime.fromisoformat(e["offer_deadline_at"]))
        final = self.wait_state(e["episode_id"], 12)
        self.check(f"E: recovered after restart without new input -> {final['state']} ({final['outcome_reason']})",
                   final["state"] == "unreachable" and final["notify_status"] == "notified")
        ov = self.call("sup", "overview", "GET", f"/v1/supervisor/overview?site_id={SITE}")
        print("  supervisor SOS view: " + "; ".join(f"{x['episode_id']} {x['state']} {x['notify_status']}"
                                                    for x in ov["sos"]))
        kinds = sorted(n["kind"] for n in ov["notifications"] if n["kind"].startswith("sos_"))
        print(f"  urgent in-app notifications: {kinds}")

    # ------------------------------------------------------------------ 6

    def offline(self) -> None:
        print("\n== 6 offline client: cached snapshot, local draft, upload/retry, replacement session")
        s1 = self.s1
        snap = self.call("op1", "snapshot", "GET", f"/v1/sessions/{s1['session_id']}/state")
        cache = {"snapshot": snap["snapshot"], "tasks": [(t["task_id"], t["version"]) for t in snap["assigned_tasks"]]}
        print(f"  cached: state_version={cache['snapshot']['state_version']} schedule_version="
              f"{cache['snapshot']['schedule_version']} machine={cache['snapshot']['machine_status']}")
        print("  (client stops calling the server and captures a local draft)")
        captured = self.state.setdefault("captured_at", (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
        binding = {k: s1[k] for k in ("session_id", "operator_id", "machine_id", "site_id", "shift_id")}
        body = {"command_id": f"{self.run}-off-1", "kind": "incident.submit_draft",
                "client_draft_id": f"{self.run}-local-draft-1", "captured_at": captured, "original_binding": binding,
                "payload": {"description": "Cracked mirror bracket on the cab", "occurred_expression": "ten minutes ago"}}
        up1 = self.call("op1", "upload", "POST", f"/v1/sessions/{s1['session_id']}/commands", body)
        up2 = self.call("op1", "upload retry", "POST", f"/v1/sessions/{s1['session_id']}/commands", body)
        print(f"  upload: {up1['summary']} duplicate={up1['duplicate']}; retry duplicate={up2['duplicate']}")
        looked = self.call("op1", "status lookup", "GET", f"/v1/sessions/{s1['session_id']}/commands/{body['command_id']}")
        self.check("lost-response lookup returns the stored record", looked["record_id"] == up1["record_id"])
        other = self.call("svc", "replacement session", "POST", "/v1/sessions", {
            "client_session_key": f"lk:demod-{self.run}:op1-phone2", "room_name": f"demod-{self.run}-3",
            "participant_identity": OP1, "operator_id": OP1, "machine_id": "LDR_DEMO_001"}, expect=(200, 201))
        again = self.call("op1", "upload from replacement", "POST", f"/v1/sessions/{other['session_id']}/commands",
                          {**body, "command_id": f"{self.run}-off-2"})
        self.check("same local draft from a replacement session on another machine -> the one stored draft",
                   again["record_id"] == up1["record_id"] and again["duplicate_draft"] is True)
        state = self.call("op1", "state", "GET", f"/v1/sessions/{s1['session_id']}/state")
        offline = [d for d in state["incident_drafts"] if d["capture_mode"] == "offline_sync"]
        self.check(f"one offline draft on the original machine {M1} (occurred "
                   f"{offline[0]['occurred_at'][11:16]} UTC = captured - 10 min)",
                   len(offline) == 1 and offline[0]["machine_id"] == M1)
        changed = self.call("op1", "changed upload", "POST", f"/v1/sessions/{s1['session_id']}/commands",
                            {**body, "command_id": f"{self.run}-off-3", "payload": {"description": "Different"}},
                            expect=409)
        print(f"  changed content under the same draft ID: {changed['error']['code']}")
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        task_id, version = cache["tasks"][-1]
        stale = self.call("op1", "stale task", "POST", f"/v1/sessions/{s1['session_id']}/commands", {
            "command_id": f"{self.run}-stale-start", "kind": "task.start", "captured_at": old,
            "expected_version": version, "payload": {"task_id": task_id, "acknowledge_conditions": True}}, expect=409)
        self.check("stale queued task start refused (409)", stale["error"]["code"] in ("invalid_transition",
                                                                                         "version_conflict"))
        event = self.call("op1", "events", "GET", f"/v1/sessions/{s1['session_id']}/events")["events"][0]
        self.call("op1", "screen receipt", "POST", f"/v1/sessions/{s1['session_id']}/events/{event['event_id']}"
                  "/presentation", {"presentation_id": f"{self.run}-pres-1", "consumer_id": "demo-phone-1",
                                    "channel": "screen", "status": "presented", "presented_at": captured})
        event = self.call("op1", "events", "GET", f"/v1/sessions/{s1['session_id']}/events")["events"][0]
        self.check("screen receipt recorded; no audio playback claimed",
                   [p["channel"] for p in event["presentations"]] == ["screen"] and not any(
                       d["status"] == "played" for d in event["deliveries"]))

    # ------------------------------------------------------------------ 7

    def counts(self) -> dict:
        consents = self.call("op2", "consents", "GET", f"/v1/operators/{OP2}/consents")["version"]
        state = self.call("op1", "state", "GET", f"/v1/sessions/{self.s1['session_id']}/state")
        approvals = self.call("sup", "approvals", "GET", "/v1/approvals?limit=100")["items"]
        ov = self.call("sup", "overview", "GET", f"/v1/supervisor/overview?site_id={SITE}")
        return {"consent_version": consents,
                "offline_drafts": len([d for d in state["incident_drafts"] if d["capture_mode"] == "offline_sync"]),
                "decisions": len([a for a in approvals if a["decision"]]),
                "schedule_version": state["snapshot"]["schedule_version"],
                "notifications": len(ov["notifications"]), "sos_episodes": len(ov["sos"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=SERVICE_DIR / "data" / "demo_d")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--run-id", default="d1")
    args = ap.parse_args()
    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    run_file = data_dir / f"demo_d_run_{args.run_id}.json"
    replay = run_file.is_file()
    state = json.loads(run_file.read_text(encoding="utf-8")) if replay else {"service_date": site_today(load_fixture())}
    day = state["service_date"]
    if not (data_dir / "weather.json").is_file():
        shutil.copyfile(DEMO_DIR / "weather_fixture_replan_calm_v1.json", data_dir / "weather.json")
    env = {"COCOON_WEATHER_MODE": "fixture", "COCOON_WEATHER_FIXTURE_PATH": str(data_dir / "weather.json"),
           "COCOON_SOS_TIMER_PROFILE": "accelerated_demo", "COCOON_WORKER_INTERVAL_SECONDS": "0.5",
           "COCOON_FEED_POLL_SECONDS": "0.1", "COCOON_FEED_HEARTBEAT_SECONDS": "1"}
    print(f"== Batch D demo run {args.run_id} ({'same-ID REPLAY' if replay else 'fresh run'}), service date {day}")
    proc = spawn(data_dir, args.port, env)
    settings = admin_settings(data_dir)
    store = Store(settings.db_path)  # a second synthetic site, only for the foreign-site denial check
    store.seed_demo_site({"sites": [("site_id", ("SITE_DEMO_SOUTH", "Synthetic south yard (demo only)", "Asia/Kolkata",
                                                 "+05:30", "demo-d-1", "synthetic_demo_fixture"))]})
    store.close()
    tokens = {"op1": token(settings, data_dir, "op1", "--role", "operator", "--operator-id", OP1),
              "op2": token(settings, data_dir, "op2", "--role", "operator", "--operator-id", OP2),
              "sup": token(settings, data_dir, "sup-north", "--role", "supervisor", "--principal-id", "sup-north"),
              "south": token(settings, data_dir, "sup-south", "--role", "supervisor", "--principal-id", "sup-south")}
    grant(settings, "sup-north", SITE)
    grant(settings, "sup-south", "SITE_DEMO_SOUTH")
    base = f"http://127.0.0.1:{args.port}"
    svc = client(base)
    tokens["svc"] = svc.headers["Authorization"].split(" ", 1)[1]
    demo = DDemo(base, args.run_id, data_dir, tokens, state)
    holder = {"proc": proc}

    def restart(wait_until: datetime) -> None:
        holder["proc"].kill()
        holder["proc"].wait(timeout=15)
        pause = max(0.0, (wait_until - datetime.now(timezone.utc)).total_seconds()) + 1.0
        print(f"  backend stopped abruptly; waiting {pause:.1f} s past the deadline, then restarting (no new input)")
        time.sleep(pause)
        holder["proc"] = spawn(data_dir, args.port, env)

    try:
        demo.actors()
        demo.wellbeing(day)
        demo.approvals(day)
        demo.replanning(day)
        demo.sos(restart)
        demo.offline()
        print("\n== 7 stable-ID replay check")
        counts = demo.counts()
        print(f"  counts: {counts}")
        if replay:
            demo.check("replay created no new consent change, draft, decision, application or notification",
                       counts == state.get("counts"))
        else:
            state["counts"] = counts
            print("  (run the same command again to replay every stable request)")
    finally:
        run_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        (data_dir / f"demo_d_transcript_{args.run_id}.json").write_text(json.dumps(demo.log, indent=2),
                                                                         encoding="utf-8")
        holder["proc"].terminate()
        holder["proc"].wait(timeout=15)
    failed = [label for label, ok in demo.checks if not ok]
    print(f"\n== {len(demo.checks) - len(failed)}/{len(demo.checks)} checks passed"
          + (f"; FAILED: {failed}" if failed else ""))
    print("Simulated: impacts, wellbeing samples, machine telemetry, voice playback and screen reports are synthetic "
          "HTTP inputs; no device, wearable, phone or person was involved.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
