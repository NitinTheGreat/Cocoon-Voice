"""Batch C2 (hazards): proximity, sudden starts/stops, steep slopes, fuel per load cycle and repeated violations on
simulated observations. Each family is checked on its normal, trigger and clearing path plus its unknown cases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.simulation import HAZARD_SCENARIOS, hazard_events

from .conftest import AUTH, seeded_demo
from .test_tasks import command, say, session_for

START = datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)


def post(c, sid, body) -> dict:
    r = c.post(f"/v1/sessions/{sid}/telemetry", headers=AUTH, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def run(c, sid, machine, scenario, run_id="r1") -> list[dict]:
    return [post(c, sid, e) for e in hazard_events(machine, scenario, START, run_id)]


def state(c, sid) -> dict:
    return c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()


def alerts_of(c, sid, alert_type) -> list[dict]:
    from cocoon_agent.store import Store  # all episodes, active and cleared, oldest first
    store = Store(c.app.state.service.settings.db_path)
    try:
        rows = store._all("SELECT alert_id FROM alerts WHERE session_id = ? AND alert_type = ? ORDER BY started_at",
                          (sid, alert_type))
        ids = [r["alert_id"] for r in rows]
    finally:
        store.close()
    announced = {a.alert_id: a for a, _, _ in c.app.state.service.store.announced_alerts(sid)}
    return [announced[i].model_dump(mode="json") for i in ids if i in announced]


def coverage(c, sid) -> dict[str, dict]:
    return {x["rule_id"]: x for x in state(c, sid)["rule_coverage"]}


@pytest.fixture
def app(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        yield c


def test_every_scenario_is_deterministic_with_stable_ids():
    for name in HAZARD_SCENARIOS:
        assert hazard_events("EXC_DEMO_001", name, START, "x") == hazard_events("EXC_DEMO_001", name, START, "x")


def test_normal_operation_opens_nothing_and_reports_coverage(app):
    sid = session_for(app, "EXC_DEMO_001")["session_id"]
    out = run(app, sid, "EXC_DEMO_001", "normal_operation")
    assert all(r["alerts_opened"] == [] and r["announcements_created"] == [] for r in out)
    cov = coverage(app, sid)
    assert cov["demo.proximity.v1"]["status"] == "evaluated"
    assert cov["demo.steep_slope.v1"]["status"] == "evaluated"
    assert cov["demo.sudden_stop.v1"]["status"] == "evaluated"
    assert cov["demo.fuel_per_cycle.v1"]["status"] == "unknown"  # no meters: never read as "fine"


def test_proximity_warning_rises_to_danger_in_one_episode_and_clears_only_when_observed(app):
    sid = session_for(app, "EXC_DEMO_001")["session_id"]
    out = run(app, sid, "EXC_DEMO_001", "proximity_approach")
    opened = [r["alerts_opened"] for r in out]
    assert opened[1] and all(not o for i, o in enumerate(opened) if i != 1)  # one episode for P1
    alert_id = opened[1][0]
    assert out[3]["alerts_updated"] == [alert_id] and out[3]["announcements_created"][0].endswith("_escalated")
    assert out[4]["alerts_updated"] == [] and out[4]["announcements_created"] == []  # still danger: nothing new
    assert out[5]["alerts_updated"] == [alert_id] and out[5]["announcements_created"] == []  # lower: recorded only
    assert out[6]["alerts_cleared"] == [alert_id]  # observed at 15 m (excavator clear distance 14 m)
    alert = alerts_of(app, sid, "proximity")[0]
    assert (alert["subject_key"], alert["cleared_reason"], alert["level"]) == ("P1", "observed_clear", "warning")
    assert [(u["previous_level"], u["level"]) for u in alert["updates"]] == [("warning", "danger"), ("danger", "warning")]
    assert alert["details"]["detection"]["distance_m"] == 11.0  # opening evidence kept
    assert alert["updates"][0]["details"]["detection"]["distance_m"] == 4.0  # the escalation's own evidence
    assert alert["details"]["thresholds_m"] == {"warning": 12.0, "danger": 6.0, "clear": 14.0}
    events = app.get(f"/v1/sessions/{sid}/events?after=0", headers=AUTH).json()["events"]
    kinds = [(e["type"], e["priority"]) for e in events if e["alert_id"] == alert_id]
    assert kinds == [("alert_started", "high"), ("alert_escalated", "critical"), ("alert_cleared", "low")]
    why = say(app, sid, "t1", "why did you warn me about the person behind me?")["actions"][0]
    assert why["alert"]["alert_id"] == alert_id and len(why["alert"]["updates"]) == 2


def test_a_missing_detection_is_not_proof_the_person_left(app):
    sid = session_for(app, "EXC_DEMO_001")["session_id"]
    out = run(app, sid, "EXC_DEMO_001", "proximity_lost")
    alert_id = out[0]["alerts_opened"][0]
    assert out[1]["alerts_cleared"] == [] and out[2]["alerts_cleared"] == []  # empty scan / no data: still active
    cov = coverage(app, sid)["demo.proximity.v1"]
    assert out[3]["alerts_cleared"] == [alert_id]
    alert = alerts_of(app, sid, "proximity")[0]
    assert alert["cleared_reason"] == "expired_without_detection"
    events = app.get(f"/v1/sessions/{sid}/events?after=0", headers=AUTH).json()["events"]
    assert "doesn't mean they've gone" in [e for e in events if e["alert_id"] == alert_id][-1]["speech"]
    assert cov["status"] == "evaluated"


def test_proximity_lost_track_is_reported_in_coverage(app):
    sid = session_for(app, "EXC_DEMO_001")["session_id"]
    events = hazard_events("EXC_DEMO_001", "proximity_lost", START, "lt")
    post(app, sid, events[0])
    post(app, sid, events[2])  # 30 s later, no detector data
    cov = coverage(app, sid)["demo.proximity.v1"]
    assert cov["status"] == "unknown" and "P2 not detected for 30 s" in cov["reason"]


def test_sudden_stop_needs_dense_samples_and_is_never_counted_twice(app):
    sid = session_for(app, "EXC_DEMO_001")["session_id"]
    out = run(app, sid, "EXC_DEMO_001", "sudden_stop")
    assert out[0]["alerts_opened"] == []  # smooth: 0.5 m/s²
    assert len(out[1]["alerts_opened"]) == 1  # 4 m/s² over 100 ms steps
    assert out[2]["alerts_opened"] == [] and out[3]["alerts_opened"] == []  # gappy window; replayed samples
    alert = alerts_of(app, sid, "sudden_stop")[0]
    assert alert["status"] == "cleared" and alert["cleared_reason"] == "instant_event"
    assert alert["details"]["acceleration_mps2"] == pytest.approx(-4.0)
    events = hazard_events("EXC_DEMO_001", "sudden_stop", START, "chk")
    assert state(app, sid)["active_alerts"] == []
    post(app, sid, {**events[2], "event_id": "gap-only", "observed_at": (START + timedelta(seconds=9)).isoformat()})
    assert "gap longer than 500 ms" in coverage(app, sid)["demo.sudden_stop.v1"]["reason"]
    bad = {**events[1], "event_id": "backwards", "observed_at": (START + timedelta(seconds=10)).isoformat()}
    bad["readings"] = {**bad["readings"], "motion": {**bad["readings"]["motion"],
                                                     "samples": list(reversed(bad["readings"]["motion"]["samples"]))}}
    assert post(app, sid, bad)["alerts_opened"] == []
    assert "strictly increasing" in coverage(app, sid)["demo.sudden_stop.v1"]["reason"]


def test_slope_uses_model_limits_with_hysteresis_and_unknown_where_unconfigured(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        out = run(c, sid, "EXC_DEMO_001", "slope")
        assert out[0]["alerts_opened"] == [] and len(out[1]["alerts_opened"]) == 1  # 17 deg > 15 deg
        assert out[2]["alerts_cleared"] == [] and out[3]["alerts_cleared"] == out[1]["alerts_opened"]  # 14 keeps
        assert coverage(c, sid)["demo.steep_slope.v1"]["status"] == "not_applicable"  # idling on a slope
        truck = session_for(c, "TRK_DEMO_001")["session_id"]
        out = run(c, truck, "TRK_DEMO_001", "slope")
        assert all(r["alerts_opened"] == [] for r in out)
        cov = coverage(c, truck)["demo.steep_slope.v1"]
        assert cov["status"] == "not_configured" and "mining_truck" in cov["reason"]  # never a borrowed limit
        dozer = session_for(c, "DOZ_DEMO_001")["session_id"]
        post(c, dozer, {"event_id": "g1", "observed_at": START.isoformat(), "simulated": True,
                        "readings": {"engine_on": True, "seatbelt_fastened": True, "idle_seconds": 0,
                                     "operating_state": "working", "grade_pct": 40.0}})  # 21.8 deg > 20 (bulldozer)
        alert = state(c, dozer)["active_alerts"][0]
        assert alert["alert_type"] == "steep_slope" and alert["details"]["pitch_derived_from"] == "grade_pct"
        assert alert["details"]["pitch_deg"] == pytest.approx(21.8, abs=0.01)


def start_first_task(c, sid) -> dict:
    task = state(c, sid)["assigned_tasks"][0]
    r = command(c, sid, f"start-{task['task_id']}", "task.start", task["task_id"])
    assert r.status_code == 200, r.text
    return task


def test_fuel_per_cycle_compares_a_covered_task_window_with_its_baseline(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        before = run(c, sid, "EXC_DEMO_001", "fuel_high", "no-task")
        assert all(r["alerts_opened"] == [] for r in before)  # no task in progress: not applicable
        assert "no task in progress" in coverage(c, sid)["demo.fuel_per_cycle.v1"]["reason"]
        start_first_task(c, sid)  # earth excavation, demo baseline 0.45 L/cycle
        later = START + timedelta(hours=1)
        out = [post(c, sid, e) for e in hazard_events("EXC_DEMO_001", "fuel_high", later, "high")]
        assert [i for i, r in enumerate(out) if r["alerts_opened"]] == [10]  # after a full 600 s covered window
        alert = state(c, sid)["active_alerts"][0]
        assert alert["alert_type"] == "abnormal_fuel_per_cycle"
        d = alert["details"]
        assert (d["cycles"], d["litres_per_cycle"], d["ratio"], d["covered_fraction"]) == (20, 0.9, 2.0, 1.0)
        normal = START + timedelta(hours=2)
        out = [post(c, sid, e) for e in hazard_events("EXC_DEMO_001", "fuel_normal", normal, "norm")]
        assert [i for i, r in enumerate(out) if r["alerts_cleared"]] == [10]  # 1.0 x baseline: clears

        reset = START + timedelta(hours=3)
        out = [post(c, sid, e) for e in hazard_events("EXC_DEMO_001", "fuel_reset", reset, "rst")]
        assert "meter reset" in coverage(c, sid)["demo.fuel_per_cycle.v1"]["reason"]

        idle = START + timedelta(hours=4)
        for i in range(11):  # engine on, fuel used, zero cycles: idle fuel, never a ratio or a division by zero
            post(c, sid, {"event_id": f"idle-{i}", "observed_at": (idle + timedelta(seconds=60 * i)).isoformat(),
                          "simulated": True, "readings": {"engine_on": True, "seatbelt_fastened": True,
                                                          "idle_seconds": 0, "operating_state": "working",
                                                          "fuel_meter_l": 2000.0 + i, "load_cycles_total": 9000}})
        assert "zero load cycles" in coverage(c, sid)["demo.fuel_per_cycle.v1"]["reason"]

        gaps = START + timedelta(hours=5)
        for i, minute in enumerate([0, 1, 2, 6, 7, 8, 9, 10]):  # a 4-minute gap: coverage below 80 %
            post(c, sid, {"event_id": f"gap-{i}", "observed_at": (gaps + timedelta(minutes=minute)).isoformat(),
                          "simulated": True, "readings": {"engine_on": True, "seatbelt_fastened": True,
                                                          "idle_seconds": 0, "operating_state": "working",
                                                          "fuel_meter_l": 3000.0 + 2 * minute,
                                                          "load_cycles_total": 10000 + 2 * minute}})
        assert "covered" in coverage(c, sid)["demo.fuel_per_cycle.v1"]["reason"]

        dozer = session_for(c, "DOZ_DEMO_001")["session_id"]
        start_first_task(c, dozer)  # grading: no configured baseline for a bulldozer
        run(c, dozer, "DOZ_DEMO_001", "fuel_high", "doz")
        cov = coverage(c, dozer)["demo.fuel_per_cycle.v1"]
        assert cov["status"] == "not_applicable" and "bulldozer/grading" in cov["reason"]


def test_repeated_violations_create_one_pending_review_and_one_lesson_per_trigger(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        events = hazard_events("EXC_DEMO_001", "repeat_belt", START, "rep")
        out = [post(c, sid, e) for e in events]
        assert [post(c, sid, e)["duplicate"] for e in events] == [True] * len(events)  # retries never inflate
        repeat = [a for a in state(c, sid)["active_alerts"] if a["alert_type"] == "repeated_violations"]
        assert len(repeat) == 1 and repeat[0]["details"]["count"] == 3
        assert repeat[0]["subject_key"] == "seatbelt_engine_on"
        opened_at = [i for i, r in enumerate(out) if repeat[0]["alert_id"] in r["alerts_opened"]]
        assert opened_at == [7]  # the third distinct belt episode
        approvals = state(c, sid)["pending_approvals"]
        assert [(a["kind"], a["status"], a["alert_id"]) for a in approvals] == [
            ("repeated_violations", "pending", repeat[0]["alert_id"])]  # requested, not sent or approved
        assert repeat[0]["training_assignment_id"] is not None
        assert [a["lesson_id"] for a in state(c, sid)["training_assignments"]] == ["L1"]  # one outstanding lesson
        more = [post(c, sid, e) for e in hazard_events("EXC_DEMO_001", "repeat_belt", START + timedelta(minutes=10),
                                                       "rep2")]
        assert not any(repeat[0]["alert_id"] in r["alerts_opened"] for r in more)
        assert len(state(c, sid)["pending_approvals"]) == 1  # still the same trigger
        speech = [e["speech"] for e in c.get(f"/v1/sessions/{sid}/events?after=0", headers=AUTH).json()["events"]
                  if e["alert_id"] == repeat[0]["alert_id"]]
        assert "pending" in speech[0]


def test_malformed_hazard_inputs_are_rejected_and_late_samples_change_nothing(app):
    sid = session_for(app, "EXC_DEMO_001")["session_id"]
    base = {"engine_on": True, "seatbelt_fastened": True, "idle_seconds": 0, "operating_state": "working"}
    bad = [{"proximity": [{"entity_id": "P", "entity_type": "person", "distance_m": -1, "bearing_deg": 0,
                           "quality": "good", "source": "synthetic_scenario"}]},
           {"proximity": [{"entity_id": "P", "entity_type": "person", "distance_m": 3, "bearing_deg": 360,
                           "quality": "good", "source": "synthetic_scenario"}]},
           {"proximity": [{"entity_id": "P", "entity_type": "person", "distance_m": 3, "bearing_deg": 10,
                           "quality": "good", "source": "cat_detect"}]},  # generated data is never relabelled
           {"pitch_deg": 120}]
    for i, extra in enumerate(bad):
        r = app.post(f"/v1/sessions/{sid}/telemetry", headers=AUTH, json={
            "event_id": f"bad-{i}", "observed_at": START.isoformat(), "simulated": True, "readings": {**base, **extra}})
        assert r.status_code == 422
    post(app, sid, {"event_id": "now", "observed_at": (START + timedelta(minutes=5)).isoformat(), "simulated": True,
                    "readings": base})
    late = post(app, sid, {"event_id": "late", "observed_at": START.isoformat(), "simulated": True,
                           "readings": {**base, "proximity": [{"entity_id": "P", "entity_type": "person",
                                                               "distance_m": 2, "bearing_deg": 0, "quality": "good",
                                                               "source": "synthetic_scenario"}]}})
    assert late["stale"] and late["alerts_opened"] == []
