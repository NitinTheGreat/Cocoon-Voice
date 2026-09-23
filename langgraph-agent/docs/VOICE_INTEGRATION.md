# Voice ↔ backend integration handoff (Batch A)

For the `livekit-voice` owner. Updated 2026-09-24, backend branch `backend`. Canonical rules: [`API_CONTRACT.md`](../../API_CONTRACT.md). Every JSON body below was captured from a real backend run (mock mode), with only IDs shortened.

## Status

| Layer | Status |
|---|---|
| Backend HTTP (7 JSON routes) | **Working.** Covered by backend tests and a real-HTTP smoke run in mock mode on 2026-09-24. |
| Live model (Gemini `gemini-3.8-flash`, Vertex, `orbit-507316`, `global`) | **Working but capacity-limited.** One model call per turn. In a 5-turn run, 2 turns succeeded in about 5 s and 3 failed with a retryable 503 (details below). |
| Worker wiring (`VOICE_BRAIN=remote_langgraph`) | **Voice owner's task.** Not done: `providers.py` still raises "future-only". |
| Microphone → backend → speaker | **Not yet observed.** Backend HTTP evidence is not audio evidence. |
| Streaming, cancel, per-turn playback reports | **Not implemented.** `POST .../turns/stream`, `GET .../turns/{id}/stream`, `POST .../turns/{id}/cancel`, `POST .../turns/{id}/delivery` and `GET .../events/stream` exist only in `contracts/proposed/`. The backend answers them with 404 or 405. Do not call them. |

## The calls the worker makes

`cocoon_voice/backend_client.py` already calls exactly these routes. Every call sends `Authorization: Bearer <COCOON_SERVICE_TOKEN>` (the same value in both `.env` files; server-side only) and may send `X-Request-ID`, which is echoed back.

### 1. Session: `POST /v1/sessions`

```json
{"client_session_key": "lk:cocoon-demo-room:op-demo-1-1", "room_name": "cocoon-demo-room",
 "participant_identity": "op-demo-1-1", "operator_id": "OP_DEMO_1_1", "machine_id": "EXC_DEMO_001"}
```

201 on create, 200 with the same body on retry:

```json
{"session_id": "ses_5f7c…2588", "client_session_key": "lk:cocoon-demo-room:op-demo-1-1",
 "room_name": "cocoon-demo-room", "participant_identity": "op-demo-1-1", "operator_id": "OP_DEMO_1_1",
 "machine_id": "EXC_DEMO_001", "state_version": 0, "created_at": "2026-09-23T21:55:57.538949Z",
 "dataset_manifest_sha256": "5d7de31c…40e42d", "binding_status": "catalog_verified",
 "site_id": null, "shift_id": null, "context_status": "unavailable", "context_source": null}
```

- **IDs must come from the catalog.** Machines: `EXC_DEMO_001` (Cat 320), `DOZ_DEMO_001` (Cat D6), `LDR_DEMO_001` (Cat 950 GC), `TRK_DEMO_001` (Cat 793), `BHL_DEMO_001` (Cat 420). Operators: `OP_DEMO_1_1` … `OP_DEMO_5_3`. There are no aliases.
- The worker's current defaults (`COCOON_DEFAULT_MACHINE_ID=cat-320-demo`, and the participant identity as operator) produce this, and no session is created:

  ```json
  {"error": {"code": "unknown_machine", "message": "session association is not in the verified catalog",
   "retryable": false, "request_id": "req_…",
   "details": [{"field": "body.machine_id", "issue": "not in the machine catalog"},
               {"field": "body.operator_id", "issue": "not in the operator catalog"}]}}
  ```

  Fix: set `COCOON_DEFAULT_MACHINE_ID=EXC_DEMO_001`, and put a catalog operator in job metadata or the participant attribute `operator_id` (`remote_bridge.py` reads these before falling back to the identity).
- The same key with a different association returns 409 `session_conflict` ("client_session_key already bound to a different machine_id"), and the stored session is unchanged.

### 2. Turn: `POST /v1/sessions/{session_id}/turns`

Send one finalized, wake-approved utterance. Assign `turn_id` once (for example `lk-<chat item id>`) and reuse it on every retry.

```json
{"turn_id": "lk-item_demo0001", "text": "Log an incident: the hydraulic hose on the boom is leaking",
 "source": "voice"}
```

200, completed. Speak **`speech`**; `actions` are the saved outcomes:

