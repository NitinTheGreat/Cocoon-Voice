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
from contextlib import contextmanager
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
from .hazards import HazardEngine, load_hazard_policy
from .lms import coaching_prompts
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


# Optional request fields added after B. They are left out of an idempotency digest while unset, so a request saved
# before the upgrade still matches its own retry byte-for-byte (a digest never changes because the model grew).
ADDED_READINGS = ("proximity", "motion", "pitch_deg", "roll_deg", "grade_pct", "fuel_meter_l", "load_cycles_total")
ADDED_PAYLOAD = ("severity_unknown", "occurred_expression", "occurred_at", "acknowledge_conditions", "checkin_id",
                 "response")
# Task commands captured longer ago than this (device clock vs server receipt) must be re-confirmed: a queued start or
# condition acknowledgement from a disconnected period never stands in for a current pre-task check.
OFFLINE_TASK_MAX_AGE = timedelta(seconds=120)


def _without_unset(data: dict[str, Any], added: tuple[str, ...]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if not (k in added and v is None)}


@contextmanager
def domain_errors(noun: str):
    """Map store-level refusals to the shared API errors (one mapping for taps, consent and wellbeing)."""
    try:
        yield
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


def thread_config(session_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": session_id}}


class CocoonService:
    def __init__(self, settings: Settings, store: Store, graph, brain: Brain, catalog: Catalog | None = None,
                 bindings: SessionBindings | None = None, catalog_issue: str | None = None,
                 conditions: Conditions | None = None, planner=None, lms=None, wellbeing=None,
                 approvals=None, supervision=None, replanner=None, sos=None, channels=None):
        self.settings = settings
        self.store = store
        self.graph = graph  # compiled graph with a durable checkpointer
        self.brain = brain
        self.catalog = catalog  # immutable verified snapshot, loaded once at startup; None = not admitting
        self.bindings = bindings
        self.catalog_issue = catalog_issue
        self.policy: SafetyPolicy = load_policy(settings.safety_policy_path)
        self.conditions = conditions
        self.planner = planner  # cocoon_agent.planning.Planner (conditions + saved estimates per task)
        self.lms = lms  # cocoon_agent.lms.LMS (curriculum; None = no LMS)
        self.wellbeing = wellbeing  # cocoon_agent.wellbeing.Wellbeing (consent, private samples, advice, breaks)
        self.approvals = approvals  # cocoon_agent.approvals.Approvals (decisions and application)
        self.supervision = supervision  # cocoon_agent.supervision.Supervision (site scope, overview, feed)
        self.replanner = replanner  # cocoon_agent.replanning.Replanner (weather reorder proposals)
        self._feed_subscribers = 0
        self.sos = sos  # cocoon_agent.sos.Sos (impact check-ins, deadlines, emergency notifications)
        self.channels = channels  # cocoon_agent.channels.Channels (presence and presentation receipts)
        self.hazards = HazardEngine(load_hazard_policy(settings.hazard_policy_path))
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
            first = self.planner.enrich(session, tasks[0]) if self.planner is not None else tasks[0]
            if first.estimate is not None and first.estimate.predicted_minutes is not None:
                est = f", about {first.estimate.predicted_minutes:.0f} minutes by the configured demo estimate"
            else:
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
        body = {"session_id": session.session_id, "kind": req.kind,
                "payload": _without_unset(req.payload.model_dump(mode="json"), ADDED_PAYLOAD),
                "expected_version": req.expected_version}
        if req.original_binding is not None or req.client_draft_id is not None:  # D4 fields only when used
            body.update(original_binding=req.original_binding.model_dump(mode="json") if req.original_binding
                        else None, client_draft_id=req.client_draft_id)
        record_session = session
        if req.original_binding is not None:
            record_session = self._original_session(session, req.original_binding)
            if req.kind != "incident.submit_draft" and record_session.session_id != session.session_id:
                raise ApiError(409, "binding_mismatch", "only offline incident drafts may target another session; "
                                                        "task, lesson and check-in commands act on this session",
                               details=[{"field": "body.original_binding.session_id",
                                         "issue": "not the current session"}])
        if req.kind == "incident.submit_draft":
            from .sync import submit_draft_mutation

            body.pop("session_id")  # the same upload retried from a replacement session is the same command
            body["captured_at"] = req.captured_at.isoformat()
        elif req.kind.startswith("task.") and req.captured_at is not None \
                and utcnow() - req.captured_at > OFFLINE_TASK_MAX_AGE:
            raise ApiError(409, "invalid_transition", "this task command was captured too long ago; check the current "
                                                      "task and conditions and confirm again",
                           details=[{"field": "body.captured_at",
                                     "issue": f"older than {int(OFFLINE_TASK_MAX_AGE.total_seconds())} s"}])
        fingerprint = payload_hash(body)
        if req.kind == "incident.submit_draft":
            mutate, noun = submit_draft_mutation(record_session, req), "incident"
        elif req.kind.startswith(("lesson.", "quiz.")):
            mutate, noun = self._lms_mutation(session, req), "lesson"
        elif req.kind == "sos.respond":
            if self.sos is None:
                raise ApiError(404, "not_found", "SOS check-ins are not enabled")
            mutate = self.sos.respond_mutation(session, req.payload.checkin_id, req.payload.response)
            noun = "check-in"
        elif req.kind.startswith("break."):
            if self.wellbeing is None:
                raise ApiError(404, "not_found", "break records are not enabled")
            at = self.conditions.data_time(session) if self.conditions is not None else utcnow()
            mutate, noun = self.wellbeing.break_mutation(session, req.kind, req.expected_version, at), "break"
        elif req.kind.startswith("task."):
            gate = self.conditions.gate_for(session) if self.conditions is not None and req.kind == "task.start" \
                else None
            mutate = Store.task_transition(
                session, req.kind, req.payload.task_id, req.expected_version, gate,
                acknowledged=bool(req.payload.acknowledge_conditions),
                estimate_for=self.planner.estimate_fn(session) if self.planner is not None and req.kind == "task.start"
                else None)
            noun = "task"
        else:
            edits = req.payload.model_dump(include={"description", "severity", "severity_unknown", "location_text",
                                                    "occurred_expression"}, exclude_none=True)
            if req.payload.occurred_at is not None:
                edits["occurred_at"] = req.payload.occurred_at
            mutate = Store.incident_transition(session, req.kind, req.payload.incident_id, req.expected_version, edits)
            noun = "incident"
        from .sync import DraftConflict

        try:
            result, duplicate = self.run_domain_command(
                scope=f"actor:{principal.subject_id}", command_id=req.command_id, kind=req.kind,
                fingerprint=fingerprint, session=record_session, mutate=mutate, noun=noun)
        except DraftConflict as exc:  # raised inside the mutation, before domain_errors maps Conflict
            raise ApiError(409, "binding_mismatch" if exc.reason == "binding" else "idempotency_conflict", str(exc),
                           details=[{"field": "body.client_draft_id", "issue": f"stored as {exc.draft_id}"}]) from exc
        return s.SessionCommandResult(**{k: v for k, v in result.items() if k in s.SessionCommandResult.model_fields},
                                      status="completed", duplicate=duplicate)

    def _lms_mutation(self, session: s.Session, req: s.SessionCommand):
        from . import lms as lms_ops

        if self.lms is None:
            raise ApiError(404, "not_found", "no curriculum is loaded")
        p = req.payload
        machine = self.catalog.machines.get(session.machine_id) if self.catalog is not None else None
        if req.kind == "lesson.start":
            return lms_ops.start_lesson(self.lms, session, p.lesson_id, machine.category if machine else None)
        if req.kind == "quiz.start":
            return lms_ops.start_quiz(self.lms, session, p.lesson_id)
        if req.kind == "quiz.answer":
            return lms_ops.answer(self.lms, session, p.attempt_id, p.question_id, p.choice_id, "tap")
        action = req.kind.split(".", 1)[1]
        until = None
        if action == "defer":
            until = (self.store.data_clock(session.session_id) or utcnow()) + timedelta(minutes=p.defer_minutes or 30)
        return lms_ops.lesson_step(self.lms, session, p.lesson_id, action, p.expected_step, until)

    def learner(self, session: s.Session) -> s.LearnerView | None:
        from .lms import learner_view

        if self.lms is None:
            return None
        return learner_view(self.store, self.lms, session,
                            self.catalog.operator_skill.get(session.operator_id) if self.catalog else None)

    def lesson(self, session: s.Session, lesson_id: str) -> s.LessonView:
        if self.lms is None or lesson_id not in self.lms.lessons:
            raise ApiError(404, "not_found", "lesson not found")
        return self.lms.lesson_view(self.lms.lessons[lesson_id])

    def media(self, asset_id: str) -> s.LessonMediaAsset:
        if self.lms is None or asset_id not in self.lms.media:
            raise ApiError(404, "not_found", "content not found")
        return self.lms.media_view(asset_id)

    def media_file(self, asset_id: str, captions: bool = False):
        view = self.media(asset_id)
        path = self.lms.media_path(asset_id, captions=captions)
        if view.availability != "available" or path is None or not path.is_file():
            raise ApiError(404, "not_found", "content file not available")
        return path, ("text/vtt" if captions else view.mime_type), view.checksum_sha256

    def run_domain_command(self, *, scope: str, command_id: str, kind: str, fingerprint: str, session: s.Session,
                           mutate, noun: str) -> tuple[dict, bool]:
        with domain_errors(noun):
            return self.store.run_command(
                scope=scope, command_id=command_id, kind=kind, fingerprint=fingerprint, session_id=session.session_id,
                turn_id=None, mutate=mutate)

    # ------------------------------------------------------------------ supervision and approvals (D2)

    def supervisor_sites(self, principal: Principal) -> set[str]:
        return self.supervision.sites(principal) if self.supervision is not None else set()

    def require_site(self, principal: Principal, site_id: str) -> None:
        """The named site must be one of the supervisor's CLI grants (the query parameter grants nothing)."""
        if principal.kind != "supervisor" or not principal.has_scope("supervise"):
            raise ApiError(403, "forbidden", "only a supervisor with a site grant may use this route")
        if site_id not in self.supervisor_sites(principal):
            raise ApiError(403, "forbidden", "no grant for this site")

    def overview(self, principal: Principal, site_id: str) -> s.SupervisorSiteOverview:
        self.require_site(principal, site_id)
        self.approvals.expire_due()
        return self.supervision.overview(site_id)

    def _approval_reader(self, principal: Principal) -> set[str]:
        if principal.kind == "supervisor" and principal.has_scope("supervise"):
            return self.supervisor_sites(principal)
        if principal.kind == "operator" and principal.has_scope("sessions:own"):
            return set()
        raise ApiError(403, "forbidden", "approvals are read by supervisors and the operator concerned")

    def list_approvals(self, principal: Principal, status: str | None, limit: int,
                       cursor: str | None) -> s.ApprovalPageView:
        return self.approvals.list(principal, self._approval_reader(principal), status, limit, cursor)

    def get_approval(self, principal: Principal, approval_id: str) -> s.ApprovalView:
        with domain_errors("approval"):
            return self.approvals.get(principal, self._approval_reader(principal), approval_id)

    def decide(self, principal: Principal, approval_id: str,
               req: s.ApprovalDecisionRequest) -> s.ApprovalDecisionResult:
        from .approvals import ApprovalExpired, DecisionConflict, PayloadMismatch

        if principal.kind != "supervisor" or not principal.has_scope("supervise"):
            raise ApiError(403, "forbidden", "only a scoped supervisor may decide")
        try:
            with domain_errors("approval"):
                result = self.approvals.decide(principal, self.supervisor_sites(principal), approval_id, req)
        except DecisionConflict as exc:
            raise ApiError(409, "decision_conflict", str(exc)) from exc
        except ApprovalExpired as exc:
            raise ApiError(409, "approval_expired", str(exc)) from exc
        except PayloadMismatch as exc:
            raise ApiError(409, "version_conflict", str(exc), details=[
                {"field": "body.payload_sha256", "issue": f"current payload hash is {exc.current}"}]) from exc
        log.info("approval decision approval=%s decision=%s recorded=%s application=%s", approval_id, req.decision,
                 result.decision_recorded, result.approval.application.status)
        return result

    def notification_receipt(self, principal: Principal, notification_id: str,
                             req: s.NotificationReceipt) -> s.SupervisorNotificationItem:
        if principal.kind != "supervisor" or not principal.has_scope("supervise"):
            raise ApiError(403, "forbidden", "notifications are reported by the supervisor client")
        with domain_errors("notification"):
            return self.approvals.notification_receipt(self.supervisor_sites(principal), notification_id,
                                                       req.status, principal.subject_id)

    def propose_schedule(self, shift_id: str) -> s.ScheduleProposalResult:
        if self.replanner is None:
            raise ApiError(404, "not_found", "re-planning is not enabled")
        if self.store.get_shift(shift_id) is None:
            raise ApiError(404, "not_found", "shift not found")
        result = self.replanner.propose(shift_id)
        log.info("schedule proposal shift=%s status=%s", shift_id, result.status)
        return result

    def feed_start(self, principal: Principal, site_id: str, after: int | None, last_event_id: str | None) -> int:
        """Validate scope and cursor before the stream opens (errors are plain JSON responses)."""
        self.require_site(principal, site_id)
        cursor = after
        if last_event_id is not None:
            if not last_event_id.isdigit() or (after is not None and int(last_event_id) != after):
                raise ApiError(422, "invalid_cursor", "Last-Event-ID must be a feed sequence equal to `after`")
            cursor = int(last_event_id)
        floor, newest = self.supervision.feed_bounds()
        if cursor is None:
            cursor = newest
        if cursor > newest:
            raise ApiError(422, "invalid_cursor", "cursor is beyond the newest feed event")
        if cursor < floor:
            raise ApiError(410, "replay_expired", "events after this cursor are no longer retained; reload the "
                                                  "overview and continue from its feed_cursor")
        if self._feed_subscribers >= self.settings.feed_max_subscribers:
            raise ApiError(429, "rate_limited", "too many open supervisor streams", retryable=True)
        self._feed_subscribers += 1
        return cursor

    def _still_allowed(self, principal: Principal, site_id: str) -> bool:
        """Revocation and grant removal apply to an open stream at its next poll."""
        meta = self.store.get_actor_token(principal.token_id) if principal.token_id else None
        if meta is None or meta["revoked_at"] is not None or meta["expires_at"] <= utcnow():
            return False
        return site_id in self.supervisor_sites(principal)

    async def feed_stream(self, principal: Principal, site_id: str, cursor: int, is_disconnected):
        """SSE: replay after the cursor, then tail; heartbeat comments; bounded lifetime; closes on revocation."""
        loop = asyncio.get_running_loop()
        started = last_beat = loop.time()
        try:
            yield f"retry: 2000\n: cocoon supervisor feed, cursor {cursor}\n\n"
            while True:
                if await is_disconnected():
                    return
                if not self._still_allowed(principal, site_id):
                    yield "event: stream_closed\ndata: {\"reason\": \"access_revoked\"}\n\n"
                    return
                floor, _ = self.supervision.feed_bounds()
                if cursor < floor:
                    yield "event: stream_closed\ndata: {\"reason\": \"replay_expired\"}\n\n"
                    return
                events, cursor = self.supervision.feed_page(site_id, cursor)
                for e in events:
                    yield f"id: {e['sequence']}\nevent: {e['type']}\ndata: {json.dumps(e, separators=(',', ':'))}\n\n"
                if events:
                    continue
                now = loop.time()
                if now - started >= self.settings.feed_max_stream_seconds:
                    yield f"event: stream_closed\ndata: {{\"reason\": \"max_duration\", \"cursor\": {cursor}}}\n\n"
                    return
                if now - last_beat >= self.settings.feed_heartbeat_seconds:
                    last_beat = now
                    yield f": heartbeat {cursor}\n\n"
                await asyncio.sleep(self.settings.feed_poll_seconds)
        finally:
            self._feed_subscribers -= 1

    def run_due_work(self) -> dict[str, Any]:
        """One pass of the background worker: short transactions only, no model call, no network."""
        out: dict[str, Any] = {}
        if self.approvals is not None:
            out["expired"] = self.approvals.expire_due()
            out["applied"] = self.approvals.apply_pending()
        if self.supervision is not None:
            out["feed_pruned"] = self.supervision.prune_feed()
        if self.wellbeing is not None:
            out["samples_purged"] = self.wellbeing.purge_expired()
        if self.sos is not None:
            out["sos_deadlines"] = self.sos.run_due()
        return out

    # ------------------------------------------------------------------ SOS, presence, presentation (D3)

    def impact(self, session: s.Session, req: s.HumanImpactCandidate) -> s.ImpactResult:
        if self.sos is None:
            raise ApiError(404, "not_found", "SOS check-ins are not enabled")
        with domain_errors("impact"):
            result = self.sos.ingest(session, req)
        log.info("impact candidate session=%s source=%s status=%s episode=%s", session.session_id,
                 req.source_event_id, result.status, result.episode.episode_id if result.episode else None)
        return result

    def presence(self, session: s.Session, req: s.ConsumerPresenceReport) -> s.PresenceResult:
        with domain_errors("presence"):
            return self.channels.report_presence(session, req)

    def presentation(self, session: s.Session, event_id: str, req: s.PresentationReceipt) -> s.PresentationView:
        with domain_errors("presentation"):
            return self.channels.report_presentation(session, event_id, req)

    # ------------------------------------------------------------------ consent and wellbeing (D1)

    def _need_wellbeing(self):
        if self.wellbeing is None:
            raise ApiError(404, "not_found", "wellbeing is not enabled")
        return self.wellbeing

    def _consent_subject(self, principal: Principal, operator_id: str, write: bool) -> None:
        """Consent belongs to the operator. Reads: the operator themself, or the trusted voice service. Writes: only
        the operator's own actor token (a service or supervisor credential is not proof of consent)."""
        if principal.kind == "operator":
            if principal.operator_id != operator_id or not principal.has_scope("sessions:own"):
                raise ApiError(404, "not_found", "operator not found")
            return
        if principal.kind == "service" and not write:
            if self.catalog is None or not self.catalog.has_operator(operator_id):
                raise ApiError(404, "not_found", "operator not found")
            return
        raise ApiError(403, "forbidden", "only the operator can change their own consent" if write
                       else "this principal cannot read operator consent")

    def consents(self, principal: Principal, operator_id: str) -> s.OperatorConsentState:
        self._consent_subject(principal, operator_id, write=False)
        return self._need_wellbeing().consent_state(operator_id)

    def change_consent(self, principal: Principal, operator_id: str,
                       req: s.OperatorConsentChange) -> s.OperatorConsentChangeResult:
        self._consent_subject(principal, operator_id, write=True)
        with domain_errors("consent"):
            result = self._need_wellbeing().change_consent(operator_id, principal.subject_id, req)
        log.info("consent change operator=%s change=%s purpose=%s action=%s applied=%s", operator_id, req.change_id,
                 req.purpose, req.action, result.applied)
        return result

    def wellbeing_sample(self, session: s.Session, req: s.WellbeingSampleRequest) -> s.WellbeingSampleResult:
        with domain_errors("wellbeing"):
            result = self._need_wellbeing().ingest(session, req,
                                                   timedelta(seconds=self.settings.announcement_ttl_seconds))
        # never log values: only identifiers and the outcome
        log.info("wellbeing sample session=%s sample=%s status=%s advice_opened=%s", session.session_id,
                 req.sample_id, result.status, result.advice_opened)
        return result

    def wellbeing_view(self, session: s.Session) -> s.WellbeingView:
        if session.binding_status != "catalog_verified":
            raise ApiError(404, "not_found", "no wellbeing record for this session")
        return self._need_wellbeing().view(session)

    def _original_session(self, current: s.Session, binding: s.OriginalBinding) -> s.Session:
        """Resolve an offline command's original association against the server's own record."""
        original = self.store.get_session(binding.session_id)
        if original is None or original.binding_status != "catalog_verified" \
                or original.operator_id != current.operator_id or binding.operator_id != current.operator_id:
            raise ApiError(404, "not_found", "original session not found")
        wrong = [f for f in ("machine_id", "site_id", "shift_id")
                 if getattr(binding, f) is not None and getattr(binding, f) != getattr(original, f)]
        if wrong:
            raise ApiError(409, "binding_mismatch", "original_binding does not match the stored session",
                           details=[{"field": f"body.original_binding.{f}", "issue": "differs from the stored session"}
                                    for f in wrong])
        return original

    def get_command(self, principal: Principal, session: s.Session, command_id: str) -> s.SessionCommandResult:
        """Status after a lost response. Never executes. Visible from any verified session of the same operator (an
        offline draft may be looked up from a replacement session)."""
        row = self.store.get_command(f"actor:{principal.subject_id}", command_id)
        owner = self.store.get_session(row["session_id"]) if row is not None else None
        same = owner is not None and (owner.session_id == session.session_id or (
            owner.operator_id == session.operator_id and owner.binding_status == "catalog_verified"
            and session.binding_status == "catalog_verified"))
        if row is None or not same:
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
            display_name=principal.display_name, site_ids=sorted(self.supervisor_sites(principal)),
            allowed_associations=associations,
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
            rule_coverage=self.store.rule_coverage(session_id),
            learning=self.learner(session),
            shift_briefing=self.store.get_shift_briefing(session.shift_id) if session.shift_id else None,
            sos=self.sos.current(session.operator_id) if self.sos is not None
            and session.binding_status == "catalog_verified" else None,
            presence=self.channels.presence(session_id) if self.channels is not None else [],
            snapshot=self._snapshot(session),
            wellbeing=self.wellbeing.view(session) if self.wellbeing is not None
            and session.binding_status == "catalog_verified" else None,
        )

    def _snapshot(self, session: s.Session) -> s.SyncSnapshot:
        now = utcnow()
        machine = self._machine_state(session.session_id)
        presence = self.channels.presence(session.session_id) if self.channels is not None else []
        live = [p for p in presence if p.live and p.connection != "offline"]
        shift = self.store._one("SELECT schedule_version FROM shifts WHERE shift_id = ?", (session.shift_id,)) \
            if session.shift_id else None
        tasks = self.store.list_assigned_tasks(session.shift_id) if session.shift_id else []
        return s.SyncSnapshot(
            server_time=now, state_version=self.store.state_version(session.session_id),
            schedule_version=shift[0] if shift else None, task_versions={t.task_id: t.version for t in tasks},
            machine_data_time=machine.observed_at, machine_status=machine.status,
            machine_data_age_seconds=int((now - machine.received_at).total_seconds()) if machine.received_at else None,
            last_contact_at=max((p.received_at for p in presence), default=None),
            voice_available=any(p.voice_available for p in live), screen_available=any(p.screen_available for p in live))

    def tasks_with_conditions(self, session: s.Session) -> list[s.AssignedTask]:
        tasks = self.store.list_assigned_tasks(session.shift_id) if session.shift_id else []
        if self.planner is not None:
            return [self.planner.enrich(session, t) for t in tasks]
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
        body = req.model_dump(mode="json", exclude={"event_id"})
        body["readings"] = _without_unset(body["readings"], ADDED_READINGS)
        digest = payload_hash(body)
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
            category = machine.category if machine else None
            task = self.store.in_progress_task(session.shift_id)
            outcome = self.store.apply_observation(
                session, req, digest, self.policy, category,
                timedelta(seconds=self.settings.announcement_ttl_seconds),
                requires_engine_on=self.settings.seatbelt_rule_requires_engine_on,
                outcomes=outcomes, in_task_check=in_task_check,
                evaluate_in_tx=lambda c, state: self.hazards.evaluate(c, session, req, category, task, state),
                repeat_in_tx=lambda c: self.hazards.repeat_outcomes(c, session, req),
                coaching_in_tx=lambda c: coaching_prompts(c, session, req),
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
        if report.status == "played" and self.sos is not None:  # reported voice playback of a check-in = an offer
            self.sos.offer_after_report(event_id, "voice", f"delivery:{report.consumer_id}")
        log.info("delivery session=%s event=%s consumer=%s status=%s", session_id, event_id, report.consumer_id,
                 report.status)
        return record



def _briefing_conditions(check: s.WorkingConditionsCheck) -> str:
    sentence = conditions_sentence(check)
    if sentence.lower().startswith("conditions"):
        return sentence[0].upper() + sentence[1:]
    return "Conditions: " + sentence[0].lower() + sentence[1:]
