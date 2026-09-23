# Cocoon Insights: onboarding, question history and analytics (PostgreSQL)

**Status (2026-09-24):**
- Implemented and verified against a remote PostgreSQL database in mock LLM mode.
- The live-mode summary (Gemini on Vertex AI) is implemented but not yet verified.

**Owner area:** `langgraph-agent/cocoon_agent/insights/`. This add-on does not change the existing backend.

---

## 1. What this feature is

Cocoon's voice assistant answers operators' questions. Insights adds three things on top of it:

1. **Onboarding.** On first use, the app shows the five CAT machines from the dataset. The operator picks the
   one they run and enters their employee ID. The backend stores this profile.
2. **History.** Every session and every question the employee asks is recorded in PostgreSQL, together with the
   assistant's reply, the backend actions it triggered and the timestamps.
3. **Analytics.** On request, a small LangChain workflow turns that history into chart-ready data:
   - questions per day and per hour;
   - topics and backend actions;
   - a short summary with recommendations.

   The Android app draws the charts. A browser dashboard exists for quick checks.

### Design rules

| Rule | How it is met |
|---|---|
| **Existing work stays untouched** | New code lives only in `cocoon_agent/insights/`. The core app factory, routes, SQLite store and API contract are unchanged. `python -m cocoon_agent` behaves exactly as before. |
| **Voice turns are never slowed or broken** | Recording happens after the response is sent, in a background task. A PostgreSQL failure is logged and dropped. |
| **Safe when the database is down** | The core API keeps serving voice turns. Only `/v1/insights/*` answers a retryable 503. |
| **One identity model** | The employee ID is the catalog operator ID that voice sessions already carry, so history links without any change to the voice worker. |
| **No invented data** | Mock mode uses a deterministic template. Live mode uses the model; if the model fails, the summary is marked unavailable and is never replaced by mock text. |

---

## 2. Architecture

```mermaid
flowchart LR
    subgraph Client
        A[Android app] -- onboarding / analytics --> B
        A -- joins room, attributes operator_id + machine_id --> LK[(LiveKit Cloud)]
    end
    LK --> W[Voice worker<br/>livekit-voice]
    W -- /v1/sessions, /v1/sessions/{id}/turns --> B
    subgraph B[Backend: python -m cocoon_agent.insights]
        M[InsightsCaptureMiddleware] --> C[Core app<br/>cocoon_agent.api.app, unchanged]
        R[/v1/insights/* router/] --> WF[LangChain analytics workflow]
    end
    C --> S[(SQLite cocoon.db<br/>core state, unchanged)]
    M -. after response .-> P[(PostgreSQL<br/>insights_* tables)]
    R --> P
    WF -. live mode only .-> V[Gemini on Vertex AI]
```

- **Core app.** `cocoon_agent.insights.app.create_app()` builds the normal backend with the existing
  `create_app()`, then calls `install()`. `install()` adds three things:
  - the `/v1/insights/*` router;
  - the capture middleware;
  - a lifespan wrapper that opens the PostgreSQL pool after the core startup (SQLite, catalog, graph).
- **Capture middleware.** A pure ASGI middleware. It observes three existing routes and never changes a
  request or response.
- **Data flow.** The voice worker needs no change: it keeps calling the same seven JSON routes. The middleware
  sees those calls and records them.

### Files

| File | Purpose |
|---|---|
| `cocoon_agent/insights/__main__.py` | Entrypoint `python -m cocoon_agent.insights`. Uses the same host, port, `.env` and single Uvicorn worker as the core. |
| `cocoon_agent/insights/app.py` | `create_app()` (core + insights) and `install(app)`. |
| `cocoon_agent/insights/settings.py` | `InsightsSettings`: `INSIGHTS_*` and `POSTGRES_*` from `langgraph-agent/.env`. |
| `cocoon_agent/insights/db.py` | PostgreSQL schema (created idempotently) and the `InsightsStore` repository. |
| `cocoon_agent/insights/capture.py` | `InsightsCaptureMiddleware`: records sessions and turns. |
| `cocoon_agent/insights/workflow.py` | LangChain analytics workflow: stats, topics and summary. |
| `cocoon_agent/insights/api.py` | `/v1/insights/*` routes and the dashboard route. |
| `cocoon_agent/insights/dashboard.html` | Browser dashboard (Chart.js from jsDelivr). |
| `scripts/insights_db_setup.py` | Creates the database (if allowed) and the tables. Safe to rerun. |
| `requirements-insights.txt` | Extra packages: `psycopg[binary]`, `psycopg-pool`. |

