# Cocoon — LangGraph agent

## Purpose and scope

- Cocoon is Team Butterfly's proactive operator assistant for a Caterpillar hackathon. The implementation budget is roughly 12 hours: deliver a small working integration first.
- This folder owns reasoning, validated actions, application state, persistence, and proactive rules. `../livekit-voice` owns voice transport. `Cocoon-App` is a separate Android repository and will connect later.
- Demo capabilities: retrieve a task, record an incident, assign/retrieve a training lesson, issue a simulated alert without a user prompt, and explain that alert in a follow-up.
- The original problem statement and datasets were not available when this file was prepared. Do not invent mandatory requirements or claim full compliance. Reconcile supplied materials when available.
- These instructions capture the agreed design, not evidence that code exists. Initial implementation and test status are **UNVERIFIED**. Inspect the checkout before changing that status.

## At the start of every task

1. Read applicable repository instructions, this file's handoff section, this folder's README, and root `API_CONTRACT.md` / `contracts/openapi.yaml` if present. Read relevant contract examples and recent handoff records.
2. Inspect `git status`, the current branch, recent changes, dependency files, and relevant code. Preserve colleagues' changes; do not reset, clean, or overwrite unrelated work.
3. Distinguish requested behavior, existing implementation, and verified behavior. If code conflicts with the contract, record the mismatch and fix the relevant side; do not quietly redefine the contract.
4. Select one useful task in the user's scope. Add/update your active-work row with owner, branch, files, and next checkpoint. If ownership is unknown, use the branch/session identifier, not an invented teammate name.
5. Implement and verify the smallest complete flow. Missing live credentials should not prevent contract, persistence, graph, and mock-mode work.

## Ownership and architecture

- Backend owner maintains this service and proposes changes to the shared API specification, examples, and contract checks. Voice owner maintains its client and mock backend. Keep both owners' handoffs consistent when a contract changes.
- Services communicate through HTTP JSON only. Never import sibling Python modules, share a virtual environment, or allow voice code to access the backend database.
- Run FastAPI/Uvicorn on port **8000** by default, using **one application worker** for the prototype. Provide `/docs`, `/healthz`, and `/readyz`; liveness and readiness must have truthful meanings.
- Voice uses LiveKit WebRTC, STT, our HTTP API, then TTS. The normal text LLM and agentic workflow live here. No realtime speech model or reasoning LLM belongs in the voice service.
- Use Python 3.11 unless existing compatible configuration warrants otherwise; a valid package name such as `cocoon_agent`; local `requirements.txt`, `requirements-dev.txt`, `.env.example`, README, and tests.
- Pin dependencies after resolving compatible versions. Check installed LangGraph/checkpointer APIs and current official documentation before using unfamiliar signatures.
- Keep environment loading independent of the caller's working directory. Gitignore secrets, local environments, databases, and generated artifacts.

## Shared HTTP contract — v1 baseline

Root `contracts/openapi.yaml`, `API_CONTRACT.md`, and examples are the shared reference once created and checked against FastAPI. This summary is a bootstrap baseline, not a separate specification. Document exact types, enums, defaults, pagination, and retry semantics there before wiring clients.

| Method and path | Required purpose / fields |
| --- | --- |
| `POST /v1/sessions` | Create/retrieve by stable `client_session_key`; receive `room_name`, `participant_identity`, `operator_id`, `machine_id`; return server-issued `session_id`. |
| `POST /v1/sessions/{session_id}/turns` | Receive `turn_id`, `text`, `source` (`voice` or `text`); completed result includes `session_id`, `turn_id`, `status`, `speech`, typed action results, `state_version`. |
| `GET /v1/sessions/{session_id}/turns/{turn_id}` | Retrieve processing status or the saved result after retries/timeouts. |
| `GET /v1/sessions/{session_id}/state` | Read tasks, incidents, training assignments, active alerts, and `state_version`. |
| `POST /v1/sessions/{session_id}/telemetry` | Receive `event_id`, UTC `observed_at`, `simulated=true`, typed readings such as `engine_on`, `seatbelt_fastened`, `idle_seconds`. |
| `GET /v1/sessions/{session_id}/events?after={cursor}` | Read retained events in order; each has `event_id`, `sequence`, `type`, `priority`, `speech`, `created_at`, optional `expires_at`. |
| `POST /v1/sessions/{session_id}/events/{event_id}/delivery` | Receive stable `consumer_id` and playback `status`: `played`, `interrupted`, `failed`, or `expired`. |

