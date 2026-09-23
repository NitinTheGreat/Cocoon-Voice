# Cocoon target contract (PROPOSED, I01)

This folder is the frozen interface that the backend (`langgraph-agent`), the voice worker, the Android app and the supervisor web view build against. **Nothing here is served by the running backend unless its operation says `x-implementation-status: implemented`.** The running API is `../openapi.yaml`. The capability table and compatibility decisions are in `../../API_CONTRACT.md`.

## Files

| Path | What it is | How it is produced |
|---|---|---|
| `openapi.json` | Full target API: the 9 implemented operations copied unchanged from `../openapi.yaml`, plus 18 proposed operations. Each operation has `x-implementation-status`, `x-target-stage`, `x-callers` and, where relevant, `x-idempotency` and `x-target-changes`. | Generated: `python scripts/export_proposed_contract.py` (from `langgraph-agent/`) |
| `schemas/cocoon.turn-stream.v1.schema.json` | One turn SSE envelope (six variants). | Generated |
| `schemas/cocoon.announcements.v1.schema.json` | One announcement envelope (polling and SSE). | Generated |
| `schemas/cocoon.supervisor-feed.v1.schema.json` | One supervisor change-feed event (role-filtered). | Generated |
| `schemas/cocoon.command.v1.schema.json` | One command envelope (tap, voice-confirmed, offline sync). | Generated |
| `internal/*.schema.json` | Backend-internal classifier decision and action plan. Never sent to clients. | Generated |
| `examples/index.json`, `examples/valid/`, `examples/invalid/` | 100 fixtures. Each has an expected outcome and, for invalid ones, which layer rejects it (`both` = Pydantic and JSON Schema; `model` = a cross-field rule that only the Pydantic model enforces). | Reviewed, hand-authored |
| `sequences/` | Turn and announcement event transcripts, valid and invalid, checked by `cocoon_agent/contract/sequences.py`. | Reviewed, hand-authored |
| `exchanges/` | Request → status → body examples for duplicates, conflicts, cursors, expiry, cancellation, offline sync, unknown machine and approval decisions. | Reviewed, hand-authored |
| `sse/turn_stream_chunked_unicode.json` | Raw SSE bytes as network chunks (hex), with the parsed events expected. | Reviewed, hand-authored |

The source of the generated files is `langgraph-agent/cocoon_agent/contract/`. Checks: `python scripts/export_proposed_contract.py --check` and `pytest tests/test_proposed_contract.py`.

## What the fixtures are and are not

- IDs, sites, zones, forecasts, vitals, lessons, videos, approvals and hashes in the fixtures are **synthetic contract examples** (`origin: synthetic_contract_fixture` where provenance is shown). They are not persisted records from any run, not dataset fields, not measurements, not sourced thresholds, not approved lesson content and not WESAD evidence.
- The five asset IDs (`EXC_DEMO_001` and so on), `OP_DEMO_1_1` and the shift ID `EXC_DEMO_001_2026-08-24` are the canonical IDs of the audited dataset v1.
- `SITE_FIXTURE_A`, `ZONE_FIXTURE_A_PIT` and every `*_FIXTURE_*` ID do not exist in the dataset. Sites and zones are data gap DG-01.
- Rule IDs use a `demo.` prefix with `evidence_status: synthetic_demo_assumption`. No citation or threshold is invented.
- The lesson video is `availability: not_yet_supplied` with no URL, because no approved video exists yet (DG-10).
- Scenario labels from the dataset's `expected_scenarios.json` are not used as input fields anywhere.

The contract checks prove that schemas are well formed, fixtures behave as labelled and event transcripts obey the sequence rules. They do **not** prove database uniqueness, authorisation, cancellation, recovery, network flushing, a working SSE parser or any client integration. Those are proven in the stage named by `x-target-stage`.

## SSE framing (proposed; implemented in I08)

- Response headers: `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`. No response compression. Hop-by-hop headers are not set blindly.
- One event consists of:
  - optional `id: <event_id>`;
  - `event: <type>`;
  - one or more `data:` lines holding one JSON envelope;
  - a blank line.
- Line endings may be LF or CRLF. Several `data:` lines are joined with LF before JSON parsing. One leading space after the colon is removed.
- Comment lines start with `:`. The server sends `: keep-alive` every `SSE_HEARTBEAT_SECONDS` (proposed default 10, shorter than the client/proxy read timeout). A heartbeat has no sequence, is neither progress nor speech, and does not extend the turn deadline.
- Clients decode incrementally. A network chunk can end mid-frame, mid-line or inside a UTF-8 code point, and can hold several frames. Enforce a maximum frame size. Never `.json()` or `.read()` a live response.
- Before the stream opens, errors use HTTP status and the JSON error envelope: 401/403/404/409/422/429/503. After it opens, errors arrive as a `turn.failed` event. Output size, event count, subscribers per turn and subscriber buffers are bounded; a slow subscriber is disconnected and told to replay rather than growing memory.

## Voice adapter checklist (for the voice owner, when integration is assigned)

This increment changes nothing in `livekit-voice/`. When phase-2 integration is assigned, the adapter should:

1. Create or reuse the backend session from trusted room/participant context, sending a catalog `machine_id` and `operator_id`. Since I02a, a new session with an unknown ID gets 422 `unknown_machine` / `unknown_operator`.
2. Apply the wake gate and final-turn detection locally. Send one finalized utterance with one stable `turn_id`, reused on every retry, with `Accept: text/event-stream`.
3. Parse SSE incrementally as above. Validate schema, session, turn, response, event identity and sequence. Track the last received event, the speech admitted to TTS and the reported playback separately.
4. Feed only `speech.delta.data.text` of the current response to TTS. Never speak progress events, and never re-speak `turn.completed.speech`.
5. On barge-in: stop playback, invalidate the response epoch, POST an idempotent cancel with `reason: barge_in`, and drop late deltas. Cancellation does not undo saved records.
6. On a network break, GET the turn status, then resume by cursor if replay is still available. Do not replay speech already admitted to TTS.
7. Keep a separate announcement subscription with its own cursor. Schedule by priority and expiry; critical announcements may bypass the conversational wake gate under the agreed policy. Deduplicate by `event_id` across polling and SSE.
8. Report response playback (`.../turns/{turn_id}/delivery`) and announcement playback (`.../events/{event_id}/delivery`) with honest position confidence (`unknown` → `played_text: null`). Playback is not acknowledgement.

Actual audio, early TTS, wake/PTT and noise results belong to the joint integration report, not to these fixtures.
