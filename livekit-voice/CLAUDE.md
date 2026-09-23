# Cocoon — LiveKit voice

## Purpose and scope

- Cocoon is Team Butterfly's proactive operator assistant for a Caterpillar hackathon. The implementation budget is roughly 12 hours: deliver a small working integration first.
- This folder owns LiveKit audio, speech recognition/synthesis, the HTTP backend client, and announcement playback. `../langgraph-agent` owns reasoning, tools, state, and rules. `Cocoon-App` is a separate Android repository for later integration.
- Initial testing uses LiveKit Playground or its current Agent Console equivalent. Phase-2 demo capabilities (backend connected): ask for tasks, report an incident, request a lesson, hear an unsolicited simulated warning, and ask why it happened.

## Phased decision (recorded 2026-09-23; supersedes conflicting notes below)

- **Phase 1 — current: standalone voice.** The worker is a self-contained voice assistant ("Cat") tested in Playground/Agent Console: LiveKit WebRTC → AssemblyAI streaming STT (direct plugin + key) → Gemini text LLM on Vertex AI via ADC (streaming) → Cartesia streaming TTS (direct plugin + key). Not LiveKit Inference.
- This is a **temporary standalone brain**, not a second LLM beside LangGraph. `cocoon_voice/providers.py:create_brain()` is the single replacement point; the later remote LangGraph adapter must be a streaming `llm.LLM` returned there. The LiveKit chat context is the only conversation history.
- Phase 1 includes noise handling (Krisp in the worker), streaming responses, wake gating ("Hey Cat"), barge-in, thinking cue and latency measurement.
- Phase 1 must not claim tasks, incidents, wellbeing, hazard detection, machine data or control. No Android UI, simulator, business tools, dashboard or LangGraph wiring in phase 1.
- **Phase 2 — pending, not started:** connect `langgraph-agent`. Only after the user evaluates and accepts the phase-1 voice experience. Phase-0 HTTP bridge code (`bridge.py`, `backend_client.py`, `announcements.py`, `mock_backend.py`, `remote_bridge.py`) is retained and tested but not wired into the worker. Preemptive generation must be disabled for the action-capable backend.
- Vertex auth: ADC only (`GOOGLE_GENAI_USE_VERTEXAI=true`, project `orbit-507316`, location `global`). Never read/copy/print the ADC file, never set `GOOGLE_APPLICATION_CREDENTIALS` to it, never add `GOOGLE_API_KEY`/`GEMINI_API_KEY`.
- Git on branch `voice`: author/committer `NitinTheGreat <nitinpandey1304@gmail.com>` (local config), no AI attribution trailers (`.claude/settings.local.json` sets `attribution` to empty).
- The original problem statement and datasets were not available when this file was prepared. Do not invent mandatory requirements or claim full compliance. Reconcile supplied materials when available.
- These instructions capture the agreed design, not evidence that code exists. Initial implementation and test status are **UNVERIFIED**. Inspect the checkout before changing that status.

## At the start of every task

1. Read applicable repository instructions, this file's handoff section, this folder's README, and root `API_CONTRACT.md` / `contracts/openapi.yaml` if present. Read relevant contract examples and recent handoff records.
2. Inspect `git status`, the current branch, recent changes, dependency files, and relevant code. Preserve colleagues' changes; do not reset, clean, or overwrite unrelated work.
3. Distinguish requested behavior, existing implementation, and verified behavior. If code conflicts with the contract, record the mismatch and fix the relevant side; do not quietly redefine the contract.
4. Select one useful task in the user's scope. Add/update your active-work row with owner, branch, files, and next checkpoint. If ownership is unknown, use the branch/session identifier, not an invented teammate name.
5. Implement and verify the smallest complete flow. Missing LiveKit/provider credentials should not prevent the HTTP adapter, mock server, and contract work.

## Ownership and architecture

- Voice owner maintains this folder, its HTTP client, independent mock backend, and Playground walkthrough. Backend owner maintains the real server and shared API schemas. Record changes affecting both developers.
- Services communicate through HTTP JSON only. Never import sibling Python modules, share a virtual environment, or access the backend database. Keep business decisions and application memory in the backend.
- Run the official long-lived **LiveKit Agents worker**, connected to LiveKit Cloud. It is not an ordinary FastAPI handler; do not create a worker per request or access its session objects from an unrelated process.
- The backend is a separate FastAPI/Uvicorn server on port **8000** by default. Configure the worker's supported health endpoint on a nonconflicting port and document the selected SDK's actual behavior.
- Audio uses **WebRTC through LiveKit**. Voice-to-backend calls use **HTTP JSON**. HTTP is not a replacement transport for WebRTC audio.
- Phase-2 pipeline (pending): STT produces a completed utterance; an HTTP call runs LangGraph; returned `speech` goes to TTS. Phase 1 temporarily uses the standalone Vertex brain above. Never a realtime speech-to-speech API, and never two competing brains/histories.
- Use Python 3.11 unless existing compatible configuration warrants otherwise; a valid package name such as `cocoon_voice`; local `requirements.txt`, `requirements-dev.txt`, `.env.example`, README, and tests.
- Pin dependencies after resolving compatible versions. Keep environment loading independent of the caller's working directory. Gitignore secrets, environments, local delivery state, and generated artifacts.

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