---

## 3. Identity: employee ID = catalog operator ID

- The dataset catalog has 15 operators (`OP_DEMO_1_1` … `OP_DEMO_5_3`) and 5 machines.
- New backend sessions only accept catalog IDs (a rule since I02a).
- Insights therefore uses the catalog operator ID as the employee ID:
  - Onboarding with `employee_id=OP_DEMO_2_1` creates the profile.
  - When that operator speaks to Cat, the voice session carries `operator_id=OP_DEMO_2_1`.
  - That session and its questions appear in the employee's history automatically.

| Machine ID | Model | Category |
|---|---|---|
| `EXC_DEMO_001` | Cat 320 | hydraulic_excavator |
| `DOZ_DEMO_001` | Cat D6 | bulldozer |
| `LDR_DEMO_001` | Cat 950 GC | wheel_loader |
| `TRK_DEMO_001` | Cat 793 | mining_truck |
| `BHL_DEMO_001` | Cat 420 | backhoe_loader |

**Setting `INSIGHTS_REQUIRE_CATALOG_EMPLOYEE=false`:**
- Onboarding then accepts any ID matching `[A-Za-z0-9_.-]{2,64}`.
- Voice history links only if the app sends the same ID as the participant's `operator_id`.
- The core backend still refuses new voice sessions whose operator is not in the catalog.

When a real HR roster exists, add an employee-to-operator mapping.

---

## 4. Data model (PostgreSQL)

The tables are created with `CREATE TABLE IF NOT EXISTS`, both at server startup and by the setup script.
Names start with `insights_`, so they can share a database with other applications.

### `insights_users`: one row per onboarded employee

| Column | Type | Notes |
|---|---|---|
| `employee_id` | TEXT PK | Catalog operator ID by default |
| `machine_id` | TEXT NOT NULL | Chosen machine (catalog ID) |
| `machine_model` | TEXT | For example "Cat 320" (copied from the catalog at onboarding) |
| `machine_category` | TEXT | For example "hydraulic_excavator" |
| `display_name` | TEXT | Optional |
| `onboarded_at` | TIMESTAMPTZ | First onboarding |
| `updated_at` | TIMESTAMPTZ | Last profile change (for example a machine change) |

### `insights_sessions`: one row per backend session

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT PK | Backend session ID (`ses_…`) |
| `employee_id` | TEXT NOT NULL | The session's `operator_id` |
| `machine_id` | TEXT NOT NULL | The session's machine |
| `room_name`, `participant_identity` | TEXT | LiveKit room and participant |
| `started_at` | TIMESTAMPTZ | Backend session `created_at` |
| `last_activity_at` | TIMESTAMPTZ | Updated on every recorded turn |

There is deliberately **no foreign key** to `insights_users`: a voice session may start before onboarding, and
its history must not be lost. The index is `(employee_id, started_at DESC)`.

### `insights_interactions`: one row per question (backend turn)

| Column | Type | Notes |
|---|---|---|
| `session_id`, `turn_id` | TEXT, **PK together** | `turn_id` is the worker's stable `lk-<chat item id>`; retries update the same row |
| `employee_id`, `machine_id` | TEXT NOT NULL | From the session |
| `question` | TEXT NOT NULL | What the employee said (the final transcript, wake phrase removed) |
| `source` | TEXT | `voice` or `text` |
| `status` | TEXT | `completed`, `processing` (a 202 not yet polled) or `failed` |
| `reply` | TEXT | What Cat said (backend `speech`) |
| `action_types` | TEXT[] | Backend actions, for example `{next_task}`, `{incident_logged}` |
| `llm_mode` | TEXT | `mock` or `live` |
| `asked_at`, `completed_at` | TIMESTAMPTZ | From the backend turn result |

The index is `(employee_id, asked_at DESC)`.

