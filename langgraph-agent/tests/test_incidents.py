"""Batch B2: branch routing, structured incidents, pending escalation, drafts and truthful action outcomes."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.store import Store

from .conftest import AUTH, seeded_demo
from .test_tasks import say, session_for


def incident_count(settings) -> int:
    conn = sqlite3.connect(settings.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    finally:
        conn.close()


def make_draft(settings, session_id: str, episode_id: str, description: str) -> str:
    store = Store(settings.db_path)
    try:
        session = store.get_session(session_id)
        with store._tx() as c:
            return Store.insert_auto_draft(c, session, episode_id, description, "high",
                                           datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc))
    finally:
        store.close()


def incident_command(c, sid, cid, kind, incident_id, expected=None, **payload):
    body = {"command_id": cid, "kind": kind, "payload": {"incident_id": incident_id, **payload}}
    if expected is not None:
        body["expected_version"] = expected
    return c.post(f"/v1/sessions/{sid}/commands", headers=AUTH, json=body)


def test_incident_with_supervisor_request_is_structured_pending_and_recorded_once(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        say(c, sid, "t1", "start the next task")
        text = "Log an incident: hydraulic hose leaking near the stockpile yard, high severity, and tell my supervisor"
        out = say(c, sid, "t2", text)
        assert out["branch"] == "safety_incidents"
        assert [a["type"] for a in out["actions"]] == ["incident_logged", "escalation_requested"]
        inc = out["actions"][0]["incident"]
        assert inc["description"] == "hydraulic hose leaking near the stockpile yard"
        assert (inc["operator_id"], inc["machine_id"], inc["status"], inc["origin"]) == (
            "OP_DEMO_1_1", "EXC_DEMO_001", "confirmed", "operator_reported")
        assert (inc["severity"], inc["severity_basis"]) == ("high", "reported")
        assert (inc["site_id"], inc["site_zone_id"], inc["zone_basis"]) == ("SITE_DEMO_NORTH", "ZONE_N_STOCK", "reported")
        assert inc["occurred_basis"] == "time_of_report" and inc["occurred_at"]
        approval = out["actions"][1]["approval"]
        assert (approval["status"], approval["incident_id"]) == ("pending", inc["incident_id"])
        assert "pending" in out["speech"] and "can't message your supervisor" in out["speech"]
        records = out["action_records"]
        assert [(r["kind"], r["outcome"], r["record_id"]) for r in records] == [
            ("incident.log", "completed", inc["incident_id"]),
            ("escalation.request", "completed", approval["approval_id"])]

        assert say(c, sid, "t2", text) == out  # same turn: stored result, nothing re-run
        assert incident_count(settings) == 1
        # a new intentional report with the same words is a new report, not silently merged
        again = say(c, sid, "t3", text)
        assert again["actions"][0]["incident"]["incident_number"] == inc["incident_number"] + 1

        # no stated place: zone from the task in progress (labelled); severity explicitly unknown, never guessed
        plain = say(c, sid, "t4", "report an incident: the rear camera stopped working, severity unknown")
        plain = plain["actions"][0]["incident"]
        assert (plain["site_zone_id"], plain["zone_basis"]) == ("ZONE_N_PIT", "active_task")
        assert plain["severity"] is None and plain["severity_basis"] == "stated_unknown"

        state = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
        assert len(state["incidents"]) == 3 and state["incident_drafts"] == []
        assert [a["status"] for a in state["pending_approvals"]] == ["pending", "pending"]


def test_missing_description_is_asked_for_and_escalation_carries_over(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        ask = say(c, sid, "t1", "log this incident and tell my supervisor")
        assert [a["type"] for a in ask["actions"]] == ["information_requested"] and ask["action_records"] == []
        assert c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["pending_question"]["notify_supervisor"]
        # a bare "yes" is not a description: nothing is written and the question stays open
        yes = say(c, sid, "t2", "yes")
        assert [a["type"] for a in yes["actions"]] == ["information_requested"] and yes["action_records"] == []
        done = say(c, sid, "t3", "a rock fell off the bench onto the haul road, high severity")
        assert [a["type"] for a in done["actions"]] == ["incident_logged", "escalation_requested"]
        assert done["actions"][0]["incident"]["site_zone_id"] == "ZONE_N_HAUL"
        assert c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["pending_question"] is None


def test_failure_after_a_committed_action_keeps_it_and_retry_does_not_repeat_it(tmp_path, monkeypatch):
    settings, _ = seeded_demo(tmp_path)
    original = Store.escalation_request

    def broken(session, incident_id):
        def mutate(c):
            raise RuntimeError("simulated crash while saving the escalation")
        return mutate

    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        text = "log an incident: fuel smell near the north pit, medium severity, and notify my supervisor"
        monkeypatch.setattr(Store, "escalation_request", staticmethod(broken))
        r = c.post(f"/v1/sessions/{sid}/turns", headers=AUTH, json={"turn_id": "t1", "text": text, "source": "voice"})
        assert r.status_code == 500
        err = r.json()["error"]
        assert err["retryable"] and "1 action(s) were saved" in err["message"]
        assert err["details"] == [{"field": "action_records", "issue": "incident.log completed: INC-0001"}]
        failed = c.get(f"/v1/sessions/{sid}/turns/t1", headers=AUTH).json()
        assert failed["status"] == "failed"
        assert [(x["kind"], x["record_id"]) for x in failed["action_records"]] == [("incident.log", "INC-0001")]
        assert incident_count(settings) == 1

        monkeypatch.setattr(Store, "escalation_request", staticmethod(original))
        out = say(c, sid, "t1", text)  # retry of the same turn finishes only what is missing
        assert out["actions"][0]["incident"]["incident_id"] == "INC-0001" and out["actions"][0]["created"] is False
        assert out["actions"][1]["created"] is True
        assert [x["kind"] for x in out["action_records"]] == ["incident.log", "escalation.request"]
        assert incident_count(settings) == 1


def test_drafts_are_separate_and_confirmed_or_dismissed_once_by_voice_or_tap(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        other = session_for(c, "DOZ_DEMO_001")["session_id"]
        say(c, sid, "t0", "log an incident: loose step on the cab ladder, low severity")  # INC-0001, a normal report
        d1 = make_draft(settings, sid, "EP-1", "Seatbelt unfastened while the engine was running")
        d2 = make_draft(settings, sid, "EP-2", "Prolonged idling")
        assert make_draft(settings, sid, "EP-1", "same episode again") == d1  # one draft per episode
        state = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
        assert [d["draft_id"] for d in state["incident_drafts"]] == [d1, d2] == ["DRF-0001", "DRF-0002"]
        assert [i["incident_id"] for i in state["incidents"]] == ["INC-0001"]  # drafts are not incidents
        first = state["incident_drafts"][0]
        assert (first["origin"], first["status"], first["severity_basis"], first["occurred_basis"]) == (
            "auto_draft", "draft", "rule_default", "observation_time")

        ambiguous = say(c, sid, "t1", "yes")  # two drafts waiting: a bare yes must not confirm either
        assert ambiguous["actions"][0]["type"] == "clarification_needed"
        assert ambiguous["actions"][0]["reason"] == "several_candidates" and ambiguous["action_records"] == []
        ok = say(c, sid, "t2", "confirm draft number 1")
        action = ok["actions"][0]
        assert action["type"] == "incident_confirmed" and action["draft"]["status"] == "confirmed"
        # the real incident ID is allocated once, on confirmation
        assert action["incident"]["incident_id"] == action["draft"]["incident_id"] == "INC-0002"
        assert (action["incident"]["origin"], action["incident"]["draft_id"]) == ("auto_draft", d1)
        assert ok["action_records"][0]["record_id"] == "INC-0002"
        assert say(c, sid, "t2", "confirm draft number 1") == ok

        # taps: edit with a version check, dismiss once, then illegal and foreign attempts change nothing
        edit = incident_command(c, sid, "c1", "incident.edit", d2, expected=1, severity="medium",
                                location_text="by the crusher")
        assert edit.status_code == 200, edit.text
        body = edit.json()["draft"]
        assert (body["version"], body["severity"], body["severity_basis"], body["site_zone_id"], body["zone_basis"]) == (
            2, "medium", "reported", "ZONE_N_HAUL", "reported")
        stale = incident_command(c, sid, "c2", "incident.edit", d2, expected=1, severity="low")
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
        dismiss = incident_command(c, sid, "c3", "incident.dismiss", d2, expected=2)
        assert dismiss.status_code == 200 and dismiss.json()["draft"]["status"] == "dismissed"
        assert dismiss.json()["incident"] is None
        again = incident_command(c, sid, "c3", "incident.dismiss", d2, expected=2)
        assert again.json()["duplicate"] is True and again.json()["draft"] == dismiss.json()["draft"]
        late = incident_command(c, sid, "c4", "incident.confirm", d2)
        assert late.status_code == 409 and late.json()["error"]["code"] == "invalid_transition"
        foreign = incident_command(c, other, "c5", "incident.confirm", d1)
        assert foreign.status_code == 404

        assert say(c, sid, "t3", "yes")["actions"][0]["reason"] == "nothing_pending"
        d3 = make_draft(settings, sid, "EP-3", "Idling with the seatbelt unfastened")
        single = say(c, sid, "t4", "yes")  # exactly one workflow waiting
        assert single["actions"][0]["type"] == "incident_confirmed"
        assert single["actions"][0]["draft"]["draft_id"] == d3
        state = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
        assert [i["incident_id"] for i in state["incidents"]] == ["INC-0001", "INC-0002", "INC-0003"]
        assert state["incident_drafts"] == []


def test_unsupported_capabilities_are_reported_honestly(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        out = say(c, sid, "t1", "what's the weather going to be like")
        assert out["branch"] == "general_assistance" and out["action_records"] == []
        assert out["actions"] == [{"type": "capability_unavailable", "capability": "weather_forecast"}]
        assert "isn't available" in out["speech"]
        conn = sqlite3.connect(settings.db_path)
        saved = conn.execute("SELECT route_json FROM turns WHERE turn_id = 't1'").fetchone()[0]
        conn.close()
        assert '"unsupported"' in saved  # the classification is saved once and reused on a retry
