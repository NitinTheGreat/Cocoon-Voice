"""Public v1 HTTP contract for the Cocoon backend.

These Pydantic models are the source of truth for `contracts/openapi.yaml`.
Change them only in backwards-compatible ways (see API_CONTRACT.md), then
regenerate the committed spec with `python scripts/export_openapi.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

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
    "unknown_machine",
    "unknown_operator",
    "catalog_unavailable",
    "forbidden",
    "auth_unavailable",
    "version_conflict",
    "invalid_transition",
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
    machine_id: str = Field(
        min_length=1, max_length=128,
        description="Exact catalog asset ID for a NEW session (unknown → 422 unknown_machine). An existing "
                    "client_session_key is resolved against its stored association first.")
    site_id: str | None = Field(
        default=None, pattern=ID_PATTERN,
        description="Optional. Accepted only together with shift_id and only if it matches a server-configured "
                    "trusted binding; otherwise 422. Never authoritative on its own.")
    shift_id: str | None = Field(default=None, pattern=ID_PATTERN,
                                 description="Optional; see site_id.")


BindingStatus = Literal["catalog_verified", "legacy_unverified"]
ContextStatus = Literal["unavailable", "trusted_binding", "legacy_unverified"]


class Session(ContractModel):
    session_id: str
    client_session_key: str
    room_name: str
    participant_identity: str
    operator_id: str
    machine_id: str
    state_version: int
    created_at: datetime
    dataset_manifest_sha256: str | None = Field(
        default=None, description="Verified catalog snapshot this session was admitted under. Null for sessions "
                                  "created before catalog binding existed (never attached retroactively).")
    binding_status: BindingStatus = Field(
        default="legacy_unverified",
        description="catalog_verified: IDs checked against dataset_manifest_sha256 at creation. legacy_unverified: "
                    "pre-upgrade session; its IDs were never checked.")
    site_id: str | None = None
    shift_id: str | None = None
    context_status: ContextStatus = Field(
        default="legacy_unverified",
        description="unavailable: no trusted site/shift is known (not a default site). trusted_binding: site/shift "
                    "matched a server-configured binding. legacy_unverified: pre-upgrade session.")
    context_source: str | None = Field(default=None, description="Binding record that established site/shift.")


# --------------------------------------------------------------------------- domain records


class Task(ContractModel):
    task_id: str
    title: str
    details: str
    priority: Literal["low", "normal", "high"]
    status: Literal["pending", "in_progress", "done"]


class TaskConditions(ContractModel):
    source: Literal["synthetic_demo_fixture"] = Field(
        description="Where the conditions come from. Only a synthetic fixture exists until live weather (Batch C).")
    summary: str | None = None
    temperature_c: float | None = None


class TaskDuration(ContractModel):
    minutes: int | None = None
    source: Literal["demo_supplied_estimate"] = Field(
        description="A supplied demo figure, not a calibrated prediction (the estimator is Batch C).")


class AssignedTask(ContractModel):
    """A task assigned to the session's operator/machine for its trusted shift. Versioned for command concurrency."""

    task_id: str
    shift_id: str
    machine_id: str
    site_zone_id: str
    zone_name: str
    scheduled_order: int
    scheduled_start_at: datetime
    scheduled_start_local: str = Field(description="HH:MM at the site (site UTC offset).")
    task_type: str
    title: str
    details: str
    work_quantity: float | None = None
    work_unit: str | None = None
    status: Literal["scheduled", "in_progress", "completed"]
    version: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    weather: TaskConditions
    duration: TaskDuration


class ShiftInfo(ContractModel):
    shift_id: str
    site_id: str
    site_name: str
    timezone: str
    service_date: str
    start_at: datetime
    end_at: datetime
    utc_offset: str = Field(pattern=r"^[+-]\d{2}:\d{2}$", description="Site offset used for the service date.")
    source: Literal["synthetic_demo_fixture"]

    def local_date(self, when: datetime) -> str:
        sign = 1 if self.utc_offset[0] == "+" else -1
        hours, minutes = int(self.utc_offset[1:3]), int(self.utc_offset[4:6])
        return (when.astimezone(timezone.utc) + sign * timedelta(hours=hours, minutes=minutes)).date().isoformat()


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


class AssignedTasksAction(ContractModel):
    """Read of the shift's assigned tasks (next task or full list)."""

    type: Literal["assigned_tasks"]
    scope: Literal["next", "all"]
    tasks: list[AssignedTask]
    shift_bound: bool = Field(description="False when the session has no trusted shift; tasks is then empty.")


