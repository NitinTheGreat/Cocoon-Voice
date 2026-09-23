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
import secrets
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from langchain_core.runnables import RunnableConfig

from .api import schemas as s
from .auth import MAX_CREDENTIAL_LENGTH, ROLE_SCOPES, SERVICE_PRINCIPAL, TOKEN_PREFIX, Principal, token_digest
from .catalog import Catalog, SessionBindings
from .config import Settings
from .graph.brain import Brain, LLMUnavailable
from .graph.builder import turn_input
from .rules import SafetyPolicy, load_policy
from .conditions import Conditions, conditions_sentence
from .store import (
    ConditionsGate, Conflict, InvalidInput, InvalidTransition, NewSessionBinding, NotFound, Store, VersionConflict,
    utcnow,
)

log = logging.getLogger("cocoon_agent.service")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False,
                 details: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable
        self.details = details


def payload_hash(obj: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def thread_config(session_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": session_id}}


class CocoonService:
    def __init__(self, settings: Settings, store: Store, graph, brain: Brain, catalog: Catalog | None = None,
                 bindings: SessionBindings | None = None, catalog_issue: str | None = None,
                 conditions: Conditions | None = None):
        self.settings = settings
        self.store = store
        self.graph = graph  # compiled graph with a durable checkpointer
        self.brain = brain
        self.catalog = catalog  # immutable verified snapshot, loaded once at startup; None = not admitting
        self.bindings = bindings
        self.catalog_issue = catalog_issue
        self.policy: SafetyPolicy = load_policy(settings.safety_policy_path)
        self.conditions = conditions
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Telemetry has its own per-session lock: an urgent sample never waits behind a turn's model call.
        self._telemetry_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}

    # ------------------------------------------------------------------ sessions

    def create_session(self, req: s.SessionCreateRequest) -> tuple[s.Session, bool]:
        try:
            session, created = self.store.get_or_create_session(req, self._admit_new_session)
        except Conflict as exc:
            raise ApiError(409, "session_conflict", str(exc)) from exc
        if session.shift_id and self.store.get_shift(session.shift_id) is not None:
            # Once per seeded shift (also retried here for a session whose briefing write was interrupted).
            self.store.ensure_shift_briefing(session, self._briefing_speech(session),
                                             timedelta(seconds=self.settings.announcement_ttl_seconds))
        return session, created

    def _briefing_speech(self, session: s.Session) -> str:
        """Built only from this operator's assigned tasks and the conditions actually available (synthetic)."""
        shift = self.store.get_shift(session.shift_id)
        assert shift is not None
        tasks = [t for t in self.store.list_assigned_tasks(session.shift_id) if t.status != "completed"]
        machine = self.catalog.machines.get(session.machine_id) if self.catalog is not None else None
        start, end = shift.start_at.astimezone(shift.tz()), shift.end_at.astimezone(shift.tz())
        parts = [f"Shift briefing for the {machine.model if machine else session.machine_id} at {shift.site_name}, "
                 f"{start:%H:%M} to {end:%H:%M}."]
        if tasks:
            first = tasks[0]
            est = f", about {first.duration.minutes} minutes by the demo estimate" if first.duration.minutes else ""
            parts.append(f"You have {len(tasks)} task{'s' if len(tasks) != 1 else ''}. First: {first.title} in "
                         f"{first.zone_name} at {first.scheduled_start_local}{est}.")
            if self.conditions is not None:
                check = self.conditions.check(session, first)
                parts.append(_briefing_conditions(check))
            elif first.weather.summary:
                parts.append(f"Conditions (synthetic demo value, not a forecast): {first.weather.summary}.")
        else:
            parts.append("You have no open tasks on this shift.")
        parts.append("Keep your seatbelt fastened whenever the engine is running.")
        return " ".join(parts)

    def _admit_new_session(self, req: s.SessionCreateRequest) -> NewSessionBinding:
        """Rules for a NEW client_session_key only. Raising aborts the insert; nothing is written.

        Precedence (deterministic): catalog availability, then machine_id, then operator_id, then site/shift.
        When both IDs are unknown the code is unknown_machine and details name both fields."""
        catalog = self.catalog
        if catalog is None:
            raise ApiError(503, "catalog_unavailable",
                           "the verified machine/operator catalog is not loaded; new sessions cannot be admitted")
        details = []
        if not catalog.has_machine(req.machine_id):
            details.append({"field": "body.machine_id", "issue": "not in the machine catalog"})
        if not catalog.has_operator(req.operator_id):
            details.append({"field": "body.operator_id", "issue": "not in the operator catalog"})
        if details:
            code = "unknown_machine" if details[0]["field"] == "body.machine_id" else "unknown_operator"
            raise ApiError(422, code, "session association is not in the verified catalog", details=details)
        if req.site_id is None and req.shift_id is None:
            auto = self._current_trusted_binding(req.operator_id, req.machine_id)
            if auto is not None:
                return NewSessionBinding(
                    dataset_manifest_sha256=catalog.manifest_sha256, context_status="trusted_binding",
                    site_id=auto.site_id, shift_id=auto.shift_id,
                    context_source=f"session-bindings:{self.bindings.sha256}:{auto.binding_id}:auto")
            return NewSessionBinding(dataset_manifest_sha256=catalog.manifest_sha256, context_status="unavailable")
        if req.site_id is None or req.shift_id is None:
            raise ApiError(422, "validation_error", "site_id and shift_id must be supplied together",
                           details=[{"field": "body.site_id" if req.site_id is None else "body.shift_id",
                                     "issue": "required when the other is supplied"}])
        binding = None
        if self.bindings is not None:
            binding = self.bindings.match(req.operator_id, req.machine_id, req.site_id, req.shift_id)
        if binding is None:
            raise ApiError(422, "validation_error", "site_id/shift_id do not match a trusted binding",
                           details=[{"field": "body.site_id", "issue": "no trusted binding for this association"}])
        return NewSessionBinding(
            dataset_manifest_sha256=catalog.manifest_sha256, context_status="trusted_binding",
            site_id=binding.site_id, shift_id=binding.shift_id,
            context_source=f"session-bindings:{self.bindings.sha256}:{binding.binding_id}",
        )

    def _current_trusted_binding(self, operator_id: str, machine_id: str):
        """Server-side choice (never client metadata): the single trusted binding for this operator/machine whose
        seeded shift is for today's date at the site (site-local wall clock). None when there is no binding file,
        no such shift, or more than one candidate."""
        if self.bindings is None:
            return None
        now = utcnow()
        candidates = []
        for b in self.bindings.bindings:
            if b.operator_id != operator_id or b.machine_id != machine_id:
                continue
            shift = self.store.get_shift(b.shift_id)
            if shift and shift.site_id == b.site_id and shift.service_date == shift.local_date(now):
                candidates.append(b)
        return candidates[0] if len(candidates) == 1 else None

    # ------------------------------------------------------------------ task commands (taps and graph tools)

    def execute_command(self, principal: Principal, session: s.Session,
                        req: s.SessionCommand) -> s.SessionCommandResult:
        """Tap path. The same domain mutation as the graph tools; identity is scoped to the calling principal."""
        fingerprint = payload_hash({"session_id": session.session_id, "kind": req.kind,
                                    "payload": req.payload.model_dump(mode="json"),
                                    "expected_version": req.expected_version})
        if req.kind.startswith("task."):
            gate = self.conditions.gate_for(session) if self.conditions is not None and req.kind == "task.start" \
                else None
            mutate = Store.task_transition(session, req.kind, req.payload.task_id, req.expected_version, gate,
                                           acknowledged=bool(req.payload.acknowledge_conditions))
            noun = "task"
        else:
            edits = req.payload.model_dump(include={"description", "severity", "severity_unknown", "location_text",
                                                    "occurred_expression"}, exclude_none=True)
            if req.payload.occurred_at is not None:
                edits["occurred_at"] = req.payload.occurred_at
            mutate = Store.incident_transition(session, req.kind, req.payload.incident_id, req.expected_version, edits)
            noun = "incident"
        result, duplicate = self.run_domain_command(
            scope=f"actor:{principal.subject_id}", command_id=req.command_id, kind=req.kind, fingerprint=fingerprint,
            session=session, mutate=mutate, noun=noun)
        return s.SessionCommandResult(**{k: v for k, v in result.items() if k in s.SessionCommandResult.model_fields},
                                      status="completed", duplicate=duplicate)

    def run_domain_command(self, *, scope: str, command_id: str, kind: str, fingerprint: str, session: s.Session,
                           mutate, noun: str) -> tuple[dict, bool]:
        try:
            return self.store.run_command(
                scope=scope, command_id=command_id, kind=kind, fingerprint=fingerprint, session_id=session.session_id,
                turn_id=None, mutate=mutate)
        except Conflict as exc:
            raise ApiError(409, "idempotency_conflict", str(exc)) from exc
        except NotFound as exc:
            raise ApiError(404, "not_found", str(exc)) from exc
        except VersionConflict as exc:
            raise ApiError(409, "version_conflict", str(exc), details=[
                {"field": "body.expected_version", "issue": f"current version is {exc.current_version}"}]) from exc
        except InvalidTransition as exc:
            details = [{"field": "body.kind", "issue": f"{noun} status is {exc.current_status}"}]
            details += [{"field": f"draft.{m}", "issue": "not stated yet"} for m in exc.missing]
            raise ApiError(409, "invalid_transition", str(exc), details=details) from exc
        except InvalidInput as exc:
            raise ApiError(422, "validation_error", str(exc), details=[
                {"field": f"body.{exc.field}", "issue": exc.issue}]) from exc
        except ConditionsGate as exc:
            check = exc.check
            details = [{"field": "conditions.level", "issue": check.level if check else "unknown"}]
            details += [{"field": f"conditions.{f.variable}", "issue": f"{f.level}: {f.value} {f.unit}"}
                        for f in (check.findings if check else []) if f.level in ("acknowledge", "block")]
            if exc.reason == "conditions_need_acknowledgement":
                details.append({"field": "body.payload.acknowledge_conditions",
                                "issue": "required to start despite these findings"})
            raise ApiError(409, "invalid_transition", f"task start stopped by working conditions ({exc.reason})",
                           details=details) from exc

    def get_command(self, principal: Principal, session: s.Session, command_id: str) -> s.SessionCommandResult:
        row = self.store.get_command(f"actor:{principal.subject_id}", command_id)
        if row is None or row["session_id"] != session.session_id:
            raise ApiError(404, "not_found", "command not found")
        result = json.loads(row["result_json"])
        return s.SessionCommandResult(**{k: v for k, v in result.items() if k in s.SessionCommandResult.model_fields},
                                      status="completed", duplicate=True)

    # ------------------------------------------------------------------ authentication and authorisation

    def authenticate(self, credential: str | None, header_count: int, now: datetime) -> Principal:
        """Resolve exactly one server-side principal. Every failure is the same sanitized 401, so the response
        never reveals whether a token was unknown, expired or revoked. An auth-store failure is 503, never success."""
        denied = ApiError(401, "unauthorized", "missing or invalid bearer token")
        if header_count > 1 or not credential or len(credential) > MAX_CREDENTIAL_LENGTH:
            raise denied
        expected = self.settings.service_token.get_secret_value()
        if secrets.compare_digest(credential.encode(), expected.encode()):
            return SERVICE_PRINCIPAL
        if not credential.startswith(TOKEN_PREFIX):
            raise denied  # never falls back to service privileges
        try:
            meta = self.store.resolve_actor_token(token_digest(credential))
        except sqlite3.Error as exc:
            log.error("actor token lookup failed: %s", type(exc).__name__)
            raise ApiError(503, "auth_unavailable", "authentication is temporarily unavailable",
                           retryable=True) from exc
        if meta is None or meta["revoked_at"] is not None or meta["expires_at"] <= now \
                or meta["kind"] not in ROLE_SCOPES:
            raise denied
        return Principal(
            kind=meta["kind"], subject_id=meta["principal_id"], operator_id=meta["operator_id"],
            display_name=meta["display_name"], token_id=meta["token_id"],
            # a stored token can never hold more than its role allows
            scopes=frozenset(meta["scopes"]) & frozenset(ROLE_SCOPES[meta["kind"]]),
            expires_at=meta["expires_at"],
        )

    @staticmethod
    def require_service(principal: Principal) -> None:
        if principal.kind != "service":
            raise ApiError(403, "forbidden", "this operation is reserved for the trusted service")

    def authorize_session(self, principal: Principal, session_id: str) -> s.Session:
        """Parent-session check done before any child read or tool runs. An operator sees only their own
        catalog_verified sessions; anything else is the same 404 as a missing session (existence is not revealed)."""
        if principal.kind == "service":
            return self.require_session(session_id)
        if principal.kind != "operator" or not principal.has_scope("sessions:own"):
            raise ApiError(403, "forbidden", "this principal cannot access sessions")
        session = self.store.get_session(session_id)
        if session is None or session.binding_status != "catalog_verified" \
                or session.operator_id != principal.operator_id:
            raise ApiError(404, "not_found", "session not found")
        return session

    def describe(self, principal: Principal) -> s.MeResponse:
        associations = []
        if principal.kind == "operator" and principal.operator_id:
            associations = [
                s.SessionAssociation(operator_id=x.operator_id, machine_id=x.machine_id, site_id=x.site_id,
                                     shift_id=x.shift_id, session_id=x.session_id)
                for x in self.store.owned_verified_sessions(principal.operator_id)
            ]
        return s.MeResponse(
            subject_id=principal.subject_id, principal_kind=principal.kind, operator_id=principal.operator_id,
            display_name=principal.display_name, site_ids=[], allowed_associations=associations,
            scopes=sorted(principal.scopes), token_id=principal.token_id, token_expires_at=principal.expires_at,
        )

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
                action_records=self.store.action_records(session_id, turn_id),
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
                    created_at=created_at, completed_at=utcnow(), branch=(final.get("route") or {}).get("branch"),
                    action_records=self.store.action_records(session_id, req.turn_id),
                )
                self.store.complete_turn(session_id, req.turn_id, result.model_dump(mode="json"))
                log.info(
                    "turn completed session=%s turn=%s intent=%s actions=%s ms=%d",
                    session_id, req.turn_id, (final.get("route") or {}).get("intent"),
                    [a["type"] for a in final.get("actions", [])], (time.perf_counter() - started) * 1000,
                )
                return result
        except ApiError as exc:
            self._note_saved_actions(session_id, req.turn_id, exc)
            self.store.fail_turn(session_id, req.turn_id, self._error_dict(exc, request_id))
            log.warning("turn failed session=%s turn=%s code=%s", session_id, req.turn_id, exc.code)
            raise
        except Exception as exc:
            log.exception("turn crashed session=%s turn=%s", session_id, req.turn_id)
            err = ApiError(500, "internal_error", "turn processing failed", retryable=True)
            self._note_saved_actions(session_id, req.turn_id, err)
            self.store.fail_turn(session_id, req.turn_id, self._error_dict(err, request_id))
            raise err from exc
        finally:
            self._inflight.pop(key, None)

    def _note_saved_actions(self, session_id: str, turn_id: str, exc: ApiError) -> None:
        """A failure after committed writes must not read as "nothing was saved": name them in the error. Retrying
        the same turn_id reuses them (turn-scoped command IDs) and only runs what is still missing."""
        records = self.store.action_records(session_id, turn_id)
        if records:
            exc.message = (f"{exc.message}; {len(records)} action(s) were saved before the failure and will not be "
                           "repeated on retry")
            exc.details = (exc.details or []) + [
                {"field": "action_records", "issue": f"{r.kind} {r.outcome}: {r.record_id or '-'}"} for r in records]

    @staticmethod
    def _error_dict(exc: ApiError, request_id: str) -> dict[str, Any]:
        out = {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "request_id": request_id}
        if exc.details:
            out["details"] = exc.details
        return out

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
            incident_drafts=self.store.list_drafts(session_id),
            pending_approvals=self.store.list_pending_approvals(session_id),
            training_assignments=self.store.list_assignments(session),
            available_lessons=self.store.list_lessons(),
            active_alerts=self.store.active_alerts(session_id),
            latest_alert=self.store.latest_alert(session_id),
            pending_question=s.PendingQuestion.model_validate(pending) if pending else None,
            shift=self.store.get_shift(session.shift_id) if session.shift_id else None,
            assigned_tasks=self.tasks_with_conditions(session),
            site_conditions=self.conditions.check(session, None) if self.conditions and session.site_id else None,
            machine_state=self._machine_state(session_id),
            idle_reasons=self.store.list_idle_reasons(session_id),
            shift_briefing=self.store.get_shift_briefing(session.shift_id) if session.shift_id else None,
        )

    def tasks_with_conditions(self, session: s.Session) -> list[s.AssignedTask]:
        tasks = self.store.list_assigned_tasks(session.shift_id) if session.shift_id else []
        if self.conditions is None:
            return tasks
        return [t.model_copy(update={"conditions": self.conditions.check(session, t)}) if t.status != "completed"
                else t for t in tasks]

    def _machine_state(self, session_id: str) -> s.MachineStateView:
        limit = self.settings.telemetry_stale_seconds
        row = self.store.machine_state(session_id)
        if row is None:
            return s.MachineStateView(status="unavailable", stale_after_seconds=limit)
        readings = s.TelemetryReadings.model_validate_json(row["readings_json"])
        received = datetime.fromisoformat(row["received_at"])
        observed = datetime.fromisoformat(row["observed_at"])
        idle_since = datetime.fromisoformat(row["idle_since"]) if row["idle_since"] else None
        return s.MachineStateView(
            status="stale" if (utcnow() - received).total_seconds() > limit else "fresh",
            observed_at=observed, received_at=received, engine_on=readings.engine_on,
            seatbelt_fastened=readings.seatbelt_fastened, operating_state=readings.operating_state,
            speed_kph=readings.speed_kph, idle_since=idle_since,
            idle_seconds_observed=int((observed - idle_since).total_seconds()) if idle_since else None,
            stale_after_seconds=limit,
        )

    # ------------------------------------------------------------------ telemetry

    async def submit_telemetry(self, session_id: str, req: s.TelemetryRequest) -> s.TelemetryResult:
        """Rules run synchronously on the sample, in one short transaction, without any model call. The graph reads
        alerts from the database when a turn starts, so no checkpoint write (and no turn lock) is needed here."""
        session = self.require_session(session_id)
        digest = payload_hash(req.model_dump(mode="json", exclude={"event_id"}))
        async with self._telemetry_locks[session_id]:
            prior = self.store.get_telemetry(session_id, req.event_id)
            if prior is not None:
                if prior[0] != digest:
                    raise ApiError(409, "idempotency_conflict", "event_id was already used with a different payload")
                return self._telemetry_result(session_id, prior[1], duplicate=True)
            machine = self.catalog.machines.get(session.machine_id) if self.catalog is not None else None
            outcomes, in_task_check = [], None
            if self.conditions is not None:
                worse, in_task_check = self.conditions.worsening(session, req.observed_at)
                if worse is not None:
                    outcomes.append(worse)
            outcome = self.store.apply_observation(
                session, req, digest, self.policy, machine.category if machine else None,
                timedelta(seconds=self.settings.announcement_ttl_seconds),
                requires_engine_on=self.settings.seatbelt_rule_requires_engine_on,
                outcomes=outcomes, in_task_check=in_task_check,
            )
            if outcome["alerts_opened"] or outcome["alerts_cleared"]:
                log.info("alert transition session=%s opened=%s cleared=%s drafts=%s", session_id,
                         outcome["alerts_opened"], outcome["alerts_cleared"], outcome["drafts_created"])
            return self._telemetry_result(session_id, outcome, duplicate=False)

    def _telemetry_result(self, session_id: str, outcome: dict[str, Any], duplicate: bool) -> s.TelemetryResult:
        outcome = {k: v for k, v in outcome.items() if k in s.TelemetryResult.model_fields}  # pre-B3 saved results
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



def _briefing_conditions(check: s.ConditionCheck) -> str:
    sentence = conditions_sentence(check)
    return "Conditions: " + sentence[0].lower() + sentence[1:]
