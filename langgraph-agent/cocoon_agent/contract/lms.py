"""Proposed LMS contract. Target stages I12A (content), I12B (lessons, quizzes, levels), I12C (assignments).

A lesson is never completed by playback alone. Learning level is an educational result, not a licence and not
the dataset's operator_skill.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import OperatorId, Provenance, Sha256, StableId, Strict, UtcTime

LearningLevel = Literal["beginner", "intermediate", "expert"]


class ContentAsset(Strict):
    """GET /v1/content/{asset_id}. Approved text/media metadata; not an arbitrary URL fetcher."""

    asset_id: StableId
    version: int = Field(ge=1)
    kind: Literal["text", "video", "image"]
    mime_type: str = Field(max_length=64, examples=["text/markdown", "video/mp4"])
    title: str = Field(max_length=200)
    duration_seconds: float | None = Field(default=None, gt=0)
    availability: Literal["available", "not_yet_supplied", "withdrawn"] = Field(
        description="`not_yet_supplied` marks a planned asset with no real file. Clients must not play it.")
    content_ref: str | None = Field(default=None, max_length=300,
                                    description="Server-relative asset path or permitted HTTPS URL.")
    checksum_sha256: Sha256 | None = None
    licence: str | None = Field(default=None, max_length=100)
    provenance: Provenance

    @model_validator(mode="after")
    def _available(self) -> "ContentAsset":
        if self.availability == "available" and not (self.content_ref and self.checksum_sha256 and self.licence):
            raise ValueError("an available asset needs content_ref, checksum_sha256 and licence")
        if self.availability != "available" and self.content_ref is not None:
            raise ValueError("an unavailable asset exposes no content_ref")
        if self.kind == "video" and self.availability == "available" and self.duration_seconds is None:
            raise ValueError("an available video needs duration_seconds")
        return self


class GuidedStep(Strict):
    step_id: StableId
    kind: Literal["text", "video", "quiz", "practice_scenario"]
    asset_id: StableId | None = None
    quiz_id: StableId | None = None
    spoken_prompt: str | None = Field(default=None, max_length=400)

    @model_validator(mode="after")
    def _refs(self) -> "GuidedStep":
        if self.kind == "quiz" and self.quiz_id is None:
            raise ValueError("a quiz step needs quiz_id")
        if self.kind in ("text", "video") and self.asset_id is None:
            raise ValueError("a text/video step needs asset_id")
        return self


class LessonVersion(Strict):
    lesson_id: StableId
    version: int = Field(ge=1)
    title: str = Field(max_length=200)
    level: LearningLevel
    duration_seconds: int = Field(ge=10, le=7200)
    machine_models: list[str] = Field(default_factory=list)
    prerequisite_lesson_ids: list[StableId] = Field(default_factory=list)
    steps: list[GuidedStep] = Field(min_length=1)
    completion_policy: Literal["assessment_pass_required"] = "assessment_pass_required"
    provenance: Provenance


class CourseModule(Strict):
    module_id: StableId
    title: str = Field(max_length=200)
    lesson_ids: list[StableId] = Field(min_length=1)


class Course(Strict):
    course_id: StableId
    version: int = Field(ge=1)
    title: str = Field(max_length=200)
    level: LearningLevel
    modules: list[CourseModule] = Field(min_length=1)


class LevelEvidence(Strict):
    level: LearningLevel
    policy_id: str = Field(max_length=64)
    passed_attempt_ids: list[StableId] = Field(min_length=1)
    achieved_at: UtcTime


class LearnerProfile(Strict):
    operator_id: OperatorId
    learning_level: LearningLevel
    level_evidence: list[LevelEvidence] = Field(default_factory=list)
    self_reported_experience_years: float | None = Field(default=None, ge=0)
    dataset_operator_skill: LearningLevel | None = Field(
        default=None, description="Copied from the dataset for context only; never used as learning level.")
    preferred_language: str | None = Field(default=None, max_length=16)
    version: int = Field(ge=1)


class LessonAssignment(Strict):
    assignment_id: StableId
    operator_id: OperatorId
    lesson_id: StableId
    lesson_version: int = Field(ge=1)
    origin: Literal["operator_request", "behaviour_policy", "supervisor"]
    policy_id: str | None = Field(default=None, max_length=64)
    source_episode_id: StableId | None = None
    status: Literal["assigned", "in_progress", "deferred", "completed", "superseded"]
    assigned_at: UtcTime
    deferred_until: UtcTime | None = None

    @model_validator(mode="after")
    def _origin(self) -> "LessonAssignment":
        if self.origin == "behaviour_policy" and (self.policy_id is None or self.source_episode_id is None):
            raise ValueError("a behaviour-triggered assignment links its policy and episode")
        return self


class QuizAttempt(Strict):
    attempt_id: StableId
    assignment_id: StableId
    quiz_id: StableId
    question_id: StableId
    answer_text: str = Field(max_length=500)
    interpretation: Literal["correct", "incorrect", "ambiguous"]
    score: float = Field(ge=0, le=1)
    passed: bool
    remediation_step_id: StableId | None = None
    answered_at: UtcTime

    @model_validator(mode="after")
    def _pass(self) -> "QuizAttempt":
        if self.passed and self.interpretation != "correct":
            raise ValueError("a wrong or ambiguous answer is never a pass")
        if not self.passed and self.remediation_step_id is None:
            raise ValueError("a failed attempt points to its remediation step")
        return self


class LessonProgress(Strict):
    assignment_id: StableId
    lesson_id: StableId
    lesson_version: int = Field(ge=1)
    current_step_id: StableId | None
    completed_step_ids: list[StableId]
    playback_completed: bool = Field(description="All media steps played. Never sufficient for completion.")
    assessment_passed: bool
    completion_status: Literal["not_started", "in_progress", "awaiting_assessment", "completed"]
    version: int = Field(ge=1)
    updated_at: UtcTime

    @model_validator(mode="after")
    def _completion(self) -> "LessonProgress":
        if self.completion_status == "completed" and not self.assessment_passed:
            raise ValueError("completion requires a passed assessment; playback alone does not complete a lesson")
        return self
