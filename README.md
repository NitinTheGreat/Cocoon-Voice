# Cocoon-Voice

Cocoon is a proactive voice assistant for construction equipment operators, built by Team Butterfly for a Caterpillar hackathon. This repository contains the voice and backend services. The Android client, Cocoon-App, lives in a separate repository.

> **Current phase: standalone voice** (voice work merged into `main` and `backend` through PR #1). `livekit-voice` runs on its own as "Cat": AssemblyAI STT → Gemini on Vertex AI (ADC) → Cartesia TTS, with a "Hey Cat" wake gate and Krisp input filtering. It is **not connected to `langgraph-agent` yet**. The diagram below shows the planned phase-2 integration, which is pending acceptance of the voice experience. See [livekit-voice/README.md](livekit-voice/README.md) and [livekit-voice/docs/voice-latency-report.md](livekit-voice/docs/voice-latency-report.md).

```
 browser / future Android app                 LiveKit Cloud                       this repo
 ────────────────────────────                ─────────────                ───────────────────────────────────────────
  mic/speaker ──WebRTC──▶  room  ◀──WebRTC──  livekit-voice worker (Developer A, health :8081)
                                              STT ─▶ llm_node ──HTTP JSON /v1──▶ langgraph-agent :8000 (Developer B)
                                              TTS ◀─ speech   ◀──────────────── FastAPI + LangGraph + SQLite
                                              announcement poller ◀─ GET /events (retained, cursor)
                                                                  simulator ─▶ POST /telemetry (simulated)
```

| Folder | Owner | Runs as | Default port |
|---|---|---|---|
| [`livekit-voice/`](livekit-voice/README.md) | Developer A: audio transport, STT and TTS, wake gate, noise handling; phase 2: HTTP backend client and announcement delivery | A long-lived LiveKit Agents worker with explicit dispatch name `cocoon-voice`. It connects out to LiveKit Cloud. Phase 1 uses a standalone Vertex brain (`providers.create_brain()`). | `127.0.0.1:8081` health; phase-0 mock backend on `8010` |
| [`langgraph-agent/`](langgraph-agent/README.md) | Developer B: FastAPI, LangGraph, state, tools, rules, persistence | One Uvicorn process (one worker) | `127.0.0.1:8000` (`/docs`, `/healthz`, `/readyz`) |
| [`contracts/`](contracts/), [`API_CONTRACT.md`](API_CONTRACT.md) | Shared **documentation and fixtures**, not a runtime package | none | none |

Each service folder has its own `.venv`, `requirements.txt`, `requirements-dev.txt`, `.env.example`, README and tests, and each loads its own `.env` from its own folder regardless of the working directory. Neither service imports the other, reads the other's database or needs the other's venv. The only coupling is the versioned HTTP contract. See [API_CONTRACT.md](API_CONTRACT.md) for its rules and for how to make compatible changes.

## Quick start (two terminals, mock LLM, no provider credentials)

Python 3.11 is required. PowerShell is shown; each service README has the Bash equivalents.

```powershell
# terminal 1: backend
cd langgraph-agent
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt; pip install -e . --no-deps
Copy-Item .env.example .env          # COCOON_LLM_MODE=mock
python -m cocoon_agent               # http://127.0.0.1:8000/docs
python scripts\smoke.py              # in another shell: end-to-end HTTP check

# terminal 2: voice worker (needs LiveKit, AssemblyAI and Cartesia keys plus Vertex ADC; see livekit-voice/README.md)
cd livekit-voice
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt; pip install -e . --no-deps
Copy-Item .env.example .env          # set LIVEKIT_*, ASSEMBLYAI_API_KEY, CARTESIA_API_KEY (Vertex uses ADC)
python -m cocoon_voice.agent dev
```

Then follow the [Playground / Agent Console walkthrough](livekit-voice/README.md#testing-in-the-browser).

## Scope and known limits (v1 prototype)

- One operator per room. The worker binds to the first standard participant that joins.
- The telemetry rule and all telemetry are **simulated prototypes**, not validated machine safety logic.
- Turn and announcement handling is idempotent and retry-safe, but **not exactly-once**. See API_CONTRACT.md.
- The backend must run as a single Uvicorn worker because per-session ordering locks live in process.
- Out of scope: MQTT, Redis, Kafka, Celery, vector databases, Kubernetes, custom signalling, LMS, wearable ML and Android UI. The phase-1 voice worker streams LLM text into TTS; the backend `/v1` contract is still nonstreaming. Backend streaming and the operator/supervisor routes are specified as a proposed contract in [`contracts/proposed/`](contracts/proposed/README.md) (I01) and are not implemented yet.

## Future Android client (Cocoon-App, not implemented here)

- The app joins the **same LiveKit Cloud project and room** as a normal participant over WebRTC, using a LiveKit client SDK.
- It must get its LiveKit token from a **server-side token endpoint behind user authentication**, never from an embedded API secret. This repo does not have that endpoint yet. Add it either as a small authenticated route in `langgraph-agent` (for example `POST /v1/livekit-token`) or as a separate auth service.
- The endpoint should do three things:
  - Authenticate the operator.
  - Mint a token for that identity and room.
  - Include `RoomConfiguration.agents=[RoomAgentDispatch(agent_name="cocoon-voice", metadata={"operator_id", "machine_id"})]`, so the worker receives trusted operator and machine IDs in its job metadata.

  `livekit-voice/scripts/dispatch.py token` shows the token shape. It is a dev-only helper.
- Operator authentication and authorization for the `/v1` API itself are also future work. Today `/v1` trusts one shared service token held by the voice worker.
