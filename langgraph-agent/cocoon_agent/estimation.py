"""Explainable task-duration estimates (Batch C3).

One deterministic function over a frozen, versioned configuration (estimation/estimator_v1.json). No model is
trained at startup or refitted per request, and no language model is involved: the explanation lists exactly the
factors the calculation applied.

Paths, chosen by what the inputs allow (never by the outcome):
1. `productivity_with_factors`: work_quantity / configured rate for this machine category + task type + unit.
2. `provided_estimate_adjusted`: a planner's pre-task estimate (available before the task, never the actual duration)
   adjusted by the same context factors. Used for the five provided benchmark rows, which lack quantity, ground and
   machine identity.
3. `typical_duration_fallback`: the configured typical minutes for the task type.
Context factors (each only when its input is known): ground condition, operator skill (dataset skill, not an LMS
learning level), machine age, numeric weather (rain rate, gusts, heat, visibility) or categorical weather.

The configuration is an UNCALIBRATED demo prior (the historical dataset is not in this checkout). Its predictions are
illustrative and carry no statistical confidence interval.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .api import schemas as s

SERVICE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SERVICE_DIR / "estimation" / "estimator_v1.json"
_UNIT_WORDS = {"m3": "cubic metres", "m": "metres", "m2": "square metres", "t": "tonnes", "t_km": "tonne-kilometres"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Rate(_Strict):
    category: str
    task_type: str
    unit: str
    per_hour: float = Field(gt=0)


class EstimatorConfig(_Strict):
    schema_id: Literal["cocoon.duration-estimator.v1"] = Field(alias="schema")
    estimator_version: str
    calibration_status: Literal["uncalibrated_configured_prior", "calibrated"]
    note: str
    productivity: dict[str, Any]
    typical_minutes: dict[str, Any]
    factors: dict[str, Any]

    def rate(self, category: str | None, task_type: str | None, unit: str | None) -> Rate | None:
        for r in self.productivity["rates"]:
            rate = Rate(**r)
            if (rate.category, rate.task_type, rate.unit) == (category, task_type, unit):
                return rate
        return None


@dataclass(frozen=True)
class Frozen:
    config: EstimatorConfig
    sha256: str  # of the exact config bytes: the estimator identity used in every saved result


def load_estimator(path: Path | None = None) -> Frozen:
    data = (path or DEFAULT_CONFIG).read_bytes()
    return Frozen(EstimatorConfig.model_validate(json.loads(data)), hashlib.sha256(data).hexdigest())


@dataclass(frozen=True)
class EstimateInputs:
    task_type: str | None
    work_quantity: float | None = None
    work_unit: str | None = None
    ground_condition: str | None = None
    machine_category: str | None = None
    operator_skill: str | None = None
    machine_age_years: float | None = None
    weather: s.WeatherSnapshot | None = None
    categorical_weather: str | None = None
    provided_estimate_min: float | None = None

    def snapshot(self) -> dict[str, Any]:
        w = self.weather
        return {"task_type": self.task_type, "work_quantity": self.work_quantity, "work_unit": self.work_unit,
                "ground_condition": self.ground_condition, "machine_category": self.machine_category,
                "operator_skill": self.operator_skill, "machine_age_years": self.machine_age_years,
                "weather": None if w is None else {
                    "record_id": w.record_id, "provider": w.provider, "kind": w.kind, "valid_from": w.valid_from.isoformat(),
                    "temperature_c": w.temperature_c, "precipitation_rate_mm_h": w.precipitation_rate_mm_h,
                    "wind_gust_ms": w.wind_gust_ms, "visibility_m": w.visibility_m},
                "categorical_weather": self.categorical_weather,
                "provided_estimate_min": self.provided_estimate_min}


def _step(table: list[dict[str, float]], value: float) -> float | None:
    for row in table:
        if "at_or_above" in row and value >= row["at_or_above"]:
            return row["multiplier"]
        if "below" in row and value < row["below"]:
            return row["multiplier"]
    return None


def estimate(frozen: Frozen, inputs: EstimateInputs) -> dict[str, Any]:
    """Pure: the same inputs and config always give the same result (no clock, no randomness)."""
    cfg = frozen.config
    missing: list[str] = []
    rate = cfg.rate(inputs.machine_category, inputs.task_type, inputs.work_unit)
    if inputs.work_quantity is not None and rate is not None:
        method, base = "productivity_with_factors", inputs.work_quantity / rate.per_hour * 60
        base_note = (f"{inputs.work_quantity:g} {_UNIT_WORDS.get(rate.unit, rate.unit)} at {rate.per_hour:g} per hour "
                     f"for a {inputs.machine_category.replace('_', ' ')}")
    else:
        if inputs.work_quantity is None:
            missing.append("work_quantity")
        elif rate is None:
            missing.append("productivity_rate_for_machine_task_unit")
        if inputs.machine_category is None:
            missing.append("machine_identity")
        if inputs.provided_estimate_min is not None:
            method, base = "provided_estimate_adjusted", float(inputs.provided_estimate_min)
            base_note = f"the provided pre-task estimate of {base:g} minutes"
        elif inputs.task_type in cfg.typical_minutes:
            method, base = "typical_duration_fallback", float(cfg.typical_minutes[inputs.task_type])
            base_note = f"the configured typical {base:g} minutes for this task type"
        else:
            return {"method": "not_estimable", "predicted_minutes": None, "base_minutes": None, "factors": [],
                    "missing_inputs": missing + ["task_type"], "explanation": "No estimate: the task type is unknown.",
                    **_identity(frozen, inputs)}
    factors: list[dict[str, Any]] = []
    f = cfg.factors

    def apply(name: str, value: Any, multiplier: float | None, basis: str) -> None:
        if multiplier is not None and multiplier != 1.0:
            factors.append({"name": name, "value": value, "multiplier": multiplier, "basis": basis})

    if inputs.ground_condition is None:
        missing.append("ground_condition")
    elif inputs.ground_condition in f["ground_condition"]:
        apply("ground_condition", inputs.ground_condition, f["ground_condition"][inputs.ground_condition],
              "configured_demo_assumption")
    else:
        missing.append("ground_condition (unrecognised value)")
    if inputs.operator_skill is None:
        missing.append("operator_skill")
    else:
        apply("operator_skill", inputs.operator_skill, f["operator_skill"].get(inputs.operator_skill),
              "configured_demo_assumption")
    if inputs.machine_age_years is None:
        missing.append("machine_age_years")
    else:
        apply("machine_age_years", inputs.machine_age_years, _step(f["machine_age_years"], inputs.machine_age_years),
              "configured_demo_assumption")
    if inputs.weather is not None:
        for var, table in f["weather"].items():
            value = getattr(inputs.weather, var)
            if value is not None:
                apply(var, value, _step(table, value), f"{inputs.weather.provider}_weather")
    elif inputs.categorical_weather is not None:
        apply("categorical_weather", inputs.categorical_weather,
              f["categorical_weather"].get(inputs.categorical_weather), "configured_demo_assumption")
    else:
        missing.append("weather")
    predicted = base
    for factor in factors:
        before = predicted
        predicted *= factor["multiplier"]
        factor["effect_minutes"] = round(predicted - before, 1)
    predicted = round(predicted, 1)
    parts = [f"{f_['name'].replace('_', ' ')} {f_['value']} ({'+' if f_['effect_minutes'] >= 0 else ''}"
             f"{f_['effect_minutes']:g} min)" for f_ in factors]
    explanation = (f"About {predicted:g} minutes: from {base_note}"
                   + (f", adjusted for {', '.join(parts)}" if parts else ", with no known factor adjusting it")
                   + ". Configured demo estimate (not calibrated on historical outcomes).")
    return {"method": method, "predicted_minutes": predicted, "base_minutes": round(base, 1), "base_source": base_note,
            "factors": factors, "missing_inputs": missing, "explanation": explanation, **_identity(frozen, inputs)}


def _identity(frozen: Frozen, inputs: EstimateInputs) -> dict[str, Any]:
    snap = inputs.snapshot()
    return {"estimator_version": frozen.config.estimator_version, "config_sha256": frozen.sha256,
            "calibration_status": frozen.config.calibration_status, "inputs": snap,
            "inputs_sha256": hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()}
