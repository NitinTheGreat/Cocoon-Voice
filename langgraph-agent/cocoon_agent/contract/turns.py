"""Proposed turn contract: streaming turn requests, SSE events, public action results, cancel and delivery.

Target stage I08 (streaming) and I04 (action results). Plan section 6.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, RootModel, model_validator

from ..api import schemas as s
from .common import ProposedErrorBody, StableId, Strict, UtcTime

TURN_STREAM_SCHEMA = "cocoon.turn-stream.v1"


# --------------------------------------------------------------------------- request


class ClientContext(Strict):
    """Allowlisted, optional hints from the voice/app client. Never overrides the trusted session binding."""

    wake_phrase_removed: bool | None = None
    push_to_talk: bool | None = None
    stt_final_confidence: float | None = Field(default=None, ge=0, le=1)


class TurnRequestTarget(s.TurnRequest):
    """Body of the proposed POST .../turns/stream and the additive target of POST .../turns.

    The three v1 fields are unchanged; everything added is optional. One finalized, wake-approved utterance:
    no raw audio, transcript history, credentials or identity claims. Canonical idempotency identity is
    (session_id, turn_id); X-Request-ID is tracing only. Semantic fields (text, source, language,
    client_context, supersedes_turn_id) are part of the request fingerprint."""

    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$",
                                 description="Optional BCP-47 tag. A tag alone does not prove multilingual support.")
    client_context: ClientContext | None = None
    supersedes_turn_id: StableId | None = Field(
        default=None, description="An earlier turn in the same session. Its run gets a persisted cancellation "
                                  "intent; committed actions are never rolled back.")

    @model_validator(mode="after")
    def _not_self(self) -> "TurnRequestTarget":
        if self.supersedes_turn_id is not None and self.supersedes_turn_id == self.turn_id:
            raise ValueError("supersedes_turn_id must identify an earlier turn, not this one")
        return self


# --------------------------------------------------------------------------- public action results

ActionName = Literal[
    "get_next_task", "get_daily_tasks", "start_task", "complete_task", "explain_task_estimate",
    "log_incident", "create_incident_draft", "confirm_incident", "request_clarification",
    "assign_lesson", "get_training_status", "submit_quiz_answer",
    "explain_alert", "acknowledge_alert", "record_idle_reason",
    "request_supervisor_approval", "respond_to_check_in", "cancel_pending", "answer_general_question",
]
"""Finite allowlist of public action names. `apply_schedule` and `notify_supervisor` are deliberately absent:
they execute only through an approved workflow record, never as a turn action."""


class TaskActionDetails(Strict):
    kind: Literal["task"]
    task_id: StableId
    task_version: int = Field(ge=1)
    lifecycle: Literal["scheduled", "in_progress", "paused", "completed", "cancelled", "blocked"]


class IncidentActionDetails(Strict):
    kind: Literal["incident"]
    incident_id: StableId
    state: Literal["draft", "confirmed", "dismissed"]
    created: bool = Field(description="False when an earlier attempt of the same identity already saved it.")


class ClarificationDetails(Strict):
    kind: Literal["clarification"]
    for_action: ActionName
    missing_fields: list[Annotated[str, Field(max_length=64)]] = Field(min_length=1)
    context_ref_id: StableId | None = None


class LessonAssignmentDetails(Strict):
    kind: Literal["lesson_assignment"]
    assignment_id: StableId
    lesson_id: StableId
    lesson_version: int = Field(ge=1)
    created: bool


class TrainingStatusDetails(Strict):
    kind: Literal["training_status"]
    active_assignment_ids: list[StableId]


class QuizAnswerDetails(Strict):
    kind: Literal["quiz_answer"]
    attempt_id: StableId
    interpretation: Literal["correct", "incorrect", "ambiguous"]
    passed: bool

    @model_validator(mode="after")
    def _ambiguous_never_passes(self) -> "QuizAnswerDetails":
        if self.passed and self.interpretation != "correct":
            raise ValueError("only a correct interpretation can pass")
        return self


class AlertExplanationDetails(Strict):
    kind: Literal["alert_explanation"]
    alert_id: StableId
    episode_id: StableId
    rule_id: str = Field(max_length=128)
    rule_version: str = Field(max_length=32)


class AcknowledgementDetails(Strict):
    kind: Literal["acknowledgement"]
    acknowledgement_id: StableId
    alert_id: StableId


class IdleReasonDetails(Strict):
    kind: Literal["idle_reason"]
    idle_reason_id: StableId
    reason: Literal["waiting_for_truck", "waiting_for_instruction", "other"]
    alert_id: StableId | None = Field(
        default=None, description="Linked alert. Recording a reason never clears the alert or its condition.")


class ApprovalRequestDetails(Strict):
    kind: Literal["approval_request"]
    approval_id: StableId
    approval_status: Literal["pending"] = Field(
        description="A turn can only create a request. Approval, rejection and application are later workflow "
                    "states read from GET /v1/approvals/{approval_id}.")
    requested_action: Literal["apply_schedule_change", "escalate_repeat_violation", "notify_supervisor"]


class CheckInResponseDetails(Strict):
    kind: Literal["check_in_response"]
    episode_id: StableId
    check_in_id: StableId
    response: Literal["ok", "need_help"]


class NoDetails(Strict):
    kind: Literal["none"]


ActionDetails = Annotated[
    Union[
        TaskActionDetails, IncidentActionDetails, ClarificationDetails, LessonAssignmentDetails,
        TrainingStatusDetails, QuizAnswerDetails, AlertExplanationDetails, AcknowledgementDetails,
        IdleReasonDetails, ApprovalRequestDetails, CheckInResponseDetails, NoDetails,
    ],
    Field(discriminator="kind"),
]

_DETAILS_FOR_ACTION: dict[str, set[str]] = {
    "get_next_task": {"task", "none"}, "get_daily_tasks": {"none"}, "start_task": {"task"},
    "complete_task": {"task"}, "explain_task_estimate": {"task"},
    "log_incident": {"incident"}, "create_incident_draft": {"incident"}, "confirm_incident": {"incident"},
    "request_clarification": {"clarification"},
    "assign_lesson": {"lesson_assignment"}, "get_training_status": {"training_status"},
    "submit_quiz_answer": {"quiz_answer"},
    "explain_alert": {"alert_explanation", "none"}, "acknowledge_alert": {"acknowledgement"},
    "record_idle_reason": {"idle_reason"},
    "request_supervisor_approval": {"approval_request"}, "respond_to_check_in": {"check_in_response"},
    "cancel_pending": {"none"}, "answer_general_question": {"none"},
}
_RECORD_ACTIONS = {
    "start_task", "complete_task", "log_incident", "create_incident_draft", "confirm_incident", "assign_lesson",
    "submit_quiz_answer", "acknowledge_alert", "record_idle_reason", "request_supervisor_approval",
    "respond_to_check_in",
}


class PublicActionResult(Strict):
    """Public outcome of one action. `completed` means the record committed; `unknown` means the outcome could not
    be confirmed (the client must say so, not claim success or failure). Internal states such as planned or
    waiting_approval never appear here."""

    action_id: StableId
    action_name: ActionName
    status: Literal["completed", "failed", "unknown"]
    record_id: StableId | None = Field(default=None, description="The real stored record ID, when one exists.")
    summary: str = Field(max_length=300, description="Safe, speakable-grade summary. No private evidence.")
    details: ActionDetails

    @model_validator(mode="after")
    def _consistent(self) -> "PublicActionResult":
        failed_without_record = self.status != "completed" and self.details.kind == "none"
        if not failed_without_record and self.details.kind not in _DETAILS_FOR_ACTION[self.action_name]:
            raise ValueError(f"details kind {self.details.kind!r} does not belong to action {self.action_name!r}")
        if self.status == "completed" and self.action_name in _RECORD_ACTIONS and not self.record_id:
            raise ValueError("a completed record-producing action must carry its real record_id")
        if self.status != "completed" and self.details.kind == "approval_request":
            raise ValueError("approval_request details describe a created request; use status completed")
        return self


# --------------------------------------------------------------------------- SSE events


class TurnAcceptedData(Strict):
    status: Literal["processing"]
    run_id: StableId = Field(description="Durable execution identity; identical retries attach to this run.")


class TurnProgressData(Strict):
    stage: Literal["queued", "routing", "tool_running", "responding"]
    tool_name: ActionName | None = Field(default=None, description="Allowlisted public name only; no arguments.")


class SpeechDeltaData(Strict):
    text: str = Field(min_length=1, max_length=2000,
                      description="Append-only approved speech. Whitespace and punctuation preserved exactly.")


class TurnCompletedData(Strict):
    status: Literal["completed"]
    speech: str = Field(description="Full saved speech; equals the concatenation of this turn's deltas.")
    actions: list[PublicActionResult]
    state_version: int = Field(ge=0)


CancelReason = Literal["barge_in", "stop", "session_closed", "timeout"]


class TurnCancelledData(Strict):
    status: Literal["cancelled"]
    reason: CancelReason
    generated_speech: str = Field(
        description="Speech generated before cancellation (concatenated deltas). Not evidence it was heard.")
    actions: list[PublicActionResult] = Field(description="Known outcomes, including actions committed before "
                                                          "cancellation. Cancellation never rolls them back.")
    state_version: int = Field(ge=0)


class TurnFailedData(Strict):
    status: Literal["failed"]
    error: ProposedErrorBody
    actions: list[PublicActionResult] = Field(description="Known and unknown action outcomes.")
    state_version: int | None = Field(default=None, ge=0)


class _TurnEventBase(Strict):
    schema_version: Literal["cocoon.turn-stream.v1"]
    session_id: StableId
    turn_id: StableId
    response_id: StableId = Field(description="Fixed for the run across retries and replay.")
    event_id: str = Field(max_length=160, description="Equals '<turn_id>:<sequence>' and the SSE id field.")
    sequence: int = Field(ge=1, description="Strictly increasing per turn. Heartbeats have no sequence.")
    created_at: UtcTime

    @model_validator(mode="after")
    def _event_id_matches(self):
        if self.event_id != f"{self.turn_id}:{self.sequence}":
            raise ValueError("event_id must equal '<turn_id>:<sequence>'")
        return self


class TurnAcceptedEvent(_TurnEventBase):
    type: Literal["turn.accepted"]
    data: TurnAcceptedData


class TurnProgressEvent(_TurnEventBase):
    type: Literal["turn.progress"]
    data: TurnProgressData


class SpeechDeltaEvent(_TurnEventBase):
    type: Literal["speech.delta"]
    data: SpeechDeltaData


class TurnCompletedEvent(_TurnEventBase):
    type: Literal["turn.completed"]
    data: TurnCompletedData


class TurnCancelledEvent(_TurnEventBase):
    type: Literal["turn.cancelled"]
    data: TurnCancelledData


class TurnFailedEvent(_TurnEventBase):
    type: Literal["turn.failed"]
    data: TurnFailedData


TurnStreamEventUnion = Annotated[
    Union[TurnAcceptedEvent, TurnProgressEvent, SpeechDeltaEvent, TurnCompletedEvent, TurnCancelledEvent,
          TurnFailedEvent],
    Field(discriminator="type"),
]


class TurnStreamEvent(RootModel[TurnStreamEventUnion]):
    """One `cocoon.turn-stream.v1` envelope. SSE `event` equals `type`; SSE `id` equals `event_id`."""


TERMINAL_TURN_EVENTS = frozenset({"turn.completed", "turn.cancelled", "turn.failed"})


# --------------------------------------------------------------------------- JSON turn status (target)


class TurnResultTarget(s.TurnResult):
    """Additive target of the existing JSON turn result and GET .../turns/{turn_id}.

    v1 fields keep their meaning (`actions` stays the v1 `type`-discriminated list). `cancelled` appears only for
    runs cancelled through the new cancel route, which v1-only clients never call."""

    status: Literal["processing", "completed", "cancelled", "failed"]
    response_id: StableId | None = None
    action_results: list[PublicActionResult] = Field(
        default_factory=list, description="Target public action results (action_id/action_name/status).")
    generated_speech: str | None = Field(default=None, description="Partial text of a cancelled/failed run.")
    last_sequence: int | None = Field(default=None, ge=0, description="Highest persisted stream sequence.")
    replay_available_until: UtcTime | None = None


# --------------------------------------------------------------------------- cancellation


class CancelRequest(Strict):
    cancel_id: StableId
    reason: CancelReason


class CancelResult(Strict):
    """202 while cancellation is pending; 200 once the run is terminal (whichever terminal event won the race)."""

    cancel_id: StableId
    session_id: StableId
    turn_id: StableId
    outcome: Literal["cancellation_pending", "cancelled", "already_completed", "already_failed",
                     "already_cancelled"]
    turn_status: Literal["processing", "completed", "cancelled", "failed"]
    recorded_at: UtcTime

    @model_validator(mode="after")
    def _consistent(self) -> "CancelResult":
        expected = {"cancellation_pending": "processing", "cancelled": "cancelled",
                    "already_completed": "completed", "already_failed": "failed",
                    "already_cancelled": "cancelled"}[self.outcome]
        if self.turn_status != expected:
            raise ValueError(f"outcome {self.outcome} requires turn_status {expected}")
        return self


# --------------------------------------------------------------------------- response playback


PlaybackStatus = Literal["played", "interrupted", "failed", "expired"]
PositionConfidence = Literal["exact", "estimated", "unknown"]


class TurnDeliveryReport(Strict):
    """Playback of a turn response, reported separately from generation and from actions."""

    delivery_id: StableId = Field(description="Identity of one playback attempt. Same ID + same body is a safe "
                                              "retry; same ID + different body is 409.")
    consumer_id: StableId
    response_id: StableId
    status: PlaybackStatus
    played_text: str | None = Field(default=None, max_length=4000)
    position_confidence: PositionConfidence

    @model_validator(mode="after")
    def _position(self) -> "TurnDeliveryReport":
        if self.position_confidence == "exact" and self.played_text is None:
            raise ValueError("exact position requires played_text (validated against the generated prefix)")
        if self.position_confidence == "unknown" and self.played_text is not None:
            raise ValueError("unknown position cannot assert played_text; send null")
        return self


class TurnDeliveryRecord(Strict):
    delivery_id: StableId
    consumer_id: StableId
    response_id: StableId
    status: PlaybackStatus
    played_text: str | None
    position_confidence: PositionConfidence
    recorded_at: UtcTime
    aggregate_status: PlaybackStatus = Field(
        description="Best observed outcome for this consumer/response. A delayed older failure never downgrades "
                    "an earlier successful playback.")
