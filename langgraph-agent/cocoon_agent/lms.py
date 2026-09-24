"""Core LMS (Batch C4): versioned curriculum, progress, voice quizzes, a branching practice scenario, learning levels
and episode-linked coaching.

Content comes from the tracked curriculum file (content/curriculum_v1.json) and is copied once per lesson version into
`lesson_versions`, so progress and attempts stay pinned to the exact version they started on. Public projections never
contain answer keys. All writes are mutations run through Store.run_command (turn-scoped for voice, principal-scoped
for taps), so a retried command replays its saved result.

Rules kept explicit:
- a lesson completes only when every step was presented AND its assessment was passed; reading text, media playback,
  assignment or a confident spoken answer never complete it;
- scoring is server-side and deterministic; a failed attempt never completes the lesson; retakes are new attempts;
- learning levels (Beginner/Intermediate/Expert) follow the versioned criteria with saved evidence, and are separate
  from the dataset operator_skill and from any equipment certification;
- lessons are available to catalog_verified operator sessions only (learner = the catalog operator); legacy sessions
  and other operators never see or continue someone's progress.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .api import schemas as s
from .store import InvalidInput, InvalidTransition, NotFound, Store, VersionConflict, iso, parse_dt, utcnow

SERVICE_DIR = Path(__file__).resolve().parent.parent
CONTENT_ROOT = SERVICE_DIR / "content"
DEFAULT_CURRICULUM = CONTENT_ROOT / "curriculum_v1.json"
END_PASS, END_FAIL = "END_PASS", "END_FAIL"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Choice(_Strict):
    choice_id: str
    text: str


class Question(_Strict):
    question_id: str
    prompt: str
    choices: list[Choice]
    answer: str
    remediation: str


class Quiz(_Strict):
    quiz_id: str
    version: int
    pass_mark: float = Field(gt=0, le=1)
    questions: list[Question]


class Option(_Strict):
    option_id: str
    text: str
    next: str
    safe: bool
    feedback: str


class Node(_Strict):
    node_id: str
    prompt: str
    options: list[Option]


class Scenario(_Strict):
    quiz_id: str
    version: int
    start: str
    pass_mark: float
    nodes: list[Node]

    def node(self, node_id: str) -> Node:
        return next(n for n in self.nodes if n.node_id == node_id)


class Step(_Strict):
    step_id: str
    kind: Literal["text", "media"]
    speak: str


class Lesson(_Strict):
    lesson_id: str
    version: str
    title: str
    level: Literal["beginner", "intermediate", "expert"]
    summary: str
    intended_duration_seconds: int
    machine_applicability: list[str]
    prerequisites: list[str]
    media: list[str]
    steps: list[Step]
    quiz: Quiz | None = None
    scenario: Scenario | None = None

    @property
    def assessment_kind(self) -> Literal["quiz", "scenario"]:
        return "scenario" if self.scenario else "quiz"

    def applies_to(self, category: str | None) -> bool:
        return "all" in self.machine_applicability or category in self.machine_applicability


class Media(_Strict):
    asset_id: str
    version: int
    kind: Literal["video"]
    mime_type: str
    title: str
    path: str
    captions_path: str | None = None
    duration_seconds: float
    checksum_sha256: str
    licence: str
    provenance: str
    review_status: str
    playback: str


class LevelCriterion(_Strict):
    level: Literal["beginner", "intermediate", "expert"]
    completed_lessons: list[str]
    passed_scenarios: list[str]
    min_average_best_score: float


class Levels(_Strict):
    criteria_version: str
    note: str
    order: list[str]
    criteria: list[LevelCriterion]


class Curriculum(_Strict):
    schema_id: Literal["cocoon.curriculum.v1"] = Field(alias="schema")
    curriculum_version: str
    review_status: str
    author: str
    sources_consulted: list[dict[str, str]]
    media: list[Media]
    courses: list[dict[str, Any]]
    lessons: list[Lesson]
    levels: Levels
    retake_policy: str
    defer_policy: str


class LMS:
    def __init__(self, curriculum: Curriculum, root: Path = CONTENT_ROOT):
        self.c = curriculum
        self.root = root.resolve()
        self.lessons = {lesson.lesson_id: lesson for lesson in curriculum.lessons}
        self.media = {m.asset_id: m for m in curriculum.media}
        self.availability: dict[str, str] = {}
        for m in curriculum.media:  # a file whose bytes do not match the catalog is never offered
            path = self.media_path(m.asset_id)
            ok = path is not None and path.is_file() and \
                hashlib.sha256(path.read_bytes()).hexdigest() == m.checksum_sha256
            self.availability[m.asset_id] = "available" if ok else "withdrawn"
        self._validate()

    def _validate(self) -> None:
        for lesson in self.c.lessons:
            if bool(lesson.quiz) == bool(lesson.scenario):
                raise ValueError(f"lesson {lesson.lesson_id} needs exactly one assessment (quiz or scenario)")
            if any(p not in self.lessons for p in lesson.prerequisites):
                raise ValueError(f"lesson {lesson.lesson_id} has an unknown prerequisite")
            if any(a not in self.media for a in lesson.media):
                raise ValueError(f"lesson {lesson.lesson_id} names unknown media")
            for q in lesson.quiz.questions if lesson.quiz else []:
                if q.answer not in {c.choice_id for c in q.choices}:
                    raise ValueError(f"question {q.question_id} answer is not a choice")
            if lesson.scenario:
                ids = {n.node_id for n in lesson.scenario.nodes} | {END_PASS, END_FAIL}
                if any(o.next not in ids for n in lesson.scenario.nodes for o in n.options):
                    raise ValueError(f"scenario {lesson.lesson_id} has a dangling branch")

    # ------------------------------------------------------------------ catalog, media and projections

    def media_path(self, asset_id: str, captions: bool = False) -> Path | None:
        """Only catalog assets inside the content root; never an arbitrary path."""
        m = self.media.get(asset_id)
        rel = (m.captions_path if captions else m.path) if m else None
        if not rel:
            return None
        path = (self.root / rel).resolve()
        return path if path.is_relative_to(self.root) else None

    def media_view(self, asset_id: str) -> s.LessonMediaAsset:
        m = self.media[asset_id]
        available = self.availability[asset_id] == "available"
        return s.LessonMediaAsset(
            asset_id=m.asset_id, version=m.version, kind=m.kind, mime_type=m.mime_type, title=m.title,
            duration_seconds=m.duration_seconds, availability=self.availability[asset_id],
            content_ref=f"/v1/content/{m.asset_id}/file" if available else None,
            captions_ref=f"/v1/content/{m.asset_id}/captions" if available and m.captions_path else None,
            checksum_sha256=m.checksum_sha256 if available else None, licence=m.licence, provenance=m.provenance,
            review_status=m.review_status, playback=m.playback)

    def lesson_view(self, lesson: Lesson) -> s.LessonView:
        return s.LessonView(
            lesson_id=lesson.lesson_id, version=lesson.version, title=lesson.title, summary=lesson.summary,
            level=lesson.level, intended_duration_seconds=lesson.intended_duration_seconds,
            machine_applicability=lesson.machine_applicability, prerequisites=lesson.prerequisites,
            review_status=self.c.review_status, curriculum_version=self.c.curriculum_version,
            steps=[step_view(lesson, i) for i in range(len(lesson.steps))],
            media=[self.media_view(a) for a in lesson.media],
            assessment=s.AssessmentSummary(
                kind=lesson.assessment_kind, quiz_id=(lesson.quiz or lesson.scenario).quiz_id,
                version=(lesson.quiz or lesson.scenario).version,
                question_count=len(lesson.quiz.questions) if lesson.quiz else None,
                pass_mark=(lesson.quiz or lesson.scenario).pass_mark))

    # ------------------------------------------------------------------ seeding

    def seed(self, store: Store) -> dict[str, int]:
        """Idempotent: lesson rows for new lessons, one pinned copy per lesson version (a changed copy under the
        same version is refused, never overwritten)."""
        added = 0
        with store._tx() as c:
            for lesson in self.c.lessons:
                c.execute("INSERT OR IGNORE INTO lessons(lesson_id, title, summary, duration_minutes) VALUES (?, ?, ?, ?)",
                          (lesson.lesson_id, lesson.title, lesson.summary,
                           max(1, math.ceil(lesson.intended_duration_seconds / 60))))
                content = lesson.model_dump_json()
                digest = hashlib.sha256(content.encode()).hexdigest()
                row = c.execute("SELECT content_sha256 FROM lesson_versions WHERE lesson_id = ? AND version = ?",
                                (lesson.lesson_id, lesson.version)).fetchone()
                if row is None:
                    c.execute("INSERT INTO lesson_versions(lesson_id, version, curriculum_version, content_sha256,"
                              " content_json, review_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (lesson.lesson_id, lesson.version, self.c.curriculum_version, digest, content,
                               self.c.review_status, iso(utcnow())))
                    added += 1
                elif row["content_sha256"] != digest:
                    raise ValueError(f"lesson {lesson.version} content changed without a new version")
        return {"lesson_versions_added": added}


def load_lms(path: Path | None = None) -> LMS:
    return LMS(Curriculum.model_validate(json.loads((path or DEFAULT_CURRICULUM).read_text(encoding="utf-8"))))


# ---------------------------------------------------------------------- pure views


def step_view(lesson: Lesson, index: int) -> s.LessonStepView:
    step = lesson.steps[index]
    return s.LessonStepView(step_id=step.step_id, kind=step.kind, index=index + 1, total=len(lesson.steps),
                            speak=step.speak, media_asset_ids=lesson.media if step.kind == "media" else [])


def question_view(lesson: Lesson, attempt: sqlite3.Row) -> s.QuizQuestionView | None:
    if attempt["status"] != "active":
        return None
    if lesson.scenario:
        node = lesson.scenario.node(attempt["current_node"])
        return s.QuizQuestionView(attempt_id=attempt["attempt_id"], lesson_id=lesson.lesson_id, kind="scenario",
                                  question_id=node.node_id, index=attempt["answered"] + 1, total=None,
                                  prompt=node.prompt, choices=[s.QuizChoice(choice_id=o.option_id, text=o.text)
                                                               for o in node.options])
    q = lesson.quiz.questions[attempt["current_index"]]
    return s.QuizQuestionView(attempt_id=attempt["attempt_id"], lesson_id=lesson.lesson_id, kind="quiz",
                              question_id=q.question_id, index=attempt["current_index"] + 1,
                              total=len(lesson.quiz.questions), prompt=q.prompt,
                              choices=[s.QuizChoice(choice_id=c.choice_id, text=c.text) for c in q.choices])


# ---------------------------------------------------------------------- mutations (run through Store.run_command)


def learner_id(session: s.Session) -> str:
    if session.binding_status != "catalog_verified":
        raise NotFound("lessons need a catalog-verified operator session")
    return session.operator_id


def _pinned(c: sqlite3.Connection, lesson_id: str, version: str) -> Lesson:
    row = c.execute("SELECT content_json FROM lesson_versions WHERE lesson_id = ? AND version = ?",
                    (lesson_id, version)).fetchone()
    if row is None:
        raise NotFound(f"lesson version {version} is not available")
    return Lesson.model_validate_json(row["content_json"])


def _progress(c: sqlite3.Connection, learner: str, lesson_id: str) -> sqlite3.Row | None:
    return c.execute("SELECT * FROM lesson_progress WHERE learner_id = ? AND lesson_id = ?"
                     " ORDER BY started_at DESC LIMIT 1", (learner, lesson_id)).fetchone()


def _completed(c: sqlite3.Connection, learner: str, lesson_id: str) -> bool:
    return c.execute("SELECT 1 FROM lesson_progress WHERE learner_id = ? AND lesson_id = ? AND status = 'completed'",
                     (learner, lesson_id)).fetchone() is not None


def _progress_view(c: sqlite3.Connection, lms: LMS, learner: str, lesson_id: str) -> s.LessonProgressView:
    lesson = lms.lessons[lesson_id]
    row = _progress(c, learner, lesson_id)
    attempts = c.execute("SELECT status, score FROM quiz_attempts WHERE learner_id = ? AND lesson_id = ?"
                         " ORDER BY attempt_number", (learner, lesson_id)).fetchall()
    scores = [a["score"] for a in attempts if a["score"] is not None]
    assignment = c.execute("SELECT assignment_id, source_episode_id, deferred_until FROM training_assignments"
                           " WHERE operator_id = ? AND lesson_id = ?", (learner, lesson_id)).fetchone()
    pinned = _pinned(c, lesson_id, row["lesson_version"]) if row else lesson
    return s.LessonProgressView(
        lesson_id=lesson_id, title=lesson.title, level=lesson.level,
        lesson_version=row["lesson_version"] if row else None,
        status=row["status"] if row else "not_started",
        current_step=row["current_step"] + 1 if row else None, total_steps=len(pinned.steps),
        steps_presented=row["steps_seen"] if row else 0, attempts=len(attempts),
        best_score=max(scores) if scores else None, last_attempt_status=attempts[-1]["status"] if attempts else None,
        assignment_id=assignment["assignment_id"] if assignment else None,
        assigned_for_episode_id=assignment["source_episode_id"] if assignment else None,
        deferred_until=parse_dt(row["deferred_until"]) if row and row["deferred_until"] else None)


def _result(event: str, lesson: Lesson | None, **extra: Any) -> dict[str, Any]:
    out = {"record_type": "learning", "record_id": extra.pop("record_id", lesson.lesson_id if lesson else None),
           "summary": extra.pop("summary"), "learning": {"type": "learning", "event": event,
                                                         "lesson_id": lesson.lesson_id if lesson else None,
                                                         "lesson_title": lesson.title if lesson else None}}
    out["learning"].update({k: (v.model_dump(mode="json") if isinstance(v, BaseModel) else v) for k, v in extra.items()})
    return out


def start_lesson(lms: LMS, session: s.Session, lesson_id: str, category: str | None):
    def mutate(c: sqlite3.Connection) -> dict[str, Any]:
        learner = learner_id(session)
        lesson = lms.lessons.get(lesson_id)
        if lesson is None:
            raise NotFound("no such lesson")
        if not lesson.applies_to(category):
            raise InvalidTransition("not_applicable", f"{lesson.title} does not apply to this machine")
        missing = [p for p in lesson.prerequisites if not _completed(c, learner, p)]
        if missing:
            raise InvalidTransition("prerequisites", f"complete {', '.join(missing)} first", missing)
        row = _progress(c, learner, lesson_id)
        now = iso(utcnow())
        if row is None:
            c.execute("INSERT INTO lesson_progress(learner_id, lesson_id, lesson_version, status, current_step,"
                      " steps_seen, started_at, updated_at, last_session_id) VALUES (?, ?, ?, 'in_progress', 0, 1, ?,"
                      " ?, ?)", (learner, lesson_id, lesson.version, now, now, session.session_id))
            row = _progress(c, learner, lesson_id)
            event = "lesson_started"
        elif row["status"] in ("paused", "deferred", "in_progress"):
            c.execute("UPDATE lesson_progress SET status = 'in_progress', deferred_until = NULL, updated_at = ?,"
                      " version = version + 1, last_session_id = ? WHERE learner_id = ? AND lesson_id = ? AND"
                      " lesson_version = ?", (now, session.session_id, learner, lesson_id, row["lesson_version"]))
            row = _progress(c, learner, lesson_id)
            event = "lesson_resumed"
        else:  # awaiting_assessment or completed: nothing to restart; say where it stands
            pinned = _pinned(c, lesson_id, row["lesson_version"])
            return _result("assessment_ready" if row["status"] == "awaiting_assessment" else "lesson_already_completed",
                           pinned, summary=f"{pinned.title}: {row['status']}.",
                           progress=_progress_view(c, lms, learner, lesson_id))
        pinned = _pinned(c, lesson_id, row["lesson_version"])
        return _result(event, pinned, summary=f"{event.replace('_', ' ').capitalize()}: {pinned.title}.",
                       step=step_view(pinned, row["current_step"]),
                       media=[lms.media_view(a) for a in pinned.media if pinned.steps[row["current_step"]].kind == "media"],
                       progress=_progress_view(c, lms, learner, lesson_id))
    return mutate


def lesson_step(lms: LMS, session: s.Session, lesson_id: str, action: str, expected_step: int | None = None,
                defer_until: datetime | None = None):
    """next / pause / resume / defer on the learner's current progress for this lesson."""
    def mutate(c: sqlite3.Connection) -> dict[str, Any]:
        learner = learner_id(session)
        if lesson_id not in lms.lessons:
            raise NotFound("no such lesson")
        row = _progress(c, learner, lesson_id)
        if row is None and action == "defer":  # an assigned lesson can be put off before it is ever started
            assigned = c.execute("SELECT 1 FROM training_assignments WHERE operator_id = ? AND lesson_id = ? AND"
                                 " status = 'assigned'", (learner, lesson_id)).fetchone()
            if assigned is None:
                raise NotFound("this lesson is neither started nor assigned")
            until = iso(defer_until or utcnow() + timedelta(minutes=30))
            c.execute("UPDATE training_assignments SET deferred_until = ? WHERE operator_id = ? AND lesson_id = ?",
                      (until, learner, lesson_id))
            return _result("lesson_deferred", lms.lessons[lesson_id], summary=f"Deferred {lesson_id}.",
                           deferred_until=until, progress=_progress_view(c, lms, learner, lesson_id))
        if row is None:
            raise NotFound("this lesson has not been started")
        pinned = _pinned(c, lesson_id, row["lesson_version"])
        key = (learner, lesson_id, row["lesson_version"])
        now = iso(utcnow())
        if row["status"] == "completed" and action != "defer":
            raise InvalidTransition("completed", f"{pinned.title} is already complete")
        if action == "pause":
            if row["status"] not in ("in_progress",):
                raise InvalidTransition(row["status"], f"{pinned.title} is {row['status']}")
            c.execute("UPDATE lesson_progress SET status = 'paused', updated_at = ?, version = version + 1"
                      " WHERE learner_id = ? AND lesson_id = ? AND lesson_version = ?", (now, *key))
            return _result("lesson_paused", pinned, summary=f"Paused {pinned.title}.",
                           progress=_progress_view(c, lms, learner, lesson_id))
        if action == "defer":
            until = iso(defer_until or utcnow() + timedelta(minutes=30))
            if row["status"] in ("in_progress", "paused"):
                c.execute("UPDATE lesson_progress SET status = 'deferred', deferred_until = ?, updated_at = ?,"
                          " version = version + 1 WHERE learner_id = ? AND lesson_id = ? AND lesson_version = ?",
                          (until, now, *key))
            c.execute("UPDATE training_assignments SET deferred_until = ? WHERE operator_id = ? AND lesson_id = ?",
                      (until, learner, lesson_id))
            return _result("lesson_deferred", pinned, summary=f"Deferred {pinned.title}.", deferred_until=until,
                           progress=_progress_view(c, lms, learner, lesson_id))
        if action == "resume":
            if row["status"] not in ("paused", "deferred"):
                raise InvalidTransition(row["status"], f"{pinned.title} is {row['status']}")
            c.execute("UPDATE lesson_progress SET status = 'in_progress', deferred_until = NULL, updated_at = ?,"
                      " version = version + 1 WHERE learner_id = ? AND lesson_id = ? AND lesson_version = ?",
                      (now, *key))
            return _result("lesson_resumed", pinned, summary=f"Resumed {pinned.title}.",
                           step=step_view(pinned, row["current_step"]),
                           progress=_progress_view(c, lms, learner, lesson_id))
        # next
        if row["status"] == "awaiting_assessment":
            return _result("assessment_ready", pinned, summary=f"{pinned.title}: assessment ready.",
                           progress=_progress_view(c, lms, learner, lesson_id))
        if row["status"] in ("paused", "deferred"):  # "continue" after a pause resumes where it stopped
            c.execute("UPDATE lesson_progress SET status = 'in_progress', deferred_until = NULL WHERE learner_id = ?"
                      " AND lesson_id = ? AND lesson_version = ?", key)
        if expected_step is not None and expected_step != row["current_step"] + 1:
            raise VersionConflict(row["current_step"] + 1)
        nxt = row["current_step"] + 1
        if nxt >= len(pinned.steps):
            c.execute("UPDATE lesson_progress SET status = 'awaiting_assessment', steps_seen = ?, updated_at = ?,"
                      " version = version + 1 WHERE learner_id = ? AND lesson_id = ? AND lesson_version = ?",
                      (len(pinned.steps), now, *key))
            return _result("assessment_ready", pinned, summary=f"Finished the steps of {pinned.title}.",
                           progress=_progress_view(c, lms, learner, lesson_id))
        c.execute("UPDATE lesson_progress SET status = 'in_progress', current_step = ?, steps_seen = MAX(steps_seen, ?),"
                  " updated_at = ?, version = version + 1 WHERE learner_id = ? AND lesson_id = ? AND lesson_version = ?",
                  (nxt, nxt + 1, now, *key))
        return _result("lesson_step", pinned, summary=f"{pinned.title} step {nxt + 1}.", step=step_view(pinned, nxt),
                       media=[lms.media_view(a) for a in pinned.media if pinned.steps[nxt].kind == "media"],
                       progress=_progress_view(c, lms, learner, lesson_id))
    return mutate


