"""Async HTTP client for the Cocoon backend v1 contract (one reusable httpx.AsyncClient).

Turn semantics implemented here:
  200 completed -> return the result.
  202 processing -> poll GET .../turns/{turn_id} after retry_after_ms.
  409 -> BackendRejected (turn_id reused with different payload: a client bug).
  other 4xx -> BackendRejected (nothing was executed).
  5xx/429/timeout/network -> retry with the SAME turn_id, bounded by attempts and a deadline.
After a timeout the outcome is unknown, so the client first asks for the turn's status.
If the deadline passes without a definitive answer -> TurnOutcomeUnknown (never "failed").
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from . import contract as c

log = logging.getLogger("cocoon_voice.backend")


class BackendError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None,
                 retryable: bool = False, request_id: str | None = None):
        super().__init__(message)
        self.status, self.code, self.retryable, self.request_id = status, code, retryable, request_id


class BackendRejected(BackendError):
    """Definitive 4xx: the backend refused the request and did not execute it."""


class BackendUnavailable(BackendError):
    """Transport failure or 5xx after bounded retries."""


class SessionNotFound(BackendRejected):
    pass


class TurnOutcomeUnknown(Exception):
    """The deadline passed before the backend confirmed a result. Actions may or may not have been saved."""

    def __init__(self, turn_id: str):
        super().__init__(f"outcome of turn {turn_id} is unknown")
        self.turn_id = turn_id


_TRANSIENT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


def _error_from(resp: httpx.Response) -> BackendError:
    try:
        err = c.ErrorResponse.model_validate(resp.json()).error
        code, msg, retryable, rid = err.code, err.message, err.retryable, err.request_id
    except Exception:
        code, msg, retryable, rid = None, resp.text[:200], resp.status_code >= 500, resp.headers.get("X-Request-ID")
    if resp.status_code == 404 and code == "not_found" and "session" in msg:
        cls: type[BackendError] = SessionNotFound
    elif 400 <= resp.status_code < 500 and resp.status_code != 429:
        cls = BackendRejected
    else:
        cls = BackendUnavailable
    return cls(f"HTTP {resp.status_code} {code}: {msg}", status=resp.status_code, code=code,
               retryable=retryable or resp.status_code in (429, 502, 503, 504), request_id=rid)


class BackendClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        request_timeout: float = 10.0,
        connect_timeout: float = 3.0,
        max_attempts: int = 4,
        turn_deadline: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "User-Agent": "cocoon-voice/0.1"},
            timeout=httpx.Timeout(request_timeout, connect=connect_timeout),
            transport=transport,
        )
        self._request_timeout = request_timeout
        self._max_attempts = max_attempts
        self._turn_deadline = turn_deadline
        self._sleep = sleep
        self._clock = clock

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ low level

    async def _send(self, method: str, path: str, *, request_id: str, timeout: float | None = None,
                    **kwargs: Any) -> httpx.Response:
        started = time.perf_counter()
        extra = {"timeout": timeout} if timeout is not None else {}
        try:
            resp = await self._http.request(method, path, headers={"X-Request-ID": request_id}, **kwargs, **extra)
        except _TRANSIENT as exc:
            log.warning("backend %s %s request_id=%s transport_error=%s ms=%d", method, path, request_id,
                        type(exc).__name__, (time.perf_counter() - started) * 1000)
            raise
        log.info("backend %s %s request_id=%s status=%s ms=%d", method, path, request_id, resp.status_code,
                 (time.perf_counter() - started) * 1000)
        return resp

    async def _backoff(self, attempt: int, cap: float = 4.0) -> None:
        await self._sleep(min(cap, 0.25 * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2))

    async def _call(self, method: str, path: str, *, request_id: str, **kwargs: Any) -> httpx.Response:
        """Bounded retries for idempotent calls. Returns a 2xx response or raises BackendError."""
        last: BackendError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = await self._send(method, path, request_id=f"{request_id}.{attempt}", **kwargs)
            except _TRANSIENT as exc:
                last = BackendUnavailable(f"{type(exc).__name__} calling {path}", retryable=True)
            else:
                if resp.is_success:
                    return resp
                last = _error_from(resp)
                if isinstance(last, BackendRejected):
                    raise last
            if attempt < self._max_attempts:
                await self._backoff(attempt)
        assert last is not None
        raise last

    # ------------------------------------------------------------------ sessions

    async def ensure_session(self, req: c.SessionCreateRequest) -> c.Session:
        resp = await self._call("POST", "/v1/sessions", request_id=f"session-{req.client_session_key}"[:100],
                                json=req.model_dump())
        return c.Session.model_validate(resp.json())

    async def session_exists(self, session_id: str) -> bool:
        try:
            await self._call("GET", f"/v1/sessions/{session_id}/state", request_id=f"verify-{session_id}")
            return True
        except SessionNotFound:
            return False

    # ------------------------------------------------------------------ turns

    async def get_turn(self, session_id: str, turn_id: str, *, timeout: float | None = None) -> c.TurnResult | None:
        """One status read (no retry loop). None when the backend never saw this turn."""
        resp = await self._send("GET", f"/v1/sessions/{session_id}/turns/{turn_id}",
                                request_id=f"{turn_id}.status", timeout=timeout)
        if resp.status_code == 404:
            err = _error_from(resp)
            if isinstance(err, SessionNotFound):
                raise err
            return None
        if not resp.is_success:
            raise _error_from(resp)
        return c.TurnResult.model_validate(resp.json())

    async def submit_turn(self, session_id: str, turn_id: str, text: str, source: str = "voice") -> c.TurnResult:
        body = c.TurnRequest(turn_id=turn_id, text=text, source=source).model_dump()  # type: ignore[arg-type]
        path = f"/v1/sessions/{session_id}/turns"
        deadline = self._clock() + self._turn_deadline
        started = time.perf_counter()
        for attempt in range(1, self._max_attempts + 1):
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                resp = await self._send("POST", path, request_id=f"{turn_id}.{attempt}", json=body,
                                        timeout=min(self._request_timeout, remaining))
            except _TRANSIENT:
                # The request may have reached the backend and saved actions. Ask before resubmitting.
                result = await self._reconcile(session_id, turn_id, deadline)
                if result is not None:
                    return self._done(result, started, attempt)
                await self._backoff(attempt)
                continue

            if resp.status_code in (200, 202):
                result = c.TurnResult.model_validate(resp.json())
                if result.status == "completed":
                    return self._done(result, started, attempt)
                if result.status == "processing":  # 202: duplicate still running elsewhere
                    polled = await self._poll(session_id, turn_id, deadline, result.retry_after_ms)
                    if polled is not None:
                        return self._done(polled, started, attempt)
                continue  # failed (retryable) or lost: resubmit the same turn_id
            err = _error_from(resp)
            if isinstance(err, BackendRejected):
                raise err
            log.warning("turn %s attempt %d backend error %s (retrying same turn_id)", turn_id, attempt, err)
            await self._backoff(attempt)
        log.error("turn %s outcome unknown after %.1fs", turn_id, time.perf_counter() - started)
        raise TurnOutcomeUnknown(turn_id)

    def _done(self, result: c.TurnResult, started: float, attempts: int) -> c.TurnResult:
        log.info("turn %s completed attempts=%d ms=%d mode=%s", result.turn_id, attempts,
                 (time.perf_counter() - started) * 1000, result.llm_mode)
        return result

    async def _reconcile(self, session_id: str, turn_id: str, deadline: float) -> c.TurnResult | None:
        try:
            status = await self.get_turn(session_id, turn_id,
                                         timeout=max(0.1, min(self._request_timeout, deadline - self._clock())))
        except (_TRANSIENT + (BackendUnavailable,)):
            return None
        if status is None or status.status == "failed":
            return None
        if status.status == "completed":
            return status
        return await self._poll(session_id, turn_id, deadline, status.retry_after_ms)

    async def _poll(self, session_id: str, turn_id: str, deadline: float,
                    retry_after_ms: int | None) -> c.TurnResult | None:
        """Poll until completed (returns it), failed/unknown to backend (returns None) or deadline."""
        delay = max(0.1, (retry_after_ms or 500) / 1000)
        while self._clock() + delay < deadline:
            await self._sleep(delay)
            try:
                status = await self.get_turn(session_id, turn_id,
                                             timeout=max(0.1, min(self._request_timeout, deadline - self._clock())))
            except (_TRANSIENT + (BackendUnavailable,)):
                delay = min(delay * 2, 4.0)
                continue
            if status is None or status.status == "failed":
                return None
            if status.status == "completed":
                return status
            delay = max(0.1, (status.retry_after_ms or 500) / 1000)
        return None

    # ------------------------------------------------------------------ announcements

    async def list_events(self, session_id: str, after: int, limit: int = 20) -> c.EventsPage:
        """Single attempt; the announcement loop owns backoff."""
        try:
            resp = await self._send("GET", f"/v1/sessions/{session_id}/events", request_id=f"events-{after}",
                                    params={"after": after, "limit": limit})
        except _TRANSIENT as exc:
            raise BackendUnavailable(f"{type(exc).__name__} polling events", retryable=True) from exc
        if not resp.is_success:
            raise _error_from(resp)
        return c.EventsPage.model_validate(resp.json())

    async def report_delivery(self, session_id: str, event_id: str, consumer_id: str, status: str,
                              detail: str | None = None) -> c.DeliveryRecord:
        body = c.DeliveryReport(consumer_id=consumer_id, status=status, detail=detail)  # type: ignore[arg-type]
        resp = await self._call("POST", f"/v1/sessions/{session_id}/events/{event_id}/delivery",
                                request_id=f"delivery-{event_id}"[:100], json=body.model_dump())
        return c.DeliveryRecord.model_validate(resp.json())
