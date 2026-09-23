# Insights: onboarding, history and analytics (PostgreSQL)

This add-on lives in `cocoon_agent/insights/`. It does not modify the existing backend: `python -m cocoon_agent`
behaves exactly as before. `python -m cocoon_agent.insights` serves the same app plus the insights features.

## What it does

1. **Onboarding.** The app shows the 5 catalog machines (`GET /v1/insights/machines`). The employee picks one and
   enters their employee ID, and `POST /v1/insights/users` stores the profile. Posting again changes the machine.
2. **History.** Every session and every question is recorded in PostgreSQL. This covers voice turns through the
   worker and text turns: what was asked, what the assistant said, the backend actions and the timestamps.
3. **Analytics.** `GET /v1/insights/users/{employee_id}/analytics` runs a LangChain workflow (stats and topics in
   parallel, then a summary) and returns chart-ready data. Each report is also saved in `insights_reports`.
   `/insights/dashboard` renders the charts in a browser.

**Employee ID.** In this dataset, an employee ID is a catalog operator ID (`OP_DEMO_1_1` … `OP_DEMO_5_3`).
Voice sessions carry the same ID, so history links to the profile automatically.
`INSIGHTS_REQUIRE_CATALOG_EMPLOYEE=false` accepts any ID matching `[A-Za-z0-9_.-]{2,64}`. The app must then send
that same ID as the participant's `operator_id` for voice history to link, and new backend sessions still need a
catalog operator. When a real HR roster exists, add a mapping from employee to operator.

## How capture works (no handler changes)

A pure ASGI middleware observes three existing routes, only when they succeed:

| Route | Recorded as |
|---|---|
| `POST /v1/sessions` | `insights_sessions` (session ID, operator = employee, machine, room, participant) |
| `POST /v1/sessions/{id}/turns` | `insights_interactions` (question, reply, status, action types, LLM mode) |
| `GET /v1/sessions/{id}/turns/{turn_id}` (a polled 202) | updates the same row when it completes |

- **Timing.** Responses pass through unchanged. Rows are written in a background task after the response is sent.
- **Failures.** A PostgreSQL failure is logged (`insights capture failed`) and never affects a voice turn.
- **Retries.** Retries of the same `turn_id` update one row.
- **Older sessions.** A session created before insights was running is looked up in the core SQLite store.

## Start it

From `langgraph-agent/`, with the venv active:

```powershell
uv pip install -r requirements-insights.txt   # psycopg 3 + pool (or: pip install -r requirements-insights.txt)
# Set POSTGRES_PASSWORD in langgraph-agent/.env yourself (never commit it). Defaults: postgres@127.0.0.1:5432.
python scripts/insights_db_setup.py            # creates database cocoon_insights and the insights_* tables
python -m cocoon_agent.insights                # instead of `python -m cocoon_agent`; same host, port, .env
```

Startup logs `insights ready: PostgreSQL postgres@127.0.0.1:5432/cocoon_insights (schema ok)`. The server also
creates the tables itself if they are missing.

If PostgreSQL is down or the password is wrong, the core API still serves voice turns, and every
`/v1/insights/*` route answers 503 (`internal_error`, retryable, message "the insights database (PostgreSQL) is not connected"). The log shows the reason (`insights NOT available ...`).

## Settings (`langgraph-agent/.env`)

| Variable | Default | Meaning |
|---|---|---|
| `INSIGHTS_ENABLED` | `true` | `false` serves the plain backend even under the insights entrypoint |
| `INSIGHTS_DATABASE_URL` | unset | Full `postgresql://...` URL; overrides the `POSTGRES_*` values |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_DB` | `127.0.0.1` / `5432` / `cocoon_insights` | Target database |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | `postgres` / empty | Credentials (the setup script prompts, hidden, if the password is empty) |
| `POSTGRES_SSLMODE` | `prefer` | libpq sslmode |
| `INSIGHTS_REQUIRE_CATALOG_EMPLOYEE` | `true` | Employee IDs must be catalog operator IDs |
| `COCOON_LLM_MODE` | (core setting) | `mock`: template summary, no model call. `live`: Gemini on Vertex (same ADC settings); a failure returns `summary.status: unavailable`, never mock text |

## API

All routes use the backend's error envelope. The Bearer credential rules:
- The **service token** can access every employee.
- An **operator token** (`scripts/actor_tokens.py`) can access only its own employee ID; anything else gets 404.
- `GET /v1/insights/machines` and `/insights/dashboard` are public.

| Method and path | Purpose |
|---|---|
| `GET /v1/insights/machines` | The 5 catalog machines: `machine_id`, `model`, `category` |
| `POST /v1/insights/users` | Body `{"employee_id": "OP_DEMO_1_1", "machine_id": "EXC_DEMO_001", "display_name": "optional"}`. Returns 201 when created, 200 when updated; 422 `unknown_machine` / `unknown_operator` (employee ID not in the catalog) |
| `GET /v1/insights/users/{employee_id}` | Profile (404 if not onboarded) |
| `GET /v1/insights/users/{employee_id}/sessions?limit=50` | Sessions, newest first, with question counts |
| `GET /v1/insights/users/{employee_id}/interactions?limit=100&session_id=` | Questions and replies, newest first |
| `GET /v1/insights/users/{employee_id}/analytics` | Report: `stats` (totals, `questions_per_day` for 14 days, `questions_by_hour_utc`, `actions`, `top_questions`), `topics.distribution`, `summary` (`text`, `recommendations`, `source`, `status`), `recent_questions`, `report_id` |
| `GET /insights/dashboard` | HTML charts. Enter the employee ID and a token; the token stays in the page and is sent only to this server |

PowerShell example (service token from `.env`):

```powershell
$B = "http://127.0.0.1:8010"; $H = @{ Authorization = "Bearer <COCOON_SERVICE_TOKEN>" }
Invoke-RestMethod "$B/v1/insights/machines"
Invoke-RestMethod -Method Post "$B/v1/insights/users" -Headers $H -ContentType "application/json" `
  -Body '{"employee_id":"OP_DEMO_1_1","machine_id":"EXC_DEMO_001"}'
Invoke-RestMethod "$B/v1/insights/users/OP_DEMO_1_1/analytics" -Headers $H
```

## Android integration notes

- **Onboarding:** `GET /v1/insights/machines`, then `POST /v1/insights/users`.
- **Voice sessions:** join the LiveKit room with participant attributes `operator_id=<employee_id>` and
  `machine_id=<selected machine>`. The worker already reads both, so the backend session and the insights
  history carry the right IDs.
- **Credentials:** never embed the service token in the app. Issue an operator token for the employee
  (`python scripts/actor_tokens.py ...`) and let a trusted server hand it to the app.
- **Charts:** render them from the analytics JSON; the fields are chart-ready.

## Tables

- **`insights_users`:** one row per employee (profile and machine).
- **`insights_sessions`:** one row per backend session. There is no foreign key to users, so a session that
  starts before onboarding keeps its history.
- **`insights_interactions`:** one row per `(session_id, turn_id)`.
- **`insights_reports`:** every generated analytics report (JSONB).

## Limits

- **Capture is best effort:** a write that fails while PostgreSQL is down is not retried.
- **Scope:** only turns that go through this server process are recorded.
- **Topics:** assigned from the backend action type first, then by keyword. They are a coarse grouping, not a
  model classification. Only the summary uses the model, and only in live mode.
- **Deployment:** runs with one Uvicorn worker, like the core backend.
