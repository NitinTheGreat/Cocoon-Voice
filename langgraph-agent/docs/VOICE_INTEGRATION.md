# Voice ↔ backend integration handoff

For the `livekit-voice` owner. Status as of 2026-09-24, backend branch `backend`. The canonical rules are in [`API_CONTRACT.md`](../../API_CONTRACT.md); this page is the short path to connect the worker to the backend.

## What is ready

The backend serves the **seven JSON routes the worker's existing client already calls** (`cocoon_voice/backend_client.py`). All seven are checked by the backend test suite and were run over real HTTP against the live model on 2026-09-24.

| Worker call (`backend_client.py`) | Backend route | Status |
|---|---|---|
| `ensure_session` | `POST /v1/sessions` | ready (catalog IDs required, see below) |
| `session_exists` | `GET /v1/sessions/{session_id}/state` | ready |
| `submit_turn` (+ 202 polling, retries) | `POST /v1/sessions/{session_id}/turns` | ready: 200 completed, 202 processing, 409 conflict, 503 retryable |
| `get_turn` | `GET /v1/sessions/{session_id}/turns/{turn_id}` | ready |
| `list_events` | `GET /v1/sessions/{session_id}/events?after=&limit=` | ready |
| `report_delivery` | `POST /v1/sessions/{session_id}/events/{event_id}/delivery` | ready |
| (simulator) | `POST /v1/sessions/{session_id}/telemetry` | ready (backend `scripts/simulate_telemetry.py`) |

**Not ready: streaming.** `POST .../turns/stream`, replay, cancel and turn-delivery exist only in `contracts/proposed/` (planned for I08). Today a turn returns the **complete** reply text in one JSON response. Speak `speech` from the 200 response. Because there are no partial deltas, audio starts only when the whole reply is ready.

## What the worker must change

These are all in `livekit-voice`. The backend team has not touched that folder.

1. **Wire the remote brain.** `VOICE_BRAIN=remote_langgraph` is still marked future-only: `config.py` reports it as an issue and `providers.py` raises `ConfigError`. Connect `remote_bridge.py` / `TurnBridge` to the agent's `llm_node` so one finalized, wake-approved utterance becomes one `POST .../turns` with a stable `turn_id` (the existing `lk-<chat item id>` scheme is fine), and the `speech` of the result goes to TTS. Keep preemptive generation **off**, because a turn can save records.
2. **Send catalog IDs.** New sessions reject free-text IDs (since I02a):
   - `COCOON_DEFAULT_MACHINE_ID=cat-320-demo` (the current default) → **422 `unknown_machine`**. Set `COCOON_DEFAULT_MACHINE_ID=EXC_DEMO_001`, or one of `DOZ_DEMO_001`, `LDR_DEMO_001`, `TRK_DEMO_001`, `BHL_DEMO_001`.
   - `operator_id` currently falls back to the participant identity → **422 `unknown_operator`**. Provide a catalog operator (`OP_DEMO_1_1` … `OP_DEMO_5_3`) through job metadata or the participant attribute `operator_id`, which `remote_bridge.py` already reads first. A worker-side default operator setting would also work.
   - Sessions created before this change keep working when retried with their original key and IDs.
3. **Handle the new status codes.** Retry only where it says so, always with the **same** `turn_id`:

| Code | Meaning | Worker action |
|---|---|---|
| 422 `unknown_machine` / `unknown_operator` | Configuration error | Do not retry; log it and say the assistant is unavailable |
| 503 `catalog_unavailable` | Backend has no verified catalog | Do not retry until the backend is fixed; existing sessions still work |
| 503 `llm_unavailable` (retryable) | Model/provider failure or quota; **nothing was fabricated** | Retry with the same `turn_id`, then say "I can't confirm that yet" rather than claiming success or failure |
| 503 `auth_unavailable` (retryable) | Token store unreadable | Retry with backoff |
| 401 / 403 | Wrong service token / not allowed | Configuration error |

