# Cocoon data-gap report

Authority: `BACKEND_IMPLEMENTATION_PLAN.md` revision 2.1, section 3. Audited in I00 on 2026-09-24 against the local `Cocoon_Dataset_v1/` folder.

`Cocoon_Dataset_v1/` is present in the working tree but **untracked** on branch `backend`. It belongs to the data stream. I00 read and validated it, and left it unmodified and unstaged.

## Verified state of v1

These checks were run in I00 with Python 3.11.15 and the standard library only:

| Check | Result |
| --- | --- |
| `sha256sum -c CHECKSUMS.sha256` (33 files) | All OK, before and after running the dataset tests. |
| `python cocoon_data.py validate` | `status: passed`. Covers file hashes, row uniqueness, typed finite values, joins, interval conservation, meter continuity, fuel integration, causal EWMAs, daily rollups, task durations and episode boundaries. |
| `python -m unittest test_data` | 12 tests, OK. |
| Manifest `data/generated/manifest.json` | `schema_version 1.0`, seed `20260923`, 60 s sampling, 480-minute shifts, `dataset_origin: synthetic`. |
| Row counts | `history_minutes` 72,000 (14,400 per machine); `history_operator_days` 150; `task_history` 1,214; `machines` 5; `operators` 15. These match the counts in plan section 3. |
| Assets | `EXC_DEMO_001` Cat 320, `DOZ_DEMO_001` Cat D6, `LDR_DEMO_001` Cat 950 GC, `TRK_DEMO_001` Cat 793, `BHL_DEMO_001` Cat 420. Reference URLs are scoped to `model_identity_and_category_only`. |
| Time range | 2026-08-24 to 2026-09-22 UTC, one 06:00–14:00 UTC shift per machine per day. |
| Scenarios | 5 scenario kinds × 30 shifts: `normal`, `seatbelt_repeat_clear_retrigger`, `sustained_idle`, `warm_operation`, `operator_break_context`. 60 expected seatbelt episodes. `expected_scenarios.json` declares `rules_are_unvalidated_demo_rules: true` and is test-only. |
| Null semantics | `end_stops_count` is empty for all 14,400 `TRK_DEMO_001` rows (N/A for a haul truck). `jerk` and `task_id` are empty on a subset of rows. N/A is not zero. |
| Five-task benchmark | `data/raw/problem_statement_task_samples.csv` T001–T005. Absolute errors 2, 7, 12, 2, 15 give **MAE 7.6 min**, recomputed from the file. |
| Four machine samples | `data/raw/problem_statement_machine_samples.csv`. Fuel per load cycle recomputes to 0.4333, 1.9, 0.61 and 2.0 L/cycle, as the plan states. |
| Legacy source | `data/raw/` and `data/cleaned_source/` keep `provided_unverified` origin, `timezone_status: unknown`, `random_1000` run labels and the `**` prefixes in the pasted operator-day text. |

Whether this local copy is byte-identical to the copy the data owner reviewed cannot be established from the checkout alone. The checksums are internally consistent, but no independent reference hash was supplied. **Action:** the data owner should confirm the `data/generated/manifest.json` SHA-256 `5d7de31c1856daf4179110a653d175891a102a53263356843792dd383f40e42d`, or commit the folder so Git records it.

## What v1 already supplies

| Plan input | v1 fields | Limits |
| --- | --- | --- |
| Engine / seatbelt | `engine_on`, `seatbelt_fastened`, `operating_state` | Minute summaries. They cannot prove that the belt state was observed before motion. |
| Idle | `idle_seconds`, `consecutive_idle_seconds`, `engine_run_minutes` | No idle-reason record ("waiting for truck" is runtime). |
| Fuel / cycles | `fuel_rate_lph`, `fuel_used_l`, `load_cycles` | No eligible-baseline policy yet. |
| Motion | `speed_kph` (per minute), `jerk` (dimensionless index), control counts | Too coarse for sudden starts/stops. Not acceleration. |
| Thermal / environment | `ambient` °C, `humidity_pct`, categorical `weather` (`sunny`, `cloudy`, `rainy`, `windy`) | No rain amount, wind speed, visibility or forecast times. |
| Wellbeing | `heart_rate_bpm`, `skin_temp_c`, `activity`, `break_seconds`, `minutes_since_break`, `shift_elapsed_min` | Synthetic. No sampling/quality/source profile, consent or WESAD provenance. |
| Task estimation | `task_type`, `work_quantity`/`work_unit`, `ground_condition`, `material`, `attachment`, `weather`, `operator_skill`, `machine_age_years`, outcomes | Completed history only. `estimated_duration_min` is a generator placeholder. `actual_duration_min` is a constructed outcome. |
| Service meter | `engine_hours` (cumulative, continuous) | No service records. |

## Required missing fields

Owner key: **Data** = dataset/rules stream; **Backend** = `langgraph-agent`; **Client** = Android/voice/supervisor. Type: `generated` = versioned synthetic extension; `runtime` = SQLite records created by workflows (never pre-filled CSV rows); `external` = a real source that must be obtained; `content` = authored/reviewed material. Provenance is the label every value must carry.

