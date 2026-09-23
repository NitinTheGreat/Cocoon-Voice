"""Proposed daily-task and duration-estimate contract. Target stages I07A (dashboard/lifecycle), I07B (estimates)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from .common import MachineId, OperatorId, Provenance, ShiftId, SiteId, StableId, Strict, UtcTime, ZoneId
from .telemetry import ConditionCheck, ConditionsSnapshot

TaskLifecycle = Literal["scheduled", "in_progress", "paused", "completed", "cancelled", "blocked"]


class DurationFactor(Strict):
    factor: Literal["task_type", "work_quantity", "ground_condition", "weather", "operator_skill", "machine_age",
                    "material", "attachment", "other"]
    effect_minutes: float = Field(description="Signed contribution relative to the estimator's base.")
    evidence: str = Field(max_length=200, description="Observed input value or calibration note.")


class DurationEstimate(Strict):
    estimate_id: StableId
    estimator_version: str = Field(max_length=32, description="Frozen before any benchmark error is reported.")
    method: Literal["full_inputs", "reduced_inputs_fallback", "insufficient_data"]
    predicted_minutes: float | None = Field(default=None, gt=0)
    interval_low_minutes: float | None = Field(default=None, gt=0)
    interval_high_minutes: float | None = Field(default=None, gt=0)
    input_cutoff_at: UtcTime = Field(description="No input observed after this time (no outcome leakage).")
    inputs_used: list[str]
    missing_inputs: list[str]
    factors: list[DurationFactor]
    calibration_sample_count: int | None = Field(default=None, ge=0)
    uncertainty_note: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _consistent(self) -> "DurationEstimate":
        if self.method == "insufficient_data":
            if self.predicted_minutes is not None:
                raise ValueError("insufficient_data carries no prediction")
        elif self.predicted_minutes is None:
            raise ValueError("a prediction is required unless method is insufficient_data")
        if self.method == "reduced_inputs_fallback" and not self.missing_inputs:
            raise ValueError("a reduced-input fallback must name the missing inputs")
        low, high = self.interval_low_minutes, self.interval_high_minutes
        if (low is None) != (high is None) or (low is not None and not low <= (self.predicted_minutes or low) <= high):
            raise ValueError("interval must bracket the prediction and have both ends")
        return self


class TaskAssignment(Strict):
    task_id: StableId
    version: int = Field(ge=1, description="Optimistic-concurrency version; commands send it as expected_version.")
    shift_id: ShiftId
    operator_id: OperatorId
    machine_id: MachineId
    machine_model: str = Field(max_length=64)
    machine_age_years: float | None = Field(default=None, ge=0)
    site_id: SiteId
    site_zone_id: ZoneId
    scheduled_order: int = Field(ge=1)
    scheduled_start_at: UtcTime | None = None
    outdoor_exposure: Literal["outdoor", "covered", "indoor", "unknown"]
    task_type: str = Field(max_length=64)
    work_quantity: float | None = Field(default=None, gt=0)
    work_unit: str | None = Field(default=None, max_length=16, examples=["m3", "t", "m"])
    ground_condition: str | None = Field(default=None, max_length=32)
    material: str | None = Field(default=None, max_length=32)
    attachment: str | None = Field(default=None, max_length=32)
    operator_skill: Literal["beginner", "intermediate", "expert"] | None = Field(
        default=None, description="Dataset/assigned skill category. Not an LMS learning level or a licence.")
    depends_on_task_ids: list[StableId] = Field(default_factory=list)
    lifecycle: TaskLifecycle
    progress_fraction: float | None = Field(default=None, ge=0, le=1)
    started_at: UtcTime | None = None
    completed_at: UtcTime | None = None
    estimate: DurationEstimate | None = None
    condition_check: ConditionCheck | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def _consistent(self) -> "TaskAssignment":
        if (self.work_quantity is None) != (self.work_unit is None):
            raise ValueError("work_quantity and work_unit go together")
        if self.lifecycle == "completed" and self.completed_at is None:
            raise ValueError("a completed task needs completed_at")
        if self.lifecycle in ("scheduled",) and (self.started_at or self.completed_at):
            raise ValueError("a scheduled task has not started")
        return self


class DailyTaskDashboard(Strict):
    """Today's queue for the bound operator/machine/shift (REQ-01a)."""

    operator_id: OperatorId
    machine_id: MachineId
    site_id: SiteId
    shift_id: ShiftId
    service_date: date = Field(description="Site-local date.")
    site_timezone: str = Field(max_length=64, examples=["Asia/Kolkata"])
    server_time: UtcTime
    data_time: UtcTime = Field(description="Session data clock (replay time in replay mode).")
    clock_mode: Literal["live", "replay"]
    snapshot_version: int = Field(ge=0)
    tasks: list[TaskAssignment]
    conditions: ConditionsSnapshot | None = Field(default=None, description="Null means conditions unknown.")
