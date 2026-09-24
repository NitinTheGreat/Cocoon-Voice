"""Batch C3: explainable duration estimates, saved-prediction stability and the frozen five-row benchmark."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from cocoon_agent.api import schemas as s
from cocoon_agent.api.app import create_app
from cocoon_agent.estimation import EstimateInputs, estimate, load_estimator

from .conftest import AUTH, DEMO_OPERATORS, seeded_demo, write_catalog
from .test_conditions import local, observe
from .test_tasks import command, say, session_for

FROZEN = load_estimator()
SERVICE_DIR = Path(__file__).resolve().parents[1]


def weather(**values) -> s.WeatherSnapshot:
    now = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)
    base = dict(record_id="wx", provider="fixture", kind="synthetic_fixture", site_id="S", valid_from=now,
                valid_to=now + timedelta(hours=1), retrieved_at=now, units={}, quality="complete", provenance="t")
    return s.WeatherSnapshot(**{**base, **values})


def test_productivity_path_applies_only_known_factors_and_explains_them():
    full = estimate(FROZEN, EstimateInputs(
        task_type="earth_excavation", work_quantity=90, work_unit="m3", machine_category="hydraulic_excavator",
        ground_condition="firm", operator_skill="beginner", machine_age_years=12,
        weather=weather(precipitation_rate_mm_h=7.0, wind_gust_ms=5.0, temperature_c=30.0, visibility_m=9000.0)))
    assert full["method"] == "productivity_with_factors" and full["base_minutes"] == 63.5  # 90 m3 at 85 m3/h
    assert [(f["name"], f["multiplier"]) for f in full["factors"]] == [
        ("operator_skill", 1.2), ("machine_age_years", 1.08), ("precipitation_rate_mm_h", 1.2)]
    assert full["predicted_minutes"] == round(90 / 85 * 60 * 1.2 * 1.08 * 1.2, 1)
    assert "operator skill beginner" in full["explanation"] and "not calibrated" in full["explanation"]
    assert full["missing_inputs"] == [] and full["calibration_status"] == "uncalibrated_configured_prior"
    assert estimate(FROZEN, EstimateInputs(task_type="earth_excavation", work_quantity=90, work_unit="m3",
                                           machine_category="hydraulic_excavator", ground_condition="firm",
                                           operator_skill="beginner", machine_age_years=12,
                                           weather=weather(precipitation_rate_mm_h=7.0, wind_gust_ms=5.0,
                                                           temperature_c=30.0, visibility_m=9000.0))) == full


def test_missing_inputs_are_reported_and_fallbacks_are_labelled():
    wrong_unit = estimate(FROZEN, EstimateInputs(task_type="earth_excavation", work_quantity=90, work_unit="t",
                                                 machine_category="hydraulic_excavator"))
    assert wrong_unit["method"] == "typical_duration_fallback" and wrong_unit["predicted_minutes"] == 60.0
    assert "productivity_rate_for_machine_task_unit" in wrong_unit["missing_inputs"]
    assert {"ground_condition", "operator_skill", "machine_age_years", "weather"} <= set(wrong_unit["missing_inputs"])
    reduced = estimate(FROZEN, EstimateInputs(task_type="trenching", provided_estimate_min=45,
                                              categorical_weather="rainy"))
    assert (reduced["method"], reduced["predicted_minutes"]) == ("provided_estimate_adjusted", round(45 * 1.15, 1))
    assert "machine_identity" in reduced["missing_inputs"] and "work_quantity" in reduced["missing_inputs"]
    unknown = estimate(FROZEN, EstimateInputs(task_type="teleportation"))
    assert unknown["method"] == "not_estimable" and unknown["predicted_minutes"] is None


def test_benchmark_baseline_arithmetic_and_committed_report_are_reproducible():
    import importlib.util
    spec = importlib.util.spec_from_file_location("evaluate_estimator", SERVICE_DIR / "scripts" / "evaluate_estimator.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.evaluate()
    assert report["baseline"] == {"total_abs_error_min": 38, "mae_min": 7.6, "bias_min": -6.0}
    assert [r["baseline_error_min"] for r in report["rows"]] == [2, -7, -12, 2, -15]
    assert all(r["method"] == "provided_estimate_adjusted" for r in report["rows"])
    assert report["estimator_config_sha256"] == FROZEN.sha256
    assert report["audit"]["label"] == "illustrative_benchmark_not_independent"
    committed = json.loads((SERVICE_DIR / "estimation" / "benchmark_report_v1.json").read_text())
    assert committed["rows"] == report["rows"] and committed["estimator"] == report["estimator"]
    assert committed["estimator_config_sha256"] == FROZEN.sha256  # the scored configuration is the shipped one


def test_task_estimates_are_saved_used_by_voice_and_kept_from_the_start(tmp_path):
    settings, _ = seeded_demo(tmp_path, COCOON_WEATHER_MODE="fixture")
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        tasks = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["assigned_tasks"]
        first = tasks[0]["estimate"]
        assert first["method"] == "productivity_with_factors" and first["inputs"]["ground_condition"] == "firm"
        assert first["inputs"]["weather"]["provider"] == "fixture"  # weather at the scheduled start (07:30 local)
        assert "operator_skill" in first["missing_inputs"]  # this catalog carries no skill: not guessed
        again = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["assigned_tasks"][0]["estimate"]
        assert again["estimate_id"] == first["estimate_id"]  # saved once per input snapshot
        demo = tasks[2]["estimate"]  # demolition at 11:00 local: gusty and hot fixture weather
        assert {f["name"] for f in demo["factors"]} >= {"wind_gust_ms", "temperature_c"}
        how = say(c, sid, "t1", "how long will my next task take?")
        assert how["actions"][0]["type"] == "task_estimate" and "About 63.5 minutes" in how["speech"]

        observe(c, sid, "o1", local("08:00"))
        r = command(c, sid, "c1", "task.start", tasks[0]["task_id"])
        assert r.status_code == 200
        started = r.json()["task"]["start_estimate"]
        assert started["estimate_id"] == first["estimate_id"]
        replay = command(c, sid, "c1", "task.start", tasks[0]["task_id"])
        assert replay.json()["duplicate"] and replay.json()["task"]["start_estimate"] == started
        observe(c, sid, "o2", local("12:30"))  # the weather changes afterwards
        task = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["assigned_tasks"][0]
        assert task["start_estimate"] == started and task["elapsed_minutes"] is not None
        assert say(c, sid, "t2", "how long will this take?")["actions"][0]["estimate"] == started


def test_catalog_skill_and_machine_age_are_used_as_dataset_inputs(tmp_path):
    root = tmp_path / "cat"
    machines = ("machine_id,model,category,machine_age_years\nEXC_DEMO_001,Cat 320,hydraulic_excavator,12\n"
                "DOZ_DEMO_001,Cat D6,bulldozer,3\nLDR_DEMO_001,Cat 950 GC,wheel_loader,7\n"
                "TRK_DEMO_001,Cat 793,mining_truck,\nBHL_DEMO_001,Cat 420,backhoe_loader,x\n")
    ops = "operator_id,operator_skill\n" + "".join(
        f"{o},{skill}\n" for o, skill in zip(DEMO_OPERATORS, ["beginner", "expert", "certified", "", "intermediate"]))
    digest = write_catalog(root, machines=machines, operators=ops)
    settings, _ = seeded_demo(tmp_path, machines=machines, operators=ops)
    from cocoon_agent.catalog import load_catalog
    catalog = load_catalog(root, digest)
    assert dict(catalog.operator_skill) == {"OP_DEMO_1_1": "beginner", "OP_DEMO_2_1": "expert",
                                            "OP_DEMO_5_1": "intermediate"}  # unknown values are not guessed
    assert dict(catalog.machine_age_years) == {"EXC_DEMO_001": 12.0, "DOZ_DEMO_001": 3.0, "LDR_DEMO_001": 7.0}
    with TestClient(create_app(settings)) as c:
        sid = session_for(c, "EXC_DEMO_001")["session_id"]
        est = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["assigned_tasks"][0]["estimate"]
        assert est["inputs"]["operator_skill"] == "beginner" and est["inputs"]["machine_age_years"] == 12.0
        assert [f["name"] for f in est["factors"]][:2] == ["operator_skill", "machine_age_years"]