- Validate contract responses with typed models. Handle the common error envelope: `code`, `message`, `retryable`, `request_id`. Never turn an arbitrary error body into speech.
- Completed turns return **200**, including saved results for duplicate requests. A duplicate still processing returns **202** with polling instructions. Reusing an ID for a different payload returns **409**. Handle 401/404/422/5xx explicitly.
- Send the service bearer token from environment configuration on application requests. Provider keys and service tokens stay server-side; do not put them in browser metadata, fixtures, logs, or speech.
- Keep the client and mock aligned with committed schemas/examples. Record proposed breaking changes; update the canonical contract and affected implementation together only within the current task's scope.
- Cursor persistence, delivery retries, session identity conflicts, and expiry must be explicit in the canonical contract. Coordinate unresolved details instead of guessing incompatible defaults.

## The remote LangGraph bridge

- Check the installed LiveKit SDK and current official docs before implementing lifecycle hooks or copying examples. Use an overridden `llm_node`, or the documented equivalent text-generation interface, to call the remote backend.
- LiveKit's adapter for an in-process compiled LangGraph is not an HTTP URL adapter. Do not move the backend graph into this worker to make that adapter fit.
- STT/TTS are the direct AssemblyAI and Cartesia plugins (phase-1 decision; LiveKit Inference is not used). Make valid model/voice identifiers configurable; verify them for the project and SDK version. Avoid a general provider framework.
- On job start, bind one operator participant to the room. Establish a stable `client_session_key` from the room/session namespace and participant; create/retrieve the backend session once and retain its server-issued `session_id`.
- Reuse an application session ID from trusted dispatch metadata when the contract supports it. Keep room, participant, operator, machine, and backend session mappings consistent. Untrusted participant metadata must not switch another operator's session.
- Support **one operator per room** initially. Define when a new shift/room starts a new application session so reused room names do not accidentally reuse old work context.
- Submit a confirmed final user utterance through **one path**. Do not submit from both a transcription event and `llm_node`. Partial transcripts and empty input must not execute tools.
- Assign a stable `turn_id` once per logical utterance and reuse it across retries/polling. Pass only the new text; the backend owns persisted conversation history.
- Disable preemptive/speculative generation for this action-capable bridge using supported SDK configuration. Test that internal retries or interruptions do not submit the same logical turn under new IDs.
- Reuse an asynchronous `httpx` client. Bound timeouts, retry count, and total polling time; honor documented retry guidance. Do not retry authentication/validation failures indefinitely.
- On **202**, poll the turn endpoint. On a timeout after submission, query the same `turn_id`; the action may already be committed. Never announce failure, cancellation, or success when the outcome is unknown.
- Barge-in can interrupt speech, but does not roll back a backend mutation. Do not let cancellation of a TTS task silently erase an unresolved turn from tracking.
- Yield only the final operator-facing `speech` to TTS. Do not speak classifier output, internal tool arguments, exception traces, credentials, or full JSON responses.
- Use a fixed greeting through `session.say()` instead of triggering generation on an old user turn. Log correlation IDs and request timings, not raw audio or secrets.

## Proactive announcements

- Start one cancellable event-polling task for each active voice session, initially about once per second and configurable. Use bounded backoff on outages; stop it and close clients when the session ends.
- The backend owns telemetry, alert rules, episode deduplication, and alert explanations. This worker only retrieves events, schedules speech, and reports delivery outcomes.
- Announce supplied event `speech` through `session.say()` without a new user utterance or LLM call. Use the supported SDK speech handle/events to observe actual playback completion.
- Serialize announcements with normal speech. Queue by documented priority/expiry rules; do not overlap audio or silently discard an event because someone is speaking. Handle interruption explicitly.
- Keep stable `consumer_id`, cursor, and pending-delivery progress according to the contract. Do not advance beyond unresolved events in a way that loses them, or repeatedly replay completed events after ordinary reconnects.
- Report `played` only after playback completes. Report interrupted/failed/expired accurately and apply a bounded retry policy. A submitted TTS request alone is not proof the operator heard it.
- Retried delivery reports must be safe. Document the possible replay window between audio completion and persisted acknowledgement; exactly-once audio across a crash is not promised.
- Playback is separate from human acknowledgement and hazard resolution. Never label an alert resolved just because it was spoken.
- After a warning, “Why?” follows the ordinary turn API and uses backend alert context. Do not invent a local explanation disconnected from the actual rule/event.
- Label the demonstration as simulated and unvalidated for machinery. A disconnected worker cannot deliver cloud/backend announcements; do not present retries as full offline operation.

## Independent development and Playground checks