```json
{"session_id": "ses_5f7c…2588", "turn_id": "lk-item_demo0001", "status": "completed",
 "speech": "I've logged incident number 2: the hydraulic hose on the boom is leaking.",
 "actions": [{"type": "incident_logged", "created": true,
              "incident": {"incident_id": "INC-0002", "incident_number": 2, "session_id": "ses_5f7c…2588",
                           "operator_id": "OP_DEMO_1_1", "machine_id": "EXC_DEMO_001",
                           "description": "the hydraulic hose on the boom is leaking",
                           "source_turn_id": "lk-item_demo0001", "created_at": "2026-09-23T21:55:57.785543Z"}}],
 "state_version": 1, "llm_mode": "mock", "error": null, "retry_after_ms": null, "poll_url": null,
 "created_at": "…", "completed_at": "…"}
```

- **Same `turn_id` and same text → the identical stored result.** No new incident and no model call. This was checked in the run.
- **Same `turn_id` with different text → 409 `idempotency_conflict`.** That is a client bug; do not retry.
- **202 processing:** the same `turn_id` is still running (for example the first request timed out on the worker side). The body carries `"status": "processing"`, `"retry_after_ms": 500` and a `poll_url`; the headers carry `Retry-After` and `Location`. Poll `GET` on the turn, and do not resubmit with a new `turn_id`.
- The `actions[].type` values the worker may see: `next_task`, `incident_logged`, `information_requested`, `training_assigned`, `training_status`, `alert_explained`, `pending_cancelled`. The worker only needs `speech`.

### 3. Status after uncertainty: `GET /v1/sessions/{session_id}/turns/{turn_id}`

This returns `processing`, `completed` (the same body as above) or `failed` (with `error`). **After a timeout or disconnect, read the status before deciding anything.** If it is completed, speak the saved `speech`. If it is processing, poll. If it is failed with `retryable: true`, resubmit with the **same** `turn_id`. Never turn an uncertain incident report into a new request with a new `turn_id`, because that can create a second incident.

### 4. Errors

