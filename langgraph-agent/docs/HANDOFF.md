# Backend handoff

Newest increment first. Each entry separates what was observed from what is still unverified.

## B1: assigned tasks and the shared command path

- **Schema v4:** `sites`, `site_zones`, `shifts`, `task_assignments` (versioned lifecycle) and `command_log`
  (scope + command ID, fingerprint, outcome, record reference, summary, state version, saved result). The three
  shared seed tasks are untouched; legacy and unbound sessions still use them for "next task".
- **Seeding:** `scripts/seed_demo.py` loads the tracked synthetic fixture into the database and writes the trusted
  bindings file for one service date. It is idempotent and never overwrites differing rows.
- **Binding:** a bound session gets its shift and tasks. Binding is explicit (`site_id` + `shift_id` matching a
  trusted binding) or automatic on the server (exactly one trusted binding for that operator/machine for today at
  the site).
- **One command path:** voice ("start the next task", "I finished the task") and `POST .../commands` (`task.start` /
  `task.complete`). Identity is scoped per principal or per turn; an identical retry returns the saved result, a
  reused ID with a different payload is 409 `idempotency_conflict`, and a stale version or illegal step is 409
  `version_conflict` / `invalid_transition`. An operator only reaches their own shift's tasks; supervisors get 403.
- **Checks:** `tests/test_tasks.py` (7): idempotent seeding and refusal to overwrite, auto and explicit binding, the
  voice lifecycle and same-turn retry, tap rules shared with voice, isolation across all 5 assets and operators,
  unbound legacy behaviour, and a populated v3 → v4 upgrade. Existing migration, session, auth, API, resilience and
  contract tests pass (version assertions now derive from the migration list). Both contract checks were
  regenerated and are clean.

## Batch A (fast track): connect voice through the existing JSON APIs

- **Recorded:** 2026-09-24. Branch `backend`. Order per `BACKEND_FAST_TRACK_PLAN.md`: voice integration comes before I02c/I02d. The user's commit `c779e26` ("check apis") holds the Gemini-on-Vertex integration and git-ignores `Cocoon_Dataset_v1/`. Batch A adds `4d324fc` (bounded Vertex calls, one model call per turn) and the commit containing this entry (499 mapping, voice handoff, docs).
- **Runs now:**
  - The 7 JSON routes, unchanged.
  - Live mode on Gemini `gemini-3.8-flash` via Vertex (`orbit-507316`, `global`, SDK ADC; the credential file is never opened by backend code). One routing call per turn; the reply is worded from the saved action results.
  - Bounds: 1 call in flight, 8 waiting, 3 attempts with 1–4 s jittered backoff for 408/429/5xx/timeouts only, 20 s per call, 45 s per turn.
  - No mock or alternate-model fallback.
- **Checks:**
  - Focused provider tests: 18, covering retry budget and hint capping, non-retried 400/403/404, the 499 deadline message, one call in flight, queue full, template wording, and an HTTP turn where a 429 leaves nothing behind and the same `turn_id` later succeeds once with one model call.
  - Full suite run once after the provider change: 287 passed, 1 skipped (Windows symlink case). The 499 test was added afterwards and passes.
  - Both contract drift checks are clean.
- **Backend HTTP evidence (mock, real process):**
  - `smoke.py` SMOKE OK.
  - Session 201/200; unknown IDs → 422; association change → 409.
  - Turn 200; identical retry returns the stored result; changed text → 409; status GET.
  - Telemetry → announcement; events poll; delivery 200; "why" explanation; 404 and 401 envelopes.

  The exact bodies are in `docs/VOICE_INTEGRATION.md`.
