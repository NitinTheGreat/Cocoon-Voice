"""Turn submission semantics against scripted HTTP responses (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from cocoon_voice.backend_client import BackendClient, BackendRejected, SessionNotFound, TurnOutcomeUnknown

from .conftest import FakeClock

SID = "ses_1"
TID = "lk-item_abc"


def completed(speech: str = "Your next task is grading.") -> dict:
    return {"session_id": SID, "turn_id": TID, "status": "completed", "speech": speech, "actions": [],
            "state_version": 3, "llm_mode": "mock", "error": None, "retry_after_ms": None, "poll_url": None,
            "created_at": "2026-09-23T10:00:00Z", "completed_at": "2026-09-23T10:00:01Z"}


def processing() -> dict:
    return {**completed(), "status": "processing", "speech": None, "state_version": None, "completed_at": None,
            "retry_after_ms": 500, "poll_url": f"/v1/sessions/{SID}/turns/{TID}"}


def err(code: str, retryable: bool, message: str = "x") -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable, "request_id": "req_1"}}


class Script:
    """Replays a list of responses/exceptions and records requests."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        status, body = step
        return httpx.Response(status, json=body)

    def methods(self) -> list[str]:
        return [r.method for r in self.requests]


def make(script: Script, clock: FakeClock | None = None, **kw) -> BackendClient:
    clock = clock or FakeClock()
    return BackendClient("http://backend", "tok", transport=httpx.MockTransport(script), sleep=clock.sleep,
                         clock=clock, **kw)


async def test_completed_turn_and_headers():
    s = Script([(200, completed())])
    result = await make(s).submit_turn(SID, TID, "what's next")
    assert result.speech == "Your next task is grading."
    req = s.requests[0]
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.headers["X-Request-ID"] == f"{TID}.1"
    assert json.loads(req.content) == {"turn_id": TID, "text": "what's next", "source": "voice"}


async def test_202_polls_until_completed():
    s = Script([(202, processing()), (200, processing()), (200, completed())])
    result = await make(s).submit_turn(SID, TID, "slow one")
    assert result.status == "completed"
    assert s.methods() == ["POST", "GET", "GET"]


async def test_timeout_checks_status_before_resubmitting():
    # The POST timed out but the backend had finished and saved the action.
    s = Script([httpx.ReadTimeout("slow"), (200, completed())])
    result = await make(s).submit_turn(SID, TID, "log an incident: leak")
    assert result.status == "completed"
    assert s.methods() == ["POST", "GET"]  # no second POST, so no chance of a second submission


async def test_timeout_then_unknown_turn_resubmits_same_id():
    s = Script([httpx.ConnectTimeout("down"), (404, err("not_found", False, "turn not found")), (200, completed())])
    await make(s).submit_turn(SID, TID, "hi")
    posts = [r for r in s.requests if r.method == "POST"]
    assert len(posts) == 2
    assert all(json.loads(r.content)["turn_id"] == TID for r in posts)


async def test_retryable_5xx_retries_with_same_turn_id():
    s = Script([(503, err("llm_unavailable", True)), (500, err("internal_error", True)), (200, completed())])
    await make(s).submit_turn(SID, TID, "hi")
    assert s.methods() == ["POST", "POST", "POST"]
    assert [r.headers["X-Request-ID"] for r in s.requests] == [f"{TID}.1", f"{TID}.2", f"{TID}.3"]


async def test_persistent_timeouts_raise_outcome_unknown_not_failure():
    steps = []
    for _ in range(4):
        steps += [httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")]  # POST + status check
    s = Script(steps)
    with pytest.raises(TurnOutcomeUnknown):
        await make(s, max_attempts=4).submit_turn(SID, TID, "hi")
    assert s.methods().count("POST") == 4  # bounded


async def test_deadline_bounds_endless_processing():
    clock = FakeClock()
    s = Script([(202, processing())] + [(200, processing())] * 200)
    with pytest.raises(TurnOutcomeUnknown):
        await make(s, clock, turn_deadline=5.0).submit_turn(SID, TID, "hi")
    assert clock.now <= 5.0


async def test_conflict_is_not_retried():
    s = Script([(409, err("idempotency_conflict", False))])
    with pytest.raises(BackendRejected) as exc:
        await make(s).submit_turn(SID, TID, "different text")
    assert exc.value.status == 409 and s.methods() == ["POST"]


async def test_missing_session_is_distinguished():
    s = Script([(404, err("not_found", False, "session not found"))])
    with pytest.raises(SessionNotFound):
        await make(s).submit_turn(SID, TID, "hi")


async def test_client_closes_cleanly():
    client = make(Script([]))
    await client.aclose()
    assert client._http.is_closed
