# Cocoon backend feature matrix

Authority: `BACKEND_IMPLEMENTATION_PLAN.md` revision 2.1, section 2 (filled Review 1 form, fields F10–F16).
Baseline audited in I00 on 2026-09-24 against branch `backend`, base commit `ee83254`. Contract readiness added in I01 on 2026-09-24 (base `f8afb4d`).

## How to read this file

- **Status** uses the plan's ledger values: `not_started`, `in_progress`, `verified`, `blocked`, `deferred_with_reason`. `verified` requires the plan's acceptance criterion to pass with recorded evidence. A mock fixture, a contract entry or a screenshot alone never makes a row `verified`.
- **Existing baseline** records only behaviour observed in this checkout during I00, by running the backend tests and the HTTP smoke check (see [HANDOFF.md](HANDOFF.md)). "Partial" means some related code exists but the plan's requirement is not met.
- **Tier:** `core` = five required groups plus the seven cross-cutting additions scheduled in F16; `stretch` = S01, gated on Checkpoint 2; `roadmap` = technologies F11 names as roadmap; `follow-on` = other F14 entries delivered after the core gates.
- **Data prerequisite** refers to the gap IDs in [DATA_GAPS.md](DATA_GAPS.md). "v1" means the prepared `Cocoon_Dataset_v1` already carries the input, subject to the limits recorded there.
- Other owners: **Voice** = `livekit-voice/` owner; **Android** = Cocoon-App (separate repo); **Supervisor UI** = React/Tailwind client; **Data** = dataset/rules stream.

No row below is `verified` at I00. The existing backend is a working v1 voice-contract prototype (7 JSON routes, mock/live router, SQLite, one prototype seatbelt rule). It predates the filled form, so it does not satisfy any form requirement in full.