- Use typed Pydantic models. Errors consistently expose `code`, `message`, `retryable`, and `request_id`. Apply the agreed envelope to validation errors too.
- Completed turns return **200**, including saved results for duplicate requests. A duplicate still processing returns **202** with polling instructions. Reusing an ID for a different payload returns **409**. Use appropriate 401/404/422/5xx responses.
- Authenticate application endpoints with a service bearer token from environment configuration. Do not expose provider credentials through API responses, browser tokens, fixtures, or logs. Bind locally by default.
- Include stable correlation IDs and UTC timestamps. Keep responses short and nonstreaming initially; `speech` must contain operator-facing language, not tool traces or classifier JSON.
- Backend ownership of the schema does not authorize silent breaking changes. Update schemas, examples, affected consumers/mocks, and a contract check together within the task's scope; otherwise record the required coordinated change.
- Resolve unspecified details in the canonical contract, including cursor persistence, event expiry, delivery retries, and session identity conflicts. Do not leave either developer to infer incompatible behavior.

## Graph and business behavior

- Build a typed `StateGraph`: load context, route intent, execute a validated action or ask for missing information, then compose a short spoken response. Avoid routing everything to an unrestricted chat response.
- Keep one configurable normal text LLM provider in live mode. Prefer the repository's existing provider. Validate structured routing/tool arguments before touching business state.
- Explicit mock mode uses deterministic model responses through the **same graph and tools**, requires no model credentials, and clearly identifies itself. Live mode must not silently substitute mock answers.
- Minimum actions: retrieve a seeded next task; persist an incident with a real ID; assign/retrieve one of three seeded lessons; explain the latest alert. A simple lesson catalog is sufficient for this prototype.
- Ask for missing required information and persist the pending question/action. Short follow-ups must resolve against the correct session context.
- Only state that an action succeeded after the corresponding record is committed. A generated response, tool selection, or proposed action is not completion.
- An interrupted audio response does not cancel a committed backend action. Do not infer business cancellation from an HTTP disconnect.

## Persistence, ordering, and retries

- SQLite is exclusively owned here. Persist sessions, business records, turn results, alert episodes/events, delivery progress, and conversation state/checkpoints. Initialization and seeding must be repeatable.
- Use the server-issued `session_id` as the LangGraph thread ID. Add only the new utterance once; do not replay the full voice transcript into an existing checkpoint.
- Serialize state-changing operations per session, including telemetry relevant to graph context. Prevent two turns or a turn and an alert update from losing each other's state. Keep sessions isolated.
- Use database uniqueness and transactions for request IDs and side effects. A process-local dictionary alone is insufficient. The same completed `turn_id` returns its saved result without rerunning tools.
- Persist enough provenance to associate a mutation with its turn. Define recovery for a crash between mutation, checkpoint, and response persistence; never blindly re-execute an uncertain action.
- Document prototype limits: a single worker, the actual serialization mechanism, and unfinished-turn recovery. Do not promise exactly-once execution or multi-worker safety without implementing it.
- Keep persisted state and `state_version` coherent. Ordinary conversation context and saved records must survive a restart; do not mark this verified based only on an in-process check.

## Proactive flow

- Own a small HTTP telemetry simulator and one deterministic demo rule, such as engine on with seatbelt unfastened. Label both inputs and rule as simulated and unvalidated for real machinery.
- Detect a new condition episode, persist the alert and explainable context, then expose one announcement. Repeated samples of the same active condition must not spam events; clearing and retriggering starts a new episode.
- Deduplicate telemetry IDs and define how stale/out-of-order observations are handled. Use server time for receipt and preserve observation time; do not silently make stale data the current state.
- Events are retained ordered records, not a destructive queue. A reader advancing its polling cursor does not itself mean speech was played. Specify how the consumer resumes unresolved delivery after reconnect.
- Make delivery reports idempotent under the documented state transition rules. `played` is separate from human acknowledgement and from a resolved condition.
- The voice worker polls and speaks the supplied text. This backend must not import LiveKit, hold a voice-session object, or require an incoming user message to create an alert.
- Ensure “Why did you warn me?” and a contextual “Why?” can explain the persisted alert. Alert context must be available before its event is visible to the worker.

## Local development and evidence

