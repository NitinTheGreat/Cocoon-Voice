"""Application service: per-session ordering, turn idempotency and telemetry episodes.

Single-process assumption: per-session asyncio locks and the in-flight registry
live in memory, so run exactly one Uvicorn worker. Durable idempotency comes from
the SQLite unique keys in store.py, not from these in-memory structures.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict
from datetime import timedelta
from typing import Any

from langchain_core.runnables import RunnableConfig

from .api import schemas as s
from .config import Settings
from .graph.brain import Brain, LLMUnavailable
from .graph.builder import turn_input
from .rules import SEATBELT_RULE, seatbelt_condition
from .store import Conflict, Store, utcnow

log = logging.getLogger("cocoon_agent.service")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable


def payload_hash(obj: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def thread_config(session_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": session_id}}


class CocoonService:
    def __init__(self, settings: Settings, store: Store, graph, brain: Brain):
        self.settings = settings
        self.store = store
        self.graph = graph  # compiled graph with a durable checkpointer
        self.brain = brain
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}

    # ------------------------------------------------------------------ sessions

    def create_session(self, req: s.SessionCreateRequest) -> tuple[s.Session, bool]:
        try:
            return self.store.get_or_create_session(req)
        except Conflict as exc:
            raise ApiError(409, "session_conflict", str(exc)) from exc

    def require_session(self, session_id: str) -> s.Session:
        session = self.store.get_session(session_id)
        if session is None:
            raise ApiError(404, "not_found", "session not found")
        return session

    # ------------------------------------------------------------------ turns

    async def submit_turn(self, session_id: str, req: s.TurnRequest, request_id: str) -> tuple[int, s.TurnResult]:
        self.require_session(session_id)
        key = (session_id, req.turn_id)
        digest = payload_hash({"text": req.text, "source": req.source})
        try:
            row, inserted = self.store.claim_turn(session_id, req.turn_id, digest)
        except Conflict as exc:
            raise ApiError(409, "idempotency_conflict", str(exc)) from exc

        if not inserted:
            if row.status == "completed":
                log.info("turn replay session=%s turn=%s (stored result, no actions re-run)", session_id, req.turn_id)
                return 200, s.TurnResult.model_validate(row.result)
            running = self._inflight.get(key)
            if running is not None and not running.done():
                return 202, self._processing(session_id, req.turn_id, row.created_at)
            # failed earlier, or orphaned by a restart: re-run. Actions are keyed on turn_id.
            log.warning("re-running turn session=%s turn=%s previous_status=%s", session_id, req.turn_id, row.status)
            self.store.restart_turn(session_id, req.turn_id)

        task = asyncio.create_task(self._run_turn(session_id, req, row.created_at, request_id))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # mark exceptions retrieved
        self._inflight[key] = task
        # shield: a client disconnect/timeout must not cancel a turn that may be saving actions
        result = await asyncio.shield(task)
        return 200, result

    def get_turn(self, session_id: str, turn_id: str) -> s.TurnResult:
        self.require_session(session_id)
        row = self.store.get_turn(session_id, turn_id)
        if row is None:
            raise ApiError(404, "not_found", "turn not found")
        if row.status == "completed":
            return s.TurnResult.model_validate(row.result)
        if row.status == "failed":
            return s.TurnResult(
                session_id=session_id, turn_id=turn_id, status="failed", error=s.ErrorBody(**row.error),
                created_at=row.created_at, llm_mode=self.brain.mode,
            )
        return self._processing(session_id, turn_id, row.created_at)

    def _processing(self, session_id: str, turn_id: str, created_at) -> s.TurnResult:
        return s.TurnResult(
            session_id=session_id, turn_id=turn_id, status="processing", created_at=created_at,
            retry_after_ms=self.settings.turn_poll_after_ms,
            poll_url=f"/v1/sessions/{session_id}/turns/{turn_id}", llm_mode=self.brain.mode,
        )

    async def _run_turn(self, session_id: str, req: s.TurnRequest, created_at, request_id: str) -> s.TurnResult:
        key = (session_id, req.turn_id)
        started = time.perf_counter()
        try:
            async with self._locks[session_id]:
                try:
                    final = await asyncio.wait_for(
                        self.graph.ainvoke(turn_input(session_id, req.turn_id, req.text), thread_config(session_id)),
                        timeout=self.settings.turn_timeout_seconds,
                    )
                except LLMUnavailable as exc:
                    raise ApiError(503, "llm_unavailable", str(exc), retryable=True) from exc
                except asyncio.TimeoutError as exc:
                    raise ApiError(503, "turn_failed", "turn processing timed out", retryable=True) from exc
                version = self.store.bump_state_version(session_id)
                result = s.TurnResult(
                    session_id=session_id, turn_id=req.turn_id, status="completed", speech=final["speech"],
                    actions=final.get("actions", []), state_version=version, llm_mode=self.brain.mode,
                    created_at=created_at, completed_at=utcnow(),
                )
                self.store.complete_turn(session_id, req.turn_id, result.model_dump(mode="json"))
                log.info(
                    "turn completed session=%s turn=%s intent=%s actions=%s ms=%d",
                    session_id, req.turn_id, (final.get("route") or {}).get("intent"),
                    [a["type"] for a in final.get("actions", [])], (time.perf_counter() - started) * 1000,
                )
                return result
        except ApiError as exc:
            self.store.fail_turn(session_id, req.turn_id, self._error_dict(exc, request_id))
            log.warning("turn failed session=%s turn=%s code=%s", session_id, req.turn_id, exc.code)
            raise
        except Exception as exc:
            log.exception("turn crashed session=%s turn=%s", session_id, req.turn_id)
            err = ApiError(500, "internal_error", "turn processing failed", retryable=True)
            self.store.fail_turn(session_id, req.turn_id, self._error_dict(err, request_id))
            raise err from exc
        finally:
            self._inflight.pop(key, None)

    @staticmethod
    def _error_dict(exc: ApiError, request_id: str) -> dict[str, Any]:
        return {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "request_id": request_id}

    # ------------------------------------------------------------------ state

    async def get_state(self, session_id: str) -> s.SessionState:
        session = self.require_session(session_id)
        snapshot = await self.graph.aget_state(thread_config(session_id))
        pending = (snapshot.values or {}).get("pending") if snapshot else None
        return s.SessionState(
            session_id=session_id,
            state_version=self.store.state_version(session_id),
            llm_mode=self.brain.mode,
            tasks=self.store.list_tasks(),
            incidents=self.store.list_incidents(session_id),
            training_assignments=self.store.list_assignments(session.operator_id),
            available_lessons=self.store.list_lessons(),
            active_alerts=self.store.active_alerts(session_id),
            latest_alert=self.store.latest_alert(session_id),
            pending_question=s.PendingQuestion.model_validate(pending) if pending else None,
        )

    # ------------------------------------------------------------------ telemetry

    async def submit_telemetry(self, session_id: str, req: s.TelemetryRequest) -> s.TelemetryResult:
        self.require_session(session_id)
        digest = payload_hash(req.model_dump(mode="json", exclude={"event_id"}))
        async with self._locks[session_id]:  # ordered with turns for the same session
            prior = self.store.get_telemetry(session_id, req.event_id)
            if prior is not None:
                if prior[0] != digest:
                    raise ApiError(409, "idempotency_conflict", "event_id was already used with a different payload")
                return self._telemetry_result(session_id, prior[1], duplicate=True)
            condition = seatbelt_condition(
                req.readings, requires_engine_on=self.settings.seatbelt_rule_requires_engine_on
            )
            outcome = self.store.apply_telemetry(
                session_id, req, digest, condition, SEATBELT_RULE,
                timedelta(seconds=self.settings.announcement_ttl_seconds),
            )
            if outcome["alerts_opened"] or outcome["alerts_cleared"]:
                latest = self.store.latest_alert(session_id)
                # keep graph context in step so a later "why?" resolves against this alert
                await self.graph.aupdate_state(
                    thread_config(session_id),
                    {"latest_alert": latest.model_dump(mode="json") if latest else None},
                    as_node="compose",
                )
                log.info("alert transition session=%s opened=%s cleared=%s",
                         session_id, outcome["alerts_opened"], outcome["alerts_cleared"])
            return self._telemetry_result(session_id, outcome, duplicate=False)

    def _telemetry_result(self, session_id: str, outcome: dict[str, Any], duplicate: bool) -> s.TelemetryResult:
        return s.TelemetryResult(**outcome, duplicate=duplicate, active_alerts=self.store.active_alerts(session_id))

    # ------------------------------------------------------------------ announcements

    def list_events(self, session_id: str, after: int, limit: int) -> s.EventsPage:
        self.require_session(session_id)
        events, has_more = self.store.list_events(session_id, after, limit)
        return s.EventsPage(
            session_id=session_id, events=events, next_cursor=events[-1].sequence if events else after,
            has_more=has_more,
        )

    def record_delivery(self, session_id: str, event_id: str, report: s.DeliveryReport) -> s.DeliveryRecord:
        self.require_session(session_id)
        if not self.store.announcement_exists(session_id, event_id):
            raise ApiError(404, "not_found", "announcement not found")
        record = self.store.record_delivery(event_id, report)
        log.info("delivery session=%s event=%s consumer=%s status=%s", session_id, event_id, report.consumer_id,
                 report.status)
        return record