### `insights_reports`: every generated analytics report

| Column | Type | Notes |
|---|---|---|
| `report_id` | BIGSERIAL PK | Returned by the analytics route |
| `employee_id` | TEXT | |
| `generated_at` | TIMESTAMPTZ | |
| `llm_mode` | TEXT | Mode of the summary step |
| `report` | JSONB | The full analytics response |

---

## 5. How the flows work

### 5.1 Onboarding

```mermaid
sequenceDiagram
    participant App as Android app
    participant B as Backend (insights)
    participant P as PostgreSQL
    App->>B: GET /v1/insights/machines
    B-->>App: 5 machines (from the verified catalog)
    App->>B: POST /v1/insights/users {employee_id, machine_id}
    B->>B: auth + catalog checks (machine, employee)
    B->>P: UPSERT insights_users
    B-->>App: 201 created (or 200 updated)
```

### 5.2 Recording a voice conversation

```mermaid
sequenceDiagram
    participant W as Voice worker
    participant M as Capture middleware
    participant C as Core app (unchanged)
    participant P as PostgreSQL
    W->>M: POST /v1/sessions {operator_id, machine_id, ...}
    M->>C: passes through
    C-->>W: 201/200 Session
    M--)P: after the response: INSERT insights_sessions
    W->>M: POST /v1/sessions/{id}/turns {turn_id, text}
    M->>C: passes through (LangGraph runs)
    C-->>W: 200 {speech, actions} (or 202 processing)
    M--)P: after the response: UPSERT insights_interactions
    W->>M: GET /v1/sessions/{id}/turns/{turn_id} (poll after a 202)
    C-->>W: 200 completed
    M--)P: UPDATE the same interaction row (status, reply)
```

- **What is captured:**
  - Only 2xx responses are recorded; bodies over 256 KB are skipped.
  - The request and response bytes are passed through unchanged.
- **Sessions that predate insights:**
  - A turn can arrive for a session created before insights was running, for example after a restart.
  - Its owner is then read from the core SQLite store, and the session row is created on the fly.
- **Failures:**
  - Any capture error is logged as `insights capture failed (<kind>): <ExceptionName>` and dropped.
  - Capture is best effort; a failed write is not retried.

### 5.3 Analytics workflow (LangChain)

```mermaid
flowchart LR
    I["input: employee, sessions, interactions, now"] --> P{{RunnableParallel}}
    P --> S[compute_stats]
    P --> T[classify_topics]
    P --> E[employee / interactions passthrough]
    S --> A[assemble]
    T --> A
    E --> A
    A --> Q{COCOON_LLM_MODE}
    Q -- mock --> MS[mock_summary<br/>deterministic template]
    Q -- live --> LS[prompt → Gemini on Vertex AI → StrOutputParser]
    MS --> R[report]
    LS --> R
    R --> DB[(insights_reports)]
```

- **`compute_stats`** (pure Python):
  - totals: sessions, questions, answered, not confirmed, days active, average questions per session;
  - `questions_per_day` for the last 14 days and `questions_by_hour_utc` for hours 0–23;
  - backend action counts and the top 5 repeated questions.
- **`classify_topics`** (deterministic): a question gets a topic from its backend action first, then from
  keywords. The first match wins.

  | Topic | From backend action | Or keywords (examples) |
  |---|---|---|
  | Tasks & schedule | `next_task`, `pending_cancelled` | task, next, schedule, assign, shift |
  | Incidents | `incident_logged`, `information_requested` | incident, accident, damage, injur, spill |
  | Safety & alerts | `alert_explained` | warn, alert, seatbelt, safety, hazard, "why did" |
  | Training | `training_assigned`, `training_status` | training, lesson, course, learn, quiz |
  | Maintenance & inspection | none | inspect, check, oil, hydraulic, tire, track, engine, walkaround |
  | Operating the machine | none | how do, how to, operate, dig, load, bucket, blade |
  | Other | none | anything else |

- **Summary, mock mode:** a template built from the numbers. Its source is labelled
  `mock (deterministic template, no model call)`. Rule-based recommendations cover:
  - a high share of safety-alert questions;
  - incidents that need follow-up;
  - no training questions yet;
  - questions the system never confirmed.