- **Live evidence (real Vertex calls, sanitized):**
  - Direct SDK probes: 7 of 7 calls succeeded (4 spaced about 8 s apart, then a burst of 3); 1.6–4.2 s each; `traffic_type ON_DEMAND`.
  - A 5-turn live run through the backend: 2 turns returned 200 (≈5.7 s and ≈5.0 s; `information_requested` and `alert_explained`).
  - 2 turns hit `429 RESOURCE_EXHAUSTED` ("Resource exhausted. Please try again later.") on all 3 attempts. The payload had no quota ID and no RetryInfo.
  - 1 turn ended with `499 CANCELLED` at the 20 s per-call deadline.
  - Every failure was a retryable `503 llm_unavailable` with no record written. No token appeared in any log.
  - **Diagnosis:** time-dependent shared on-demand capacity, not a fixed per-project call count. No quota purchase or model change was made (neither is authorised).
- **Not observed:** microphone → backend → speaker audio, Android, and the voice worker's `remote_langgraph` wiring (voice owner). Streaming, cancel and per-turn delivery are still proposed only.
- **Voice owner's action:** `docs/VOICE_INTEGRATION.md` → "Voice owner's remaining work".
- **Next:** Batch B (operator workflow), not an automatic return to I02c.

## I02b: actor authentication and `/v1/me`