I02a (2026-09-24) implemented versioned migrations and catalog-bound session creation (SYS-09 and SYS-13 progress). No form requirement is verified by it. I01 froze a **proposed** target contract for these requirements (see [Contract readiness after I01](#contract-readiness-after-i01)). Contract readiness is tracked separately: a schema, fixture or passing contract check never changes a row's implementation status.

## Five required outcomes

| ID | Source | Tier | Backend component | Other owner | Data prerequisite | Stage | Status | Existing baseline (I00 evidence) | Acceptance still required |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| REQ-01a | F14.1 daily task dashboard | core | task read model, `/state` projection | Android (screen) | DG-01, DG-02, DG-06 | I03, I07A | not_started | Partial. `tasks` table holds 3 hard-coded seed rows (T-101..T-103). They are global: no operator, machine, site, zone, day, predicted duration, weather or version. `next_task` returns the first pending row. | Today's tasks for the bound operator/machine/site with zone, prediction, weather, state and version. |
| REQ-01b | F14.1/F16 spoken shift briefing | core | briefing episode + announcement outbox | Voice (playback) | DG-01, DG-02, DG-06 | I07A, I11 | not_started | None. Announcement types are only `alert_started` and `alert_cleared`. | Once per operator/shift/version, deferred delivery, expiry, no repeat after restart. |
| REQ-01c | F14.1 start/complete by voice or tap | core | shared command service | Android (tap), Voice | DG-02 | I04, I07A, I15 | not_started | None. Task `status` enum exists in the schema, but no route or graph node changes it. | Voice and tap reach the same validated command; version checks; progress persisted once. |
| REQ-02a | F14.2 seatbelt before motion | core | deterministic rule engine, telemetry ingestion | Voice (announcement) | v1 (minute cadence); DG-04 for motion ordering | I05A, I10 | not_started | Partial. `prototype.seatbelt_unfastened.v1` opens one episode on engine-on + unfastened, clears and retriggers (`test_alert_episode_announces_once_and_resets`). Telemetry must be `simulated: true`. No motion signal, rule registry or published source. | Episode rule with versioned policy; fixture where warning precedes motion; measured backend latency. |
| REQ-02b | F14.2 repeat violations escalated | core | episode ledger, window counter, approval request | Supervisor UI | DG-09, DG-11 | I10, I13 | not_started | None. Episodes are stored, but nothing counts them or escalates. | Distinct episodes counted in a versioned window; scoped supervisor escalation with evidence. |
| REQ-02c | F14.2 proximity hazards | core | proximity rule family | Android/detector (acquisition) | DG-03 | I05A, I10 | not_started | None. v1 has no proximity data, and the dataset's own `feature_coverage.csv` lists it as missing inputs. | Warning/danger state per entity with distance/direction; crossing, clearing, stale/unknown. |
| REQ-02d | F14.2 structured incident report | core | incident service | Android (list) | DG-01, DG-11 | I06 | not_started | Partial. An incident stores `description` only, plus session/operator/machine/turn IDs. A real ID (`INC-0001`) is returned after commit. Idempotent on `(session_id, source_turn_id)`. Follow-up asks for a missing description. No what/where/when/severity/site zone, telemetry refs, draft or confirmation. | Structured fields, clarification of missing facts, trusted refs, idempotent confirmation. |
| REQ-02e | F14.2/F16 automatic incident drafts | core | episode→draft link | Android (confirm/edit) | DG-11 | I06, I10 | not_started | None. | One linked draft per safety episode; operator confirms/edits/dismisses; never silently confirmed. |
| REQ-02f | F14.2 working-condition warnings | core | condition checks before outdoor task start | Android | DG-06, DG-01, DG-09 | I07A, I09, I10 | not_started | None. v1 has categorical `weather`, `humidity_pct` and ambient °C only. | Numeric temperature/RH/rain/wind/visibility checks with forecast times and task exposure. |
| REQ-03a | F14.3 text/video training, voice quizzes | core | LMS content, quiz attempts | Android (playback) | DG-10 | I12A, I12B | not_started | Partial. 3 seeded lessons (L1–L3) with title, summary and duration only. Assign and status actions exist. No lesson text, video, quiz or progress. | Versioned curriculum, real playable approved video, scored attempts, saved progress. |
| REQ-03b | F14.3 Beginner/Intermediate/Expert levels | core | progression policy | Android | DG-10 | I12B | not_started | None. `operator_skill` in v1 is dataset skill, not learning level. | Explicit criteria and persisted evidence, separate from certification and dataset skill. |
| REQ-03c | F14.3 behaviour-triggered lessons | core | episode→assignment policy | Voice (timing) | DG-09, DG-10 | I10, I12C | not_started | None. Assignment is only by request. Uniqueness is `(operator_id, lesson_id)`, so an assignment can never be repeated or refreshed. | Idempotent policy-authorised assignment linked to the episode, delivered at a safe time. |
| REQ-04a | F14.4 prolonged idling; idle + unbelted | core | idle and combined rules, idle-reason record | Voice | v1 (`consecutive_idle_seconds`, `seatbelt_fastened`, `engine_on`); DG-09 | I10, I11 | not_started | None. `idle_seconds` is accepted in telemetry but no rule reads it. | Duration and combined rules; "waiting for truck" is recorded as a reason and does not clear the condition. |
| REQ-04b | F14.4 abnormal fuel per load cycle | core | fuel/cycle rule with baseline | — | v1 (`fuel_used_l`, `load_cycles`); DG-09 | I10 | not_started | None. | Compatible windows, nonzero cycles, coverage, eligible baseline, no division by zero. |
| REQ-04c | F14.4 sudden starts/stops | core | motion-change rule | Android/device | DG-04 | I05A, I10 | not_started | None. v1 `speed_kph` is one value per minute and `jerk` is a dimensionless index. Neither detects a sudden start. | High-frequency timestamped speed/acceleration with units and quality. |
| REQ-04d | F14.4 steep slopes | core | tilt rule | Android/device | DG-04, DG-09 | I05A, I10 | not_started | None. v1 has no pitch, roll or grade. | Pitch/roll/grade under model/site policy; no invented universal limit. |
| REQ-04e | F14.4 repeated violations | core | episode trend counters, coaching | Supervisor UI | DG-09, DG-11 | I10, I12C, I13 | not_started | None. | Episode-based windows, coaching and escalation, not sample counts. |
| REQ-05a | F14.5 contextual task-time estimate | core | estimator + explanation | — | v1 (`task_type`, `work_quantity`, `ground_condition`, `weather`, `operator_skill`, `machine_age_years`); DG-06 for numeric weather | I07B | not_started | None. v1 `estimated_duration_min` is a generator placeholder (`demo_quantity_divided_by_nominal_productivity`), not a backend estimator. | Reproducible estimator reporting duration, evidence and contributing factors. |
| REQ-05b | F14.5/F16 five-task benchmark | core | frozen evaluation | — | DG-14 | I03B, I07B | not_started | Input verified. `data/raw/problem_statement_task_samples.csv` holds T001–T005. Recomputed in I00: absolute errors 2, 7, 12, 2, 15 give **MAE 7.6 min**. No estimator exists yet. | Freeze rows and estimator version, report every prediction/error, declare influence. |

## Additional entries in field 14

| ID | Entry | Tier | Backend component | Other owner | Data prerequisite | Stage | Status | Existing baseline (I00 evidence) | Acceptance still required |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ADD-01 | Camera drowsiness + sleep/shift fatigue | roadmap | detector-event ingestion, fatigue advisory | Android (camera/MediaPipe) | DG-07, DG-13 | R01 | not_started | None. v1 `jerk` is not a drowsiness signal. | Consented, labelled detector events; unknown sleep handled; no diagnosis. |
| ADD-02 | Hands-free voice, noise cancellation, PTT | core (shared) | finalized-text turn contract, streaming, recovery | Voice (AssemblyAI, Cartesia, LiveKit, Krisp, VAD, PTT) | — | I04, I08, I11, I16 | not_started | Partial. The JSON turn contract is idempotent with 200/202/409 (`test_retrying_same_turn_does_not_repeat_actions`, `test_duplicate_while_processing_returns_202_then_result`). No streaming route: `POST .../turns/stream` returned 405 in the I00 probe. The voice worker is standalone and **not connected** to this backend (root README, `livekit-voice/CLAUDE.md`). | Streaming turns, cancellation and delivery; actual joint audio checks. |
| ADD-03 | Explainable alerts and "Why?" | core | alert evidence + linked follow-up | Voice | DG-09 | I10–I11 | not_started | Partial. `alert_explained` explains the latest stored alert for the session. `trigger_readings` are persisted with the alert (`test_why_after_announcement_uses_stored_alert`; the smoke run heard the explanation). No rule version or recommended action is stored, and the lookup is "latest alert", not an explicit reference. | Versioned evidence persisted before the announcement; explicit alert reference; later telemetry cannot replace it. |
| ADD-04 | Heat stress, breaks, consent/privacy | core | wellbeing rules, consent, risk-only projection | Supervisor UI | DG-06, DG-08, DG-09 | I02, I09–I10, I13 | not_started | None in the backend. v1 carries synthetic `heart_rate_bpm`, `skin_temp_c`, `break_seconds` and `minutes_since_break`. No consent records. | Consent-gated advisory; supervisor sees risk only; adversarial privacy checks. |
| ADD-05 | Weather-aware re-planning | core | proposal + approval + atomic apply | Supervisor UI | DG-02, DG-06 | I09, I13 | not_started | None. | Forecast-backed proposal; approval before application; reject/expire/stale/duplicate paths. |
| ADD-06 | Man-down SOS | core | SOS episode state machine, timers | Android, Voice | DG-05 | I14 | not_started | None. v1 `impacts_count` is hydraulic/control impacts, not a human-fall signal. | Impact → check-in → timer → scoped alert; response, no-response, unreachable, restart and race paths. |
| ADD-07 | Supervisor view | core (backend) | role-scoped read models, decision APIs | Supervisor UI (React/Tailwind) | DG-11 | I01–I02, I13 | not_started | None. The only principal is one shared service token. No roles, sites or supervisor routes (`GET /v1/me` → 404 in probe). | Scoped supervisor auth; no raw vitals; no cross-site access. |
| ADD-08 | Poor-connectivity mode | core (shared) | versioned snapshots, command inbox, presentation receipts | Android (cache/vibration) | DG-11 | I15–I16 | not_started | Partial foundation only. `state_version` exists, and events are retained and cursor-paged without consuming them. No command inbox, presence or presentation reports. | Disconnect/queue/reconnect/duplicate upload with the original association. |
| ADD-09 | Engine-hour service countdown | stretch | service schedule + reminders | — | DG-12 | S01 | deferred_with_reason | None. v1 has a cumulative `engine_hours` meter, but no service records. Gated on Checkpoint 2 being on time (F16). | Configured model-specific intervals; meter-reset cases. |
| ADD-10 | Real smartwatch vitals | roadmap | wearable adapter, consent | Android (Health Connect) | DG-08 | R02 | deferred_with_reason | None. v1 vitals are synthetic. Roadmap per F11. | Real-device trace or explicit unverified status. |
| ADD-11 | Hindi, Tamil and more | roadmap | language-aware content/intent | Voice (provider support) | DG-10 | R03 | deferred_with_reason | None. The turn request has no `language` field. Roadmap per F11. | Reviewed multilingual fixtures plus real speech checks. |
| ADD-12 | On-device safety without network | follow-on | portable rule package, reconciliation | Android (local execution) | DG-09 | R04 | deferred_with_reason | None. Follows the core gates (plan section 1). | Airplane-mode device test. |
| ADD-13 | Bluetooth proximity between phones | roadmap | BLE observation ingestion | Android (BLE) | DG-03 | R05 | deferred_with_reason | None. Roadmap per F11. | Calibrated distance with uncertainty; RSSI kept separate. |
| ADD-14 | Voice pre-shift walkaround | follow-on | checklist workflow | Voice, Android | DG-13 | R06 | deferred_with_reason | None. T-101 "Pre-start walkaround inspection" is a seed task title, not a checklist workflow. | Fail → repair → recheck; restart/resume. |
| ADD-15 | Operator-manual Q&A | follow-on | cited retrieval | — | DG-13 | R07 | deferred_with_reason | None. No manual corpus. | Known/unknown/model-mismatch answers with citations. |
| ADD-16 | Safety/efficiency score, fuel saved | follow-on | transparent metrics | Android | DG-13 | R08 | deferred_with_reason | None. | Hand-calculated fixtures; no fabricated savings. |
| ADD-17 | End-of-shift handover reports | follow-on | once-per-shift report | Supervisor UI | DG-01, DG-11 | R09 | deferred_with_reason | None. | Restart, incomplete shift, corrections. |
| ADD-18 | Instructor booking + scenario simulation | core LMS scenarios; follow-on booking | LMS practice scenarios; booking workflow | Android | DG-10, DG-13 | I12B, R10 | not_started | None. | Authored scenarios in the core LMS; simulated bookings clearly labelled. |

## Cross-cutting commitments

| ID | Requirement / source | Tier | Stage | Status | Existing baseline (I00 evidence) | Acceptance still required |
| --- | --- | --- | --- | --- | --- | --- |
| SYS-01 | Initial LLM classifier, four branches (F10/F12/F15) | core | I04 | not_started | Partial. The graph is `load_context → route → {next_task, log_incident, training, explain_alert, cancel_pending} → compose`. `route` picks one of 7 intents. It is not the four-branch `resolve_context → classify_intent → route_intent` design, and it has no multi-intent support. | Four branches, typed decision, labelled examples, same mock/live schema. |
| SYS-02 | System-event routing (F12) | core | I04, I10–I11 | not_started | Partial. The seatbelt alert is detected by a deterministic rule without any LLM call. No semantic follow-up route per episode. | At most one semantic run per episode; alert never waits for it. |
| SYS-03 | One request, several actions (F12) | core | I04, I13 | not_started | None. Each turn produces exactly one action. | Ordered child actions, partial outcomes, approval pauses. |
| SYS-04 | Approval for actions affecting others (F12) | core | I02, I13–I14 | not_started | None. | Trusted supervisor, versioned proposal, decision, expiry, exact-once apply. |
| SYS-05 | Rules, not LLM safety detection (F10/F12) | core | I09–I10 | not_started | Partial. The single rule is Python code, labelled PROTOTYPE, with one env toggle (`COCOON_SEATBELT_RULE_REQUIRES_ENGINE_ON`). There is no versioned registry, citation or applicability. | Versioned policy registry with evidence status per parameter. |
| SYS-06 | Data extension + WESAD (F11/F13) | core | I03A–I03B | not_started | v1 verified intact (hashes, 12/12 dataset tests). No extension and no WESAD artefact exist: no file in the dataset mentions WESAD. | Versioned extension; real WESAD processing trace or visible unmet status. |
| SYS-07 | Open-Meteo (F11) | core | I09 | not_started | None. No weather client or fixture mode. | Cached, site-aware numeric forecasts with issue/valid/retrieval times. |
| SYS-08 | Synthetic labelling and simulator (F10/F13) | core | I03A, I05B, I10 | not_started | Partial. `scripts/simulate_telemetry.py` posts hand-built seatbelt samples. The dataset's `cocoon_data.py replay` emits JSON without contacting the backend. The two are not connected, and the backend does not import the dataset. | Replay from the dataset with an explicit simulation clock and preserved provenance. |
| SYS-09 | Authoritative state / idempotency (F13) | core | I02, I08 | in_progress | Partial. v1 scope as before: SQLite uniqueness on session key, `(session_id, turn_id)`, `(session_id, event_id)`, the incident turn and one active episode per rule; restart survival tested. **I02a adds:** versioned migrations (`schema_migrations`, v2) with atomic steps and refusal of unknown/newer layouts; immutable session associations (DB trigger); DB-serialised session creation tested with concurrent creators on separate connections; an orderly two-process restart check (one session, one incident, identical saved result). Still missing: action/command ledger, recovery scan, actor scoping. Not exactly-once. | Action/command ledger and recovery (later I02 slices). |
| SYS-10 | Android/voice/supervisor integration (F11/F13) | core | I01, I13, I15–I16 | not_started | Partial. The shared contract is enforced by `contracts/openapi.yaml` and 16 examples (`export_openapi.py --check` passes; `test_contract.py` passes). The voice worker holds its own client copy but is not wired to the backend. No Android or supervisor contract. | Frozen schemas and fixtures for every client feature (I01). |
| SYS-11 | Checkpoint 1 (F13/F16) | core | I06, I08, I16 | not_started | Backend half partially demonstrated in **mock mode over HTTP only**: the smoke run stored `INC-0001` and returned confirmation speech. No audio and no Android app. | Spoken request → real stored incident → spoken confirmation → exactly one incident in the app. |
| SYS-12 | Checkpoint 2 (F13/F16) | core | I10–I11, I16 | not_started | Backend half partially demonstrated in **mock mode over HTTP only**: simulated telemetry created one announcement, and "Why did you warn me?" returned the stored explanation. No audio playback. | Silent telemetry → unsolicited spoken warning → correct persisted-context "Why?". |
| SYS-13 | Low cost / CAT ecosystem (F10/F14) | core | I03, I10 | in_progress | Partial. No LLM is called per telemetry sample. **I02a:** new sessions must use exact catalog asset/operator IDs from a verified, pinned catalog snapshot (422 `unknown_machine`/`unknown_operator`; no aliases), and each session stores the snapshot hash. The catalog is a provisional local dev snapshot, not data-owner reviewed. No site/source IDs yet. Product Link/VisionLink, Cat Detect and Cat Inspect are **not** integrated. | Reviewed catalog; canonical site/source IDs (I03); caches and episode dedup (I10). |
| SYS-14 | Git ownership and team review (F11/F15) | core | every increment | in_progress | I00 commit authored and committed as Naif Naqeeb (see HANDOFF.md). Colleague paths (`livekit-voice/`, `Cocoon_Dataset_v1/`) untouched and unstaged. | Scoped commits with evidence at every increment. |
| SYS-15 | Milestones and 15-hour schedule (F15/F16) | core | I00, I16–I17 | in_progress | I00 separates verified behaviour (this file) from form-reported milestones. The form's reported milestones are **not** verified by this audit beyond the artefacts listed in HANDOFF.md. | Checkpoints and final noise/reconnect/demo checks recorded with real evidence. |

## Plan versus checkout mismatches found in I00

These are recorded, not fixed, in I00. The stage that resolves each one is named.

| Plan expectation | Actual checkout | Resolve in |
| --- | --- | --- |
| Live model is Vertex AI via ADC (`GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `VERTEX_MODEL`); no other provider key (plan section 7). | Live mode uses the Anthropic SDK (`AnthropicBrain`, `ANTHROPIC_API_KEY`, `COCOON_LLM_MODEL=claude-opus-5`). No Vertex dependency is in `requirements.txt`. | **Decided (I01): Vertex is the selected provider.** The Anthropic code is an implementation mismatch. It is replaced in I04, including model/version/access verification. The HTTP contract is provider-independent. |
| Setting names `AGENT_MODE`, `DATABASE_PATH`, `DATASET_ROOT`, `SSE_*`, `TURN_DEADLINE_SECONDS`. | Existing names are `COCOON_LLM_MODE`, `COCOON_DATA_DIR`, `COCOON_TURN_TIMEOUT_SECONDS` etc. | **Partly done (I02a):** `DATASET_ROOT` implemented with the plan's name, plus `DATASET_MANIFEST_SHA256` and `SESSION_BINDINGS_PATH`. The other names keep their compatible `COCOON_*` form; mapping to be documented with the settings table in later I02 slices. |
| Unknown assets fail; session bound to a dataset machine/operator/shift/site. | Before I02a any non-empty `machine_id`/`operator_id` was accepted (201). | **Implemented (I02a):** 422 `unknown_machine` / `unknown_operator` for new sessions; the manifest hash is stored per session; site/shift only from a trusted binding, otherwise `context_status: unavailable`. Legacy sessions stay `legacy_unverified`. Tasks are still not bound to operator or machine (I07A). |
| Versioned SQLite migrations. | Before I02a: `CREATE TABLE IF NOT EXISTS` on every start, no versioning. | **Implemented (I02a):** `schema_migrations` ledger, v1 baseline adoption, v2 catalog-bound sessions; see `docs/MIGRATIONS.md`. |
| Twelve session/turn/event routes (plan section 6). | Seven JSON `/v1` routes exist. The five streaming/cancel/delivery routes do not. | **Contract done (I01):** the five routes are specified in `contracts/proposed/openapi.json` as `proposed`, and the plan wording is corrected. Implemented in I08. |
| `docs/FEATURE_MATRIX.md`, `HANDOFF.md`, `RECOVERY.md`, `MIGRATIONS.md`, `DEMO_RUNBOOK.md`. | Before I00 none existed. I00 adds this file, `DATA_GAPS.md` and `HANDOFF.md` under `langgraph-agent/docs/`. | I00 (this), later stages for the rest |
| Root README describes the current phase as branch `voice`. | The `backend` branch contains the merged voice work (`ee83254`). | **Fixed (I01):** narrow README edit. Voice setup text is unchanged. |

## Contract readiness after I01

`contract_proposed` means typed models, generated schemas and fixtures exist in `contracts/proposed/` and pass `tests/test_proposed_contract.py`. `partial_contract` means only some inputs are specified. `not_in_I01` means the contract belongs to the row's follow-on stage. **None of this is implementation.** Every row keeps its implementation status above.

| ID | Contract | Main contract models (in `cocoon_agent/contract/`) |
| --- | --- | --- |
| REQ-01a | contract_proposed | `DailyTaskDashboard`, `TaskAssignment`, `ConditionsSnapshot`, `SessionStateTarget.dashboard` |
| REQ-01b | contract_proposed | `AnnouncementEnvelope` type `shift_briefing` with `correlation.briefing_id` |
| REQ-01c | contract_proposed | `Command` kinds `task.start`/`task.complete`, `CommandResult`, `PublicActionResult` `start_task`/`complete_task` |
| REQ-02a | contract_proposed | `MachineStateObservation`, `MotionObservation`, `RulePolicyRef` family `seatbelt_engine_on`, `OperatorAlertView` |
| REQ-02b | contract_proposed | `ApprovalRecord` + `EscalationPayload` (distinct episodes, window, rule version) |
| REQ-02c | contract_proposed | `ProximityObservation` (distance, bearing, frame, detection state), family `proximity` |
| REQ-02d | contract_proposed | `IncidentReport` (what/where/when/severity with reported/inferred basis), `incident.*` commands |
| REQ-02e | contract_proposed | `IncidentReport` origin `auto_draft_from_episode`, `OperatorAlertView.linked_incident_draft_id` |
| REQ-02f | contract_proposed | `ConditionsSnapshot`, `ConditionCheck`, `EnvironmentObservation` |
| REQ-03a | contract_proposed | `ContentAsset`, `LessonVersion`, `GuidedStep`, `QuizAttempt`, `LessonProgress`, `GET /v1/content/{asset_id}` |
| REQ-03b | contract_proposed | `LearnerProfile`, `LevelEvidence` (separate from `dataset_operator_skill`) |
| REQ-03c | contract_proposed | `LessonAssignment` origin `behaviour_policy` with policy and episode |
| REQ-04a | contract_proposed | idle fields of `MachineStateObservation`, `IdleReasonPayload`, families `prolonged_idle`/`idle_unbelted` |
| REQ-04b | contract_proposed | `fuel_used_l_interval`, `load_cycles_interval`, family `fuel_per_load_cycle` |
| REQ-04c | contract_proposed | `MotionObservation` acceleration with derivation interval, family `motion_change` |
| REQ-04d | contract_proposed | `pitch_deg`/`roll_deg`/`grade_pct`, family `slope` |
| REQ-04e | contract_proposed | family `repeat_violation`, `EscalationPayload`, behaviour-policy `LessonAssignment` |
| REQ-05a | contract_proposed | `DurationEstimate` (method, input cutoff, factors, missing inputs), `DurationFactor` |
| REQ-05b | contract_proposed | `DurationEstimate` `reduced_inputs_fallback` for rows without quantity/ground/model |
| ADD-01 | partial_contract | `camera_drowsiness` consent purpose and `SleepSummary` only. Detector events are R01. |
| ADD-02 | contract_proposed | `TurnRequestTarget`, `TurnStreamEvent`, `CancelRequest`, `TurnDeliveryReport`, `AnnouncementEnvelope`, chunked SSE fixture |
| ADD-03 | contract_proposed | `OperatorAlertView` (immutable `trigger_evidence`, rule version), `AlertExplanationDetails` |
| ADD-04 | contract_proposed | `VitalsObservation`, `OperatorWellbeingView`, `SupervisorRiskView`, `ConsentRecord`/`ConsentChangeRequest` |
| ADD-05 | contract_proposed | `ScheduleProposalPayload`, `ApprovalRecord`, `DecisionRequest`/`DecisionResult` |
| ADD-06 | contract_proposed | `HumanImpactObservation`, `SosEpisode`, `ChannelOffer`, `SupervisorNotification`, `sos.respond` |
| ADD-07 | contract_proposed | `MeResponse`, `SupervisorOverview`, `SupervisorFeedEvent`, approval routes |
| ADD-08 | contract_proposed | `Command`/`CommandResult` (original binding, duplicate, conflict), `PresenceReport`, `PresentationReport` |
| ADD-09 | not_in_I01 | S01; only the `engine_hours_meter` observation field exists |
| ADD-10 | partial_contract | `VitalsObservation` with device provenance; the wearable adapter is R02 |
| ADD-11 | partial_contract | optional `language` only; not multilingual support (R03) |
| ADD-12..ADD-17 | not_in_I01 | R04–R09 |
| ADD-18 | partial_contract | `GuidedStep` kind `practice_scenario`; booking is R10 |
| SYS-01 | contract_proposed | internal `ClassifierDecision` (four branches, allowlisted intents, dependencies) |
| SYS-02 | partial_contract | announcement `correlation.episode_id`; the semantic system-event entry is internal to I04 |
| SYS-03 | contract_proposed | internal `ActionPlan`, public `PublicActionResult` |
| SYS-04 | contract_proposed | principals, `ApprovalRecord`, `DecisionRequest`, emergency in-app policy ID |
| SYS-05 | contract_proposed | `RulePolicyRef` with `evidence_status` and citation rule |
| SYS-06 | partial_contract | `Provenance` origins incl. `assumption_based`; the WESAD artefact format is I03B |
| SYS-07 | contract_proposed | `ConditionsSnapshot` source `open_meteo`/`fixture` with issue/valid/retrieval/available times |
| SYS-08 | contract_proposed | `Provenance`, `clock_mode`, `data_time`, `dataset_manifest_sha256` |
| SYS-09 | contract_proposed | idempotency rule per route (`x-idempotency`), `CommandResult`, exchange fixtures |
| SYS-10 | contract_proposed | all of `contracts/proposed/` (runtime vs proposed separation) |
| SYS-11, SYS-12 | contract_proposed | checkpoint flows as sequence/exchange fixtures only; no audio or app evidence |
| SYS-13 | contract_proposed | catalog `MachineId` + 422 `unknown_machine` decision |
| SYS-14, SYS-15 | n/a | process requirements |
