"""Batch D4: versioned snapshots, presence ordering and liveness, offline draft upload through the shared command
service (cross-session identity, original binding, captured time), stale task commands, lost-response lookup,
presentation receipts separate from audio, and announcement expiry."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.service import payload_hash

from .conftest import AUTH, seeded_demo
from .test_auth import bearer, issue
from .test_tasks import session_for
from .test_wellbeing import change

OP, MACHINE = "OP_DEMO_1_1", "EXC_DEMO_001"


@pytest.fixture
def world(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    op, _ = issue(settings, tmp_path, "operator", "--operator-id", OP, name="op")
    other, _ = issue(settings, tmp_path, "operator", "--operator-id", "OP_DEMO_2_1", name="other")
    with TestClient(create_app(settings)) as c:
        first = session_for(c, MACHINE, key="lk:phone-1:op")
        yield {"c": c, "settings": settings, "op": bearer(op), "other": bearer(other), "first": first}


def binding(session: dict) -> dict:
    return {k: session[k] for k in ("session_id", "operator_id", "machine_id", "site_id", "shift_id")}


def upload(c, sid, headers, command_id, draft_id, original, captured, description="Loose guard rail by the pit",
           **payload):
    return c.post(f"/v1/sessions/{sid}/commands", headers=headers, json={
        "command_id": command_id, "kind": "incident.submit_draft", "client_draft_id": draft_id,
        "captured_at": captured.isoformat(), "original_binding": original,
        "payload": {"description": description, **payload}})


def drafts(settings) -> list[tuple]:
    with sqlite3.connect(settings.db_path) as conn:
        return conn.execute("SELECT draft_id, session_id, machine_id, client_draft_id, capture_mode FROM"
                            " incident_drafts WHERE capture_mode = 'offline_sync'").fetchall()


def test_snapshot_separates_versions_data_age_and_channel_state(world):
    c, sid = world["c"], world["first"]["session_id"]
    snap = c.get(f"/v1/sessions/{sid}/state", headers=world["op"]).json()["snapshot"]
    assert snap["machine_status"] == "unavailable" and snap["voice_available"] is False
    assert snap["schedule_version"] == 1 and len(snap["task_versions"]) == 3 and snap["last_contact_at"] is None
    now = datetime.now(timezone.utc)
    report = {"report_id": "r2", "consumer_id": "phone-1", "sequence": 2, "connection": "online",
              "voice_available": True, "screen_available": True,
              "reported_at": (now + timedelta(days=1)).isoformat(), "ttl_seconds": 5}   # a future client clock
    first = c.post(f"/v1/sessions/{sid}/presence", headers=world["op"], json=report).json()
    received = datetime.fromisoformat(first["presence"]["received_at"])
    assert datetime.fromisoformat(first["presence"]["expires_at"]) == received + timedelta(seconds=5)
    older = c.post(f"/v1/sessions/{sid}/presence", headers=world["op"], json={
        **report, "report_id": "r1", "sequence": 1, "connection": "offline"}).json()
    assert older["applied"] is False and older["ignored_reason"] == "older_sequence"
    assert older["presence"]["connection"] == "online"
    again = c.post(f"/v1/sessions/{sid}/presence", headers=world["op"], json=report).json()
    assert again["duplicate"] is True
    assert c.post(f"/v1/sessions/{sid}/presence", headers=world["op"],
                  json={**report, "sequence": 3}).status_code == 409                    # same report_id, new body
    # a second consumer going offline never erases the first one
    c.post(f"/v1/sessions/{sid}/presence", headers=AUTH, json={
        **report, "report_id": "v1", "consumer_id": "voice-worker", "sequence": 1, "connection": "offline",
        "voice_available": False, "screen_available": False})
    state = c.get(f"/v1/sessions/{sid}/state", headers=world["op"]).json()
    assert {p["consumer_id"]: p["connection"] for p in state["presence"]} == \
        {"phone-1": "online", "voice-worker": "offline"}
    assert state["snapshot"]["voice_available"] is True


def test_offline_draft_uploads_once_across_retries_and_replacement_sessions(world):
    c, s1, settings = world["c"], world["first"], world["settings"]
    captured = datetime.now(timezone.utc) - timedelta(minutes=40)       # captured while disconnected
    original = binding(s1)
    r = upload(c, s1["session_id"], world["op"], "cmd-1", "local-7", original, captured,
               occurred_expression="ten minutes ago")
    assert r.status_code == 200, r.text
    body = r.json()
    draft = body["draft"]
    assert body["duplicate"] is False and body["duplicate_draft"] is False and draft["status"] == "draft"
    assert draft["capture_mode"] == "offline_sync" and draft["client_draft_id"] == "local-7"
    assert datetime.fromisoformat(draft["occurred_at"]) == captured - timedelta(minutes=10)  # not reconnect time
    assert draft["missing"] == ["severity"]                              # upload is not confirmation
    # the same request again (lost response) and a status lookup never create anything
    assert upload(c, s1["session_id"], world["op"], "cmd-1", "local-7", original, captured,
                  occurred_expression="ten minutes ago").json()["duplicate"] is True
    looked = c.get(f"/v1/sessions/{s1['session_id']}/commands/cmd-1", headers=world["op"]).json()
    assert looked["record_id"] == draft["draft_id"] and looked["duplicate"] is True
    # a replacement session on ANOTHER machine (the operator's current selection) uploads the same local draft
    s2 = session_for(c, MACHINE, key="lk:phone-2:op")
    other_machine = c.post("/v1/sessions", headers=AUTH, json={
        "client_session_key": "lk:phone-3:op", "room_name": "r3", "participant_identity": "op", "operator_id": OP,
        "machine_id": "DOZ_DEMO_001"}).json()
    for sid, cid in ((s2["session_id"], "cmd-1"), (other_machine["session_id"], "cmd-2")):
        again = upload(c, sid, world["op"], cid, "local-7", original, captured, occurred_expression="ten minutes ago")
        assert again.status_code == 200, again.text
        assert again.json()["record_id"] == draft["draft_id"]
        assert again.json()["record_session_id"] in (None, s1["session_id"])
    assert c.get(f"/v1/sessions/{other_machine['session_id']}/commands/cmd-1", headers=world["op"]).status_code == 200
    stored = drafts(settings)
    assert stored == [(draft["draft_id"], s1["session_id"], MACHINE, "local-7", "offline_sync")]  # one, original machine
    # changed content or binding under the same draft ID is refused; the device keeps its local copy
    changed = upload(c, s1["session_id"], world["op"], "cmd-3", "local-7", original, captured,
                     description="Something else")
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "idempotency_conflict"
    moved = upload(c, s1["session_id"], world["op"], "cmd-4", "local-7", binding(s2), captured,
                   occurred_expression="ten minutes ago")
    assert moved.status_code == 409 and moved.json()["error"]["code"] == "binding_mismatch"
    forged = upload(c, s1["session_id"], world["op"], "cmd-5", "local-8", {**original, "machine_id": "DOZ_DEMO_001"},
                    captured)
    assert forged.status_code == 409 and forged.json()["error"]["code"] == "binding_mismatch"
    b_sid = session_for(c, "DOZ_DEMO_001")["session_id"]
    foreign = upload(c, b_sid, world["other"], "cmd-6", "local-9", {**original, "operator_id": "OP_DEMO_2_1"},
                     captured)
    assert foreign.status_code == 404
    assert len(drafts(settings)) == 1


def test_delayed_task_commands_need_current_versions_and_cannot_target_another_session(world):
    c, s1 = world["c"], world["first"]
    task = c.get(f"/v1/sessions/{s1['session_id']}/state", headers=world["op"]).json()["assigned_tasks"][0]
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    stale = c.post(f"/v1/sessions/{s1['session_id']}/commands", headers=world["op"], json={
        "command_id": "t1", "kind": "task.start", "captured_at": old, "expected_version": task["version"],
        "payload": {"task_id": task["task_id"], "acknowledge_conditions": True}})
    assert stale.status_code == 409 and "captured too long ago" in stale.json()["error"]["message"]
    wrong = c.post(f"/v1/sessions/{s1['session_id']}/commands", headers=world["op"], json={
        "command_id": "t2", "kind": "task.start", "expected_version": task["version"] + 5,
        "payload": {"task_id": task["task_id"]}})
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "version_conflict"
    s2 = session_for(c, MACHINE, key="lk:phone-2:op")
    cross = c.post(f"/v1/sessions/{s2['session_id']}/commands", headers=world["op"], json={
        "command_id": "t3", "kind": "task.start", "original_binding": binding(s1),
        "payload": {"task_id": task["task_id"]}})
    assert cross.status_code == 409 and cross.json()["error"]["code"] == "binding_mismatch"
    now = datetime.now(timezone.utc).isoformat()
    fresh = c.post(f"/v1/sessions/{s1['session_id']}/commands", headers=world["op"], json={
        "command_id": "t4", "kind": "task.start", "captured_at": now, "expected_version": task["version"],
        "payload": {"task_id": task["task_id"]}})
    assert fresh.status_code == 200 and fresh.json()["task"]["status"] == "in_progress"


def test_an_offline_consent_retry_cannot_undo_a_later_revocation(world):
    c, op = world["c"], world["op"]
    change(c, op, "g1", "vitals_processing", "grant", 0)
    change(c, op, "r1", "vitals_processing", "revoke", 1)
    queued = change(c, op, "g-offline", "vitals_processing", "grant", 1)          # captured before the revocation
    assert queued.status_code == 409 and queued.json()["error"]["code"] == "version_conflict"
    assert change(c, op, "g1", "vitals_processing", "grant", 0).json()["applied"] is False


def test_screen_presentation_is_not_audio_and_expired_events_are_marked(world):
    c, sid, settings = world["c"], world["first"]["session_id"], world["settings"]
    event = c.get(f"/v1/sessions/{sid}/events", headers=world["op"]).json()["events"][0]   # the shift briefing
    body = {"presentation_id": "pres-1", "consumer_id": "phone-1", "channel": "screen", "status": "presented",
            "presented_at": datetime.now(timezone.utc).isoformat()}
    assert c.post(f"/v1/sessions/{sid}/events/{event['event_id']}/presentation", headers=world["op"],
                  json=body).status_code == 200
    assert c.post(f"/v1/sessions/{sid}/events/{event['event_id']}/presentation", headers=world["op"],
                  json=body).status_code == 200
    assert c.post(f"/v1/sessions/{sid}/events/missing/presentation", headers=world["op"],
                  json=body).status_code == 404
    assert c.post(f"/v1/sessions/{sid}/events/{event['event_id']}/presentation", headers=world["other"],
                  json=body).status_code == 404
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("UPDATE announcements SET expires_at = '2000-01-01T00:00:00+00:00' WHERE event_id = ?",
                     (event["event_id"],))
    listed = c.get(f"/v1/sessions/{sid}/events", headers=world["op"]).json()["events"][0]
    assert listed["deliveries"] == [] and [p["channel"] for p in listed["presentations"]] == ["screen"]
    assert listed["expired"] is True


def test_command_digests_of_earlier_batches_are_unchanged(world):
    """A B/C-era command keeps its idempotency digest: new optional fields are left out while unset."""
    c, sid = world["c"], world["first"]["session_id"]
    task = c.get(f"/v1/sessions/{sid}/state", headers=world["op"]).json()["assigned_tasks"][0]
    r = c.post(f"/v1/sessions/{sid}/commands", headers=world["op"], json={
        "command_id": "legacy-shape", "kind": "task.start", "payload": {"task_id": task["task_id"]}})
    assert r.status_code == 200
    c_era = {"task_id": task["task_id"], "incident_id": None, "description": None, "severity": None,
             "location_text": None, "lesson_id": None, "attempt_id": None, "question_id": None, "choice_id": None,
             "expected_step": None, "defer_minutes": None}
    expected = payload_hash({"session_id": sid, "kind": "task.start", "payload": c_era, "expected_version": None})
    with sqlite3.connect(world["settings"].db_path) as conn:
        stored = conn.execute("SELECT fingerprint FROM command_log WHERE command_id = 'legacy-shape'").fetchone()[0]
    assert stored == expected
