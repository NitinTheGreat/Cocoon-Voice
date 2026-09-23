# Cocoon backend: fast-track execution plan

Revision 1.0 · 23 September 2026 · Backend owner: Naif Naqeeb

Put this beside `BACKEND_IMPLEMENTATION_PLAN.md` in the repository root. This is the new execution order, based on the user's request to integrate voice immediately and deliver working features with less process. It overrides the old phase ordering and repeated verification gates. The filled Review 1 form, its requirement matrix, compatible contracts and ownership boundaries remain the scope reference.

## 1. Starting point

The following is reported by the implementation agent, not independently inspected in this document:

- `252124d` completes I02b on `backend`; database schema is version 3. Preserve the preceding user merge `2ea49eb` and all teammate changes.
- Catalog sessions, migrations, actor tokens, `/v1/me`, existing JSON turns, incidents and announcement polling work. The three shared read-only demo tasks are not yet the required daily task workflow. The existing training assignment tool is not yet the complete LMS.
- Vertex integration, 11 provider tests and `docs/VOICE_INTEGRATION.md` are pending local changes. Reported results: 281 tests passed, one skipped; live responses sometimes succeed, but frequent 429 errors interrupt turns.
- The voice worker works standalone. Its `remote_langgraph` adapter still needs wiring. Working backend HTTP calls do not establish a completed microphone-to-speaker integration.
- SSE, backend cancellation and the proposed per-turn playback-report route are not implemented. Current playback reporting covers announcements.

Inspect the actual checkout and reuse working behavior. Do not repeat I00/I01 or rebuild existing components to match this document's suggested structure.

## 2. Delivery order

Each batch is a feature milestone, not a requirement for one giant commit. Commit each coherent working slice. Execute only the batch assigned in the current prompt; follow-up batches remain ready to assign without another architecture exercise.

| Batch | Working result | Previous work combined | Completion evidence |
| --- | --- | --- | --- |
| A — Connect now | Vertex changes committed; voice sends finalized utterances to the backend and speaks its saved reply; independent announcement polling connected | Pending provider work, existing JSON contract, essential I04/I16 integration | Actual spoken task lookup and exactly-one incident; silent alert and contextual “Why?” where existing rules support it |
| B — Operator workflow | Initial classifier, assigned daily tasks, start/complete, structured incidents, selected-machine replay, seatbelt/idling alerts, automatic drafts, shift briefing and explanations | Necessary I02d/I03 work with I04–I07A/I10/I11 | One continuous task → incident → proactive warning → follow-up demonstration |
| C — Complete the five required outcomes | Remaining safety/unusual-behavior rules, working conditions, task estimates and the complete core LMS | Remaining required data, I07B/I09/I10/I12 | All five required groups work, with one practical scenario per behavior and truthful estimator results |
| D — Complete scheduled additions | Consent and wellbeing, scoped supervisor data, approvals, weather re-planning, SOS and offline synchronization | I02c, relevant I02d, I13–I15 | Operator/supervisor scenario, wellbeing privacy check, SOS check-in, offline retry without duplicates |
| E — Streaming and final handoff | Incremental speech, cancellation, replay/recovery and delivery tracking, followed by final joint rehearsal | I08 and remaining I16/I17 | Real-network streaming and interruption checks against the agreed contract |

The first usable-demo target is A plus B, then C. Completing A alone does not mean the five required outcomes are done. D and E stay in the implementation backlog; move E earlier only if measured full-reply latency is blocking an otherwise working demo. Streaming improves delivery timing; it does not itself fix model-capacity errors.

## 3. Batch A: the immediate assignment

### Backend owner

