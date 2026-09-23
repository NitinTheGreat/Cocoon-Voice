# langgraph-agent: Cocoon backend (Developer B)

FastAPI, LangGraph and SQLite. This service owns the v1 HTTP API, the conversation graph, the business records and the prototype alert rule. It serves `http://127.0.0.1:8000` by default, with interactive docs at `/docs`.

## Layout

```
cocoon_agent/
  api/schemas.py   v1 contract (source of truth for ../contracts/openapi.yaml)
  api/app.py       routes, bearer auth, X-Request-ID, error envelope, /healthz /readyz
  service.py       per-session ordering, turn idempotency (200/202/409), telemetry episodes
  graph/builder.py typed StateGraph: load_context -> route -> {next_task | log_incident | training | explain_alert | cancel_pending} -> compose
  graph/brain.py   live router/wording (Gemini on Vertex AI; legacy Claude option) and the explicit MockBrain
  store.py         SQLite repositories, demo seed, idempotent writes (unique keys on turn_id / event_id)
  migrations.py    versioned cocoon.db migrations (schema_migrations ledger); see docs/MIGRATIONS.md
  catalog.py       verified read-only machine/operator catalog + trusted site/shift bindings
  backup.py        consistent SQLite backups (Online Backup API)
  auth.py          actor principals, opaque token generation/digests, lifetimes
  token_admin.py   issue/list/show/revoke logic behind scripts/actor_tokens.py
  rules.py         PROTOTYPE seatbelt rule on simulated telemetry
  contract/        PROPOSED target contract models (I01); exported to ../contracts/proposed, never imported by the app
scripts/           smoke.py, chat_cli.py, simulate_telemetry.py, reset_db.py, backup_db.py, actor_tokens.py,
                   export_openapi.py, export_proposed_contract.py
tests/             contract drift, API behaviour, resilience/restart, mock brain, offline live-brain, proposed-contract,
                   migrations, catalog and session admission (fixtures/: tiny synthetic catalog, NOT the dataset)
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
- `GET /readyz` shows whether SQLite and the checkpointer are open, whether the verified catalog is loaded (`catalog`, `catalog_version`, sanitized `catalog_issue`), the cocoon.db `schema_version` and the `llm_mode`. It returns 503 when the service is not ready, including when the catalog is missing or does not match its pinned hash. In that case the process still serves existing sessions but refuses new ones with 503 `catalog_unavailable`.
- On start the server applies pending cocoon.db migrations (docs/MIGRATIONS.md). A migration error stops startup and leaves the database at its previous version. Back up a database you care about first: `python scripts/backup_db.py`.
- Uvicorn always runs with `workers=1`, because per-session ordering depends on in-process locks.

The server binds to `127.0.0.1`. To let a voice worker on another machine reach it, set `COCOON_HOST=0.0.0.0` behind a firewall or tunnel and use a long random `COCOON_SERVICE_TOKEN`.

## LLM modes

| `COCOON_LLM_MODE` | Behaviour | Needs |
|---|---|---|
| `mock` (default) | Deterministic keyword router and templated wording that run **through the same graph and tools**. Reported as `llm_mode: "mock"` in `/readyz`, every turn result and the state, and logged at startup. | nothing |
| `live` (`COCOON_LLM_PROVIDER=vertex`, default) | Gemini on Vertex AI (`VERTEX_MODEL`, default `gemini-3.8-flash`, `VERTEX_THINKING_LEVEL=low`) routes with schema-validated JSON output and words the reply from the saved action results. Uses Application Default Credentials through the Google SDK; the backend never opens the credential file. | `GOOGLE_CLOUD_PROJECT` (+ `GOOGLE_CLOUD_LOCATION`, default `global`) and ADC: gcloud's default location, or `GOOGLE_APPLICATION_CREDENTIALS=<path>` |
| `live` (`COCOON_LLM_PROVIDER=anthropic`, legacy) | Claude (`COCOON_LLM_MODEL`, default `claude-opus-5`, effort `low`). | `ANTHROPIC_API_KEY` |

Live mode **never** falls back to mock answers. If the provider fails, refuses or returns unusable output, the turn fails with `503 llm_unavailable` (retryable), and the voice worker tells the operator it cannot confirm yet. The model only classifies and words replies. Every mutation happens in validated Python functions in `store.py`, and the reply is composed only after the record is saved.

**Live load and capacity (observed 2026-09-24):**
- A turn makes one model call: routing. The reply is worded from the saved action results unless `COCOON_LLM_COMPOSE=model`.
- Calls are bounded: one in flight (`COCOON_LLM_MAX_CONCURRENCY`) with up to 8 waiting (`COCOON_LLM_MAX_WAITING`), and at most 3 attempts per call (`VERTEX_MAX_ATTEMPTS`) with 1–4 s jittered backoff, for 408/429/5xx/timeouts only. The SDK's own retry stays off.
- On project `orbit-507316` / `global`, `gemini-3.8-flash` returned intermittent `429 RESOURCE_EXHAUSTED` with no quota identifier. 7 of 7 isolated calls succeeded in one window, and 3 of 5 turns failed in another, so this looks like shared capacity rather than a fixed per-project count. A failed turn is a retryable `503 llm_unavailable` and never a mock answer. Evidence: `docs/HANDOFF.md`.

## Develop without audio

```
python scripts/smoke.py                 # scripted end-to-end check of every flow over HTTP (exit 1 on failure)
python scripts/chat_cli.py              # interactive text chat; /state, /events, /quit
python scripts/simulate_telemetry.py --room <room> --identity <participant>   # SIMULATED seatbelt scenario
python scripts/simulate_telemetry.py --session-id ses_...  --scenario seatbelt-start
python scripts/reset_db.py [--wipe]     # apply migrations and seed demo data idempotently; --wipe deletes data/ (stop server first)
python scripts/backup_db.py [--dest D] # consistent backup of cocoon.db + checkpoints.db (safe while running)
```

The simulator resolves the session with the same `client_session_key` the worker uses (`lk:<room>:<identity>`), so it targets the live voice session. Its defaults are the catalog IDs `--machine EXC_DEMO_001 --operator OP_DEMO_1_1`, and they must match what the worker sent for that key. `smoke.py` and `chat_cli.py` use the same defaults; pass `--machine`/`--operator` for another catalog.

**Catalog IDs are required for new sessions.** The server loads `DATASET_ROOT` (default `../Cocoon_Dataset_v1`, the untracked local development dataset) and checks it against `DATASET_MANIFEST_SHA256`. Without that folder, or with a different snapshot, `/readyz` is not ready and new sessions get 503. Existing `client_session_key`s keep their original association. Details and the client migration note: `docs/MIGRATIONS.md` and `../API_CONTRACT.md`.

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
- Business records go to `data/cocoon.db`: sessions, turns, tasks, lessons, incidents, training assignments, alerts, telemetry, announcements and deliveries. Migrations and seeding are idempotent and run on every start. Verified catalog snapshots are recorded in `catalog_versions*`, and each new session stores the snapshot it was admitted under.
- The pending question and the latest alert are explicit graph state. Telemetry transitions write the latest alert into the checkpoint as well as the database, so a later "Why?" resolves against it.

## Demo site, shifts and assigned tasks (Batch B)

`demo/demo_site_v1.json` is a tracked **synthetic** fixture: one site, 5 zones, and a 07:00–15:00 shift for each of the
5 catalog machines with its demo operator (`EXC_DEMO_001`/`OP_DEMO_1_1` … `BHL_DEMO_001`/`OP_DEMO_5_1`), plus 1–3
scheduled tasks each. Task types and units follow the dataset's task history. Conditions and durations are labelled
`synthetic_demo_fixture` and `demo_supplied_estimate`; they are not live weather or calibrated estimates. Seeding is
idempotent: it adds or reuses rows, refuses to overwrite a differing row, never wipes the database and never touches
the dataset CSVs. It also writes the trusted site/shift bindings file read by the server.

```bash
python scripts/seed_demo.py                              # today at the site; or --service-date 2026-09-24
SESSION_BINDINGS_PATH=data/demo/session_bindings.json python -m cocoon_agent
```

```powershell
python scripts\seed_demo.py
$env:SESSION_BINDINGS_PATH = "data\demo\session_bindings.json"; python -m cocoon_agent
```

- **Binding a session to its shift.** A new session is bound to its shift when it sends a matching
  `site_id`/`shift_id`. If it sends neither, the server binds it automatically when exactly one trusted binding for
  that operator and machine is for today's date at the site (`context_source` ends in `:auto`).
- **What a bound session gets.** `shift` and `assigned_tasks` in `/state`; voice "what's my next task", "what are my
  tasks", "start the next task" and "I finished the task"; and tap commands on
  `POST /v1/sessions/{session_id}/commands` (`task.start` / `task.complete`, with optional `expected_version`).
- **Shared rules.** Voice and taps go through one command service that records each committed command once, with
  the same ownership, legal-transition and version checks.
- **Incidents (B2).** "Log an incident: hose leaking near the stockpile yard, high severity, and tell my supervisor"
  saves a structured report (what, where + site zone, when, severity, each with its basis; identity from the session)
  and a linked supervisor-review request that stays `pending` (no notification or decision exists yet). A missing
  description is asked for. Drafts are listed separately in `/state.incident_drafts`; "confirm/dismiss the draft",
  a bare "yes" (only when exactly one thing is waiting) and taps `incident.edit` / `incident.confirm` /
  `incident.dismiss` change them once. Every turn result carries `branch` and `action_records` (also on a failed
  turn); retrying a failed `turn_id` reuses what was saved and runs only what is missing.

## Actor tokens (local prototype auth, I02b)

The trusted service (voice worker, simulator, scripts) keeps using `COCOON_SERVICE_TOKEN`. Operators and supervisors get their own opaque tokens, issued locally. There is no public sign-up, password or token-minting endpoint, and no token is ever embedded in Android or React builds. This is a prototype mechanism, not an identity provider. Beyond localhost, use TLS.

The CLI works on `COCOON_DATA_DIR/cocoon.db` (from `.env` or the environment), and its first output line names the database. The token is written once to a **new** file given with `--out`; it is never printed, logged or stored (only its SHA-256 is). Put the file under `data/tokens/`, which is git-ignored. On Windows, `os.open` mode bits do not restrict readers, so the file inherits the folder's ACL; keep it inside your own user profile.

Bash (from `langgraph-agent/`):

```bash
python scripts/actor_tokens.py issue --role operator --operator-id OP_DEMO_1_1 --out data/tokens/op-demo-1-1.token
python scripts/actor_tokens.py issue --role supervisor --principal-id sup-demo-1 --out data/tokens/sup-demo-1.token --ttl-hours 4
python scripts/actor_tokens.py list                      # metadata only: token_id, principal, expiry, status
python scripts/actor_tokens.py revoke tok_0123456789abcdef --reason "demo finished"   # idempotent
curl -s -H "Authorization: Bearer $(cat data/tokens/op-demo-1-1.token)" http://127.0.0.1:8000/v1/me
```

PowerShell (from `langgraph-agent\`):

```powershell
python scripts\actor_tokens.py issue --role operator --operator-id OP_DEMO_1_1 --out data\tokens\op-demo-1-1.token
$H = @{ Authorization = "Bearer $((Get-Content data\tokens\op-demo-1-1.token -Raw).Trim())" }
Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/me -Headers $H
python scripts\actor_tokens.py revoke tok_0123456789abcdef
```

- **Local tester workflow.**
  1. Issue an operator token with a catalog operator ID.
  2. The trusted service creates that operator's session with `POST /v1/sessions`, for example the voice worker or `scripts/smoke.py --operator OP_DEMO_1_1`.
  3. `GET /v1/me` with the operator token lists the operator's own `catalog_verified` sessions (`allowed_associations[].session_id`).
  4. Use that session's turns/state/events routes with the operator token.

  Always send the token in the `Authorization` header, never in a URL.
- **Operator tokens** reach only their own `catalog_verified` sessions. Any other session is 404. They cannot create sessions or post telemetry or delivery reports (403). **Supervisor tokens** can only call `GET /v1/me` for now (no site grants exist). The full matrix is in `../API_CONTRACT.md` → "Actor access (I02b)".
- **Lifetime** is 5 minutes to 30 days, 12 h by default, on the wall clock. Expiry and revocation are checked on every request. A principal's role or operator can never be changed by issuing another token. Operator issuance needs the verified catalog; supervisor issuance does not.

## Tests

```
pytest                                  # 270 tests + 1 skipped on Windows without symlink rights; no credentials, no network
python scripts/export_openapi.py --check           # runtime contract (what is served)
python scripts/export_proposed_contract.py --check  # proposed target contract (not served)
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
- Proposed-contract checks (`tests/test_proposed_contract.py`, 146 cases). They cover the generated target spec, 100 valid/invalid fixtures, event-sequence invariants, exchange examples and the chunked SSE fixture. They also confirm that v1 examples stay valid under the target models and that no proposed route is registered. These are schema/fixture checks only: no proposed route is implemented.
- Migrations (`tests/test_migrations.py`): fresh database; adoption and upgrade of a populated copy of the verbatim v1 schema (`tests/fixtures/baseline_v1_schema.sql`) with every row preserved; legacy session retrieval and turn replay; rollback of a failing migration; newer/unknown/gapped schemas refused; bounded lock failure; immutability triggers; backup of an open WAL database.
- Catalog and sessions (`tests/test_catalog.py`, `tests/test_sessions.py`):
  - path resolution from several working directories;
  - wrong pinned hash, changed or missing files, malformed or duplicate IDs, row counts, and files outside the root (the symlink case is skipped where symlinks are not permitted);
  - all five assets, unknown-ID codes and their precedence;
  - retries, conflicts and concurrent creators on separate connections;
  - trusted and spoofed site/shift;
  - catalog unavailability;
  - version pinning across catalog changes.
- The local dataset check runs only when `../Cocoon_Dataset_v1` exists.
- Actor auth (`tests/test_auth.py`):
  - upgrade of a populated v2 database, and no plaintext tokens stored;
  - CLI issue/list/show/revoke and every refusal case;
  - missing, malformed, wrong, expired (injected wall clock) and revoked credentials, duplicate headers, and a failing token store (503);
  - the full route matrix per role and `/v1/me` for each principal;
  - two-operator isolation (URL tampering, body overrides, utterance text, nested state, the training tool);
  - legacy sessions stay service-only;
  - catalog loss after issuance.

## Environment variables

Mock mode needs `COCOON_SERVICE_TOKEN` and a verified catalog (`DATASET_ROOT`, `DATASET_MANIFEST_SHA256`; defaults point at the local development dataset). `SESSION_BINDINGS_PATH` is optional. Live mode also needs `COCOON_LLM_MODE=live`, `GOOGLE_CLOUD_PROJECT` and ADC (see "LLM modes"). Everything else has a default; see `.env.example`.