| ID | Entity / fields missing | Needed by | Owner | Type | Required provenance | Stage |
| --- | --- | --- | --- | --- | --- | --- |
| DG-01 | Sites, zones, timezone: `site_id`, `site_zone_id`, site timezone, approved coordinates, zone type, outdoor flag. Shift assignment to site. | REQ-01a/b, REQ-02d/f, ADD-05, ADD-17, auth scoping | Data + Backend | generated | `synthetic` with labelled demo locations | I03A |
| DG-02 | Scheduled (not completed) task assignments: scheduled order/start, site/zone, outdoor exposure, dependencies, resource needs, status, version, recorded progress. v1 `task_history` is completed history. The backend `tasks` table is 3 global seed rows not linked to any operator, machine or day. | REQ-01a/b/c, REQ-05a, ADD-05 | Data + Backend | generated seed + runtime state | `synthetic`; runtime progress from commands only | I03A, I07A |
| DG-03 | Proximity events: entity ID/type, `distance_m`, `bearing_deg`/direction, reference frame, warning/danger policy ID, observation time, quality, source. | REQ-02c, ADD-13 | Data | generated (simulated detector) | `synthetic`; missing detection means unknown | I03A, I05A |
| DG-04 | High-resolution motion/tilt: sub-minute timestamps, speed, acceleration + derivation interval, `pitch_deg`, `roll_deg`, optional grade % with units and source. | REQ-02a (before motion), REQ-04c, REQ-04d | Data | generated event stream | `synthetic`; never interpolated from 60 s rows | I03A, I05A |
| DG-05 | Human impact events: source/device/operator, peak acceleration g, duration, orientation. `impact_force_n` only with a documented physical model. v1 `impacts_count` is a control metric, not a fall. | ADD-06 | Data | generated | `synthetic` | I03A, I14 |
| DG-06 | Numeric conditions and forecasts: temperature °C, RH %, precipitation with accumulation interval, wind speed/gust m/s, visibility m, weather code, forecast issue/valid/retrieval time, location, provider, quality. | REQ-01a, REQ-02f, REQ-05a, ADD-04, ADD-05, SYS-07 | Backend (+ Data for replay fixtures) | external (Open-Meteo) + fixtures | `open-meteo` with timestamps, or `fixture`; replay aligned to data time | I09 |
| DG-07 | Operator profile / sleep: synthetic age where needed, experience + source, sleep duration/window/quality source, last break. `operators.csv` has only `operator_id` and `operator_skill`. | ADD-01, ADD-04, REQ-05a | Data | generated | `synthetic`; missing sleep means unknown, not zero | I03A, R01 |
| DG-08 | Vitals metadata and consent: sampling window, quality, source profile/version; collection consent and risk-sharing consent with purpose, notice version, effective and revoked times. | ADD-04, ADD-10, ADD-07 privacy | Data (vitals) + Backend (consent runtime) | generated + runtime | `synthetic` / `assumption_based` until I03B; demo consents labelled synthetic | I02, I03A, I03B |
| DG-09 | Rule and zone policy registry: per-rule thresholds with units, applicability, entry/reset/persistence/cooldown, citation or explicit demo assumption, review/version. Only one hard-coded prototype seatbelt rule exists. | REQ-02a/b/c/f, REQ-03c, REQ-04a–e, ADD-03/04/12, SYS-05 | Backend + Data | content (policy) | `published-guidance-based`, `site-configured` or `synthetic-demo-assumption` | I09, I10 |
| DG-10 | LMS resources: courses/modules/lessons, lesson text, **at least one genuinely playable approved video** with rights/version/checksum, levels, prerequisites, quizzes, assessment/completion policies. Only 3 title/summary/duration rows exist. | REQ-03a/b/c, ADD-11, ADD-18 | Backend + content author | content | Source and licence per asset | I12A |
| DG-11 | Runtime records: incident drafts/confirmations with structured fields, approvals, action ledger, SOS episodes/check-ins/timers, notifications, delivery/presentation receipts, offline command inbox, consents. | REQ-02b/d/e, REQ-04e, ADD-06/07/08/17, SYS-03/04/09 | Backend | runtime | Created by actual actions only | I02 onward |
| DG-12 | Service metadata: last-service meter/date, configured interval, model source, meter reset/replacement. | ADD-09 | Data | generated (configured) | No invented OEM schedules | S01 |
| DG-13 | Follow-on resources: detector outputs, manual corpus by model/version, walkaround checklist, score formula, instructor availability, branching scenarios. | ADD-01, ADD-14–18 | Data + content | content / external | Per-source licence and version | R01–R10 |
| DG-14 | Five-task benchmark inputs: rows lack quantity, ground condition, model identity and machine link. They must stay physically separate and must not be filled in. | REQ-05b | Backend | frozen reference | `provided_unverified` | I03B, I07B |
| DG-15 | WESAD: no WESAD file, metadata, checksum or derived profile exists anywhere in the dataset. | SYS-06, ADD-04 | Data | external | Real source version/checksum/terms, or visible `unmet` status | I03B |
| DG-16 | Session ↔ dataset binding: the backend never reads the dataset (no dataset-root setting) and accepts any `machine_id`/`operator_id` (I00 probe: `NO_SUCH_MACHINE` → 201). | REQ-01a, SYS-08, SYS-13 | Backend | runtime/config | Manifest version + hash recorded per session | I02, I03A |

## Source limitations that must stay visible

- All v1 generated values are synthetic (`is_simulated=true`). Machine ages, rates, productivity and temperatures are simulation assumptions, not Caterpillar specifications or thresholds.
- Legacy source histories (`EXC001`, `EXC002`, `OP1001`…) have unverified collection provenance, unknown timezone and unknown model identity. They are never relabelled as a Cat model.
- The screenshot CSVs are transcriptions. Their task-to-machine links are unknown.
- The provided five-row benchmark is a **provided benchmark**, not independent real-world ground truth. Four telemetry rows do not show that unbuckling causes fuel use.
- No real hardware, wearable, camera, BLE, weather provider or WESAD source has been integrated. Fixtures do not change that.