1. Inspect and commit the pending Vertex work, including its dependency lock changes and voice handoff. Fix concrete defects discovered in that diff; avoid unrelated refactoring.
2. Keep the selected configuration:

   ```dotenv
   COCOON_LLM_MODE=live
   GOOGLE_CLOUD_PROJECT=orbit-507316
   GOOGLE_CLOUD_LOCATION=global
   GOOGLE_GENAI_USE_VERTEXAI=true
   VERTEX_MODEL=gemini-3.8-flash
   COCOON_SERVICE_TOKEN=<local-service-secret>
   ```

   Reconcile these names against the actual loader; keep `COCOON_LLM_MODE`, already reported as implemented. The local environment may use `GOOGLE_APPLICATION_CREDENTIALS=C:\Users\HP\Downloads\test\application_default_credentials.json`, as selected by the user. Let the SDK perform authentication. Never inspect, copy, print or commit that credential file. Committed environment examples use placeholders only. Keep live failures explicit; mock mode must be deliberately selected.
3. Investigate 429 using sanitized status, retry information and any quota identifier returned by the provider. A 429 alone does not prove a two-call project quota. Keep the selected project, global location and model. No quota purchase or model substitution is authorized by this plan.
4. Inspect existing retry behavior before adding another layer. Start with one concurrent backend model request, a bounded queue and a total turn deadline. Use bounded backoff with jitter for genuinely retryable provider failures; coordinate SDK, backend and voice retry budgets. Do not retry authentication/configuration errors as capacity failures.
5. Preserve the initial typed LLM classifier. Where it already returns sufficient validated intent/entities, produce task-status replies and action confirmations from tool results without an obligatory second LLM call. Confirm actions only after their database commit. Do not make a classifier rewrite a prerequisite for connecting voice; remaining optimization belongs in B.
6. Supply exact working requests, responses, runtime configuration and error handling in the existing `docs/VOICE_INTEGRATION.md`. Distinguish committed action outcomes from failed answer generation. An uncertain result must lead to status lookup, not a fresh incident request.

### Interface available for the voice owner

Use the runtime contract and existing client; proposed schemas do not establish implemented routes.

| Call | Use now |
| --- | --- |
| `POST /v1/sessions` | Create/retrieve the trusted room/operator/machine association |
| `POST /v1/sessions/{session_id}/turns` | Send one finalized utterance and receive the complete JSON result; preserve stable `turn_id` |
| `GET /v1/sessions/{session_id}/turns/{turn_id}` | Inspect processing, saved result and known outcomes after uncertainty |
| `GET /v1/sessions/{session_id}/state` | Read authoritative application state |
| `POST /v1/sessions/{session_id}/telemetry` | Simulator/backend integration input; the worker need not invent readings |
| `GET /v1/sessions/{session_id}/events?after={cursor}` | Poll proactive announcements independently of user speech |
| `POST /v1/sessions/{session_id}/events/{event_id}/delivery` | Report announcement playback using implemented outcomes |

Authenticate server-to-server with the shared `COCOON_SERVICE_TOKEN`. Never put it in Android or the supervisor frontend. Use trusted room/participant bindings and catalog IDs such as `EXC_DEMO_001` and `OP_DEMO_1_1`; untrusted participant metadata cannot choose another operator's session.

### Voice owner

- Wire `VOICE_BRAIN=remote_langgraph` into the worker's existing `llm_node`/backend client using the installed LiveKit interface. Keep wake gating, STT, VAD, noise processing, TTS, PTT and playback in the voice service.
- Send the finalized, wake-approved utterance once. Use stable session and turn identities across transport retries. Handle the existing 200 completed / 202 processing behavior; recover uncertain requests through status before deciding whether a retry is permitted.
- Speak only the backend's user-facing answer through the current TTS path. Do not run the answer through the standalone LLM or repeat the request in both modes.
- Poll announcements on a bounded interval while the operator is silent; respect cursor order, deduplication, expiry and priority. An eligible critical alert can speak without a wake phrase. Report actual playback outcomes, not merely HTTP receipt.
- During JSON-mode barge-in, stop local playback and discard obsolete responses using a response generation/epoch. The backend action may still finish. Do not call an unimplemented cancel or per-turn delivery route, or promise action rollback. Serialize state-changing follow-ups while an earlier outcome is unresolved.
- Keep both services independently runnable. Make voice changes only in the voice owner's assigned checkout and paths.

### Short acceptance run

First use explicit mock mode to test wiring; then try a small number of sequential live turns. Stop a failing live run after collecting actionable error evidence rather than repeatedly exhausting capacity.

