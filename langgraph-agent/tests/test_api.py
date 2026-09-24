"""HTTP behaviour of the v1 contract in mock LLM mode (no credentials needed)."""

from __future__ import annotations

from .conftest import AUTH, new_session, telemetry, turn


def _assert_envelope(r, status: int, code: str, retryable: bool = False):
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert err["code"] == code
    assert err["retryable"] is retryable
    assert err["request_id"] == r.headers["X-Request-ID"]
    return err


# ---------------------------------------------------------------------- errors / auth


def test_health_and_ready_are_public(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz").json()
    assert ready["status"] == "ready" and ready["llm_mode"] == "mock"


def test_auth_required(client):
    sid = new_session(client)
    _assert_envelope(client.get(f"/v1/sessions/{sid}/state"), 401, "unauthorized")
    _assert_envelope(client.get(f"/v1/sessions/{sid}/state", headers={"Authorization": "Bearer nope"}), 401,
                     "unauthorized")


def test_malformed_requests_use_error_envelope(client):
    sid = new_session(client)
    r = client.post(f"/v1/sessions/{sid}/turns", json={"turn_id": "", "text": "  ", "source": "radio"}, headers=AUTH)
    err = _assert_envelope(r, 422, "validation_error")
    fields = {d["field"] for d in err["details"]}
    assert {"body.turn_id", "body.text", "body.source"} <= fields
    r = client.post(f"/v1/sessions/{sid}/turns", json={"turn_id": "a", "text": "hi", "source": "voice", "x": 1},
                    headers=AUTH)
    _assert_envelope(r, 422, "validation_error")
    r = client.post(f"/v1/sessions/{sid}/telemetry", headers=AUTH, json={
        "event_id": "e1", "observed_at": "2026-09-23T10:00:00", "simulated": True,
        "readings": {"engine_on": True, "seatbelt_fastened": False, "idle_seconds": 0}})
    _assert_envelope(r, 422, "validation_error")  # naive timestamp
    r = client.post(f"/v1/sessions/{sid}/telemetry", headers=AUTH, json={
        "event_id": "e1", "observed_at": "2026-09-23T10:00:00Z", "simulated": False,
        "readings": {"engine_on": True, "seatbelt_fastened": False, "idle_seconds": 0}})
    _assert_envelope(r, 422, "validation_error")  # only simulated data accepted


def test_unknown_session_and_turn_are_404(client):
    _assert_envelope(turn(client, "ses_missing", "t1", "hello"), 404, "not_found")
    sid = new_session(client)
    _assert_envelope(client.get(f"/v1/sessions/{sid}/turns/nope", headers=AUTH), 404, "not_found")


def test_request_id_is_echoed(client):
    r = client.get("/healthz", headers={"X-Request-ID": "voice-turn-42"})
    assert r.headers["X-Request-ID"] == "voice-turn-42"


# ---------------------------------------------------------------------- sessions


def test_session_creation_is_idempotent_by_key(client):
    body = {"client_session_key": "lk:r:p", "room_name": "r", "participant_identity": "p",
            "operator_id": "OP_TEST_1", "machine_id": "EXC_DEMO_001"}
    first = client.post("/v1/sessions", json=body, headers=AUTH)
    second = client.post("/v1/sessions", json=body, headers=AUTH)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["session_id"] == second.json()["session_id"]
    conflict = client.post("/v1/sessions", json={**body, "operator_id": "someone-else"}, headers=AUTH)
    _assert_envelope(conflict, 409, "session_conflict")


# ---------------------------------------------------------------------- flows


def test_next_task_comes_from_seeded_persistence(client):
    sid = new_session(client)
    r = turn(client, sid, "t1", "What's my next task?")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed" and body["llm_mode"] == "mock"
    assert body["actions"][0]["task"]["task_id"] == "T-101"
    assert "Pre-start walkaround" in body["speech"]


def test_incident_with_follow_up_then_visible_in_state(client):
    sid = new_session(client)
    ask = turn(client, sid, "t1", "I need to report an incident").json()
    assert ask["actions"] == [{"type": "information_requested", "for_action": "log_incident",
                               "missing_field": "description", "draft": None, "reason": None}]
    state = client.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
    assert state["pending_question"]["kind"] == "incident_description"
    assert state["incidents"] == []

    done = turn(client, sid, "t2", "The left track tension is loose, low severity").json()
    incident = done["actions"][0]["incident"]
    assert incident["description"] == "The left track tension is loose"
    assert incident["incident_id"] in done["speech"] or str(incident["incident_number"]) in done["speech"]

    state = client.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
    assert [i["incident_id"] for i in state["incidents"]] == [incident["incident_id"]]
    assert state["pending_question"] is None
    assert state["state_version"] == done["state_version"]


def test_training_assign_and_status(client):
    sid = new_session(client)
    a = turn(client, sid, "t1", "Assign me the hydraulic leak lesson").json()["actions"][0]
    assert a["type"] == "training_assigned" and a["assignment"]["lesson_id"] == "L3" and a["created"] is True
    again = turn(client, sid, "t2", "Assign me the hydraulic leak lesson").json()["actions"][0]
    assert again["created"] is False  # different turn, same lesson: no duplicate assignment
    status = turn(client, sid, "t3", "What training do I have?").json()["actions"][0]
    assert status["type"] == "training_status"
    assert [x["lesson_id"] for x in status["assignments"]] == ["L3"]
    assert len(status["available_lessons"]) == 8  # C4 curriculum: L1-L7 and the practice scenario S1


def test_retrying_same_turn_does_not_repeat_actions(client):
    sid = new_session(client)
    first = turn(client, sid, "t-retry", "Log an incident: cracked mirror on the cab door, low severity")
    second = turn(client, sid, "t-retry", "Log an incident: cracked mirror on the cab door, low severity")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    state = client.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
    assert len(state["incidents"]) == 1
    conflict = turn(client, sid, "t-retry", "Log an incident: something different")
    _assert_envelope(conflict, 409, "idempotency_conflict")
    polled = client.get(f"/v1/sessions/{sid}/turns/t-retry", headers=AUTH).json()
    assert polled == first.json()


def test_follow_up_stays_in_its_session(client):
    a = new_session(client, "lk:room-a:op-1")
    b = new_session(client, "lk:room-b:op-2", participant_identity="op-2", operator_id="OP_TEST_2",
                    machine_id="DOZ_DEMO_001")
    turn(client, a, "a1", "Report an incident")
    # Session B says something that would answer A's pending question; it must not.
    rb = turn(client, b, "b1", "The hose burst near the bucket").json()
    assert rb["actions"] == []
    state_b = client.get(f"/v1/sessions/{b}/state", headers=AUTH).json()
    assert state_b["incidents"] == [] and state_b["pending_question"] is None
    ra = turn(client, a, "a2", "Coolant is leaking under the engine, high severity").json()
    assert ra["actions"][0]["type"] == "incident_logged"
    assert ra["actions"][0]["incident"]["session_id"] == a
    # identical turn ids in different sessions are independent
    assert turn(client, b, "a2", "What's my next task?").json()["actions"][0]["type"] == "next_task"


def test_cancel_pending_question(client):
    sid = new_session(client)
    turn(client, sid, "t1", "report an incident")
    r = turn(client, sid, "t2", "never mind").json()
    assert r["actions"] == [{"type": "pending_cancelled", "cancelled": "log_incident", "kept_draft_number": None}]
    assert client.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["pending_question"] is None


# ---------------------------------------------------------------------- telemetry + announcements


def test_alert_episode_announces_once_and_resets(client):
    sid = new_session(client)
    assert telemetry(client, sid, "s1", "2026-09-23T10:00:00Z", engine_on=False, seatbelt=True).json()[
        "alerts_opened"] == []
    opened = telemetry(client, sid, "s2", "2026-09-23T10:00:01Z", engine_on=True, seatbelt=False).json()
    assert len(opened["alerts_opened"]) == 1 and len(opened["announcements_created"]) == 1
    for i in range(3, 6):  # repeated samples of the same active condition
        again = telemetry(client, sid, f"s{i}", f"2026-09-23T10:00:0{i}Z", engine_on=True, seatbelt=False).json()
        assert again["alerts_opened"] == [] and again["announcements_created"] == []
    events = client.get(f"/v1/sessions/{sid}/events", headers=AUTH).json()["events"]
    assert [e["type"] for e in events] == ["alert_started"]

    cleared = telemetry(client, sid, "s6", "2026-09-23T10:00:06Z", engine_on=True, seatbelt=True).json()
    assert cleared["alerts_cleared"] == opened["alerts_opened"] and cleared["active_alerts"] == []
    reopened = telemetry(client, sid, "s7", "2026-09-23T10:00:07Z", engine_on=True, seatbelt=False).json()
    assert len(reopened["alerts_opened"]) == 1 and reopened["alerts_opened"] != opened["alerts_opened"]
    events = client.get(f"/v1/sessions/{sid}/events", headers=AUTH).json()["events"]
    assert [e["type"] for e in events] == ["alert_started", "alert_cleared", "alert_started"]
    assert [e["sequence"] for e in events] == [1, 2, 3]
    assert all(e["expires_at"] for e in events)


def test_telemetry_duplicates_conflicts_and_stale_samples(client):
    sid = new_session(client)
    first = telemetry(client, sid, "dup", "2026-09-23T10:00:05Z", engine_on=True, seatbelt=False).json()
    replay = telemetry(client, sid, "dup", "2026-09-23T10:00:05Z", engine_on=True, seatbelt=False).json()
    assert replay["duplicate"] is True and replay["alerts_opened"] == first["alerts_opened"]
    _assert_envelope(telemetry(client, sid, "dup", "2026-09-23T10:00:05Z", engine_on=True, seatbelt=True), 409,
                     "idempotency_conflict")
    stale = telemetry(client, sid, "old", "2026-09-23T09:59:00Z", engine_on=True, seatbelt=True).json()
    assert stale["stale"] is True and stale["alerts_cleared"] == []
    assert len(stale["active_alerts"]) == 1


def test_why_after_announcement_uses_stored_alert(client):
    sid = new_session(client)
    none = turn(client, sid, "t0", "Why did you warn me?").json()
    assert none["actions"][0]["alert"] is None
    opened = telemetry(client, sid, "s1", "2026-09-23T10:00:01Z", engine_on=True, seatbelt=False).json()
    why = turn(client, sid, "t1", "Why?").json()
    assert why["actions"][0]["alert"]["alert_id"] == opened["alerts_opened"][0]
    assert "seatbelt" in why["speech"] and "simulated" in why["speech"]
    # still explainable after the alert clears
    telemetry(client, sid, "s2", "2026-09-23T10:00:02Z", engine_on=True, seatbelt=True)
    later = turn(client, sid, "t2", "why did you warn me").json()["actions"][0]["alert"]
    assert later["alert_id"] == opened["alerts_opened"][0] and later["status"] == "cleared"


def test_event_cursor_and_delivery_reports(client):
    sid = new_session(client)
    telemetry(client, sid, "s1", "2026-09-23T10:00:01Z", engine_on=True, seatbelt=False)
    telemetry(client, sid, "s2", "2026-09-23T10:00:02Z", engine_on=True, seatbelt=True)
    page = client.get(f"/v1/sessions/{sid}/events?after=0&limit=1", headers=AUTH).json()
    assert [e["sequence"] for e in page["events"]] == [1] and page["has_more"] is True and page["next_cursor"] == 1
    page2 = client.get(f"/v1/sessions/{sid}/events?after=1", headers=AUTH).json()
    assert [e["sequence"] for e in page2["events"]] == [2] and page2["has_more"] is False
    empty = client.get(f"/v1/sessions/{sid}/events?after=2", headers=AUTH).json()
    assert empty["events"] == [] and empty["next_cursor"] == 2

    ev = page["events"][0]["event_id"]
    url = f"/v1/sessions/{sid}/events/{ev}/delivery"
    rec = client.post(url, json={"consumer_id": "worker-1", "status": "interrupted"}, headers=AUTH).json()
    assert rec["status"] == "interrupted"
    client.post(url, json={"consumer_id": "worker-1", "status": "played"}, headers=AUTH)
    # non-destructive: the event is still listed, now with its latest delivery status
    again = client.get(f"/v1/sessions/{sid}/events?after=0", headers=AUTH).json()["events"][0]
    assert [(d["consumer_id"], d["status"]) for d in again["deliveries"]] == [("worker-1", "played")]
    # played is not acknowledgement: the alert itself is untouched
    assert client.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["latest_alert"]["status"] == "cleared"
    _assert_envelope(client.post(f"/v1/sessions/{sid}/events/nope/delivery",
                                 json={"consumer_id": "w", "status": "played"}, headers=AUTH), 404, "not_found")
    _assert_envelope(client.post(url, json={"consumer_id": "w", "status": "acknowledged"}, headers=AUTH), 422,
                     "validation_error")
