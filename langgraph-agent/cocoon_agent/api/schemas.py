"""Public v1 HTTP contract for the Cocoon backend.

These Pydantic models are the source of truth for `contracts/openapi.yaml`.
Change them only in backwards-compatible ways (see API_CONTRACT.md), then
regenerate the committed spec with `python scripts/export_openapi.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$"

StableId = Annotated[str, Field(pattern=ID_PATTERN, examples=["lk-item_4f2c9a"])]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- errors

ErrorCode = Literal[
    "unauthorized",
    "not_found",
    "validation_error",
    "idempotency_conflict",
    "session_conflict",
    "llm_unavailable",
    "turn_failed",
    "internal_error",
]


class ErrorDetail(ContractModel):
    field: str
    issue: str


class ErrorBody(ContractModel):
    code: ErrorCode
    message: str
    retryable: bool
    request_id: str
    details: list[ErrorDetail] | None = None


class ErrorResponse(ContractModel):
    error: ErrorBody


# --------------------------------------------------------------------------- sessions


class SessionCreateRequest(ContractModel):
    client_session_key: str = Field(min_length=1, max_length=256)
    room_name: str = Field(min_length=1, max_length=256)
    participant_identity: str = Field(min_length=1, max_length=256)
    operator_id: str = Field(min_length=1, max_length=128)
    machine_id: str = Field(min_length=1, max_length=128)


class Session(ContractModel):
    session_id: str
    client_session_key: str
    room_name: str
    participant_identity: str
    operator_id: str
    machine_id: str
    state_version: int
    created_at: datetime


# --------------------------------------------------------------------------- domain records


class Task(ContractModel):
    task_id: str
    title: str
    details: str
    priority: Literal["low", "normal", "high"]
    status: Literal["pending", "in_progress", "done"]


class Incident(ContractModel):
    incident_id: str
    incident_number: int
    session_id: str
    operator_id: str
    machine_id: str
    description: str
    source_turn_id: str
    created_at: datetime


class Lesson(ContractModel):
    lesson_id: str
    title: str
    summary: str
    duration_minutes: int


class TrainingAssignment(ContractModel):
    assignment_id: str
    lesson_id: str
    lesson_title: str
    operator_id: str
    status: Literal["assigned", "completed"]
    assigned_at: datetime


class TelemetryReadings(ContractModel):
    engine_on: bool
    seatbelt_fastened: bool
    idle_seconds: int = Field(ge=0, le=86_400)


class Alert(ContractModel):
    alert_id: str
    rule_id: str
    alert_type: Literal["seatbelt_unfastened"]
    severity: Literal["warning", "critical"]
    status: Literal["active", "cleared"]
    message: str
    explanation: str
    simulated: bool
    trigger_readings: TelemetryReadings
    started_at: datetime
    cleared_at: datetime | None = None


class PendingQuestion(ContractModel):
    kind: Literal["incident_description"]
    for_action: Literal["log_incident"]
    asked_in_turn_id: str


# --------------------------------------------------------------------------- turns


class TurnRequest(ContractModel):
    turn_id: StableId
    text: str = Field(min_length=1, max_length=2000)
    source: Literal["voice", "text"]

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must contain non-whitespace characters")
        return value


class NextTaskAction(ContractModel):
    type: Literal["next_task"]
    task: Task | None


class IncidentLoggedAction(ContractModel):
    type: Literal["incident_logged"]
    incident: Incident
    created: bool = Field(description="False when an earlier attempt of the same turn already saved it.")


class InformationRequestedAction(ContractModel):
    type: Literal["information_requested"]
    for_action: Literal["log_incident"]
    missing_field: Literal["description"]


class TrainingAssignedAction(ContractModel):
    type: Literal["training_assigned"]
    assignment: TrainingAssignment
    created: bool = Field(description="False when this lesson was already assigned to the operator.")


class TrainingStatusAction(ContractModel):
    type: Literal["training_status"]
    assignments: list[TrainingAssignment]
    available_lessons: list[Lesson]


class AlertExplainedAction(ContractModel):
    type: Literal["alert_explained"]
    alert: Alert | None


class PendingCancelledAction(ContractModel):
    type: Literal["pending_cancelled"]
    cancelled: Literal["log_incident"] | None


ActionResult = Annotated[
    Union[
        NextTaskAction,
        IncidentLoggedAction,
        InformationRequestedAction,
        TrainingAssignedAction,
        TrainingStatusAction,
        AlertExplainedAction,
        PendingCancelledAction,
    ],
    Field(discriminator="type"),
]


class TurnResult(ContractModel):
    session_id: str
    turn_id: str
    status: Literal["processing", "completed", "failed"]
    speech: str | None = Field(
        default=None, description="Operator-facing text for TTS. Present only when status is completed."
    )
    actions: list[ActionResult] = Field(default_factory=list)
    state_version: int | None = None
    llm_mode: Literal["live", "mock"] | None = None
    error: ErrorBody | None = None
    retry_after_ms: int | None = Field(
        default=None, description="Present when status is processing: wait this long, then GET poll_url."
    )
    poll_url: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


# --------------------------------------------------------------------------- state


class SessionState(ContractModel):
    session_id: str
    state_version: int
    llm_mode: Literal["live", "mock"]
    tasks: list[Task]
    incidents: list[Incident]
    training_assignments: list[TrainingAssignment]
    available_lessons: list[Lesson]
    active_alerts: list[Alert]
    latest_alert: Alert | None
    pending_question: PendingQuestion | None


# --------------------------------------------------------------------------- telemetry


class TelemetryRequest(ContractModel):
    event_id: StableId
    observed_at: AwareDatetime = Field(description="Timezone-aware timestamp; normalised to UTC.")
    simulated: Literal[True] = Field(description="Must be true: v1 accepts prototype simulator data only.")
    readings: TelemetryReadings

    @field_validator("observed_at")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class TelemetryResult(ContractModel):
    session_id: str
    event_id: str
    duplicate: bool
    stale: bool = Field(description="True when observed_at is older than the newest processed sample.")
    alerts_opened: list[str]
    alerts_cleared: list[str]
    announcements_created: list[str]
    active_alerts: list[Alert]
    state_version: int


# --------------------------------------------------------------------------- announcements


DeliveryStatus = Literal["played", "interrupted", "failed", "expired"]


class DeliveryReport(ContractModel):
    consumer_id: StableId
    status: DeliveryStatus
    detail: str | None = Field(default=None, max_length=500)


class DeliveryRecord(ContractModel):
    event_id: str
    consumer_id: str
    status: DeliveryStatus
    detail: str | None = None
    recorded_at: datetime


class Announcement(ContractModel):
    event_id: str
    sequence: int
    type: Literal["alert_started", "alert_cleared"]
    priority: Literal["low", "normal", "high", "critical"]
    speech: str
    alert_id: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    deliveries: list[DeliveryRecord] = Field(
        default_factory=list, description="Latest playback report per consumer. Played is not acknowledgement."
    )


class EventsPage(ContractModel):
    session_id: str
    events: list[Announcement]
    next_cursor: int = Field(description="Pass as ?after= on the next poll.")
    has_more: bool


# --------------------------------------------------------------------------- health


class HealthResponse(ContractModel):
    status: Literal["ok"]


class ReadyResponse(ContractModel):
    status: Literal["ready", "not_ready"]
    llm_mode: Literal["live", "mock"]
    database: bool
    checkpointer: bool
    version: str