- **Recorded:** 2026-09-24. Branch `backend`, base `2ea49eb`. That commit merges `main` (voice PR #2, `livekit-voice/**` only) on top of I02a `ec9f02c`. Chain verified: `6bcbf88` ← `ec9f02c` ← `2ea49eb` = HEAD. Backend and contract files at `2ea49eb` are identical to `ec9f02c`.
- **Scope:** the actor-identity slice of I02. **I02 is still partial.** Consent (I02c) and the command/action ledger with recovery (I02d) are not done.
- **Commit:** `feat(auth): add scoped actor tokens and current-principal endpoint`; see `git log -1 -- langgraph-agent/docs/HANDOFF.md`.

### What changed

- **Schema version 3** (`actor_tokens`). New `principals` table (operator or supervisor, immutable; an operator principal is bound to one catalog operator and the snapshot it was verified against; at most one principal per operator). New `actor_tokens` table: SHA-256 digest only, scopes, issue/expiry; only revocation can change, once; no deletes. All v2 records, catalog snapshots and the session immutability rule are unchanged; this was tested on a populated v2 database.
- **Tokens.**
  - Format: `cct_` + `secrets.token_urlsafe(32)`, i.e. 256 random bits.
  - Lifetime: 5 minutes to 30 days (default 12 h), on the wall clock. Data time and client timestamps never affect it.
  - Scopes: operator `me:read sessions:own`, supervisor `me:read`. The scopes a token presents are intersected with its role's scopes, so a token can never widen itself.
  - Expiry and revocation are checked on every request, with no cache.
- **Service credential** (`COCOON_SERVICE_TOKEN`): unchanged. It is compared in constant time before any token lookup and never stored. An unrecognised `cct_` token never falls back to service access.
- **Resolver.**
  - Exactly one `Authorization` header; a credential of at most 256 characters.
  - Every failure gets the same sanitized 401. A token-store error gets 503 `auth_unavailable` and never succeeds.
  - Route dependencies deny before the body is parsed or any tool runs.
- **CLI** `scripts/actor_tokens.py` (`issue`, `list`, `show`, `revoke`):
  - The token is written once to a new `--out` file. It refuses to overwrite, and it revokes the new token if the file cannot be written.
  - The token is never printed. `list` and `show` never show the token or its digest. Revocation is by `token_id`.
- **Access matrix (implemented):**

| Operation | Service | Operator token | Supervisor token |
|---|---|---|---|
| `/healthz`, `/readyz` | public | public | public |
| `GET /v1/me` | service principal | own principal + own verified sessions | own principal |
| `POST /v1/sessions` | unchanged | 403 | 403 |
| turns (POST, GET), state, events | unchanged | own `catalog_verified` session, else 404 | 403 |
| telemetry, event delivery | unchanged | 403 | 403 |

- **Ownership checked for operators.**
  - **Parent session:** it must be `catalog_verified` and its stored `operator_id` must equal the token's operator; otherwise the same 404 as a missing session.
  - **Children:** turn and event lookups stay inside that session.
  - **Tools:** incidents, alerts, announcements, pending questions and the conversation checkpoint are per session. The incident tool writes under the trusted session, not utterance text; request bodies with unknown fields (`operator_id`, `session_id`, `role`) are 422.
  - **Training (a fix found during I02b testing):** assignments are keyed by an operator string that legacy sessions may share. An assignment is now visible and reusable only when its owning session has the **same binding class** as the reader.
    - A verified operator never sees or receives a legacy record.
    - A legacy session no longer sees a verified operator's assignment. That is a narrowing of the service's view of legacy sessions, and it only applies when a legacy session's free-text operator equals a catalog ID.
    - A pair held by the other class is withheld (the tool answers with training status) rather than handed over.
- **Shared content, deliberately not scoped:** the seed task list (T-101..T-103) and the lesson catalogue. They have no owner and no mutation path; operator assignments come in I07A. No actor operation on the seven routes is withheld beyond the matrix above.
- **Legacy sessions:** service-only. No actor adopts them through a name match, and their association is unchanged.
- **`GET /v1/me`:** `Cache-Control: no-store`. It returns kind, subject, operator, scopes, `token_id` (non-secret) and expiry. `site_ids` is always empty. Associations are only the operator's own verified sessions; catalog membership is never listed.

### Contracts

- **Runtime `contracts/openapi.yaml` (additive):**
  - new path `GET /v1/me` and schemas `MeResponse` and `SessionAssociation`;
  - new error codes `forbidden` and `auth_unavailable`;
  - 403 (and 503 `auth_unavailable`) responses documented on the seven routes;
  - the security scheme text describes the two alternative credentials.
  - No property was removed, and required lists are unchanged.
- **Proposed contract:** `GET /v1/me` is promoted to `implemented` (stage I02b), and the proposed `MeResponse` now *is* the runtime model. All other proposed routes stay proposed and unregistered.
- **Docs:** `API_CONTRACT.md` has a new "Actor access (I02b)" section, the updated auth rule and error table, and the TLS note.

### Checks run and observed results

| Check | Result |
| --- | --- |
| `COCOON_LLM_MODE=mock python -m pytest -q` | **270 passed, 1 skipped.** The skip is the Windows symlink case, still not exercised on this machine. |
| New `tests/test_auth.py` | 22 passed. Version-2 upgrade, CLI and refusal cases, credential validation, expiry via an injected clock, live revocation, fail-closed token store, the full matrix, `/v1/me` for each principal, two-operator isolation, nested state and training-tool scoping, legacy sessions, and catalog loss after issuance. |
| Updated tests | `test_migrations.py` and `test_sessions.py` now expect schema version 3 (intentional). |
| `export_openapi.py --check` / `export_proposed_contract.py --check` | Both up to date |
| Real processes (temp DB, random service secret, port 8768, local dataset) | CLI issued two operator tokens and one supervisor token (values never printed) and refused an unknown operator. Smoke passed. Operator 1: `/me` 200 with its own sessions and `no-store`; own state 200; own turn 200; operator 2's state and turn 404; session create and telemetry 403. Supervisor: `/me` 200 with no sites or associations; state 403. Service `/me` returns the service principal. **Revoked operator 1 while running → next request 401.** |
| Orderly restart on the same DB | Revoked token still 401; operator 2, supervisor and service still 200; `list` shows 1 revoked and 2 active; 1 incident for operator 1, 0 for operator 2; schema 3. No plaintext token in the DB files; 0 secrets in server logs or CLI output. Temp data, tokens and logs deleted. |

**Not proven:** an abrupt crash, in-flight turn behaviour at revocation (a request that already passed authentication completes), long-lived stream revalidation (I08), and anything to do with site scoping, consent or client integration. The Windows ACL of token files is inherited, not restricted.

**Hard-kill observation from I02a:** still unresolved and documented in `docs/MIGRATIONS.md` → "Recovery notes". It was not reproduced during I02b, which used orderly stops only.

### Next increment

**I02c: consent records and enforcement.**
- Operator-owned, purpose-specific consent grants and revocations (`vitals_processing`, `risk_sharing_supervisor`, `camera_drowsiness`) with notice version and expected version.
- `GET`/`POST /v1/operators/{operator_id}/consents`: the operator token only for writes, and a supervisor gets 403.
- An enforcement hook that later wellbeing and risk projections must call. Missing or revoked consent means unavailable, never low risk.

The command/action ledger and recovery (I02d) stays separate. Supervisor site grants wait for DG-01 or a documented trusted site source.

## I02a: versioned storage and catalog-bound sessions

- **Recorded:** 2026-09-24. Branch `backend`, base `6bcbf88` (I01). Chain verified: `f8afb4d` ← `6bcbf88` = HEAD before this commit, and no later commits existed.
- **Scope:** the first slice of I02 only. **I02 is partly complete.** Not done here: actor tokens, `/v1/me`, consent enforcement, the command/action ledger, queue and recovery redesign.
- **Requirements advanced:** SYS-09 (versioned, atomic migrations; DB-serialised session creation) and SYS-13 (catalog-verified IDs). No form feature becomes verified.
- **Commit:** this handoff is part of `feat(storage): add versioned migrations and catalog-bound sessions`; see `git log -1 -- langgraph-agent/docs/HANDOFF.md`.

### What changed at runtime

- **Migrations.**
  - cocoon.db now has a `schema_migrations` ledger at **version 2**.
  - A pre-I02a database whose layout exactly matches the v1 baseline is *adopted* as v1 without changes, then v2 adds catalog snapshot tables and nullable session binding columns.
  - Each step and its ledger row commit together in one `BEGIN IMMEDIATE` transaction; no `executescript`.
  - Unknown layouts, newer versions, ledger gaps and locks fail clearly and change nothing. A migration error stops startup.
  - Details and the backup/restore procedure are in `docs/MIGRATIONS.md`, with the new `scripts/backup_db.py` (SQLite Online Backup API).
- **Catalog.**
  - `DATASET_ROOT` (default `../Cocoon_Dataset_v1`, resolved from `langgraph-agent/`) and `DATASET_MANIFEST_SHA256` (pinned full digest).
  - Startup verifies the manifest bytes, then `machines.csv` and `operators.csv` against the manifest digests, parses those same bytes strictly, and keeps an immutable snapshot. The snapshot is recorded append-only in `catalog_versions*`.
  - If the catalog fails, the process still runs, but `/readyz` returns 503 (`catalog: false`, `catalog_issue`), new sessions get 503 `catalog_unavailable`, and existing sessions keep working. There is no fallback to arbitrary IDs.
- **Sessions.** `POST /v1/sessions` first compares an existing `client_session_key` with its stored association (200 or 409). For a new key:
  - an unknown machine gets 422 `unknown_machine`, checked first; if both IDs are unknown, the details name both fields;
  - an unknown operator gets 422 `unknown_operator`;
  - otherwise it creates the session, storing `dataset_manifest_sha256` and `binding_status: catalog_verified`.
- **Site/shift.**
  - `site_id`/`shift_id` are optional and must be sent together. They must match a trusted server-controlled binding (`SESSION_BINDINGS_PATH`, schema `cocoon.session-bindings.v1`); otherwise the answer is 422 `validation_error`.
  - Without them, `context_status` is `unavailable` and site/shift are null. No binding file is configured by default, and the dataset has no site/shift data (DG-01).
  - A retry can never change a stored binding, and a DB trigger makes stored associations immutable.
- **Legacy sessions.** Pre-upgrade rows keep their IDs, history and saved results, with `binding_status: legacy_unverified`, `context_status: legacy_unverified` and a NULL manifest. No current hash is attached retroactively. Retrieval under the old free-text IDs still works, and turns and state still work. To move to verified IDs, create a new session with a new `client_session_key`.
- **API (runtime, additive only).**
  - `SessionCreateRequest` gains optional `site_id`/`shift_id`.
  - `Session` gains `dataset_manifest_sha256`, `binding_status`, `site_id`, `shift_id`, `context_status` and `context_source`.
  - `ReadyResponse` gains `catalog`, `catalog_version`, `catalog_issue` and `schema_version`.
  - The error codes `unknown_machine`, `unknown_operator` and `catalog_unavailable` are added.
  - Required lists are unchanged and no property was removed (checked against the previous `contracts/openapi.yaml`).

### Catalog reference (full digests)

| Record | SHA-256 | Bytes covered |
| --- | --- | --- |
| Pinned manifest digest (runtime) | `5d7de31c1856daf4179110a653d175891a102a53263356843792dd383f40e42d` | `Cocoon_Dataset_v1/data/generated/manifest.json` |
| Package fingerprint (documentation) | `bdd55830094dcb413daa1ee8858f4240d8a57b225d63cc27b6288d311bd2b3d1` | `Cocoon_Dataset_v1/CHECKSUMS.sha256` |

Both were recomputed in I02a with the I01 procedure and are identical to I01; `sha256sum -c` passes for 33/33 files. **Status: provisional local development snapshot.** The dataset is still untracked, **not reviewed by the data owner**, and it does not approve any missing safety input.

Development catalog IDs used by the smoke test: machine `EXC_DEMO_001`, operator `OP_DEMO_1_1` (default), plus rejection checks with `cat-320-demo` and `smoke-operator`. The restart check used `LDR_DEMO_001` + `OP_DEMO_1_1`.

### Changed paths

| Path | Change |
| --- | --- |
| `cocoon_agent/migrations.py`, `catalog.py`, `backup.py` | New |
| `cocoon_agent/store.py` | Uses migrations; atomic retrieve-or-admit session creation; catalog snapshot registration; binding fields |
| `cocoon_agent/service.py`, `api/app.py`, `api/schemas.py`, `config.py` | Admission rules, lifecycle catalog loading, readiness, error details, settings |
| `cocoon_agent/contract/common.py`, `identity.py`, `spec.py` | Moved now-implemented session behaviour out of "target"; the proposed spec was regenerated |
| `scripts/smoke.py`, `simulate_telemetry.py`, `chat_cli.py`, `reset_db.py`, new `backup_db.py` | Catalog IDs by default; smoke checks the 422s, the retry and the binding fields |
| `tests/conftest.py`, `test_api.py`, `test_resilience.py`, `test_contract.py` | Catalog IDs instead of `op`/`m`/`cat-320-demo`, with the same behavioural purpose; new example registered |
| `tests/test_migrations.py`, `test_catalog.py`, `test_sessions.py`, `tests/fixtures/**` | New tests and a tiny committed synthetic catalog (5 canonical asset IDs, operators `OP_TEST_1..3`, labelled `synthetic_test_fixture`), a synthetic binding file, the verbatim v1 baseline schema, and `.gitattributes` (`-text`) so the fixture bytes and hashes stay stable |
| `.env.example`, `README.md`, `docs/MIGRATIONS.md` (new), `docs/FEATURE_MATRIX.md`, `docs/DATA_GAPS.md`, `CLAUDE.md` | Settings, commands, status |
| `contracts/openapi.yaml`, `contracts/examples/*`, `contracts/proposed/**`, `API_CONTRACT.md` | Regenerated runtime and proposed specs; examples use catalog IDs; new `error_unknown_machine.response.json`; client migration note |

Unchanged: `livekit-voice/**`, `Cocoon_Dataset_v1/` (read-only), dependencies and lock files, graph/brain/rules, telemetry/events/delivery behaviour, streaming (still absent), `/v1/me` (still absent).

### Checks run and observed results

| Check | Result |
| --- | --- |
| `COCOON_LLM_MODE=mock python -m pytest -q` | **248 passed, 1 skipped.** The skipped case is the symlink-escape test: this Windows account cannot create symlinks. The check is implemented but not exercised here. |
| Pre-existing test files only (api, contract, live-brain offline, mock brain, resilience, proposed contract) | **204 passed**: 203 before, plus the newly registered runtime example. Tests that used `op`/`m` now use catalog IDs; their assertions are unchanged. |
| New tests | `test_migrations.py` 11, `test_catalog.py` 19 + 1 skip, `test_sessions.py` 14 |
| `export_openapi.py --check` / `export_proposed_contract.py --check` | Both up to date after regeneration |
| Local integration | `test_local_development_dataset_matches_the_pinned_reference` passed against the untracked dataset (5 assets with the expected models, 15 operators) |
| Real processes (temporary data dir, random token, port 8767, default `DATASET_ROOT`) | `/readyz` ready, `catalog_version` = pinned digest, `schema_version` 2; `smoke.py`: `SMOKE OK`; new session 201 `catalog_verified`; unknown machine → 422 `unknown_machine`; unknown operator → 422 `unknown_operator` |
| Orderly restart (Ctrl+Break → `Application shutdown complete`, exit code 3 = Windows Ctrl+Break status) | After restart: session retry 200 with an identical association and binding (`state_version` 0 → 1 from the turn); `GET` turn result identical; turn retry identical; DB has **1** session for the key and **1** incident; ledger `[1 applied, 2 applied]`; one catalog version (5 machines, 15 operators) |
| Log hygiene | 0 occurrences of the token and 0 lines containing operator IDs in the server logs. Processes stopped; temporary data and logs deleted; port free. |

A first attempt used `terminate()` (a hard kill on Windows). Reading the DB immediately afterwards gave a transient `disk I/O error`; on re-open the database passed `PRAGMA integrity_check`. That run was discarded, and the reported result is the graceful-stop run.

**Not proven:** interrupted-turn recovery, crash in the middle of a migration beyond SQLite's atomic commit, multi-process startup, cancellation, streaming, a live model (Vertex or Anthropic), voice or client integration, and site-scoped authorisation. The voice worker's own contract tests were **not run**: its venv doesn't exist and its folder is off-limits. The changes are additive and its models ignore unknown response fields, but that check is still open.

### Handoffs and prerequisites

- **Voice owner (not contacted; note in `API_CONTRACT.md` → "Client migration note (I02a)"):** new sessions need catalog IDs (`EXC_DEMO_001` + a catalog operator, for example `OP_DEMO_1_1`). The worker's current `cat-320-demo`/identity defaults would get 422 on a new session. Existing keys keep working. Before remote integration with this backend, the worker must switch. **Status: open; nobody has confirmed reading it.**
- **Data owner:** review and version the dataset snapshot (commit it or publish a reviewed manifest hash). Until then the pin stays provisional. Site/zone/shift data (DG-01) is still missing, so real sessions report `context_status: unavailable`.
- **Operators of the backend:** back up with `scripts/backup_db.py` before upgrading a database you care about. The upgrade is automatic at startup.

### Next increment

**I02b: scoped actor authentication and `/v1/me`.**
- Server-issued opaque actor tokens (operator, supervisor), stored hashed, with expiry and revocation, plus a local provisioning CLI with no default secret.
- Principal resolution alongside the unchanged service token; `GET /v1/me`.
- Operator actors bound to their own `catalog_verified` sessions only. Supervisor tokens are refused on operator routes.

Consent (I02c) and the command/action ledger with recovery (I02d) stay separate, reviewable slices. Supervisor site scoping has to wait for DG-01 site data or a documented trusted binding source.

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
