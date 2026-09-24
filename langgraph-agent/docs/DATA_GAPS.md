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
| DG-01 | Sites, zones, timezone, shifts. **Partly filled for the demo (B1):** one synthetic site with 5 zones, a timezone/UTC offset and one shift per catalog machine/operator per seeded date (`demo/demo_site_v1.json`, provenance `synthetic_demo_fixture`, pinned catalog hash). No real site, coordinates or roster. | REQ-01a/b, REQ-02d/f, ADD-05, ADD-17, auth scoping | Data + Backend | generated | `synthetic` with labelled demo locations | I03A |
| DG-02 | Scheduled task assignments. **Partly filled for the demo (B1):** 10 seeded tasks with order, site-local start, zone, quantity/unit, conditions (synthetic) and a demo duration, plus a versioned lifecycle in SQLite. Not a real plan, no dependencies or resources. | REQ-01a/b/c, REQ-05a, ADD-05 | Data + Backend | generated seed + runtime state | `synthetic`; runtime progress from commands only | I03A, I07A |
| DG-03 | Proximity events: entity ID/type, `distance_m`, `bearing_deg`/direction, reference frame, warning/danger policy ID, observation time, quality, source. | REQ-02c, ADD-13 | Data | generated (simulated detector) | `synthetic`; missing detection means unknown | I03A, I05A |
| DG-04 | High-resolution motion/tilt: sub-minute timestamps, speed, acceleration + derivation interval, `pitch_deg`, `roll_deg`, optional grade % with units and source. **Partly filled for the demo (B3):** small generated second-level engine/belt/state/speed change events (`cocoon_agent/simulation.py`, provenance `synthetic_scenario`); no acceleration or tilt. | REQ-02a (before motion), REQ-04c, REQ-04d | Data | generated event stream | `synthetic`; never interpolated from 60 s rows | I03A, I05A |
| DG-05 | Human impact events: source/device/operator, peak acceleration g, duration, orientation. `impact_force_n` only with a documented physical model. v1 `impacts_count` is a control metric, not a fall. | ADD-06 | Data | generated | `synthetic` | I03A, I14 |
| DG-06 | Numeric conditions and forecasts: temperature °C, RH %, precipitation with accumulation interval, wind speed/gust m/s, visibility m, weather code, forecast issue/valid/retrieval time, location, provider, quality. | REQ-01a, REQ-02f, REQ-05a, ADD-04, ADD-05, SYS-07 | Backend (+ Data for replay fixtures) | external (Open-Meteo) + fixtures | `open-meteo` with timestamps, or `fixture`; replay aligned to data time | I09 **C2 (partly closed):** live Open-Meteo adapter (verified once) and a synthetic site-local fixture (`demo/weather_fixture_v1.json`) aligned to the data clock; site coordinates are a labelled synthetic demo location. Historical replay weather is still missing (a live lookup for past data is `misaligned`). |
| DG-07 | Operator profile / sleep: synthetic age where needed, experience + source, sleep duration/window/quality source, last break. `operators.csv` has only `operator_id` and `operator_skill`. | ADD-01, ADD-04, REQ-05a | Data | generated | `synthetic`; missing sleep means unknown, not zero | I03A, R01 |
| DG-08 | Vitals metadata and consent: sampling window, quality, source profile/version; collection consent and risk-sharing consent with purpose, notice version, effective and revoked times. **D1:** consent runtime done (v13, synthetic demo notices `policies/consent_notices_v1.json`, not privacy-reviewed); samples carry window, quality, source and `simulated`; no real source profile. | ADD-04, ADD-10, ADD-07 privacy | Data (vitals) + Backend (consent runtime) | generated + runtime | `synthetic` / `assumption_based` until I03B; demo consents labelled synthetic | I02, I03A, I03B |
| DG-09 | Rule and zone policy registry: per-rule thresholds with units, applicability, entry/reset/persistence/cooldown, citation or explicit demo assumption, review/version. **Partly filled for the demo (B3):** `policies/safety_policy_v1.json` versions three rules (belt+engine, 300 s idle, 60 s idle unbelted) with applicability and `source_status: demo_assumption`. No published thresholds, zones or reviewer. | REQ-02a/b/c/f, REQ-03c, REQ-04a–e, ADD-03/04/12, SYS-05 | Backend + Data | content (policy) | `synthetic-demo-assumption` for now | I09, I10 |
| DG-10 | LMS resources: courses/modules/lessons, lesson text, **at least one genuinely playable approved video** with rights/version/checksum, levels, prerequisites, quizzes, assessment/completion policies. Only 3 title/summary/duration rows exist. | REQ-03a/b/c, ADD-11, ADD-18 | Backend + content author | content | Source and licence per asset | I12A |
| DG-11 | Runtime records: incident drafts/confirmations with structured fields, approvals, action ledger, SOS episodes/check-ins/timers, notifications, delivery/presentation receipts, offline command inbox, consents. | REQ-02b/d/e, REQ-04e, ADD-06/07/08/17, SYS-03/04/09 | Backend | runtime | Created by actual actions only | I02 onward |
| DG-12 | Service metadata: last-service meter/date, configured interval, model source, meter reset/replacement. | ADD-09 | Data | generated (configured) | No invented OEM schedules | S01 |
| DG-13 | Follow-on resources: detector outputs, manual corpus by model/version, walkaround checklist, score formula, instructor availability, branching scenarios. | ADD-01, ADD-14–18 | Data + content | content / external | Per-source licence and version | R01–R10 |
| DG-14 | Five-task benchmark inputs: rows lack quantity, ground condition, model identity and machine link. They must stay physically separate and must not be filled in. | REQ-05b | Backend | frozen reference | `provided_unverified` | I03B, I07B |
| DG-15 | WESAD: no WESAD file, metadata, checksum or derived profile exists anywhere in the dataset. **Re-checked in D1 (2026-09-24): still absent, no teammate artefact; status `unmet`.** D1 uses assumption-based synthetic samples only and never calls a profile WESAD-derived. | SYS-06, ADD-04 | Data | external | Real source version/checksum/terms, or visible `unmet` status | I03B |
| DG-16 | Session ↔ dataset binding. **Partly closed in I02a:** the backend reads the catalog read-only from `DATASET_ROOT`, verifies the pinned manifest hash, rejects unknown IDs for new sessions and stores the manifest hash per session. Still open: no reviewed snapshot (the pin is a provisional local dev snapshot), and no site/shift data to bind (DG-01). | REQ-01a, SYS-08, SYS-13 | Backend + Data | runtime/config | Manifest version + hash recorded per session (done for new sessions) | I02a (done), I03A |

