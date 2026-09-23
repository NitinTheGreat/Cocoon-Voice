# Backend handoff

Newest increment first. Each entry separates what was observed from what is still unverified.

## I01: freeze the compatible contracts and fixtures

- **Recorded:** 2026-09-24. Branch `backend`, base `f8afb4d` (I00, verified as an ancestor of HEAD; no later commits existed).
- **Requirements covered:** contract readiness for REQ/ADD/SYS rows (see `FEATURE_MATRIX.md`, "Contract readiness after I01"). **No runtime behaviour changed and no form feature became runtime-verified.**
- **Commit:** this handoff is part of the I01 commit (`feat(contract): define compatible agent and application interfaces`). Use `git log --oneline -1 -- langgraph-agent/docs/HANDOFF.md`.

### Current versus proposed artifacts

| Artifact | Role |
| --- | --- |
| `contracts/openapi.yaml` | **Runtime contract (unchanged).** What the app serves today: 7 `/v1` routes plus health. |
| `contracts/proposed/openapi.json` | **Proposed target contract, not served.** 9 implemented operations copied unchanged, plus 18 proposed operations with `x-implementation-status`, `x-target-stage`, `x-callers`, `x-idempotency` and `x-target-changes`. |
| `contracts/proposed/schemas/` | Standalone JSON Schemas: `cocoon.turn-stream.v1`, `cocoon.announcements.v1`, `cocoon.supervisor-feed.v1`, `cocoon.command.v1` |
| `contracts/proposed/internal/` | Backend-internal classifier decision and action plan schemas |
| `contracts/proposed/examples/` (100), `sequences/` (12), `exchanges/` (6), `sse/` (1) | Synthetic contract fixtures with expected outcomes |
| `langgraph-agent/cocoon_agent/contract/` | Source models; never imported by the app (checked by a test in a fresh subprocess) |

### Changed paths

| Path | Change |
| --- | --- |
| `langgraph-agent/cocoon_agent/contract/*.py` (17 files) | New proposed-contract models, route registry/builder and pure sequence checks |
| `langgraph-agent/scripts/export_proposed_contract.py` | New generator with `--check` |
| `langgraph-agent/tests/test_proposed_contract.py` | New contract checks (146 cases) |
| `contracts/proposed/**` | Generated spec/schemas plus reviewed fixtures and README |
| `API_CONTRACT.md` | Artifact roles, drift commands, endpoint capability table (27 operations), compatibility decisions, streaming/announcement/privacy rules, state-transition tables |
| `BACKEND_IMPLEMENTATION_PLAN.md` | One sentence in section 6: the "twelve routes" are 7 implemented + 5 proposed |
| `README.md` (root) | Narrow edit: phase wording no longer says branch `voice`; notes that backend streaming is proposed, not implemented |
| `langgraph-agent/README.md`, `langgraph-agent/CLAUDE.md`, `docs/FEATURE_MATRIX.md`, `docs/DATA_GAPS.md`, `docs/HANDOFF.md` | Documentation and status updates |

Unchanged: `cocoon_agent/api/**`, `graph/**`, `service.py`, `store.py`, `rules.py`, `config.py`, `contracts/openapi.yaml`, `contracts/examples/**`, dependencies and lock files, `livekit-voice/**`, `Cocoon_Dataset_v1/`. No dependency was added: `jsonschema` and `pyyaml` were already dev dependencies.

### Checks run and observed results

Environment: the I00 `.venv`, Python 3.11.15, explicit `COCOON_LLM_MODE=mock`, no credentials, no network calls.

| Command (from `langgraph-agent/`) | Result |
| --- | --- |
| `COCOON_LLM_MODE=mock python -m pytest -q` | **203 passed** (57 existing + 146 new) |
| `python -m pytest -q` on the five pre-existing test files | **57 passed**, the same as the I00 baseline |
| `python scripts/export_openapi.py --check` | `contract up to date` (runtime export unchanged) |
| `python scripts/export_proposed_contract.py --check` | `proposed contract up to date` |
| Mock server on port 8766, temp data dir, throwaway token + `scripts/smoke.py` | `/readyz` ready; `SMOKE OK (llm_mode=mock)` |
| Probes on the same server | unknown machine → **201** (documented gap, unchanged); `POST .../turns/stream` → 405; `GET /v1/me` → 404 |
| Token grep in server log | 0 matches; process stopped, temp data and log deleted |
| Dataset `sha256sum -c CHECKSUMS.sha256` (read-only) | 33/33 OK; fingerprint recorded in `DATA_GAPS.md` |

