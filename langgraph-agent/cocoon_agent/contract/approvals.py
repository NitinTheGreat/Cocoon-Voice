"""Proposed approval and weather re-planning contract. Target stage I13 (with I09 forecasts, I10 episodes).

Approval and execution are recorded separately: an approved proposal whose application failed is never reported
as applied.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .common import PageInfo, Sha256, SiteId, StableId, Strict, UtcTime


class ScheduleChange(Strict):
    task_id: StableId
    before_order: int = Field(ge=1)
    after_order: int = Field(ge=1)
    before_start_at: UtcTime | None = None
    after_start_at: UtcTime | None = None
    reason: str = Field(max_length=300, description="Which forecast value or constraint motivated this change.")


class ScheduleProposalPayload(Strict):
    kind: Literal["schedule_change"]
    shift_id: StableId
    base_schedule_version: int = Field(ge=1)
    forecast_conditions_id: StableId
    forecast_issued_at: UtcTime
    changes: list[ScheduleChange] = Field(min_length=1)


class EscalationPayload(Strict):
    kind: Literal["repeat_violation_escalation"]
    operator_id: StableId
    rule_id: str = Field(max_length=128)
    rule_version: str = Field(max_length=32)
    window_start: UtcTime
    window_end: UtcTime
    episode_ids: list[StableId] = Field(min_length=2, description="Distinct episodes, not repeated samples.")


ProposalPayload = Annotated[Union[ScheduleProposalPayload, EscalationPayload], Field(discriminator="kind")]


class ApprovalDecision(Strict):
    decision_id: StableId
    decision: Literal["approve", "reject"]
    decided_by: StableId
    decided_at: UtcTime
    reason: str | None = Field(default=None, max_length=300)


class ApplicationOutcome(Strict):
    status: Literal["not_started", "applied", "failed_stale_inputs", "failed", "not_applicable"]
    attempted_at: UtcTime | None = None
    applied_schedule_version: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _applied(self) -> "ApplicationOutcome":
        if self.status == "applied" and (self.attempted_at is None or self.applied_schedule_version is None):
            raise ValueError("an applied outcome needs attempted_at and applied_schedule_version")
        return self


class ApprovalRecord(Strict):
    approval_id: StableId
    version: int = Field(ge=1)
    action_type: Literal["apply_schedule_change", "escalate_repeat_violation", "notify_supervisor"]
    status: Literal["pending", "approved", "rejected", "expired", "cancelled"]
    site_id: SiteId
    proposed_by: StableId
    approver_role: Literal["supervisor"] = "supervisor"
    created_at: UtcTime
    expires_at: UtcTime
    payload: ProposalPayload
    payload_sha256: Sha256 = Field(description="Hash of the canonical payload; a decision must quote it.")
    decision: ApprovalDecision | None = None
    application: ApplicationOutcome

    @model_validator(mode="after")
    def _transitions(self) -> "ApprovalRecord":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.status == "pending" and (self.decision is not None or self.application.status != "not_started"):
            raise ValueError("a pending proposal has no decision and nothing applied")
        expected = {"approved": "approve", "rejected": "reject"}.get(self.status)
        if expected is not None and (self.decision is None or self.decision.decision != expected):
            raise ValueError("status must match the recorded decision")
        if expected is None and self.decision is not None:
            raise ValueError("only approved/rejected proposals carry a decision")
        if self.application.status == "applied" and self.status != "approved":
            raise ValueError("only an approved proposal can be applied")
        if self.status in ("rejected", "expired", "cancelled") and self.application.status not in (
            "not_started", "not_applicable"
        ):
            raise ValueError("a rejected/expired/cancelled proposal is never applied")
        return self


class ApprovalPage(Strict):
    approvals: list[ApprovalRecord]
    page: PageInfo


class DecisionRequest(Strict):
    """POST /v1/approvals/{approval_id}/decision. Scoped supervisor principal only."""

    decision_id: StableId
    decision: Literal["approve", "reject"]
    expected_version: int = Field(ge=1)
    payload_sha256: Sha256
    reason: str | None = Field(default=None, max_length=300)


class DecisionResult(Strict):
    approval: ApprovalRecord
    decision_recorded: bool = Field(description="False for an identical retry of an already recorded decision.")
    execution_status: Literal["not_started", "applied", "failed_stale_inputs", "failed", "not_applicable"] = Field(
        description="Reported separately: approval is not execution.")
