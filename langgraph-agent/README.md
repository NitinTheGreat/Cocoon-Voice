# langgraph-agent: Cocoon backend (Developer B)

FastAPI, LangGraph and SQLite. This service owns the v1 HTTP API, the conversation graph, the business records and the prototype alert rule. It serves `http://127.0.0.1:8000` by default, with interactive docs at `/docs`.

## Layout

```
cocoon_agent/
  api/schemas.py   v1 contract (source of truth for ../contracts/openapi.yaml)
  api/app.py       routes, bearer auth, X-Request-ID, error envelope, /healthz /readyz
  service.py       per-session ordering, turn idempotency (200/202/409), telemetry episodes
  graph/builder.py typed StateGraph: load_context -> route -> {next_task | log_incident | training | explain_alert | cancel_pending} -> compose
  graph/brain.py   live router/wording (Claude via the Anthropic SDK) and the explicit MockBrain
  store.py         SQLite schema, demo seed, idempotent writes (unique keys on turn_id / event_id)
  rules.py         PROTOTYPE seatbelt rule on simulated telemetry
scripts/           smoke.py, chat_cli.py, simulate_telemetry.py, reset_db.py, export_openapi.py
tests/             contract drift, API behaviour, resilience/restart, mock brain, offline live-brain
data/              cocoon.db + checkpoints.db (git-ignored, created on first start)
```

## Install

Windows PowerShell:

```powershell
cd langgraph-agent
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt     # or requirements.txt for runtime only
pip install -e . --no-deps
Copy-Item .env.example .env
```

Bash (macOS or Linux):

```bash
cd langgraph-agent
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e . --no-deps
cp .env.example .env
```

Using uv instead of pip is faster: `uv venv --python 3.11 .venv`, then `uv pip sync requirements-dev.txt`, then `uv pip install -e . --no-deps`. The requirement files are pinned and were compiled with `uv pip compile --universal --python-version 3.11` from `requirements*.in`. To change a dependency, edit the `.in` file, recompile, and rerun the tests.

## Run and stop

```
python -m cocoon_agent            # or: cocoon-agent
```

Stop the server with Ctrl+C. Settings come from `langgraph-agent/.env` whichever directory you start from.

- `GET /healthz` returns `{"status":"ok"}`. It shows the process is up.
- `GET /readyz` shows whether SQLite and the checkpointer are open, and the `llm_mode`. It returns 503 when the service is not ready.
- Uvicorn always runs with `workers=1`, because per-session ordering depends on in-process locks.

The server binds to `127.0.0.1`. To let a voice worker on another machine reach it, set `COCOON_HOST=0.0.0.0` behind a firewall or tunnel and use a long random `COCOON_SERVICE_TOKEN`.

## LLM modes

| `COCOON_LLM_MODE` | Behaviour | Needs |
|---|---|---|
| `mock` (default) | Deterministic keyword router and templated wording that run **through the same graph and tools**. Reported as `llm_mode: "mock"` in `/readyz`, every turn result and the state, and logged at startup. | nothing |
| `live` | Claude (`COCOON_LLM_MODEL`, default `claude-opus-5`, effort `low`) routes with structured output and words the reply from the saved action results. Server-side refusal fallbacks are on by default (`COCOON_LLM_FALLBACKS=default`). | `ANTHROPIC_API_KEY` |

Live mode **never** falls back to mock answers. If the provider fails, refuses or returns unusable output, the turn fails with `503 llm_unavailable` (retryable), and the voice worker tells the operator it cannot confirm yet. The model only classifies and words replies. Every mutation happens in validated Python functions in `store.py`, and the reply is composed only after the record is saved.

## Develop without audio

```
python scripts/smoke.py                 # scripted end-to-end check of every flow over HTTP (exit 1 on failure)
python scripts/chat_cli.py              # interactive text chat; /state, /events, /quit
python scripts/simulate_telemetry.py --room <room> --identity <participant>   # SIMULATED seatbelt scenario
python scripts/simulate_telemetry.py --session-id ses_...  --scenario seatbelt-start
python scripts/reset_db.py [--wipe]     # create schema and seed demo data idempotently; --wipe deletes data/ (stop server first)
```

The simulator resolves the session with the same `client_session_key` the worker uses (`lk:<room>:<identity>`, where operator defaults to identity and machine to `cat-320-demo`), so it targets the live voice session.

## Demo flows (all work in mock mode)

| Say | Result |
|---|---|
| "What's my next task?" | The first seeded pending task (T-101) from SQLite |
| "Report an incident", then "The hydraulic hose is leaking" | Asks for the missing description, stores the pending question in graph state, then saves the incident (`INC-0001`) and speaks its number |
| "Log an incident: cracked mirror" | Saved in one turn |
| "Assign me the seatbelt lesson" / "What training do I have?" | Assigns one of three seeded lessons (L1–L3), or lists assignments |
| (simulator) then "Why?" / "Why did you warn me?" | Explains the latest alert stored for **this** session, whether it is active or cleared |
| "Never mind" while a question is pending | Clears the pending question |

## State and persistence

- The LangGraph thread ID is the application `session_id`, and checkpoints go to `data/checkpoints.db` through `AsyncSqliteSaver`. Only the new utterance goes into graph memory each turn, with message IDs `user:<turn_id>` and `ai:<turn_id>`, so a re-run replaces messages instead of duplicating them.
- Business records go to `data/cocoon.db`: sessions, turns, tasks, lessons, incidents, training assignments, alerts, telemetry, announcements and deliveries. Schema creation and seeding are idempotent and run on every start.
- The pending question and the latest alert are explicit graph state. Telemetry transitions write the latest alert into the checkpoint as well as the database, so a later "Why?" resolves against it.

## Tests

```
pytest                                  # 57 tests, no credentials, no network
python scripts/export_openapi.py --check
```

The tests cover:

- Contract drift, and validation of every example against both Pydantic and the committed OpenAPI.
- 401, 404 and 422 envelopes.
- Idempotent sessions and turns, including 409 conflicts.
- Incident then state, follow-ups, and cross-session isolation.
- Training.
- Alert episodes: one announcement, reset, duplicates, stale samples.
- The events cursor and delivery reports.
- 202 for a duplicate in flight.
- A retry after the action was saved but wording failed.
- An orphaned turn after a restart.
- Records, pending question and completed turns surviving a restart.
- Offline request-shape checks of the live Claude path, which cover the beta header, `fallbacks`, the JSON schema and refusal mapping. These do **not** call the provider.

## Environment variables

Mock mode needs `COCOON_SERVICE_TOKEN` and nothing else. Live mode also needs `COCOON_LLM_MODE=live` and `ANTHROPIC_API_KEY`. Everything else has a default; see `.env.example`.