class TaskTransitionAction(ContractModel):
    type: Literal["task_started", "task_completed"]
    task: AssignedTask
    command_id: str
    created: bool = Field(description="False when this exact command was already committed (retry).")


class TaskRejectedAction(ContractModel):
    """A task command that changed nothing, with the reason (no shift, no eligible task, illegal transition)."""

    type: Literal["task_rejected"]
    for_action: Literal["task.start", "task.complete"]
    reason: Literal["no_shift", "no_eligible_task", "invalid_transition"]
    current_status: str | None = None


ActionResult = Annotated[
    Union[
        NextTaskAction,
        IncidentLoggedAction,
        InformationRequestedAction,
        TrainingAssignedAction,
        TrainingStatusAction,
        AlertExplainedAction,
        PendingCancelledAction,
        AssignedTasksAction,
        TaskTransitionAction,
        TaskRejectedAction,
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
    shift: ShiftInfo | None = Field(
        default=None, description="The session's trusted shift (synthetic demo fixture), or null when unbound.")
    assigned_tasks: list[AssignedTask] = Field(
        default_factory=list, description="Tasks of the trusted shift. `tasks` stays the legacy shared demo list.")


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


# --------------------------------------------------------------------------- current principal


class SessionAssociation(ContractModel):
    """An association the caller actually holds. For an operator: one of their own catalog_verified sessions,
    created by the trusted service. Catalog membership alone is never listed as an association."""

    operator_id: str
    machine_id: str
    site_id: str | None = None
    shift_id: str | None = None
    session_id: str | None = Field(default=None, description="The owned session this association comes from.")


class MeResponse(ContractModel):
    """GET /v1/me. Never contains a bearer token, its digest or service configuration."""

    subject_id: str = Field(description="`service` for the service credential; otherwise the principal_id.")
    principal_kind: Literal["service", "operator", "supervisor"]
    operator_id: str | None = Field(default=None, description="Catalog operator ID of an operator principal.")
    display_name: str | None = None
    site_ids: list[str] = Field(description="Granted sites. Always empty in I02b: no site grants exist yet.")
    allowed_associations: list[SessionAssociation] = Field(
        description="Operator: own catalog_verified sessions. Service and supervisor: empty (the service is trusted "
                    "for all sessions it manages; supervisors have no session access in I02b).")
    scopes: list[str] = Field(default_factory=list, description="Actor token scopes; empty for the service.")
    token_id: str | None = Field(default=None, description="Non-secret record ID of the presented actor token.")
    token_expires_at: datetime | None = Field(default=None, description="Null for the service credential.")


# --------------------------------------------------------------------------- health


class HealthResponse(ContractModel):
    status: Literal["ok"]


class ReadyResponse(ContractModel):
    status: Literal["ready", "not_ready"]
    llm_mode: Literal["live", "mock"]
    database: bool
    checkpointer: bool
    version: str
    catalog: bool = Field(default=False, description="Verified machine/operator catalog loaded; needed to admit "
                                                     "new sessions. Existing sessions keep working without it.")
    catalog_version: str | None = Field(default=None, description="Manifest SHA-256 of the loaded catalog.")
    catalog_issue: str | None = Field(default=None, description="Sanitized reason code when catalog is false.")
    schema_version: int | None = Field(default=None, description="Applied cocoon.db migration version.")


# --------------------------------------------------------------------------- commands (taps and graph tools)

CommandKind = Literal["task.start", "task.complete"]


class CommandPayload(ContractModel):
    task_id: StableId | None = None


class SessionCommand(ContractModel):
    """POST /v1/sessions/{session_id}/commands. A subset of the proposed cocoon.command.v1 envelope: the binding is
    taken from the authenticated session (never from the body); `expected_version` guards against stale taps."""

    schema_version: Literal["cocoon.command.v1"] = "cocoon.command.v1"
    command_id: StableId = Field(description="Unique per caller; an identical retry returns the saved result.")
    kind: CommandKind
    captured_at: AwareDatetime | None = Field(default=None, description="Device time of the tap (informational).")
    expected_version: int | None = Field(default=None, ge=1)
    payload: CommandPayload

    @model_validator(mode="after")
    def _payload_for_kind(self) -> "SessionCommand":
        if self.kind.startswith("task.") and not self.payload.task_id:
            raise ValueError("task commands need payload.task_id")
        return self


class SessionCommandResult(ContractModel):
    command_id: str
    kind: str
    status: Literal["completed"]
    duplicate: bool = Field(description="True when this identical command was already committed.")
    record_type: str | None = None
    record_id: str | None = None
    summary: str
    state_version: int
    task: AssignedTask | None = None
    created_at: datetime