1. Real microphone → finalized utterance → backend → audible reply: “What is my next task?”
2. Speak a test incident with enough facts. Confirm the stored ID; retry the same turn and confirm there is still one incident. Check Android if available; a state read proves only the backend part of Checkpoint 1.
3. Inject a supported simulated safety event without speaking. Hear the announcement, then ask “Why?” and check it refers to that saved alert. If an existing rule cannot support this, record the exact missing piece for the first B slice.
4. Interrupt a reply and ensure its late text is not played. Inspect the final action outcome separately.

Record mock/live mode, actual success/failure, turn latency and whether audio/Android were observed. Do not label a mock-only connection a successful live-model demo.

## 4. Batch B: make the existing backend useful

Keep one coherent demonstration on Cat 320 first; preserve selection of all five catalog machines. Other-machine smoke checks follow once the flow works.

- **Classification:** retain an explicit initial structured LLM decision routing to tasks, safety/incidents, training or general assistance. Resolve “Why?”, “yes”, quiz answers and incident clarifications from persisted workflow references. Clarify ambiguity. Validate a multi-action plan such as “log this and tell my supervisor”; save the incident and create the approval request without claiming an unapproved escalation was sent.
- **Task lifecycle:** import a small, explicitly synthetic site/shift/assignment fixture; show today's operator/machine tasks, zone, weather and duration source. Persist start/complete commands from voice and tap through the same service. Reject stale versions and unauthorized transitions. Emit a once-per-shift briefing.
- **Incidents:** structure what/where/when/severity/zone, use trusted operator/machine bindings and clarify missing facts. Preserve confirmed reports separately from alert-generated drafts. A repeated confirmation must reuse the same record.
- **Telemetry and rules:** run selected-machine replay through the actual ingestion endpoint. Implement engine-on/unfastened-seatbelt detection before motion, prolonged idling and their combined pattern. Record episodes and evidence before announcements; repeated samples do not create repeated incidents. Preserve stale/unknown status and explicit simulation provenance.
- **Follow-up:** “Why?” uses the exact alert evidence. “I'm waiting for a truck” records operating context without clearing an unfastened-seatbelt condition. Warning publication must not wait for an LLM call.
- **Learning connection:** link a qualifying episode to a real, deduplicated lesson assignment once suitable lesson content exists. The complete quiz/progression flow is delivered in C.

Add the minimal durable operation records and database constraints needed by these writes now. Keep transactions short and separate from remote model calls. Defer a generalized command framework until a concrete feature needs it; do not defer duplicate-action protection. Preserve the verified-versus-legacy training isolation fixed in I02b.

## 5. Batch C: finish the required feature set

1. **Safety and unusual behavior:** add proximity warning/danger zones with distance/direction, fuel per compatible load-cycle window, sufficiently sampled sudden starts/stops, tilt/slope policies, distinct repeat-violation episodes and supervisor escalation requests. Guard zero cycles, missing sensors and stale observations. Fetch numeric weather with a fixture fallback explicitly labeled as such; check temperature, humidity, rain, wind and visibility before outdoor work.
2. **Task estimation:** use type, quantity, ground, weather, skill and machine age when available; explain factors and missing-input fallback. Preserve the supplied five rows and recompute their 7.6-minute baseline MAE. Report the actual result; do not tune on those outcomes and call them an independent test, or promise improvement that has not occurred.
3. **LMS:** deliver versioned short text lessons, at least one genuinely playable reviewed video, voice quizzes, saved attempts/progress, explicit Beginner/Intermediate/Expert learning criteria, behavior-triggered assignments and one small authored practice scenario. Learning levels are not equipment certification. “Video coming soon” and an assignment counter do not complete this requirement.

Extend data only as each feature needs it. Use versioned generator inputs and small synthetic scenarios, retaining the original v1 snapshot and provided rows. New site/shift bindings must be trusted server configuration; they do not rebind existing immutable sessions. Use compatible machine-specific task/rule policies across all five assets. Daily jerk averages cannot prove second-by-second motion or human impact.

