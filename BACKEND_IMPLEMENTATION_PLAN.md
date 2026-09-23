# Cocoon backend implementation plan

Revision 2.1 — reconciled against the filled Review 1 form; commit identity corrected to Naif Naqeeb  
Project: Cocoon, Team Butterfly, Caterpillar Hackathon 2026 — VIT University  
Repository: `Cocoon-Voice`; service: `langgraph-agent/`; expected branch: `backend`  
Status: final implementation specification for this review; backend execution and verification are still to be established in the actual checkout.

## 1. Authority, scope and execution

Save this file as `Cocoon-Voice/BACKEND_IMPLEMENTATION_PLAN.md`. This revision replaces the provisional plans that lacked the filled form. **The five required outcomes are now known. The missing-form blocker is closed.** Read existing repository instructions, compatible code, tests and `API_CONTRACT.md` before changing anything.

Implement one assigned increment at a time, prove its acceptance checks, update the handoff and make a local commit using the exact identity in section 5. Stop after the assigned increment and report evidence so the next prompt can be based on the real result. Existing verified code can satisfy a stage; do not rebuild it merely to match suggested filenames. The user has authorised incremental development and commits, not pushes, merges or deployments.

The screenshot establishes an existing `backend` branch and service folders, not running features. Preserve the teammate's `voice` branch, worker, changes and environment. Backend work provides the contract and independent test client; modifying/connecting the voice worker remains a separately assigned integration task.

### How the form's scope is interpreted

Field 14 contains **five required groups and eighteen additional entries**, while fields 13/15 describe “seven additional features and one stretch.” Field 16 supplies a concrete timed build order. To avoid silently dropping any entry:

- **Core demo:** all five required groups, plus the seven cross-cutting additions explicitly scheduled in field 16: voice operation, explainable alerts, wellbeing/privacy, weather-aware re-planning, man-down SOS, supervisor view, and poor-connectivity support. This grouping follows field 16; it is an interpretation of the form's scheduling, not a claim that field 14 literally contains only seven entries.
- **Stretch:** engine-hour service reminders, exactly as field 16 states, enabled only if Checkpoint 2 is reached on time.
- **Roadmap:** camera drowsiness, real smartwatch vitals, expanded Indian-language speech and BLE acquisition are explicitly named as roadmap technologies in field 11. Their backend interfaces and implementation stages remain specified below.
- **Form-listed follow-on work:** on-device offline safety, walkaround inspection, manual Q&A, efficiency scores, handover reports, instructor booking and richer training simulation all remain in the plan. Their full delivery is not silently claimed within field 16's timed scope. Execute their named follow-on increments after the core gates, or earlier if the team explicitly reprioritises them.

No functionality in field 14 is erased or left as an unnamed “conditional feature.” Each has a backend owner, required inputs, implementation increment and acceptance criterion. Core-demo completion, stretch completion and complete delivery of the whole form's longer feature list are separate statuses.

### Corrections to the earlier plan

The earlier plan covered initial intent classification, basic tasks/incidents, lightweight LMS, telemetry context and the voice streaming contract. This revision additionally makes the following concrete: proximity distance/direction rules; numeric weather and pre-task checks; sudden starts/stops and slopes; repeated-violation escalation; automatic incident drafts; behaviour-triggered lessons; real supervisor authorisation/approval state; weather schedule proposals; man-down check-ins and timers; consent and risk-only supervisor views; offline draft/command sync; shift briefings; service reminders; WESAD/Open-Meteo provenance; and evaluation on the five provided tasks.

The existing dataset is a useful v1 foundation, but **does not already contain all these inputs**. A versioned extension and scenario suite are required. No statement in this plan means new CSVs, models, endpoints, videos, source integrations or application code have already been implemented.

## 2. Complete Review 1 requirement map

Source shorthand: F10–F16 means the numbered field in the filled form. Stage identifiers refer to section 8. UI/audio work remains owned by the relevant teammate; backend support must still be tested.

### Five required outcomes

| ID | Form requirement | Required backend behavior | Inputs / proof | Stage |
| --- | --- | --- | --- | --- |
| REQ-01a | Daily task dashboard, F14.1 | Today's scheduled tasks with assigned machine, site zone, predicted duration, weather, state and version; role-scoped read model for Android. | Site/shift/timezone/task assignments; correct current-day selection. | I03, I07A |
| REQ-01b | Spoken shift-start briefing, F14.1/F16 | A durable once-per-shift briefing event, generated from current assignments and conditions; deliver when an eligible session exists, with expiry and deduplication. | Shift schedule, tasks, forecast freshness; restart does not repeat a played briefing. | I07A, I11 |
| REQ-01c | Start/complete tasks by voice or tap, F14.1 | Both entry paths call the same validated command service; progress and completion persist once. | Stable command/action IDs, transition and version checks. | I04, I07A, I15 |
| REQ-02a | Seatbelt warning before motion, F14.2 | Deterministic engine-on/unfastened rule independent of LLM latency; immediate event upon observation; reset/retrigger and no duplicate warning per episode. | Engine/belt change events; a fixture where warning precedes motion. | I05A, I10 |
| REQ-02b | Repeat violations escalated, F14.2 | Count distinct qualifying episodes over a defined window; create a supervisor escalation request with evidence and approval policy. Repeated samples are not repeat violations. | Episode ledger, rule/window version, scoped supervisor. | I10, I13 |
| REQ-02c | Proximity hazards, F14.2 | Warning/danger-zone state per detected person/object, with distance and direction; crossing, clearing and stale/unknown handling. | Distance, bearing/frame, entity, model/site zone policy and sensor quality. | I05A, I10 |
| REQ-02d | Structured incident report, F14.2 | Capture what/where/when/severity/site zone with trusted operator/machine and telemetry references; clarify missing facts; return the actual stored ID. | Voice/tap inputs, durable drafts, idempotent confirmation. | I06 |
| REQ-02e | Automatic incident drafts, F14.2/F16 | Safety episodes create one linked draft automatically; operator confirms/edits/dismisses it. A draft is never silently a confirmed incident. | Unique episode-to-draft link; user confirmation and replay checks. | I06, I10 |
| REQ-02f | Working-condition warnings, F14.2 | Temperature, humidity, rain, wind and visibility checks before outdoor task start and on relevant changes; explanation and recommended action. | Numeric conditions, forecast issue/valid times, outdoor exposure and task policy. | I07A, I09, I10 |
| REQ-03a | Text/video training and voice quizzes, F14.3 | Versioned curriculum and playable approved video metadata, guided lessons, assessment attempts/scoring and saved progress. | Actual lesson/media assets, quizzes, content provenance and applicability. | I12A, I12B |
| REQ-03b | Beginner/Intermediate/Expert learning levels, F14.3 | Explicit progression criteria and persisted evidence; separate educational level from real equipment certification and dataset skill. | Versioned assessment/completion policies; no progress on playback alone. | I12B |
| REQ-03c | Behaviour-triggered lessons, F14.3 | Qualifying behaviour creates an idempotent policy-authorised assignment, e.g. a 60-second seatbelt lesson; schedule instruction at an appropriate time. | Episode-to-assignment link, deduplication, safe delivery timing. | I10, I12C |
| REQ-04a | Prolonged idling and idle + unfastened belt, F14.4 | Separate duration and combined-condition rules, with context and explanation; distinguish waiting-for-truck from detected belt status. | Engine/idle streak/belt/working-state and reason records. | I10, I11 |
| REQ-04b | Abnormal fuel per load cycle, F14.4 | Compare compatible task/machine windows with nonzero cycle counts and sufficient coverage against eligible baseline. | Fuel/cycles, context, denominator and coverage; no division by zero. | I10 |
| REQ-04c | Sudden starts/stops, F14.4 | Detect configured motion changes using sufficiently frequent timestamped speed/acceleration observations, not a daily jerk score. | High-frequency synthetic motion events; units and sample quality. | I05A, I10 |
| REQ-04d | Steep slopes, F14.4 | Detect configured pitch/roll/grade conditions under model/site applicability and operating context. | Tilt/grade observations and justified policy; never one invented universal CAT limit. | I05A, I10 |
| REQ-04e | Repeated violations, F14.4 | Episode-based trend/window counters, coaching and escalation workflow without duplicate or punitive inference from repeated samples. | Episode history, distinct rule IDs and approval/assignment policies. | I10, I12C, I13 |
| REQ-05a | Contextual task-time estimate, F14.5 | Use task type, quantity, ground condition, weather, skill and machine age; report duration, evidence and what is adding time. | Eligible historical calibration, compatible units, contribution/method explanation. | I07B |
| REQ-05b | Five-task benchmark, F14.5/F16 | Preserve the supplied five rows; report baseline MAE 7.6 min and actual estimator errors; target improvement without guaranteeing it. | Missing-feature policy and transparent calibration/test separation. | I07B |

### Every additional entry in field 14

| ID | Additional entry | Delivery classification and backend responsibility | Proof / stage |
| --- | --- | --- | --- |
| ADD-01 | Camera drowsiness + sleep/shift fatigue | Roadmap acquisition and prediction. Typed detector/sleep inputs, consent, freshness, risk state and explanations; camera processing owned by client. | R01: real or explicitly simulated detector events; no camera claim from CSV jerk. |
| ADD-02 | Hands-free voice, noise cancellation, PTT | Core, shared. Finalized-text/session/stream contract, stable actions, interruption/recovery; voice owner implements AssemblyAI, Cartesia, LiveKit, Krisp/VAD/PTT. | I04, I08, I11, I16; actual joint audio checks. |
| ADD-03 | Explainable alerts and “Why?” | Core. Persist exact trigger evidence, rule/version and recommended action before announcement; follow-ups use the linked alert. | I10–I11; later telemetry must not replace the explanation. |
| ADD-04 | Heat-stress alerts, breaks, consent/privacy | Core. Fixed contextual wellbeing rules using heat index, heart rate, skin temperature and shift/break data; operator consent; supervisor sees risk level only. | I02, I09–I10, I13; adversarial projection/privacy checks. |
| ADD-05 | Weather-aware re-planning | Core. Forecast-backed proposed task reordering; human approval before atomic schedule application. | I09, I13; reject/expire/stale/duplicate approval scenarios. |
| ADD-06 | Man-down SOS | Core. Distinct impact event → operator check-in → response/delivery-aware timer → scoped supervisor alert when unresolved. | I14; response/no-response/unreachable/restart/race tests. |
| ADD-07 | Supervisor view | Core backend. Role-scoped live alerts/incidents/approval read models and decision APIs; React/Tailwind UI is a teammate's work. | I01–I02, I13; no raw vitals and no cross-site access. |
| ADD-08 | Poor-connectivity mode | Core, shared. Connection/freshness state, versioned task snapshots, durable offline incident/command sync and nonvoice presentation receipts. Android owns cache, screen and vibration. | I15–I16; disconnect, queue, reconnect and duplicate upload. |
| ADD-09 | Engine-hour service countdown | Stretch only after Checkpoint 2 on time. Service schedule/last-service records, due-soon/overdue computation and proactive reminder episodes. | S01; model-specific configured intervals and meter-reset cases. |
| ADD-10 | Real smartwatch vitals | Roadmap. Source-specific adapter, consent, quality and provenance; Android Health Connect acquisition belongs to client. | R02; verify actual hardware separately from generated streams. |
| ADD-11 | Hindi, Tamil and more | Roadmap expansion. Language-aware content, intent and unit preservation; backend language field alone does not prove multilingual voice. | R03 plus voice language tests; Sarvam/AI4Bharat remain roadmap choices. |
| ADD-12 | On-device safety without network | Form-listed follow-on. Versioned deterministic rule package, compatible event schema and deduplication when offline alerts sync. Device executes local rules. | R04; airplane-mode device test required. |
| ADD-13 | Bluetooth proximity between phones | Roadmap acquisition. Ingest calibrated estimates with uncertainty/source/quality and original timestamps; reuse proximity rules. | R05; BLE/RSSI is not automatically an accurate distance. |
| ADD-14 | Voice pre-shift walkaround | Form-listed follow-on. Versioned checklist, voice/tap responses, evidence, readiness and unresolved findings. | R06; fail/repair/recheck and restart/resume. |
| ADD-15 | Operator-manual Q&A | Form-listed follow-on. Reviewed model/version-specific documents, retrieval, source/page citations and unsupported-answer handling. | R07; known-answer/unknown/model-mismatch checks. |
| ADD-16 | Personal safety/efficiency score and fuel saved | Form-listed follow-on. Transparent versioned formula, data coverage, appropriate comparison baseline and traceable savings calculations. | R08; no fabricated savings or unjustified cross-operator ranking. |
| ADD-17 | End-of-shift handover reports | Form-listed follow-on. Persist factual task/incident/alert/training summary and unresolved items once per shift; separate approved sharing. | R09; restart, incomplete shift and corrections. |
| ADD-18 | Instructor booking and scenario simulation | Core LMS includes small authored practice scenarios; booking/richer simulation are follow-on. Persist requests/availability/confirmation and branching scenario attempts. | I12B, R10; no fabricated real booking or implied external contact. |

### Cross-cutting form commitments

| ID | Requirement/source | Required treatment | Stage |
| --- | --- | --- | --- |
| SYS-01 | Initial LLM classifier and four branches, F10/F12/F15 | Explicit contextual intent decision before business tools; tasks, safety/incidents, training, general assistance. | I04 |
| SYS-02 | System-event routing, F12 | Deterministic detection/persisted warning first; a deduplicated semantic event route can select approved follow-up workflows without gating alerts on an LLM. | I04, I10–I11 |
| SYS-03 | One request, several actions, F12 | Persist ordered child actions/dependencies; commit each result once; pause only actions requiring approval and report partial outcomes honestly. | I04, I13 |
| SYS-04 | Approval for actions affecting others, F12 | Trusted supervisor identity, versioned proposal, explicit decision, expiry and exact-once local application; defined emergency-notification policy. | I02, I13–I14 |
| SYS-05 | Rules rather than LLM safety detection, F10/F12 | Versioned threshold/guidance registry, deterministic evaluation, templates and evidence. A model cannot create/change a safety limit at runtime. | I09–I10 |
| SYS-06 | Data extension + WESAD, F11/F13 | Versioned generated fields, retained provided rows, actual WESAD-derived preprocessing evidence or an explicit unmet-source status. | I03A–I03B |
| SYS-07 | Open-Meteo, F11 | Cached site-aware numeric conditions/forecasts with issue/valid/retrieval times, units, timeout and fixture mode. | I09 |
| SYS-08 | Synthetic labelling and simulator, F10/F13 | Stable source IDs, separate raw/generated/runtime stores, explicit simulation clock and live-paced delivery without relabelling hardware provenance. | I03A, I05B, I10 |
| SYS-09 | Backend authoritative state/idempotency, F13 | Durable sessions, turn/action/command/event identities, database uniqueness, recovery and consistent projections. | I02, I08 |
| SYS-10 | Android/voice/supervisor integration, F11/F13 | Shared schemas/fixtures, scoped HTTP and independent services; four work streams have clear owners. | I01, I13, I15–I16 |
| SYS-11 | Checkpoint 1, F13/F16 | Spoken incident request → real stored incident → spoken confirmation and exactly one incident in the app. | I06, I08, I16 |
| SYS-12 | Checkpoint 2, F13/F16 | Silent telemetry trigger → unsolicited spoken warning → correct persisted-context “Why?” response. | I10–I11, I16 |
| SYS-13 | Low cost / CAT ecosystem, F10/F14 | Reuse data caches and episode dedup, no LLM per sample; adapter-ready model/site/source IDs. Product Link/VisionLink, Cat Detect and Cat Inspect are future integrations, not implemented API claims. | I03, I10; production handoff |
| SYS-14 | Git ownership and team review, F11/F15 | Scoped changes, incremental commits, actual test evidence and human-readable diffs/handoff; specified author/committer only. | Every increment |
| SYS-15 | Milestones and 15-hour schedule, F15/F16 | Separate user-reported prior milestones from newly verified work; preserve both checkpoints and final noise/reconnect/demo checks. | I00, I16–I17 |