What the 146 contract checks cover:
- the generated spec equals the committed files;
- implemented operations are copied byte-for-byte from the runtime spec;
- no proposed route is registered in the app, and the runtime spec has no target-only schemas;
- all `$ref`s resolve, and component and standalone schemas pass the JSON Schema meta-schema;
- each of the 100 fixtures behaves as labelled (valid → accepted by Pydantic and JSON Schema; invalid → rejected by the stated layer);
- every v1 example is still valid under its target model;
- sequence invariants hold (fixed identity, increasing sequence, one terminal event, deltas equal to saved speech, unique announcement IDs);
- exchange statuses are documented and their bodies validate;
- the chunked SSE fixture is self-consistent against a test-only reference reader;
- supervisor projections are closed and have no private field names;
- the API_CONTRACT table lists every route with the right status.

**Not run and not claimed:** any proposed route (none exists), live Vertex or Anthropic calls, the voice worker, Android or supervisor clients, streaming over a socket, cancellation, restart recovery, database uniqueness for new records, and dataset generation. The contract checks do not prove runtime behaviour.

### I00 findings: resolved at contract level versus still open

| I00 finding | After I01 |
| --- | --- |
| Unknown machine accepted (201) | Contract decided: 422 `unknown_machine` / `unknown_operator` (intentional, documented tightening). **Runtime unchanged**; enforced in I02 with the catalog. |
| No streaming route, no `/v1/me` | Specified as `proposed` (I08, I02). **Not implemented.** The 405 comes from the registered `GET .../turns/{turn_id}` template matching `turn_id=stream`. |
| "Twelve routes" wording | Plan and API_CONTRACT now say 7 implemented + 5 proposed. |
| Anthropic implementation vs Vertex design | Decision recorded: Vertex selected; replacement and model/access verification in I04. No provider code or dependency changed. |
| Missing data/content (DG-01..DG-16) | Shapes defined in the contract. **Data, content, published guidance and WESAD still missing.** |
| Untracked dataset | Local snapshot fingerprint recorded. Data-owner review and versioning is still a prerequisite for I03. |
| Root README said branch `voice` | Fixed with a narrow edit. |
| No client integration | Unchanged. Voice, Android and supervisor integration remain later, separately assigned work. |

### Migration note for other owners

- **Voice and simulator:** `POST /v1/sessions` will reject free-text machine IDs such as `cat-320-demo` once I02 lands. Switch to a catalog asset ID (`EXC_DEMO_001` etc.) before then. Every other change to the seven routes is additive and optional.
- **Android and supervisor:** build against `contracts/proposed/openapi.json` and the standalone schemas. Use scoped actor tokens (I02), never the service token. Supervisor views come only from `/v1/supervisor/*`.
- **Data:** the fixture shapes in `contracts/proposed/examples/valid/telemetry_v2_batch.request.json`, `conditions_forecast.json` and `task_assignment.json` are the target for the I03A extension. Their values are not data.

### Next increment: first I02 slice

**I02a: catalog-bound sessions on a versioned schema.**
1. Add versioned SQLite migrations (schema version table) that upgrade the current v1 tables in place.
2. Add a `DATASET_ROOT` setting (default `../Cocoon_Dataset_v1`). Load a read-only machine/operator catalog keyed by manifest hash.
3. Enforce 422 `unknown_machine` / `unknown_operator` on `POST /v1/sessions`, and store `site_id`/`shift_id`/manifest hash on the session as the target contract specifies.
4. Update the backend tests, smoke script and simulator to use catalog IDs, and regenerate both contracts.

Actor tokens, `/v1/me`, consent and the command/action ledger follow as separate I02 slices.

