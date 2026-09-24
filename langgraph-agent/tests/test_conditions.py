"""Batch C2 (weather): site weather sources, the working-conditions policy, task-start gating by voice and tap, and
worsening during a task. Tests pin the data clock with a telemetry sample, so they do not depend on the wall clock."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api import schemas as s
from cocoon_agent.api.app import create_app
from cocoon_agent.demo_site import load_fixture, site_today
from cocoon_agent.weather import (
    OpenMeteoWeather, Site, WeatherService, evaluate, load_conditions_policy, parse_open_meteo, start_gate,
)

from .conftest import AUTH, seeded_demo
from .test_tasks import command, say, session_for

IST = timezone(timedelta(hours=5, minutes=30))
POLICY = load_conditions_policy()
SITE = Site("SITE_DEMO_NORTH", "+05:30", 12.9692, 79.1559)
NOW = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)


def local(hhmm: str) -> datetime:
    day = datetime.fromisoformat(site_today(load_fixture()))
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def observe(c, sid, event_id: str, at: datetime, belt=True, state="working", speed=0.0) -> dict:
    r = c.post(f"/v1/sessions/{sid}/telemetry", headers=AUTH, json={
        "event_id": event_id, "observed_at": at.isoformat(), "simulated": True,
        "readings": {"engine_on": True, "seatbelt_fastened": belt, "idle_seconds": 0, "operating_state": state,
                     "speed_kph": speed}})
    assert r.status_code == 200, r.text
    return r.json()


def snap(**values) -> s.WeatherSnapshot:
    base = dict(record_id="wx_t", provider="fixture", kind="synthetic_fixture", site_id="S", valid_from=NOW,
                valid_to=NOW + timedelta(hours=1), retrieved_at=NOW, units={}, quality="complete", provenance="test",
                temperature_c=30.0, relative_humidity_pct=50.0, precipitation_rate_mm_h=0.0, wind_speed_ms=3.0,
                wind_gust_ms=5.0, visibility_m=10000.0)
    return s.WeatherSnapshot(**{**base, **values})


@pytest.mark.parametrize("values,task_type,level,gate", [
    ({}, "earth_excavation", "clear", "proceed"),                                    # normal
    ({"temperature_c": 36.0}, "earth_excavation", "advisory", "proceed"),
    ({"wind_gust_ms": 13.5}, "earth_excavation", "acknowledge", "acknowledge"),
    ({"wind_gust_ms": 12.0}, "demolition", "acknowledge", "acknowledge"),             # stricter task override
    ({"wind_gust_ms": 12.0}, "earth_excavation", "advisory", "proceed"),
    ({"precipitation_rate_mm_h": 12.5}, "trenching", "block", "block"),
    ({"visibility_m": 120.0}, "grading", "block", "block"),                           # "below" comparison
    ({"visibility_m": 800.0}, "grading", "advisory", "proceed"),
    ({"wind_speed_ms": None}, "grading", "clear", "proceed"),                         # gap: that finding is unknown
])
def test_policy_levels_are_deterministic(values, task_type, level, gate):
    check = evaluate(POLICY, snap(**values), "fresh", data_time=NOW, now=NOW, task_type=task_type)
    assert check.level == level and start_gate(POLICY, check) == gate
    assert all(f.basis == "synthetic_demo_assumption" for f in check.findings)
    if values.get("wind_speed_ms", 0) is None:
        assert "wind_speed_ms" in check.reason and next(f for f in check.findings
                                                        if f.variable == "wind_speed_ms").level == "unknown"
    assert evaluate(POLICY, snap(**values), "fresh", data_time=NOW, now=NOW, task_type=task_type) == check


def test_missing_or_misaligned_weather_is_unknown_never_clear():
    none = evaluate(POLICY, None, "unavailable", data_time=NOW, now=NOW, reason="no live weather retrieved yet")
    assert (none.level, none.coverage, none.reason) == ("unknown", "unavailable", "no live weather retrieved yet")
    assert start_gate(POLICY, none) == "proceed"  # policy: missing weather is an advisory, shown to the operator
    indoor = evaluate(POLICY, snap(), "fresh", data_time=NOW, now=NOW, outdoor=False)
    assert indoor.level == "not_applicable" and indoor.findings == []


def test_fixture_follows_the_site_local_data_clock():
    service = WeatherService("fixture")
    for hhmm, gust in (("08:00", 5.0), ("11:30", 14.0), ("12:30", 21.0), ("13:30", 7.0)):
        at = local(hhmm)
        rec, coverage, _ = service.lookup(SITE, at, NOW)
        assert coverage == "fresh" and rec.provider == "fixture" and rec.kind == "synthetic_fixture"
        assert rec.wind_gust_ms == gust and rec.valid_from <= at < rec.valid_to
    assert service.lookup(Site("OTHER", "+05:30", None, None), NOW, NOW)[1] == "unavailable"
    assert WeatherService("off").lookup(SITE, NOW, NOW)[0] is None


OPEN_METEO = {
    "latitude": 12.970123, "longitude": 79.11818, "generationtime_ms": 0.3,
    "current_units": {"time": "iso8601", "interval": "seconds", "temperature_2m": "°C", "relative_humidity_2m": "%",
                      "precipitation": "mm", "wind_speed_10m": "km/h", "wind_gusts_10m": "km/h", "visibility": "m",
                      "weather_code": "wmo code"},
    "current": {"time": "2026-09-24T06:00", "interval": 900, "temperature_2m": 31.2, "relative_humidity_2m": 60,
                "precipitation": 0.5, "wind_speed_10m": 36.0, "wind_gusts_10m": 54.0, "visibility": 22700.0,
                "weather_code": 3},
    "hourly_units": {"time": "iso8601", "temperature_2m": "°C", "relative_humidity_2m": "%", "precipitation": "mm",
                     "wind_speed_10m": "m/s", "wind_gusts_10m": "m/s", "visibility": "furlong", "weather_code": "wmo code"},
    "hourly": {"time": ["2026-09-24T06:00", "2026-09-24T07:00"], "temperature_2m": [31.0, 32.0],
               "relative_humidity_2m": [60, 58], "precipitation": [0.0, 2.0], "wind_speed_10m": [5.0, 4.0],
               "wind_gusts_10m": [10.0, 9.0], "visibility": [20000.0, 900.0], "weather_code": [3, 61]},
}


def test_open_meteo_values_are_validated_and_normalised():
    records = parse_open_meteo(OPEN_METEO, "SITE_DEMO_NORTH", NOW)
    cur, _, hour = records
    assert (cur.kind, cur.wind_speed_ms, cur.wind_gust_ms) == ("modelled_current", 10.0, 15.0)  # km/h -> m/s
    assert cur.precipitation_rate_mm_h == 2.0  # 0.5 mm over the 15-minute interval
    assert (cur.precipitation_window_start, cur.precipitation_window_end) == (NOW - timedelta(minutes=15), NOW)
    assert cur.issued_at is None and cur.latitude == 12.970123 and cur.quality == "complete"
    assert hour.kind == "modelled_forecast" and hour.visibility_m is None and hour.quality == "partial"  # unknown unit
    assert hour.valid_to - hour.valid_from == timedelta(hours=1)


async def test_live_cache_labels_stale_and_misaligned_and_failures_stay_unavailable():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, json=OPEN_METEO)

    live = OpenMeteoWeather(timeout=2, fresh_seconds=900, stale_limit_seconds=3600, misalign_seconds=5400,
                            transport=httpx.MockTransport(handler))
    assert live.lookup(SITE, NOW, NOW)[1] == "unavailable"  # nothing cached yet (a refresh is scheduled)
    await asyncio.sleep(0.05)
    assert len(calls) == 1 and calls[0].host == "api.open-meteo.com"
    assert calls[0].params["latitude"] == "12.9692" and calls[0].params["wind_speed_unit"] == "ms"
    rec, coverage, _ = live.lookup(SITE, NOW + timedelta(minutes=5), NOW + timedelta(minutes=5))
    assert coverage == "fresh" and rec.kind == "modelled_current"
    later = NOW + timedelta(minutes=20)
    rec, coverage, _ = live.lookup(SITE, later, later)
    assert coverage == "stale" and rec.kind == "modelled_forecast"  # past the freshness limit: labelled, still usable
    assert live.lookup(SITE, NOW - timedelta(days=30), NOW)[1] == "misaligned"  # a past replay
    assert live.lookup(SITE, NOW + timedelta(hours=2), NOW + timedelta(hours=2))[1] == "unavailable"  # too old

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    failing = OpenMeteoWeather(timeout=1, fresh_seconds=900, stale_limit_seconds=3600, misalign_seconds=5400,
                               transport=httpx.MockTransport(broken))
    assert await failing.refresh(SITE, NOW) is False
    rec, coverage, reason = failing.lookup(SITE, NOW, NOW)
    assert rec is None and coverage == "unavailable"  # never a synthetic "sunny" substitute


def test_voice_start_needs_acknowledgement_and_the_saved_check_is_kept(tmp_path):
    settings, _ = seeded_demo(tmp_path, COCOON_WEATHER_MODE="fixture")
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        observe(c, sid, "o1", local("11:30"))  # data clock: gusts 14 m/s in the fixture
        report = say(c, sid, "t0", "what are the conditions like?")
        assert report["actions"][0]["type"] == "conditions_report"
        assert report["actions"][0]["check"]["level"] == "acknowledge" and "gusts 14" in report["speech"]
        stop = say(c, sid, "t1", "start the next task")
        action = stop["actions"][0]
        assert (action["type"], action["reason"]) == ("task_rejected", "conditions_need_acknowledgement")
        assert action["task_title"] == "Excavate the north pit bench" and stop["action_records"] == []
        assert "Say start anyway" in stop["speech"]
        state = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
        assert state["pending_question"]["kind"] == "task_start_ack"
        assert state["assigned_tasks"][0]["status"] == "scheduled"
        assert state["assigned_tasks"][0]["conditions"]["level"] == "acknowledge"
        assert state["site_conditions"]["weather"]["provider"] == "fixture"

        go = say(c, sid, "t2", "start anyway")
        started = go["actions"][0]
        assert started["type"] == "task_started" and started["conditions"]["acknowledged"] is True
        assert started["conditions"]["check_id"].startswith("CHK-") and started["conditions"]["level"] == "acknowledge"
        assert say(c, sid, "t2", "start anyway") == go  # replay
        state = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
        task = state["assigned_tasks"][0]
        assert task["status"] == "in_progress" and task["start_check"]["check_id"] == started["conditions"]["check_id"]
        assert state["pending_question"] is None

        # a bare "yes" for a pending start is only accepted when nothing else is waiting
        observe(c, sid, "o2", local("11:35"))
        done = say(c, sid, "t3", "I finished the task")["actions"][0]
        assert done["type"] == "task_completed"
        say(c, sid, "t4", "start the next task")
        yes = say(c, sid, "t5", "yes")["actions"][0]
        assert yes["type"] == "task_started" and yes["conditions"]["acknowledged"] is True
        # the saved start check is never rewritten by later weather
        observe(c, sid, "o3", local("13:30"))
        again = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["assigned_tasks"][0]
        assert again["start_check"] == task["start_check"]


def test_tap_start_uses_the_same_gate_and_a_block_cannot_be_acknowledged(tmp_path):
    settings, _ = seeded_demo(tmp_path, COCOON_WEATHER_MODE="fixture")
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        tasks = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["assigned_tasks"]
        observe(c, sid, "o1", local("11:30"))  # gusts 14: needs acknowledgement
        need = command(c, sid, "c1", "task.start", tasks[0]["task_id"])
        assert need.status_code == 409 and need.json()["error"]["code"] == "invalid_transition"
        assert any(d["field"] == "body.payload.acknowledge_conditions" for d in need.json()["error"]["details"])
        ok = c.post(f"/v1/sessions/{sid}/commands", headers=AUTH, json={
            "command_id": "c2", "kind": "task.start",
            "payload": {"task_id": tasks[0]["task_id"], "acknowledge_conditions": True}})
        assert ok.status_code == 200 and ok.json()["task"]["start_check"]["acknowledged"] is True

        observe(c, sid, "o2", local("12:30"))  # storm: gusts 21, rain 7, visibility 400
        r = command(c, sid, "c3", "task.start", tasks[1]["task_id"])
        assert r.status_code == 409
        assert {"field": "conditions.level", "issue": "block"} in r.json()["error"]["details"]
        forced = c.post(f"/v1/sessions/{sid}/commands", headers=AUTH, json={
            "command_id": "c4", "kind": "task.start",
            "payload": {"task_id": tasks[1]["task_id"], "acknowledge_conditions": True}})
        assert forced.status_code == 409  # a block is never overridden by an acknowledgement
        done = command(c, sid, "c5", "task.complete", tasks[0]["task_id"])
        assert done.status_code == 200
        voice = say(c, sid, "t1", "start the next task")["actions"][0]
        assert voice["reason"] == "conditions_block" and voice["task_id"] == tasks[1]["task_id"]


def test_worsening_during_an_outdoor_task_is_announced_once_explained_and_cleared(tmp_path):
    settings, _ = seeded_demo(tmp_path, COCOON_WEATHER_MODE="fixture")
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        observe(c, sid, "o1", local("10:30"))  # hot and dry: clear
        started = say(c, sid, "t1", "start the next task")["actions"][0]
        assert started["type"] == "task_started" and started["conditions"]["level"] == "clear"
        quiet = observe(c, sid, "o2", local("11:30"))  # acknowledge-level gusts: above start and at the minimum
        assert len(quiet["alerts_opened"]) == 1
        worse = observe(c, sid, "o3", local("12:10"))  # storm: the same episode rises to block (an update)
        assert worse["alerts_opened"] == [] and worse["alerts_updated"] == quiet["alerts_opened"]
        assert [a.endswith("_escalated") for a in worse["announcements_created"]] == [True]
        again = observe(c, sid, "o3b", local("12:20"))  # still block: nothing new
        assert again["alerts_updated"] == [] and again["announcements_created"] == []
        state = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
        alert = next(a for a in state["active_alerts"] if a["alert_type"] == "working_conditions")
        assert alert["details"]["start_level"] == "clear" and alert["details"]["level"] == "acknowledge"  # as opened
        assert alert["level"] == "block" and alert["severity"] == "critical"
        assert [(u["previous_level"], u["level"]) for u in alert["updates"]] == [("acknowledge", "block")]
        assert alert["updates"][0]["details"]["check"]["weather"]["wind_gust_ms"] == 21.0  # its own evidence
        assert alert["details"]["check_id"].startswith("CHK-") and alert["source_status"] == "demo_assumption"
        events = c.get(f"/v1/sessions/{sid}/events?after=0", headers=AUTH).json()["events"]
        warn = [e for e in events if e["alert_id"] == alert["alert_id"]]
        assert [e["type"] for e in warn] == ["alert_started", "alert_escalated"] and "gusts 14" in warn[0]["speech"]
        why = say(c, sid, "t2", "why did you warn me about the weather?")["actions"][0]
        assert why["alert"]["alert_id"] == alert["alert_id"] and "synthetic fixture weather" in why["alert"]["explanation"]
        cleared = observe(c, sid, "o4", local("13:10"))  # light showers: back to the start level
        assert cleared["alerts_cleared"] == [alert["alert_id"]]
        observe(c, sid, "o4", local("13:10"))  # duplicate sample: nothing new


def test_missing_weather_is_visible_and_start_proceeds_as_an_advisory(tmp_path):
    settings, _ = seeded_demo(tmp_path, COCOON_WEATHER_MODE="off")
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        out = say(c, sid, "t1", "start the next task")
        check = out["actions"][0]["conditions"]
        assert (check["level"], check["coverage"]) == ("unknown", "unavailable")
        assert "Weather isn't available" in out["speech"]


def test_a_hanging_weather_provider_never_delays_a_seatbelt_warning(tmp_path):
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=OPEN_METEO)

    live = WeatherService("live", live=OpenMeteoWeather(timeout=30, fresh_seconds=900, stale_limit_seconds=3600,
                                                        misalign_seconds=5400, transport=httpx.MockTransport(hang)))
    settings, _ = seeded_demo(tmp_path, COCOON_WEATHER_MODE="live")
    with TestClient(create_app(settings, weather=live)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        started = say(c, sid, "t1", "start the next task")["actions"][0]
        assert started["conditions"]["coverage"] == "unavailable"  # nothing cached; the start did not wait
        begin = time.monotonic()
        warn = observe(c, sid, "o1", datetime.now(timezone.utc), belt=False)
        assert time.monotonic() - begin < 2 and len(warn["announcements_created"]) == 1