- Provide a small contract-compatible mock FastAPI server **inside this folder**, on a configurable port distinct from 8000. It must support the session, turn-status, event, and delivery calls the worker actually uses, without importing the real backend.
- Make mock mode unmistakable. Keep client behavior the same against mock and real servers. Use contract fixtures to prevent the mock from masking an incompatible real API.
- A mock backend permits work without the backend teammate. Credential-free HTTP tests can also use a stub speech sink; those tests do not establish that LiveKit STT/TTS works.
- Use configurable backend URL and service token. `http://127.0.0.1:8000` works when worker and backend share a machine. A cloud worker's localhost does not reach a developer's laptop.
- Register the worker with an explicit configurable dispatch name, default **`cocoon-voice`**. Browser client and worker must connect to the same LiveKit Cloud project; dispatch the exact registered name.
- Document three separate milestones: worker registered; agent dispatched into a room; actual speech transcribed and answered. A registered but undispatched worker has not passed a voice test.
- Verify the current Playground/Agent Console workflow against official docs. Include model downloads and explicit-dispatch setup where required by the chosen SDK.
- README must give exact installation, startup, shutdown, mock-server, test, and live-demo commands, plus PowerShell/Bash differences and working directories.
- Document actual configuration in `.env.example`, including `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, chosen STT/TTS settings, backend URL/token, dispatch name, and polling settings. Never store values here.
- **Commands currently verified:** see `docs/work-log.md` (each entry lists commands, environment and results). Do not invent runnable module paths.
- Android is out of scope now. Document future server-side user authentication/token issuance and room joining; never suggest embedding LiveKit API secrets or the service token in the Android app.

## Verification and scope

- Verify adapter serialization against contract fixtures; real HTTP requests and speech output; a single submission per logical turn; duplicate/retry behavior; **202** polling; timeout with unknown outcome; and clean task shutdown.
- Verify event ordering, expiry, interruption, delivery reports, ordinary reconnect behavior, and no overlapping speech. Then demonstrate an alert arriving while the operator is silent and a grounded “Why?” follow-up.
- End-to-end proof must reach the **real backend**, save an action, and read its saved state. Separately verify live microphone/STT/TTS in Playground. Do not label mock audio or a console response as a successful live voice test.
- Record exact checks and results. If credentials are missing, list their variable names and remaining live steps; continue all independently verifiable work.
- Add/run focused checks for consequential behavior, not tests that mirror trivial code or documentation. Broaden testing only for a concrete remaining risk or required gate.
- Priority: one real HTTP voice turn; persistent action integration; proactive announcement plus follow-up; reproducible teammate handoff.
- No MQTT, Redis, Kafka, Celery, vector database, Kubernetes, custom WebRTC signaling, full LMS, wearable ML, or Android UI in this foundation. Phase 1 streams LLM text into TTS; the phase-2 backend contract may start nonstreaming.
- Noise handling (Krisp) is in phase-1 scope. Disconnected operation, proximity sensing, heart rate/temperature features, and predictive models remain future ideas, not delivered capabilities. This prototype does not control equipment.

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
| Phase decision | DONE | Standalone voice (phase 1) recorded above; phase 2 (LangGraph) PENDING until the user accepts the voice experience. |
| Settings, providers, doctor | DONE (offline + Vertex/LiveKit live) | `pytest`; `python -m cocoon_voice.doctor`: Vertex ADC PASS, LiveKit PASS, Krisp constructs. |
| Worker registration | DONE | `python -m cocoon_voice.agent start` registered `cocoon-voice` (2026-09-23). |
| Wake gate, turn policy, streaming guard, Porcupine plumbing, metrics | DONE (offline) | 127 tests incl. real `AgentSession` runs; see `docs/work-log.md`. |
| Latency | PARTIAL | Vertex measured (warm TTFT p50 668 ms, n=9); STT/TTS/playout unmeasured. `docs/voice-latency-report.md`. |
| Live dispatch + speech in Playground | BLOCKED | AssemblyAI verified live; Cartesia returns 401 "Invalid API key" for the configured key. |
| Acoustic wake | IN_PROGRESS | Decision: livekit-wakeword (Porcupine needs a company email). Implemented + verified with real `hey_livekit` model; `Hey Cat` model must be trained (`wakeword/README.md`). |
| Krisp effect | BLOCKED | Needs a live LiveKit Cloud session. |

### Active work

| Task | Owner / branch | Status | Files / contract impact | Next checkpoint |
| --- | --- | --- | --- | --- |
| Standalone voice pipeline (phase 1) | NitinTheGreat / `voice` | IN_PROGRESS (awaiting keys) | `livekit-voice/` only; no shared-contract change. | Add provider keys → doctor → smoke → Playground checklist in README → record results. |

### Latest handoff

- See `docs/work-log.md` for dated milestone entries (commands, results, limits, next step). Keep the newest entry current before each handoff.
- **For every subsequent handoff record:** UTC time; owner/branch and base commit if known; task; changed paths; behavior now working; exact checks and results; contract changes; remaining risks/blockers; next actionable step. Never invent an identity, commit, command result, or completion claim.
