"""Proposed command inbox, presence, presentation and state-snapshot contract.

Target stages: I02 (command identity/ledger), I07A/I06/I12/I14 (the services each command calls), I15 (offline
sync, presence, presentation). Voice, tap and offline sync all reach the same domain services through these
commands; there is no generic function executor.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, RootModel, model_validator

from ..api import schemas as s
from .common import (
    MachineId, OperatorId, ProposedErrorBody, ShiftId, SiteId, StableId, Strict, UtcTime,
)
from .incidents import IncidentLocation, IncidentReport, ReportedSeverity, ReportedText, ReportedTime
from .lms import LearnerProfile, LessonAssignment, LessonProgress
from .rules import OperatorAlertView, OperatorWellbeingView
from .sos import SosEpisode, SosResponsePayload
from .tasks import DailyTaskDashboard

COMMAND_SCHEMA = "cocoon.command.v1"


class CommandBinding(Strict):
    """The association captured on the device when the command was created. It must match server-authorised
    context; it grants no authority. A delayed upload keeps this original binding even if a newer session exists."""

    operator_id: OperatorId
    machine_id: MachineId
    shift_id: ShiftId
    site_id: SiteId
    device_id: StableId
    session_id: StableId = Field(description="Session the command was captured in (may no longer be active).")


class TaskCommandPayload(Strict):
    task_id: StableId


class IncidentDraftPayload(Strict):
    what: ReportedText | None = None
    where: IncidentLocation | None = None
    when: ReportedTime | None = None
    severity: ReportedSeverity | None = None
    episode_id: StableId | None = None


class IncidentConfirmPayload(Strict):
    incident_id: StableId
    edits: IncidentDraftPayload | None = Field(default=None, description="Operator edits applied before confirming.")


class IncidentDismissPayload(Strict):
    incident_id: StableId
    reason: str | None = Field(default=None, max_length=300)


class AlertAcknowledgePayload(Strict):
    alert_id: StableId


class IdleReasonPayload(Strict):
    alert_id: StableId | None = None
    reason: Literal["waiting_for_truck", "waiting_for_instruction", "other"]


class QuizAnswerPayload(Strict):
    assignment_id: StableId
    quiz_id: StableId
    question_id: StableId
    answer_text: str = Field(min_length=1, max_length=500)


class StepProgressPayload(Strict):
    assignment_id: StableId
    step_id: StableId
    playback: Literal["played", "skipped"] = Field(description="Records playback only; never completes a lesson.")


class DeferAssignmentPayload(Strict):
    assignment_id: StableId
    defer_until: UtcTime


class _CommandBase(Strict):
    schema_version: Literal["cocoon.command.v1"]
    command_id: StableId = Field(description="Dedup key, unique per issuing subject across sessions/reconnects.")
    captured_at: UtcTime = Field(description="Device time when the operator acted.")
    client_draft_id: StableId | None = None
    binding: CommandBinding
    expected_version: int | None = Field(default=None, ge=1,
                                         description="Version of the target record the device last saw.")


class TaskStartCommand(_CommandBase):
    kind: Literal["task.start"]
    payload: TaskCommandPayload


class TaskCompleteCommand(_CommandBase):
    kind: Literal["task.complete"]
    payload: TaskCommandPayload


class IncidentSubmitDraftCommand(_CommandBase):
    kind: Literal["incident.submit_draft"]
    payload: IncidentDraftPayload


class IncidentConfirmCommand(_CommandBase):
    kind: Literal["incident.confirm"]
    payload: IncidentConfirmPayload


class IncidentDismissCommand(_CommandBase):
    kind: Literal["incident.dismiss"]
    payload: IncidentDismissPayload


class AlertAcknowledgeCommand(_CommandBase):
    kind: Literal["alert.acknowledge"]
    payload: AlertAcknowledgePayload


class IdleReasonCommand(_CommandBase):
    kind: Literal["idle.record_reason"]
    payload: IdleReasonPayload


class SosRespondCommand(_CommandBase):
    kind: Literal["sos.respond"]
    payload: SosResponsePayload


class QuizAnswerCommand(_CommandBase):
    kind: Literal["lms.answer_quiz"]
    payload: QuizAnswerPayload


class StepProgressCommand(_CommandBase):
    kind: Literal["lms.record_step"]
    payload: StepProgressPayload


class DeferAssignmentCommand(_CommandBase):
    kind: Literal["lms.defer_assignment"]
    payload: DeferAssignmentPayload


CommandUnion = Annotated[
    Union[TaskStartCommand, TaskCompleteCommand, IncidentSubmitDraftCommand, IncidentConfirmCommand,
          IncidentDismissCommand, AlertAcknowledgeCommand, IdleReasonCommand, SosRespondCommand,
          QuizAnswerCommand, StepProgressCommand, DeferAssignmentCommand],
    Field(discriminator="kind"),
]


class VersionConflict(Strict):
    expected_version: int
    current_version: int
    reason: Literal["stale_version", "task_state_changed", "binding_mismatch"]


class CommandResult(Strict):
    """Authoritative command outcome. Accepted or queued never means completed. GET never re-executes."""

    command_id: StableId
    kind: str = Field(max_length=32)
    status: Literal["queued", "completed", "failed", "conflict", "rejected"]
    duplicate: bool = Field(description="True when this identical command was already received.")
    record_ids: list[StableId] = Field(default_factory=list, description="Real stored record IDs, when completed.")
    conflict: VersionConflict | None = None
    error: ProposedErrorBody | None = None
    original_binding: CommandBinding
    received_at: UtcTime
    completed_at: UtcTime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "CommandResult":
        if (self.status == "conflict") != (self.conflict is not None):
            raise ValueError("conflict details are present exactly for status conflict")
        if self.status == "completed" and self.completed_at is None:
            raise ValueError("a completed command needs completed_at")
        if self.status != "completed" and self.record_ids:
            raise ValueError("only a completed command reports record IDs")
        if self.status in ("failed", "rejected") and self.error is None:
            raise ValueError("failed/rejected commands carry a sanitized error")
        return self


# --------------------------------------------------------------------------- presence and presentation


class PresenceReport(Strict):
    """Last-known connectivity of one consumer. Not evidence that the operator is conscious or listening."""

    report_id: StableId
    consumer_id: StableId
    sequence: int = Field(ge=1, description="Per consumer; older sequences are ignored.")
    connection: Literal["online", "degraded", "going_offline"]
    voice_available: bool
    screen_available: bool
    reported_at: UtcTime
    ttl_seconds: int = Field(ge=5, le=120)


class PresenceRecord(Strict):
    consumer_id: StableId
    connection: Literal["online", "degraded", "going_offline", "expired"]
    voice_available: bool
    screen_available: bool
    last_contact_at: UtcTime
    expires_at: UtcTime


class PresentationReport(Strict):
    """Screen/vibration presentation of an announcement. Not audio playback and not acknowledgement."""

    presentation_id: StableId
    consumer_id: StableId
    channel: Literal["screen", "vibration"]
    status: Literal["presented", "failed", "unknown"]
    presented_at: UtcTime | None = None

    @model_validator(mode="after")
    def _time(self) -> "PresentationReport":
        if self.status == "presented" and self.presented_at is None:
            raise ValueError("presented needs presented_at")
        return self


class PresentationRecord(Strict):
    presentation_id: StableId
    event_id: StableId
    consumer_id: StableId
    channel: Literal["screen", "vibration"]
    status: Literal["presented", "failed", "unknown"]
    presented_at: UtcTime | None
    recorded_at: UtcTime
    evidence_limit: Literal["client_reported"] = "client_reported"


# --------------------------------------------------------------------------- state snapshot


class FreshnessSummary(Strict):
    telemetry_last_observed_at: UtcTime | None = None
    telemetry: Literal["fresh", "stale", "unknown"]
    conditions: Literal["fresh", "stale", "unknown"]


class ApprovalStatusView(Strict):
    """What the operator sees about approvals that concern them (never the approver's private notes)."""

    approval_id: StableId
    action_type: Literal["apply_schedule_change", "escalate_repeat_violation", "notify_supervisor"]
    status: Literal["pending", "approved", "rejected", "expired", "cancelled"]
    application_status: Literal["not_started", "applied", "failed_stale_inputs", "failed", "not_applicable"]


class SessionStateTarget(s.SessionState):
    """Additive target of GET .../state for the bound operator (voice service or operator principal).

    Every v1 field is preserved. The supervisor NEVER reads this model; it uses SupervisorOverview."""

    server_time: UtcTime | None = None
    data_time: UtcTime | None = None
    clock_mode: Literal["live", "replay"] | None = None
    freshness: FreshnessSummary | None = None
    dashboard: DailyTaskDashboard | None = None
    incident_reports: list[IncidentReport] = Field(default_factory=list, max_length=50)
    operator_alerts: list[OperatorAlertView] = Field(default_factory=list, max_length=50)
    learner: LearnerProfile | None = None
    lesson_assignments: list[LessonAssignment] = Field(default_factory=list, max_length=50)
    lesson_progress: list[LessonProgress] = Field(default_factory=list, max_length=50)
    approvals: list[ApprovalStatusView] = Field(default_factory=list, max_length=50)
    sos: SosEpisode | None = None
    wellbeing: OperatorWellbeingView | None = None


class Command(RootModel[CommandUnion]):
    """One `cocoon.command.v1` envelope, discriminated by `kind`."""
