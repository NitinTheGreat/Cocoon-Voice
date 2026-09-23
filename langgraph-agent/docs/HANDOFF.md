# Backend handoff

Newest increment first. Each entry separates what was observed from what is still unverified.

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
