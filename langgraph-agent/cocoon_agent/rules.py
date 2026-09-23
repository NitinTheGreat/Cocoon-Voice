"""PROTOTYPE safety rules on SIMULATED telemetry, driven by a versioned policy file.

These are illustrative demo rules. They are not validated machine safety logic and must not be used to protect
people on a real machine. Thresholds come from the policy file and are labelled `demo_assumption`.

Rules read observations only (engine, belt, operating state), never scenario names or expected-output labels.
Durations use the observation clock (`observed_at` of the samples); wall-clock time is used only for freshness,
token expiry, announcement expiry and provider deadlines.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .api.schemas import TelemetryReadings

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "safety_policy_v1.json"


class RulePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    family: Literal["seatbelt_engine_on", "prolonged_idle", "idle_unbelted"]
    alert_type: Literal["seatbelt_unfastened", "prolonged_idle", "idle_unbelted"]
    severity: Literal["warning", "critical"]
    idle_threshold_seconds: int | None = Field(default=None, ge=1)
    message: str
    reason: str
    recommended_action: str
    start_speech: str
    clear_speech: str | None = None
    draft_incident: bool = False
    draft_severity: Literal["low", "medium", "high", "critical"] | None = None
    applies_to_categories: list[str] = Field(default_factory=list, description="Empty = every machine category.")

    def applies_to(self, category: str | None) -> bool:
        return not self.applies_to_categories or category in self.applies_to_categories

    def explanation(self, observed_at: datetime, idle_since: datetime | None, idle_seconds: int | None) -> str:
        at = observed_at.strftime("%H:%M:%S UTC")
        if self.family == "seatbelt_engine_on":
            what = f"At {at} the simulated telemetry showed the engine running while the seatbelt was unfastened."
        else:
            since = idle_since.strftime("%H:%M:%S UTC") if idle_since else "?"
            minutes = round((idle_seconds or 0) / 60, 1)
            belt = " with the seatbelt unfastened" if self.family == "idle_unbelted" else ""
            what = (f"The simulated telemetry showed the engine idling{belt} from {since} to {at}, {minutes} minutes "
                    f"of observation time (demo limit {self.idle_threshold_seconds} seconds).")
        return what + " This is a prototype rule on simulated data, not validated machine safety logic."


class SafetyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_id: Literal["cocoon.safety-policy.v1"] = Field(alias="schema")
    policy_version: str
    source_status: Literal["demo_assumption", "published_limit"]
    note: str
    rules: list[RulePolicy]


def load_policy(path: Path | None = None) -> SafetyPolicy:
    policy = SafetyPolicy.model_validate(json.loads((path or DEFAULT_POLICY_PATH).read_text(encoding="utf-8")))
    families = [r.family for r in policy.rules]
    if len(set(families)) != len(families) or len({r.rule_id for r in policy.rules}) != len(policy.rules):
        raise ValueError("safety policy: rule families and rule_ids must be unique")
    for r in policy.rules:
        if r.family != "seatbelt_engine_on" and r.idle_threshold_seconds is None:
            raise ValueError(f"safety policy: {r.rule_id} needs idle_threshold_seconds")
    # Seatbelt first: a correlated idle/belt episode must see the belt episode of the same sample.
    policy.rules.sort(key=lambda r: r.family != "seatbelt_engine_on")
    return policy


def seatbelt_condition(readings: TelemetryReadings, *, requires_engine_on: bool) -> bool:
    """True while the (prototype) seatbelt condition holds for this sample."""
    if readings.seatbelt_fastened:
        return False
    return readings.engine_on or not requires_engine_on


def idle_observation(readings: TelemetryReadings) -> bool | None:
    """Idling per this observation: True/False, or None when the operating state is unknown."""
    if not readings.engine_on:
        return False
    if readings.operating_state is None:
        return None
    return readings.operating_state == "idle"


def condition(rule: RulePolicy, readings: TelemetryReadings, idle: bool | None, idle_seconds: int,
              *, requires_engine_on: bool) -> bool | None:
    """Whether the rule's condition holds for this sample; None = cannot be evaluated (unknown input)."""
    if rule.family == "seatbelt_engine_on":
        return seatbelt_condition(readings, requires_engine_on=requires_engine_on)
    if idle is None:
        return None
    held = idle and idle_seconds >= (rule.idle_threshold_seconds or 0)
    if rule.family == "idle_unbelted":
        return held and not readings.seatbelt_fastened
    return held
