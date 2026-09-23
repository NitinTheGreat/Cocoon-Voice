"""Batch B4: explanations from saved evidence, idle reasons, once-per-shift briefing and episode-linked training."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.simulation import scenario_events

from .conftest import AUTH, seeded_demo
from .test_safety import START, post, sample
from .test_tasks import say, session_for


def state(c, sid):
    return c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()


def test_why_uses_saved_evidence_and_asks_when_warnings_compete(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        say(c, sid, "t0", "what's my next task")
        post(c, sid, sample("e0", START, belt=True))
        opened = post(c, sid, sample("e1", START + timedelta(seconds=10), belt=False))
        belt_id = opened["alerts_opened"][0]
        event_id = opened["announcements_created"][0]
        c.post(f"/v1/sessions/{sid}/events/{event_id}/delivery", headers=AUTH,
               json={"consumer_id": "voice-1", "status": "played"})
        # the readings change after the warning; the explanation must still use the triggering sample
        post(c, sid, sample("e2", START + timedelta(seconds=20), belt=False, state="working", speed=4.0))
        why = say(c, sid, "t1", "Why did you warn me?")
        action = why["actions"][0]
        assert action["alert"]["alert_id"] == belt_id and action["announcement_event_id"] == event_id
        assert action["alert"]["evidence"]["event_id"] == "e1"
        assert action["alert"]["evidence"]["readings"]["operating_state"] == "idle"
        assert [d["status"] for d in action["deliveries"]] == ["played"]  # delivery, not acknowledgement
        assert "fasten your seatbelt" in why["speech"].lower()
        assert state(c, sid)["active_alerts"][0]["status"] == "active"  # explaining or playing clears nothing

        # a second warning announced since the last turn: "why?" is ambiguous, a named one is not
        post(c, sid, sample("e3", START + timedelta(seconds=30), belt=False, state="idle"))
        post(c, sid, sample("e4", START + timedelta(seconds=340), belt=False, state="idle"))
        say(c, sid, "t2", "what's my next task")
        post(c, sid, sample("e5", START + timedelta(seconds=350), belt=True, state="working", speed=3.0))
        post(c, sid, sample("e6", START + timedelta(seconds=360), belt=False, state="idle"))
        post(c, sid, sample("e7", START + timedelta(seconds=700), belt=False, state="idle"))
        ambiguous = say(c, sid, "t3", "why?")
        assert ambiguous["actions"][0]["type"] == "clarification_needed"
        assert ambiguous["actions"][0]["options"] == ["the idling warning", "the seatbelt warning"]
        named = say(c, sid, "t4", "why did you warn me about idling?")
        assert named["actions"][0]["alert"]["alert_type"] == "prolonged_idle"


def test_waiting_reason_is_recorded_and_keeps_the_belt_warning(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    events = scenario_events("EXC_DEMO_001", "belt_idle", START, "w")
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        for e in events[:8]:  # up to prolonged idle, still unbelted
            post(c, sid, e)
        idle_alert = next(a for a in state(c, sid)["active_alerts"] if a["alert_type"] == "prolonged_idle")
        out = say(c, sid, "t1", "I'm waiting for a truck")
        action = out["actions"][0]
        assert action["type"] == "idle_reason_recorded" and action["alert_id"] == idle_alert["alert_id"]
        assert action["belt_warning_active"] is True and "still active" in out["speech"]
        assert say(c, sid, "t1", "I'm waiting for a truck") == out  # retry: recorded once
        s = state(c, sid)
        assert len(s["idle_reasons"]) == 1 and s["idle_reasons"][0]["alert_id"] == idle_alert["alert_id"]
        assert {a["alert_type"] for a in s["active_alerts"]} == {"seatbelt_unfastened", "idle_unbelted",
                                                                "prolonged_idle"}


def test_one_briefing_per_shift_across_sessions_and_restarts(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        first = session_for(c, "EXC_DEMO_001")["session_id"]
        second = session_for(c, "EXC_DEMO_001", key="another-device")["session_id"]
        events = c.get(f"/v1/sessions/{first}/events?after=0", headers=AUTH).json()["events"]
        assert [e["type"] for e in events] == ["shift_briefing"]
        speech = events[0]["speech"]
        assert "Excavate the north pit bench" in speech and "North pit" in speech and "07:30" in speech
        # C2: conditions come from the weather check; with no weather source the briefing says so (never "fine")
        assert "weather isn't available" in speech and "3 tasks" in speech
        assert c.get(f"/v1/sessions/{second}/events?after=0", headers=AUTH).json()["events"] == []
        assert state(c, second)["shift_briefing"]["event_id"] == events[0]["event_id"]
    with TestClient(create_app(settings)) as c:  # restart + session retrieval: still one briefing
        assert session_for(c, "EXC_DEMO_001")["session_id"] == first
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM announcements WHERE type = 'shift_briefing'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM shift_briefings").fetchone()[0] == 1
    conn.close()


def test_belt_episode_assigns_the_lesson_once_and_it_can_be_read_without_completing(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        other = session_for(c, "DOZ_DEMO_001")["session_id"]
        out = [post(c, sid, e) for e in scenario_events("EXC_DEMO_001", "belt_retrigger", START, "tr")]
        s = state(c, sid)
        assert len(s["training_assignments"]) == 1  # two episodes, one outstanding lesson
        assignment = s["training_assignments"][0]
        assert (assignment["lesson_id"], assignment["status"]) == ("L1", "assigned")
        assert assignment["source_episode_id"] == out[1]["alerts_opened"][0]
        conn = sqlite3.connect(settings.db_path)
        links = conn.execute("SELECT training_assignment_id FROM alerts WHERE alert_type = 'seatbelt_unfastened'"
                             " ORDER BY started_at").fetchall()
        conn.close()
        assert links == [(assignment["assignment_id"],), (assignment["assignment_id"],)]
        assert state(c, other)["training_assignments"] == []  # another operator's assignment is not visible

        read = say(c, sid, "t1", "read my seatbelt lesson")
        action = read["actions"][0]
        assert action["type"] == "lesson_content" and action["assignment"]["assignment_id"] == assignment["assignment_id"]
        assert action["lesson"]["version"] == "L1.demo.1"
        assert action["lesson"]["content_status"] == "demo_authored_unreviewed"
        assert "does not mark it complete" in read["speech"]
        assert state(c, sid)["training_assignments"][0]["status"] == "assigned"  # reading is not completion


def test_episode_lesson_never_links_a_legacy_record(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        conn = sqlite3.connect(settings.db_path)
        conn.execute("INSERT INTO sessions(session_id, client_session_key, room_name, participant_identity,"
                     " operator_id, machine_id, created_at) VALUES ('ses_legacy', 'legacy', 'old', 'old',"
                     " 'OP_DEMO_1_1', 'cat-320-demo', '2026-09-01T00:00:00+00:00')")
        conn.execute("INSERT INTO training_assignments(assignment_id, operator_id, lesson_id, session_id,"
                     " source_turn_id, status, assigned_at) VALUES ('TA-legacy', 'OP_DEMO_1_1', 'L1', 'ses_legacy',"
                     " 'x', 'assigned', '2026-09-01T00:00:01+00:00')")
        conn.commit()
        conn.close()
        opened = [post(c, sid, e) for e in scenario_events("EXC_DEMO_001", "selection_check", START, "lg")]
        alert = state(c, sid)
        assert alert["training_assignments"] == []  # the legacy record stays withheld
        conn = sqlite3.connect(settings.db_path)
        link = conn.execute("SELECT training_assignment_id FROM alerts WHERE alert_id = ?",
                            (opened[1]["alerts_opened"][0],)).fetchone()[0]
        conn.close()
        assert link is None
