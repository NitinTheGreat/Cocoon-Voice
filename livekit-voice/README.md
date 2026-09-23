# livekit-voice: Cocoon voice worker (Developer A)

A long-lived **LiveKit Agents worker** (`livekit-agents==1.8.2`) that connects out to LiveKit Cloud and registers under the explicit dispatch name **`cocoon-voice`**. For each room it is dispatched into, it runs:

```
WebRTC mic ─▶ LiveKit Inference STT ─▶ end-of-turn (turn detector) ─▶ CocoonAgent.llm_node
          ─▶ POST {COCOON_BACKEND_URL}/v1/sessions/{id}/turns ─▶ speech text ─▶ LiveKit Inference TTS ─▶ WebRTC
```

There is no realtime speech-to-speech model and no second reasoning LLM here. All reasoning happens in `langgraph-agent`.

## Layout

```
cocoon_voice/
  agent.py          AgentServer + rtc_session(agent_name), CocoonAgent.llm_node, session config, health port
  bridge.py         single submission path: final user text -> stable turn_id -> backend; session binding
  backend_client.py one reusable httpx.AsyncClient; 200/202-poll/409/5xx/timeout semantics; TurnOutcomeUnknown
  announcements.py  event poller + queued playback + delivery reports + bounded backoff
  contract.py       the worker's own copy of the v1 models it uses (not imported from langgraph-agent)
  mock_backend.py   MOCK contract-compatible backend (in-memory) for developing without langgraph-agent
  config.py         settings from livekit-voice/.env (also exports LIVEKIT_* for the SDK)
scripts/dispatch.py      explicit dispatch / dev join token / list dispatches
scripts/backend_probe.py drive the bridge against a backend with no LiveKit or audio
```

### SDK findings that shaped the bridge (verified against the installed 1.8.2 source)

- **`llm_node` only runs if the session has an LLM.** `agent_activity.py` does `elif self.llm is None: return  # skip response if no llm is set`. The session therefore gets `BackendBridgeLLM`, an `llm.LLM` placeholder whose `chat()` raises, and `CocoonAgent.llm_node` makes the HTTP call and yields the final speech string. `LLMAdapter`, the local compiled-graph adapter, is not used.
- **Preemptive generation defaults to on** (`PreemptiveGenerationOptions.enabled=True`). It is disabled with `TurnHandlingOptions(preemptive_generation={"enabled": False})`, so a turn that can trigger actions is never posted speculatively. `tests/test_bridge.py` asserts this.
- `turn_id = "lk-" + <LiveKit ChatMessage.id>`. It is stable for the utterance and reused on every retry. If the last chat item is not a user message, `llm_node` submits nothing, so an old turn is never resubmitted. Partial transcripts never reach `llm_node`.
- The greeting and announcements use `session.say()`, which never invokes `llm_node`.
- Model strings are validated against the SDK's `STTModels` and `TTSModels` literals and the current quickstart:
  - STT `assemblyai/universal-3-5-pro` (`en`)
  - TTS `fishaudio/s2.1-pro`, voice `fa4c9eb3dccc4806b382b40d61c6b10a`
  - Turn detection `inference.TurnDetector()`

  To use another model, set `COCOON_STT_*` or `COCOON_TTS_*`, for example `deepgram/nova-3` or `cartesia/sonic-3`. Voice IDs are provider-specific.

## Install

Windows PowerShell:

```powershell
cd livekit-voice
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt     # dev: tests + mock backend; requirements.txt = worker runtime only
pip install -e . --no-deps
Copy-Item .env.example .env             # then fill LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
```

Bash:

```bash
cd livekit-voice
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e . --no-deps
cp .env.example .env
```

With uv: `uv venv --python 3.11 .venv`, then `uv pip sync requirements-dev.txt`, then `uv pip install -e . --no-deps`. The pins were compiled with `uv pip compile --universal --python-version 3.11` from `requirements*.in`.

**Model downloads:** none are needed for this configuration. VAD uses the SDK's bundled native model, and STT, TTS and turn detection run on LiveKit Inference. `python -m livekit.agents download-files` is the current command (running `download-files` through the agent script is deprecated as of 1.5.10). It reports "nothing to download" and is safe to run in deployment scripts. If you later add a `livekit-plugins-*` package with local models, run it before `start`.

## Run and stop

| Command (from `livekit-voice/`, venv active) | What it does |
|---|---|
| `python -m cocoon_voice.agent dev` | Registers with LiveKit Cloud, reloads on file changes, debug logs |
| `python -m cocoon_voice.agent start` | Production-style worker |
| `python -m cocoon_voice.agent console` | Local mic and speaker in the terminal. It still uses LiveKit Inference, so it needs the LiveKit keys. **This is not a Playground or WebRTC test.** |
| `python -m cocoon_voice.mock_backend` | MOCK backend on `127.0.0.1:8010`. Set `COCOON_BACKEND_URL=http://127.0.0.1:8010`. |
| `python scripts/backend_probe.py "What's my next task?"` | Runs the same bridge code as `llm_node` against the configured backend, with no LiveKit |

