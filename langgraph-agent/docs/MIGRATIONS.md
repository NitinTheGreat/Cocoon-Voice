# cocoon.db migrations, backup and the catalog reference

Applies to `langgraph-agent/data/cocoon.db`. The LangGraph checkpoint database (`data/checkpoints.db`) is owned by `langgraph-checkpoint-sqlite` and is not migrated here.

## Version ledger

The authoritative version is the highest row of the `schema_migrations` table (`version`, `name`, `mode`, `applied_at`). Its rows must be exactly 1..N. The code is `cocoon_agent/migrations.py`.

| Version | Name | What it does |
|---|---|---|
| 1 | `baseline_v1` | The v1 tables exactly as the pre-I02a code created them: sessions, turns, tasks, lessons, incidents, training_assignments, alerts (+ `one_active_episode_per_rule`), telemetry_events, announcements, deliveries. |
| 2 | `catalog_bound_sessions` | Adds `catalog_versions`, `catalog_version_machines` and `catalog_version_operators`: append-only snapshots, and triggers refuse UPDATE/DELETE. Adds nullable session columns `dataset_manifest_sha256`, `site_id`, `shift_id` and `context_source`, plus `binding_status` and `context_status` (NOT NULL, default `legacy_unverified`). Adds the `sessions_association_immutable` trigger: after creation, only `state_version` and `last_observed_at` can change. |
| 3 | `actor_tokens` | Adds `principals` (operator or supervisor; an operator principal is bound to one catalog operator and the catalog snapshot it was verified against; immutable) and `actor_tokens` (SHA-256 digest only, never the token; scopes, issue/expiry times; only `revoked_at`/`revoke_reason` can change, once; no deletes). Existing rows are untouched. |

All changes are additive (`CREATE TABLE`, `ALTER TABLE ... ADD COLUMN`, `CREATE TRIGGER`). No table is rebuilt, and no row, ID, index, constraint or reference is rewritten.

## How startup applies migrations

1. `Store.init_schema()` runs `migrate()` on the service connection, which is in autocommit mode (`isolation_level=None`).
2. Each migration and its ledger row run inside one explicit `BEGIN IMMEDIATE ... COMMIT`. `executescript()` is never used, because Python's `executescript()` COMMITs any pending transaction first.
3. SQLite DDL is transactional. If any statement fails, the whole step rolls back and the database stays at its previous version with every row intact.
4. Detection cases:
   - **Empty database:** versions 1, 2 and 3 are applied in order.
   - **Unversioned database whose tables, columns and named indexes exactly equal the v1 baseline:** version 1 is recorded as `adopted_existing` without touching anything, then 2 and 3 are applied. Existing sessions become `binding_status = legacy_unverified` and `context_status = legacy_unverified`, with NULL manifest, site and shift. No provenance is invented and no current catalog hash is attached retroactively.
   - **Any other unversioned layout:** refused (`unknown_layout`). Nothing is modified.
   - **Version newer than supported, or a gap in the ledger:** refused (`newer_schema`, `ledger_inconsistent`).
   - **Database locked:** `BEGIN IMMEDIATE` waits up to `PRAGMA busy_timeout` (5000 ms), then fails with `database_locked`. No step is applied.
5. Every migration error stops startup, and the server does not serve requests. Re-running startup on a current database does nothing.

The single-Uvicorn-worker boundary is unchanged. Migrations are not designed for several processes starting at once, although `BEGIN IMMEDIATE` serialises them.

**Not implemented:** automatic downgrade, automatic pre-migration backup, recovery from a crash in the middle of a statement beyond SQLite's own atomic commit, and migration of `checkpoints.db`.

## Back up before upgrading a database you care about

Copying `cocoon.db` while the server runs is not a backup: in WAL mode, recent commits can sit in `cocoon.db-wal`. Use the SQLite Online Backup API wrapper instead. It is safe while the server runs.

```powershell
cd langgraph-agent
python scripts\backup_db.py                  # -> data\backups\<UTC timestamp>\cocoon.db and checkpoints.db
python scripts\backup_db.py --dest D:\cocoon-backups
```

Each backup file is a complete, self-contained database with no `-wal` or `-shm` companion. The script prints the schema version inside the backup ("unversioned (pre-I02a)" for an old database).

### Restore

1. Stop the backend (Ctrl+C) so no connection is open.
2. Move the current `data/cocoon.db`, `cocoon.db-wal` and `cocoon.db-shm` out of `data/` (keep them until the restore is confirmed). Do the same for the `checkpoints.db*` files if you restore those too. Restore both files from the same backup folder so conversation checkpoints match the records.
3. Copy the backup's `cocoon.db` (and `checkpoints.db`) into `data/`.
4. Start the backend. Migrations bring an older backup forward. A backup from a *newer* code version is refused (`newer_schema`) rather than being modified.

**If a migration fails:** the database is still at its previous version. Fix the cause (for example, a lock held by another process), then restart. Restore from backup only if the database was damaged by something other than the migration.

## Recovery notes and open observations