4. **Announcements.** Poll `GET .../events?after=<cursor>` as the worker already does. Speak each `speech` once, then report playback with `POST .../events/{event_id}/delivery` (`played`, `interrupted`, `failed`, `expired`). `played` is not an acknowledgement. Events stay retained; polling never consumes them.

Nothing else is required. Unknown response fields are additive, and the worker's models ignore them.

## Running the backend for integration

From `langgraph-agent/` (Python 3.11, see its README for install):

```powershell
Copy-Item .env.example .env        # then edit .env:
#   COCOON_SERVICE_TOKEN=<long random value>   <- the SAME value goes in livekit-voice/.env
#   COCOON_LLM_MODE=mock                        <- start here; switch to live when quota allows
python -m cocoon_agent             # http://127.0.0.1:8000 ; check /readyz shows "ready" and "catalog": true
python scripts/smoke.py            # end-to-end check of every route used by the worker
```

The backend needs the local dataset folder `../Cocoon_Dataset_v1` (the machine/operator catalog). Without it, `/readyz` is not ready and new sessions get 503.

Worker side (`livekit-voice/.env`): `COCOON_BACKEND_URL=http://127.0.0.1:8000`, the same `COCOON_SERVICE_TOKEN`, `VOICE_BRAIN=remote_langgraph` once wired, and `COCOON_DEFAULT_MACHINE_ID=EXC_DEMO_001`.

A minimal valid session request:

```json
{"client_session_key": "lk:cocoon-demo-room:op-demo-1-1", "room_name": "cocoon-demo-room",
 "participant_identity": "op-demo-1-1", "operator_id": "OP_DEMO_1_1", "machine_id": "EXC_DEMO_001"}
```

## Live model (Gemini on Vertex AI)

To run the backend with `COCOON_LLM_MODE=live`, set these in `langgraph-agent/.env`:
- `COCOON_LLM_PROVIDER=vertex`
- `GOOGLE_CLOUD_PROJECT=orbit-507316`
- `GOOGLE_CLOUD_LOCATION=global`
- `VERTEX_MODEL=gemini-3.8-flash`
- Application Default Credentials: either gcloud's default ADC or `GOOGLE_APPLICATION_CREDENTIALS=<path to the ADC file>`.

The backend only hands that path to the Google SDK; it never reads or copies the file.

Observed on 2026-09-24:
- A live turn works end to end ("why did you warn me" → correct explanation of the stored alert in 4.2 s).
- **Quota is the blocker.** Project `orbit-507316` returned 429 for `gemini-3.8-flash` after about 2 calls in quick succession. Each turn makes 2 model calls, so most live turns ended in retryable `503 llm_unavailable`.
- Single calls took about 2–15 s.
- A demo on this model needs a Vertex quota increase, or a different `VERTEX_MODEL`.

Integrate in **mock mode first**. It exercises the same routes, graph and database, with no quota.

## Integration acceptance check

1. The worker starts with `VOICE_BRAIN=remote_langgraph`, and the backend `/readyz` is ready.
2. Say "what's my next task" → the reply is the seeded task (mock) or its live wording.
3. Say "report an incident", then describe it → you hear confirmation with an incident number, and `GET .../state` shows exactly one incident. Retrying the same `turn_id` does not create a second one.
4. Run `python scripts/simulate_telemetry.py --room <room> --identity <participant> --machine EXC_DEMO_001 --operator OP_DEMO_1_1` from `langgraph-agent/`. You hear the unsolicited seatbelt warning once, and one delivery report is recorded.
5. Say "why?" → the explanation refers to that alert.
6. Stop the backend mid-conversation: the worker says it cannot confirm instead of inventing an answer, and it recovers when the backend returns.

Streaming (early audio), barge-in cancellation and reply playback reporting come with backend I08. They are specified in `contracts/proposed/README.md` → "Voice adapter checklist".