Maintain this exact mapping in `docs/FEATURE_MATRIX.md`: requirement ID/source, priority tier, backend component, other owner, data prerequisite, stage, status, acceptance result and evidence. The filled Review 1 form is authoritative; proximity and all other required groups are core requirements.

## 3. Dataset extension, external sources and evaluation

### Existing v1 and its limits

The prepared v1 manifest contains 72,000 minute observations, 150 shift summaries and 1,214 generated tasks across the five assets below. Verify hashes in the real checkout. The original histories are separate and have unverified original provenance/model identities.

| Exact selectable asset | Model | Category |
| --- | --- | --- |
| `EXC_DEMO_001` | Cat 320 | Hydraulic excavator |
| `DOZ_DEMO_001` | Cat D6 | Bulldozer |
| `LDR_DEMO_001` | Cat 950 GC | Wheel loader |
| `TRK_DEMO_001` | Cat 793 | Mining truck |
| `BHL_DEMO_001` | Cat 420 | Backhoe loader |

The v1 fields include engine/belt, fuel/idling/cycles, speed, control-event counts, a dimensionless jerk index, machine thermal signals, categorical weather, humidity, synthetic heart rate/skin temperature, shift/break context and task quantity/ground-condition/history. They do **not** establish proximity, numeric rain/wind/visibility, site zones, tilt, human-impact force, sleep, operator age, consent, approvals, service history or WESAD-derived calibration.

Do not quietly populate unknown real source fields. Preserve `data/raw/` and `data/cleaned_source/`. Create a versioned generated extension, preferably separate entity/event tables instead of a wide CSV with fabricated values on every machine-minute. Update the generator, dictionary, validation, manifest and scenarios together. Runtime workflows live in SQLite rather than being faked as pre-completed CSV rows.

### Required data additions

| Dataset/entity | Fields / semantics to add | Uses and constraints |
| --- | --- | --- |
| Sites, zones, shifts | `site_id`, `site_zone_id`, site timezone, approved coordinates, zone type, shift start/end, assigned operator/machine, outdoor flag. | Daily tasks, weather lookup, incidents, permissions, briefings. Synthetic locations must be labelled. |
| Task assignments | Scheduled order/start, site/zone, outdoor exposure, quantity/unit, ground/material/attachment, dependencies, resource requirements, status/version, recorded progress. | Today's queue and safe schedule proposals. Keep completed historical outcomes separate. |
| Proximity events | Entity ID/type, `distance_m`, machine-relative `bearing_deg`/direction, reference frame, warning/danger policy ID, observation time, quality/source. | No fixed universal safety radius. Missing/outdated detection is unknown, not a clear zone. |
| Motion/tilt events | High-resolution time, speed/acceleration and derivation interval, `pitch_deg`, `roll_deg`, optional grade with explicit units and source. | Sudden starts/stops and steep slopes; grade percent is not degrees. |
| Human impact events | Distinct source/device/operator, impact peak acceleration in g, duration, body/device orientation if available, and `impact_force_n` only when an actual sensor/model supports it. | Man-down candidates. Hydraulic/control `impacts_count` is not a human fall sensor; acceleration is not force without a documented physical model. |
| Numeric conditions/forecasts | Temperature C, RH %, rain/precipitation amount with accumulation interval, wind speed/gust m/s, visibility m, weather code, forecast issue/valid/retrieval time, location/provider/quality. | Pre-task conditions and re-planning. State an average/rate/interval explicitly. |
| Operator profile/sleep | Synthetic age where needed, explicit experience/source, sleep duration/window/quality source, shift start, last break and activity. | Age/sleep were stated in the form but absent in v1. No sleep data means unknown, not zero sleep. Minimise raw personal data use. |
| Vitals and consent | HR bpm, skin temp C, sampling/window/quality, source profile/version, collection consent, risk-sharing consent, purpose/effective/revoked time. | Keep synthetic participants separate from real people; do not infer permission from app login. |
| Rule/zone policies | Per-rule thresholds/units, applicability, persistence/reset/cooldown, published source citation or explicit demo assumption, review/version. | Required for predictable rules and truthful guidance claims. |
| LMS resources | Courses/modules/lessons, text/video references, duration, level, prerequisites, quizzes, assessment/completion policies and source/licence. | Actual content must exist; learner progress/enrolment are runtime records. |
| Runtime records | Incident drafts/confirmations, approvals, action ledger, SOS episodes/check-ins/timers, notifications, delivery/presentation receipts, offline command inbox. | Persist actual actions and responses; never generate successes as history. |
| Service metadata | Last service engine-hour meter/date, configured interval, model/source, meter reset/replacement information. | S01 stretch; don't invent OEM service schedules. |
| Follow-on resources | Detector/model outputs, manual corpus, checklist, score formula, instructor availability and scenario definitions. | R01–R10 have explicit content/adapter prerequisites; do not claim them available now. |

### Sampling and time

Minute history supports summaries, not a claim of detecting engine start, a proximity crossing or an impact instantly. Generate a separate clearly synthetic event stream at a documented sufficient cadence for each scenario, with change events/high-resolution timestamps. Do not interpolate a 60-second row and call it measured motion or an impact. For the seatbelt demo, arrange engine-on/unfastened observation before any movement; show measured backend latency after receipt, not an unproved physical guarantee.

Maintain two clocks: wall-clock UTC for request receipt, deadlines, delivery, stream retention and expiry; session data time for replayed observations, durations, task cutoffs and rule windows. Replay speed changes pacing only, not physical duration/fuel. Historical data stays synthetic with original timestamps. In replay mode, fixtures/forecasts must align to data time; today's live weather must not be silently joined to an August task. Future forecast *valid* times are allowed when the forecast was available at the cutoff; future measured outcomes are not.

Bind machine/operator/shift/site immutably to the session. Unknown assets fail. A dataset shift/operator change requires an explicit new association/session. Paths resolve from the service directory; default dataset root is `../Cocoon_Dataset_v1`. Import/index by manifest version/hash, not a full CSV scan for every turn. Keep N/A/null distinct from zero, notably truck end-stop fields and inapplicable work-cycle metrics.

For generated v1: fuel totals integrate interval amounts; burn rate uses engine-on hours; idle fraction uses engine-on seconds; control-event rates use active hours. Preserve those definitions and mark legacy unknown units separately. Partial shifts use only eligible intervals, not future daily totals. Current task context excludes completion time/actual duration/target labels. Scenario expectation files remain test-only.

### WESAD and weather provenance

WESAD is a laboratory stress/affect dataset with physiological/motion signals from 15 participants, not a ground-truth dataset of CAT operators, occupational heat illness, sleep or falls [S1, S2]. The implementation must record the actual source version/checksum/acknowledgement, selected signals/subjects, quality rules and preprocessing. Derive an HR summary only from an appropriate available signal/method; BVP samples are not already heart-rate readings. Preserve sampling and temperature-sensor semantics.

Use WESAD-derived distributions/traces only where justified to shape the *synthetic* vitals profile. Do not re-identify subjects as demo operators, convert stress labels into heat/fatigue diagnoses, or claim WESAD calibration before the processing artifact exists. If source retrieval is unavailable, an assumption-based fixture may unblock engineering but the WESAD requirement stays visibly unverified. Neither skin temperature nor WESAD validation establishes core temperature or a clinical detector.

Implement Open-Meteo current/hourly retrieval for configured sites, preserving units and provider timestamps, with a bounded timeout, shared cache and deterministic fixtures [S3]. Keep `weather_mode=fixture` explicit. Expired cache returns stale/unknown and cannot justify an automatic schedule claim. Do not require the LLM to browse weather during every turn. Forecast records identify their provider/model retrieval context and availability time.

### Published guidance and rule registry

Every operational rule has a `RulePolicy` with ID/version, source/title/URL/section/date, exact parameter units, machine/task/site applicability, entry/reset behavior, and an evidence status: published-guidance-based, site-configured, or synthetic-demo-assumption. A source must support the parameter actually used; a generic safety article is not evidence for an exact slope angle or proximity radius.

Use an authoritative heat-index implementation with validity limits and tested temperature/RH/unit handling. NIOSH distinguishes heat index screening from occupational WBGT exposure assessment [S4, S5]. Include the form's contextual HR/skin-temperature/heat-index/break rule as an explicitly bounded advisory; do not present an invented combined score as a validated diagnosis. WESAD does not supply its occupational thresholds.

Seatbelt logic, proximity boundaries, slope limits, wind/visibility task restrictions, motion-change thresholds, heat advisories, break timing and repeat-violation policy each need their own applicable source/policy. For missing OEM/site inputs, support deterministic labelled demo configuration and keep the published-guidance gate unmet; do not fabricate citations or suppress the gap. A rules test proves implementation behavior, not certification or workplace safety.

### Five provided task rows and the 7.6-minute target

The transcribed baseline is independently recomputed:

| Task | Provided estimate min | Provided actual min | Absolute error min |
| --- | --- | --- | --- |
| T001 Earth Excavation | 60 | 58 | 2 |
| T002 Trenching | 45 | 52 | 7 |
| T003 Material Loading | 30 | 42 | 12 |
| T004 Grading | 35 | 33 | 2 |
| T005 Demolition | 90 | 105 | 15 |
| Baseline MAE | — | — | **38 / 5 = 7.6** |

The source telemetry fuel-per-cycle values are approximately 0.4333, 1.9, 0.61 and 2.0 L/cycle. These verify the form's descriptive arithmetic; four rows do not establish that unbuckling caused high fuel use or predict an accident. Call the five-task sample the **provided benchmark**; its original real-world collection provenance remains unverified.

Implement a reproducible estimator and freeze its version before reporting errors. Use full known inputs for generated/runtime tasks and explain which observed factors change the estimate, including sparse-data uncertainty. The five provided rows lack quantity, ground condition and model identity: use a documented reduced-feature/fallback path, or report inability to evaluate that estimator configuration. Do not invent those fields or take quantity from actual duration.

Keep provided rows physically separate. Calibrate on permitted training history/ranges and validate on chronological/grouped synthetic splits. If provided actual outcomes influence coefficients, synthetic target generation, feature selection or repeated tuning, report the five-row score as an **influenced illustrative benchmark**, not independent holdout accuracy. A leave-one-out experiment, if used, also reports its small-sample limits. Beating 7.6 is a target, never a promised result or a reason to tune silently on the test answers. Report every prediction/error, MAE/bias, sample count, missing-input policy and source provenance.

## 4. Backend workflows and ownership boundaries

### Initial classifier and multi-action orchestration

After authentication, durable turn reservation and bounded context retrieval, `resolve_context → classify_intent → route_intent` is the first reasoning path. The LLM produces an internal typed decision with branch, allowlisted intent(s), validated entities, missing fields, routing certainty, active workflow references and whether clarification is needed. The four branches are **tasks**, **safety_incidents**, **training**, and **general_assistance**. Classifier JSON/reasoning never becomes speech.

“Yes”, incident details, quiz answers, “continue”, “Why?” and “I'm waiting for a truck” resolve against the correct active workflow/alert, not a fresh unrelated guess. Invalid/stale/cross-session references clarify or reject. A learner's skill is read/assessed separately; don't infer competence from accent/grammar or force every beginner's task query into training.

One utterance can produce an ordered `ActionPlan` with stable plan ID and typed child actions/dependencies. “Log an incident and tell my supervisor” can commit the incident, then create a linked escalation approval request. It must not report the notification completed while approval is pending. Persist individual outcomes, wait points and recovery markers; retries reuse identities. A later failure/cancellation leaves known committed actions intact. Validate actions in Python; the LLM cannot issue arbitrary SQL or bypass roles/policy.

For machine events, fixed rules classify the physical condition and persist alert/evidence immediately. At most one semantic graph run per eligible episode may classify follow-up intent and route tasks/training/safety/general actions. This fulfils the form's event-routing requirement without placing LLM latency on safety detection. Deterministic policies can create linked drafts/assignments directly under the same idempotent action services. Never call an LLM for every telemetry row or re-execute follow-up tools on every repeated sample.

### Persistent state and authoritative records

SQLite with versioned migrations is the prototype authority. Persist session bindings; scoped actors/consumers; turns/fingerprints; action plans/children; task/schedule versions; incidents/drafts; course/learner records; consents; source observations/projections; rule episodes/evidence; approval proposals/decisions/application results; SOS check-ins/deadlines; turn/announcement events; notifications; playback/presentation reports; offline commands; and conversation checkpoints.

Use session ID as the LangGraph thread identifier, while operator-owned LMS data and site-owned schedules have their own scope/version. Domain records/action ledger commit together in short transactions. A checkpoint failure cannot delete or duplicate a domain action. Reserve IDs durably before work; enforce database uniqueness, not only in-memory locks. Version-check shared operator/schedule updates across sessions.

Run one conversational turn at a time per session, with bounded queues and supervised tasks. Do not hold a SQLite write transaction/session lock across a model call or playback. Telemetry and urgent outbox writes must continue during a long response. Increment state versions atomically and avoid stale graph snapshots overwriting newer alerts. One Uvicorn worker is the supported prototype boundary; SQLite and process-local locks do not confer multi-instance coordination or exactly-once external effects.

### Rules, context and proactive lifecycle

Each accepted observation is authenticated and checked for subject/site/machine, type/unit, provenance, timestamp, quality, event ID/fingerprint and ordering. Duplicates have no additional effects. Out-of-order records may be retained for history without rewriting current hazard state. Gaps cannot imply continuously observed danger or safety. Human impact events, control impacts and training simulations remain different types.

Evaluate the required rule families from section 2 deterministically. Record a condition episode, original trigger snapshot, rule version and recommended action; create the linked incident draft/learning action according to policy; commit the announcement outbox before exposure. Entry/reset/hysteresis prevents repeated-sample spam; re-entry after a valid clear is a new episode. Repeat-violation counters count episodes in a defined window, not incoming row count.

Urgent warning speech may use an approved template immediately. A later LLM explanation must retain the same factual context. “I'm waiting for a truck” records a structured idle reason and may alter efficiency coaching under policy; it must not clear an engine-on/unfastened-belt condition, remove fuel consumption, or rewrite the observed event. Alert occurrence, playback, human acknowledgement and physical condition resolution are separate states.

Before starting an outdoor task, retrieve eligible conditions and applicable restrictions. State whether an advisory, required acknowledgement or policy block applies; this is application workflow behavior, never remote machine control. A forecast-change proposal is separate from the current schedule. Shift briefings are once per operator/shift/version, with deferred delivery if no session exists and expiry when outdated. Avoid repeating the briefing after reconnect or restart.

