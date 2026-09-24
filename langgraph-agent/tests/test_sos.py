"""Batch D3: human-impact candidates, durable check-ins with two server-clock deadlines, operator answers through the
shared command/voice paths, scoped in-app emergency notifications, and recovery across a real process restart."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app

from .conftest import AUTH, TOKEN, seeded_demo
from .test_auth import bearer, issue, run_admin
from .test_tasks import say, session_for

OP, SITE, MACHINE = "OP_DEMO_1_1", "SITE_DEMO_NORTH", "EXC_DEMO_001"
SERVICE_DIR = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def settings_for(tmp_path, **overrides):
    settings, _ = seeded_demo(tmp_path, COCOON_WORKER_INTERVAL_SECONDS="60", **overrides)
    op, _ = issue(settings, tmp_path, "operator", "--operator-id", OP, name="op")
    sup, _ = issue(settings, tmp_path, "supervisor", "--principal-id", "sup-north", name="sup")
    assert run_admin(settings, "grant-site", "--principal-id", "sup-north", "--site-id", SITE)[0] == 0
    return settings, bearer(op), bearer(sup)


@pytest.fixture
def world(tmp_path):
    settings, op, sup = settings_for(tmp_path)
    clock = Clock()
    with TestClient(create_app(settings)) as c:
        c.app.state.service.sos.clock = clock  # injected workflow clock (the channels use the same one)
        sid = session_for(c, MACHINE)["session_id"]
        yield {"c": c, "sid": sid, "op": op, "sup": sup, "clock": clock, "settings": settings,
               "sos": c.app.state.service.sos}


def impact(c, sid, source_id, at, headers=AUTH, peak=4.2, quality="good"):
    return c.post(f"/v1/sessions/{sid}/impacts", headers=headers, json={
        "source_event_id": source_id, "device_id": "watch-1", "observed_at": at.isoformat(), "peak_accel_g": peak,
        "duration_ms": 180, "orientation_after": "prone", "quality": quality, "provenance": "simulated"})


def opened(w, source="imp-1") -> dict:
    r = impact(w["c"], w["sid"], source, w["clock"].now - timedelta(seconds=2))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "opened"
    return r.json()["episode"]


def episode(w, episode_id) -> dict:
    return w["c"].get(f"/v1/sessions/{w['sid']}/state", headers=AUTH).json()["sos"]


def notes(w) -> list[dict]:
    return w["c"].get(f"/v1/supervisor/overview?site_id={SITE}", headers=w["sup"]).json()["notifications"]


def played(w, event_id, consumer="voice-worker", status="played"):
    return w["c"].post(f"/v1/sessions/{w['sid']}/events/{event_id}/delivery", headers=AUTH,
                       json={"consumer_id": consumer, "status": status})


def shown(w, event_id, pid, channel="screen", status="presented", at=None):
    return w["c"].post(f"/v1/sessions/{w['sid']}/events/{event_id}/presentation", headers=w["op"], json={
        "presentation_id": pid, "consumer_id": "phone-1", "channel": channel, "status": status,
        "presented_at": (at or w["clock"].now).isoformat()})


def test_offered_by_voice_then_okay_needs_no_notification(world):
    ep = opened(world)
    events = world["c"].get(f"/v1/sessions/{world['sid']}/events", headers=AUTH).json()["events"]
    checkin = next(e for e in events if e["type"] == "sos_checkin")
    assert checkin["priority"] == "critical" and checkin["event_id"] == ep["checkin"]["event_id"]
    assert ep["state"] == "queued" and ep["timer_profile"] == "standard"
    assert played(world, checkin["event_id"]).status_code == 200
    ep = episode(world, ep["episode_id"])
    assert ep["state"] == "offered" and ep["offer_channel"] == "voice"
    out = say(world["c"], world["sid"], "t1", "I'm okay")
    assert out["actions"][0]["event"] == "response_recorded" and "glad you're okay" in out["speech"]
    assert episode(world, ep["episode_id"])["state"] == "okay" and notes(world) == []


def test_offered_on_screen_without_answer_notifies_once_and_a_late_okay_is_an_update(world):
    ep = opened(world)
    event_id = ep["checkin"]["event_id"]
    assert shown(world, event_id, "p-vib", channel="vibration").status_code == 200   # vibration alone: no offer
    assert episode(world, ep["episode_id"])["state"] == "queued"
    first_at = world["clock"].now
    assert shown(world, event_id, "p1").status_code == 200
    offered = episode(world, ep["episode_id"])
    assert offered["state"] == "offered" and offered["offer_channel"] == "screen"
    world["clock"].advance(30)
    assert shown(world, event_id, "p1", at=first_at).status_code == 200              # identical retry
    assert shown(world, event_id, "p1").status_code == 409                           # same ID, changed body
    assert shown(world, event_id, "p2").status_code == 200                           # a later report never extends
    assert episode(world, ep["episode_id"])["response_deadline_at"] == offered["response_deadline_at"]
    world["clock"].advance(world["sos"].timers.response_window_seconds)
    assert world["sos"].run_due() == [ep["episode_id"]] and world["sos"].run_due() == []
    ep = episode(world, ep["episode_id"])
    assert (ep["state"], ep["outcome_reason"], ep["notify_status"]) == \
        ("unresolved_no_response", "no_response_after_offer", "notified")
    urgent = notes(world)
    assert [(n["kind"], n["priority"], n["policy_id"]) for n in urgent] == \
        [("sos_no_response", "urgent", "preauthorised_emergency_in_app.v1")]
    assert "Not a confirmed injury" in urgent[0]["summary"] and "simulated" in urgent[0]["summary"]
    late = world["c"].post(f"/v1/sessions/{world['sid']}/commands", headers=world["op"], json={
        "command_id": "late-1", "kind": "sos.respond", "payload": {"checkin_id": ep["episode_id"], "response": "okay"}})
    assert late.status_code == 200 and late.json()["sos"]["late_response"] == "okay"
    kinds = sorted(n["kind"] for n in notes(world))
    assert kinds == ["sos_no_response", "sos_recovered"]                            # the original alert stays
    assert episode(world, ep["episode_id"])["state"] == "unresolved_no_response"


def test_help_notifies_immediately_without_waiting_for_a_timer(world):
    ep = opened(world)
    r = world["c"].post(f"/v1/sessions/{world['sid']}/commands", headers=world["op"], json={
        "command_id": "h1", "kind": "sos.respond", "payload": {"checkin_id": ep["episode_id"], "response": "help"}})
    assert r.status_code == 200 and r.json()["sos"]["state"] == "help_requested"
    again = world["c"].post(f"/v1/sessions/{world['sid']}/commands", headers=world["op"], json={
        "command_id": "h1", "kind": "sos.respond", "payload": {"checkin_id": ep["episode_id"], "response": "help"}})
    assert again.json()["duplicate"] is True
    assert [n["kind"] for n in notes(world)] == ["sos_help"]
    ov = world["c"].get(f"/v1/supervisor/overview?site_id={SITE}", headers=world["sup"])
    assert ov.json()["sos"][0]["state"] == "help_requested" and "heart" not in ov.text


def test_unreachable_reasons_distinguish_no_channel_from_an_unconfirmed_offer(world):
    ep = opened(world, "imp-a")
    played(world, ep["checkin"]["event_id"], status="failed")                        # failed playback is no offer
    world["clock"].advance(world["sos"].timers.offer_wait_seconds)
    world["sos"].run_due()
    first = episode(world, ep["episode_id"])
    assert (first["state"], first["outcome_reason"]) == ("unreachable", "no_live_channel")
    c, sid = world["c"], world["sid"]
    r = c.post(f"/v1/sessions/{sid}/presence", headers=world["op"], json={
        "report_id": "pr-1", "consumer_id": "phone-1", "sequence": 1, "connection": "online",
        "voice_available": True, "screen_available": True, "reported_at": world["clock"].now.isoformat(),
        "ttl_seconds": 120})
    assert r.status_code == 200 and r.json()["presence"]["live"] is True
    ep2 = opened(world, "imp-b")
    world["clock"].advance(world["sos"].timers.offer_wait_seconds)
    world["sos"].run_due()
    second = episode(world, ep2["episode_id"])
    assert (second["state"], second["outcome_reason"]) == ("unreachable", "offer_not_confirmed")
    assert sorted(n["kind"] for n in notes(world)) == ["sos_unreachable", "sos_unreachable"]


def test_missing_policy_or_recipient_is_a_visible_blocked_outcome(tmp_path):
    policy = json.loads((SERVICE_DIR / "policies" / "emergency_notification_v1.json").read_text())
    policy["sites"] = {}
    path = tmp_path / "no_site_policy.json"
    path.write_text(json.dumps(policy))
    settings, op, sup = settings_for(tmp_path, COCOON_EMERGENCY_POLICY_PATH=str(path))
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, MACHINE)["session_id"]
        now = datetime.now(timezone.utc)
        ep = impact(c, sid, "i1", now).json()["episode"]
        out = say(c, sid, "t1", "I need help", headers=op)
        assert out["actions"][0]["episode"]["notify_status"] == "blocked_no_policy"
        assert "no emergency contact is set up" in out["speech"]
        assert c.get(f"/v1/supervisor/overview?site_id={SITE}", headers=sup).json()["notifications"] == []
        assert ep["site_id"] == SITE
    settings2, op2, _ = settings_for(tmp_path / "second")
    assert run_admin(settings2, "revoke-site", "--principal-id", "sup-north", "--site-id", SITE)[0] == 0
    with TestClient(create_app(settings2)) as c:
        sid = session_for(c, MACHINE)["session_id"]
        impact(c, sid, "i1", datetime.now(timezone.utc))
        out = say(c, sid, "t1", "I need help", headers=op2)
        assert out["actions"][0]["episode"]["notify_status"] == "blocked_no_recipient"


def test_duplicates_merges_stale_and_future_candidates(world):
    c, sid, clock = world["c"], world["sid"], world["clock"]
    ep = opened(world, "imp-1")
    dup = impact(c, sid, "imp-1", clock.now - timedelta(seconds=2)).json()
    assert dup["status"] == "duplicate" and dup["episode"]["episode_id"] == ep["episode_id"]
    assert impact(c, sid, "imp-1", clock.now - timedelta(seconds=2), peak=9.9).status_code == 409
    clock.advance(20)
    merged = impact(c, sid, "imp-2", clock.now).json()
    assert merged["status"] == "merged" and merged["episode"]["impact_count"] == 2
    assert merged["episode"]["offer_deadline_at"] == ep["offer_deadline_at"]          # deadlines never restart
    old = impact(c, sid, "imp-old", clock.now - timedelta(hours=2)).json()
    assert old["status"] == "historical" and old["episode"] is None
    assert impact(c, sid, "imp-weak", clock.now, peak=1.0).json()["status"] == "below_threshold"
    assert impact(c, sid, "imp-future", clock.now + timedelta(minutes=10)).status_code == 422
    assert impact(c, sid, "x", clock.now, headers=world["sup"]).status_code == 403


def test_an_answer_at_the_exact_deadline_is_late_and_bare_yes_never_answers(world):
    ep = opened(world)
    played(world, ep["checkin"]["event_id"])
    out = say(world["c"], world["sid"], "t1", "yes")
    assert out["actions"][0]["event"] == "clarify" and "I'm okay, or I need help" in out["speech"]
    assert episode(world, ep["episode_id"])["state"] == "offered"
    deadline = datetime.fromisoformat(episode(world, ep["episode_id"])["response_deadline_at"])
    world["clock"].now = deadline                                                   # response and deadline coincide
    out = say(world["c"], world["sid"], "t2", "I'm okay")
    final = out["actions"][0]["episode"]
    assert out["actions"][0]["event"] == "late_response_recorded"
    assert (final["state"], final["late_response"]) == ("unresolved_no_response", "okay")
    assert sorted(n["kind"] for n in notes(world)) == ["sos_no_response", "sos_recovered"]
    assert say(world["c"], world["sid"], "t3", "I'm okay")["actions"][0]["event"] == "no_open_checkin"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start(settings, port: int) -> subprocess.Popen:
    env = {**os.environ, "COCOON_SERVICE_TOKEN": TOKEN, "COCOON_DATA_DIR": str(settings.data_dir),
           "COCOON_LLM_MODE": "mock", "COCOON_WEATHER_MODE": "off", "COCOON_PORT": str(port),
           "COCOON_HOST": "127.0.0.1", "DATASET_ROOT": str(settings.dataset_root),
           "DATASET_MANIFEST_SHA256": settings.dataset_manifest_sha256,
           "SESSION_BINDINGS_PATH": str(settings.session_bindings_path),
           "COCOON_SOS_TIMER_PROFILE": "accelerated_demo", "COCOON_WORKER_INTERVAL_SECONDS": "0.5"}
    proc = subprocess.Popen([sys.executable, "-m", "cocoon_agent"], cwd=SERVICE_DIR, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    proc.kill()
    raise RuntimeError("backend did not start")


def test_a_pending_checkin_survives_a_real_process_restart(tmp_path):
    settings, op, sup = settings_for(tmp_path)
    port = _port()
    proc = _start(settings, port)
    base = f"http://127.0.0.1:{port}"
    try:
        sid = httpx.post(f"{base}/v1/sessions", headers=AUTH, json={
            "client_session_key": "lk:restart:op", "room_name": "restart", "participant_identity": "op",
            "operator_id": OP, "machine_id": MACHINE}).json()["session_id"]
        ep = httpx.post(f"{base}/v1/sessions/{sid}/impacts", headers=AUTH, json={
            "source_event_id": "r1", "device_id": "watch-1", "observed_at": datetime.now(timezone.utc).isoformat(),
            "peak_accel_g": 4.0, "duration_ms": 150, "quality": "good", "provenance": "simulated"}).json()["episode"]
        assert ep["state"] == "queued" and ep["timer_profile"] == "accelerated_demo"
    finally:
        proc.kill()  # abrupt stop while the check-in is pending
        proc.wait(timeout=10)
    deadline = datetime.fromisoformat(ep["offer_deadline_at"])
    time.sleep(max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds()) + 0.5)
    proc = _start(settings, port)                                   # no impact, turn or telemetry after restart
    try:
        for _ in range(20):
            state = httpx.get(f"{base}/v1/sessions/{sid}/state", headers=AUTH).json()["sos"]
            if state["state"] != "queued":
                break
            time.sleep(0.25)
        assert (state["state"], state["outcome_reason"], state["notify_status"]) == \
            ("unreachable", "no_live_channel", "notified")
        overview = httpx.get(f"{base}/v1/supervisor/overview?site_id={SITE}", headers=sup).json()
        assert [n["kind"] for n in overview["notifications"]] == ["sos_unreachable"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