Record rule-policy sources and applicability. Where a verified threshold is unavailable, label the configuration as a demo assumption and leave that source requirement open. WESAD acquisition/calibration stays tracked explicitly; do not claim WESAD-derived vitals from hand-chosen synthetic ranges. These source gaps need not stop unrelated task and incident work.

## 6. Batch D: scheduled additional features

Build these as small working slices, reusing the same event, action and approval records:

- **Consent + wellbeing:** operator-only, versioned purpose-specific grants/revocation before wellbeing processing. Missing consent or inputs produces unavailable status. Combine permitted heart rate, skin temperature, heat index and shift/break information under documented deterministic policies. Persist explanations; supervisor projections expose risk level only, never raw vitals, sleep or private trigger evidence.
- **Supervisor + re-planning:** establish trusted site scope before enabling supervisor reads. Add alerts/incidents/approval projections and the change feed needed by the React owner. Propose weather-based task reordering; an authorized one-tap decision applies the exact version once. Handle rejection, expiry and changed schedules without silent overwrite.
- **SOS:** use a distinct human-impact source, persist a check-in and deadline, accept the operator's response, and notify the scoped supervisor under a documented pre-authorized emergency policy when unresolved. Distinguish no response from failed delivery. Keep ordinary escalations behind approval. Prototype notifications stay within the application.
- **Poor connectivity:** expose connection/freshness information and versioned task snapshots. Accept offline incident drafts/commands with stable IDs and original operator/machine/shift. Reconcile after reconnect with version/conflict handling. Android owns its cache, screen and vibration; backend success alone does not prove offline-device behavior.

Engine-hour service reminders remain the gated stretch after Checkpoint 2. Camera drowsiness/sleep prediction, real smartwatch acquisition, expanded Indian-language voice, on-device offline rules, BLE, walkaround, manual Q&A, efficiency/fuel-savings scores, handover reports, instructor booking and richer simulation remain named follow-on requirements in the original plan. None is silently marked completed or removed by this execution change.

## 7. Batch E and checks that remain necessary

Implement the agreed SSE/cancellation/replay protocol after the first functional JSON demo, using the existing proposed contract. Preserve durable turn identity, saved events before exposure, speaking-node filtering, action outcomes, separate announcements and playback records, exactly one terminal outcome and safe restart handling. Socket closure must not rerun tools or undo actions. Verify incremental output over a real localhost connection; an in-process buffered client cannot prove streaming. Keep the earlier unresolved hard-kill observation open until a real recovery check succeeds.

Use a lean verification policy throughout:

- Run existing focused tests for the behavior changed. Add a regression check for a concrete defect or new consequential behavior; avoid duplicating fixtures merely to increase test counts.
- Keep one short real-HTTP smoke for each working slice. Preserve duplicate-write, cross-operator access, post-commit failure and consent/privacy checks where relevant.
- Run the migration upgrade check only when schema changes; contract drift checks when public models change. Keep existing tests; do not delete or weaken assertions to make progress look faster.
- Run the complete existing suite once before the joint demo/release checkpoint, or earlier for a broad cross-cutting change. Do not rerun it after every documentation edit.
- Maintain one concise handoff entry per commit: behavior, commands/results, remaining blockers and commit ID. Update contracts/feature status when behavior changes; avoid rewriting every document per slice.

A missing teammate or live-model capacity does not block all backend work: deliver the runnable adapter/contract or explicit mock wiring, record the unverified joint gate, and proceed with independent feature work. Never convert an unobserved integration into a passing result.

## 8. Git and ownership

Backend commits use repository-local author **and** committer `Naif Naqeeb <naifnaqeeb.123@gmail.com>`. Verify actual metadata after committing. No AI co-author, generated-by footer or other author identity. This is already authorized; no further confirmation is needed for these scoped local commits.

Preserve the user's current branch history, teammate files and uncommitted work. Stage explicit owned paths; do not stage the untracked dataset wholesale. Commit generator/fixture changes only within the assigned ownership. Do not push, merge, deploy, reset, stash or rewrite prior authorship. Voice-owner commits follow their own authorized identity; this backend instruction does not reattribute their work to Naif.

## 9. First prompt for the backend Claude session