Prerequisites:
- the data owner confirms or commits the dataset snapshot (the manifest hash above), or explicitly accepts the local fingerprint for development;
- the voice owner is told about the catalog-ID switch (no voice change is made by the backend);
- the I02 handoff records the unknown-ID behaviour change.

## I00: inspect the checkout and close the scope audit

- **Recorded:** 2026-09-24. Branch `backend`, base commit `ee83254` (merge of PR #1, the voice branch, on top of `521d318`).
- **Plan:** `BACKEND_IMPLEMENTATION_PLAN.md` revision 2.1, committed with this increment.
- **Requirements covered:** audit of every REQ/ADD/SYS row; SYS-14 and SYS-15 progress. No runtime behaviour changed.
- **Commit:** this handoff is part of the I00 commit (`docs(backend): reconcile review form and implementation baseline`). The hash cannot be written into its own commit. Use `git log --oneline -1 -- langgraph-agent/docs/HANDOFF.md`.

### Changed paths

| Path | Change |
| --- | --- |
| `BACKEND_IMPLEMENTATION_PLAN.md` | Added to Git; it was untracked. Content unchanged from the reviewed revision 2.1. |
| `langgraph-agent/docs/FEATURE_MATRIX.md` | New. Every REQ/ADD/SYS row with tier, owner, data prerequisite, stage, status and I00 evidence, plus a plan-versus-checkout mismatch table. |
| `langgraph-agent/docs/DATA_GAPS.md` | New. Verified v1 dataset state, supplied fields, 16 missing-data entries (DG-01..DG-16) and source limits. |
| `langgraph-agent/docs/HANDOFF.md` | New. This file. |
| `langgraph-agent/CLAUDE.md` | Snapshot, verified commands, active work and latest handoff updated from I00 evidence. Pointer to the plan added. |

No code, schema, contract, fixture, dependency or config file changed. `contracts/openapi.yaml` is unchanged and its drift check still passes.

### Environment used for checks

- Python 3.11.15 (uv-managed interpreter). Created a gitignored `langgraph-agent/.venv` with `uv venv --python 3.11 .venv`, `uv pip sync requirements-dev.txt` and `uv pip install -e . --no-deps`.
- Installed versions: fastapi 0.141.1, uvicorn 0.53.0, langgraph 1.2.12, langgraph-checkpoint 4.2.0, langgraph-checkpoint-sqlite 3.1.1 (`AsyncSqliteSaver`), aiosqlite 0.22.1, langchain-core 1.6.4, anthropic 1.8.0, pydantic 2.13.5, pydantic-settings 2.15.0, httpx 0.28.1.
- LLM mode: **mock only**. No live provider was called. No `.env` file was created: the smoke server took its settings from process environment variables, with a throwaway token and a data directory outside the repository.
- No credential file was read. ADC and Vertex access were not exercised.

### Checks run and observed results

| Command (working directory) | Result |
| --- | --- |
| `.venv/Scripts/python -m pytest -q` (`langgraph-agent/`) | **57 passed** in 7.1 s. No network, no credentials. |
| `.venv/Scripts/python scripts/export_openapi.py --check` (`langgraph-agent/`) | `contract up to date`, exit 0. |
| `python -m cocoon_agent` with `COCOON_LLM_MODE=mock`, port 8765, temp data dir | `/healthz` → `{"status":"ok"}`. `/readyz` → ready, database and checkpointer true, `llm_mode: mock`. |
| `.venv/Scripts/python scripts/smoke.py --base-url http://127.0.0.1:8765` | `SMOKE OK (llm_mode=mock)`. Covered: next task (T-101), incident with follow-up (stored as incident 1), lesson assignment, simulated seatbelt announcement, and "Why did you warn me?" explained from the stored alert. |
| HTTP probes on the same server | Unknown `machine_id`/`operator_id` session create → **201** (accepted; gap DG-16). No token → 401. `POST /v1/sessions/x/turns/stream` → 405 (route absent). `GET /v1/me` → 404 (route absent). |
| Server log grep for the token value | 0 matches. |
| `sha256sum -c CHECKSUMS.sha256` (`Cocoon_Dataset_v1/`) | 33/33 OK, before and after the dataset tests. |
| `python cocoon_data.py validate` (`Cocoon_Dataset_v1/`) | `status: passed`. |
| `python -m unittest test_data` (`Cocoon_Dataset_v1/`) | 12 tests OK. |
| Benchmark recomputation from `data/raw/problem_statement_task_samples.csv` | MAE 7.6 min (errors 2, 7, 12, 2, 15). |

**Not run:** live Anthropic or Vertex calls, any voice worker check, any Android or supervisor client, streaming (not implemented), and restart of a real server process. Restart survival is covered only by the in-process pytest cases `test_records_and_context_survive_restart` and `test_orphaned_processing_turn_is_rerun_after_restart`.

### Verified existing behaviour (v1 prototype, mock mode)

- Seven `/v1` JSON routes match `contracts/openapi.yaml`. Bearer service token, `X-Request-ID` echo and the error envelope are in place.
- Idempotency is enforced in SQLite: session create by `client_session_key` (200/201/409); turns by `(session_id, turn_id)` (200 replay, 202 in flight, 409 conflict); telemetry by `(session_id, event_id)`; incidents by source turn; training by `(operator_id, lesson_id)`; one active alert episode per rule.
- One prototype seatbelt rule on `simulated: true` telemetry creates one announcement per episode, clears and retriggers, and marks stale samples.
- Events are retained and cursor-paged without being consumed. Delivery reports are kept per consumer.
- "Why?" explains the latest stored alert for the session.

See `FEATURE_MATRIX.md` for how little of the filled form this covers. No form requirement is `verified`.

### Path ownership and working tree after I00

| Path | Owner | State |
| --- | --- | --- |
| `langgraph-agent/**`, `BACKEND_IMPLEMENTATION_PLAN.md` | Backend | I00 files committed. `.venv/`, `.pytest_cache/` and `cocoon_agent.egg-info/` are local and gitignored. |
| `API_CONTRACT.md`, `contracts/**` | Backend + voice (joint) | Unchanged. |
| `livekit-voice/**` | Voice | Unchanged, not read beyond its README/CLAUDE.md scope notes. Worker remains standalone. |
| `Cocoon_Dataset_v1/` | Data stream | **Untracked, left unstaged.** Validated read-only. The data owner decides whether to commit it. |
| Root `README.md`, `.gitignore` | Shared | Unchanged. The README still calls the current phase "branch `voice`". |

Git identity: repository-local `user.name`/`user.email` set to `Naif Naqeeb <naifnaqeeb.123@gmail.com>`, the values already configured globally for Naif. Global config was not changed. `GIT_AUTHOR_*`/`GIT_COMMITTER_*` overrides were not set in the environment.

### Blockers and prerequisites

1. **LLM provider mismatch.** The plan specifies Vertex AI via ADC. The code uses the Anthropic SDK with `ANTHROPIC_API_KEY`. Decide before I04. Do not silently swap.
2. **Dataset not in Git** on `backend`. Its integrity is verified locally, but there is no reviewed reference hash (DATA_GAPS.md). The data owner should commit it or confirm the manifest hash.
3. **Missing core inputs** (DG-01..DG-11, DG-15, DG-16): sites/zones, scheduled tasks, proximity, high-rate motion/tilt, human impact, numeric weather/forecasts, sleep, consent, rule policies, LMS content with a real video, and WESAD.
4. **No client integration** exists. The voice worker is standalone and there is no Android or supervisor client in this repository. Checkpoints 1 and 2 are unproven beyond mock HTTP.
5. **Published guidance** for thresholds (seatbelt, proximity, slope, wind/visibility, heat) is not yet sourced. Rules stay labelled demo assumptions until it is.

### Next increment

**I01: freeze the compatible contracts and fixtures.** Keep the seven existing routes compatible. Specify the five streaming/control routes and the additive operator/supervisor/command/consent/content/presentation contracts as *proposed*. Add strict schemas and fixtures for every new form field, and record the Vertex-versus-Anthropic decision as an input to I04.
