# Cocoon backend API contract (v1)

The contract between the voice worker (`livekit-voice`) and the backend (`langgraph-agent`). It covers HTTP JSON over a versioned `/v1` path and nothing else: the two services share no code, no database and no virtual environment.

| Artifact | Role |
|---|---|
| `langgraph-agent/cocoon_agent/api/schemas.py` | **Source of truth.** The Pydantic models used by FastAPI. |
| `contracts/openapi.yaml` | Generated from the schemas. Committed so that Developer A (and later Cocoon-App) can read it without running the backend. |
| `contracts/examples/*.json` | Request and response fixtures produced by a real run, then reviewed. |
| `livekit-voice/cocoon_voice/contract.py` | The worker's own hand-written copy of the subset it uses. |

## Ownership

| Area | Owner |
|---|---|
| Endpoint behaviour, schemas and `contracts/openapi.yaml` | **Developer B** (`langgraph-agent`) |
| How the worker calls the API, including retries, polling and delivery reports | **Developer A** (`livekit-voice`) |
| A change that affects both | Needs agreement from both developers in the same PR |

## Drift checks (run both before merging)

```bash
# langgraph-agent/: the committed spec equals the live FastAPI schema, and every example validates
python scripts/export_openapi.py --check
pytest tests/test_contract.py

# livekit-voice/: the worker's requests and parsers, and the mock backend, match the committed spec
pytest tests/test_contract.py
```

## Making compatible changes

1. Edit `schemas.py` and keep the change **additive**:
   - Add a new endpoint.
   - Add an **optional** request field with a default.
   - Add a response field.
   - Add a new `actions[].type`, a new announcement `type` or a new error `code`.

   The worker ignores unknown response fields and treats `actions` and `type` values as opaque, so these changes do not break it.
2. Run `python scripts/export_openapi.py` and commit the regenerated `contracts/openapi.yaml`.
3. If a fixture changes, update `contracts/examples/` and register any new file in `tests/test_contract.py`.
4. Anything that is not additive needs a `/v2` path served alongside `/v1` until the worker migrates. That includes removing or renaming a field, making a field required, changing a type, narrowing an enum the worker sends, or changing status-code semantics.

## Common rules

- **Auth.** Every `/v1/*` call sends `Authorization: Bearer $COCOON_SERVICE_TOKEN`, a shared service secret configured in both `.env` files. `/healthz`, `/readyz` and `/docs` are public. The server binds to `127.0.0.1` by default.
- **Correlation.** Clients may send `X-Request-ID` (`[A-Za-z0-9._:-]{1,128}`). The server echoes it, or generates one, and includes it in every error. The worker uses `<turn_id>.<attempt>`.
- **Time.** All timestamps are ISO-8601 in UTC (`...Z`). Timezone-naive input is rejected.
- **Secrets.** Provider credentials (Anthropic, LiveKit) never appear in requests, responses or logs.
- **Error envelope.** Every non-2xx response has this shape:

```json
{"error": {"code": "idempotency_conflict", "message": "...", "retryable": false, "request_id": "req_...",
           "details": [{"field": "body.turn_id", "issue": "..."}]}}
```

| Status | `code` | Retry? |
|---|---|---|
| 401 | `unauthorized` | no |
| 404 | `not_found` (unknown session, turn or announcement) | no |
| 409 | `session_conflict`, `idempotency_conflict` | no (client bug) |
| 422 | `validation_error` (with `details`) | no |
| 500 | `internal_error` | yes, with the **same** IDs |
| 503 | `llm_unavailable`, `turn_failed` | yes, with the **same** IDs |

## Endpoints

| Method and path | Purpose | Success |
|---|---|---|
| `POST /v1/sessions` | Create the session idempotently, keyed by `client_session_key` | `201` created, `200` existing. `409` if the key is already bound to a different room, participant, operator or machine. |
| `POST /v1/sessions/{session_id}/turns` | Submit one completed utterance and run LangGraph | `200` completed. `202` means the same `turn_id` is still processing. `409` means the ID was reused with different text or source. |
| `GET /v1/sessions/{session_id}/turns/{turn_id}` | Read a turn's status and stored result | `200` with `status` `processing`, `completed` or `failed` |
| `GET /v1/sessions/{session_id}/state` | Read tasks, incidents, training assignments, lessons, active and latest alert, pending question and `state_version` | `200` |
| `POST /v1/sessions/{session_id}/telemetry` | Post one **simulated** sensor sample | `200`. A repeat of the same `event_id` returns `duplicate: true`. `409` if the payload differs. |
| `GET /v1/sessions/{session_id}/events?after={cursor}&limit=20` | Read retained announcements with `sequence > after`. Reading does not consume them. | `200` with `next_cursor` and `has_more` |
| `POST /v1/sessions/{session_id}/events/{event_id}/delivery` | Record a playback outcome, one record per `consumer_id` (last write wins) | `200` |