### Supervisor access, approvals and external effects

Introduce explicit **operator**, **supervisor** and **trusted service/simulator** principals. For the local demo, a backend CLI can provision short-lived opaque tokens bound to subject/role/site; store hashes and document this as demo authentication. Do not embed the global service token in Android or React bundles. Never derive supervisor authority from an utterance, `consumer_id`, room name or client-supplied role.

Supervisor read models show authorised sites/machines, alerts, confirmed incidents/draft status where permitted and pending approvals. Apply field-level privacy before JSON, SSE, exports, LLM context and logs. A health alert's internal evidence containing raw vitals is not a safe supervisor response model.

Approval records contain proposer, site, action type, referenced incident/forecast, proposed payload, payload hash, schedule/resource versions, approver scope, created/expiry times and status. An authenticated supervisor sees a clear before/after proposal and approves or rejects with a stable `decision_id`. Enforce pending → approved/rejected/expired/cancelled once; repeated identical decisions are safe, contradictory decisions conflict. Revalidate inputs at apply time; stale schedules/forecasts require a new proposal. Record approval and execution separately so an approved-but-failed change is not reported as applied.

| Action | Policy |
| --- | --- |
| Immediate warning, incident draft, policy-based lesson assignment | Deterministic existing policy and authenticated event/request; persist and explain. No invented human approval or record completion. |
| Confirming an incident | Explicit operator reporting intent/details or confirmation of an auto-draft; actual record must commit before success speech. |
| Schedule changes and ordinary escalations | Create proposal/pending approval; no schedule mutation or outbound escalation before a properly scoped supervisor decision. |
| Pending proposal visible in supervisor view | This is a request for review, not evidence that the underlying escalation/change executed. |
| Man-down unresolved check-in | The form explicitly calls for a supervisor alert. Use a documented, preauthorised emergency **in-app notification** policy for timely warning, or record the missing policy as a blocker; do not wait for the same recipient to approve receiving it. Further discretionary escalation still requires approval. |
| SMS/email/calls/external dispatch | Not part of the core backend delivery. Never send real messages/book people merely because a draft, approval fixture or instruction mentions it. Any later connector action requires explicit authorised integration and truthful delivery status. |

### Weather-aware re-planning

Use forecast issue/valid times, task exposure, predicted duration, dependency/resource constraints, current progress and approved site policy to create a proposed reordered schedule. Explain which forecast/constraint motivated each change. Do not interrupt/reassign an active task silently. Persist the old/new ordering and all input versions; supervisor approval atomically applies the proposal once and emits an update for Android/voice. Reject/expire/cancel and forecast-outage paths leave the existing schedule intact.

### Operator consent and wellbeing privacy

Store purpose-specific consent for receiving/processing vitals and separately sharing a derived risk level with supervisors, with notice version, scope, time and revocation. For the demo use explicit synthetic consent records; never treat them as permission for real participants. Missing/revoked consent suppresses restricted processing/sharing under policy and exposes an unavailable status rather than fabricated low risk.

Operators receive their own bounded contextual advice. Supervisors receive only the allowed risk level/status, observation freshness and permitted response context; **never raw heart rate, skin temperature, sleep records or a verbatim operator explanation containing those values**. Prevent leakage via alert evidence, incidents, generic `/state`, replay, reports, prompts and logs. Consent withdrawal affects subsequent access and cached projections; document retention/deletion and the limits of already downloaded client data. Do not set a medical diagnosis or claim a validated fatigue/heat prediction.

### LMS: a persistent learning module

Implement learner onboarding/profile; versioned courses/modules/lessons; machine/level applicability and prerequisites; enrolment; text and actual approved video metadata; short guided steps; voice-compatible quizzes; attempt/score/remediation; authored practice scenarios; completion evidence; and Beginner/Intermediate/Expert learning progression. Seed at least the core safety/fuel/condition lessons, including the 60-second seatbelt lesson. Three seeds are starting content, not a hard-coded maximum.

Learning level is an educational result, not an equipment licence or inferred real-world skill. Separate synthetic dataset skill, self-reported experience and assessed learning progress. Step playback alone does not advance/completely certify a learner. A wrong/ambiguous quiz answer is not a pass; deterministic questions/rubrics and explicit progression policies define completion.

Required violation-to-lesson behavior is an **assignment**, not only a recommendation. A versioned, authorised rule maps a qualifying episode to an applicable lesson and commits one assignment/link. Suppress duplicates when an equivalent assignment is active; handle already-completed versions through an explicit refresher policy. The operator can defer the lesson; do not initiate nonurgent instruction during a hazard or unsafe workload. “Why this lesson?” cites the episode without claiming incompetence.

A trusted operator resumes learning across sessions/shifts. Protect progress against concurrent updates and other operators. Return learning path, current step, completed modules, attempts, gaps and next content through typed state/tool results. Video URLs/assets must exist with usage rights, version and checksum; supply at least one genuinely playable approved/demo video for the video requirement. Android owns caching/playback. Instructor booking and richer scenario workflows remain fully specified in R10.

### Man-down check-in and SOS

Model an episode state machine: candidate impact → check-in queued → check-in offered via an available channel → awaiting response → responded / unresolved / unreachable → notified/resolved as appropriate. Use distinct human-impact data, not machine control counts. Keep impact evidence, check-in event, correlation/workflow ID, delivery/presentation attempts, operator response and persisted deadlines.

No response is meaningful only under a defined offered-contact policy. Generation/SSE receipt does not prove playback. A confirmed voice playback or on-screen presentation can start the configured response window; actual human perception remains unknown. If every channel is unavailable, record **operator unreachable/communication failure** and use the configured urgent supervisor notification path, not “operator ignored the check-in” or “all clear.”

“I'm okay” resolves the correct pending check-in; “I need help” raises the appropriate urgent notification. Resolve timer/response races transactionally. Restart resumes persisted timers without duplicate alerts. A configurable test clock accelerates demonstrations without changing production timeout semantics. Record late responses as updates; never erase an already issued alert. There is no automatic emergency-service call or machine shutdown in this prototype.

### Poor connectivity and client projections

Return versioned task/state snapshots with server time, data freshness and clock/provenance. Track presence/last contact and voice availability separately from sensor freshness. The phone caches tasks and local incident drafts; the backend supplies the version and durable sync/reconciliation contract. A dead connection cannot receive a new server warning: cached display/vibration is client behavior and full no-network detection belongs to ADD-12.

Use a command inbox with stable `command_id` and `client_draft_id`, original operator/machine/site/shift association, captured time, expected record version and canonical fingerprint. Retry upload returns the same result; conflicting payload/unsafe stale task state returns a conflict, not a silent overwrite. Later sync never assigns an old incident to the new active machine/operator. Voice and tap commands share validation and repositories. Distinguish saved-locally, syncing, saved-on-server and failed/conflict so the UI never claims server persistence early.

Keep audio delivery separate from on-screen/vibration presentation reports. An on-screen alert is not recorded as audio “played,” nor as a human acknowledgement. Event replay/expiry/dedup works across voice and nonvoice consumers. Client reconnect handles uncertain server outcomes through status/command lookup before retrying with a fresh identity.

## 5. Repository, ownership and commit policy

Backend owns `langgraph-agent/**`, relevant root run docs, this plan and jointly owned `API_CONTRACT.md`/`contracts/**`. Dataset extension is coordinated with the data/rules stream and preserves originals. Voice owns `livekit-voice/**`; Android and React UI code are separate owners. The voice stack remains LiveKit Cloud/WebRTC and Agents, AssemblyAI STT, Cartesia TTS, VAD/turn detection and Krisp background voice cancellation; Android uses Kotlin/Jetpack Compose and the LiveKit Android SDK, and the supervisor client uses React/Tailwind. Backend fixtures do not prove these client capabilities. No shared runtime package, sibling service imports, shared venv or direct client database access. Keep Python, FastAPI, LangGraph, SQLite and pandas for appropriate data work; add no unrelated broker/database/platform redesign.

At every increment:

1. Read repository/root/service instructions and actual branch/worktree/status. Preserve pre-existing staged/unstaged changes; do not reset/clean/stash another person's work. Continue the selected backend branch, expected `backend`. Only if none is selected create/resume `agent-streaming` safely, using a worktree if needed. Do not use the voice branch.
2. Use **Naif Naqeeb as both author and committer**, preserving the Git email already configured for Naif in this checkout. This explicit user correction supersedes earlier commit-identity instructions. Inspect the effective Git identity and scoped environment overrides; set the repository-local name only if needed. Do not substitute another person's email or infer a different Git email from team-contact details. If Naif's configured email is unavailable, complete the non-commit work and report the missing value rather than inventing it. Do not change global config or rewrite older commits.
3. Stage only inspected paths/hunks belonging to the increment. No blind `git add .`/`git add -A` in the shared checkout. Inspect the staged diff, run focused meaningful acceptance checks and update handoff evidence.
4. Commit every completed assigned increment. No empty commits and no success claim for failing required checks. **No Claude/Anthropic/bot authorship, `Co-Authored-By: Claude`, `Committed by Claude`, AI attribution or generated-by footers in commit messages/trailers.** Preserve legitimate existing authorship.
5. Verify the actual commit and report its hash, observed checks and remaining changes. Do not push, merge, deploy or message teammates through tools.

```bash
git config --get user.name
git config --get user.email
git var GIT_AUTHOR_IDENT
git var GIT_COMMITTER_IDENT
git show -s --format='%H%n%an <%ae>%n%cn <%ce>%n%B' HEAD
git status --short
```

Use existing compatible packaging and dependency tooling; inspect installed versions before selecting LangGraph/checkpointer/provider APIs. Keep credentials, `.env`, SQLite/WAL/SHM, downloaded private data and transient fixtures out of commits. Maintain actual OpenAPI/SSE schemas, fixtures, `FEATURE_MATRIX.md`, `HANDOFF.md`, `RECOVERY.md`, `MIGRATIONS.md`, `DEMO_RUNBOOK.md` and service `CLAUDE.md` without duplicating or overwriting teammate instructions.

Suggested logical modules are API/schemas, settings/auth/clocks, storage/repositories/action supervisor, graph/classifier/workflows/speaking node, domain tools, data/weather/rules/LMS/approvals/SOS services, resources and scripts/tests. Adapt existing names; do not force a rewrite to match this layout.

## 6. Public HTTP contract

### Common rules

Preserve `Authorization: Bearer <COCOON_SERVICE_TOKEN>` on the trusted voice/service application contract. Android and supervisor callers use the scoped actor-token extension below. Bind localhost by default. Do not place credentials in URLs, fixtures, telemetry, transcripts or logs. A shared service token authenticates a trusted server-side caller; it is not end-user authentication and must never be embedded in the Android client.

Resolve room/participant/operator/machine and allowed consumer bindings through trusted service configuration or validated session creation. Allowlisted `client_context` fields never override these bindings. Document the trusted-worker boundary; do not claim hostile holders of the same all-powerful service token are isolated by participant metadata alone.

New streaming clients send a stable `X-Request-ID` for tracing, and the server returns it. Preserve existing JSON clients that omit it by generating/returning a request ID if that is their established behavior; do not introduce a breaking header requirement. Apply bounded text/ID lengths and UTC timestamps. Turn identity is `(session_id, turn_id)` across JSON and SSE. Include semantic optional fields in the canonical request fingerprint; exclude transport headers and server-generated values. Keep one normalization policy and document omitted-field defaults.

Use a consistent sanitized error model with `code`, `message`, `retryable`, `request_id`, and bounded recovery information where relevant. Document 401/403/404/409/410/422/429/5xx, including validation errors and `Retry-After` where appropriate. No stack traces or raw provider payloads in public errors.

### Endpoints to preserve and add

| Method/path | Contract |
| --- | --- |
| `POST /v1/sessions` | Idempotent create/retrieve by `client_session_key` within the trusted service scope; conflicting immutable association returns 409. |
| `POST /v1/sessions/{session_id}/turns` | Existing JSON execution/result behavior. Completed result 200; duplicate processing 202 with polling guidance; conflicting payload 409. |
| `GET /v1/sessions/{session_id}/turns/{turn_id}` | Authoritative status, saved speech, action outcomes, state version and recovery information. |
| `GET /v1/sessions/{session_id}/state` | Bound tasks, incidents, training and alerts plus `state_version`; freshness/provenance summaries where applicable. |
| `POST /v1/sessions/{session_id}/telemetry` | Typed event ingestion; durable deduplication and explicit accepted/duplicate/ignored/rejected semantics. |
| `GET /v1/sessions/{session_id}/events?after={cursor}` | Ordered retained announcements, bounded pagination, nondestructive reads. |
| `POST /v1/sessions/{session_id}/events/{event_id}/delivery` | Idempotent per-consumer announcement playback reporting. Preserve existing compatible payloads. |
| `POST /v1/sessions/{session_id}/turns/stream` | Accept finalized text; return 200 SSE after validation and durable acceptance. 200 means connection opened, not action completed. |
| `GET /v1/sessions/{session_id}/turns/{turn_id}/stream?after={sequence}` | Replay retained events then tail the same run. Never starts another graph execution. |
| `POST /v1/sessions/{session_id}/turns/{turn_id}/cancel` | Persist idempotent cancellation intent; 202 while pending, 200 if already terminal. |
| `POST /v1/sessions/{session_id}/turns/{turn_id}/delivery` | Record response playback separately from generation and actions. |
| `GET /v1/sessions/{session_id}/events/stream?after={cursor}` | Independent announcement replay/tail with the polling endpoint's event order. |

If existing event-delivery clients lack a new `delivery_id`, preserve their documented deduplication behavior while adding the new contract; do not silently make an optional baseline field required. Actions remain graph/tool results. Do not invent a generic `/actions` route.

### Turn request and action results

```json
{
  "turn_id": "turn_001",
  "text": "What is my next task?",
  "source": "voice",
  "language": "en",
  "client_context": {"wake_phrase_removed": true}
}
```

The voice worker sends one finalized, wake-approved utterance. Do not require raw audio, a duplicate message-history array, the full LiveKit transcript or fresh machine readings per turn. The backend owns history and telemetry access. Optional `supersedes_turn_id` follows the ordering rules below. Keep language and client context optional and allowlisted.

Each public action result has typed fields for `action_id`, `action_name`, `status` (`completed`, `failed`, or `unknown`), optional real `record_id`, and a safe `summary`. Use discriminated models for action-specific result details. Internal scheduling/pending states are distinct from a completed business action. Reuse canonical compatible field names if already established and document any additive mapping.

### Turn SSE envelope and speech rules

Use UTF-8 SSE framing, `Content-Type: text/event-stream`, `Cache-Control: no-cache, no-transform`, and appropriate response/proxy buffering controls. Do not set hop-by-hop HTTP headers blindly. Document proxy buffering, compression exclusions, flush behavior and read timeout requirements for the selected stack.

```text
id: turn_001:3
event: speech.delta
data: {"schema_version":"cocoon.turn-stream.v1","session_id":"session_001","turn_id":"turn_001","response_id":"response_001","event_id":"turn_001:3","sequence":3,"type":"speech.delta","created_at":"2026-09-23T16:00:00Z","data":{"text":"Your next task is "}}

```