## Contract status after I01

I01 defines the **shape** of each missing input in the proposed contract (`contracts/proposed/`). It supplies no data: every value in those fixtures is a synthetic contract example. Each gap stays open until its stage produces real, provenance-labelled data.

| ID | Contract defined in I01 | Data still missing (owner, stage unchanged) |
| --- | --- | --- |
| DG-01 | `site_id`, `site_zone_id`, `site_timezone`, `outdoor_exposure` fields in `TaskAssignment` / `DailyTaskDashboard` / `SessionTarget` | Real or labelled synthetic site/zone/shift tables (I03A) |
| DG-02 | `TaskAssignment` (order, start, exposure, dependencies, lifecycle, version, progress) | Scheduled assignments in the dataset/runtime seed (I03A, I07A) |
| DG-03 | `ProximityObservation` (entity, `distance_m`, `bearing_deg`, reference frame, detection state, uncertainty) | Simulated detector stream (I03A, I05A) |
| DG-04 | `MotionObservation` (`speed_mps`, `accel_mps2` + derivation interval, `pitch_deg`, `roll_deg`, `grade_pct`) | High-rate event stream (I03A, I05A) |
| DG-05 | `HumanImpactObservation` (distinct from `control_impacts_count_interval`; force only with a model) | Impact scenarios (I03A, I14) |
| DG-06 | `EnvironmentObservation`, `ConditionsSnapshot` (issue/valid/retrieval/available times, precipitation interval) | Open-Meteo client and replay fixtures (I09) |
| DG-07 | `SleepSummary` (null = unknown); `LearnerProfile.dataset_operator_skill` kept separate | Age/sleep/experience fields (I03A, R01) |
| DG-08 | `VitalsObservation`, `ConsentRecord`/`ConsentChangeRequest`, `OperatorWellbeingView`, `SupervisorRiskView` | Vitals metadata; consent runtime (I02); WESAD shaping (I03B) |
| DG-09 | `RulePolicyRef` (units, applicability, persistence/reset/cooldown, `evidence_status`, citation required for published guidance) | Actual policies and any real citations (I09, I10) |
| DG-10 | `ContentAsset` (availability, checksum, licence), `LessonVersion`, `GuidedStep`, `QuizAttempt`, `LessonProgress` | Approved lesson text and at least one real playable video (I12A) |
| DG-11 | Incidents, approvals, action results, SOS, commands, presence, presentation, delivery records | Runtime persistence (I02 onward) |
| DG-12 | Only `engine_hours_meter` | Service records (S01) |
| DG-13 | Not defined (follow-on) | R01–R10 |
| DG-14 | `DurationEstimate` method `reduced_inputs_fallback` with `missing_inputs` | Frozen benchmark and estimator (I03B, I07B) |
| DG-15 | `Provenance.origin` includes `assumption_based`; no WESAD artefact format yet | WESAD source, checksum and processing (I03B) |
| DG-16 | Catalog `MachineId`; target 422 `unknown_machine`/`unknown_operator`; `SessionTarget.dataset_manifest_sha256` | Catalog loading and enforcement (I02, I03A) |