- **Summary, live mode:**
  - It uses `ChatPromptTemplate` → Gemini (`VERTEX_MODEL`, Vertex AI, the same ADC settings as the backend)
    → `StrOutputParser`.
  - The model sees only the stats, the topics and the 30 most recent questions, and is told not to invent facts.
  - On any failure (quota 429, auth, timeout) the summary is `status: "unavailable"` with the reason; the
    charts still render.

---

## 6. API reference

- **Base URL:** the backend, for example `http://127.0.0.1:8010` in the current local setup.
- **Errors:** every error uses the backend envelope
  `{"error": {"code", "message", "retryable", "request_id", "details"}}`.

### Authentication

| Caller | Access |
|---|---|
| Trusted service token (`COCOON_SERVICE_TOKEN`) | Every employee (read and write) |
| Operator actor token (`cct_…`, from `scripts/actor_tokens.py issue --role operator --operator-id OP_DEMO_1_1 --out <file>`) | Only its own `employee_id`. Any other ID gets the same 404, so the ID's existence is not revealed |
| Supervisor token | No insights access (404) |
| No token | `GET /v1/insights/machines` and `/insights/dashboard` only |

**Never embed the service token in the Android app.** Issue a per-employee operator token and deliver it
through a trusted server.

### `GET /v1/insights/machines` (public)

```json
{"machines": [
  {"machine_id": "BHL_DEMO_001", "model": "Cat 420", "category": "backhoe_loader"},
  {"machine_id": "DOZ_DEMO_001", "model": "Cat D6", "category": "bulldozer"},
  {"machine_id": "EXC_DEMO_001", "model": "Cat 320", "category": "hydraulic_excavator"},
  {"machine_id": "LDR_DEMO_001", "model": "Cat 950 GC", "category": "wheel_loader"},
  {"machine_id": "TRK_DEMO_001", "model": "Cat 793", "category": "mining_truck"}]}
```

### `POST /v1/insights/users`: onboard or change machine

Request (unknown fields are rejected):

```json
{"employee_id": "OP_DEMO_1_1", "machine_id": "EXC_DEMO_001", "display_name": "Asha (optional)"}
```

Response: `201` on the first onboarding, `200` when updated:

```json
{"employee_id": "OP_DEMO_1_1", "machine_id": "EXC_DEMO_001", "machine_model": "Cat 320",
 "machine_category": "hydraulic_excavator", "display_name": "Asha (optional)",
 "onboarded_at": "2026-09-24T04:02:28.412+00:00", "updated_at": "2026-09-24T04:02:28.412+00:00", "created": true}
```

| Error | When |
|---|---|
| 422 `unknown_machine` | `machine_id` not in the catalog |
| 422 `unknown_operator` | `employee_id` not a catalog operator (while `INSIGHTS_REQUIRE_CATALOG_EMPLOYEE=true`) |
| 422 `validation_error` | Bad format or an unknown field |
| 401 / 404 | Missing or invalid token / operator token for another employee |
| 503 `internal_error` (retryable) | PostgreSQL not connected or failing ("the insights database (PostgreSQL) is …") |
| 503 `catalog_unavailable` | The backend has no verified catalog loaded |

### `GET /v1/insights/users/{employee_id}`

Returns the profile, in the same shape as above without `created`. Returns 404 if the employee is not onboarded.

### `GET /v1/insights/users/{employee_id}/sessions?limit=50` (limit 1–200)

```json
{"employee_id": "OP_DEMO_1_1", "sessions": [
  {"session_id": "ses_…", "employee_id": "OP_DEMO_1_1", "machine_id": "EXC_DEMO_001",
   "room_name": "cocoon-demo-room", "participant_identity": "op-demo-1-1",
   "started_at": "2026-09-24T04:10:02+00:00", "last_activity_at": "2026-09-24T04:14:40+00:00", "questions": 6}]}
```

### `GET /v1/insights/users/{employee_id}/interactions?limit=100&session_id=` (limit 1–500)

Returns the newest questions first:

```json
{"employee_id": "OP_DEMO_1_1", "interactions": [
  {"session_id": "ses_…", "turn_id": "lk-item_abc", "machine_id": "EXC_DEMO_001",
   "question": "What's my next task?", "source": "voice", "status": "completed",
   "reply": "Your next task is T-101: …", "action_types": ["next_task"], "llm_mode": "mock",
   "asked_at": "2026-09-24T04:10:05+00:00", "completed_at": "2026-09-24T04:10:05+00:00"}]}
```

### `GET /v1/insights/users/{employee_id}/analytics`

Runs the workflow, saves the report and returns it. Returns 404 when the employee has neither a profile nor
any sessions. The values below are illustrative:

```json
{
  "employee_id": "OP_DEMO_1_1", "report_id": 12, "generated_at": "2026-09-24T05:00:00+00:00", "llm_mode": "mock",
  "employee": {"employee_id": "OP_DEMO_1_1", "machine_id": "EXC_DEMO_001", "machine_model": "Cat 320", "...": "..."},
  "stats": {
    "total_sessions": 3, "total_questions": 14, "answered": 13, "not_confirmed": 1, "days_active": 2,
    "avg_questions_per_session": 4.7, "first_seen": "…", "last_seen": "…",
    "questions_per_day": [{"date": "2026-09-11", "count": 0}, "… 14 entries …"],
    "questions_by_hour_utc": [{"hour": 0, "count": 0}, "… 24 entries …"],
    "actions": [{"type": "next_task", "count": 5}, {"type": "alert_explained", "count": 2}],
    "top_questions": [{"question": "whats my next task", "count": 4}]
  },
  "topics": {"top_topic": "tasks", "distribution": [
    {"topic": "tasks", "label": "Tasks & schedule", "count": 6, "share": 0.429},
    {"topic": "safety_alerts", "label": "Safety & alerts", "count": 3, "share": 0.214}]},
  "summary": {"status": "ok", "source": "mock (deterministic template, no model call)",
              "text": "14 questions across 3 session(s) on 2 day(s); most were about tasks & schedule (43%).",
              "recommendations": ["Frequent safety-alert questions: review the seatbelt and alert procedures together."]},
  "recent_questions": [{"question": "…", "reply": "…", "status": "completed", "session_id": "ses_…",
                        "asked_at": "…", "topic": "tasks"}]
}
```

In live mode, `summary.source` is `live (<VERTEX_MODEL>)`. On a model failure it looks like this:

```json
{"status": "unavailable", "source": "live (gemini-3.8-flash)", "reason": "ClientError: 429 …",
 "text": null, "recommendations": []}
```

### `GET /insights/dashboard` (public page)

