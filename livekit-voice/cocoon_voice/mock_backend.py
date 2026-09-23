"""MOCK Cocoon backend for developing the voice worker without langgraph-agent.

    python -m cocoon_voice.mock_backend            # 127.0.0.1:8010 by default
    then set COCOON_BACKEND_URL=http://127.0.0.1:8010 in livekit-voice/.env

In-memory, deterministic, and clearly labelled mock (llm_mode="mock", speech starts
with "Mock backend"). Implements the v1 calls the worker uses: sessions, turns
(incl. 202 + polling when the utterance contains "slow"), state, telemetry with a
toy seatbelt rule, events and delivery. Responses validate against contracts/openapi.yaml.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import get_settings

SLOW_SECONDS = float(os.environ.get("COCOON_MOCK_SLOW_SECONDS", "3"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


class MockState:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.by_key: dict[str, str] = {}
        self.turns: dict[tuple[str, str], dict] = {}
        self.telemetry: dict[tuple[str, str], tuple[str, dict]] = {}
        self.events: dict[str, list[dict]] = {}
        self.alert_active: dict[str, str | None] = {}


def create_mock_app(token: str | None = None) -> FastAPI:
    if token is None:
        configured = get_settings().service_token
        if configured is None:
            raise SystemExit("COCOON_SERVICE_TOKEN is not set (future-only; needed only for the mock backend tools)")
        token = configured.get_secret_value()
    app = FastAPI(title="Cocoon MOCK backend (livekit-voice)", version="1.0.0-mock")
    st = MockState()
    app.state.mock = st

    def error(status: int, code: str, message: str, request: Request, retryable: bool = False) -> JSONResponse:
        rid = request.headers.get("X-Request-ID") or "mock_" + uuid.uuid4().hex[:8]
        return JSONResponse({"error": {"code": code, "message": message, "retryable": retryable, "request_id": rid}},
                            status_code=status, headers={"X-Request-ID": rid})

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.url.path.startswith("/v1/"):
            got = request.headers.get("Authorization", "")
            if not secrets.compare_digest(got.encode(), f"Bearer {token}".encode()):
                return error(401, "unauthorized", "missing or invalid service bearer token", request)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        return {"status": "ready", "llm_mode": "mock", "database": True, "checkpointer": True, "version": "mock"}

    @app.post("/v1/sessions")
    async def create_session(request: Request):
        body = await request.json()
        required = ("client_session_key", "room_name", "participant_identity", "operator_id", "machine_id")
        if set(body) != set(required) or not all(isinstance(body[k], str) and body[k] for k in required):
            return error(422, "validation_error", "request did not match the v1 contract", request)
        sid = st.by_key.get(body["client_session_key"])
        if sid:
            return JSONResponse(st.sessions[sid], status_code=200)
        sid = "ses_mock" + uuid.uuid4().hex[:12]
        st.by_key[body["client_session_key"]] = sid
        st.sessions[sid] = {"session_id": sid, **body, "state_version": 0, "created_at": _now()}
        st.events[sid] = []
        st.alert_active[sid] = None
        return JSONResponse(st.sessions[sid], status_code=201)

    def _turn_result(sid: str, tid: str, status: str, created_at: str, speech: str | None = None) -> dict:
        return {
            "session_id": sid, "turn_id": tid, "status": status, "speech": speech, "actions": [],
            "state_version": st.sessions[sid]["state_version"] if status == "completed" else None,
            "llm_mode": "mock", "error": None,
            "retry_after_ms": 500 if status == "processing" else None,
            "poll_url": f"/v1/sessions/{sid}/turns/{tid}" if status == "processing" else None,
            "created_at": created_at, "completed_at": _now() if status == "completed" else None,
        }

    async def _finish(sid: str, tid: str, text: str, delay: float) -> None:
        await asyncio.sleep(delay)
        st.sessions[sid]["state_version"] += 1
        rec = st.turns[(sid, tid)]
        rec["result"] = _turn_result(sid, tid, "completed", rec["created_at"], f"Mock backend here. You said: {text}")

    @app.post("/v1/sessions/{sid}/turns")
    async def submit_turn(sid: str, request: Request):
        if sid not in st.sessions:
            return error(404, "not_found", "session not found", request)
        body = await request.json()
        if (set(body) != {"turn_id", "text", "source"} or body.get("source") not in ("voice", "text")
                or not str(body.get("text", "")).strip() or not body.get("turn_id")):
            return error(422, "validation_error", "request did not match the v1 contract", request)
        key = (sid, body["turn_id"])
        digest = _hash({"text": body["text"], "source": body["source"]})
        rec = st.turns.get(key)
        if rec is None:
            rec = st.turns[key] = {"hash": digest, "created_at": _now(), "result": None}
            slow = "slow" in body["text"].lower()
            task = asyncio.create_task(_finish(sid, body["turn_id"], body["text"], SLOW_SECONDS if slow else 0))
            if not slow:
                await task
        elif rec["hash"] != digest:
            return error(409, "idempotency_conflict", "turn_id was already used with a different text or source",
                         request)
        if rec["result"] is None:
            return JSONResponse(_turn_result(sid, body["turn_id"], "processing", rec["created_at"]), status_code=202,
                                headers={"Retry-After": "1", "Location": f"/v1/sessions/{sid}/turns/{body['turn_id']}"})
        return JSONResponse(rec["result"])

    @app.get("/v1/sessions/{sid}/turns/{tid}")
    async def get_turn(sid: str, tid: str, request: Request):
        if sid not in st.sessions:
            return error(404, "not_found", "session not found", request)
        rec = st.turns.get((sid, tid))
        if rec is None:
            return error(404, "not_found", "turn not found", request)
        return rec["result"] or _turn_result(sid, tid, "processing", rec["created_at"])

    @app.get("/v1/sessions/{sid}/state")
    async def state(sid: str, request: Request):
        if sid not in st.sessions:
            return error(404, "not_found", "session not found", request)
        return {"session_id": sid, "state_version": st.sessions[sid]["state_version"], "llm_mode": "mock",
                "tasks": [], "incidents": [], "training_assignments": [], "available_lessons": [],
                "active_alerts": [], "latest_alert": None, "pending_question": None}

    @app.post("/v1/sessions/{sid}/telemetry")
    async def telemetry(sid: str, request: Request):
        if sid not in st.sessions:
            return error(404, "not_found", "session not found", request)
        body = await request.json()
        if body.get("simulated") is not True or "readings" not in body or "event_id" not in body:
            return error(422, "validation_error", "request did not match the v1 contract", request)
        digest = _hash({k: v for k, v in body.items() if k != "event_id"})
        prior = st.telemetry.get((sid, body["event_id"]))
        if prior:
            if prior[0] != digest:
                return error(409, "idempotency_conflict", "event_id reused with different payload", request)
            return {**prior[1], "duplicate": True}
        r = body["readings"]
        condition = bool(r.get("engine_on")) and not r.get("seatbelt_fastened")
        opened, cleared, announced = [], [], []
        active = st.alert_active[sid]
        if condition and not active:
            active = st.alert_active[sid] = "ALR-mock" + uuid.uuid4().hex[:6]
            opened.append(active)
            announced.append(_announce(sid, active, "alert_started", "high",
                                       "Mock backend warning: seatbelt unfastened with the engine running."))
        elif not condition and active:
            cleared.append(active)
            st.alert_active[sid] = None
            announced.append(_announce(sid, active, "alert_cleared", "low", "Mock backend: seatbelt warning cleared."))
        if announced:
            st.sessions[sid]["state_version"] += 1
        result = {"session_id": sid, "event_id": body["event_id"], "duplicate": False, "stale": False,
                  "alerts_opened": opened, "alerts_cleared": cleared, "announcements_created": announced,
                  "active_alerts": [], "state_version": st.sessions[sid]["state_version"]}
        st.telemetry[(sid, body["event_id"])] = (digest, result)
        return result

    def _announce(sid: str, alert_id: str, kind: str, priority: str, speech: str) -> str:
        events = st.events[sid]
        now = datetime.now(timezone.utc)
        event_id = f"ann_{alert_id}_{'start' if kind == 'alert_started' else 'clear'}"
        events.append({"event_id": event_id, "sequence": len(events) + 1, "type": kind, "priority": priority,
                       "speech": speech, "alert_id": alert_id, "created_at": now.isoformat().replace("+00:00", "Z"),
                       "expires_at": (now + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                       "deliveries": []})
        return event_id

    @app.get("/v1/sessions/{sid}/events")
    async def events(sid: str, request: Request, after: int = 0, limit: int = 20):
        if sid not in st.sessions:
            return error(404, "not_found", "session not found", request)
        newer = [e for e in st.events[sid] if e["sequence"] > after]
        page = newer[:limit]
        return {"session_id": sid, "events": page, "next_cursor": page[-1]["sequence"] if page else after,
                "has_more": len(newer) > limit}

    @app.post("/v1/sessions/{sid}/events/{event_id}/delivery")
    async def delivery(sid: str, event_id: str, request: Request):
        event = next((e for e in st.events.get(sid, []) if e["event_id"] == event_id), None)
        if event is None:
            return error(404, "not_found", "announcement not found", request)
        body = await request.json()
        if body.get("status") not in ("played", "interrupted", "failed", "expired") or not body.get("consumer_id"):
            return error(422, "validation_error", "request did not match the v1 contract", request)
        record = {"event_id": event_id, "consumer_id": body["consumer_id"], "status": body["status"],
                  "detail": body.get("detail"), "recorded_at": _now()}
        event["deliveries"] = [d for d in event["deliveries"] if d["consumer_id"] != body["consumer_id"]] + [record]
        return record

    return app


def main() -> None:
    import uvicorn

    port = int(os.environ.get("COCOON_MOCK_BACKEND_PORT", "8010"))
    print(f"MOCK backend (not langgraph-agent) on http://127.0.0.1:{port}")
    uvicorn.run(create_mock_app(), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
