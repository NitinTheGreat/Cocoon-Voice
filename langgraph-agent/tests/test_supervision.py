"""Batch D2: supervisor site scope, safe projections, durable approvals with separate application, the supervisor
change feed (over a real localhost socket) and forecast-backed task reordering."""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.auth import fmt_time, generate_token, token_digest
from cocoon_agent.demo_site import load_fixture, site_today
from cocoon_agent.migrations import MIGRATIONS, migrate
from cocoon_agent.simulation import hazard_events
from cocoon_agent.store import Store

from .conftest import AUTH, seeded_demo
from .test_auth import bearer, issue, run_admin
from .test_tasks import say, session_for
from .test_wellbeing import change, sample

IST = timezone(timedelta(hours=5, minutes=30))
OP, SITE, MACHINE = "OP_DEMO_1_1", "SITE_DEMO_NORTH", "EXC_DEMO_001"
DEMO = Path(__file__).resolve().parents[1] / "demo"
SENTINEL_TEXT = "SENTINEL-PRIVATE-NARRATIVE"


def local(hhmm: str, seconds: int = 0) -> datetime:
    day = datetime.fromisoformat(site_today(load_fixture()))
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST) + timedelta(seconds=seconds)


def add_foreign_site(settings) -> None:
    store = Store(settings.db_path)
    store.seed_demo_site({"sites": [("site_id", ("SITE_DEMO_SOUTH", "Synthetic south yard", "Asia/Kolkata", "+05:30",
                                                 "test-1", "synthetic_demo_fixture"))]})
    store.close()


def legacy_supervisor_token(settings, principal_id: str) -> str:
    """A token issued before D2 (scopes me:read only) for an existing supervisor principal."""
    store = Store(settings.db_path)
    token = generate_token()
    now = datetime.now(timezone.utc)
    store.issue_actor_token(kind="supervisor", principal_id=principal_id, operator_id=None,
                            operator_catalog_sha256=None, display_name=None, token_sha256=token_digest(token),
                            scopes=("me:read",), issued_at=fmt_time(now), expires_at=fmt_time(now + timedelta(hours=1)))
    store.close()
    return token


def world_for(tmp_path, **overrides):
    settings, _ = seeded_demo(tmp_path, **overrides)
    add_foreign_site(settings)
    op, _ = issue(settings, tmp_path, "operator", "--operator-id", OP, name="op")
    sup, sup_id = issue(settings, tmp_path, "supervisor", "--principal-id", "sup-north", name="sup")
    south, _ = issue(settings, tmp_path, "supervisor", "--principal-id", "sup-south", name="south")
    assert run_admin(settings, "grant-site", "--principal-id", "sup-north", "--site-id", SITE)[0] == 0
    assert run_admin(settings, "grant-site", "--principal-id", "sup-south", "--site-id", "SITE_DEMO_SOUTH")[0] == 0
    code, _, err = run_admin(settings, "grant-site", "--principal-id", "sup-north", "--site-id", "NOWHERE")
    assert code == 1 and "no such site" in err
    legacy = legacy_supervisor_token(settings, "sup-north")
    return settings, {"op": bearer(op), "sup": bearer(sup), "south": bearer(south), "legacy": bearer(legacy),
                      "sup_token_id": sup_id}


@pytest.fixture
def world(tmp_path):
    settings, tokens = world_for(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, MACHINE)["session_id"]
        yield {"c": c, "settings": settings, "sid": sid, **tokens}


def replay(c, sid, events):
    for e in events:
        r = c.post(f"/v1/sessions/{sid}/telemetry", headers=AUTH, json=e)
        assert r.status_code == 200, r.text


def repeat_review(c, sid, run="r1") -> dict:
    replay(c, sid, hazard_events(MACHINE, "repeat_belt", local("10:00"), run))
    state = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
    return next(a for a in state["pending_approvals"] if a["kind"] == "repeated_violations")


def decide(c, headers, approval, decision_id, decision="approve", version=None, digest=None):
    return c.post(f"/v1/approvals/{approval['approval_id']}/decision", headers=headers, json={
        "decision_id": decision_id, "decision": decision, "expected_version": version or approval["version"],
        "payload_sha256": digest or approval["payload_sha256"]})