- Provide a text CLI or HTTP smoke script that runs the real backend without LiveKit dependencies or credentials. Keep the simulator similarly independent.
- README must state exact installation, configuration, database initialization, startup, shutdown, and test commands, plus PowerShell/Bash differences. Record the working directory for each command.
- Document selected environment variable names in `.env.example`: service token, database location, mock/live mode, provider key/model, and network settings. Never store values in this file.
- **Commands currently verified:** none; this file was prepared without inspecting the checkout. Replace this entry with tested commands and their environment/commit when available. Do not invent runnable module paths.
- Focus checks on contract conformance; persisted incident retrieval; duplicate/conflicting turns; pending follow-ups and session isolation; repeated/reset alerts; event delivery/cursors; restart persistence; and uncertain outcomes after timeouts.
- Run only checks relevant to the change and required integration gates. Add tests for consequential behavior, not for documentation edits or trivial implementation details.
- Distinguish deterministic mock checks, real HTTP integration with the voice adapter, and actual LiveKit speech tests. Report exactly what ran; live speech remains unverified unless observed.

## Scope and implementation order

1. Inspect existing work; settle schemas/examples and prove one turn over real HTTP.
2. Connect persistent actions, pending follow-ups, retries, and restart recovery.
3. Connect the simulated alert, delivery tracking, and “Why?” follow-up.
4. Finish reproducible commands and teammate handoff before adding features.

- No MQTT, Redis, Kafka, Celery, vector database, Kubernetes, full LMS, custom WebRTC signaling, wearable ML, or Android UI in this foundation.
- Noise handling, disconnected operation, proximity sensing, heart rate/temperature features, and predictive models are future ideas. Do not claim them implemented, infer diagnoses, or fabricate machine thresholds. This prototype does not control equipment.
- A rule-triggered warning is proactive behavior; it is not evidence of accident prediction. An offline client cannot receive cloud-generated speech until connectivity returns.

## Shared work tracking — keep current

- This committed file is team context; it is not automatic cross-machine synchronization or a lock. Colleagues see changes after normal Git sharing. Do not rely on private Claude auto-memory for team decisions.
- Update the active-work table at task start, after a substantial milestone, and before handoff. Update verification only from observed evidence. Preserve others' rows and unrelated edits.
- Use separate branches/worktrees where practical. Record overlapping files or shared-contract dependencies before changing them; continue unblocked work. Do not treat an old status row as a permanent lock.
- Keep statuses precise: `UNVERIFIED`, `TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`. `DONE` requires implementation plus relevant passing verification; missing credentials make the live check unverified, not passed.
- Record durable decisions here and detailed procedures in the README. Do not automatically commit, push, deploy, or send messages solely because this file requests a handoff update; follow the current task's authorization.
- Keep this file near 200 lines or fewer. Retain current status and recent handoffs; move older completed records to `docs/work-log.md` if needed, and read relevant entries when resuming.

### Current snapshot

| Area | Status | Evidence / next action |
| --- | --- | --- |
| Architecture | DESIGN AGREED | Independent HTTP services; FastAPI + LangGraph backend; LiveKit voice client. |
| Existing code and dependencies | UNVERIFIED | Inspect this checkout; reconcile rather than recreate working code. |
| Canonical contract and consumer compatibility | UNVERIFIED | Check root contract, real schemas, fixtures, and voice-client expectations. |
| Graph, persistence, actions, and proactive flow | UNVERIFIED | Verify each behavior against code and relevant checks. |
| Commands, real HTTP integration, and live audio | UNVERIFIED | Record evidence separately for each level. |

### Active work

| Task | Owner / branch | Status | Files / contract impact | Next checkpoint |
| --- | --- | --- | --- | --- |
| Repository reconciliation | Unassigned | TODO | Inspect backend and shared contract; no implementation claims yet. | Record actual state and choose the next incomplete flow. |

### Latest handoff

- **Recorded:** 2026-09-23; context-file preparation only. No repository implementation was inspected or tested during preparation.
- **Next action:** read the checkout and canonical contract, replace unknowns with evidence, and claim a bounded task above.
- **Blocking information:** live credentials and exact problem-statement requirements are not established in this document; inspect available configuration/materials without printing secrets.
- **For every subsequent handoff record:** UTC time; owner/branch and base commit if known; task; changed paths; behavior now working; exact checks and results; contract changes; remaining risks/blockers; next actionable step. Never invent an identity, commit, command result, or completion claim.
