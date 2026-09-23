"""Proposed structured incident and automatic-draft contract. Target stage I06 (drafts linked to I10 episodes)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import MachineId, OperatorId, ShiftId, SiteId, StableId, Strict, UtcTime, ZoneId

FactBasis = Literal["reported", "inferred", "system_observed"]
"""`reported` = the operator said/typed it; `inferred` = the system proposed it and the operator has not confirmed
it; `system_observed` = copied from persisted telemetry/episode evidence."""

IncidentField = Literal["what", "where", "when", "severity"]


class ReportedText(Strict):
    value: str = Field(min_length=1, max_length=1000)
    basis: FactBasis


class ReportedTime(Strict):
    value: UtcTime
    basis: FactBasis


class ReportedSeverity(Strict):
    value: Literal["low", "medium", "high", "critical"]
    basis: FactBasis


class IncidentLocation(Strict):
    site_id: SiteId
    site_zone_id: ZoneId | None = None
    description: str | None = Field(default=None, max_length=300)
    basis: FactBasis


class IncidentEvidence(Strict):
    episode_id: StableId | None = None
    alert_id: StableId | None = None
    observation_ids: list[StableId] = Field(default_factory=list, max_length=50)


class IncidentReport(Strict):
    incident_id: StableId
    version: int = Field(ge=1)
    state: Literal["draft", "confirmed", "dismissed"]
    origin: Literal["operator_reported", "auto_draft_from_episode"]
    operator_id: OperatorId
    machine_id: MachineId
    shift_id: ShiftId | None = None
    what: ReportedText | None = None
    where: IncidentLocation | None = None
    when: ReportedTime | None = None
    severity: ReportedSeverity | None = None
    missing_fields: list[IncidentField]
    evidence: IncidentEvidence
    client_draft_id: StableId | None = Field(default=None, description="Device-side draft identity (offline).")
    source_turn_id: StableId | None = None
    source_command_id: StableId | None = None
    created_at: UtcTime
    confirmed_at: UtcTime | None = None
    dismissed_at: UtcTime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "IncidentReport":
        absent = {name for name in ("what", "where", "when", "severity") if getattr(self, name) is None}
        if absent != set(self.missing_fields):
            raise ValueError("missing_fields must list exactly the absent structured fields")
        if self.origin == "auto_draft_from_episode" and self.evidence.episode_id is None:
            raise ValueError("an automatic draft must link its episode")
        if self.state == "confirmed":
            if absent or self.confirmed_at is None:
                raise ValueError("a confirmed incident needs every structured field and confirmed_at")
            inferred = [n for n in ("what", "where", "when", "severity") if getattr(self, n).basis == "inferred"]
            if inferred:
                raise ValueError(f"confirmation cannot keep unconfirmed inferred facts: {inferred}")
        if (self.state == "dismissed") != (self.dismissed_at is not None):
            raise ValueError("dismissed_at is set exactly when the draft is dismissed")
        if self.state != "confirmed" and self.confirmed_at is not None:
            raise ValueError("confirmed_at is only set on a confirmed incident")
        return self