### Turns: idempotency and retries

- `turn_id` identifies one utterance. The worker uses `lk-<LiveKit chat item id>`, assigns it once, and reuses it for every retry.
- The first POST runs the graph and returns **200** with the result. The same `turn_id` with the same payload then gets one of these responses:
  - The stored result (**200**) if the turn completed. No actions are repeated.
  - **202** if the turn is still running. The response carries `retry_after_ms`, a `poll_url`, and the `Retry-After` and `Location` headers. The client then polls `GET .../turns/{turn_id}`.
  - A **re-run** if the turn previously `failed` or was left `processing` by a crashed process.
- Side effects are keyed in SQLite, not in memory:
  - Incidents are unique on `(session_id, source_turn_id)`.
  - Training assignments are unique on `(operator_id, lesson_id)`.
  - Telemetry is unique on `(session_id, event_id)`.
  - A partial unique index allows only one active alert episode per rule.

  A re-run returns the saved record with `created: false` instead of inserting another one.
- **This is not exactly-once.** Suppose the backend dies after saving an incident but before storing the turn result. The next retry re-runs the graph, finds the incident by `turn_id` and reports it. The graph message IDs derive from `turn_id`, so the utterance is not duplicated in memory either. But if the model routes the re-run differently, the operator may hear different wording. A turn that `failed` in the wording step after an action was saved has still saved that action. That is why the worker never tells the operator an action failed or was cancelled when the outcome is unknown. It says it cannot confirm yet.
- Updates for one session are processed in order: turns and telemetry share a per-session lock. This relies on running **one Uvicorn worker**.

### Example requests

Bash:

```bash
T="Authorization: Bearer dev-local-change-me"; B=http://127.0.0.1:8000
SID=$(curl -s -H "$T" -H 'Content-Type: application/json' -d @contracts/examples/session_create.request.json $B/v1/sessions | python -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
curl -s -H "$T" -H 'Content-Type: application/json' -d '{"turn_id":"demo-1","text":"What is my next task?","source":"text"}' $B/v1/sessions/$SID/turns
curl -s -H "$T" -H 'Content-Type: application/json' -d @contracts/examples/telemetry.request.json $B/v1/sessions/$SID/telemetry
curl -s -H "$T" "$B/v1/sessions/$SID/events?after=0"
```

PowerShell:

```powershell
$H = @{ Authorization = "Bearer dev-local-change-me" }; $B = "http://127.0.0.1:8000"
$s = Invoke-RestMethod -Method Post -Uri "$B/v1/sessions" -Headers $H -ContentType "application/json" -InFile contracts/examples/session_create.request.json
Invoke-RestMethod -Method Post -Uri "$B/v1/sessions/$($s.session_id)/turns" -Headers $H -ContentType "application/json" -Body '{"turn_id":"demo-1","text":"What is my next task?","source":"text"}'
Invoke-RestMethod -Uri "$B/v1/sessions/$($s.session_id)/events?after=0" -Headers $H
```

Example bodies are in `contracts/examples/`, including a completed turn, a turn in `processing`, a turn that asks for information, an alert explanation, state, telemetry, events, delivery, and the conflict, validation and auth errors.

### Telemetry and announcements

- Telemetry must set `simulated: true`, and `readings` must include `engine_on`, `seatbelt_fastened` and `idle_seconds`. A sample whose `observed_at` is older than the newest processed sample is recorded with `stale: true` and ignored by the rule.
- **Prototype rule** `prototype.seatbelt_unfastened.v1`: an alert is active while the seatbelt is unfastened and the engine is on. This is an illustration only, not validated safety logic.
  - The first matching sample opens an alert episode and creates **one** `alert_started` announcement with `priority` `high` and `expires_at` set to now plus `COCOON_ANNOUNCEMENT_TTL_SECONDS`.
  - Repeated samples of the same condition create nothing.
  - A clearing sample closes the episode and creates one low-priority `alert_cleared` announcement.
  - The next violation starts a new episode.
- Announcements are retained. Polling with `?after=` never consumes them.
- Delivery `status` is `played`, `interrupted`, `failed` or `expired`. **`played` is not acknowledgement.** Alert state (`active` or `cleared`) changes only through telemetry.
- The worker skips events that already have `played`, `interrupted` or `expired` from any consumer. It retries `failed` events until they expire. A crash between playback and the delivery report can therefore replay one announcement: delivery is not exactly-once audio.