SSE `event` equals envelope `type`; SSE `id` equals `event_id`. Persist before exposure. Per-turn sequences increase monotonically and `response_id` remains fixed on retry/replay. Do not assume network chunks equal SSE frames.

| Event | Required data and meaning |
| --- | --- |
| `turn.accepted` | `status: processing`, durable run identity. No speech or action-success claim. |
| `turn.progress` | Allowlisted stage such as routing/tool_running/responding; optional safe tool name; never private reasoning. |
| `speech.delta` | Nonempty append-only approved speech text; preserve whitespace/punctuation and order. |
| `turn.completed` | `status: completed`, full saved `speech`, typed `actions`, `state_version`. Not a second speech instruction. |
| `turn.cancelled` | `status: cancelled`, reason, known action outcomes, state version; retain generated partial text through status/recovery. |
| `turn.failed` | `status: failed`, sanitized error, known/unknown action outcomes and state version if available. |

Exactly one terminal event is persisted per execution. Atomically coordinate terminal status and event insertion to handle completion/cancellation races. If the database is unavailable, do not fabricate a persisted outcome; close the stream and recover truth through the durable status/restart path once storage returns.

Only a dedicated speaking node/output may produce `speech.delta`. Inspect the installed LangGraph streaming event shape and filter by node/invocation plus permitted content blocks. Never forward classifier JSON, graph debug state, tool arguments, reasoning/thought blocks or provider thought signatures. Retain necessary provider-internal signatures within the model integration only.

A tool validates and commits before any success-confirmation speech is generated. A pre-action acknowledgement, if used, must be truthful and must not imply completion. For a completed turn, concatenated persisted deltas equal saved full speech exactly. Do not retract chunks, stream a draft then replace it, or wait for a full answer merely to imitate streaming at the end.

Emit comment heartbeats, e.g. `: keep-alive` plus a blank line, every configured idle interval, shorter than the configured client/proxy read timeout. They have no durable sequence and are neither progress nor speech. Heartbeats do not extend the turn deadline. Validate/authenticate and check admission limits before opening SSE; afterwards use terminal events for failures rather than pretending to change the HTTP status.

### Idempotency, replay and retention

- Identical turn retries across either POST attach to/replay the same run. Conflicting semantic payloads return 409 before SSE opens. Extra idempotency headers, if supported, must match the canonical identity.
- Support `after` as the exclusive sequence cursor and `Last-Event-ID` with equivalent semantics. Reject inconsistent combinations or event IDs from another session/turn. Validate negative/future cursors explicitly.
- Replay and then tail without a subscribe/read race that drops intervening committed events. Persisted history is authoritative; in-memory signals merely wake subscribers.
- Retain all events for a live run and a documented minimum completed replay window, default 900 wall-clock seconds after terminal completion. Keep status/action results longer under a documented local retention policy.
- Return 410 with recovery instructions when required history has expired; never restart tools. Return stored status for recovery. Reading after the terminal sequence may end immediately; the client must not infer missing completion from an already-consumed terminal event.
- Bound output size, event count, concurrent subscribers and subscriber buffers. A slow client is disconnected/referred to replay rather than growing memory indefinitely or blocking the graph/other clients.

### Cancellation, supersession and delivery

Cancellation request:

```json
{"cancel_id":"cancel_001","reason":"barge_in"}
```

Allow `barge_in`, `stop`, `session_closed`, and `timeout`. Same cancellation ID/payload is safe; conflicting reuse returns 409. Closing a socket alone does not cancel a run. At safe boundaries, stop future model output/tool scheduling; never interrupt a database transaction mid-commit or imply automatic rollback. If completion won the race, return that actual completed outcome.

An optional `supersedes_turn_id` must identify an earlier turn in the same session, never itself or a future/unrelated turn. Persist cancellation intent for the older active run and queue the follow-up in durable acceptance order. Start the follow-up only after the predecessor has reached a safe terminal/reconciled state. A new utterance never cancels committed actions or authorizes concurrent conflicting mutations. Bound the queue and define overload responses.

Delivery request:

```json
{
  "delivery_id": "delivery_001",
  "consumer_id": "voice_consumer_001",
  "response_id": "response_001",
  "status": "interrupted",
  "played_text": null,
  "position_confidence": "unknown"
}
```

Allowed outcomes: `played`, `interrupted`, `failed`, `expired`. Position confidence: `exact`, `estimated`, `unknown`. Validate consumer/session/response association. If exact text is supplied, validate it against the generated prefix; estimated position stays an estimate, and unknown may be null. No SDK position evidence means no assertion of exact playback.

Record immutable delivery attempts keyed by delivery identity. An identical retry returns the stored result; a changed outcome under the same identity conflicts. A distinct actual playback attempt uses a distinct ID; aggregated successful playback is not downgraded by a delayed older failed report. Preserve baseline event-delivery semantics where already agreed. Document these transitions and test out-of-order reports.

Store generated answer, reported played portion, and tool outcomes separately. Subsequent context must not assume an interrupted/unreported answer was heard. Playback is also separate from explicit human acknowledgement and from a machine condition clearing.

### Independent proactive announcements

Announcements use a separate `cocoon.announcements.v1` schema: `session_id`, stable `event_id`, monotonic session `sequence`, `type`, `priority`, `speech`, wall-clock `created_at`/`expires_at`, and correlation to the persisted alert/evidence. They have no required `turn_id`. Include provenance and original observation time in the stored/contextual representation.

Persist alert evidence, condition episode and its outbox event in the same transaction. Both polling and SSE read the same order without destructive consumption. Define exclusive cursors, heartbeat, pagination, expiry, retention/410 recovery and idempotent per-consumer delivery. Expired announcements can remain visible for recovery/history but must be marked unspeakable; clients never announce an expired event as current.

New condition episodes create announcements once. Repeated samples do not spam. Use configured persistence windows, hysteresis/reset, cooldown where justified, and independent expiry. A cleared condition, played audio and operator acknowledgement are different records. Explain “Why?” using the triggering snapshot, rule/version and observation time, not a later unrelated reading. Prefer an explicit alert reference; otherwise resolve the latest relevant delivered/referenced alert and clarify ambiguity.

In the future integrated voice policy, trusted critical announcements may bypass the conversational wake gate; ordinary announcements queue by priority/expiry. All still require trusted session binding and actual playback policy. Document and test this with the voice owner during authorized integration; do not enable or modify the currently standalone worker now.

### Additive operator, supervisor and sync contract

The existing twelve session/turn/event routes above remain available. Add the following capabilities without a shared runtime package or direct database access. Preserve any already compatible canonical route names; otherwise adopt these paths and commit their schemas before clients implement them. These are specifications to implement, not a claim that these routes exist today.

| Method/path | Caller and behavior |
| --- | --- |
| `GET /v1/me` | Authenticated actor; return trusted subject, role, site and allowed associations, never the bearer token. |
| `POST /v1/sessions/{session_id}/commands` | Bound operator or explicitly scoped service; accept a discriminated command for task start/completion, incident draft submission/confirmation, alert acknowledgement, SOS response or LMS interaction. Reserve `command_id` durably and invoke the same domain services as graph tools. |
| `GET /v1/sessions/{session_id}/commands/{command_id}` | Bound caller; authoritative queued/completed/failed/conflict result and real record IDs after a lost response. A GET never re-executes the command. |
| `POST /v1/sessions/{session_id}/presence` | Bound consumer; report connection/voice availability with sequence/time and short expiry. Treat presence as last-known connectivity, not proof an operator is conscious. |
| `POST /v1/sessions/{session_id}/events/{event_id}/presentation` | Bound Android consumer; idempotent screen/vibration presentation report, distinct from audio delivery or acknowledgement. |
| `GET /v1/operators/{operator_id}/consents` | The operator or a narrowly authorised service; return current purpose-specific consent/version and permitted history. |
| `POST /v1/operators/{operator_id}/consents` | The operator; record a versioned grant/revocation with `change_id`, purpose, notice version and expected version. A supervisor cannot consent on the operator's behalf. |
| `GET /v1/supervisor/overview` | Authenticated supervisor; site-scoped task summaries, live alerts, incidents, pending approvals and consent-permitted risk status, with no raw vitals. |
| `GET /v1/supervisor/events/stream?after={cursor}` | Supervisor; replay/tail a separately versioned, role-filtered change feed for the web view. It must not reuse private operator speech/evidence as its payload. |
| `GET /v1/approvals?status=pending` | Supervisor or scoped proposer; bounded, authorised proposal list. |
| `GET /v1/approvals/{approval_id}` | Authorised participant; proposal, decision and actual application outcome. |
| `POST /v1/approvals/{approval_id}/decision` | Scoped supervisor; stable `decision_id`, approve/reject, expected proposal version and payload hash, optional reason. Return the saved decision and separately report execution status. |
| `GET /v1/content/{asset_id}` | Authorised learner; approved lesson text/media metadata with content version, MIME type, duration and permitted URL or local asset reference. No arbitrary user-supplied URL fetch. |

Extend `/state` additively with typed task/estimate/weather, incident draft/confirmed state, learner progress/assignments, relevant approval status, SOS check-in status, permitted risk/freshness, and server/data time. Paginate growing collections; return a versioned snapshot rather than an unbounded lifetime history. Resolve field visibility from the authenticated principal. A supervisor uses the supervisor projection, never a privileged operator `/state` response with fields hidden only in the UI.

Keep `Authorization: Bearer <COCOON_SERVICE_TOKEN>` for the trusted voice/backend-service contract. Add server-issued scoped actor tokens for Android/React under the same Bearer mechanism; authenticate and resolve token type/role on the server. Service-token endpoints must check their allowed service functions; a service-authenticated room/participant mapping is a trust boundary, not permission for arbitrary client role claims. Bind both actor and consumer to the session. Set explicit development CORS origins and allow only required headers/methods; do not combine wildcard origins with browser credentials. Use transport security for connections beyond localhost.

Command envelopes have `schema_version`, `command_id`, `kind`, `captured_at`, optional `client_draft_id`, target binding and expected version, with a typed `payload`. The submitted binding must match server-authorised context; it does not grant authority. Maintain a finite allowlist of command kinds. Do not introduce an arbitrary function executor or duplicate business implementations for voice, taps and offline sync. Domain mutations atomically record the command/action identity and result. Requests that have not started may be queued durably; accepted or queued never means completed.

For approval-pending turns, preserve the public action result enum. A completed `request_supervisor_approval` action has a real approval record ID and typed `approval_status: pending`; it does not represent successful `apply_schedule` or `notify_supervisor`. Add a typed `pending_approvals` projection if needed. Resume an approved action from its durable workflow record, not by blindly replaying the old utterance. Notification creation, approved schedule application, supervisor display and human acknowledgement are distinct outcomes.

SOS and presentation payloads carry the episode/check-in/event identity. A check-in response cannot resolve an unrelated or newer episode. Presentation reports use their own schema, `presentation_id`, bound consumer, channel (`screen` or `vibration`), status (`presented`, `failed` or `unknown`) and timestamp. Record the limitations of client-reported evidence. An explicit acknowledgement or SOS response is a separate command. No endpoint treats a vibration receipt as spoken audio or proof of perception.

Create representative JSON examples and fixtures for all additions: operator/supervisor auth, safe versus private state, task tap, offline incident upload, stale command conflict, risk-only feed, pending/approved/rejected proposal, approved-but-not-applied failure, SOS response and screen presentation. OpenAPI documents request/response schemas and SSE media types; checked-in standalone JSON Schemas document individual SSE envelopes and variants that OpenAPI alone cannot fully express. Compare schemas against running routes in the integration checks.

## 7. Configuration and Vertex AI

Maintain a backend-only `.env.example` and settings reference. Use the following selected values and proposed setting names; preserve compatible existing names or document their mapping. Every implemented setting must have a type, default/requirement, validation and a clear meaning. Do not include fictitious environment variables that the application ignores.

```dotenv
# Explicit live model mode; use mock only when intentionally selected.
AGENT_MODE=live
GOOGLE_CLOUD_PROJECT=orbit-507316
GOOGLE_CLOUD_LOCATION=global
GOOGLE_GENAI_USE_VERTEXAI=true
VERTEX_MODEL=

# Required, independent local service secret. Never commit a value.
COCOON_SERVICE_TOKEN=

# Resolve relative paths from langgraph-agent/.
DATABASE_PATH=./data/cocoon.sqlite3
DATASET_ROOT=../Cocoon_Dataset_v1

# Backend transport and execution defaults.
SSE_HEARTBEAT_SECONDS=10
SSE_REPLAY_RETENTION_SECONDS=900
TURN_DEADLINE_SECONDS=60
```

Choose and record an actually supported configurable text model after inspecting the installed Vertex integration. Do not infer a currently available model from an old example or silently change the project/location. Preserve the repository's supported checkpoint/provider APIs and pin compatible versions after a real smoke check. Python source examples in external documentation are references, not proof of installed compatibility.

Use SDK Application Default Credentials discovery. ADC is reported as already configured on the Windows machine. **Do not manually read, copy, print, parse, relocate, upload or commit the credential file; do not print credential objects or tokens.** Do not set a copied credential path or introduce a Gemini API key. Normal SDK discovery/authentication may use the configured credentials. If authentication/model access fails, report a sanitized cause and the needed user action without opening that file.

The backend does not require LiveKit, AssemblyAI, Cartesia or wake-word provider secrets. Keep its environment independent from `livekit-voice/`. Validate missing live configuration explicitly. Mock mode must still enforce service authentication, schema validation and real persistent tools.

Implement bounded provider connection/read/overall timeouts within the turn deadline; bounded model output; per-session/global turn limits; maximum pending turns; subscriber/event/text limits; request body limits; live telemetry freshness; replay feed inactivity; and shutdown grace. Record actual defaults in the settings reference rather than leaving these as unbounded TODOs. Suggested starting demo limits are one active turn per session, four waiting turns, four active model executions overall, and a 60-second total processing deadline; tune from observed checks and document changes. Queue waiting counts toward a bounded acceptance-to-terminal deadline.

Do not log bearer tokens, raw auth headers, credential paths/contents, provider reasoning or private tool traces. IDs, safe stages, timings, action statuses and sanitized error codes are sufficient for the operational handoff. Disable full transcript/health-value logging by default; use bounded, redacted diagnostics when needed.

### Additional settings and operational policies

Commit a settings/policy table covering the following as their stages are implemented. Use existing names when compatible; these names are proposed interfaces until wired and tested. Keep numerical safety thresholds in a versioned rule policy with applicability and citations, not scattered environment variables or LLM prompts.

