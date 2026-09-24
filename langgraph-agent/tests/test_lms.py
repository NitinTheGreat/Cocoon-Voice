"""Batch C4: curriculum and media, lesson progress by voice and tap, quizzes and a scenario, learning levels,
operator isolation and episode-linked coaching."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.lms import load_lms

from .conftest import AUTH, seeded_demo
from .test_auth import issue
from .test_tasks import say, session_for

LMS = load_lms()
ANSWERS = {lesson.lesson_id: [q.answer for q in lesson.quiz.questions] for lesson in LMS.c.lessons if lesson.quiz}


def cmd(c, sid, cid, kind, headers=AUTH, **payload):
    return c.post(f"/v1/sessions/{sid}/commands", headers=headers,
                  json={"command_id": cid, "kind": kind, "payload": payload})


def learning(c, sid, headers=AUTH) -> dict:
    return c.get(f"/v1/sessions/{sid}/lessons", headers=headers).json()


def progress(c, sid, lesson_id) -> dict:
    return next(x for x in learning(c, sid)["lessons"] if x["lesson_id"] == lesson_id)


def complete_by_tap(c, sid, lesson_id, answers=None, prefix="") -> dict:
    """Steps then assessment through the tap route; returns the final command result."""
    assert cmd(c, sid, f"{prefix}{lesson_id}-start", "lesson.start", lesson_id=lesson_id).status_code == 200
    lesson = LMS.lessons[lesson_id]
    for i in range(len(lesson.steps)):
        r = cmd(c, sid, f"{prefix}{lesson_id}-next-{i}", "lesson.next", lesson_id=lesson_id)
        assert r.status_code == 200, r.text
    r = cmd(c, sid, f"{prefix}{lesson_id}-quiz", "quiz.start", lesson_id=lesson_id)
    assert r.status_code == 200, r.text
    q = r.json()["learning"]["question"]
    for i, choice in enumerate(answers or ANSWERS[lesson_id]):
        r = cmd(c, sid, f"{prefix}{lesson_id}-a{i}", "quiz.answer", attempt_id=q["attempt_id"],
                question_id=q["question_id"], choice_id=choice)
        assert r.status_code == 200, r.text
        q = r.json()["learning"].get("question") or q
    return r.json()


@pytest.fixture
def demo(tmp_path):
    settings, _ = seeded_demo(tmp_path)
    return settings


def test_public_payloads_never_carry_answer_keys_and_media_is_catalogued(demo):
    with TestClient(create_app(demo)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        view = c.get(f"/v1/sessions/{sid}/lessons/L1", headers=AUTH).json()
        text = json.dumps(view)
        assert '"answer"' not in text and "remediation" not in text and "Stay in the seat, belted" not in text
        assert view["assessment"] == {"kind": "quiz", "quiz_id": "Q-L1", "version": 1, "question_count": 3,
                                      "pass_mark": 0.66}
        assert view["review_status"] == "demo_authored_unreviewed" and view["intended_duration_seconds"] == 60
        media = view["media"][0]
        assert (media["availability"], media["mime_type"], media["content_ref"]) == (
            "available", "video/mp4", "/v1/content/MEDIA-L1-SEATBELT-V1/file")
        assert c.get("/v1/sessions/" + sid + "/lessons/NOPE", headers=AUTH).status_code == 404


def test_the_video_is_served_by_catalog_id_and_decodes(demo, tmp_path):
    with TestClient(create_app(demo)) as c:
        meta = c.get("/v1/content/MEDIA-L1-SEATBELT-V1", headers=AUTH).json()
        r = c.get(meta["content_ref"], headers=AUTH)
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
        assert hashlib.sha256(r.content).hexdigest() == meta["checksum_sha256"]
        assert r.headers["etag"] == f'"{meta["checksum_sha256"]}"'
        captions = c.get(meta["captions_ref"], headers=AUTH)
        assert captions.status_code == 200 and captions.text.startswith("WEBVTT")
        assert c.get(meta["content_ref"]).status_code == 401  # needs the bearer token
        for bad in ("../curriculum_v1.json", "..%2Fcurriculum_v1.json", "UNKNOWN"):
            assert c.get(f"/v1/content/{bad}/file", headers=AUTH).status_code == 404  # never an arbitrary path
        if shutil.which("ffprobe") and shutil.which("ffmpeg"):
            path = tmp_path / "download.mp4"
            path.write_bytes(r.content)
            probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                               "format=duration:stream=codec_name", "-of", "json", str(path)],
                                              check=True, capture_output=True, text=True).stdout)
            assert probe["streams"][0]["codec_name"] == "h264"
            assert abs(float(probe["format"]["duration"]) - meta["duration_seconds"]) < 0.5
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], check=True)  # full decode


def test_voice_lesson_quiz_fail_retake_pass_and_replay(demo):
    with TestClient(create_app(demo)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        first = say(c, sid, "t1", "start the seatbelt lesson")
        a = first["actions"][0]
        assert (a["type"], a["event"], a["step"]["index"]) == ("learning", "lesson_started", 1)
        assert "Step 1 of 5" in first["speech"]
        assert c.get("/v1/content/MEDIA-L1-SEATBELT-V1/file", headers=AUTH).status_code == 200
        assert progress(c, sid, "L1")["status"] == "in_progress"  # media retrieval is not progress
        early = say(c, sid, "t2", "quiz me")["actions"][0]
        assert early["event"] == "rejected" and "finish the lesson steps" in early["reason"]
        for i in range(4):
            step = say(c, sid, f"n{i}", "next")["actions"][0]
        assert step["step"]["index"] == 5 and step["media"][0]["asset_id"] == "MEDIA-L1-SEATBELT-V1"
        ready = say(c, sid, "n5", "next")["actions"][0]
        assert ready["event"] == "assessment_ready"
        assert progress(c, sid, "L1")["status"] == "awaiting_assessment"  # reading every step is not completion

        q1 = say(c, sid, "q1", "quiz me")
        assert q1["actions"][0]["question"]["index"] == 1 and "Say A, B or C" in q1["speech"]
        assert "answer" not in json.dumps(q1["actions"][0]["question"])
        yes = say(c, sid, "q2", "yes")  # a bare yes is never an answer
        assert yes["actions"][0]["event"] == "answer_unclear" and yes["action_records"] == []
        wrong = say(c, sid, "q3", "a")
        fb = wrong["actions"][0]["feedback"]
        assert fb["correct"] is False and fb["correct_choice_id"] == "b" and "Not quite" in wrong["speech"]
        assert say(c, sid, "q3", "a") == wrong  # same turn: replayed, not answered twice
        say(c, sid, "q4", "jump clear of the cab")  # matched by words: wrong
        failed = say(c, sid, "q5", "stop, lower the attachment and park")["actions"][0]  # right
        assert failed["result"]["status"] == "failed" and failed["result"]["correct"] == 1
        assert failed["result"]["lesson_completed"] is False
        p = progress(c, sid, "L1")
        assert (p["status"], p["attempts"], p["last_attempt_status"]) == ("awaiting_assessment", 1, "failed")

        retake = say(c, sid, "r1", "retake the quiz")["actions"][0]
        assert retake["event"] == "assessment_started" and retake["attempt_number"] == 2
        say(c, sid, "r2", "b")
        say(c, sid, "r3", "the third one")
        done = say(c, sid, "r4", "a")
        result = done["actions"][0]["result"]
        assert (result["status"], result["correct"], result["lesson_completed"]) == ("passed", 3, True)
        assert "complete" in done["speech"]
        p = progress(c, sid, "L1")
        assert (p["status"], p["best_score"], p["attempts"]) == ("completed", 1.0, 2)
        assert c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["pending_question"] is None


def test_tap_commands_share_the_rules_replay_and_reject_stale_questions(demo):
    with TestClient(create_app(demo)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        assert cmd(c, sid, "s", "lesson.start", lesson_id="L6").status_code == 409  # prerequisite L1
        start = cmd(c, sid, "s1", "lesson.start", lesson_id="L4")
        assert start.json()["learning"]["step"]["index"] == 1
        stale = cmd(c, sid, "n0", "lesson.next", lesson_id="L4", expected_step=3)
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
        for i in range(3):
            cmd(c, sid, f"n{i + 1}", "lesson.next", lesson_id="L4")
        q = cmd(c, sid, "qs", "quiz.start", lesson_id="L4").json()["learning"]["question"]
        a1 = cmd(c, sid, "a1", "quiz.answer", attempt_id=q["attempt_id"], question_id=q["question_id"], choice_id="b")
        again = cmd(c, sid, "a1", "quiz.answer", attempt_id=q["attempt_id"], question_id=q["question_id"],
                    choice_id="b")
        assert again.json()["duplicate"] is True and again.json()["learning"] == a1.json()["learning"]
        old = cmd(c, sid, "a1b", "quiz.answer", attempt_id=q["attempt_id"], question_id=q["question_id"],
                  choice_id="a")  # the first question was already answered
        assert old.status_code == 409 and old.json()["error"]["code"] == "version_conflict"
        nxt = a1.json()["learning"]["question"]
        bad = cmd(c, sid, "a2x", "quiz.answer", attempt_id=q["attempt_id"], question_id=nxt["question_id"],
                  choice_id="z")
        assert bad.status_code == 422


def test_progress_survives_restart_and_new_session_and_is_never_shared(demo, tmp_path):
    with TestClient(create_app(demo)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        other = session_for(c, "DOZ_DEMO_001")["session_id"]
        say(c, sid, "t1", "start the idling lesson")
        say(c, sid, "t2", "next")
        paused = say(c, sid, "t3", "pause")["actions"][0]
        assert paused["event"] == "lesson_paused" and progress(c, sid, "L4")["status"] == "paused"
        deferred = say(c, sid, "t4", "later")["actions"][0]
        assert deferred["event"] == "lesson_deferred" and progress(c, sid, "L4")["deferred_until"]
    with TestClient(create_app(demo)) as c:  # orderly restart, then a new authorized session for the same operator
        new = session_for(c, "EXC_DEMO_001", key="lk:another-room:op")["session_id"]
        assert new != sid
        resumed = say(c, new, "t1", "continue my lesson")["actions"][0]
        assert resumed["lesson_id"] == "L4" and resumed["step"]["index"] == 3  # continues after step 2
        assert progress(c, other, "L4")["status"] == "not_started"  # another operator never sees it
        token, _ = issue(demo, tmp_path, "operator", "--operator-id", "OP_DEMO_2_1")
        op2 = {"Authorization": f"Bearer {token}"}
        assert c.get(f"/v1/sessions/{new}/lessons", headers=op2).status_code == 404
        assert cmd(c, other, "x", "lesson.next", headers=op2, lesson_id="L4").status_code == 404


def test_levels_follow_versioned_criteria_with_evidence_and_the_scenario_branches(demo):
    with TestClient(create_app(demo)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        assert learning(c, sid)["level"] == "beginner"
        complete_by_tap(c, sid, "L1")
        complete_by_tap(c, sid, "L4")
        out = complete_by_tap(c, sid, "L5")
        change = out["learning"]["level_change"]
        assert (change["from"], change["to"], change["criteria_version"]) == (
            "beginner", "intermediate", "demo-levels-2026-09-24.1")
        assert set(change["evidence"]["lessons"]) == {"L1", "L4", "L5"}
        view = learning(c, sid)
        assert view["level"] == "intermediate" and view["level_history"][0]["evidence"]["average_best_score"] == 1.0
        assert view["dataset_operator_skill"] is None  # dataset skill stays a separate (here absent) field

        complete_by_tap(c, sid, "L6")
        complete_by_tap(c, sid, "L7")
        assert learning(c, sid)["level"] == "intermediate"  # expert also needs the practice scenario
        cmd(c, sid, "sc-start", "lesson.start", lesson_id="S1")
        cmd(c, sid, "sc-next", "lesson.next", lesson_id="S1")
        node = cmd(c, sid, "sc-quiz", "quiz.start", lesson_id="S1").json()["learning"]["question"]
        assert node["kind"] == "scenario" and node["total"] is None
        fail = cmd(c, sid, "sc-bad", "quiz.answer", attempt_id=node["attempt_id"], question_id=node["question_id"],
                   choice_id="b").json()["learning"]
        assert fail["result"]["status"] == "failed" and fail["result"]["lesson_completed"] is False
        node = cmd(c, sid, "sc-quiz2", "quiz.start", lesson_id="S1").json()["learning"]["question"]
        for i in range(3):
            r = cmd(c, sid, f"sc-{i}", "quiz.answer", attempt_id=node["attempt_id"], question_id=node["question_id"],
                    choice_id="a").json()["learning"]
            node = r.get("question") or node
        assert r["result"]["status"] == "passed" and r["level_change"]["to"] == "expert"


def test_episode_lesson_is_offered_only_when_parked_after_warnings_and_after_a_deferral(demo):
    start = datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc)

    def obs(c, sid, n, secs, belt=True, state="working"):
        return c.post(f"/v1/sessions/{sid}/telemetry", headers=AUTH, json={
            "event_id": f"e{n}", "observed_at": (start + timedelta(seconds=secs)).isoformat(), "simulated": True,
            "readings": {"engine_on": state != "off", "seatbelt_fastened": belt, "idle_seconds": 0,
                         "operating_state": state}}).json()

    with TestClient(create_app(demo)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        obs(c, sid, 1, 0)
        warn = obs(c, sid, 2, 10, belt=False)  # belt warning: L1 assigned for this episode
        assert warn["alerts_opened"]
        assigned = learning(c, sid)["lessons"][0]
        assert assigned["assignment_id"] and assigned["assigned_for_episode_id"] == warn["alerts_opened"][0]
        assert not any(a.startswith("ann_coach") for a in obs(c, sid, 3, 20, belt=True)["announcements_created"])
        # warning cleared, but the machine is working: no coaching yet (not a safe time)
        say(c, sid, "t1", "later")  # no active lesson: this is not a deferral
        c.post(f"/v1/sessions/{sid}/commands", headers=AUTH, json={
            "command_id": "defer", "kind": "lesson.defer", "payload": {"lesson_id": "L1", "defer_minutes": 10}})
        assert "ann_coach" not in json.dumps(obs(c, sid, 4, 60, state="off"))  # parked, but deferred
        later = obs(c, sid, 5, 700, state="off")  # parked (engine off), no warning, deferral over
        assert [a for a in later["announcements_created"] if a.startswith("ann_coach")]
        assert not obs(c, sid, 6, 760, state="off")["announcements_created"]  # offered once
        put_off = say(c, sid, "t1b", "later")["actions"][0]  # right after the offer, "later" defers that lesson
        assert (put_off["event"], put_off["lesson_id"]) == ("lesson_deferred", "L1")
        needs = say(c, sid, "t2", "what training do I need?")
        assert needs["actions"][0]["event"] == "training_needs" and "after a safety warning" in needs["speech"]
        prog = say(c, sid, "t3", "how am I progressing?")
        assert "not a certification" in prog["speech"]


def test_legacy_or_unverified_sessions_have_no_learning_record(client):
    from .conftest import new_session
    sid = new_session(client)  # catalog-verified with the test catalog: allowed
    assert client.get(f"/v1/sessions/{sid}/lessons", headers=AUTH).status_code == 200
    import sqlite3
    conn = sqlite3.connect(client.app.state.service.settings.db_path)
    conn.execute("DROP TRIGGER sessions_association_immutable")  # test-only: simulate a legacy row
    conn.execute("UPDATE sessions SET binding_status = 'legacy_unverified' WHERE session_id = ?", (sid,))
    conn.commit()
    conn.close()
    assert client.get(f"/v1/sessions/{sid}/lessons", headers=AUTH).status_code == 404
    r = cmd(client, sid, "x", "lesson.start", lesson_id="L1")
    assert r.status_code == 404
