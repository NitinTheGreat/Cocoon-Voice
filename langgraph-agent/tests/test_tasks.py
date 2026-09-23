"""Batch B1: seeded shifts and assigned tasks, one command path for voice and taps, ownership and versions."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.catalog import load_catalog
from cocoon_agent.demo_site import FixtureError, load_fixture, seed
from cocoon_agent.store import Conflict, Store

from .conftest import AUTH, DEMO_PAIRS, seeded_demo
from .test_auth import issue


def session_for(c: TestClient, machine: str, key: str | None = None, **binding) -> dict:
    r = c.post("/v1/sessions", headers=AUTH, json={
        "client_session_key": key or f"lk:room-{machine}:op", "room_name": f"room-{machine}",
        "participant_identity": "op", "operator_id": DEMO_PAIRS[machine], "machine_id": machine, **binding})
    assert r.status_code in (200, 201), r.text
    return r.json()


def say(c: TestClient, sid: str, tid: str, text: str, headers=AUTH) -> dict:
    r = c.post(f"/v1/sessions/{sid}/turns", headers=headers, json={"turn_id": tid, "text": text, "source": "voice"})
    assert r.status_code == 200, r.text
    return r.json()


def command(c: TestClient, sid: str, cid: str, kind: str, task_id: str, expected=None, headers=AUTH):
    body = {"command_id": cid, "kind": kind, "payload": {"task_id": task_id}}
    if expected is not None:
        body["expected_version"] = expected
    return c.post(f"/v1/sessions/{sid}/commands", headers=headers, json=body)


def test_seed_is_idempotent_and_never_overwrites(tmp_path):
    settings, first = seeded_demo(tmp_path, "2026-10-01")
    assert first["inserted"] == {"sites": 1, "site_zones": 5, "shifts": 5, "task_assignments": 10}
    assert first["bindings_added"] == 5
    doc = load_fixture()
    doc["catalog_manifest_sha256"] = settings.dataset_manifest_sha256
    catalog = load_catalog(settings.dataset_root, settings.dataset_manifest_sha256)
    store = Store(settings.db_path)
    again = seed(store, catalog, doc, "2026-10-01", tmp_path / "bindings.json")
    assert again["inserted"] == {"sites": 0, "site_zones": 0, "shifts": 0, "task_assignments": 0}
    assert again["bindings_added"] == 0
    doc["assignments"][0]["tasks"][0]["title"] = "Something else"
    with pytest.raises(Conflict):
        seed(store, catalog, doc, "2026-10-01", tmp_path / "bindings.json")
    doc["catalog_manifest_sha256"] = "0" * 64
    with pytest.raises(FixtureError):
        seed(store, catalog, doc, "2026-10-02", tmp_path / "bindings.json")
    store.close()
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT title FROM task_assignments WHERE task_id = 'TSK-EXC_DEMO_001-2026-10-01-1'"
                        ).fetchone()[0] == "Excavate the north pit bench"
    assert conn.execute("SELECT COUNT(*) FROM shifts WHERE service_date = '2026-10-02'").fetchone()[0] == 0
    conn.close()
    assert len(json.loads((tmp_path / "bindings.json").read_text())["bindings"]) == 5


def test_sessions_bind_to_todays_trusted_shift_and_show_assigned_tasks(tmp_path):
    settings, report = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        auto = session_for(c, "EXC_DEMO_001")
        assert auto["context_status"] == "trusted_binding" and auto["context_source"].endswith(":auto")
        shift_id = report["shift_ids"][0]
        explicit = session_for(c, "EXC_DEMO_001", key="explicit", site_id="SITE_DEMO_NORTH", shift_id=shift_id)
        assert explicit["shift_id"] == shift_id and not explicit["context_source"].endswith(":auto")
        state = c.get(f"/v1/sessions/{auto['session_id']}/state", headers=AUTH).json()
        assert state["shift"]["shift_id"] == shift_id and state["shift"]["source"] == "synthetic_demo_fixture"
        tasks = state["assigned_tasks"]
        assert [t["scheduled_order"] for t in tasks] == [1, 2, 3]
        first = tasks[0]
        assert (first["machine_id"], first["site_zone_id"], first["status"], first["version"]) == (
            "EXC_DEMO_001", "ZONE_N_PIT", "scheduled", 1)
        assert first["scheduled_start_local"] == "07:30"
        assert first["weather"]["source"] == "synthetic_demo_fixture"
        assert first["duration"] == {"minutes": 70, "source": "demo_supplied_estimate"}
        assert len(state["tasks"]) == 3  # the legacy shared demo list is unchanged, not mutated


def test_voice_task_lifecycle_and_same_turn_retry(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        nxt = say(c, sid, "t1", "what's my next task")
        assert nxt["speech"] == "Your next task is Excavate the north pit bench in North pit, scheduled for 07:30."
        started = say(c, sid, "t2", "start the next task")
        action = started["actions"][0]
        assert action["type"] == "task_started" and action["task"]["version"] == 2 and action["created"] is True
        assert say(c, sid, "t2", "start the next task") == started  # stored result, no second transition
        assert say(c, sid, "t3", "what's my next task")["speech"].startswith("You're on Excavate the north pit bench")
        done = say(c, sid, "t4", "I finished the task")["actions"][0]
        assert done["type"] == "task_completed" and done["task"]["status"] == "completed"
        again = say(c, sid, "t5", "I finished the task")["actions"][0]
        assert again == {"type": "task_rejected", "for_action": "task.complete", "reason": "no_eligible_task",
                         "current_status": None, "conditions": None, "task_id": None, "task_title": None}
        conn = sqlite3.connect(settings.db_path)
        assert conn.execute("SELECT COUNT(*) FROM command_log WHERE session_id = ?", (sid,)).fetchone()[0] == 2
        conn.close()


def test_tap_commands_share_the_same_rules_as_voice(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        t1, t2 = (f"TSK-EXC_DEMO_001-{settings_date(c, sid)}-{n}" for n in (1, 2))
        say(c, sid, "v1", "start the next task")  # voice moves task 1 to version 2
        stale = command(c, sid, "tap-1", "task.start", t1, expected=1)
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
        illegal = command(c, sid, "tap-2", "task.complete", t2)
        assert illegal.status_code == 409 and illegal.json()["error"]["code"] == "invalid_transition"
        ok = command(c, sid, "tap-3", "task.complete", t1, expected=2)
        assert ok.status_code == 200 and ok.json()["task"]["status"] == "completed" and ok.json()["duplicate"] is False
        retry = command(c, sid, "tap-3", "task.complete", t1, expected=2)
        assert retry.json() == {**ok.json(), "duplicate": True}
        reuse = command(c, sid, "tap-3", "task.start", t2)
        assert reuse.status_code == 409 and reuse.json()["error"]["code"] == "idempotency_conflict"
        looked_up = c.get(f"/v1/sessions/{sid}/commands/tap-3", headers=AUTH)
        assert looked_up.status_code == 200 and looked_up.json()["record_id"] == t1
        assert c.get(f"/v1/sessions/{sid}/commands/nope", headers=AUTH).status_code == 404


def settings_date(c: TestClient, sid: str) -> str:
    return c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["shift"]["service_date"]


def test_operator_and_asset_isolation(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    exc_token, _ = issue(settings, tmp_path, "operator", "--operator-id", "OP_DEMO_1_1", name="exc")
    doz_token, _ = issue(settings, tmp_path, "operator", "--operator-id", "OP_DEMO_2_1", name="doz")
    sup_token, _ = issue(settings, tmp_path, "supervisor", "--principal-id", "sup-1", name="sup")
    with TestClient(create_app(settings)) as c:
        sessions = {m: session_for(c, m) for m in DEMO_PAIRS}
        for machine, sess in sessions.items():
            tasks = c.get(f"/v1/sessions/{sess['session_id']}/state", headers=AUTH).json()["assigned_tasks"]
            assert tasks and {t["machine_id"] for t in tasks} == {machine}
        exc, doz = sessions["EXC_DEMO_001"]["session_id"], sessions["DOZ_DEMO_001"]["session_id"]
        date = settings_date(c, exc)
        exc_hdr = {"Authorization": f"Bearer {exc_token}"}
        own = command(c, exc, "op-1", "task.start", f"TSK-EXC_DEMO_001-{date}-1", headers=exc_hdr)
        assert own.status_code == 200
        # another operator's task through my own session: not found in my shift; their session: not mine
        assert command(c, exc, "op-2", "task.start", f"TSK-DOZ_DEMO_001-{date}-1", headers=exc_hdr).status_code == 404
        assert command(c, doz, "op-3", "task.start", f"TSK-DOZ_DEMO_001-{date}-1", headers=exc_hdr).status_code == 404
        doz_state = c.get(f"/v1/sessions/{doz}/state", headers={"Authorization": f"Bearer {doz_token}"}).json()
        assert [t["status"] for t in doz_state["assigned_tasks"]] == ["scheduled", "scheduled"]
        sup = command(c, exc, "sup-1", "task.start", f"TSK-EXC_DEMO_001-{date}-2",
                      headers={"Authorization": f"Bearer {sup_token}"})
        assert sup.status_code == 403


def test_unbound_sessions_keep_the_legacy_demo_queue(tmp_path):
    settings, _ = seeded_demo(tmp_path, "2020-01-01")  # a shift that is not today: no automatic binding
    with TestClient(create_app(settings)) as c:
        sess = session_for(c, "EXC_DEMO_001")
        assert sess["context_status"] == "unavailable" and sess["shift_id"] is None
        assert say(c, sess["session_id"], "u1", "what's my next task")["actions"][0]["type"] == "next_task"
        listed = say(c, sess["session_id"], "u2", "what are my tasks")
        assert listed["actions"][0] == {"type": "assigned_tasks", "scope": "all", "tasks": [], "shift_bound": False}
        assert c.get(f"/v1/sessions/{sess['session_id']}/state", headers=AUTH).json()["assigned_tasks"] == []


def test_populated_v3_database_upgrades_without_changes(tmp_path):
    from cocoon_agent.migrations import MIGRATIONS, migrate

    from .conftest import make_settings
    settings = make_settings(tmp_path)
    conn = sqlite3.connect(settings.db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn, MIGRATIONS[:3])
    conn.close()
    from datetime import timedelta

    from cocoon_agent.auth import fmt_time, generate_token, token_digest, utcnow
    store = Store(settings.db_path)  # no init_schema: the database stays at v3 until the explicit upgrade below
    token = generate_token()
    now = utcnow()
    token_id = store.issue_actor_token(
        kind="supervisor", principal_id="sup-v3", operator_id=None, operator_catalog_sha256=None, display_name=None,
        token_sha256=token_digest(token), scopes=("me:read",), issued_at=fmt_time(now),
        expires_at=fmt_time(now + timedelta(hours=1)))["token_id"]
    before = sqlite3.connect(settings.db_path).execute("SELECT * FROM actor_tokens").fetchall()
    assert store.init_schema() == [f"applied:{m.version}" for m in MIGRATIONS[3:]]
    store.close()
    assert sqlite3.connect(settings.db_path).execute("SELECT * FROM actor_tokens").fetchall() == before
    with TestClient(create_app(settings)) as c:
        assert c.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()["token_id"] == token_id
