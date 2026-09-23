"""BACKEND-INTERNAL target structures (I04). Not part of any public payload.

The classifier decision and action plan are validated in Python before any tool runs. They never reach speech,
SSE `speech.delta`, logs beyond safe IDs/stages, or client responses. Public outcomes use PublicActionResult.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import StableId, Strict

Branch = Literal["tasks", "safety_incidents", "training", "general_assistance"]

Intent = Literal[
    # tasks
    "get_next_task", "get_daily_tasks", "start_task", "complete_task", "explain_task_estimate",
    # safety / incidents
    "log_incident", "confirm_incident_draft", "explain_alert", "acknowledge_alert", "record_idle_reason",
    "respond_to_check_in", "request_supervisor_escalation",
    # training
    "assign_lesson", "get_training_status", "continue_lesson", "answer_quiz", "defer_lesson",
    # general
    "general_question", "cancel_pending", "smalltalk",
]

INTENT_BRANCH: dict[str, str] = {
    **dict.fromkeys(["get_next_task", "get_daily_tasks", "start_task", "complete_task", "explain_task_estimate"],
                    "tasks"),
    **dict.fromkeys(["log_incident", "confirm_incident_draft", "explain_alert", "acknowledge_alert",
                     "record_idle_reason", "respond_to_check_in", "request_supervisor_escalation"],
                    "safety_incidents"),
    **dict.fromkeys(["assign_lesson", "get_training_status", "continue_lesson", "answer_quiz", "defer_lesson"],
                    "training"),
    **dict.fromkeys(["general_question", "cancel_pending", "smalltalk"], "general_assistance"),
}


class ContextRef(Strict):
    """The active workflow a short reply ("yes", "why?", a quiz answer) resolves against."""

    kind: Literal["alert", "incident_draft", "pending_question", "quiz", "check_in", "approval", "task"]
    ref_id: StableId


class ClassifiedEntities(Strict):
    """Allowlisted, typed entities. Anything else the model produces is dropped, not executed."""

    task_id: StableId | None = None
    lesson_id: StableId | None = None
    alert_id: StableId | None = None
    incident_what: str | None = Field(default=None, max_length=1000)
    incident_severity: Literal["low", "medium", "high", "critical"] | None = None
    quiz_answer: str | None = Field(default=None, max_length=500)
    idle_reason: Literal["waiting_for_truck", "waiting_for_instruction", "other"] | None = None
    check_in_response: Literal["ok", "need_help"] | None = None


class ClassifiedIntent(Strict):
    intent: Intent
    entities: ClassifiedEntities
    depends_on: list[int] = Field(default_factory=list, description="Indexes of earlier intents in this decision.")


class ClassifierDecision(Strict):
    branch: Branch = Field(description="Branch of the first intent; routing starts here.")
    intents: list[ClassifiedIntent] = Field(min_length=1, max_length=4)
    missing_fields: list[str] = Field(default_factory=list)
    routing_certainty: Literal["high", "medium", "low"]
    context_ref: ContextRef | None = None
    needs_clarification: bool

    @model_validator(mode="after")
    def _consistent(self) -> "ClassifierDecision":
        if INTENT_BRANCH[self.intents[0].intent] != self.branch:
            raise ValueError("branch must be the branch of the first intent")
        for i, item in enumerate(self.intents):
            if any(d >= i or d < 0 for d in item.depends_on):
                raise ValueError("depends_on may only reference earlier intents")
        if self.routing_certainty == "low" and not self.needs_clarification:
            raise ValueError("low routing certainty must ask for clarification instead of acting")
        if self.missing_fields and not self.needs_clarification:
            raise ValueError("missing fields require clarification")
        return self


class PlannedAction(Strict):
    action_id: StableId
    action_name: str = Field(max_length=64, description="Must be in the public ActionName allowlist or "
                                                        "an internal workflow step name.")
    depends_on: list[StableId] = Field(default_factory=list)
    state: Literal["planned", "waiting_dependency", "waiting_approval", "running", "committed", "failed",
                   "unknown", "skipped"]


class ActionPlan(Strict):
    plan_id: StableId
    session_id: StableId
    turn_id: StableId
    actions: list[PlannedAction] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _dag(self) -> "ActionPlan":
        seen: set[str] = set()
        for action in self.actions:
            if any(d not in seen for d in action.depends_on):
                raise ValueError("dependencies must reference earlier actions in the plan")
            if action.action_id in seen:
                raise ValueError("action IDs are unique within a plan")
            seen.add(action.action_id)
        return self
