"""Reproducible evaluation of the frozen duration estimator on the five provided benchmark rows.

    python scripts/evaluate_estimator.py            # prints the report and writes estimation/benchmark_report_v1.json
    python scripts/evaluate_estimator.py --check    # exit 1 if the committed report differs from a fresh run

The estimator configuration is frozen before scoring: its SHA-256 is printed and stored with the result, and the
script never changes it. The baseline is recomputed from the provided rows. The rows lack quantity, ground condition,
machine identity (and, in this checkout, the source CSV's weather/skill/age columns), so the documented reduced path
`provided_estimate_adjusted` is used: the provided pre-task estimate adjusted only by factors whose inputs are known.
Nothing is filled in from actual durations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cocoon_agent.estimation import DEFAULT_CONFIG, EstimateInputs, estimate, load_estimator  # noqa: E402

SERVICE_DIR = Path(__file__).resolve().parent.parent
ROWS = SERVICE_DIR / "estimation" / "provided_benchmark_v1.json"
REPORT = SERVICE_DIR / "estimation" / "benchmark_report_v1.json"


def evaluate() -> dict:
    frozen = load_estimator(DEFAULT_CONFIG)
    doc = json.loads(ROWS.read_text(encoding="utf-8"))
    rows = []
    for r in doc["rows"]:
        result = estimate(frozen, EstimateInputs(
            task_type=r["task_type"], work_quantity=r["work_quantity"], work_unit=r["work_unit"],
            ground_condition=r["ground_condition"], categorical_weather=r["weather"], operator_skill=r["operator_skill"],
            machine_age_years=r["machine_age_years"], provided_estimate_min=r["provided_estimate_min"]))
        actual = r["provided_actual_min"]
        rows.append({
            "task_id": r["task_id"], "task_type": r["task_type"], "provided_estimate_min": r["provided_estimate_min"],
            "provided_actual_min": actual, "baseline_error_min": r["provided_estimate_min"] - actual,
            "method": result["method"], "predicted_min": result["predicted_minutes"],
            "error_min": round(result["predicted_minutes"] - actual, 1),
            "factors_applied": [f["name"] for f in result["factors"]], "missing_inputs": result["missing_inputs"]})
    n = len(rows)
    base_abs = [abs(x["baseline_error_min"]) for x in rows]
    est_abs = [abs(x["error_min"]) for x in rows]
    return {
        "schema": "cocoon.estimator-benchmark-report.v1",
        "benchmark_version": doc["benchmark_version"],
        "estimator_version": frozen.config.estimator_version,
        "estimator_config_sha256": frozen.sha256,
        "calibration_status": frozen.config.calibration_status,
        "count": n,
        "baseline": {"total_abs_error_min": sum(base_abs), "mae_min": round(sum(base_abs) / n, 2),
                     "bias_min": round(sum(x["baseline_error_min"] for x in rows) / n, 2)},
        "estimator": {"total_abs_error_min": round(sum(est_abs), 1), "mae_min": round(sum(est_abs) / n, 2),
                      "bias_min": round(sum(x["error_min"] for x in rows) / n, 2)},
        "rows": rows,
        "missing_input_handling": (
            "Reduced path provided_estimate_adjusted for every row: quantity, ground condition and machine identity are "
            "absent from the provided rows (DG-14), and the source CSV's weather/skill/age columns are not in this "
            "checkout, so no context factor could be applied. Missing inputs were not synthesised."),
        "audit": {
            "label": "illustrative_benchmark_not_independent",
            "actuals_used_for_fitting": False,
            "reasons": [
                "No parameter was fitted: the configuration is an uncalibrated prior and the dataset is absent.",
                "The configuration author had seen the five actual durations (they are printed in the assignment), so "
                "the five rows cannot count as an independent holdout even though they were not used for tuning.",
                "Whether the provided actuals influenced the synthetic dataset's generated targets cannot be audited "
                "here: Cocoon_Dataset_v1 and its generator are not in this checkout.",
                "No synthetic training/validation split was run (no historical data available here)."],
        },
        "conclusion": None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if the committed report is out of date")
    args = ap.parse_args()
    report = evaluate()
    b, e = report["baseline"], report["estimator"]
    if e["mae_min"] < b["mae_min"]:
        report["conclusion"] = f"estimator MAE {e['mae_min']} min is below the provided baseline {b['mae_min']} min"
    elif e["mae_min"] == b["mae_min"]:
        report["conclusion"] = (f"no improvement: estimator MAE {e['mae_min']} min equals the provided baseline "
                                "(no context factor was available for these rows)")
    else:
        report["conclusion"] = f"worse than baseline: estimator MAE {e['mae_min']} min vs {b['mae_min']} min"
    text = json.dumps(report, indent=2) + "\n"
    if args.check:
        current = REPORT.read_text(encoding="utf-8") if REPORT.exists() else ""
        if current != text:
            print("benchmark report is out of date: run python scripts/evaluate_estimator.py", file=sys.stderr)
            return 1
        print("benchmark report up to date")
        return 0
    REPORT.write_text(text, encoding="utf-8")
    print(f"estimator {report['estimator_version']} config sha256 {report['estimator_config_sha256'][:16]}... "
          f"({report['calibration_status']})")
    print(f"{'task':6} {'type':18} {'provided':>8} {'actual':>6} {'base err':>8} {'method':28} {'pred':>6} {'err':>6}")
    for r in report["rows"]:
        print(f"{r['task_id']:6} {r['task_type']:18} {r['provided_estimate_min']:>8} {r['provided_actual_min']:>6} "
              f"{r['baseline_error_min']:>8} {r['method']:28} {r['predicted_min']:>6} {r['error_min']:>6}")
    print(f"baseline: total |error| {b['total_abs_error_min']} min, MAE {b['mae_min']}, bias {b['bias_min']} (n={report['count']})")
    print(f"estimator: total |error| {e['total_abs_error_min']} min, MAE {e['mae_min']}, bias {e['bias_min']}")
    print(f"audit: {report['audit']['label']}; conclusion: {report['conclusion']}")
    print(f"wrote {REPORT.relative_to(SERVICE_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