Press Ctrl+C to stop. The worker drains, then the session's shutdown callback stops the announcement poller and closes the HTTP client.

**Health endpoint** (the SDK's own server, on `COCOON_HEALTH_HOST:COCOON_HEALTH_PORT`, default `127.0.0.1:8081`, which does not clash with the backend on 8000 or the mock on 8010):

- `GET /` returns `OK`. It returns `503` only after LiveKit connection retries are exhausted, or if the inference process died. **`OK` does not prove the worker is registered.** While it is still retrying a bad `LIVEKIT_URL`, it says `OK`.
- `GET /worker` returns JSON including `"agent_name": "cocoon-voice"`, `worker_type`, `sdk_version` and `worker_load`.

## Environment variables

| Mode | Required |
|---|---|
| Mock checks (`pytest`, mock backend, `backend_probe.py`) | `COCOON_SERVICE_TOKEN` (the tests set their own) |
| Worker against a backend | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `COCOON_BACKEND_URL`, `COCOON_SERVICE_TOKEN` (same value as the backend's) |

Optional settings, all with defaults in `.env.example`:

- `COCOON_AGENT_NAME` (default `cocoon-voice`)
- `COCOON_HEALTH_HOST` and `COCOON_HEALTH_PORT`
- `COCOON_STT_MODEL`, `COCOON_STT_LANGUAGE`, `COCOON_TTS_MODEL`, `COCOON_TTS_VOICE`
- `COCOON_BACKEND_*` timeouts and attempts, and `COCOON_TURN_DEADLINE_SECONDS`
- `COCOON_EVENT_POLL_INTERVAL` (default 1s) and `COCOON_EVENT_POLL_MAX_BACKOFF`
- `COCOON_DEFAULT_MACHINE_ID`
- `COCOON_GREETING`

## Session binding

- On a job, the worker calls `ctx.connect()` and then `wait_for_participant()`. It binds to the first standard participant, because v1 supports **one operator per room**, and passes that identity to `RoomOptions(participant_identity=...)`.
- It calls `POST /v1/sessions` with `client_session_key = lk:<room>:<participant identity>`. `operator_id` and `machine_id` come from the first of these that is set:
  1. Trusted **job metadata** (JSON from a server-side dispatch).
  2. The participant's `operator_id` and `machine_id` attributes.
  3. The identity and `COCOON_DEFAULT_MACHINE_ID`.
- If the job metadata has `session_id` and the backend knows it, that session is reused.
- If the backend is down at job start, the greeting says so and the session is bound on the first turn. If the backend later returns "session not found", for example after a data wipe, the worker re-creates the session by the same key and retries the same `turn_id` once.

## Turn handling and failure semantics

| Backend response | Worker behaviour |
|---|---|
| `200 completed` | Speaks `speech`. Actions and other internal fields never reach TTS. |
| `202 processing` | Polls `GET .../turns/{turn_id}` every `retry_after_ms` until completed or the deadline |
| timeout or network error | **Checks the turn status first** with the same `turn_id`, then resubmits the same `turn_id` if the backend never saw it or reports `failed` |
| `5xx` or `429` | Bounded exponential backoff with jitter, then resubmits the same `turn_id` |
| `409` or other `4xx` | No retry. Says "Sorry, I couldn't process that request." |
| deadline passed (`COCOON_TURN_DEADLINE_SECONDS`) | Says "I'm having trouble reaching the site system, so I can't confirm that yet…". It **never** says the action failed or was cancelled. |

Logs carry `request_id=<turn_id>.<attempt>`, status and milliseconds for every backend call, and only the text length. They contain no transcripts, audio or tokens.

## Proactive announcements

- `AnnouncementPump` polls `GET /events?after=<cursor>` every `COCOON_EVENT_POLL_INTERVAL` seconds. On a backend outage it backs off exponentially up to `COCOON_EVENT_POLL_MAX_BACKOFF`, so it never busy-loops.
- For each new event:
  1. Skip it if any consumer already reported `played`, `interrupted` or `expired`.
  2. Report `expired` if it is past `expires_at`.
  3. Otherwise wait up to `COCOON_ANNOUNCEMENT_QUIET_WAIT_SECONDS` for a quiet moment, meaning the agent is not thinking or speaking and the user is not speaking.
  4. Call `session.say()`, wait for playout, then report `played`, `interrupted` or `failed`.
- Announcements play one at a time, and `session.say` queues behind ordinary speech.
- The poller stops when the session closes or the job shuts down.
- After a reconnect it starts from `after=0` and relies on the persisted delivery reports, so completed events are not replayed. A crash between playout and the report can replay one announcement.
- `played` is not operator acknowledgement, and it does not resolve the hazard.

## Testing in the browser

### Three states to tell apart

| State | How you know |
|---|---|
| **Worker registered** | `dev`/`start` logs show a successful registration to your `LIVEKIT_URL`. `GET :8081/worker` shows `agent_name: cocoon-voice`. The LiveKit Cloud dashboard's Agents page shows it for the same project. |
| **Agent dispatched into a room** | The worker logs a received job and then `bound room=<room> participant=<identity> -> session_id=ses_...`. The backend logs `POST /v1/sessions`. |
| **Speech connected** | You hear the greeting, and each utterance logs `submitting turn ...` and `turn ... completed` on the worker and `turn completed ...` on the backend. |

**Common trap: registered but never dispatched.** Because `agent_name` is set, LiveKit **does not auto-dispatch** this worker. A room gets the agent only through an explicit dispatch that names exactly `cocoon-voice` (or your `COCOON_AGENT_NAME`), in the **same LiveKit Cloud project** as `LIVEKIT_URL`. If the name differs (for example the console defaults to another agent, or there is a typo), or if the browser uses a different project, the worker stays idle with no error. A token-embedded dispatch also fires **only when the room is created**, so it is ignored for a room that already exists.

### Option A: LiveKit Cloud Agent Console

1. Start the backend (`langgraph-agent`: `python -m cocoon_agent`) and the worker (`python -m cocoon_voice.agent dev`).
2. In the LiveKit Cloud dashboard for the **same project**, open **Agents** and click **Launch Console**. The LiveKit docs say the Console works with agents running locally.
3. If the Console lets you choose or type an agent name, use `cocoon-voice`, start a session, allow the microphone, and speak.

*Not verified here:* no LiveKit credentials were available, so I could not confirm how the current Console selects an explicitly named, self-hosted agent. If it offers no way to target `cocoon-voice`, use option B, which does not depend on the Console UI.

### Option B: explicit dispatch you control (deterministic)

```powershell
# fresh room name, so the token's dispatch fires on room creation
python scripts\dispatch.py token --room cocoon-demo-1 --identity operator-7 --machine cat-320-demo
```

This prints the `LIVEKIT_URL`, a 2-hour **dev** token and a `https://meet.livekit.io/custom?...` link. Open the link, or paste the URL and token into any LiveKit client that accepts them, such as the hosted Agents Playground's manual connection. The room is created by your join, the token's `RoomAgentDispatch(agent_name="cocoon-voice")` dispatches the worker, and you should hear the greeting.

For a room that already exists, dispatch through the API instead: `python scripts\dispatch.py dispatch --room <room>`, or `lk dispatch create --agent-name cocoon-voice --room <room>` if you have the `lk` CLI. List dispatches with `python scripts\dispatch.py list --room <room>`.

### What to say

1. "What's my next task?"
2. "I want to report an incident", then "The hydraulic hose on the boom is leaking"
3. "Assign me the seatbelt lesson"
4. In another shell, run `python scripts/simulate_telemetry.py --room cocoon-demo-1 --identity operator-7 --machine cat-320-demo` from `langgraph-agent`. You should hear the seatbelt warning within about one second of the second sample, then "Why?" or "Why did you warn me?" is answered from the stored alert.

### Local worker vs hosted worker

A worker running on your laptop opens **outbound** connections to LiveKit Cloud and can call `http://127.0.0.1:8000` on the same laptop. A worker **hosted in the cloud**, including one deployed to LiveKit Cloud, **cannot reach `localhost` on a developer's laptop**. Point `COCOON_BACKEND_URL` at a reachable HTTPS URL, such as a deployed backend or a tunnel, and keep the bearer token secret.

## Tests

```
pytest                                    # 35 credential-free tests (+1 integration test skipped)
# real HTTP against a running langgraph-agent (no LiveKit, no audio):
$env:COCOON_INTEGRATION_BACKEND_URL="http://127.0.0.1:8000"; pytest -m integration        # PowerShell
COCOON_INTEGRATION_BACKEND_URL=http://127.0.0.1:8000 pytest -m integration                # Bash
```

The tests cover:

- Client semantics: 200, 202 and polling, a status check before resubmitting after a timeout, same-`turn_id` retries, bounded attempts and deadline, and no retry on 409.
- The bridge: stable `turn_id`, no resubmission, speech-only `llm_node` output, unknown-outcome wording, rebinding, lazy binding, and preemptive generation being off.
- Announcements: once per event, skip or expire, interrupted and failed reports, bounded backoff, and stop.
- Contract checks: worker requests and the mock backend against `../contracts/openapi.yaml`, plus the bridge driven against the mock backend, including 202.

None of these tests exercise real STT, TTS or WebRTC.