def dump(settings) -> str:
    conn = sqlite3.connect(settings.db_path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


# ------------------------------------------------------------------ scope


def test_site_scope_comes_only_from_cli_grants(world):
    c = world["c"]
    me = c.get("/v1/me", headers=world["sup"]).json()
    assert me["site_ids"] == [SITE] and "supervise" in me["scopes"]
    assert c.get(f"/v1/supervisor/overview?site_id={SITE}", headers=world["sup"]).status_code == 200
    for who in ("south", "legacy", "op"):
        r = c.get(f"/v1/supervisor/overview?site_id={SITE}", headers=world[who])
        assert r.status_code == 403, who
    assert c.get(f"/v1/supervisor/overview?site_id={SITE}", headers=AUTH).status_code == 403
    assert c.get("/v1/supervisor/overview?site_id=SITE_DEMO_SOUTH", headers=world["sup"]).status_code == 403
    # every session route still refuses supervisors
    assert c.get(f"/v1/sessions/{world['sid']}/state", headers=world["sup"]).status_code == 403
    # revoking the grant applies to the next request
    assert run_admin(world["settings"], "revoke-site", "--principal-id", "sup-north", "--site-id", SITE)[0] == 0
    assert c.get(f"/v1/supervisor/overview?site_id={SITE}", headers=world["sup"]).status_code == 403


def test_projections_never_carry_private_values_and_follow_current_consent(world):
    c, sid, op, sup = world["c"], world["sid"], world["op"], world["sup"]
    assert say(c, sid, "t1", f"Log an incident: {SENTINEL_TEXT} near the pit, high severity")["actions"]
    change(c, op, "g1", "vitals_processing", "grant", 0)
    for i in range(3):
        sample(c, sid, f"s{i}", local("11:01", 30 * i), hr=187.3, skin=36.4, headers=op)
    ov = c.get(f"/v1/supervisor/overview?site_id={SITE}", headers=sup)
    assert ov.json()["incidents"][0]["severity"] == "high"
    assert ov.json()["risks"] == [{"operator_id": OP, "site_id": SITE, "risk_level": "unavailable",
                                   "unavailable_reason": "consent_not_granted", "freshness": "none", "as_of": None}]
    change(c, op, "g2", "risk_sharing_supervisor", "grant", 1)
    shared = c.get(f"/v1/supervisor/overview?site_id={SITE}", headers=sup).json()["risks"][0]
    assert shared["risk_level"] == "high" and shared["freshness"] == "fresh"
    change(c, op, "r1", "risk_sharing_supervisor", "revoke", 2)
    ov = c.get(f"/v1/supervisor/overview?site_id={SITE}", headers=sup)
    assert ov.json()["risks"][0]["unavailable_reason"] == "consent_revoked"
    # replaying the whole feed re-projects every risk event under the CURRENT (revoked) consent
    events, _ = c.app.state.service.supervision.feed_page(SITE, 0)
    risk_events = [e for e in events if e["type"] == "risk.changed"]
    assert risk_events and all(e["data"]["risk_level"] == "unavailable" for e in risk_events)
    everything = ov.text + json.dumps(events) + c.get("/v1/approvals", headers=sup).text
    for private in (SENTINEL_TEXT, "187.3", "36.4", "heart", "skin", "explanation", "speech", "evidence"):
        assert private not in everything, private
    assert SENTINEL_TEXT in c.get(f"/v1/sessions/{sid}/state", headers=op).text  # the operator still sees it


# ------------------------------------------------------------------ approvals


def test_decision_is_recorded_once_applied_once_and_notifies_once(world):
    c, sid, sup = world["c"], world["sid"], world["sup"]
    pending = repeat_review(c, sid)
    listed = c.get("/v1/approvals?status=pending", headers=sup).json()["items"]
    approval = next(a for a in listed if a["approval_id"] == pending["approval_id"])
    assert approval["eligibility"] == "eligible" and approval["application"]["status"] == "not_started"
    assert approval["escalation"]["rule_family"] == "seatbelt_engine_on" and approval["escalation"]["episode_count"] == 3
    assert c.get(f"/v1/approvals/{approval['approval_id']}", headers=world["south"]).status_code == 404
    assert c.get(f"/v1/approvals/{approval['approval_id']}", headers=world["op"]).status_code == 200
    assert decide(c, world["op"], approval, "d0").status_code == 403
    assert decide(c, world["south"], approval, "d0").status_code == 404
    bad = decide(c, sup, approval, "d1", digest="0" * 64)
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "version_conflict"
    assert decide(c, sup, approval, "d1", version=approval["version"] + 1).status_code == 409
    assert c.get("/v1/supervisor/overview?site_id=" + SITE, headers=sup).json()["notifications"] == []
    ok = decide(c, sup, approval, "d1")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["decision_recorded"] and body["approval"]["status"] == "approved"
    assert body["approval"]["application"]["status"] == "applied"
    retry = decide(c, sup, approval, "d1").json()
    assert retry["decision_recorded"] is False and retry["approval"] == body["approval"]
    contra = decide(c, sup, approval, "d2", decision="reject", version=body["approval"]["version"])
    assert contra.status_code == 409 and contra.json()["error"]["code"] == "decision_conflict"
    notes = c.get("/v1/supervisor/overview?site_id=" + SITE, headers=sup).json()["notifications"]
    assert len(notes) == 1 and notes[0]["status"] == "created" and notes[0]["source_id"] == approval["approval_id"]
    nid = notes[0]["notification_id"]
    url = f"/v1/supervisor/notifications/{nid}/receipt"
    assert c.post(url, headers=sup, json={"status": "acknowledged"}).json()["status"] == "acknowledged"
    assert c.post(url, headers=sup, json={"status": "presented"}).status_code == 409
    assert c.post(url, headers=world["south"], json={"status": "presented"}).status_code == 404


def test_rejection_and_expiry_create_nothing(world):
    c, sid, sup = world["c"], world["sid"], world["sup"]
    say(c, sid, "t1", "Log an incident: fence down by the pit, medium severity, and tell my supervisor")
    esc = next(a for a in c.get("/v1/approvals", headers=sup).json()["items"] if a["kind"] == "incident_escalation")
    rejected = decide(c, sup, esc, "d-rej", decision="reject").json()["approval"]
    assert rejected["status"] == "rejected" and rejected["application"]["status"] == "not_applicable"
    pending = repeat_review(c, sid)
    with sqlite3.connect(world["settings"].db_path) as conn:
        conn.execute("UPDATE approval_requests SET expires_at = '2000-01-01T00:00:00+00:00' WHERE approval_id = ?",
                     (pending["approval_id"],))
    view = c.get(f"/v1/approvals/{pending['approval_id']}", headers=sup).json()
    assert view["status"] == "expired"
    late = decide(c, sup, view, "d-late", version=view["version"] - 1)
    assert late.status_code == 409 and late.json()["error"]["code"] == "approval_expired"
    assert c.get("/v1/supervisor/overview?site_id=" + SITE, headers=sup).json()["notifications"] == []


def test_populated_v12_database_upgrades_pending_reviews_with_trusted_scope_only(tmp_path):
    settings, tokens = world_for(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, MACHINE)["session_id"]
        pending = repeat_review(c, sid)
    # rebuild the same rows in a v12 database: one bound to a trusted session, one to a legacy session
    old = tmp_path / "v12.db"
    conn = sqlite3.connect(old, isolation_level=None)
    migrate(conn, MIGRATIONS[:12])
    conn.execute("INSERT INTO sessions(session_id, client_session_key, room_name, participant_identity, operator_id,"
                 " machine_id, created_at) VALUES ('ses_legacy', 'k', 'r', 'p', 'OP_X', 'm', '2026-09-01T00:00:00+00:00')")
    conn.execute("INSERT INTO approval_requests(approval_id, session_id, operator_id, kind, incident_id, created_at)"
                 " VALUES ('APR-legacy', 'ses_legacy', 'OP_X', 'incident_escalation', 'INC-0001',"
                 " '2026-09-01T00:00:01+00:00')")
    migrate(conn)
    row = conn.execute("SELECT site_id, eligibility, status, payload_json FROM approval_requests").fetchone()
    assert row[:3] == (None, "ineligible_no_site", "pending") and json.loads(row[3])["incident_id"] == "INC-0001"
    conn.close()
    # and the live database (v14 already) kept its pending C review eligible at its trusted site
    with TestClient(create_app(settings)) as c:
        view = c.get(f"/v1/approvals/{pending['approval_id']}", headers=tokens["sup"]).json()
        assert view["site_id"] == SITE and view["status"] == "pending"


def test_an_approved_but_unapplied_request_is_applied_after_restart(tmp_path):
    settings, tokens = world_for(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, MACHINE)["session_id"]
        pending = repeat_review(c, sid)
    # simulate a crash after the decision transaction and before the application transaction
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("UPDATE approval_requests SET status = 'approved', decision_id = 'd1', decision = 'approve',"
                     " decision_hash = 'x', decided_by = 'sup-north', decided_at = '2026-09-24T00:00:00+00:00',"
                     " version = version + 1, application_status = 'pending' WHERE approval_id = ?",
                     (pending["approval_id"],))
    with TestClient(create_app(settings)) as c:  # startup recovery, no new input
        view = c.get(f"/v1/approvals/{pending['approval_id']}", headers=tokens["sup"]).json()
        assert view["application"]["status"] == "applied"
        notes = c.get(f"/v1/supervisor/overview?site_id={SITE}", headers=tokens["sup"]).json()["notifications"]
        assert len(notes) == 1
    with TestClient(create_app(settings)) as c:
        assert len(c.get(f"/v1/supervisor/overview?site_id={SITE}",
                         headers=tokens["sup"]).json()["notifications"]) == 1


# ------------------------------------------------------------------ weather re-planning


def use_fixture(target: Path, name: str) -> None:
    shutil.copyfile(DEMO / name, target)
    stamp = time.time_ns() + 1_000_000  # a distinct mtime even on coarse filesystems
    import os
    os.utime(target, ns=(stamp, stamp))


@pytest.fixture
def replan(tmp_path):
    weather = tmp_path / "weather.json"
    use_fixture(weather, "weather_fixture_replan_calm_v1.json")
    settings, tokens = world_for(tmp_path, COCOON_WEATHER_MODE="fixture", COCOON_WEATHER_FIXTURE_PATH=str(weather))
    with TestClient(create_app(settings)) as c:
        session = session_for(c, MACHINE)
        replay(c, session["session_id"], [{"event_id": "clock", "observed_at": local("07:00").isoformat(),
                                           "simulated": True, "readings": {"engine_on": False,
                                                                           "seatbelt_fastened": True,
                                                                           "idle_seconds": 0}}])
        yield {"c": c, "weather": weather, "shift": session["shift_id"], "sid": session["session_id"], **tokens}


def propose(c, shift):
    r = c.post(f"/v1/shifts/{shift}/schedule-proposals", headers=AUTH, json={})
    assert r.status_code == 200, r.text
    return r.json()


def order(c, sid):
    return [(t["title"], t["scheduled_start_local"], t["version"])
            for t in c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["assigned_tasks"]]


def test_reorder_is_proposed_only_when_it_helps_and_applied_once_after_approval(replan):
    c, sup, shift, sid = replan["c"], replan["sup"], replan["shift"], replan["sid"]
    before = order(c, sid)
    calm = propose(c, shift)
    assert calm["status"] == "no_proposal" and "no feasible order" in calm["reason"]
    assert c.post(f"/v1/shifts/{shift}/schedule-proposals", headers=replan["sup"], json={}).status_code == 403
    use_fixture(replan["weather"], "weather_fixture_replan_gusty_v1.json")       # forecast change
    p1 = propose(c, shift)
    assert p1["status"] == "proposed"
    sched = p1["approval"]["schedule"]
    assert sched["before_score"] == [0, 1, 0] and sched["after_score"] == [0, 0, 1]
    moved = {ch["title"]: (ch["before_order"], ch["after_order"]) for ch in sched["changes"]}
    assert moved["Break out the old culvert headwall"] == (3, 2)
    assert sched["forecast"]["provider"] == "fixture" and sched["forecast"]["issued_at"] is None
    assert propose(c, shift)["status"] == "duplicate"                             # unchanged inputs: no churn
    assert order(c, sid) == before                                                # a proposal changes nothing
    rejected = decide(c, sup, p1["approval"], "rej-1", decision="reject").json()["approval"]
    assert rejected["application"]["status"] == "not_applicable" and order(c, sid) == before
    p2 = propose(c, shift)["approval"]
    use_fixture(replan["weather"], "weather_fixture_replan_gusty_v2.json")       # changes after the proposal
    stale = decide(c, sup, p2, "app-2").json()["approval"]
    assert stale["status"] == "approved" and stale["application"]["status"] == "failed_stale_inputs"
    assert "forecast changed" in stale["application"]["reason"] and order(c, sid) == before
    p3 = propose(c, shift)["approval"]
    applied = decide(c, sup, p3, "app-3").json()["approval"]
    assert applied["application"]["status"] == "applied"
    after = order(c, sid)
    assert [t[0] for t in after] == ["Excavate the north pit bench", "Break out the old culvert headwall",
                                     "Open the east drainage trench"]
    assert after[1][1] == "09:15" and after[1][2] == before[2][2] + 1
    assert decide(c, sup, p3, "app-3").json()["decision_recorded"] is False and order(c, sid) == after
    events = c.get(f"/v1/sessions/{sid}/events", headers=AUTH).json()["events"]
    assert [e["type"] for e in events].count("schedule_changed") == 1
    # the conditions gate still applies to the new order
    started = c.post(f"/v1/sessions/{sid}/commands", headers=AUTH, json={
        "command_id": "s1", "kind": "task.start", "payload": {"task_id": applied["schedule"]["changes"][0]["task_id"]}})
    assert started.status_code == 200 and started.json()["task"]["start_check"] is not None


def test_dependencies_leave_no_feasible_alternative(replan):
    c = replan["c"]
    use_fixture(replan["weather"], "weather_fixture_replan_gusty_v1.json")
    bhl = session_for(c, "BHL_DEMO_001")
    out = propose(c, bhl["shift_id"])
    assert out["status"] == "no_proposal" and out["evaluated"]


# ------------------------------------------------------------------ feed over a real socket


class _Server:
    def __init__(self, app):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return f"http://127.0.0.1:{self.port}"
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _read_events(response, count: int, timeout: float = 10.0) -> list[dict]:
    events, current = [], {}
    deadline = time.monotonic() + timeout
    for line in response.iter_lines():
        if line.startswith("id: "):
            current["id"] = int(line[4:])
        elif line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
        elif line.startswith(":"):
            current.setdefault("comments", []).append(line)
        elif line == "" and current:
            events.append(current)
            current = {}
            if sum(1 for e in events if "id" in e) >= count or time.monotonic() > deadline:
                break
    return events


def test_feed_streams_incrementally_replays_from_a_cursor_and_closes_on_revocation(tmp_path):
    settings, tokens = world_for(tmp_path, COCOON_FEED_HEARTBEAT_SECONDS="0.2", COCOON_FEED_POLL_SECONDS="0.05")
    app = create_app(settings)
    with _Server(app) as base, httpx.Client(base_url=base, timeout=15) as http:
        sid = http.post("/v1/sessions", headers=AUTH, json={
            "client_session_key": "lk:feed:op", "room_name": "feed", "participant_identity": "op",
            "operator_id": OP, "machine_id": MACHINE}).json()["session_id"]
        start = http.get(f"/v1/supervisor/overview?site_id={SITE}", headers=tokens["sup"]).json()["feed_cursor"]
        assert http.get(f"/v1/supervisor/events/stream?site_id={SITE}&after=999999",
                        headers=tokens["sup"]).status_code == 422
        assert http.get(f"/v1/supervisor/events/stream?site_id={SITE}",
                        headers=tokens["south"]).status_code == 403
        with http.stream("GET", f"/v1/supervisor/events/stream?site_id={SITE}&after={start}",
                         headers=tokens["sup"]) as live:
            assert live.status_code == 200 and live.headers["content-type"].startswith("text/event-stream")
            # produce a change AFTER the stream is open: it must arrive incrementally over the socket
            for e in hazard_events(MACHINE, "repeat_belt", local("10:00"), "feed"):
                assert http.post(f"/v1/sessions/{sid}/telemetry", headers=AUTH, json=e).status_code == 200
            got = _read_events(live, 3)
        typed = [e for e in got if "id" in e]
        assert {e["event"] for e in typed} >= {"alerts.changed"} and all(e["id"] > start for e in typed)
        assert any(c.startswith(": heartbeat") or c.startswith(": cocoon") for e in got for c in e.get("comments", []))
        # reconnect with Last-Event-ID: replay continues strictly after it with the same event IDs; the data is
        # re-projected at send time (current state, current consent), not a stored copy
        with http.stream("GET", f"/v1/supervisor/events/stream?site_id={SITE}",
                         headers={**tokens["sup"], "Last-Event-ID": str(typed[0]["id"])}) as again:
            replayed = [e for e in _read_events(again, 2) if "id" in e]
        assert [(e["id"], e["event"], e["data"]["event_id"]) for e in replayed[:1]] == \
            [(typed[1]["id"], typed[1]["event"], typed[1]["data"]["event_id"])]
        # a revoked token closes an open stream at its next poll
        with http.stream("GET", f"/v1/supervisor/events/stream?site_id={SITE}", headers=tokens["sup"]) as doomed:
            assert run_admin(settings, "revoke", tokens["sup_token_id"])[0] == 0
            closing = _read_events(doomed, 1, timeout=5)
        assert any(e.get("event") == "stream_closed" and e["data"]["reason"] == "access_revoked" for e in closing)


def test_feed_retention_gives_410_below_the_pruned_cursor(world):
    c, sid, sup = world["c"], world["sid"], world["sup"]
    repeat_review(c, sid)
    service = c.app.state.service
    assert service.supervision.prune_feed(datetime.now(timezone.utc) + timedelta(days=2)) > 0
    r = c.get(f"/v1/supervisor/events/stream?site_id={SITE}&after=0", headers=sup)
    assert r.status_code == 410 and r.json()["error"]["code"] == "replay_expired"
    assert c.get(f"/v1/supervisor/overview?site_id={SITE}", headers=sup).status_code == 200