| Proposed setting / policy | Required behavior |
| --- | --- |
| `CLOCK_MODE` and selected simulation scenario | Explicit live versus replay data clock. Wall-clock authentication, HTTP deadlines, retention and SOS contact timers retain their documented meanings. |
| `RULE_POLICY_PATH` | Versioned rules, applicability, guidance/assumption status and entry/reset windows. Invalid/missing required policies fail validation clearly. |
| `WEATHER_MODE` | Explicit fixture or Open-Meteo mode. Document cache TTL, allowed sites, units, provider timeout, stale threshold and replay-time alignment. |
| `WESAD_SOURCE_ROOT` / derived manifest | Optional local raw-source location and required provenance for any claimed WESAD-derived profile. Do not redistribute downloaded source data without checking its terms. |
| `CONTENT_ROOT` | Reviewed lesson/media assets and manifest, not an unrestricted file server. |
| Actor/consumer auth policy | Token issue/expiry/revocation, role/site bindings and hashing; a local provisioning CLI, with no hard-coded default secret or password. |
| Approval policy | Authorised proposer/approver scopes, expiry, repeat-violation window, version validation and emergency in-app notification permission. |
| SOS policy | Contact-offer timeout, operator-response timeout, unavailable-channel behavior and persisted scheduler polling/recovery interval. Test-clock acceleration is explicit and disabled in the ordinary run. |
| Presence/sync policy | Presence expiry, allowed command age by command type, version-conflict behavior, payload limits and offline draft retention. |
| Privacy and retention policy | Consent purposes, allowed projections, raw-vitals/transcript retention, revocation behavior and deletion/export access. No raw-vitals logging by default. |
| HTTP/stream policy | Explicit CORS, request/subscriber limits, polling bounds, frame/output limits, non-buffering proxy config and graceful shutdown. |

Never describe suggested timeout/threshold defaults as a published safety standard. Distinguish transport limits, demo scenario assumptions, reviewed site policy and medical/industrial guidance.

## 8. Incremental implementation and commit sequence

The stage IDs below replace the earlier plan's numbering. Match work by requirement and verified behavior, not by an old stage number. Each listed sub-increment is separately reviewable and gets its own scoped commit after its checks pass. The commit subjects are suggestions; section 5's author/committer rule is mandatory. Keep a progress ledger of `not_started`, `in_progress`, `verified`, `blocked` and `deferred_with_reason`, with commit/evidence links. A plan, mock fixture or screenshot alone cannot mark an integration verified.

The executable core sequence is I00 → I01 → I02 → I03A/B → I04 → I05A/B → I06 → I07A/B → I08A/B → I09 → I10 → I11 → I12A/B/C → I13 → I14 → I15 → I16 → I17. Some read-only/source work can proceed in parallel with the other team streams, but backend stages still commit independently. Reuse compatible existing implementations. S01 is the explicit gated stretch; R01–R10 cover every remaining form-listed feature. Do not let a source download, roadmap model or cosmetic restructuring block an already implementable core slice.

### I00 — Inspect the actual checkout and close the scope audit

**Depends on:** this plan and the existing checkout. **Purpose:** establish what is real before changing it.

- Read root/service instructions, `API_CONTRACT.md`, shared schemas, handoff, packaging, code and tests. Inspect branch/worktree/status and preserve all colleague changes. Verify the dataset manifest, files and schema; establish whether the existing dataset extension is identical to the reviewed v1.
- Run the existing bounded baseline checks and record actual Python/FastAPI/LangGraph/checkpointer/provider versions. Distinguish repository code from screenshot claims and reported milestones from verified outcomes.
- Create/update `docs/FEATURE_MATRIX.md` with every REQ/ADD/SYS row in section 2 and a `docs/DATA_GAPS.md` with required missing fields, owner, type, provenance and stage. The filled-form requirement audit is no longer blocked by a missing form.
- Record working commands, failing checks and path ownership in `HANDOFF.md`. Establish repository-local Git identity and a clean scope for the first implementation change. Do not edit voice code or start implementing all stages.

**Acceptance:** every form entry is mapped; no unsupported “already implemented” or “dataset complete” assertion; existing test results and uncommitted changes are recorded without secret content. **Commit:** `docs(backend): reconcile review form and implementation baseline`.

### I01 — Freeze the compatible contracts and fixtures

**Depends on:** I00. **Purpose:** unblock the independent Android, voice, supervisor and data streams.

- Reconcile section 6 with the existing canonical API. Preserve the seven JSON routes and their compatible behavior; define the five streaming/control additions without claiming they already exist. Describe added command, supervisor, consent, content and presentation contracts as proposed until implemented.
- Define strict schemas/enums for trusted identity, classifier decision, action results, task/weather estimates, incidents/drafts, learner progress, rule evidence, consent-safe risk, approvals, SOS, telemetry and offline commands. Include every new field required by the form with units, null semantics and examples.
- Commit OpenAPI, separate turn/announcement/supervisor SSE schemas, sample requests/events, compatibility fixtures and state-transition tables. Add fixtures for approval-pending actions without changing the action execution enum or implying a completed escalation.
- Specify allowed caller roles and field-level privacy. Keep the service token out of Android/React, and keep typed nonvoice presentation separate from voice delivery. Publish the contract in the repo, without contacting teammates through external tools.

**Acceptance:** schema fixtures validate; legacy fixtures remain compatible; unknown/invalid variants are rejected; every client feature has a concrete backend contract and ownership. **Commit:** `feat(contract): define compatible agent and application interfaces`.

### I02 — Durable application foundation, identity and recovery ledger

**Depends on:** I01.

- Implement settings, app lifecycle, SQLite migrations/repositories, clocks, sanitized errors, scoped service/actor authentication and the local demo-token provisioning path. Verify operator/supervisor/site/consumer permissions at service boundaries.
- Persist sessions and immutable bindings, versions, turn reservations/fingerprints, command/action identities, graph checkpoints, outbox/event sequence allocation, consent records and approval/SOS workflow foundations. Add the minimum typed task/incident/LMS tables needed by the next stages.
- Implement idempotent session creation, authoritative state/status reads and shared command/action services. Preserve compatible seed/loading behavior. State mutations and local action results commit together; uniqueness constraints enforce retries across process restarts.
- Start the supervised bounded execution queue and recovery scan. Document one-worker SQLite limits. Reconcile a saved action after restart; do not silently rerun an uncertain side effect or treat a pending in-memory task as durable execution.
- Implement consent grants/revocations and projection filtering before wellbeing data becomes available. A supervisor token cannot read private operator/vitals state; access checks apply to IDs and replay routes as well as list routes.

**Acceptance:** create/retrieve/conflict sessions; two-operator and two-site isolation; duplicate domain action once; migration/restart survival; malformed/unauthenticated requests; consent/version checks; no token or raw-vitals logs. **Commit:** `feat(storage): add durable scoped sessions and action records`.

### I03 — Versioned data foundation

#### I03A — Extend the schema and deterministic generator

**Depends on:** I01–I02 and coordination with the data owner.

- Preserve the original four telemetry/five task samples and supplied history sources. Validate known formatting issues such as stray `**` in the pasted CSV without modifying raw evidence. Keep unverified `random_1000` day labels as source labels, not invented calendar dates.
- Extend the generator/data dictionary/manifest with sites/zones/shifts, numeric weather, proximity, high-resolution motion/tilt, human impact events, sleep/age where needed, consent fixtures, lesson metadata and runtime scenario definitions from section 3.
- Keep one schema across Cat 320, D6, 950 GC, 793 and 420, with model-appropriate applicability/ranges/tasks and distinct actual values. Do not copy one machine's readings or thresholds across all five. Inapplicable fields remain null with a reason.
- Produce deterministic normal, threshold-crossing, stale/gap, clear/retrigger, duplicate and malformed scenarios. Include engine-on/unbelted before motion, proximity warning before danger, starts/stops, slope, combined idle, repeated violations, heat/breaks, forecast change and impact/check-in scenarios.
- Validate referential integrity, units, cadence, nonnegative counters, interval/cumulative definitions, monotonic meter rules, timestamp ordering and machine/operator binding. Document generator assumptions and seed. Source labels must survive import and API projection.

**Acceptance:** five-machine manifests/hashes validate; all core feature inputs exist or have an explicit justified source gap; identical seed/config reproduces output; no fabricated real-source fields. **Commit:** `feat(data): extend synthetic scenarios for the complete core scope`.

#### I03B — WESAD provenance, reference processing and baseline protection

**Depends on:** I03A; may reuse a data teammate's verified artifact.

- Locate the authorised WESAD distribution/metadata and terms; build reproducible local preprocessing for the justified signals. Record source checksum, selected records/signals, method, exclusions and derived summary/profile version.
- Keep stress/affect labels separate from heat/fatigue/fall semantics. Demonstrate the difference between raw BVP and a derived HR summary; preserve temperature sensor meaning. Generated vitals stay synthetic even when shaped from a public reference.
- Package derived references only as permitted; keep large/raw downloads out of Git. If retrieval is unavailable, retain deterministic assumed vitals with visible `assumption_based` provenance and mark WESAD shaping unverified rather than manufacturing a claim.
- Freeze the provided task benchmark and compute the 7.6-minute baseline; document any prior influence of those outcomes on generated task labels. Establish source/chronological split rules and forbid target leakage into current context.

**Acceptance:** trace a claimed WESAD-derived profile to a real processing artifact, or clearly report that source gate unmet; benchmark checksum/MAE and leakage checks are reproducible. Engineering may continue with labelled fixtures, but the WESAD completion flag cannot pass on fixtures alone. **Commit:** `feat(data): document reference provenance and evaluation boundaries`.

### I04 — Initial LLM classification and four-branch LangGraph

**Depends on:** I02 and I01 contract; I03 supplies bounded context.

- Implement explicit `resolve_context`, `classify_intent` and `route_intent` nodes before business actions. Live mode uses the selected Vertex text model and validated structured output; mock mode deterministically returns the same schema through the same graph/tools.
- Route to tasks, safety/incidents, training or general assistance. Support multi-intent utterances with ordered, typed child actions and dependencies. Missing details go through persisted clarification; no tool success is guessed.
- Handle contextual “Why?”, “yes”, “continue”, quiz answers, incident clarification, check-in responses and waiting-for-truck explanations using trusted active workflow references. Disambiguate simultaneous workflows instead of applying one “yes” to several pending actions.
- Add the separate semantic system-event entry path, tied to an existing episode/action identity. Deterministic warnings do not wait for this classifier. Use bounded model/tool retries and output limits; safe failure/clarification replaces silent live-to-mock fallback.
- Implement the dedicated final speaking path and typed tool registry. Tools enforce permissions, domain validation and idempotency regardless of model output. Provider thinking/signatures, classifier JSON and private context never become public speech. A branch whose domain tool is scheduled for a later increment returns an explicit unavailable/pending capability result; a routed request alone is not a completed workflow.

**Acceptance:** labelled examples for all four branches, multi-action and ambiguity; same mock/live schema; invalid classifier output cannot execute tools; session context is isolated; a system event routes once without delaying its persisted warning. Record live checks separately from deterministic fixture results. **Commit:** `feat(agent): add initial intent classification and typed workflows`.

### I05 — Selected-machine telemetry and replay

#### I05A — Typed ingestion, event clocks and rule-ready projections

**Depends on:** I02–I03.

- Implement typed ingest for machine, environment, vitals, proximity, motion/tilt and human-impact events. Enforce origin, unit, observation time, quality, subject binding and canonical event fingerprint. Future measured timestamps, duplicate/conflicting IDs and out-of-order data have explicit responses.
- Import/index the selected asset and only eligible history/context at the replay cutoff. Provide engine/motion/idle/fuel/cycle/shift/break projections without per-turn whole-file scans or future actual task outcomes.
- Keep site/operator/shift boundaries, N/A fields, stale state and gaps explicit. Private vitals require the applicable consent policy. A dataset operator switch does not silently change the session identity.
- Persist urgent telemetry context through short transactions even during an active conversation. Expose data source/clock/quality/freshness through typed state rather than an unbounded raw dataframe.

**Acceptance:** correct current data for all five selectable assets; future data and other operators excluded; duplicate ingest has no repeat effect; gaps/stale/unknown handled; urgent writes complete while a model is deliberately slow. **Commit:** `feat(telemetry): ingest scoped events with explicit clocks and provenance`.

#### I05B — Reproducible simulator and feature scenarios

**Depends on:** I05A.

- Supply a documented Python entry point selecting exact machine ID, operator/shift, scenario, data cutoff/start, pace and seed. Example intended shape: `python -m cocoon_agent.simulator --machine-id EXC_DEMO_001 --scenario seatbelt_before_motion`; align with existing packaging and publish the actual command.
- Stream through the same authenticated HTTP telemetry endpoint used by other sources. Include graceful stop/restart, stable event IDs, retry/backoff, finite scenario completion and explicit simulation labels.
- Use appropriate high-frequency/change events for immediate hazards and minute observations for summaries. Replay speed alters pacing only. Provide deterministic weather fixtures aligned to the replay data clock.
- Commit Bash and PowerShell scenario commands and expected outcomes in test-only fixtures. Runtime code must not read expected labels or scenario names as detection truth.

**Acceptance:** normal and hazard scenarios run independently for all five models; stop/resume/retry does not duplicate episodes; no physical-duration distortion or scenario-label leakage. **Commit:** `feat(simulator): replay five-machine scenarios through the public API`.

### I06 — Incident reporting and automatic-draft foundation

**Depends on:** I02, I04 and event schema from I05.

- Implement incident extraction with what, where, when, severity and site zone, trusted operator/machine, relevant evidence IDs and uncertainty. Ask for missing material details; distinguish reported facts from inferred suggestions. Never take operator identity or authority from the description text.
- Support new voice reports, typed/tap reports, editable drafts and explicit confirmation/dismissal. An explicit complete “log this incident” request can authorise the record; an automatically generated draft still requires confirmation.
- Implement unique episode-to-draft creation for I10. Safety rules may create a draft before any utterance. Preserve confirmation history and original evidence; repeated samples/retries create neither extra drafts nor extra incidents.
- Return real record IDs and read them through `/state`/supervisor projection as authorised. “Log an incident and tell my supervisor” commits the incident and creates a pending approval workflow; do not invent sent/received notification status.

**Acceptance:** explicit report, missing-field follow-up, edit/confirm/dismiss draft, identical/conflicting retries, post-commit response failure and restart; one saved incident in the API/app read model. This is backend evidence toward Checkpoint 1, not proof of audible output. **Commit:** `feat(incidents): persist structured reports and confirmable alert drafts`.

### I07 — Daily tasks and estimation

#### I07A — Daily dashboard, lifecycle and shift briefing

**Depends on:** I02–I04 and task data.

- Serve today's tasks using site timezone/shift and data clock, with assigned asset, zone, order, outdoor exposure, weather freshness, prediction placeholder/status and task version. Unscheduled/historical/completed work is not accidentally today's queue.
- Implement task lookup, next task, start, progress and complete through shared voice/command services. Validate assignment, machine compatibility, status, prerequisites and expected version; no duplicate starts/completions or cross-operator mutation.
- Record actual start/end/work completion for later evaluation without exposing future outcomes. Add pre-start condition-check hooks used by I09/I10, with explicit unknown-data behavior.
- Persist a once-per-shift briefing job/event using current tasks and available weather. Recompute/cancel an obsolete undelivered briefing when its inputs change; a new version does not repeatedly replay a briefing already delivered. I11 supplies the independent announcement transport.

**Acceptance:** day/timezone/shift boundaries, task transitions, conflicting taps and voice, assignment isolation, briefing defer/expiry/restart; ordinary task retrieval does not need fresh LLM-generated facts. **Commit:** `feat(tasks): add daily task lifecycle and shift briefings`.

#### I07B — Explainable duration estimates and five-row evaluation

**Depends on:** I03B and I07A; numeric weather enrichment from I09 can complete the runtime input path.