def start_quiz(lms: LMS, session: s.Session, lesson_id: str):
    def mutate(c: sqlite3.Connection) -> dict[str, Any]:
        learner = learner_id(session)
        if lesson_id not in lms.lessons:
            raise NotFound("no such lesson")
        row = _progress(c, learner, lesson_id)
        if row is None or row["status"] not in ("awaiting_assessment", "completed"):
            raise InvalidTransition(row["status"] if row else "not_started",
                                    "finish the lesson steps before the assessment", ["lesson_steps"])
        pinned = _pinned(c, lesson_id, row["lesson_version"])
        active = c.execute("SELECT * FROM quiz_attempts WHERE learner_id = ? AND lesson_id = ? AND status = 'active'",
                           (learner, lesson_id)).fetchone()
        if active is None:
            n = c.execute("SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM quiz_attempts WHERE learner_id = ? AND"
                          " lesson_id = ? AND lesson_version = ?", (learner, lesson_id, row["lesson_version"])).fetchone()[0]
            assess = pinned.quiz or pinned.scenario
            attempt_id = "ATT-" + uuid.uuid4().hex[:12]
            c.execute("INSERT INTO quiz_attempts(attempt_id, learner_id, lesson_id, lesson_version, quiz_id, quiz_version,"
                      " kind, attempt_number, status, current_index, current_node, correct, answered, total, started_at,"
                      " session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, 0, 0, ?, ?, ?)",
                      (attempt_id, learner, lesson_id, row["lesson_version"], assess.quiz_id, assess.version,
                       pinned.assessment_kind, n, pinned.scenario.start if pinned.scenario else None,
                       len(pinned.quiz.questions) if pinned.quiz else None, iso(utcnow()), session.session_id))
            active = c.execute("SELECT * FROM quiz_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            event = "assessment_started"
        else:
            event = "assessment_resumed"
        return _result(event, pinned, summary=f"{event.replace('_', ' ').capitalize()}: {pinned.title}.",
                       record_id=active["attempt_id"], question=question_view(pinned, active),
                       attempt_number=active["attempt_number"])
    return mutate


def answer(lms: LMS, session: s.Session, attempt_id: str, question_id: str, choice_id: str, source: str):
    """Score one answer server-side; the last one finishes the attempt (pass completes the lesson, fail never does)."""
    def mutate(c: sqlite3.Connection) -> dict[str, Any]:
        learner = learner_id(session)
        att = c.execute("SELECT * FROM quiz_attempts WHERE attempt_id = ? AND learner_id = ?",
                        (attempt_id, learner)).fetchone()
        if att is None:
            raise NotFound("no such attempt for this operator")
        pinned = _pinned(c, att["lesson_id"], att["lesson_version"])
        if att["status"] != "active":
            raise InvalidTransition(att["status"], f"this attempt is already {att['status']}")
        current = question_view(pinned, att)
        if question_id != current.question_id:
            raise VersionConflict(current.index)
        now = iso(utcnow())
        if pinned.scenario:
            option = next((o for o in pinned.scenario.node(question_id).options if o.option_id == choice_id), None)
            if option is None:
                raise InvalidInput("payload.choice_id", "not one of the options")
            correct, remediation, feedback = option.safe, None if option.safe else option.feedback, option.feedback
            correct_choice = next(o.option_id for o in pinned.scenario.node(question_id).options if o.safe)
            finished = option.next in (END_PASS, END_FAIL)
            c.execute("UPDATE quiz_attempts SET answered = answered + 1, correct = correct + ?, current_node = ?,"
                      " version = version + 1 WHERE attempt_id = ?", (int(correct), option.next, attempt_id))
            passed = option.next == END_PASS
        else:
            q = pinned.quiz.questions[att["current_index"]]
            if choice_id not in {ch.choice_id for ch in q.choices}:
                raise InvalidInput("payload.choice_id", "not one of the choices")
            correct, correct_choice = choice_id == q.answer, q.answer
            remediation, feedback = (None if correct else q.remediation), None
            finished = att["current_index"] + 1 >= len(pinned.quiz.questions)
            c.execute("UPDATE quiz_attempts SET answered = answered + 1, correct = correct + ?, current_index = ?,"
                      " version = version + 1 WHERE attempt_id = ?",
                      (int(correct), min(att["current_index"] + 1, len(pinned.quiz.questions) - 1), attempt_id))
            passed = None
        c.execute("INSERT INTO quiz_answers(attempt_id, question_id, choice_id, correct, answered_at, source)"
                  " VALUES (?, ?, ?, ?, ?, ?)", (attempt_id, question_id, choice_id, int(correct), now, source))
        fb = s.AnswerFeedback(question_id=question_id, choice_id=choice_id, correct=correct,
                              correct_choice_id=correct_choice, remediation=remediation, feedback=feedback)
        att = c.execute("SELECT * FROM quiz_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if not finished:
            return _result("answer_recorded", pinned, record_id=attempt_id, summary="Answer recorded.",
                           feedback=fb, question=question_view(pinned, att))
        return _finish(c, lms, session, learner, pinned, att, fb, passed)
    return mutate


def _finish(c, lms: LMS, session: s.Session, learner: str, pinned: Lesson, att: sqlite3.Row,
            fb: s.AnswerFeedback, scenario_passed: bool | None) -> dict[str, Any]:
    now = iso(utcnow())
    if pinned.scenario:
        score = att["correct"] / max(att["answered"], 1)
        passed = bool(scenario_passed)
        total = att["answered"]
    else:
        total = len(pinned.quiz.questions)
        score = att["correct"] / total
        passed = score >= pinned.quiz.pass_mark - 1e-9
    status = "passed" if passed else "failed"
    c.execute("UPDATE quiz_attempts SET status = ?, score = ?, total = ?, finished_at = ?, version = version + 1"
              " WHERE attempt_id = ?", (status, round(score, 3), total, now, att["attempt_id"]))
    wrong = [r["question_id"] for r in c.execute("SELECT question_id FROM quiz_answers WHERE attempt_id = ? AND"
                                                 " correct = 0", (att["attempt_id"],))]
    remediation = []
    if pinned.quiz:
        remediation = [q.remediation for q in pinned.quiz.questions if q.question_id in wrong]
    elif not passed:
        remediation = [fb.feedback] if fb.feedback else []
    level_change = None
    completed_now = False
    if passed:
        prog = c.execute("SELECT status FROM lesson_progress WHERE learner_id = ? AND lesson_id = ? AND lesson_version = ?",
                         (learner, pinned.lesson_id, att["lesson_version"])).fetchone()
        if prog is not None and prog["status"] != "completed":
            c.execute("UPDATE lesson_progress SET status = 'completed', completed_at = ?, updated_at = ?,"
                      " version = version + 1 WHERE learner_id = ? AND lesson_id = ? AND lesson_version = ?",
                      (now, now, learner, pinned.lesson_id, att["lesson_version"]))
            c.execute("UPDATE training_assignments SET status = 'completed', completed_at = ? WHERE operator_id = ?"
                      " AND lesson_id = ? AND session_id IN (SELECT session_id FROM sessions WHERE binding_status ="
                      " 'catalog_verified')", (now, learner, pinned.lesson_id))
            completed_now = True
        level_change = evaluate_level(c, lms, learner)
    result = s.AttemptResult(
        attempt_id=att["attempt_id"], lesson_id=pinned.lesson_id, lesson_version=att["lesson_version"],
        attempt_number=att["attempt_number"], kind=pinned.assessment_kind, status=status, correct=att["correct"],
        total=total, score=round(score, 3), pass_mark=(pinned.quiz or pinned.scenario).pass_mark,
        lesson_completed=completed_now, remediation=remediation)
    return _result("assessment_finished", pinned, record_id=att["attempt_id"],
                   summary=f"{pinned.title} assessment {status} ({att['correct']}/{total}).", feedback=fb,
                   result=result, level_change=level_change,
                   progress=_progress_view(c, lms, learner, pinned.lesson_id))


def evaluate_level(c: sqlite3.Connection, lms: LMS, learner: str) -> dict[str, Any] | None:
    """Highest level whose versioned criteria hold on saved evidence; a rise is persisted with that evidence."""
    completed = {r["lesson_id"]: r for r in c.execute(
        "SELECT lesson_id, lesson_version, completed_at FROM lesson_progress WHERE learner_id = ? AND status ="
        " 'completed'", (learner,))}
    best: dict[str, tuple[float, str]] = {}
    for r in c.execute("SELECT lesson_id, attempt_id, score, kind FROM quiz_attempts WHERE learner_id = ? AND"
                       " status = 'passed'", (learner,)):
        if r["lesson_id"] not in best or r["score"] > best[r["lesson_id"]][0]:
            best[r["lesson_id"]] = (r["score"], r["attempt_id"])
    levels = lms.c.levels
    reached = "beginner"
    evidence: dict[str, Any] = {}
    for crit in levels.criteria:
        needed = crit.completed_lessons + crit.passed_scenarios
        if any(lid not in completed for lid in needed):
            break
        scores = [best[lid][0] for lid in crit.completed_lessons if lid in best]
        avg = sum(scores) / len(scores) if scores else 1.0
        if avg + 1e-9 < crit.min_average_best_score:
            break
        reached = crit.level
        evidence = {"criteria_version": levels.criteria_version, "average_best_score": round(avg, 3),
                    "lessons": {lid: {"version": completed[lid]["lesson_version"], "best_attempt": best.get(lid, (None, None))[1],
                                      "best_score": best.get(lid, (None, None))[0]} for lid in needed}}
    row = c.execute("SELECT level FROM learner_levels WHERE learner_id = ?", (learner,)).fetchone()
    previous = row["level"] if row else "beginner"
    if levels.order.index(reached) <= levels.order.index(previous):
        if row is None:
            c.execute("INSERT INTO learner_levels(learner_id, level, criteria_version, updated_at) VALUES (?, ?, ?, ?)",
                      (learner, previous, levels.criteria_version, iso(utcnow())))
        return None
    now = iso(utcnow())
    c.execute("INSERT INTO learner_levels(learner_id, level, criteria_version, updated_at) VALUES (?, ?, ?, ?)"
              " ON CONFLICT(learner_id) DO UPDATE SET level = excluded.level, criteria_version ="
              " excluded.criteria_version, updated_at = excluded.updated_at",
              (learner, reached, levels.criteria_version, now))
    c.execute("INSERT INTO level_history(entry_id, learner_id, level, previous_level, criteria_version, evidence_json,"
              " achieved_at) VALUES (?, ?, ?, ?, ?, ?, ?)", ("LVL-" + uuid.uuid4().hex[:12], learner, reached, previous,
                                                          levels.criteria_version, json.dumps(evidence), now))
    return {"from": previous, "to": reached, "criteria_version": levels.criteria_version, "evidence": evidence}


# ---------------------------------------------------------------------- read models


def learner_view(store: Store, lms: LMS, session: s.Session, dataset_skill: str | None) -> s.LearnerView | None:
    if session.binding_status != "catalog_verified":
        return None
    learner = session.operator_id
    with store._lock:
        c = store._conn
        level = c.execute("SELECT level, criteria_version FROM learner_levels WHERE learner_id = ?", (learner,)).fetchone()
        history = c.execute("SELECT level, previous_level, criteria_version, evidence_json, achieved_at FROM"
                            " level_history WHERE learner_id = ? ORDER BY achieved_at", (learner,)).fetchall()
        lessons = [_progress_view(c, lms, learner, lid) for lid in lms.lessons]
        active = c.execute("SELECT * FROM quiz_attempts WHERE learner_id = ? AND status = 'active' ORDER BY started_at"
                           " DESC LIMIT 1", (learner,)).fetchone()
        question = question_view(_pinned(c, active["lesson_id"], active["lesson_version"]), active) if active else None
    done = {p.lesson_id for p in lessons if p.status == "completed"}
    recommended = [p.lesson_id for p in lessons if p.status != "completed" and p.assignment_id] + [
        lid for lid, lesson in lms.lessons.items()
        if lid not in done and all(q in done for q in lesson.prerequisites)
        and not any(p.lesson_id == lid and p.assignment_id for p in lessons)]
    return s.LearnerView(
        learner_id=learner, level=level["level"] if level else "beginner",
        criteria_version=level["criteria_version"] if level else lms.c.levels.criteria_version,
        level_note=lms.c.levels.note, dataset_operator_skill=dataset_skill,
        level_history=[s.LevelTransition(level=h["level"], previous_level=h["previous_level"],
                                         criteria_version=h["criteria_version"], evidence=json.loads(h["evidence_json"]),
                                         achieved_at=parse_dt(h["achieved_at"])) for h in history],
        lessons=lessons, active_question=question, recommended=list(dict.fromkeys(recommended))[:3],
        curriculum_version=lms.c.curriculum_version)


def active_learning(store: Store, session: s.Session) -> dict[str, Any] | None:
    """What the router needs to interpret "next", "pause" or an answer (no answer keys)."""
    if session.binding_status != "catalog_verified":
        return None
    row = store._one("SELECT lesson_id, status FROM lesson_progress WHERE learner_id = ? AND status IN"
                     " ('in_progress', 'paused', 'deferred', 'awaiting_assessment') ORDER BY updated_at DESC LIMIT 1",
                     (session.operator_id,))
    if row:
        return {"lesson_id": row["lesson_id"], "status": row["status"]}
    offered = store._one("SELECT lesson_id FROM training_assignments WHERE operator_id = ? AND status = 'assigned' AND"
                         " coaching_prompted_at IS NOT NULL ORDER BY coaching_prompted_at DESC LIMIT 1",
                         (session.operator_id,))  # a lesson just offered by a coaching prompt: "later" defers it
    return {"lesson_id": offered["lesson_id"], "status": "offered"} if offered else None


def coaching_prompts(c: sqlite3.Connection, session: s.Session, req: s.TelemetryRequest) -> list[tuple[str, str]]:
    """Episode-linked lessons are offered only while the machine is off or idle and no warning is active (urgent
    alerts first), once per assignment, after any deferral. Returns (assignment_id, speech) to announce."""
    r = req.readings
    if r.engine_on and r.operating_state not in ("off", "idle"):
        return []
    if c.execute("SELECT 1 FROM alerts WHERE session_id = ? AND status = 'active'", (session.session_id,)).fetchone():
        return []
    rows = c.execute(
        "SELECT ta.assignment_id, l.title, l.duration_minutes FROM training_assignments ta JOIN lessons l USING"
        " (lesson_id) JOIN sessions s ON s.session_id = ta.session_id WHERE ta.operator_id = ? AND ta.status ="
        " 'assigned' AND ta.source_episode_id IS NOT NULL AND ta.coaching_prompted_at IS NULL AND"
        " s.binding_status = ? AND (ta.deferred_until IS NULL OR ta.deferred_until <= ?) ORDER BY ta.assigned_at"
        " LIMIT 1", (session.operator_id, session.binding_status, iso(req.observed_at))).fetchall()
    return [(r_["assignment_id"],
             f"When you're parked safely: you have a short lesson waiting, {r_['title']}, about "
             f"{r_['duration_minutes']} minute{'s' if r_['duration_minutes'] != 1 else ''}. Say start my lesson when "
             "you're ready, or later to put it off.") for r_ in rows]