- **Orderly restart** (Ctrl+C / Ctrl+Break, lifespan shutdown) is verified: I02a (session, binding and saved turn result) and I02b (token metadata and persistent revocation).
- **Hard-kill observation (I02a, unresolved).** In one I02a check the server was stopped with `Popen.terminate()`, which is a hard kill on Windows. Opening `cocoon.db` immediately afterwards raised a transient `sqlite3.OperationalError: disk I/O error`. Re-opening shortly after worked, and `PRAGMA integrity_check` returned `ok`. The cause has **not** been established; a file handle still held by the dying process is a plausible explanation, but it is unproven. This was **not** a successful crash-recovery test, and it proves nothing about data written during a hard kill. The error was not reproduced during ordinary I02b operation (orderly stops only). Interrupted-turn and abrupt-termination recovery belong to I02d/I08.
- After any abrupt stop: keep the `cocoon.db`, `-wal` and `-shm` files together, start the backend normally (SQLite replays the WAL on open), and run `PRAGMA integrity_check`. If it is not `ok`, restore from a backup as above.


## Catalog reference (provisional)

New sessions are admitted against a verified, read-only machine/operator catalog. Settings, all documented in `.env.example`:

| Setting | Default | Meaning |
|---|---|---|
| `DATASET_ROOT` | `../Cocoon_Dataset_v1` | Dataset folder. Relative paths resolve against `langgraph-agent/`, never the caller's working directory. Absolute paths are allowed. No request can choose a path. |
| `DATASET_MANIFEST_SHA256` | `5d7de31c1856daf4179110a653d175891a102a53263356843792dd383f40e42d` | SHA-256 (lowercase hex) of the exact bytes of `<DATASET_ROOT>/data/generated/manifest.json`. |
| `SESSION_BINDINGS_PATH` | unset | Optional trusted site/shift bindings file (`cocoon.session-bindings.v1`). |

Verification at startup, done once:
1. Hash the manifest bytes and compare them with `DATASET_MANIFEST_SHA256`.
2. Require manifest `schema_version` `1.0`.
3. For `machines.csv` and `operators.csv`, which are fixed file names resolved inside `DATASET_ROOT`, compare the SHA-256 of the file bytes with the manifest's digest.
4. Parse those same bytes: required columns, non-empty and well-formed IDs, no duplicates, and row counts equal to the manifest.

The result is an immutable in-memory snapshot, and its identity rows are recorded in `catalog_versions*`. Changing a file while the server runs has no effect until restart, and at restart a changed file is detected (`file_hash_mismatch`).

Failure behaviour:
- The process still starts.
- `/readyz` returns 503 with `catalog: false` and a sanitized `catalog_issue`.
- New sessions get 503 `catalog_unavailable`.
- Existing sessions, turns, state, telemetry and events keep working.
- There is never a fallback to accepting arbitrary IDs.

| Record (2026-09-24) | Full SHA-256 | Bytes covered |
|---|---|---|
| Pinned manifest digest (used at runtime) | `5d7de31c1856daf4179110a653d175891a102a53263356843792dd383f40e42d` | `Cocoon_Dataset_v1/data/generated/manifest.json` |
| Package fingerprint (documentation only) | `bdd55830094dcb413daa1ee8858f4240d8a57b225d63cc27b6288d311bd2b3d1` | `Cocoon_Dataset_v1/CHECKSUMS.sha256`, which lists all 33 package files |

This is a **provisional local development snapshot**. It passes checksum validation, but `Cocoon_Dataset_v1/` is untracked and **the data owner has not reviewed it**. Pinning it does not approve the dataset's missing safety inputs or establish production provenance. When the data owner publishes a reviewed snapshot, update `DATASET_MANIFEST_SHA256`, both in `cocoon_agent/config.py` (`PINNED_DEV_MANIFEST_SHA256`) and in `.env.example`. Existing sessions stay pinned to the snapshot they were created under.

Catalog membership proves an identifier exists. It is not authorisation, a licence, a current assignment or permission for an operator to use a particular machine. The dataset has no authoritative operator↔machine assignment, so none is enforced.

## Session binding rules

| Case | Result |
|---|---|
| New key, known machine and operator | 201, `binding_status: catalog_verified`, `dataset_manifest_sha256` = active snapshot. `context_status: unavailable` with null site/shift unless a trusted binding matched. |
| New key, unknown machine (checked first) | 422 `unknown_machine`. If the operator is unknown too, the details name both fields. Nothing is written. |
| New key, unknown operator | 422 `unknown_operator`. Nothing is written. |
| New key, `site_id`/`shift_id` supplied | Both are required and must match a `SESSION_BINDINGS_PATH` record for the same operator and machine → `context_status: trusted_binding`. Otherwise 422 `validation_error`. |
| Existing key, same association | 200, the stored session. Checked **before** catalog rules, so pre-upgrade sessions with free-text IDs stay retrievable. |
| Existing key, changed room/participant/operator/machine, or a supplied site/shift that differs from the stored value (including stored null) | 409 `session_conflict`. The stored row is unchanged. Omitting site/shift on a retry asserts nothing. |
| No verified catalog loaded | Existing key: 200. New key: 503 `catalog_unavailable`. |

Legacy (pre-upgrade) sessions keep their IDs, history and saved turn results. To move to catalog-verified IDs, create a **new** session: use a new `client_session_key` with a catalog machine and operator. The old session stays readable under its own ID. There is no alias from `cat-320-demo` to `EXC_DEMO_001`, and no rebinding.

The site/shift data gap (DG-01) remains open. No site, zone or shift is derived from dataset rows, dates or models. The committed test binding (`tests/fixtures/session_bindings.json`) is synthetic and does not fill that gap. Site-scoped authorisation is **not** implemented.