- Implement a transparent baseline or regularised small-data estimator using type, quantity/unit, ground/material, weather, operator skill and machine age, with model/task applicability. Do not overfit a large ML model to five supplied outcomes.
- Persist the prediction, input snapshot, cutoff, estimator version, missing-feature/fallback status and factor explanation. Return uncertainty/range where justified; don't fabricate calibrated confidence intervals.
- Fit/calibrate only on declared eligible data. Isolate the source sample and frozen evaluation script; explicitly disclose when the provided benchmark influenced generation/tuning. Use a supported reduced-input path for the five rows lacking quantity/ground/model.
- Report all five predictions and absolute errors, MAE/bias/count and baseline 7.6. Test synthetic validation separately; seek MAE below 7.6 without claiming the target was achieved before it is measured. Explain which observed conditions add time rather than an invented causal attribution.

**Acceptance:** required runtime factors affect the documented method; missing/out-of-range/context-incompatible inputs return a truthful fallback; no target leakage; one reproducible benchmark report with no fabricated improvement. **Commit:** `feat(estimation): add contextual task predictions and benchmark report`.

### I08 — Streaming turns, cancellation and truthful recovery

#### I08A — Persisted incremental turn SSE

**Depends on:** I02, I04 and actual tool results from I06/I07.

- Implement the POST/GET turn stream routes and all six event variants, sharing the JSON endpoint's durable turn identity and result. Reserve the fingerprint/response ID before execution; persist each event before exposure and one terminal event atomically with terminal status.
- Use the installed asynchronous LangGraph streaming interface and explicitly select only the dedicated speaking output. Filter structured/classifier/tool/debug/thinking/signature content. Success speech follows a committed action result.
- Stream before full response completion with preserved whitespace; concatenated deltas equal saved full speech. Keep the run independent of the HTTP subscriber and apply bounded duration, queue/output/subscriber limits and idle heartbeats.
- Implement replay/tail without gaps, cursor validation, retention and 410 recovery; no GET can start a graph. Preserve original JSON 200/202 behavior and reject conflicting retries before opening SSE.

**Acceptance:** an actual localhost asynchronous HTTP test receives speech event one before a controlled fixture pause and later event two/terminal; split Unicode/frame/parser fixtures; two concurrent subscribers see the same run; lost initial POST response and retry create one incident. In-process buffering alone cannot prove this. **Commit:** `feat(streaming): add durable incremental turn events and replay`.

#### I08B — Cancellation, supersession, playback and restart

**Depends on:** I08A.

- Implement idempotent safe-boundary cancellation and queued `supersedes_turn_id` semantics; serialize conflicting state transitions without blocking urgent telemetry. Socket disconnect alone must not cancel.
- Persist generated text, delivery attempts and known tool outcomes separately. A terminal turn is not proof of playback. Preserve exact/estimated/unknown position, including null `played_text`, and immutable delivery-attempt semantics.
- Recover accepted/active turns on process restart from durable action records. Prevent duplicate effects after a crash between commit and speech/checkpoint. Persist a terminal recovery failure when appropriate; return `RECOVERY_REQUIRED` and genuinely unknown outcomes if reconciliation is impossible.
- Test cancellation before tools, after commit, during generation and racing completion; late obsolete text is identified by response identity for client suppression. Bound cleanup and graceful shutdown without killing unrelated runs.

**Acceptance:** replay/expired replay, interrupted post-commit incident, duplicate/conflicting cancel IDs, next-turn ordering, playback out-of-order reports, process restart and slow subscriber limits preserve truthful outcomes. **Commit:** `feat(recovery): add cancellation playback and restart reconciliation`.

After I06 and I08, provide the backend slice for **Checkpoint 1** to the voice/Android owners. Mark the joint checkpoint passed only after the real microphone-to-speaker path and exactly-one incident visible in Android are observed. Standalone voice development remains independent until that integration task is assigned.

### I09 — Weather, operator context and reviewed rule policies

**Depends on:** I03, I05 and consent/projection foundations from I02.

- Implement the site-aware Open-Meteo client with current/hourly numeric temperature, humidity, rain/precipitation, wind/gust and visibility as supported by the selected response. Preserve source units, availability/retrieval/valid times and missing fields. Cache across sessions, bound calls and provide explicit fixture/live weather modes.
- Compute the documented heat-index screening value with validity/unit checks; preserve HR/skin-temperature quality, activity, shift duration, last break and sleep availability separately. Skin temperature is not core temperature. No unobserved break or sleep interval is invented.
- Publish a reviewed rule registry for each rule family. Cite applicable published guidance where available; mark site settings and demo assumptions distinctly. Missing model-specific limits must not be filled by the LLM or described as OEM-approved.
- Provide deterministic features for working-condition checks and wellbeing rules. Unknown/stale/low-quality data returns a coverage/freshness status; missing consent suppresses restricted processing and sharing. Operator explanations and supervisor risk summaries use different models.

**Acceptance:** provider fixture/timeout/cache/stale/unit tests, replay-versus-live time alignment, heat-index validity boundaries, consent withdrawal and raw-vitals redaction across every response/feed/log path. A live provider check is separately recorded when available. **Commit:** `feat(context): add weather and consent-aware wellbeing inputs`.

### I10 — All core deterministic safety and wellbeing rules

**Depends on:** I05, I06, I07A, I09 and policy/content references from I03.

Implement each rule below as a named, versioned evaluator producing typed evidence, state transitions and recommended action. Detection must work if the LLM is unavailable.

| Rule family | Mandatory behavior and checks |
| --- | --- |
| Engine-on/unfastened seatbelt | Warn on the observed engine/belt condition before motion in the fixture; do not require speed > 0. Engine off, unknown belt state and stale readings have explicit semantics. |
| Proximity | Per-entity warning/danger transitions, distance and direction; validate reference frame/quality. Crossing to danger can raise priority within an episode; missing detections alone do not prove clear. |
| Prolonged idle | Accumulate eligible contiguous observed idle time; gaps/engine-off/reset policy are explicit. Record operator idle reason without falsifying telemetry. |
| Idle plus unfastened belt | Separate combined risk pattern linked to its evidence; coalesce overlapping announcements under policy while keeping the underlying rules visible. |
| Fuel per work/load cycle | Eligible model/task/window baseline, minimum coverage and nonzero denominator. Zero-cycle idle fuel is reported separately, not divided by zero or compared across incompatible machine tasks. |
| Sudden starts/stops | Derive valid speed/acceleration changes from the high-resolution stream; document intervals/units and reject insufficient sampling. A daily dimensionless jerk index is not physical acceleration. |
| Steep slopes | Model/site/task-applicable pitch/roll/grade policy with operating context; distinguish degrees from percent and unknown from safe. |
| Repeated violations | Count distinct qualifying episodes within a versioned time window, link history and create one coaching/escalation workflow per policy trigger. |
| Working conditions | Temperature, humidity, rain, wind and visibility warnings before outdoor task start and on relevant deterioration; explicit advisory/acknowledgement/block policy. |
| Heat-stress advisory | Combine eligible HR, skin temperature, heat index, activity and shift context through declared fixed rules; quality/consent gates and an advisory status, not a diagnosis. |
| Break reminder | Time on shift and since a recorded break, with eligibility/defer/reset behavior. Speaking a reminder is not evidence that a break was taken. |

Persist rule episodes, evidence/rule version, observation time, draft/action links and the announcement outbox atomically where they form one local action. Add entry/reset/hysteresis, retrigger, stale/expiry and prioritisation policies. Keep detection independent from the asynchronous semantic event graph; dispatch each eligible follow-up episode once. Avoid repeated-sample spam while allowing a new genuine episode or worsening danger state to be represented.

Wire qualifying safety episodes to one automatic incident draft and the policy-authorised lesson-assignment service. If the content/workflow prerequisite is not yet implemented, expose a pending/unavailable follow-up and finish it in I12C; never mark an assignment complete without a record. Repeated violations create an approval request for ordinary supervisor escalation; the immediate operator warning is never held for that approval.

**Acceptance:** threshold-below/at/above, clear/retrigger, duplicate, gap/stale, out-of-order and applicability scenarios for every row; no LLM dependency; correct linked draft; bounded announcement volume; rules use actual values, not scenario labels. Verify prompt persistence during a long reply. Guidance-source gates remain separate from behavioral test success. **Commit:** `feat(rules): implement the complete safety and wellbeing rule set`.

### I11 — Independent announcements and contextual follow-up

**Depends on:** I08, I10 and briefing events from I07A.

- Implement announcement polling and independent SSE over the same ordered durable outbox, with separate schema/sequence, heartbeat, bounded replay, expiry, retention and per-consumer delivery. Silent sessions can receive a warning without a user turn.
- Persist the alert and exact explanation snapshot before publishing. Use approved deterministic urgent speech; explain what triggered it and the recommended action. Later “Why?” resolves the relevant delivered/referenced alert, with clarification for ambiguity.
- Add contextual replies such as “I'm waiting for a truck,” “I understand,” and “remind me later” with action-specific policy. Acknowledgement, delayed coaching, played audio and resolved physical condition remain separate.
- Deliver shift briefings when eligible and fresh. Avoid playing expired/stale ordinary announcements after a reconnect. Define trusted critical-announcement priority/wake behavior for the voice owner; no code change in their standalone worker.
- Complete episode → draft → follow-up correlation and provide normal/critical/superseded/expired fixtures. Keep raw operator vitals out of the supervisor stream and private thought content out of all streams.

**Acceptance:** silently inject telemetry, observe a proactive event with no turn, reconnect without duplicate logical event, and ask “Why?” after the live reading has changed; the explanation retains the original trigger. Check polling/SSE cursor equivalence and playback independence. **Commit:** `feat(alerts): stream proactive announcements with durable why context`.

This is the backend gate for **Checkpoint 2**. The full checkpoint also requires the integrated voice worker to speak the alert without a prompted utterance and answer the follow-up audibly. Record both pieces separately. Only an on-time joint Checkpoint 2 makes S01 eligible under the filled form.

### I12 — Complete core LMS

#### I12A — Learner profile, curriculum and text/video content

**Depends on:** I02, I04, I03 content manifest.

- Implement learner profile/onboarding, course/module/lesson versions, machine/level applicability, prerequisites, enrolment, learning path and assigned-versus-available content. Preserve operator identity across sessions.
- Seed reviewed seatbelt, efficient idling/fuel and working-condition/wellbeing content, including the 60-second seatbelt lesson. Include actual text and at least one playable approved/demo video with provenance/usage rights and metadata; a placeholder URL does not satisfy the video feature.
- Return typed content/progress to Android and concise speakable steps to the graph. Keep real machinery instructions within reviewed content, and protect assets/path lookup from arbitrary files or remote URLs.

**Acceptance:** beginner/intermediate/expert path eligibility, machine applicability, text retrieval, valid video asset/playback handoff, missing content and cross-operator access. **Commit:** `feat(lms): add learner paths and versioned text video lessons`.

#### I12B — Guided lessons, voice quizzes, attempts and progression

**Depends on:** I12A.

- Persist active lesson/step, pause/resume/defer, explicit step interactions, quiz attempts/answers/scores, remediation and completion. Voice and taps use the same commands and attempt identities.
- Support contextual spoken answers, clarification for ambiguous STT, deterministic scoring of authored questions and a small authored scenario-based practice flow. Do not let an LLM fabricate the answer key or grant a pass from conversational confidence.
- Define versioned Beginner/Intermediate/Expert educational progression from actual course/assessment evidence. Keep curriculum level separate from dataset skill and equipment certification. A media delivery receipt alone cannot complete the lesson.
- Resume after a disconnect/restart or a new authorised session; version-check concurrent attempts. Preserve wrong answers and retries without double counting points/completions.

**Acceptance:** full lesson → voice-compatible quiz → fail/remediate/retry/pass → saved progression; resume on a later session; duplicate answer and attempt conflicts; level transition criteria; authored scenario attempt. **Commit:** `feat(lms): persist quizzes practice and learning progression`.

#### I12C — Behaviour assignments, coaching and gap follow-up

**Depends on:** I10–I12B.

- Complete policy-authorised incident/rule-to-lesson assignment, specifically the seatbelt-lapse → 60-second lesson case. Persist source episode, policy/content version, assignment status and reason exactly once.
- Deduplicate active equivalent assignments; define refresh/reassignment for completed/outdated lessons and repeated lapses. Record evidence-based learning gaps without declaring the operator incompetent from one event.
- Implement “Why was this assigned?”, “What should I learn next?”, progress summary and safe deferred lesson scheduling. A critical hazard takes priority over training speech.
- Provide complete LMS state to the app; supervisor views show only authorised progress summaries and never private health evidence used by a recommendation.

**Acceptance:** a silently triggered violation creates one real assignment; repeated telemetry does not multiply it; source explanation, defer/resume, completion and refresher policy work; assignment survives restart. **Commit:** `feat(lms): assign behaviour-linked lessons and progress followups`.

### I13 — Supervisor view, human approvals and weather re-planning

**Depends on:** I02 roles/approval ledger, I07 tasks/estimator, I09 weather, I10 episodes, I12 progress.

- Implement the scoped supervisor overview and live change stream with alerts, incidents/draft status where permitted, pending approvals and consent-permitted risk level. Enforce privacy before serialization; never rely on React to hide fields.
- Implement proposal creation, authorised approve/reject, expiry/cancel, expected-version/payload-hash checks and recorded application outcomes. One-tap UI approval is a single idempotent decision call, not a bypass of validation.
- Generate a weather-aware task reorder proposal using the relevant forecast, task duration/exposure, dependencies/resources and current progress. Present a before/after schedule and reason. No schedule mutation until approval; revalidate all relevant versions and apply once atomically.
- Implement repeat-violation/explicit operator escalation requests and in-app notification records under the approval policy. “Incident logged; approval requested” and “supervisor notified” are different result states. No external SMS/email/calls are implied.
- Add durable continuation of approved workflows without rerunning original tools, safe refusal of stale/unauthorised decisions and a visible approved-but-application-failed outcome. Emit consistent operator/app changes after successful application.

**Acceptance:** approve/reject/expire/cancel, duplicate and contradictory decisions, concurrent/stale schedule change, changed forecast, inaccessible site, raw-vitals redaction and applied-once restart recovery. Supply the React owner fixtures and smoke commands; separately record actual web integration. **Commit:** `feat(supervisor): add scoped approvals and weather schedule proposals`.

### I14 — Man-down check-in and SOS workflow

**Depends on:** I05 impact data, I11 announcements, I13 supervisor notifications and explicit emergency policy.

- Implement the impact-candidate/check-in state machine from section 4 with durable episode IDs, channel offers, response deadlines, timer scheduling and response records. A machine hydraulic impact count is never substituted for a human-impact event.
- Ask a concise check-in through an available channel. Start response timing under the documented contact-offer policy, using reported playback/presentation where available; maintain a separate bounded delivery-wait timeout.
- Resolve “I'm okay”/“I need help” against the correct episode. On unresolved offered check-in or unreachable channels, create the correctly labelled urgent in-app supervisor notification under the preauthorised policy; never falsely label an unreachable operator as ignoring a warning.
- Reconcile timer/response/restart races, multiple impacts within an episode and late responses. No duplicate notification, machine shutdown or emergency-service call. Test timeouts can use an injected clock without production sleeps or changing the meaning of deadlines.