- Enter an employee ID and a token (service or that operator's token) to see KPIs, the summary, questions per
  day, topics, hour of day, backend actions and recent questions.
- The token stays in the page and is sent only to this server.
- Chart.js is loaded from `cdn.jsdelivr.net`.

### PowerShell example

```powershell
$B = "http://127.0.0.1:8010"; $H = @{ Authorization = "Bearer <token>" }
Invoke-RestMethod "$B/v1/insights/machines"
Invoke-RestMethod -Method Post "$B/v1/insights/users" -Headers $H -ContentType "application/json" `
  -Body '{"employee_id":"OP_DEMO_1_1","machine_id":"EXC_DEMO_001"}'
Invoke-RestMethod "$B/v1/insights/users/OP_DEMO_1_1/interactions?limit=20" -Headers $H
Invoke-RestMethod "$B/v1/insights/users/OP_DEMO_1_1/analytics" -Headers $H
```

---

## 7. Setup

### 7.1 Install the extra packages (once per environment)

From `langgraph-agent/`, with its venv:

```powershell
uv pip install -r requirements-insights.txt      # or: pip install -r requirements-insights.txt
```

### 7.2 Choose a PostgreSQL

**Remote (recommended for the team). Neon:**
1. Sign up at https://console.neon.tech/signup.
2. Create a project at https://console.neon.tech/app/projects.
3. Click **Connect** on the project dashboard.
4. Turn **Connection pooling** off and copy the direct connection string. Its host has no `-pooler` in it.

**Remote. Supabase:**
1. Go to https://supabase.com/dashboard and create a new project.
2. Click **Connect** in the top bar and copy the **Session pooler** string (port 5432).
3. Add `?sslmode=require` to the end.

Don't use Supabase's "Direct connection" (IPv6-only on the free plan) or its "Transaction pooler" (port 6543;
no prepared statements).

**Local:** any PostgreSQL 13 or newer. This machine has PostgreSQL 18 running as a Windows service on port 5432.

### 7.3 Configure `langgraph-agent/.env`

This file is git-ignored; never commit it. `.env.example` has placeholders only.

**Remote (one URL):**

```
INSIGHTS_ENABLED=true
INSIGHTS_DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DBNAME?sslmode=require
INSIGHTS_REQUIRE_CATALOG_EMPLOYEE=true
```

If the password contains `@ : / ? # %`, URL-encode those characters (for example `@` → `%40`), or use the
separate fields instead. With a URL, the setup script only creates tables; it never tries `CREATE DATABASE`.

**Local (separate fields):**

```
INSIGHTS_ENABLED=true
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=cocoon_insights
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<your password>
POSTGRES_SSLMODE=prefer
```

### Settings reference

| Variable | Default | Meaning |
|---|---|---|
| `INSIGHTS_ENABLED` | `true` | `false` serves the plain backend even under the insights entrypoint |
| `INSIGHTS_DATABASE_URL` | unset | Full `postgresql://…` URL; when set, the `POSTGRES_*` values are ignored |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `127.0.0.1` / `5432` | Server |
| `POSTGRES_DB` | `cocoon_insights` | Database (the setup script creates it if missing, local mode only) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | `postgres` / empty | Credentials. If the password is empty, the setup script asks with a hidden prompt |
| `POSTGRES_SSLMODE` | `prefer` | libpq sslmode (`require` for remote) |
| `INSIGHTS_POOL_MAX_SIZE` | `5` | Connection pool size |
| `INSIGHTS_REQUIRE_CATALOG_EMPLOYEE` | `true` | Employee IDs must be catalog operator IDs |
| `COCOON_LLM_MODE` | core setting | `mock`: template summary. `live`: Gemini summary; failures are reported, never mocked |

### 7.4 Create the tables and start

```powershell
cd langgraph-agent
.\.venv\Scripts\python.exe scripts\insights_db_setup.py    # expect: "schema ready on …: insights_interactions, insights_reports, insights_sessions, insights_users"
.\.venv\Scripts\python.exe -m cocoon_agent.insights          # instead of `python -m cocoon_agent`
```

Expected startup log lines:

```
INFO cocoon_agent.api catalog verified … machines=5 operators=15
INFO cocoon_agent.api cocoon backend ready llm_mode=mock …
INFO cocoon_agent.insights insights ready: PostgreSQL INSIGHTS_DATABASE_URL (schema ok)
```

The voice worker needs no change: it points at the same URL and token.

---

## 8. Android integration checklist

1. **Onboarding screen.**
   - Call `GET /v1/insights/machines` and show model + category.
   - Collect the employee ID and call `POST /v1/insights/users`.
   - Map 422 `unknown_operator` to "employee ID not recognised" and 422 `unknown_machine` to "choose a machine".
2. **Returning user.** Call `GET /v1/insights/users/{id}`. On a 404, show onboarding. On a 200, go to the home
   screen.
3. **Voice session.** Join the LiveKit room with participant attributes `operator_id=<employee_id>` and
   `machine_id=<profile.machine_id>`. The worker already reads both. The session then carries the right IDs,
   and its questions show up in the employee's history.
4. **History screen.** Call `.../sessions`, then `.../interactions?session_id=…`.
5. **Analytics screen.** Call `.../analytics` and map the chart-ready fields:

   | Field | Chart |
   |---|---|
   | `stats.questions_per_day` | Bar or line |
   | `topics.distribution` | Donut |
   | `stats.questions_by_hour_utc` | Bar; convert UTC to local time |
   | `stats.actions` | Horizontal bar |
   | `summary.text` / `summary.recommendations` | Text cards. If `summary.status` is `unavailable`, show "Summary unavailable" and keep the charts |

6. **Auth.** Use a per-employee operator token, never the service token.
7. **Errors.** A 503 with `retryable: true` means try again later. Voice keeps working during a 503.

---

## 9. Operations

### Logs

| Log line | Meaning |
|---|---|
| `insights ready: PostgreSQL … (schema ok)` | Pool opened and schema checked at startup |
| `insights NOT available: cannot use PostgreSQL … (<Error>: …); core API unaffected` | Startup could not connect. Insights routes return 503 until you fix it and restart |
| `insights capture failed (session\|turn\|turn_read): <Error>` | One record was not written. The voice turn was unaffected |
| `insights live summary unavailable: <Error>` | Live-mode model failure. The report carries the reason |

### Troubleshooting

| Symptom | Fix |
|---|---|
| `password authentication failed` | Wrong password, or special characters in the URL that aren't encoded |
| `could not translate host name` / timeout | Network or IPv6 problem. On Supabase, use the Session pooler string |
| `SSL connection is required` | Add `?sslmode=require` to the URL, or set `POSTGRES_SSLMODE=require` |
| Errors mentioning `prepared statement` | You're using a transaction-mode pooler. Use Neon's direct string or Supabase's Session pooler |
| `/v1/insights/*` returns 404 | The server was started with `python -m cocoon_agent` instead of `cocoon_agent.insights` |
| A question is missing from the history | Check the log for `insights capture failed`. Only turns served by this process are recorded |
| Startup waits about 10 s before `insights NOT available` | PostgreSQL is unreachable (the pool open timeout) |

### Useful SQL

```sql
-- the latest questions of one employee
SELECT asked_at, question, status, reply FROM insights_interactions
WHERE employee_id = 'OP_DEMO_1_1' ORDER BY asked_at DESC LIMIT 20;

-- questions per employee
SELECT employee_id, count(*) FROM insights_interactions GROUP BY 1 ORDER BY 2 DESC;

-- reset demo data (irreversible)
TRUNCATE insights_interactions, insights_sessions, insights_reports, insights_users;
```

Backups: use `pg_dump` or the provider's built-in backups (Neon and Supabase both have point-in-time
restore on their plans).

