"""Proposed rule-policy, alert and wellbeing contract. Target stages I09 (policy registry), I10 (rules), I11.

No threshold value in this contract or its fixtures is a sourced safety limit. Every parameter carries the
evidence status of the policy that owns it.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from .common import MachineId, OperatorId, SiteId, StableId, Strict, UtcTime

RuleFamily = Literal[
    "seatbelt_engine_on", "proximity", "prolonged_idle", "idle_unbelted", "fuel_per_load_cycle",
    "motion_change", "slope", "working_condition", "heat_break_advisory", "repeat_violation",
]
EvidenceStatus = Literal["published_guidance", "site_configured", "synthetic_demo_assumption"]


class RuleCitation(Strict):
    title: str = Field(max_length=200)
    url: str = Field(max_length=500, pattern=r"^https://")
    section: str | None = Field(default=None, max_length=100)
    published: date | None = None


class RuleParameter(Strict):
    name: str = Field(max_length=64)
    value: float | int | bool | str
    unit: str = Field(max_length=16, description="Explicit unit, e.g. s, m, deg, pct, degC, L_per_cycle, count.")


class RuleApplicability(Strict):
    machine_models: list[str] = Field(default_factory=list, description="Empty means not yet reviewed, not 'all'.")
    task_types: list[str] = Field(default_factory=list)
    site_ids: list[SiteId] = Field(default_factory=list)


class RulePolicyRef(Strict):
    rule_id: str = Field(max_length=128)
    rule_version: str = Field(max_length=32)
    family: RuleFamily
    evidence_status: EvidenceStatus
    citation: RuleCitation | None = None
    applicability: RuleApplicability
    parameters: list[RuleParameter]
    entry_persistence_seconds: int | None = Field(default=None, ge=0)
    reset_description: str = Field(max_length=300, description="What clears an episode; re-entry is a new episode.")
    cooldown_seconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _evidence(self) -> "RulePolicyRef":
        if self.evidence_status == "published_guidance" and self.citation is None:
            raise ValueError("published_guidance needs a citation that supports the parameters actually used")
        return self


class EvidenceValue(Strict):
    field: str = Field(max_length=64)
    value: float | int | bool | str | None
    unit: str | None = Field(default=None, max_length=16)
    observation_id: StableId
    observed_at: UtcTime


class AlertAcknowledgement(Strict):
    """A human acknowledgement. Distinct from playback (delivery) and from the condition clearing."""

    acknowledgement_id: StableId
    by_subject_id: StableId
    channel: Literal["voice", "screen"]
    acknowledged_at: UtcTime


AlertCategory = Literal["safety", "efficiency", "conditions", "wellbeing"]


class OperatorAlertView(Strict):
    """The bound operator's own alert, including immutable trigger evidence for "Why?"."""

    alert_id: StableId
    episode_id: StableId
    rule: RulePolicyRef
    category: AlertCategory
    severity: Literal["info", "warning", "critical"]
    state: Literal["active", "cleared"]
    machine_id: MachineId
    operator_id: OperatorId
    started_at: UtcTime
    cleared_at: UtcTime | None = None
    trigger_evidence: list[EvidenceValue] = Field(min_length=1, description="Snapshot at entry; never replaced by "
                                                                            "later readings.")
    observation_freshness: Literal["fresh", "stale", "unknown"]
    explanation: str = Field(max_length=500)
    recommended_action: str = Field(max_length=300)
    announcement_event_id: StableId | None = None
    linked_incident_draft_id: StableId | None = None
    linked_lesson_assignment_id: StableId | None = None
    acknowledgements: list[AlertAcknowledgement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _state(self) -> "OperatorAlertView":
        if (self.state == "cleared") != (self.cleared_at is not None):
            raise ValueError("cleared_at is set exactly when the alert is cleared")
        return self


# --------------------------------------------------------------------------- wellbeing (private operator view)


class SleepSummary(Strict):
    duration_hours: float = Field(ge=0, le=24)
    window_start: UtcTime
    window_end: UtcTime
    quality: Literal["good", "fair", "poor", "unknown"]
    source: Literal["self_report", "device", "synthetic"]


class WellbeingAdvisory(Strict):
    level: Literal["advisory", "high"]
    message: str = Field(max_length=300, description="Bounded advice. Not a diagnosis.")
    recommended_action: str = Field(max_length=200)
    rule_id: str = Field(max_length=128)
    rule_version: str = Field(max_length=32)


class OperatorWellbeingView(Strict):
    """Private to the operator. Supervisors get SupervisorRiskView instead, never this model."""

    operator_id: OperatorId
    vitals_processing_consent: Literal["granted", "revoked", "not_set"]
    observed_at: UtcTime | None = None
    heart_rate_bpm: float | None = None
    skin_temp_c: float | None = Field(default=None, description="Skin temperature. Not core temperature.")
    heat_index_c: float | None = None
    minutes_since_break: int | None = Field(default=None, ge=0)
    sleep: SleepSummary | None = Field(default=None, description="Null means unknown sleep, not zero sleep.")
    advisory: WellbeingAdvisory | None = None

    @model_validator(mode="after")
    def _consent(self) -> "OperatorWellbeingView":
        if self.vitals_processing_consent != "granted" and (
            self.heart_rate_bpm is not None or self.skin_temp_c is not None or self.advisory is not None
        ):
            raise ValueError("vitals and vitals-based advice require granted vitals_processing consent")
        return self