```text
Execute Batch A of BACKEND_FAST_TRACK_PLAN.md now. This is an implementation assignment, not another audit or proposed plan. The user has changed the order: connect voice through existing JSON APIs before continuing standalone I02c/I02d phases. Preserve the original form scope.

Read the current diff, backend instructions, runtime API_CONTRACT.md and docs/VOICE_INTEGRATION.md. Preserve 252124d, the user's voice merge and newer work. Reuse the pending Vertex integration. Verify installed/configured behavior; use gemini-3.8-flash on project orbit-507316, global, through the existing Google SDK authentication. Leave the user-selected credentials file opaque to the SDK; never read or print it. No silent mock or alternate-model fallback.

Complete and commit the pending provider changes, with focused provider/turn tests and the existing HTTP smoke. Diagnose 429 from sanitized evidence; do not assume a fixed project quota. Bound concurrency/retries and avoid mandatory second model calls where the existing typed classifier plus persisted result already suffice. Keep the initial classifier and all authorization/idempotency protections. Avoid unrelated rewrites and repeated full suites.

Update the existing voice handoff with exact current JSON requests/results, catalog IDs, errors, same-turn retry/status behavior and announcement polling/delivery. Keep proposed SSE/cancel/turn-delivery endpoints clearly unavailable. Do not modify livekit-voice or its worker; provide the voice owner a ready integration handoff. A voice-owner delay must not cause repeated backend smoke runs.

Commit each coherent backend slice locally as Naif Naqeeb <naifnaqeeb.123@gmail.com>, both author and committer, with no AI attribution. This is already authorized. Stage only owned changes; no push, merge or teammate changes. Verify actual commit metadata.

Return a short report: commits, what now runs, focused checks, sanitized model failure evidence if any, exact backend startup command and the voice owner's remaining action. Distinguish backend HTTP evidence, mock/live behavior and actual audio. Stop after Batch A's backend work and handoff; the next assignment is Batch B, not an automatic return to I02c.
```

## 10. Prompt for the voice owner's Claude session

```text
Integrate the existing standalone voice worker with the backend now using its implemented JSON APIs. Read langgraph-agent/docs/VOICE_INTEGRATION.md, API_CONTRACT.md and Batch A of BACKEND_FAST_TRACK_PLAN.md. Preserve current voice behavior and compatible PR #2 work. Modify only the voice owner's assigned files; do not rewrite the backend.

Wire VOICE_BRAIN=remote_langgraph into the existing backend client and supported llm_node path. Keep STT, wake gating, VAD, noise cancellation, TTS, PTT and actual playback here. Send trusted session associations, catalog machine/operator IDs and one finalized utterance with a stable turn_id. Speak only the backend's user-facing result; do not reprocess it through another LLM. Recover ambiguous submissions through authoritative status and preserve the same turn ID where retries are allowed.

Poll retained announcements independently while the operator is silent. Implement cursor/dedup/expiry/priority handling and report actual announcement playback. Barge-in stops local audio and suppresses late obsolete replies. The JSON backend has no cancellation/per-turn-delivery endpoint yet: do not invent one or imply a committed action was cancelled. Keep state-changing follow-ups ordered when prior outcomes are unresolved.

Use explicit mock mode to verify wiring, then a small sequential live run with the backend owner. Prove task lookup, one incident despite a repeated request, and a silent warning followed by Why. Record whether actual audio and Android were observed; do not claim joint checks you cannot run. Keep existing service credentials server-side and out of logs.

Run focused checks for the adapter and one joint rehearsal, then make a scoped local commit using this voice owner's already authorized identity and report the actual result. Do not push, merge or take over another owner's changes.
```

## 11. Model references checked for this revision

- [Google: Gemini 3.8 Flash developer guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/guides/gemini-3-8-flash): model ID `gemini-3.8-flash`; supported thinking levels include `LOW`; `MINIMAL` is unsupported. Verify SDK request options against the installed version rather than introducing unrelated dependency upgrades.
- [Google: model API errors](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/api-errors): 429 can reflect quota or shared-capacity pressure. Diagnose the actual response and use bounded retries; raising quota is not an established fix for the reported runs.