**Acceptance:** offered/responded, offered/no-response, help requested, all channels unavailable, duplicate impact, concurrent response/timeout, late response and process-restart recovery. Verify the notification scope and no raw-vitals leakage. **Commit:** `feat(sos): add durable impact checkins and supervisor alerts`.

### I15 — Poor-connectivity support and offline reconciliation

**Depends on:** I02 commands/versions, I06 incidents, I07 tasks, I11 events, I14 check-in correlation.

- Implement connection/presence expiry and server/data freshness, versioned snapshots, command submission/status and nonvoice presentation receipts. Distinguish voice-down, backend-unreachable and stale sensors.
- Define a persistent client draft identity and original operator/machine/shift binding. Deduplicate uploaded drafts across reconnects and replacement sessions using an authorised operator/device draft namespace, not just the current session ID. Never rebind an old incident to a new machine.
- Apply a typed policy for delayed commands: incident drafts may sync with original observation time; stale task transitions require version/assignment checks; old acknowledgements must reference their original event. Define whether a closed original session permits a scoped historical draft upload.
- Return server persistence/conflict/recovery states for Android's queue. Reconciliation queries an uncertain command before retrying; conflicts preserve the local draft for correction instead of silently overwriting current state.
- Keep screen/vibration presentation separate from audio playback and human acknowledgement. Android owns cached tasks, local drafts, UI connection status, vibration and offline storage. Full local hazard detection remains R04.

**Acceptance:** start online → cache tasks → disconnect → capture incident locally → reconnect/upload twice → exactly one correctly bound server record; stale task conflict; lost acknowledgement; screen fallback without falsely reporting audio played; cross-operator access rejected. A real offline Android demonstration is a joint gate. **Commit:** `feat(sync): add offline command reconciliation and presentation status`.

### I16 — Hardening, independent client and integration evidence

**Depends on:** I00–I15 core behavior.

- Complete the asynchronous standalone streaming client and deterministic fixtures described in section 9, with Bash/PowerShell commands. Test over a real localhost server/socket, including controlled mid-generation pauses and incremental parsing.
- Run the focused verification matrix in section 10: compatibility, retries, cancellation, restart, slow subscribers, authentication, scope/privacy, stale telemetry, approval/SOS races and offline reconciliation. Test provider failure/timeouts and bounded overload; never silently replace a failed live model with mock success.
- Inspect buffering/flush/compression/proxy configuration, rate/body/frame/output limits, SQLite contention and graceful shutdown. Keep raw transcripts, vitals, secrets and provider thoughts out of diagnostics. Capture useful sanitized error/recovery examples.
- Measure acceptance, queue, first speakable delta, tool and final response durations with p50/p95 and sample counts; label mock/live and machine/scenario. Do not count a progress event as an answer or report backend timings as microphone-to-speaker latency.
- Supply backend contract fixtures and evidence to the team in the repository. During separately assigned joint integration, validate both checkpoints, real early TTS, wake/PTT/noise behavior, nonvoice fallback, supervisor approval UI and reconnect. Do not rewrite or take over the voice worker to make a backend test pass.

**Acceptance:** no unresolved core correctness/privacy/idempotency failure; test evidence includes actual network streaming and failure/recovery; both joint checkpoint statuses are honest, with outstanding client dependencies listed. **Commit:** `test(backend): verify streaming recovery and cross-client contracts`.

### S01 — Stretch: engine-hour service reminders

**Entry gate:** joint Checkpoint 2 met on time, core work remains on track, and the team elects to use the available stretch budget. It is not silently required before the core demo.

- Add model-specific, sourced/configured service intervals and last-service meter/date records; handle meter reset/replacement and missing history explicitly. Never infer an OEM service interval from an unrelated example.
- Compute remaining engine hours/due/overdue from eligible telemetry and persist due-soon/overdue episodes, explanation and proactive events. Repeated rows do not spam; maintenance acknowledgement does not mean the service was completed.
- Add an authorised service-completion record that establishes the next interval and a typed state/tool response for the countdown.

**Acceptance:** countdown, due crossing, overdue, restart, repeated samples, missing history, meter reset and actual service update; explanation includes source/configuration. **Commit:** `feat(maintenance): add engine-hour service reminder episodes`.

### I17 — Demo, migration and final handoff

**Depends on:** I16, plus S01 only if actually attempted.

- Run the final scenario sequence in section 10 and record actual results, commit/config/dataset versions and remaining limits. Rehearse choosing each of the five machine IDs without cross-machine data leakage; depth-test the main narrative on the selected demo asset.
- Deliver `API_CONTRACT.md`, OpenAPI/SSE schemas, fixtures, standalone async client, settings reference, feature matrix, data dictionary/manifest, guidance register, benchmark report, migrations, recovery guide, demo runbook, `HANDOFF.md` and backend `CLAUDE.md`. Link existing files instead of maintaining conflicting duplicate instructions.
- Document database backup/migration/restore and replay-retention recovery. Avoid destructive migration of a teammate's database. Record one-worker scope, demo authentication and unverified live/source integrations plainly.
- Mark each required/core feature verified, blocked or unfinished with evidence; stretch and roadmap retain separate statuses. Do not mark all backend requirements complete if a core rule/input, LMS video, privacy check, approval path or joint checkpoint remains unproved.
- Update the form/presentation evidence from actual runs. Do not restate the form's unverified claim about Caterpillar's current AI capabilities as a researched competitive fact.

**Acceptance:** another team member can start the documented backend, select an asset, run the demo/client and follow recovery instructions without hidden state or secrets; every form item has a truthful status. **Commit:** `docs(backend): record demo evidence and final integration handoff`.

### R01–R10 — Remaining form-listed backend delivery

These are explicit implementation increments, not deleted scope. Their delivery follows the core checkpoints unless the team reprioritises. Each gets its own schemas/data/content, focused acceptance checks, handoff update and commit. Backend readiness and actual hardware/mobile/provider integration are different completion flags.

| Stage / form entry | Backend work and prerequisites | Acceptance / suggested commit |
| --- | --- | --- |
| R01 — Drowsiness and fatigue, ADD-01 | Ingest consented, timestamped camera detector outputs with model/version, confidence/quality and source; mobile owns MediaPipe/camera capture. Add a documented sleep/shift/break fatigue advisory or evaluated model with justified data, missing-data rules and explanations. Camera drowsiness and predicted fatigue remain separate signals. No raw video required at the backend. | Simulated inputs labelled; actual camera stream checked separately; stale/occluded camera, unknown sleep, threshold/reset and consent revocation. No diagnosis or performance claim without an evaluation. `feat(wellbeing): add sourced drowsiness and fatigue inputs` |
| R02 — Smartwatch vitals, ADD-10 | Source adapter for Health Connect/device events, per-signal units/sampling/timezone/quality, authorised subject mapping, processing and sharing consent. Reuse privacy and rule services without relabelling synthetic data as hardware. Android owns access permissions and watch acquisition. | Real-device trace where available, delayed/batched sync, conflicting provenance, permission withdrawal and supervisor redaction; otherwise mark hardware integration unverified. `feat(vitals): add consented wearable event adapters` |
| R03 — Multilingual voice support, ADD-11 | Persist preferred/detected request language without overriding trusted identity; review Hindi/Tamil response/lesson content, classify multilingual utterances, retain machine IDs/units and use safe unsupported-language fallback. Backend emits text in the agreed language. Voice owner evaluates provider support and Sarvam/AI4Bharat options. | Labelled intent/quiz/action fixtures and human-reviewed hazard meaning in supported languages; real STT/TTS speech check separate. No language support claim from an optional JSON field alone. `feat(language): add reviewed multilingual agent responses` |
| R04 — On-device no-network safety, ADD-12 | Versioned portable deterministic rule/schema package with applicability, source/expiry and integrity checks; define local episode IDs, device time/quality and server reconciliation. Android implements local execution, notifications and persisted events without backend availability. | Airplane-mode physical client test; stale policy/version handling, offline alert replay dedup and no false server-delivery claim. Specify which rules/data remain available offline. `feat(offline): define device rule packages and alert reconciliation` |
| R05 — BLE proximity, ADD-13 | Adapter for consented worker/operator device observations, identity binding, calibrated distance estimate/uncertainty and freshness. Preserve RSSI separately; reuse I10 proximity only within validated policy. Direction is unknown unless independently observed. Mobile owns BLE radio/discovery. | Interference, missing readings, stale devices, spoofed association and uncertainty handling; real distance/direction validation before making those claims. `feat(proximity): add qualified BLE observation ingestion` |
| R06 — Voice walkaround inspection, ADD-14 | Model-specific reviewed checklist versions, per-shift inspection session, voice/tap answers, optional approved evidence references, pass/fail/unknown findings, follow-up incidents and readiness state. Machine-operation eligibility follows explicit site policy; backend does not control machinery. | Resume/duplicate answer, missing checks, failed item → corrective finding → authorised recheck, wrong model and shift rollover. `feat(inspection): add persistent voice walkaround workflows` |
| R07 — Operator-manual Q&A, ADD-15 | Curate permitted OEM/manual documents by exact model/version; ingest/index with source/page metadata, retrieve bounded passages and generate cited answers. Do not treat manuals as executable instructions or use an unsupported general LLM answer when evidence is absent. Respect document usage rights. | Known/unknown questions, wrong-model manual, obsolete version, malicious document instructions and missing source; answer with citations or a truthful limitation. `feat(manuals): add model-scoped cited manual assistance` |
| R08 — Safety/efficiency score and fuel saved, ADD-16 | Versioned transparent personal metrics with data coverage, eligible task/machine/time windows, exposure normalization, actual fuel and matched baseline. Separate observed fuel use from estimated avoided fuel; account for uncertainty and idle reasons. Keep private and avoid unfair cross-operator comparisons. | Hand-calculated fixtures, zero/partial coverage, changed baseline, reset/corrections and consent scope; never report a percentage or litres saved without a defined comparison. `feat(metrics): add explainable personal efficiency summaries` |
| R09 — End-of-shift handover, ADD-17 | Durable once-per-shift report from task outcomes, unresolved incidents/alerts, approved schedule state, permitted wellbeing risk and learning follow-ups. Include provenance/cutoff/incomplete-data flags. Corrections create a version; external sharing follows authorised approval policy. | Shift boundary/restart, unfinished task, late correction, duplicate generation and supervisor risk-only projection. No invented achievements or automatic external send. `feat(handover): add factual versioned shift reports` |
| R10 — Instructor booking and richer scenarios, ADD-18 | Instructor/availability catalogue, booking request, conflict check, approval/confirmation/cancellation and actual status; local demo bookings clearly simulated. External calendar/contact needs separately authorised integration. Add versioned branching scenario definitions, decisions, rubric, debrief and saved attempts to the core LMS practice flow. | Slot race, duplicate request, rejected/pending versus confirmed status, cancellation/restart and scenario scoring/resume; never claim a real instructor was booked from a local fixture. `feat(lms): add booking workflows and branching practice scenarios` |

Future Cat Product Link/VisionLink, Cat Detect and Cat Inspect adapters use documented authorised interfaces, canonical machine/source IDs, provenance, mapping/quality and replay protection. Their mention in the form is ecosystem alignment, not evidence of current access, a promised endpoint, or permission to connect real machinery. Verify provider terms and actual interfaces before a later integration assignment.

### Mapping to the form's fifteen-hour delivery window

The form's hours are team milestones, not a guarantee that every stage above fits in a fixed time or must be rebuilt from zero. I00 determines how much existing work satisfies them. Keep the two checkpoints early and move unfinished work visibly; do not lower correctness requirements to make the clock look successful.

| Form window | Team objective | Backend slices and joint gate |
| --- | --- | --- |
| Hours 0–2 | Voice loop/noise/PTT plus parallel generator | I00–I03 contract/foundation/data as needed; expose the existing JSON path to the independently assigned voice integration first. Stream additions follow their verified slice. Do not make the standalone voice test depend on unfinished backend work. |
| Hours 2–5 | Sessions, classifier/four branches, tasks/incidents, Android screens | I02, I04, I06, I07A and the needed I08 slice. **Checkpoint 1:** audible confirmation and exactly one stored incident visible in Android. |
| Hours 5–8 | Simulator, every core rule, conditions/wellbeing, briefing/Why, auto-drafts, estimator | I03–I05, I07B, I09–I11. **Checkpoint 2:** silent event causes speech, then correct contextual explanation. |
| Hours 8–10 | Training, re-planning and supervisor view | I12A/B/C and I13 with real client contracts/UI handoff. |
| Hours 10–12 | Man-down and poor connectivity | I14–I15 and joint phone/supervisor checks. S01 only if the form's gate is met and time remains. |
| Hours 12–15 | Integration, reconnect, latency, venue-noise tests, rehearsal/presentation | I16–I17; actual measurements and documented remaining limits. R01–R10 remain separately tracked follow-on scope. |

## 9. Exact voice adapter handoff

The backend provides contract documentation, fixtures and an independent HTTP client. The voice developer implements these steps in their own service when integration is assigned:

1. Establish/reuse the backend session from trusted room/participant context. Register/bind a stable consumer identity. Reconnect uses the same logical session; a new operator/shift does not inherit another operator's session.
2. Apply the wake gate and final-turn detection locally, strip the leading wake phrase, and send one finalized utterance with one stable `turn_id`. Keep that identity across network retries. Send bearer auth in headers and JSON with `Accept: text/event-stream`.
3. Use an asynchronous streaming HTTP client, an incremental UTF-8 decoder and a real SSE parser. Handle CRLF/LF, partial frames, multiple frames per network chunk, repeated `data:` lines joined by newlines, comments, escaped newlines and split multibyte text. Enforce maximum frame size and handle unsupported/malformed envelopes safely. Never `.json()`/`.read()` the entire live response.
4. Validate schema/session/turn/response/event identity and sequence. Track separately: last received event, speech admitted to TTS, and reported actual playback. Network receipt is not proof of speech playback.
5. Feed only `speech.delta.data.text` to the current response's TTS text pipeline. Coalesce at supported word/clause boundaries with bounded buffering. Do not invoke a second reasoning model, speak progress, re-speak terminal full text, or call an independent `session.say()` for every token.
6. Verify the installed LiveKit node/hook behavior actually starts TTS/audio before the complete reply ends. Backend HTTP streaming alone does not prove early audio; SDK node buffering and supported flush/segmentation behavior must be checked by the voice owner. Never insert fabricated fragments merely to make a latency number look lower.
7. On barge-in, stop local playback immediately, invalidate that response epoch, send idempotent cancellation and suppress late obsolete deltas even if the API is temporarily unreachable. Cancellation does not undo saved incidents/tasks/progress.
8. On a network break, query authoritative turn status and resume the same execution by cursor when appropriate. Never blindly replay speech already admitted to TTS. If admitted/playback position is uncertain, stop old speech and use the documented recovery interaction rather than guessing the unheard suffix. Superseded replies can be consumed for bookkeeping without being spoken.
9. Keep a separate session announcement subscription/cursor. Schedule trusted announcements by priority and expiry under the agreed wake/interruption policy. Deduplicate using stable event identity, including after switching between polling and streaming.
10. On generation completion, keep the result/action state without speaking it again. Report actual playback separately, using null/unknown position when unsupported. Report announcements independently. Close subscribers/HTTP clients on session teardown with bounded backoff rather than busy polling.