## Local dataset snapshot recorded in I01 (re-verified and pinned in I02a)

Observed on 2026-09-24 in the working tree, read-only. `Cocoon_Dataset_v1/` is still **untracked** on `backend`, and I01 left it unstaged and unchanged.

| Item | Value |
| --- | --- |
| Manifest | `schema_version 1.0`, `generator_seed 20260923`, `dataset_origin synthetic` |
| `data/generated/manifest.json` SHA-256 | `5d7de31c1856daf4179110a653d175891a102a53263356843792dd383f40e42d` |
| Snapshot fingerprint (SHA-256 of `CHECKSUMS.sha256`) | `bdd55830094dcb413daa1ee8858f4240d8a57b225d63cc27b6288d311bd2b3d1` |
| Checksum verification | `sha256sum -c CHECKSUMS.sha256`: 33/33 OK |

I02a recomputed both digests with the same procedure (identical values) and pinned the manifest digest as `DATASET_MANIFEST_SHA256` (see `MIGRATIONS.md`). The backend verifies it at every start. The package fingerprint stays documentation only.

This fingerprint identifies a **local, checksum-validated snapshot**. It is not a reviewed Git revision and not confirmation from the data owner. Before I03 extends the dataset, the data owner still needs to review and version it (commit it or publish a reviewed hash).

## Source limitations that must stay visible

- All v1 generated values are synthetic (`is_simulated=true`). Machine ages, rates, productivity and temperatures are simulation assumptions, not Caterpillar specifications or thresholds.
- Legacy source histories (`EXC001`, `EXC002`, `OP1001`…) have unverified collection provenance, unknown timezone and unknown model identity. They are never relabelled as a Cat model.
- The screenshot CSVs are transcriptions. Their task-to-machine links are unknown.
- The provided five-row benchmark is a **provided benchmark**, not independent real-world ground truth. Four telemetry rows do not show that unbuckling causes fuel use.
- No real hardware, wearable, camera, BLE, weather provider or WESAD source has been integrated. Fixtures do not change that.
