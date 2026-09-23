"""Records sessions and turns into PostgreSQL by observing the existing JSON routes (no handler changes).

Pure ASGI middleware. It watches only POST /v1/sessions, POST /v1/sessions/{id}/turns and
GET /v1/sessions/{id}/turns/{turn_id}, and only successful (2xx) responses. The request and response pass
through unchanged, and recording runs as a background task after the response is sent: a slow or failed
PostgreSQL write never delays or breaks a voice turn. Failures are logged and dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

log = logging.getLogger("cocoon_agent.insights.capture")

_SESSION_CREATE = re.compile(r"^/v1/sessions/?$")
_TURN_SUBMIT = re.compile(r"^/v1/sessions/(?P<sid>[^/]+)/turns/?$")
_TURN_READ = re.compile(r"^/v1/sessions/(?P<sid>[^/]+)/turns/(?P<tid>[^/]+)/?$")
_MAX_BODY = 256 * 1024  # contract bodies are small; anything larger is not captured


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _action_types(result: dict[str, Any]) -> list[str]:
    return [str(a.get("type")) for a in result.get("actions") or [] if isinstance(a, dict) and a.get("type")]


class InsightsCaptureMiddleware:
    def __init__(self, app):
        self.app = app
        self._tasks: set[asyncio.Task] = set()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method, path = scope["method"], scope["path"]
        kind = match = None
        if method == "POST" and _SESSION_CREATE.match(path):
            kind = "session"
        elif method == "POST" and (match := _TURN_SUBMIT.match(path)):
            kind = "turn"
        elif method == "GET" and (match := _TURN_READ.match(path)):
            kind = "turn_read"
        if kind is None:
            return await self.app(scope, receive, send)

        request_body = bytearray()
        response: dict[str, Any] = {"status": 0, "body": bytearray(), "too_big": False}

        async def receive_wrapper():
            message = await receive()
            if message["type"] == "http.request" and len(request_body) < _MAX_BODY:
                request_body.extend(message.get("body", b""))
            return message

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
            elif message["type"] == "http.response.body":
                if len(response["body"]) + len(message.get("body", b"")) <= _MAX_BODY:
                    response["body"].extend(message.get("body", b""))
                else:
                    response["too_big"] = True
            await send(message)

        await self.app(scope, receive_wrapper, send_wrapper)

        if not (200 <= response["status"] < 300) or response["too_big"]:
            return
        app = scope.get("app")
        store = getattr(getattr(app, "state", None), "insights", None)
        if store is None:
            return
        task = asyncio.create_task(self._record(app, store, kind, match, bytes(request_body), bytes(response["body"])))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _record(self, app, store, kind: str, match, request_body: bytes, response_body: bytes) -> None:
        try:
            result = json.loads(response_body) if response_body else {}
            if kind == "session":
                await store.record_session(result["session_id"], result["operator_id"], result["machine_id"],
                                           result.get("room_name"), result.get("participant_identity"),
                                           _parse_time(result.get("created_at")))
            elif kind == "turn":
                request = json.loads(request_body) if request_body else {}
                session_id = match.group("sid")
                owner = await self._owner(app, store, session_id)
                if owner is None or not request.get("text"):
                    return
                await store.record_turn(
                    session_id=session_id, turn_id=str(request.get("turn_id") or result.get("turn_id")),
                    employee_id=owner[0], machine_id=owner[1], question=str(request["text"]),
                    source=request.get("source"), status=str(result.get("status") or "unknown"),
                    reply=result.get("speech"), action_types=_action_types(result), llm_mode=result.get("llm_mode"),
                    asked_at=_parse_time(result.get("created_at")), completed_at=_parse_time(result.get("completed_at")))
            elif kind == "turn_read" and result.get("status") in ("completed", "failed"):
                await store.update_turn_result(
                    session_id=match.group("sid"), turn_id=match.group("tid"), status=result["status"],
                    reply=result.get("speech"), action_types=_action_types(result), llm_mode=result.get("llm_mode"),
                    completed_at=_parse_time(result.get("completed_at")))
        except Exception as exc:  # never affects the API; the turn itself already succeeded
            log.warning("insights capture failed (%s): %s", kind, type(exc).__name__)

    async def _owner(self, app, store, session_id: str) -> tuple[str, str] | None:
        """Employee/machine of a session; sessions created before insights ran are looked up in the core store."""
        owner = await store.session_owner(session_id)
        if owner is not None:
            return owner
        service = getattr(app.state, "service", None)
        session = service.store.get_session(session_id) if service is not None else None
        if session is None:
            return None
        await store.record_session(session.session_id, session.operator_id, session.machine_id, session.room_name,
                                   session.participant_identity, session.created_at)
        return session.operator_id, session.machine_id