The sample client must exercise these application semantics without depending on LiveKit, STT or TTS packages. It can emulate admission/interruption for fixture checks, but must label that as a client simulation. Actual audio/noise/wake/latency outcomes belong to the voice integration report.

For the filled-form features, also agree these integration details before the joint demo:

- Alert explanations, shift briefings, training and SOS check-ins carry their own workflow/event correlation. A reply must resolve the right active context; receiving an alert must not silently replace an in-progress quiz or incident clarification.
- Urgent approved announcements can preempt ordinary content under the documented scheduling policy. Nonurgent training waits for an appropriate operating moment. A wake gate must not suppress every proactive alert, but ordinary background speech must not become an authorised operator command.
- The worker reports actual response/announcement playback independently of the Android app's screen/vibration reports. For SOS, no-playback/no-display information can mean unavailable contact, not a refused check-in. The backend owns timers and authoritative outcomes.
- Language preference is optional and bounded; supported speech languages must be verified by the voice owner. Noise cancellation, VAD, PTT and acoustic quality are voice/mobile responsibilities and cannot be demonstrated by a backend text fixture.
- Supervisor updates use role-filtered application feeds. Do not relay private operator speech, raw vitals or private alert evidence into a supervisor room.

## 10. Verification, measurements and demonstration gates

Tests should target meaningful domain and integration risks. Use deterministic clocks/providers for repeatable behavioral checks, and separately label actual provider/network/client runs. Tests do not validate an assumed medical/OEM threshold merely by reproducing its configured behavior.

| Risk / requirement | Required evidence |
| --- | --- |
| Form completeness | Every section 2 ID has an owner, data prerequisite, stage, completion state and evidence; no leftover missing-form/optional-core assumption. |
| Dataset correctness | Five-machine selection, units, N/A, referential integrity, provenance, real versus replay time, source preservation and no future task/label leakage. |
| Source/guidance honesty | WESAD processing trace or unmet status; Open-Meteo fixture/live labels; per-rule applicable citation/site setting/demo assumption; benchmark influence disclosed. |
| Classifier and routing | All four branches, system-event path, multi-action dependencies, low-certainty/invalid output, contextual follow-up, no arbitrary tools or classification leakage into speech. |
| Daily tasks | Correct day/site/shift, assigned machine/zone/weather/prediction, start/complete by both command paths, conflict handling and one appropriate shift briefing. |
| Incident actions | Structured saved report with real ID, draft confirmation, missing facts, duplicate/lost-response/restart handling and pending escalation represented honestly. |
| All deterministic rules | Each I10 rule family has normal/trigger/clear/retrigger/gap/duplicate tests and correct evidence, timing and applicability; safety detection works without the LLM. |
| Estimator | Full runtime input support, reduced-input policy for five provided rows, baseline 7.6, every prediction/error and actual measured MAE/count; no promised improvement or leaked targets. |
| LMS | Actual text/video asset, quiz fail/remediate/pass, saved/resumed progress, explicit levels, one behaviour-triggered assignment and no completion from playback alone. |
| Supervisor approvals | Role/site checks, risk-only health output, pending/approved/rejected/expired/stale/conflicting decisions and exactly-once local application; approval is not execution. |
| Privacy | Adversarial requests and nested payloads cannot expose raw vitals/sleep/private speech through state, incidents, feeds/replay, logs, exports or LLM-generated supervisor summaries; revocation changes future access. |
| Genuine streaming | Real HTTP client sees speech before a controlled pause and later terminal completion; completion text equals deltas; no classifier/debug/thought chunks admitted. |
| Parser behavior | Arbitrary byte/frame boundaries, split UTF-8, multiple events per packet, multiline `data:`, comments/heartbeats, malformed/oversized frames and ordered deduplication. |
| Turn idempotency/replay | Duplicate/conflicting POSTs across JSON/SSE, concurrent subscribers, lost initial response, read-only replay, bad/expired cursor and no repeated tools. |
| Cancellation/recovery | Before action, after commit, during model stream, concurrent completion, superseded late deltas, process death/restart and genuine unknown recovery outcome. |
| Playback truth | Generated/received/admitted/played/acknowledged states separate; interrupted/estimated/unknown positions; delayed reports cannot erase prior successful attempts. |
| Independent proactivity | Silent telemetry produces an event without a turn; polling/SSE share order; dedup/expiry/reconnect; “Why?” uses original evidence; briefings do not repeat after restart. |
| SOS | Offered contact versus unreachable, response/timeout races, help request, late response, persisted timers and one authorised supervisor notification. |
| Poor connectivity | Cached app state, locally saved draft, original association on delayed sync, duplicate upload, stale mutation conflict and nonvoice fallback without fake audio delivery. |
| Operations | Auth/error sanitization, rate/admission/body/output/subscriber bounds, provider timeout, SQLite contention, urgent-write progress during long speech and bounded graceful shutdown. |
| Joint clients | Actual Android screens and sound, real early TTS, wake/PTT/noise performance, React approval/status updates and phone reconnect; owner/test evidence recorded separately. |
| Stretch/roadmap | Dedicated source/hardware/content and behavior checks from S01/R01–R10; no fixture-only hardware or real-booking claim. |

### Measurements

Instrument monotonic timestamps for durations; record UTC time for correlation. Report p50/p95, sample count, scenario, machine, configuration, model/provider mode, success/failure count and cold/warm distinction. With few samples, label percentiles as small-sample observations rather than an SLA.

- **API acceptance:** request receipt to durable acceptance/first accepted event; not a completed action or a spoken answer.
- **Queue delay:** durable acceptance to execution start; include it in the bounded overall deadline.
- **First speakable delta:** request receipt to first nonempty approved speech observed by the external test client. Do not substitute progress or heartbeat timing.
- **Tool duration:** validated tool start through durable result; record action identity/type without sensitive payloads.
- **Final response:** request receipt to terminal event observed by the client; distinguish completed, cancelled and failed runs.
- **Proactive latency:** observation receipt to persisted alert/outbox, then first event observed by the subscriber. Distinguish source sampling/network delay from backend processing; simulation pace is not a hardware measurement.
- **Joint voice latency:** microphone/STT finalization, backend first text, TTS start and first audible playback belong to the joint integration report. Backend text latency alone does not establish end-to-end speech latency.

Document regressions and source of delay before tuning; use caches, bounded history and per-episode work to reduce cost. Do not insert fake acknowledgement text, sleep optimizations or mock timings to improve a live number.

### Final demonstration sequence

Use synthetic actors/consents and reproducible data. Retain the IDs and results so another team member can replay the evidence.

1. Start the backend in the declared mock/live mode, provision scoped demo actors and select a single exact machine/operator/shift. Show data provenance and the daily task dashboard with zone, prediction and weather.
2. Deliver the shift briefing once. Ask for the next task, explain its predicted duration, then start/complete a suitable task by voice and verify the same state by a tap/API read.
3. Say “log a test incident”; clarify missing details if needed; hear confirmation only after persistence and show exactly one incident in Android. Repeat the same request identity/recover a lost reply and confirm no second incident. Record **Checkpoint 1** only with the real audio and app evidence.
4. Without a user turn, inject engine-on/unfastened telemetry before motion. Hear the proactive warning; ask “Why?” after the latest reading changes and receive the original explanation. Show the linked draft and learning assignment. Record **Checkpoint 2** only with actual unsolicited audio and follow-up.
5. Run proximity, idle/combined idle, abnormal fuel/cycle, sudden start/stop, slope, repeat-violation and working-condition scenarios. Show dedup/reset and a clear statement of configured thresholds/provenance. Group short scenarios for rehearsal without omitting their stored proof.
6. Demonstrate heat/break advice with synthetic consent; show the supervisor risk-only projection and a denied raw-vitals request. Revoke sharing and verify subsequent views honor the change.
7. Open the assigned 60-second seatbelt lesson, show text and playable video, take a voice quiz, fail/remediate/pass as scripted, then reconnect and display saved progress/level evidence.
8. Inject a forecast change, create a reordered schedule proposal, reject one proposal and approve a valid one in the supervisor view. Show no schedule change before approval and exactly one application afterward. Display a repeat-violation escalation request with its true status.
9. Trigger an impact check-in. Demonstrate “I'm okay,” then a separate unresolved/unreachable scenario leading to the appropriate single supervisor notification. Restart with a pending timer and verify recovery.
10. Disconnect Android, show cached tasks, save an incident draft locally, reconnect and upload twice. Show one server record with original association, a stale-command conflict, and screen/vibration fallback where voice is unavailable.
11. Interrupt a reply after a committed action, report partial/unknown playback, reconnect/replay status and issue a follow-up; no duplicate action or stale speech. Run S01 only if its entry gate is met. Verify five-machine selection through smoke evidence.
12. Present actual benchmark errors, latency samples, source/provenance evidence and honest feature status. Do not present the roadmap/hardware integrations as working merely because their contract exists.

## 11. Evidence, handoff and source references

### Handoff after every assigned increment

Update the repository handoff with:

- Stage/requirement IDs and what behavior actually changed; exact owned files and migration/config changes.
- Focused checks run, environment/provider/data modes, actual results and evidence paths; clearly separate not-run or blocked checks.
- Commit hash and verified author/committer; remaining working-tree changes by owner without staging them.
- New/changed request, state and event examples, compatibility effects and recovery behavior.
- Remaining source/data/client prerequisites, known limits and the next one assigned increment.

For each milestone retain an evidence artifact with command/config/fixture version, expected result, observed result, relevant IDs and sanitized logs/screens where available. Mark manual/joint checks with who ran them and when; do not invent observations. A plan stage may be implemented while the external integration/source gate remains unverified.

### Alignment with every form field

| Filled-form field | Plan treatment |
| --- | --- |
| 1–9: team/contact details | Project attribution to Team Butterfly; these details are not operator accounts, authentication secrets or a replacement Git identity. Do not seed teammates as machinery operators automatically. |
| 10: solution | Proactive voice-first workflows, authoritative agent state, actual actions, approval/privacy and connectivity boundaries in sections 2–4 and 6–10. |
| 11: technologies | Python/FastAPI/LangGraph/SQLite/pandas; WESAD and Open-Meteo with actual source gates; Android/LiveKit/STT/TTS/noise and React ownership; roadmap technologies explicitly tracked. |
| 12: AI use | Initial LLM routing, multi-action planning, structured incidents/explanations and context; fixed rules own detection; approval precedes the applicable effects. |
| 13: approach | Data-first extension, preserved source sample, measured benchmark, idempotency, four team streams, both risky checkpoints and honest provenance. |
| 14: features/USPs | All five required groups and all eighteen additional entries mapped individually; proactive actions/explanations/wellbeing/connectivity/cost/ecosystem claims need the specified evidence. |
| 15: reported milestones | Treated as team-reported progress until I00 verifies relevant repository/data artifacts; no retroactive assertion that this plan executed them. |
| 16: next fifteen hours | Both checkpoints, complete timed core, gated service reminder, final reconnect/noise/latency/rehearsal and explicit follow-on work preserved in section 8. |
| 17: filming/photography notice | Event participation/consent matter for the team, not an application feature. This plan neither selects the form's Yes/No choice nor substitutes for participant consent. |

### Primary references and version checks

These support implementation choices and source qualifications; they do not replace the repository's installed API behavior or a reviewed model/site safety policy. Verify current supported methods and actual source terms during the assigned increment. Do not cite a page for a numeric threshold it does not contain.

- **S1 — WESAD official dataset record:** [UCI WESAD: Wearable Stress and Affect Detection](https://archive.ics.uci.edu/dataset/465/wesad+wearable+stress+and+affect+detection). Source metadata, signals and study context.
- **S2 — Original WESAD paper:** [Introducing WESAD, a Multimodal Dataset for Wearable Stress and Affect Detection](https://ubi29.informatik.uni-siegen.de/usi/pdf/ubi_icmi2018.pdf). Laboratory design and signal interpretation; not a heat/fall/fatigue validation study.
- **S3 — Weather:** [Open-Meteo official API documentation](https://open-meteo.com/en/docs). Requested variables, units, time ranges and provider behavior.
- **S4 — Occupational heat guidance:** [CDC/NIOSH OSHA-NIOSH Heat Safety Tool](https://www.cdc.gov/niosh/heat-stress/communication-resources/app.html). Heat-index screening and WBGT distinction; additional applicable guidance must support actual occupational thresholds.
- **S5 — Heat index:** [NOAA/National Weather Service heat-index guidance](https://www.weather.gov/ama/heatindex). Definition and conditions of use; select and test an authoritative computation method with its limits.
- **S6 — Graph streaming:** [LangGraph Python streaming](https://docs.langchain.com/oss/python/langgraph/streaming). Inspect installed version and event/content shapes before implementing the speaking projection.
- **S7 — FastAPI response streaming:** [FastAPI custom responses](https://fastapi.tiangolo.com/advanced/custom-response/). Confirm actual server/proxy/client flushing over a socket.
- **S8 — Voice adapter boundary:** [LiveKit Agents nodes](https://docs.livekit.io/agents/logic/nodes/). Voice owner verifies installed hook behavior; the backend supplies text contracts.
- **S9 — Voice text/audio handling:** [LiveKit Agents audio](https://docs.livekit.io/agents/multimodality/audio/). Actual TTS/playback/interrupt behavior belongs to joint integration evidence.
- **S10 — Vertex authentication:** [Google Cloud Application Default Credentials](https://docs.cloud.google.com/docs/authentication/application-default-credentials). Use supported SDK discovery without manually handling the user's credential file.

Additional model-specific Caterpillar/manual and site-policy references must be recorded in the rule/content registry as obtained. No generic machine blog, toy listing or video title supplies a validated proximity distance, slope limit, service interval or medical threshold.

## 12. First Claude Code assignment

Use this first assignment after saving the plan in the repository root. It intentionally starts with the evidence audit, because the screenshot shows an existing backend and unreviewed changes. Subsequent prompts assign one verified next increment and repeat the scope/commit requirements.

```text
Read BACKEND_IMPLEMENTATION_PLAN.md revision 2.1 and the existing root/service
instructions, API_CONTRACT.md, contracts, backend code, tests and handoff.
Execute I00 only. Reconcile the actual checkout with every REQ/ADD/SYS entry
and the filled Review 1 scope in this plan. Inspect the dataset manifest and
record concrete missing fields and verified existing behavior. Do not claim
source integrations, hardware, streaming or app integration from fixtures alone.

Work in langgraph-agent and the scoped shared documentation. Continue the
selected backend feature branch, preserve all colleague changes, and do not
modify the standalone voice worker. Do not implement later stages yet.

Create/update the feature matrix, data-gap report and handoff with actual
baseline check results, source limitations and the next increment. Use
Naif Naqeeb as BOTH author and committer, keeping the Git email already
configured for Naif in this checkout. This overrides earlier identity
instructions. Stage only reviewed changes for I00 and make a coherent
local commit. No Claude/bot author, AI co-author trailer, Committed
by Claude text or generated-by footer. Verify the actual commit metadata.
Do not push, merge, deploy, reset/stash colleagues' work or read credential files.

Report the commit hash, verified author/committer, changed files, checks and
remaining blockers, then stop so we can assign the next increment.
```
