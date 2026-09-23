"""Batch C1: severity and occurrence-time capture, draft edits by voice and tap, and linked-episode explanations."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.incident_time import interpret, offset_timezone
from cocoon_agent.migrations import MIGRATIONS, migrate
from cocoon_agent.simulation import scenario_events
from cocoon_agent.store import Store

from .conftest import AUTH, seeded_demo
from .test_incidents import incident_command, make_draft
from .test_safety import START, post
from .test_tasks import say, session_for

IST = offset_timezone("+05:30")


def state(c, sid):
    return c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()


def turn_created_at(settings, sid, turn_id) -> datetime:
    conn = sqlite3.connect(settings.db_path)
    try:
        return datetime.fromisoformat(conn.execute("SELECT created_at FROM turns WHERE session_id = ? AND turn_id = ?",
                                                   (sid, turn_id)).fetchone()[0])
    finally:
        conn.close()


@pytest.mark.parametrize("phrase,status,local", [
    ("ten minutes ago", "resolved", "10:20"),
    ("about 25 minutes ago", "resolved", "10:05"),
    ("an hour ago", "resolved", "09:30"),
    ("half an hour ago", "resolved", "10:00"),
    ("just now", "resolved", "10:30"),
    ("at 9:30", "resolved", "09:30"),       # 21:30 is later today, so only one reading is in the past
    ("at 9 am", "resolved", "09:00"),
    ("at 23:10", "resolved", "23:10"),      # 24-hour time just before midnight: yesterday
    ("at 10:45", "ambiguous", None),        # later than now: never guessed as last night
    ("at 2 pm", "ambiguous", None),
    ("this morning", "ambiguous", None),
    ("a few minutes ago", "ambiguous", None),
    ("13 hours ago", "unsupported", None),
    ("at 25:00", "unsupported", None),
    ("when the whistle blew", "unsupported", None),
])
def test_relative_time_expressions_are_interpreted_deterministically(phrase, status, local):
    ref = datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)  # 10:30 at the site
    got = interpret(phrase, ref, IST)
    assert got.status == status and got.expression == phrase
    if local:
        assert got.occurred_at.astimezone(IST).strftime("%H:%M") == local
        assert got.occurred_at.tzinfo is not None
    assert interpret(None, ref, IST).status == "none"
    assert interpret("at 9:30", ref, None).status == "unsupported"  # no trusted site time zone for a clock time


def test_missing_severity_is_saved_as_a_draft_and_asked_for_then_confirmed(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        # alarming wording is not a severity: nothing is inferred
        ask = say(c, sid, "t1", "log an incident: the boom nearly hit a worker and tell my supervisor")
        action = ask["actions"][0]
        assert (action["type"], action["missing_field"]) == ("information_requested", "severity")
        draft = action["draft"]
        assert (draft["origin"], draft["status"], draft["severity"], draft["missing"]) == (
            "operator_report", "draft", None, ["severity"])
        assert draft["notify_supervisor"] is True and draft["occurred_basis"] == "time_of_report"
        assert [r["kind"] for r in ask["action_records"]] == ["incident.draft"]  # the report is already saved
        assert "How serious" in ask["speech"]
        st = state(c, sid)
        assert st["incidents"] == [] and st["pending_question"]["kind"] == "incident_severity"
        assert st["pending_question"]["draft_id"] == draft["draft_id"]
        assert say(c, sid, "t1", "log an incident: the boom nearly hit a worker and tell my supervisor") == ask

        # a bare yes is not a severity: the same question again, nothing written
        yes = say(c, sid, "t2", "yes")
        assert yes["actions"][0]["missing_field"] == "severity" and yes["action_records"] == []

        done = say(c, sid, "t3", "high")
        kinds = [a["type"] for a in done["actions"]]
        assert kinds == ["incident_confirmed", "escalation_requested"]
        inc = done["actions"][0]["incident"]
        assert (inc["severity"], inc["severity_basis"], inc["origin"], inc["draft_id"]) == (
            "high", "reported", "operator_reported", draft["draft_id"])
        assert inc["incident_id"] == "INC-0001"  # the real ID only on confirmation
        assert done["actions"][1]["approval"]["status"] == "pending"
        assert [r["kind"] for r in done["action_records"]] == ["incident.edit", "incident.confirm"]
        assert say(c, sid, "t3", "high") == done  # retry replays, nothing re-run
        st = state(c, sid)
        assert [i["incident_id"] for i in st["incidents"]] == ["INC-0001"]
        assert st["incident_drafts"] == [] and st["pending_question"] is None
        assert len(st["pending_approvals"]) == 1


def test_explicit_unknown_severity_is_recorded_not_guessed(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        say(c, sid, "t1", "report an incident: the step light is broken")
        done = say(c, sid, "t2", "I don't know")
        inc = done["actions"][0]["incident"]
        assert inc["severity"] is None and inc["severity_basis"] == "stated_unknown"
        assert "Severity is recorded as unknown" in done["speech"]
        one_turn = say(c, sid, "t3", "log an incident: loose handrail, severity unknown")["actions"][0]
        assert one_turn["type"] == "incident_logged"
        assert (one_turn["incident"]["severity"], one_turn["incident"]["severity_basis"]) == (None, "stated_unknown")


def test_relative_time_is_fixed_to_the_first_receipt_and_retries_do_not_move_it(tmp_path, monkeypatch):
    settings, _ = seeded_demo(tmp_path)
    original = Store.incident_report
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            def mutate(c):
                raise RuntimeError("simulated crash before the report was saved")
            return mutate
        return original(*args, **kwargs)

    monkeypatch.setattr(Store, "incident_report", staticmethod(flaky))
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        text = "log an incident: the bucket clipped the fence ten minutes ago, low severity"
        body = {"turn_id": "t1", "text": text, "source": "voice"}
        assert c.post(f"/v1/sessions/{sid}/turns", headers=AUTH, json=body).status_code == 500
        time.sleep(1.1)  # the retry happens later on the wall clock
        out = say(c, sid, "t1", text)
        inc = out["actions"][0]["incident"]
        received = turn_created_at(settings, sid, "t1")
        assert datetime.fromisoformat(inc["occurred_reference_at"]) == received
        assert datetime.fromisoformat(inc["occurred_at"]) == received - timedelta(minutes=10)
        assert (inc["occurred_basis"], inc["occurred_expression"]) == ("operator_relative", "ten minutes ago")
        assert inc["description"] == "the bucket clipped the fence"
        assert "Recorded as happening ten minutes ago" in out["speech"]


def test_ambiguous_time_is_clarified_and_unknown_time_stays_labelled_as_report_time(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        ask = say(c, sid, "t1", "log an incident: a mirror cracked this morning, low severity")
        a = ask["actions"][0]
        assert (a["missing_field"], a["reason"]) == ("occurred_time", "not precise enough")
        assert a["draft"]["occurred_basis"] == "unresolved" and a["draft"]["occurred_at"] is None
        assert a["draft"]["occurred_expression"] == "this morning"
        assert "couldn't pin down 'this morning'" in ask["speech"]
        again = say(c, sid, "t2", "a few minutes ago")  # still vague: asked again, still one draft
        assert again["actions"][0]["missing_field"] == "occurred_time" and again["action_records"] == []
        done = say(c, sid, "t3", "twenty minutes ago")
        inc = done["actions"][0]["incident"]
        received = turn_created_at(settings, sid, "t3")  # the answer is interpreted when it was said
        assert datetime.fromisoformat(inc["occurred_at"]) == received - timedelta(minutes=20)
        assert inc["occurred_expression"] == "twenty minutes ago" and inc["severity"] == "low"

        say(c, sid, "t4", "log an incident: a cone was knocked over earlier, low severity")
        unknown = say(c, sid, "t5", "not sure")["actions"][0]["incident"]
        first_receipt = turn_created_at(settings, sid, "t4")
        assert unknown["occurred_basis"] == "time_of_report"  # labelled, not claimed as the occurrence time
        assert datetime.fromisoformat(unknown["occurred_at"]) == first_receipt
        assert unknown["occurred_expression"] == "earlier"  # the original phrase is preserved


def test_draft_edits_by_voice_and_tap_share_versions_and_confirmation_needs_every_fact(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        auto = make_draft(settings, sid, "EP-1", "Seatbelt unfastened while the engine was running")
        edited = say(c, sid, "t1", "set draft 1 severity to low")
        d = edited["actions"][0]
        assert d["type"] == "incident_draft_edited" and d["draft"]["version"] == 2
        assert (d["draft"]["severity"], d["draft"]["severity_basis"]) == ("low", "reported")
        stale = incident_command(c, sid, "c1", "incident.edit", auto, expected=1, severity="high")
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"

        # tap: a time phrase is interpreted at the command's first receipt; a retry replays the saved result
        tap = incident_command(c, sid, "c2", "incident.edit", auto, expected=2, occurred_expression="five minutes ago")
        assert tap.status_code == 200, tap.text
        body = tap.json()["draft"]
        assert (body["occurred_basis"], body["occurred_expression"], body["version"]) == (
            "operator_relative", "five minutes ago", 3)
        ref = datetime.fromisoformat(body["occurred_reference_at"])
        assert datetime.fromisoformat(body["occurred_at"]) == ref - timedelta(minutes=5)
        time.sleep(1.1)
        replay = incident_command(c, sid, "c2", "incident.edit", auto, expected=2,
                                  occurred_expression="five minutes ago")
        assert replay.json()["duplicate"] is True and replay.json()["draft"] == body
        bad = incident_command(c, sid, "c3", "incident.edit", auto, occurred_expression="this morning")
        assert bad.status_code == 422 and bad.json()["error"]["details"][0]["field"] == "body.payload.occurred_expression"
        both = incident_command(c, sid, "c4", "incident.edit", auto, severity="low", severity_unknown=True)
        assert both.status_code == 422

        # an operator draft missing its severity cannot be confirmed by a tap
        say(c, sid, "t2", "log an incident: the wiper arm is bent")
        op_draft = state(c, sid)["incident_drafts"][-1]
        assert op_draft["missing"] == ["severity"]
        refused = incident_command(c, sid, "c5", "incident.confirm", op_draft["draft_id"])
        assert refused.status_code == 409
        assert {"field": "draft.severity", "issue": "not stated yet"} in refused.json()["error"]["details"]
        assert len(state(c, sid)["incidents"]) == 0
        ok = incident_command(c, sid, "c6", "incident.edit", op_draft["draft_id"], severity_unknown=True)
        assert ok.json()["draft"]["missing"] == []
        confirmed = incident_command(c, sid, "c7", "incident.confirm", op_draft["draft_id"])
        assert confirmed.status_code == 200 and confirmed.json()["incident"]["severity_basis"] == "stated_unknown"


def test_cancelling_the_question_keeps_the_draft_and_it_can_be_finished_later(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        say(c, sid, "t1", "log an incident: oil on the cab floor")
        cancel = say(c, sid, "t2", "never mind")
        assert cancel["actions"][0]["kept_draft_number"] == 1 and "stays saved as draft number 1" in cancel["speech"]
        st = state(c, sid)
        assert st["pending_question"] is None and st["incident_drafts"][0]["missing"] == ["severity"]
        ask = say(c, sid, "t3", "confirm draft number 1")  # confirmation needs the missing fact first
        assert ask["actions"][0]["missing_field"] == "severity" and ask["action_records"] == []
        done = say(c, sid, "t4", "medium")
        assert done["actions"][0]["type"] == "incident_confirmed"
        assert done["actions"][0]["incident"]["severity"] == "medium"


def test_combined_episode_has_its_own_identity_and_is_explained_with_its_parent(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        events = scenario_events("EXC_DEMO_001", "belt_idle", START, "cmb")
        for e in events[:6]:  # belt unfastened, then idling unbelted for 75 s (no prolonged idle yet)
            post(c, sid, e)
        active = {a["alert_type"]: a for a in state(c, sid)["active_alerts"]}
        belt, combined = active["seatbelt_unfastened"], active["idle_unbelted"]
        assert combined["correlated_alert_id"] == belt["alert_id"] and combined["announced"] is False
        assert combined["rule_id"] == "prototype.idle_unbelted.v1" and combined["evidence"]["idle_seconds_observed"] >= 60
        why = say(c, sid, "t1", "why did you warn me?")["actions"][0]
        assert why["alert"]["alert_id"] == belt["alert_id"]
        assert [r["alert_id"] for r in why["related_alerts"]] == [combined["alert_id"]]
        idle = say(c, sid, "t2", "why did you warn me about idling?")
        action = idle["actions"][0]
        assert action["alert"]["alert_id"] == combined["alert_id"]  # explainable on its own saved evidence
        assert action["announcement_event_id"] == why["announcement_event_id"]  # covered by the parent's warning
        assert "combined episode" in say(c, sid, "t3", "why?")["speech"]
        evidence_before = combined["evidence"]
        for e in events[6:8]:
            post(c, sid, e)
        after = next(a for a in state(c, sid)["active_alerts"] if a["alert_id"] == combined["alert_id"])
        assert after["evidence"] == evidence_before  # the published snapshot never changes


def test_populated_v7_database_upgrades_and_old_drafts_keep_numbers(tmp_path):
    path = tmp_path / "v7.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn, MIGRATIONS[:7])
    conn.execute("INSERT INTO sessions(session_id, client_session_key, room_name, participant_identity, operator_id,"
                 " machine_id, created_at, binding_status) VALUES ('ses_1', 'k', 'r', 'p', 'OP_TEST_1', 'EXC_DEMO_001',"
                 " '2026-09-24T01:00:00+00:00', 'catalog_verified')")
    for n in (1, 2):
        conn.execute("INSERT INTO incident_drafts(session_id, operator_id, machine_id, origin, description, severity,"
                     " severity_basis, occurred_at, occurred_basis, episode_id, created_at) VALUES ('ses_1', 'OP_TEST_1',"
                     " 'EXC_DEMO_001', 'auto_draft', ?, 'medium', 'rule_default', '2026-09-24T01:00:00+00:00',"
                     " 'observation_time', ?, '2026-09-24T01:00:00+00:00')", (f"episode {n}", f"EP-{n}"))
        conn.execute("UPDATE incident_drafts SET draft_id = ? WHERE draft_number = ?", (f"DRF-000{n}", n))
    conn.close()
    store = Store(path)
    assert store.init_schema() == ["applied:8"]
    session = store.get_session("ses_1")
    old = store.list_drafts("ses_1")
    assert [(d.draft_number, d.origin, d.missing) for d in old] == [(1, "auto_draft", []), (2, "auto_draft", [])]
    result, _ = store.run_command(scope="t", command_id="c1", kind="incident.confirm", fingerprint="f",
                                  session_id="ses_1", turn_id=None,
                                  mutate=Store.incident_transition(session, "incident.confirm", "DRF-0002", None))
    assert result["incident"]["incident_id"] == "INC-0001" and result["draft"]["status"] == "confirmed"
    with store._tx() as c:
        new_id = Store.insert_auto_draft(c, session, "EP-3", "episode 3", "medium", datetime.now(timezone.utc))
    assert new_id == "DRF-0003"
    store.close()


def test_a_503_after_committed_actions_names_them_and_the_retry_does_not_repeat_them(tmp_path):
    from .test_resilience import FlakyComposeBrain

    settings, _ = seeded_demo(tmp_path)
    with TestClient(create_app(settings, brain=FlakyComposeBrain(failures=1))) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        body = {"turn_id": "t1", "text": "log an incident: exhaust leak, high severity, and tell my supervisor",
                "source": "voice"}
        failed = c.post(f"/v1/sessions/{sid}/turns", headers=AUTH, json=body)
        assert failed.status_code == 503 and failed.json()["error"]["code"] == "llm_unavailable"
        err = failed.json()["error"]
        assert "2 action(s) were saved" in err["message"]  # a 503 is never read as "nothing was saved"
        status = c.get(f"/v1/sessions/{sid}/turns/t1", headers=AUTH).json()
        assert [(r["kind"], r["outcome"]) for r in status["action_records"]] == [
            ("incident.log", "completed"), ("escalation.request", "completed")]
        assert state(c, sid)["pending_approvals"][0]["status"] == "pending"  # requested, not sent or approved
        retry = say(c, sid, "t1", body["text"])
        assert [a["created"] for a in retry["actions"]] == [False, False]
        assert len(state(c, sid)["incidents"]) == 1 and len(state(c, sid)["pending_approvals"]) == 1
