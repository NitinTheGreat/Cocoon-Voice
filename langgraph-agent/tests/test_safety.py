"""Batch B3: selected-machine replay, belt/idle rule episodes, automatic drafts and proactive announcements."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cocoon_agent import service as service_module
from cocoon_agent.api.app import create_app
from cocoon_agent.simulation import dataset_events, scenario_events

from .conftest import AUTH, MACHINES, seeded_demo
from .test_tasks import session_for

START = datetime(2026, 9, 24, 2, 10, tzinfo=timezone.utc)


def post(c: TestClient, sid: str, body: dict) -> dict:
    r = c.post(f"/v1/sessions/{sid}/telemetry", headers=AUTH, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def sample(event_id: str, at: datetime, *, engine=True, belt=True, state: str | None = "idle", speed=0.0) -> dict:
    readings = {"engine_on": engine, "seatbelt_fastened": belt, "idle_seconds": 0}
    if state is not None:
        readings.update(operating_state=state, speed_kph=speed)
    return {"event_id": event_id, "observed_at": at.isoformat(), "simulated": True, "readings": readings}


def alerts(c: TestClient, sid: str) -> list[dict]:
    return c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["active_alerts"]


def test_cat320_replay_warns_before_motion_without_spam_and_links_one_draft(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    events = scenario_events("EXC_DEMO_001", "belt_idle", START, "r1", seed=7)
    assert events == scenario_events("EXC_DEMO_001", "belt_idle", START, "r1", seed=7)  # deterministic
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        assert c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["machine_state"]["status"] == "unavailable"
        results = [post(c, sid, e) for e in events]
        first_motion = next(i for i, e in enumerate(events) if e["readings"]["speed_kph"] > 0)
        belt_opened = next(i for i, r in enumerate(results) if r["alerts_opened"])
        assert belt_opened < first_motion and events[belt_opened]["readings"]["operating_state"] == "idle"
        # only three announcements in the whole replay: belt start, prolonged idle, belt cleared
        announced = [a for r in results for a in r["announcements_created"]]
        assert len(announced) == 3 and len(set(announced)) == 3
        assert [len(r["drafts_created"]) for r in results].count(1) == 1  # one draft for the one belt situation
        assert results[3]["alerts_opened"] == [] and results[3]["announcements_created"] == []  # repeated sample

        page = c.get(f"/v1/sessions/{sid}/events?after=0", headers=AUTH).json()  # no turn needed
        assert [e["type"] for e in page["events"]] == ["shift_briefing", "alert_started", "alert_started",
                                                       "alert_cleared"]
        state = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
        assert state["active_alerts"] == []  # moving and belted: every condition cleared
        assert state["machine_state"]["status"] == "fresh"
        assert state["machine_state"]["operating_state"] == "working"
        draft = state["incident_drafts"][0]
        assert draft["origin"] == "auto_draft"
        assert datetime.fromisoformat(draft["occurred_at"]) == datetime.fromisoformat(events[belt_opened]["observed_at"])
        assert state["incidents"] == []  # a draft is not a confirmed incident

    with TestClient(create_app(settings)) as c:  # orderly restart keeps episodes, drafts and replays
        from cocoon_agent.store import Store
        store = Store(settings.db_path)
        rows = store._all("SELECT alert_type, correlated_alert_id, draft_incident_id, announced, evidence_json,"
                          " policy_version FROM alerts WHERE session_id = ? ORDER BY started_at", (sid,))
        store.close()
        kinds = {r["alert_type"]: r for r in rows}
        assert set(kinds) == {"seatbelt_unfastened", "idle_unbelted", "prolonged_idle"}
        belt = kinds["seatbelt_unfastened"]
        assert belt["draft_incident_id"] == draft["draft_id"] and belt["announced"] == 1
        assert kinds["idle_unbelted"]["correlated_alert_id"] is not None and kinds["idle_unbelted"]["announced"] == 0
        assert kinds["idle_unbelted"]["draft_incident_id"] is None
        assert all(r["policy_version"] == "demo-safety-2026-09-24.2" for r in rows)
        assert '"idle_seconds_observed":315' in kinds["prolonged_idle"]["evidence_json"]
        again = post(c, sid, events[2])
        assert again["duplicate"] and again["alerts_opened"] == results[2]["alerts_opened"]


def test_idle_duration_follows_observation_time_and_unknown_state_is_not_idle(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        post(c, sid, sample("i0", START))
        assert post(c, sid, sample("i1", START + timedelta(seconds=299)))["alerts_opened"] == []
        opened = post(c, sid, sample("i2", START + timedelta(seconds=301)))  # posted instantly on the wall clock
        assert len(opened["alerts_opened"]) == 1
        active = alerts(c, sid)
        assert active[0]["alert_type"] == "prolonged_idle"
        assert active[0]["evidence"]["idle_seconds_observed"] == 301
        assert active[0]["source_status"] == "demo_assumption"
        # a sample without operating_state is unknown for idling: the idle episode neither clears nor restarts
        unknown = post(c, sid, sample("i3", START + timedelta(seconds=320), state=None))
        assert unknown["alerts_cleared"] == [] and len(alerts(c, sid)) == 1
        cleared = post(c, sid, sample("i4", START + timedelta(seconds=330), state="working"))
        assert cleared["alerts_cleared"] == opened["alerts_opened"] and cleared["announcements_created"] == []


def test_clear_retrigger_duplicates_late_and_conflicting_samples(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        events = scenario_events("EXC_DEMO_001", "belt_retrigger", START, "rt")
        out = [post(c, sid, e) for e in events]
        first, second = out[1]["alerts_opened"], out[4]["alerts_opened"]
        assert first and second and first != second  # two violations, two episodes
        assert out[3]["alerts_cleared"] == first
        assert len(out[1]["drafts_created"]) == 1 and len(out[4]["drafts_created"]) == 1
        assert out[1]["drafts_created"] != out[4]["drafts_created"]
        late = post(c, sid, sample("late", START, belt=True, state="working"))
        assert late["stale"] and late["ignored_reason"] == "late" and alerts(c, sid)[0]["alert_id"] == second[0]
        # same observation time as the newest applied sample, different readings: cannot overwrite it
        conflicting = post(c, sid, {**sample("dupe-time", START, belt=True, state="working"),
                                    "observed_at": events[4]["observed_at"]})
        assert conflicting["ignored_reason"] == "conflicting" and len(alerts(c, sid)) == 1


def test_every_catalog_machine_can_be_selected_and_stays_isolated(tmp_path, monkeypatch):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sessions = {m: session_for(c, m)["session_id"] for m in MACHINES}
        for m in MACHINES:
            out = [post(c, sessions[m], e) for e in scenario_events(m, "selection_check", START, "sel")]
            assert out[1]["alerts_opened"] and out[2]["alerts_cleared"] == out[1]["alerts_opened"]
        for m, sid in sessions.items():
            state = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
            assert len(state["incident_drafts"]) == 1 and state["incident_drafts"][0]["machine_id"] == m
            events = c.get(f"/v1/sessions/{sid}/events?after=0", headers=AUTH).json()["events"]
            assert [e["type"] for e in events] == ["shift_briefing", "alert_started", "alert_cleared"]
        # freshness uses the receipt clock: no new sample for longer than the limit -> stale, never "healthy"
        later = datetime.now(timezone.utc) + timedelta(seconds=settings.telemetry_stale_seconds + 5)
        monkeypatch.setattr(service_module, "utcnow", lambda: later)
        state = c.get(f"/v1/sessions/{sessions['BHL_DEMO_001']}/state", headers=AUTH).json()
        assert state["machine_state"]["status"] == "stale"


def test_dataset_replay_uses_catalog_rows_when_the_dataset_is_present():
    root = Path(__file__).resolve().parents[2] / "Cocoon_Dataset_v1"
    if not (root / "data" / "generated" / "history_minutes.csv").is_file():
        pytest.skip("pinned dataset not present locally (it is not tracked)")
    events = dataset_events(root, "LDR_DEMO_001", START, "ds", limit=5)
    assert len(events) == 5 and all(e["provenance"]["origin"] == "dataset_replay" for e in events)
    assert [e["observed_at"] for e in events][0] == START.isoformat()
    assert all(e["readings"]["operating_state"] in ("off", "idle", "working", "travel") for e in events)
