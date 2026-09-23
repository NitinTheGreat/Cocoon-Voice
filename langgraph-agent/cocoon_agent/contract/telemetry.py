"""Proposed typed telemetry and conditions contract. Target stages I05A (ingestion), I09 (conditions).

Units are part of every field name. `null` always means *unknown / not observed*, never zero and never safe.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, RootModel, model_validator

from ..api import schemas as s
from .common import MachineId, OperatorId, Provenance, Quality, SiteId, StableId, Strict, UtcTime


class _ObservationBase(Strict):
    observation_id: StableId = Field(description="Idempotency key within the session.")
    observed_at: UtcTime = Field(description="Original observation time at the source. Receipt time is "
                                             "server-assigned and returned in the ingest result.")
    machine_id: MachineId | None = None
    operator_id: OperatorId | None = None
    device_id: StableId | None = None
    quality: Quality
    provenance: Provenance


class MachineStateObservation(_ObservationBase):
    kind: Literal["machine_state"]
    engine_on: bool | None = None
    seatbelt_fastened: bool | None = None
    operating_state: Literal["operating", "idle", "off", "unknown"] | None = None
    interval_seconds: int | None = Field(default=None, ge=1, le=3600,
                                         description="Interval the *_interval amounts cover.")
    idle_seconds_interval: int | None = Field(default=None, ge=0)
    consecutive_idle_seconds: int | None = Field(default=None, ge=0)
    engine_hours_meter: float | None = Field(default=None, ge=0, description="Cumulative meter, hours.")
    fuel_used_l_interval: float | None = Field(default=None, ge=0)
    load_cycles_interval: int | None = Field(default=None, ge=0)
    control_impacts_count_interval: int | None = Field(
        default=None, ge=0, description="Hydraulic/control impacts of the machine. NOT a human fall or impact.")

    @model_validator(mode="after")
    def _interval(self) -> "MachineStateObservation":
        amounts = (self.idle_seconds_interval, self.fuel_used_l_interval, self.load_cycles_interval,
                   self.control_impacts_count_interval)
        if any(a is not None for a in amounts) and self.interval_seconds is None:
            raise ValueError("interval amounts need interval_seconds")
        return self


class MotionObservation(_ObservationBase):
    kind: Literal["motion"]
    speed_mps: float | None = Field(default=None, ge=0)
    accel_mps2: float | None = None
    derivation_interval_ms: int | None = Field(default=None, ge=1, le=10_000,
                                               description="Window used to derive acceleration.")
    pitch_deg: float | None = Field(default=None, ge=-90, le=90)
    roll_deg: float | None = Field(default=None, ge=-180, le=180)
    grade_pct: float | None = Field(default=None, description="Percent grade. Not degrees.")

    @model_validator(mode="after")
    def _derivation(self) -> "MotionObservation":
        if self.accel_mps2 is not None and self.derivation_interval_ms is None:
            raise ValueError("accel_mps2 needs derivation_interval_ms")
        return self


class ProximityObservation(_ObservationBase):
    kind: Literal["proximity"]
    detector_id: StableId
    entity_id: StableId
    entity_type: Literal["person", "vehicle", "object", "unknown"]
    detection_state: Literal["detected", "not_detected", "unknown"] = Field(
        description="`unknown` (occluded, stale, detector down) is never treated as a clear zone.")
    distance_m: float | None = Field(default=None, ge=0)
    distance_uncertainty_m: float | None = Field(default=None, ge=0)
    bearing_deg: float | None = Field(default=None, ge=0, lt=360)
    reference_frame: Literal["machine_body_clockwise_from_forward"] = "machine_body_clockwise_from_forward"

    @model_validator(mode="after")
    def _detected(self) -> "ProximityObservation":
        if self.detection_state == "detected" and self.distance_m is None:
            raise ValueError("a detected entity needs distance_m")
        if self.detection_state != "detected" and self.distance_m is not None:
            raise ValueError("distance_m is only reported for a detected entity")
        return self


class HumanImpactObservation(_ObservationBase):
    """A person-worn device impact. Distinct from machine control impacts and from training simulations."""

    kind: Literal["human_impact"]
    operator_id: OperatorId
    device_id: StableId
    peak_accel_g: float = Field(ge=0)
    duration_ms: int = Field(ge=0, le=60_000)
    orientation_after: Literal["upright", "prone", "supine", "side", "unknown"] = "unknown"
    impact_force_n: float | None = Field(default=None, ge=0, description="Only with a documented force model.")
    force_model_ref: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _force(self) -> "HumanImpactObservation":
        if self.impact_force_n is not None and not self.force_model_ref:
            raise ValueError("impact_force_n needs force_model_ref; acceleration is not force")
        return self


class EnvironmentObservation(_ObservationBase):
    kind: Literal["environment"]
    site_id: SiteId | None = None
    temperature_c: float | None = Field(default=None, ge=-60, le=70)
    relative_humidity_pct: float | None = Field(default=None, ge=0, le=100)
    precipitation_mm: float | None = Field(default=None, ge=0)
    precipitation_interval_minutes: int | None = Field(default=None, ge=1)
    wind_speed_mps: float | None = Field(default=None, ge=0)
    wind_gust_mps: float | None = Field(default=None, ge=0)
    visibility_m: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _precip(self) -> "EnvironmentObservation":
        if self.precipitation_mm is not None and self.precipitation_interval_minutes is None:
            raise ValueError("precipitation_mm needs its accumulation interval")
        return self


class VitalsObservation(_ObservationBase):
    """Private operator data. Processed only under `vitals_processing` consent; never in a supervisor payload."""

    kind: Literal["vitals"]
    operator_id: OperatorId
    heart_rate_bpm: float | None = Field(default=None, ge=20, le=250)
    skin_temp_c: float | None = Field(default=None, ge=20, le=45,
                                      description="Skin temperature. Not core body temperature.")
    window_seconds: int = Field(ge=1, le=3600, description="Summary window the values describe.")


Observation = Annotated[
    Union[MachineStateObservation, MotionObservation, ProximityObservation, HumanImpactObservation,
          EnvironmentObservation, VitalsObservation],
    Field(discriminator="kind"),
]


class TelemetryBatchRequest(Strict):
    """Target v2 body for POST .../telemetry. The v1 body (`simulated: true` + readings) stays accepted."""

    schema_version: Literal["cocoon.telemetry.v2"]
    batch_id: StableId
    observations: list[Observation] = Field(min_length=1, max_length=500)


class ObservationOutcome(Strict):
    observation_id: StableId
    outcome: Literal["accepted", "duplicate", "ignored_out_of_order", "rejected"]
    reason: Literal["binding_mismatch", "unit_or_range", "consent_missing", "stale", "unknown_source"] | None = None
    received_at: UtcTime

    @model_validator(mode="after")
    def _reason(self) -> "ObservationOutcome":
        if self.outcome == "rejected" and self.reason is None:
            raise ValueError("a rejected observation needs a reason")
        return self


class TelemetryIngestResult(Strict):
    session_id: StableId
    batch_id: StableId
    results: list[ObservationOutcome]
    alerts_opened: list[StableId]
    alerts_cleared: list[StableId]
    announcements_created: list[StableId]
    state_version: int = Field(ge=0)


# --------------------------------------------------------------------------- conditions / forecasts


class ConditionsSnapshot(Strict):
    """Observed or forecast site conditions (I09). Forecast rows carry issue/valid/retrieval times."""

    conditions_id: StableId
    site_id: SiteId
    kind: Literal["observed", "forecast"]
    source: Literal["open_meteo", "fixture"]
    provider_model: str | None = Field(default=None, max_length=64)
    issued_at: UtcTime | None = None
    valid_from: UtcTime
    valid_to: UtcTime
    retrieved_at: UtcTime
    available_at: UtcTime = Field(description="When this record could first have been known (replay cutoffs).")
    temperature_c: float | None = None
    relative_humidity_pct: float | None = Field(default=None, ge=0, le=100)
    precipitation_mm: float | None = Field(default=None, ge=0)
    precipitation_interval_minutes: int | None = Field(default=None, ge=1)
    wind_speed_mps: float | None = Field(default=None, ge=0)
    wind_gust_mps: float | None = Field(default=None, ge=0)
    visibility_m: float | None = Field(default=None, ge=0)
    weather_code: int | None = None
    heat_index_c: float | None = None
    heat_index_method: str | None = Field(default=None, max_length=64)
    freshness: Literal["fresh", "stale", "unknown"]
    provenance: Provenance

    @model_validator(mode="after")
    def _times(self) -> "ConditionsSnapshot":
        if self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        if self.kind == "forecast" and self.issued_at is None:
            raise ValueError("a forecast needs issued_at")
        if self.precipitation_mm is not None and self.precipitation_interval_minutes is None:
            raise ValueError("precipitation_mm needs its accumulation interval")
        if (self.heat_index_c is None) != (self.heat_index_method is None):
            raise ValueError("heat_index_c and heat_index_method go together")
        return self


class ConditionCheck(Strict):
    """Pre-task working-condition decision. Workflow advice only; never remote machine control."""

    task_id: StableId
    conditions_id: StableId | None
    outcome: Literal["clear", "advisory", "acknowledgement_required", "policy_block", "conditions_unknown"]
    rule_ids: list[str]
    explanation: str = Field(max_length=500)
    recommended_action: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _unknown(self) -> "ConditionCheck":
        if self.conditions_id is None and self.outcome != "conditions_unknown":
            raise ValueError("without eligible conditions the outcome is conditions_unknown, never clear")
        return self


class TelemetryRequestTarget(RootModel[Union[s.TelemetryRequest, TelemetryBatchRequest]]):
    """Target body of POST .../telemetry: the unchanged v1 body OR the typed v2 batch."""
