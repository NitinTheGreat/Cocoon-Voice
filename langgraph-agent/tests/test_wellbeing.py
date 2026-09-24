"""Batch D1: purpose-specific consent, private wellbeing samples, deterministic advice, breaks and the supervisor
risk projection (category only)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.demo_site import load_fixture, site_today
from cocoon_agent.wellbeing import heat_band, heat_index_c, load_consent_notices

from .conftest import AUTH, seeded_demo
from .test_auth import bearer, issue
from .test_tasks import say, session_for

IST = timezone(timedelta(hours=5, minutes=30))
NOTICES = load_consent_notices()
OP = "OP_DEMO_1_1"
SENTINEL_HR = 187.3


def local(hhmm: str, seconds: int = 0) -> datetime:
    day = datetime.fromisoformat(site_today(load_fixture()))
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST) + timedelta(seconds=seconds)


def change(c, headers, cid, purpose, action, expected, notice=None):
    return c.post(f"/v1/operators/{OP}/consents", headers=headers, json={
        "change_id": cid, "purpose": purpose, "action": action, "expected_version": expected,
        "notice_version": notice or NOTICES[purpose]["notice_version"], "is_synthetic_demo_record": True})


def sample(c, sid, sample_id, at, hr=None, skin=None, headers=AUTH, quality="good"):
    body = {"sample_id": sample_id, "observed_at": at.isoformat(), "window_seconds": 60, "quality": quality,
            "source": "synthetic_wearable_fixture", "simulated": True}
    if hr is not None:
        body["heart_rate_bpm"] = hr
    if skin is not None:
        body["skin_temp_c"] = skin
    return c.post(f"/v1/sessions/{sid}/wellbeing/samples", headers=headers, json=body)


def observe(c, sid, event_id, at):
    r = c.post(f"/v1/sessions/{sid}/telemetry", headers=AUTH, json={
        "event_id": event_id, "observed_at": at.isoformat(), "simulated": True,
        "readings": {"engine_on": True, "seatbelt_fastened": True, "idle_seconds": 0, "operating_state": "working"}})
    assert r.status_code == 200, r.text


def dump(settings) -> str:
    conn = sqlite3.connect(settings.db_path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


@pytest.fixture
def world(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    op_token, _ = issue(settings, tmp_path, "operator", "--operator-id", OP, name="op")
    other, _ = issue(settings, tmp_path, "operator", "--operator-id", "OP_DEMO_2_1", name="other")
    sup, _ = issue(settings, tmp_path, "supervisor", "--principal-id", "sup-north", name="sup")
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        yield {"c": c, "settings": settings, "sid": sid, "op": bearer(op_token), "other": bearer(other),
               "sup": bearer(sup), "wb": c.app.state.service.wellbeing}


def test_heat_index_follows_the_nws_method_and_bands():
    assert heat_index_c(20.0, 50.0) == pytest.approx(19.4, abs=0.1)        # simple formula below 80 degF
    assert heat_index_c(36.5, 48.0) == pytest.approx(43.5, abs=0.2)        # Rothfusz regression (~110 degF)
    assert heat_index_c(29.0, 88.0) > heat_index_c(29.0, 60.0)              # high-humidity adjustment branch
    assert heat_index_c(40.0, 10.0) < heat_index_c(40.0, 13.0)              # low-humidity adjustment branch
    assert [heat_band(x) for x in (25.0, 28.0, 33.0, 40.0, 52.0)] == \
        ["none", "caution", "extreme_caution", "danger", "extreme_danger"]
    for bad in ((float("nan"), 50.0), (30.0, 120.0)):
        with pytest.raises(ValueError):
            heat_index_c(*bad)


def test_consent_is_operator_owned_versioned_and_idempotent(world):
    c = world["c"]
    state = c.get(f"/v1/operators/{OP}/consents", headers=world["op"]).json()
    assert state["version"] == 0 and {x["status"] for x in state["consents"]} == {"not_set"}  # no implicit opt-in
    assert c.get(f"/v1/operators/{OP}/consents", headers=AUTH).status_code == 200            # voice service reads
    assert c.get(f"/v1/operators/{OP}/consents", headers=world["sup"]).status_code == 403
    assert c.get(f"/v1/operators/{OP}/consents", headers=world["other"]).status_code == 404
    # only the operator's own token may decide: not the service credential, not a supervisor, not another operator
    assert change(c, AUTH, "c1", "vitals_processing", "grant", 0).status_code == 403
    assert change(c, world["sup"], "c1", "vitals_processing", "grant", 0).status_code == 403
    assert change(c, world["other"], "c1", "vitals_processing", "grant", 0).status_code == 404
    assert change(c, world["op"], "c1", "vitals_processing", "grant", 0, notice="old-notice").status_code == 422
    # sharing needs processing first
    r = change(c, world["op"], "c0", "risk_sharing_supervisor", "grant", 0)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"
    r = change(c, world["op"], "c1", "vitals_processing", "grant", 0)
    assert r.status_code == 200 and r.json()["applied"] and r.json()["state"]["version"] == 1
    assert change(c, world["op"], "c2", "risk_sharing_supervisor", "grant", 1).json()["state"]["version"] == 2
    # an old offline grant (stale expected_version) cannot override a newer decision
    r = change(c, world["op"], "c3", "vitals_processing", "revoke", 2)
    assert r.json()["cascaded"] == ["risk_sharing_supervisor"]
    stale = change(c, world["op"], "c4", "vitals_processing", "grant", 2)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    # identical retry of the first grant is NOT re-applied over the later revocation
    replay = change(c, world["op"], "c1", "vitals_processing", "grant", 0).json()
    assert replay["applied"] is False and replay["duplicate"] is True
    statuses = {x["purpose"]: x["status"] for x in replay["state"]["consents"]}
    assert statuses == {"vitals_processing": "revoked", "risk_sharing_supervisor": "revoked"}
    r = change(c, world["op"], "c1", "vitals_processing", "revoke", 0)
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"


def test_without_consent_nothing_is_retained_echoed_or_logged(world, caplog):
    c, sid = world["c"], world["sid"]
    caplog.set_level(logging.DEBUG)
    r = sample(c, sid, "s1", local("11:01"), hr=SENTINEL_HR, skin=34.1)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected_consent" and body["retained"] is False
    assert str(SENTINEL_HR) not in r.text
    assert sample(c, sid, "s1", local("11:01"), hr=SENTINEL_HR).json()["status"] == "rejected_consent"
    for bad in ({"heart_rate_bpm": 999.5}, {"skin_temp_c": 98.6}, {"heart_rate_bpm": "NaN"}, {"heart_rate_f": 90}):
        r = c.post(f"/v1/sessions/{sid}/wellbeing/samples", headers=AUTH, json={
            "sample_id": "bad", "observed_at": local("11:02").isoformat(), "window_seconds": 60, "quality": "good",
            "source": "synthetic_wearable_fixture", "simulated": True, **bad})
        assert r.status_code == 422
        assert all(str(v) not in r.text for v in bad.values() if isinstance(v, float))  # never echoed
    assert c.post(f"/v1/sessions/{sid}/wellbeing/samples", headers=world["other"], json={}).status_code == 404
    text = dump(world["settings"])
    assert str(SENTINEL_HR) not in text and "999.5" not in text
    assert str(SENTINEL_HR) not in caplog.text
    with sqlite3.connect(world["settings"].db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM wellbeing_samples").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM wellbeing_sample_outcomes").fetchone()[0] == 0
    # machine safety still runs regardless of wellbeing consent
    r = c.post(f"/v1/sessions/{sid}/telemetry", headers=AUTH, json={
        "event_id": "m1", "observed_at": local("11:03").isoformat(), "simulated": True,
        "readings": {"engine_on": True, "seatbelt_fastened": False, "idle_seconds": 0}})
    assert r.json()["alerts_opened"]


def test_advice_opens_once_explains_holds_on_missing_data_and_clears(world):
    c, sid, op = world["c"], world["sid"], world["op"]
    change(c, op, "g1", "vitals_processing", "grant", 0)
    observe(c, sid, "m1", local("11:00"))  # data clock
    assert c.post(f"/v1/sessions/{sid}/commands", headers=op, json={
        "command_id": "b1", "kind": "break.start", "payload": {}}).status_code == 200  # break factor clear
    outs = [sample(c, sid, f"hi{i}", local("11:01", 30 * i), hr=131 + i, skin=34.0, headers=op).json()
            for i in range(3)]
    opened = outs[2]["advice_opened"]
    assert [o["advice_opened"] for o in outs[:2]] == [[], []] and len(opened) == 1   # needs 3 samples
    assert outs[0]["status"] == "accepted" and outs[0]["retained"]
    vitals = next(r for r in outs[2]["rules"] if r["factor"] == "vitals_strain")
    assert vitals["status"] == "advisory" and vitals["basis"] == "synthetic_demo_assumption"
    heat = next(r for r in outs[2]["rules"] if r["factor"] == "heat_index")
    assert heat["status"] == "unknown"  # weather off: never read as normal
    # identical retry: saved outcome, no second episode or announcement
    again = sample(c, sid, "hi2", local("11:01", 60), hr=133, skin=34.0, headers=op).json()
    assert again["status"] == "duplicate" and again["advice_opened"] == opened
    assert sample(c, sid, "hi2", local("11:01", 60), hr=140, headers=op).status_code == 409
    events = c.get(f"/v1/sessions/{sid}/events", headers=op).json()["events"]
    advice_events = [e for e in events if e["type"] == "wellbeing_advice"]
    assert len(advice_events) == 1 and not any(ch.isdigit() for ch in advice_events[0]["speech"])
    view = c.get(f"/v1/sessions/{sid}/wellbeing", headers=op).json()
    assert view["active_advice"][0]["advice_id"] == opened[0]
    assert "average heart rate 132 bpm" in view["active_advice"][0]["explanation"]
    spoken = say(c, sid, "why1", "Why did you suggest a break?", headers=op)
    assert spoken["actions"][0]["event"] == "advice_explained" and "132 bpm" in spoken["speech"]
    # one late-arriving lone sample: too few samples to decide, the episode holds (missing data is not recovery)
    hold = sample(c, sid, "late1", local("11:20"), hr=90, headers=op).json()
    assert hold["advice_cleared"] == [] and \
        next(r for r in hold["rules"] if r["factor"] == "vitals_strain")["status"] == "unknown"
    assert sample(c, sid, "older", local("11:05"), hr=90, headers=op).json()["status"] == "ignored_late"
    first = sample(c, sid, "lo0", local("11:21"), hr=92, headers=op).json()   # 2 samples in the window: still held
    assert first["advice_cleared"] == []
    assert sample(c, sid, "lo1", local("11:21", 30), hr=92, headers=op).json()["advice_cleared"] == opened
    assert c.get(f"/v1/sessions/{sid}/wellbeing", headers=op).json()["active_advice"] == []
    state = c.get(f"/v1/sessions/{sid}/state", headers=op).json()
    assert state["wellbeing"]["open_break"]["break_id"]


def test_heat_and_break_factors_need_no_vitals_and_breaks_are_explicit(tmp_path):
    settings, _ = seeded_demo(tmp_path, COCOON_WEATHER_MODE="fixture")
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        observe(c, sid, "m1", local("11:10"))
        out = sample(c, sid, "s1", local("11:10"), hr=120).json()      # no consent: the value is refused...
        assert out["status"] == "rejected_consent"
        rules = {r["factor"]: r for r in out["rules"]}                   # ...but environment and breaks still count
        assert rules["heat_index"]["status"] == "high" and rules["heat_index"]["basis"] == "published_guidance"
        assert rules["break_due"]["status"] == "advisory"                # 07:00 shift start, no recorded break
        assert rules["vitals_strain"]["status"] == "unknown"
        advice = c.get(f"/v1/sessions/{sid}/wellbeing", headers=AUTH).json()["active_advice"][0]
        assert advice["level"] == "high" and "NWS chart" in advice["explanation"]
        assert all(f.get("mean_heart_rate_bpm") is None for f in advice["factors"])
        # engine off / silence is not a break; only the explicit command records one
        r = say(c, sid, "t1", "I'm taking a break")
        assert r["actions"][0]["event"] == "break_started"
        assert say(c, sid, "t1", "I'm taking a break")["actions"] == r["actions"]      # saved turn result
        assert say(c, sid, "t2", "I'm taking a break")["actions"][0]["event"] == "break_rejected"
        def plan():
            return [(t["task_id"], t["status"], t["version"], t["scheduled_start_at"])
                    for t in c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["assigned_tasks"]]

        tasks_before = plan()
        ended = say(c, sid, "t3", "I'm back from my break")
        assert ended["actions"][0]["event"] == "break_ended"
        assert plan() == tasks_before  # a break never changes a task or the schedule
        r = c.post(f"/v1/sessions/{sid}/commands", headers=AUTH,
                   json={"command_id": "e1", "kind": "break.end", "payload": {}})
        assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"


def test_revocation_deletes_samples_withdraws_advice_and_hides_risk(world):
    c, sid, op, wb = world["c"], world["sid"], world["op"], world["wb"]
    change(c, op, "g1", "vitals_processing", "grant", 0)
    change(c, op, "g2", "risk_sharing_supervisor", "grant", 1)
    observe(c, sid, "m1", local("11:00"))
    c.post(f"/v1/sessions/{sid}/commands", headers=op, json={"command_id": "b1", "kind": "break.start", "payload": {}})
    for i in range(3):
        sample(c, sid, f"s{i}", local("11:01", 30 * i), hr=145, headers=op)
    store = c.app.state.service.store
    now = datetime.now(timezone.utc)
    with store._lock:
        risk = wb.risk(store._conn, OP, "SITE_DEMO_NORTH", now)
    assert risk.risk_level == "high" and risk.freshness == "fresh"
    assert set(risk.model_dump()) == {"operator_id", "site_id", "risk_level", "unavailable_reason", "freshness",
                                      "as_of"}
    change(c, op, "r1", "vitals_processing", "revoke", 2)
    with store._lock:
        risk = wb.risk(store._conn, OP, "SITE_DEMO_NORTH", now)
    assert (risk.risk_level, risk.unavailable_reason) == ("unavailable", "consent_revoked")
    with sqlite3.connect(world["settings"].db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM wellbeing_samples").fetchone()[0] == 0
        assert conn.execute("SELECT status, end_reason FROM wellbeing_advice").fetchall() == \
            [("withdrawn", "consent_revoked")]
        feed = conn.execute("SELECT type, ref_type, ref_id FROM supervisor_feed").fetchall()
    assert feed and set(feed) == {("risk.changed", "operator", OP)}
    # a retry of a sample deleted by the revocation reports it gone, never re-accepts it
    assert sample(c, sid, "s0", local("11:01"), hr=145, headers=op).json()["status"] == "expired"
    assert sample(c, sid, "s9", local("11:09"), hr=145, headers=op).json()["status"] == "rejected_consent"


def test_bounded_retention_purges_raw_samples(world):
    c, sid, op, wb = world["c"], world["sid"], world["op"], world["wb"]
    change(c, op, "g1", "vitals_processing", "grant", 0)
    assert sample(c, sid, "s1", local("11:01"), hr=100, headers=op).json()["retained"]
    assert wb.purge_expired(datetime.now(timezone.utc) + timedelta(hours=wb.policy.retention_hours + 1)) == 1
    assert sample(c, sid, "s1", local("11:01"), hr=100, headers=op).json()["status"] == "expired"
