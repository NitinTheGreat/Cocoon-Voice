"""Proposed supervisor projections and change feed (`cocoon.supervisor-feed.v1`). Target stage I13.

These are separate models, not an operator payload with fields hidden in the UI. Every model is strict, so an
attempt to add raw vitals, sleep, trigger evidence or private operator speech fails validation.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .approvals import ApprovalRecord
from .common import MachineId, OperatorId, PageInfo, SiteId, StableId, Strict, UtcTime, ZoneId
from .rules import AlertCategory

SUPERVISOR_FEED_SCHEMA = "cocoon.supervisor-feed.v1"


class SupervisorAlertView(Strict):
    view: Literal["alert"]
    alert_id: StableId
    episode_id: StableId
    rule_id: str = Field(max_length=128)
    rule_version: str = Field(max_length=32)
    category: AlertCategory
    severity: Literal["info", "warning", "critical"]
    state: Literal["active", "cleared"]
    site_id: SiteId
    machine_id: MachineId
    operator_id: OperatorId
    started_at: UtcTime
    cleared_at: UtcTime | None = None
    acknowledged: bool

    @model_validator(mode="after")
    def _no_wellbeing(self) -> "SupervisorAlertView":
        if self.category == "wellbeing":
            raise ValueError("wellbeing is shared only as SupervisorRiskView, never as an alert with evidence")
        return self


class SupervisorIncidentView(Strict):
    view: Literal["incident"]
    incident_id: StableId
    state: Literal["draft", "confirmed", "dismissed"]
    site_id: SiteId
    site_zone_id: ZoneId | None = None
    machine_id: MachineId
    operator_id: OperatorId
    severity: Literal["low", "medium", "high", "critical"] | None = None
    summary: str | None = Field(default=None, max_length=300,
                                description="Operator-confirmed description only; null for drafts.")
    occurred_at: UtcTime | None = None
    confirmed_at: UtcTime | None = None

    @model_validator(mode="after")
    def _drafts_private(self) -> "SupervisorIncidentView":
        if self.state != "confirmed" and self.summary is not None:
            raise ValueError("draft/dismissed incident text stays private to the operator")
        return self


class SupervisorRiskView(Strict):
    view: Literal["risk"]
    operator_id: OperatorId
    site_id: SiteId
    risk_level: Literal["no_advisory", "advisory", "high", "unavailable"]
    unavailable_reason: Literal["consent_not_granted", "consent_revoked", "stale_data", "no_data"] | None = None
    observed_at: UtcTime | None = None
    freshness: Literal["fresh", "stale", "unknown"]
    response_context: Literal["none", "break_recommended", "check_on_operator"]

    @model_validator(mode="after")
    def _unavailable(self) -> "SupervisorRiskView":
        if (self.risk_level == "unavailable") != (self.unavailable_reason is not None):
            raise ValueError("unavailable_reason is set exactly when risk_level is unavailable")
        return self


class SupervisorApprovalView(Strict):
    view: Literal["approval"]
    approval: ApprovalRecord


class SupervisorSosView(Strict):
    view: Literal["sos"]
    episode_id: StableId
    operator_id: OperatorId
    machine_id: MachineId | None = None
    site_id: SiteId
    state: Literal["unresolved_no_response", "operator_unreachable", "help_requested", "supervisor_notified",
                   "responded_ok", "resolved"]
    notification_id: StableId | None = None
    created_at: UtcTime


class SupervisorTaskSummary(Strict):
    view: Literal["task_summary"]
    site_id: SiteId
    shift_id: StableId
    machine_id: MachineId
    scheduled: int = Field(ge=0)
    in_progress: int = Field(ge=0)
    completed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    schedule_version: int = Field(ge=1)


SupervisorView = Annotated[
    Union[SupervisorAlertView, SupervisorIncidentView, SupervisorRiskView, SupervisorApprovalView,
          SupervisorSosView, SupervisorTaskSummary],
    Field(discriminator="view"),
]


class SupervisorOverview(Strict):
    """GET /v1/supervisor/overview for one authorised site."""

    site_id: SiteId
    generated_at: UtcTime
    feed_cursor: int = Field(ge=0, description="Resume the change feed from here.")
    tasks: list[SupervisorTaskSummary]
    alerts: list[SupervisorAlertView]
    incidents: list[SupervisorIncidentView]
    pending_approvals: list[SupervisorApprovalView]
    risk: list[SupervisorRiskView]
    sos: list[SupervisorSosView]
    page: PageInfo


class SupervisorFeedEvent(Strict):
    """One `cocoon.supervisor-feed.v1` change. Role-filtered; never reuses operator speech or evidence."""

    schema_version: Literal["cocoon.supervisor-feed.v1"]
    site_id: SiteId
    event_id: StableId
    sequence: int = Field(ge=1, description="Monotonic per site feed; exclusive cursor for ?after=.")
    type: Literal["alert.changed", "incident.changed", "risk.changed", "approval.changed", "sos.changed",
                  "tasks.changed"]
    created_at: UtcTime
    data: SupervisorView

    @model_validator(mode="after")
    def _type_matches(self) -> "SupervisorFeedEvent":
        expected = {"alert.changed": "alert", "incident.changed": "incident", "risk.changed": "risk",
                    "approval.changed": "approval", "sos.changed": "sos", "tasks.changed": "task_summary"}
        if expected[self.type] != self.data.view:
            raise ValueError("type and data.view must agree")
        return self