| HTTP / `code` | Meaning | Worker action |
|---|---|---|
| 401 `unauthorized` | Wrong or missing service token | Configuration error, no retry |
| 404 `not_found` | Unknown session or turn | Re-create or retrieve the session by key |
| 409 `session_conflict` / `idempotency_conflict` | Key or `turn_id` reused with different data | Client bug, no retry |
| 422 `unknown_machine` / `unknown_operator` / `validation_error` | Bad IDs or body | Configuration error, no retry |
| 503 `llm_unavailable`, `retryable: true` | The model could not answer: capacity (429), a deadline or a provider error. Usually raised while routing, before any write; but if the error `details` list `action_records` (also on `GET .../turns/{id}`), those writes were committed and are kept. | Retry the same `turn_id` a limited number of times (the worker's `COCOON_BACKEND_MAX_ATTEMPTS`), then say "I can't confirm that right now". Never claim success or failure. |
| 503 `catalog_unavailable` | The backend has no verified catalog (new sessions only) | Stop; the backend must be fixed |
| 503 `auth_unavailable` (retryable) / 500 `internal_error` (retryable) | Transient backend fault | Retry with backoff, same IDs |

With the default `COCOON_LLM_COMPOSE=template`, the reply is worded from the saved action, so a turn that saved an incident cannot then fail while wording the answer. Do **not** read a 503 as "nothing was saved": a turn deadline or a model-worded reply (`COCOON_LLM_COMPOSE=model`) can fail after writes committed. Every failed turn lists its committed writes in `action_records` (and in the error `details`), and retrying the same `turn_id` reuses them instead of repeating them (C1).

### 5. Announcements: `GET /v1/sessions/{session_id}/events?after={cursor}&limit=20`

Poll on a bounded interval while the operator is silent, and keep the cursor per session. Events are retained; reading does not consume them.

```json
{"session_id": "ses_5f7c…2588", "next_cursor": 1, "has_more": false,
 "events": [{"event_id": "ann_ALR-71c3…_start", "sequence": 1, "type": "alert_started", "priority": "high",
             "speech": "Warning: the engine is running and your seatbelt is not fastened. Please fasten your seatbelt.",
             "alert_id": "ALR-71c3…", "created_at": "2026-09-23T21:55:58.026522Z",
             "expires_at": "2026-09-23T21:57:58.026522Z", "deliveries": []}]}
```

- Speak each event once, in `sequence` order. Skip expired ones and deduplicate by `event_id`. `priority` is `high` for `alert_started` and `low` for `alert_cleared`.
- Pass `next_cursor` as `after` on the next poll.

### 6. Announcement playback: `POST /v1/sessions/{session_id}/events/{event_id}/delivery`

```json
{"consumer_id": "voice-cocoon-demo-room-op-demo-1-1", "status": "played"}
```

The response is `{"event_id": "ann_ALR-…_start", "consumer_id": "voice-…", "status": "played", "detail": null, "recorded_at": "…"}`.

- `status` is one of `played`, `interrupted`, `failed` or `expired`. Report what actually happened, not HTTP receipt.
- There is one record per consumer, and the last report wins.
- `played` is not an operator acknowledgement.

### 7. "Why?"

After an announcement, send an ordinary turn such as "why did you warn me". The reply explains the stored alert, for example: "I warned you because at 21:55:57 UTC the simulated telemetry showed the engine running while the seatbelt was unfastened. …"

## Run the backend

From `langgraph-agent/` (Python 3.11 venv installed as in its README). `.env` must contain `COCOON_SERVICE_TOKEN` (the same value as the worker's). The catalog folder `../Cocoon_Dataset_v1` must exist.

```powershell
# mock mode (no model calls): use this to prove the wiring first
$env:COCOON_LLM_MODE = "mock"; python -m cocoon_agent

# live mode (Gemini on Vertex); set these in .env or the environment:
#   COCOON_LLM_MODE=live
#   GOOGLE_CLOUD_PROJECT=orbit-507316
#   GOOGLE_CLOUD_LOCATION=global
#   VERTEX_MODEL=gemini-3.8-flash
#   GOOGLE_APPLICATION_CREDENTIALS=<ADC file path>   (or gcloud's default ADC)
python -m cocoon_agent
```

`GET http://127.0.0.1:8000/readyz` must show `"status": "ready"`, `"catalog": true` and the expected `llm_mode`. `GOOGLE_GENAI_USE_VERTEXAI` is not needed by the backend: it always calls Vertex explicitly.

To inject a silent warning for a running voice session, run from `langgraph-agent/`:

```powershell
python scripts\simulate_telemetry.py --room <room> --identity <participant> --machine EXC_DEMO_001 --operator OP_DEMO_1_1 --scenario seatbelt-start
```

The room, identity, machine and operator must match what the worker sent, because the simulator resolves the session by `lk:<room>:<identity>`.

## Latency and capacity (live mode)

- **Per turn:** exactly one Gemini call (routing). The reply is worded deterministically from the saved result. Successful live turns took about 5 s end to end. Replies arrive whole, with no streaming, so audio starts after that.
- **Backend bounds:**
  - one model call in flight per backend process, and up to 8 waiting;
  - up to 3 attempts per call with 1–4 s jittered backoff, for 429, 5xx and timeouts only;
  - a 20 s deadline per call and a 45 s deadline per turn.
- **Worker request timeout.** The worker's default `COCOON_BACKEND_REQUEST_TIMEOUT` is 10 s, so a slow live turn can time out on the worker side while the backend is still running it. That is fine: the next attempt with the same `turn_id` gets 202 and then the saved result. Either raise the worker timeout (about 30 s), or keep the status-poll path.
- **Observed failures (2026-09-24, sanitized):**
  - `429 RESOURCE_EXHAUSTED` "Resource exhausted. Please try again later", with no quota identifier and no retry hint in the payload;
  - once, `499 CANCELLED` when a call reached its 20 s deadline.
  - Success varied with time on the same project and model: 7 of 7 isolated calls succeeded in one window, and 3 of 5 turns failed in another.

  This behaves like shared on-demand capacity pressure, not a fixed per-project count. The backend does not switch models or fall back to mock. If live turns keep failing, that is capacity, not wiring; mock mode proves the wiring.

## Voice owner's remaining work

1. Wire `VOICE_BRAIN=remote_langgraph` into the worker's `llm_node` using `remote_bridge.py` / `TurnBridge` and `backend_client.py`. Speak only the backend's `speech`; do not pass it through the standalone model. Keep preemptive generation off.
2. Send catalog IDs (above) and one finalized utterance per stable `turn_id`. Recover through the turn status after any uncertainty.
3. Poll announcements independently while the operator is silent, speak them once, and report actual playback.
4. On barge-in, stop local audio and drop the obsolete reply by response epoch. The backend action may still complete, and **there is no cancel route**; do not imply a rollback. Keep a follow-up that changes state waiting until the earlier outcome is known.
5. Acceptance, first in mock mode, then a few sequential live turns (stop after collecting evidence if live keeps returning 503):
   1. "What is my next task?" is heard.
   2. An incident is reported; a repeated request still leaves exactly one incident in `GET .../state`.
   3. A silent warning is heard, then "Why?" explains it.
   4. An interrupted reply is not played late.

   Record mock or live, latency, and whether real audio was heard.