---

## 10. Privacy and security notes

- **What is stored.** The operator's questions and the assistant's replies. Treat them as personal operational
  data:
  - restrict database access;
  - use `sslmode=require` for remote databases;
  - never export the tables publicly.
- **Secrets.**
  - Database credentials and tokens live only in `langgraph-agent/.env`, which is git-ignored.
  - The code never logs the connection string: startup logs show `INSIGHTS_DATABASE_URL` or `user@host:port/db`.
- **Access.** Operator tokens only see their own employee. Existence of other employees is not revealed.
- **Scope.** Recorded data covers only what the backend already received. No audio is stored.

---

## 11. Verification record

**2026-09-24, isolated test.** A throwaway PostgreSQL 18 cluster and a backend on a separate port and data
directory, in mock mode. Results:
- the machine list was served;
- onboarding returned 201, and a machine change returned 200;
- unknown machine returned 422 `unknown_machine` and unknown employee 422 `unknown_operator`;
- a call without a token returned 401;
- three voice turns were captured with their replies and actions;
- a retried turn kept a single row;
- the analytics report was saved and the dashboard returned 200;
- history survived a restart;
- with PostgreSQL down, insights routes returned 503 while the core stayed ready and a voice turn completed.

**2026-09-24, remote database.** The setup script created the four tables. The backend started with
`insights ready`; onboarding `OP_DEMO_1_1` → Cat 320 returned 201 and read back; analytics produced report 1.

**Not verified yet:**
- the live-mode (Gemini) summary;
- a long real Playground conversation captured end to end;
- the Android integration.

---

## 12. Limitations and next steps

- **Capture is best effort:** writes are not retried, and there is no backfill from SQLite history.
- **Topics are coarse:** action type first, then keywords. A model-based classifier could replace
  `classify_topics` without changing the API.
- **Deployment:** one Uvicorn worker, as for the core backend.
- **Identity:** the employee ID is tied to catalog operator IDs until a real roster and an employee-to-operator
  mapping exist.
- **No delete or export API yet:** add one before storing real employee data (retention and privacy).
