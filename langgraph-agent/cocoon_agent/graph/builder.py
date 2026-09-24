"""Typed LangGraph StateGraph: load_context -> route -> action | ask -> compose.

Thread id == application session_id. Messages carry ids derived from turn_id, so
re-running an interrupted turn replaces rather than duplicates graph memory.

Every write goes through Store.run_command with a turn-scoped command ID ("{turn_id}:{kind}"), so the mutation and
its action record commit together and a retried turn reuses committed results instead of repeating them. The route
decision is saved once per turn; a retry follows the same plan without another model call.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from ..api import schemas as s
from ..incident_time import interpret
from .. import lms as lms_ops
from ..store import (
    ConditionsGate, InvalidInput, InvalidTransition, NotFound, OccurrenceTime, Store, VersionConflict, iso, utcnow,
)
from .brain import Brain, TurnContext


class CocoonState(TypedDict, total=False):
    # durable conversation context (checkpointed per session)
    messages: Annotated[list[AnyMessage], add_messages]
    pending: dict[str, Any] | None
    latest_alert: dict[str, Any] | None
    # per-turn working values (overwritten on every turn)
    session_id: str
    turn_id: str
    user_text: str
    route: dict[str, Any] | None
    actions: list[dict[str, Any]]
    speech: str


def turn_input(session_id: str, turn_id: str, text: str) -> CocoonState:
    return {
        "messages": [HumanMessage(content=text, id=f"user:{turn_id}")],
        "session_id": session_id,
        "turn_id": turn_id,
        "user_text": text,
        "route": None,
        "actions": [],
        "speech": "",
    }


def _history(state: CocoonState) -> list[tuple[str, str]]:
    out = []
    for m in state.get("messages", [])[:-1]:
        out.append(("operator" if isinstance(m, HumanMessage) else "cocoon", str(m.content)))
    return out


def build_graph(store: Store, brain: Brain, conditions=None, planner=None, lms=None, wellbeing=None, sos=None):
    """`conditions` (cocoon_agent.conditions.Conditions) enables the working-conditions gate on task starts and the
    conditions intent; without it task starts are ungated (as before Batch C)."""
    def _session(state: CocoonState) -> s.Session:
        session = store.get_session(state["session_id"])
        if session is None:
            raise LookupError(state["session_id"])
        return session

    def _ctx(state: CocoonState) -> TurnContext:
        alert = state.get("latest_alert")
        return TurnContext(
            text=state["user_text"],
            history=_history(state),
            pending=state.get("pending"),
            latest_alert=s.Alert.model_validate(alert) if alert else None,
            lessons=store.list_lessons(),
            drafts=store.list_drafts(state["session_id"]),
            learning=lms_ops.active_learning(store, _session(state)) if lms is not None else None,
        )

    def _command(state: CocoonState, kind: str, fingerprint: str, mutate) -> tuple[dict[str, Any], bool]:
        sid, turn_id = state["session_id"], state["turn_id"]
        return store.run_command(scope=f"turn:{sid}", command_id=f"{turn_id}:{kind}", kind=kind,
                                 fingerprint=fingerprint, session_id=sid, turn_id=turn_id, mutate=mutate)

    def _saved(state: CocoonState, kind: str) -> dict[str, Any] | None:
        """Result this turn already committed for `kind` (a retry must not re-resolve its target)."""
        row = store.get_command(f"turn:{state['session_id']}", f"{state['turn_id']}:{kind}")
        return json.loads(row["result_json"]) if row else None

    async def load_context(state: CocoonState) -> CocoonState:
        # The database is authoritative for alerts; mirror the latest into graph state.
        alert = store.latest_alert(state["session_id"])
        return {"latest_alert": alert.model_dump(mode="json") if alert else None}

    async def route(state: CocoonState) -> CocoonState:
        saved = store.turn_route(state["session_id"], state["turn_id"])
        if saved is not None:
            return {"route": saved}
        decision = await brain.route(_ctx(state))
        if decision.intent in ("answer_pending", "cancel_pending") and not state.get("pending"):
            decision = decision.model_copy(update={"intent": "smalltalk", "branch": "general_assistance"})
        route_ = decision.model_dump()
        store.save_turn_route(state["session_id"], state["turn_id"], route_)
        return {"route": route_}

    def choose(state: CocoonState) -> Literal[
        "tasks", "log_incident", "drafts", "training", "explain_alert", "record_idle_reason", "cancel_pending",
        "unsupported", "wellbeing", "sos", "compose"
    ]:
        intent = state["route"]["intent"]
        if intent == "answer_pending":
            return "log_incident"  # the only pending question kind
        if intent in ("next_task", "list_tasks", "start_task", "complete_task", "conditions", "task_estimate"):
            return "tasks"
        if intent in ("review_drafts", "confirm_draft", "dismiss_draft", "edit_draft", "affirm"):
            return "drafts"
        if intent == "smalltalk":
            return "compose"
        if intent == "sos_respond":
            return "sos"
        return intent

    async def tasks(state: CocoonState) -> CocoonState:
        """Tasks branch. Bound sessions use their trusted shift's assignments; unbound/legacy sessions keep the
        shared demo queue for next_task. Writes go through the same command executor as the tap route."""
        session = _session(state)
        intent = state["route"]["intent"]
        if intent == "conditions" and not session.shift_id:
            return {"actions": [_conditions_report(session, None)]}
        if intent == "task_estimate" and not session.shift_id:
            action = s.TaskEstimateAction(type="task_estimate", task=None, estimate=None)
            return {"actions": [action.model_dump(mode="json")]}
        if not session.shift_id:
            if intent == "next_task":
                return {"actions": [s.NextTaskAction(type="next_task", task=store.next_task()).model_dump(mode="json")]}
            if intent == "list_tasks":
                action = s.AssignedTasksAction(type="assigned_tasks", scope="all", tasks=[], shift_bound=False)
            else:
                action = s.TaskRejectedAction(type="task_rejected", reason="no_shift",
                                              for_action="task.start" if intent == "start_task" else "task.complete")
            return {"actions": [action.model_dump(mode="json")]}
        assigned = store.list_assigned_tasks(session.shift_id)
        if planner is not None:
            assigned = [planner.enrich(session, t) for t in assigned]
        elif conditions is not None:
            assigned = [t.model_copy(update={"conditions": conditions.check(session, t)}) if t.status != "completed"
                        else t for t in assigned]
        if intent == "task_estimate":
            current = next((t for t in assigned if t.status == "in_progress"), None) or \
                next((t for t in assigned if t.status == "scheduled"), None)
            action = s.TaskEstimateAction(type="task_estimate", task=current,
                                          estimate=(current.start_estimate or current.estimate) if current else None)
            return {"actions": [action.model_dump(mode="json")]}
        if intent == "conditions":
            current = next((t for t in assigned if t.status == "in_progress"), None) or \
                next((t for t in assigned if t.status == "scheduled"), None)
            return {"actions": [_conditions_report(session, current)]}
        if intent in ("next_task", "list_tasks"):
            if intent == "next_task":
                in_progress = [t for t in assigned if t.status == "in_progress"]
                chosen = (in_progress or [t for t in assigned if t.status == "scheduled"])[:1]
            else:
                chosen = assigned
            action = s.AssignedTasksAction(type="assigned_tasks", scope="next" if intent == "next_task" else "all",
                                           tasks=chosen, shift_bound=True)
            return {"actions": [action.model_dump(mode="json")]}
        if intent == "start_task":
            pending = state.get("pending") or {}
            ack_for = pending.get("task_id") if pending.get("kind") == "task_start_ack" else None
            ack = bool(state["route"].get("acknowledge_conditions")) and ack_for is not None
            return _start(state, session, ack_for if ack else None, ack)
        kind = "task.complete"
        command_id = f"{state['turn_id']}:{kind}"
        try:
            result, duplicate = _command(state, kind, f"{kind}:selector",
                                         Store.task_transition(session, kind, None, None))
        except NotFound:
            action = s.TaskRejectedAction(type="task_rejected", for_action=kind, reason="no_eligible_task")
            return {"actions": [action.model_dump(mode="json")]}
        except InvalidTransition as exc:
            action = s.TaskRejectedAction(type="task_rejected", for_action=kind, reason="invalid_transition",
                                          current_status=exc.current_status)
            return {"actions": [action.model_dump(mode="json")]}
        action = s.TaskTransitionAction(type="task_started" if kind == "task.start" else "task_completed",
                                        task=result["task"], command_id=command_id, created=not duplicate)
        return {"actions": [action.model_dump(mode="json")]}

    def _reference(state: CocoonState):
        """Persisted first-receipt time of this turn: relative phrases are interpreted against it, so a retry of the
        same turn can never move an occurrence time."""
        row = store.get_turn(state["session_id"], state["turn_id"])
        return row.created_at if row is not None else utcnow()

    def _occurrence(expression: str | None, reference, offset) -> tuple[OccurrenceTime, str | None]:
        got = interpret(expression, reference, offset)
        if got.status == "none":
            return OccurrenceTime.of_report(reference), None
        if got.status == "resolved":
            return OccurrenceTime(got.occurred_at, got.basis, got.expression, reference), None
        return OccurrenceTime(None, "unresolved", got.expression, reference), got.reason

    def _ask(state: CocoonState, draft: s.IncidentDraft, reason: str | None = None,
             confirm: bool = True) -> CocoonState:
        """Ask for the first fact the saved draft still misses (one question at a time)."""
        field = draft.missing[0]
        pending = s.PendingQuestion(
            kind="incident_severity" if field == "severity" else "incident_time", for_action="log_incident",
            asked_in_turn_id=state["turn_id"], notify_supervisor=draft.notify_supervisor, draft_id=draft.draft_id,
            then_confirm=confirm)
        action = s.InformationRequestedAction(type="information_requested", for_action="log_incident",
                                              missing_field=field, draft=draft,
                                              reason=reason if field == "occurred_time" else None)
        return {"actions": [action.model_dump(mode="json")], "pending": pending.model_dump()}

    def _confirmed(result: dict[str, Any], duplicate: bool) -> list[dict[str, Any]]:
        actions = [_draft_action("incident.confirm", result, created=not duplicate)]
        if result.get("approval"):
            actions.append(s.EscalationRequestedAction(type="escalation_requested", approval=result["approval"],
                                                       created=not duplicate).model_dump(mode="json"))
        return actions

    def _conditions_report(session: s.Session, task: s.AssignedTask | None) -> dict[str, Any]:
        check = task.conditions if task is not None and task.conditions is not None else (
            conditions.check(session, task) if conditions is not None else None)
        if check is None:
            action = s.CapabilityUnavailableAction(type="capability_unavailable", capability="weather_forecast")
            return action.model_dump(mode="json")
        return s.ConditionsReportAction(type="conditions_report", check=check,
                                        task_title=task.title if task else None).model_dump(mode="json")

    def _start(state: CocoonState, session: s.Session, task_id: str | None, ack: bool) -> CocoonState:
        """Start the next scheduled task (or the one waiting for confirmation) through the same command and the same
        working-conditions gate as a tap. A finding that needs acknowledgement leaves one pending question."""
        kind = "task.start"
        command_id = f"{state['turn_id']}:{kind}"
        gate = conditions.gate_for(session) if conditions is not None else None
        fingerprint = f"{kind}:selector" if task_id is None else f"{kind}:{task_id}:ack"
        pending = state.get("pending")
        keep = None if pending and pending.get("kind") == "task_start_ack" else pending
        try:
            result, duplicate = _command(state, kind, fingerprint, Store.task_transition(
                session, kind, task_id, None, gate, acknowledged=ack,
                estimate_for=planner.estimate_fn(session) if planner is not None else None))
        except NotFound:
            action = s.TaskRejectedAction(type="task_rejected", for_action=kind, reason="no_eligible_task")
            return {"actions": [action.model_dump(mode="json")], "pending": keep}
        except InvalidTransition as exc:
            action = s.TaskRejectedAction(type="task_rejected", for_action=kind, reason="invalid_transition",
                                          current_status=exc.current_status)
            return {"actions": [action.model_dump(mode="json")], "pending": keep}
        except ConditionsGate as exc:
            action = s.TaskRejectedAction(type="task_rejected", for_action=kind, reason=exc.reason,
                                          conditions=exc.check, task_id=exc.task.task_id, task_title=exc.task.title)
            if exc.reason == "conditions_need_acknowledgement":
                keep = s.PendingQuestion(kind="task_start_ack", for_action="start_task",
                                         asked_in_turn_id=state["turn_id"], task_id=exc.task.task_id).model_dump()
            return {"actions": [action.model_dump(mode="json")], "pending": keep}
        action = s.TaskTransitionAction(type="task_started", task=result["task"], command_id=command_id,
                                        created=not duplicate, conditions=result.get("conditions"))
        return {"actions": [action.model_dump(mode="json")], "pending": keep}

    async def log_incident(state: CocoonState) -> CocoonState:
        """Ordered child actions: the report, then (if asked) a linked pending supervisor-review request. Each child
        commits with its action record; a failure after the first keeps it and a retry does not repeat it.

        A report needs what happened, a severity (a stated level or an explicit "don't know"; never inferred) and a
        usable time (an interpreted phrase, or the labelled time of the report when none was given). With a fact
        missing, the report is saved as a draft first and the operator is asked, so nothing is lost meanwhile."""
        r = state["route"]
        prior = state.get("pending") if r["intent"] == "answer_pending" else None
        if prior and prior.get("kind") in ("incident_severity", "incident_time"):
            return await _answer_draft_question(state, prior)
        prior = prior or {}
        description = (r.get("incident_description") or "").strip()
        notify = bool(r.get("notify_supervisor") or prior.get("notify_supervisor"))
        severity = r.get("incident_severity") or prior.get("severity")
        severity_unknown = bool(not severity and (r.get("severity_unknown") or prior.get("severity_unknown")))
        location = (r.get("incident_location") or prior.get("location_text") or "").strip()[:300] or None
        time_expression = r.get("incident_time_expression") or prior.get("time_expression")
        if not description:
            action = s.InformationRequestedAction(
                type="information_requested", for_action="log_incident", missing_field="description"
            )
            pending = s.PendingQuestion(
                kind="incident_description", for_action="log_incident", asked_in_turn_id=state["turn_id"],
                notify_supervisor=notify, severity=severity, severity_unknown=severity_unknown,
                location_text=location, time_expression=time_expression,
            )
            return {"actions": [action.model_dump(mode="json")], "pending": pending.model_dump()}
        session = _session(state)
        when, reason = _occurrence(time_expression, _reference(state), store.site_offset(session))
        severity_basis = "reported" if severity else ("stated_unknown" if severity_unknown else None)
        if severity_basis is None or when.basis == "unresolved":
            result, _ = _command(state, "incident.draft", "incident.draft", Store.operator_draft(
                session, state["turn_id"], description[:1000], severity, severity_basis, location, when, notify))
            return _ask(state, s.IncidentDraft.model_validate(result["draft"]), reason)
        result, duplicate = _command(state, "incident.log", "incident.log", Store.incident_report(
            session, state["turn_id"], description[:1000], severity, location, severity_basis=severity_basis,
            when=when))
        actions = [s.IncidentLoggedAction(type="incident_logged", incident=result["incident"],
                                          created=not duplicate and not result.get("reused")).model_dump(mode="json")]
        if notify:
            incident_id = result["record_id"]
            esc, esc_dup = _command(state, "escalation.request", f"escalation.request:{incident_id}",
                                    Store.escalation_request(session, incident_id))
            actions.append(s.EscalationRequestedAction(type="escalation_requested", approval=esc["approval"],
                                                       created=not esc_dup).model_dump(mode="json"))
        return {"actions": actions, "pending": None}

    async def _answer_draft_question(state: CocoonState, pending: dict[str, Any]) -> CocoonState:
        """Complete the saved draft with the operator's answer, then confirm it once nothing is missing. A non-answer
        repeats the question; the draft is never lost."""
        r = state["route"]
        session = _session(state)
        draft = store.get_draft(session.session_id, pending.get("draft_id") or "")
        if draft is None or draft.status != "draft":
            action = s.ClarificationAction(type="clarification_needed", for_action="edit_draft",
                                           reason="nothing_pending")
            return {"actions": [action.model_dump(mode="json")], "pending": None}
        edits: dict[str, Any] = {}
        reason = None
        if pending["kind"] == "incident_severity":
            if r.get("incident_severity"):
                edits["severity"] = r["incident_severity"]
            elif r.get("severity_unknown"):
                edits["severity_unknown"] = True
        elif r.get("incident_time_expression"):
            when, reason = _occurrence(r["incident_time_expression"], _reference(state), store.site_offset(session))
            if when.basis != "unresolved":
                edits["when"] = when
        elif r.get("time_unknown"):  # no time known: keep the report time, labelled as such, with the original phrase
            edits["when"] = OccurrenceTime.of_report(draft.occurred_reference_at or draft.created_at,
                                                     draft.occurred_expression)
        confirm = pending.get("then_confirm", True)
        if not edits:
            if reason:
                draft = draft.model_copy(update={"occurred_expression": r.get("incident_time_expression"),
                                                 "missing": ["occurred_time"]})
            elif pending["kind"] == "incident_time":
                draft = draft.model_copy(update={"missing": ["occurred_time"]})
            return _ask(state, draft, reason, confirm=confirm)
        return _edit_then_confirm(state, session, draft, edits, confirm)

    def _edit_then_confirm(state: CocoonState, session: s.Session, draft: s.IncidentDraft,
                           edits: dict[str, Any], confirm: bool = True) -> CocoonState:
        fingerprint = "incident.edit:" + draft.draft_id + ":" + json.dumps(_edit_key(edits), sort_keys=True)
        edited, duplicate = _command(state, "incident.edit", fingerprint,
                                     Store.incident_transition(session, "incident.edit", draft.draft_id, None, edits))
        updated = s.IncidentDraft.model_validate(edited["draft"])
        if not confirm:
            return {"actions": [_draft_action("incident.edit", edited, created=not duplicate)], "pending": None}
        if updated.status == "draft" and updated.missing:
            return _ask(state, updated)
        result, duplicate = _command(state, "incident.confirm", f"incident.confirm:{draft.draft_id}",
                                     Store.incident_transition(session, "incident.confirm", draft.draft_id, None))
        return {"actions": _confirmed(result, duplicate), "pending": None}

    async def drafts(state: CocoonState) -> CocoonState:
        """Read / confirm / dismiss this session's draft reports. A bare "yes" confirms only when exactly one
        workflow is waiting; otherwise nothing changes and the operator is asked which one."""
        r = state["route"]
        intent = r["intent"]
        if intent == "review_drafts":
            action = s.IncidentDraftsAction(type="incident_drafts", drafts=store.list_drafts(state["session_id"]))
            return {"actions": [action.model_dump(mode="json")]}
        if intent == "edit_draft":
            return _edit_draft(state)
        pending = state.get("pending") or {}
        if intent == "affirm" and sos is not None and sos.answerable(_session(state).operator_id) is not None:
            # a bare "yes" never answers a safety check-in, nor anything else while one is waiting
            out = s.SosAction(type="sos", event="clarify", episode=sos.answerable(_session(state).operator_id))
            return {"actions": [out.model_dump(mode="json")]}
        if intent == "affirm" and pending.get("kind") in ("incident_severity", "incident_time"):
            draft = store.get_draft(state["session_id"], pending.get("draft_id") or "")
            if draft is not None and draft.status == "draft" and draft.missing:
                return _ask(state, draft)  # "yes" is not a severity or a time: ask the same question again
        kind = "incident.dismiss" if intent == "dismiss_draft" else "incident.confirm"
        saved = _saved(state, kind)
        if saved is not None:  # retry of a turn that already committed this
            return {"actions": _confirmed(saved, True) if kind == "incident.confirm"
                    else [_draft_action(kind, saved, created=False)]}
        open_drafts = store.list_drafts(state["session_id"])
        options = [f"draft number {d.draft_number}" for d in open_drafts]
        if intent == "affirm" and pending.get("kind") == "quiz_answer":
            return {"actions": [_unclear(pending)]}
        if intent == "affirm" and pending.get("kind") == "task_start_ack":
            if not open_drafts:  # the only thing waiting is the start confirmation
                return _start(state, _session(state), pending.get("task_id"), True)
            action = s.ClarificationAction(type="clarification_needed", for_action="affirm",
                                           reason="several_candidates",
                                           options=options + ["starting the task despite the conditions"])
            return {"actions": [action.model_dump(mode="json")]}
        if intent == "affirm":
            if state.get("pending"):
                if not open_drafts:  # "yes" is not an answer to "what happened?": ask again, keep the question
                    action = s.InformationRequestedAction(type="information_requested", for_action="log_incident",
                                                          missing_field="description")
                    return {"actions": [action.model_dump(mode="json")]}
                options.append("the incident you started reporting")
            if len(options) != 1 or not open_drafts:
                reason = "several_candidates" if len(options) > 1 else "nothing_pending"
                action = s.ClarificationAction(type="clarification_needed", for_action="affirm", reason=reason,
                                               options=options)
                return {"actions": [action.model_dump(mode="json")]}
            target = open_drafts[0]
        else:
            ref = r.get("incident_number")
            matches = [d for d in open_drafts if ref is None or d.draft_number == ref]
            if len(matches) != 1:
                for_action = "dismiss_draft" if intent == "dismiss_draft" else "confirm_draft"
                reason = "several_candidates" if len(matches) > 1 else "nothing_pending"
                action = s.ClarificationAction(type="clarification_needed", for_action=for_action, reason=reason,
                                               options=options)
                return {"actions": [action.model_dump(mode="json")]}
            target = matches[0]
        if kind == "incident.confirm" and target.missing:
            return _ask(state, target)  # a confirmation needs every fact: ask for the first missing one
        try:
            result, duplicate = _command(state, kind, f"{kind}:{target.draft_id}", Store.incident_transition(
                _session(state), kind, target.draft_id, None))
        except (InvalidTransition, NotFound):  # changed by a tap between the read and the write
            action = s.ClarificationAction(type="clarification_needed", reason="nothing_pending", options=[],
                                           for_action="dismiss_draft" if kind == "incident.dismiss" else "confirm_draft")
            return {"actions": [action.model_dump(mode="json")]}
        if kind == "incident.confirm":
            return {"actions": _confirmed(result, duplicate), "pending": _pending_after(state, target.draft_id)}
        return {"actions": [_draft_action(kind, result, created=not duplicate)],
                "pending": _pending_after(state, target.draft_id)}

    def _pending_after(state: CocoonState, draft_id: str) -> dict[str, Any] | None:
        """A question about a draft that was just confirmed/dismissed is no longer open."""
        pending = state.get("pending")
        return None if pending and pending.get("draft_id") == draft_id else pending

    def _edit_draft(state: CocoonState) -> CocoonState:
        """Voice edit of a draft through the same incident.edit command as a tap (version-checked there)."""
        r = state["route"]
        saved = _saved(state, "incident.edit")
        if saved is not None:
            return {"actions": [_draft_action("incident.edit", saved, created=False)]}
        open_drafts = store.list_drafts(state["session_id"])
        ref = r.get("incident_number")
        matches = [d for d in open_drafts if ref is None or d.draft_number == ref]
        if len(matches) != 1:
            action = s.ClarificationAction(
                type="clarification_needed", for_action="edit_draft",
                reason="several_candidates" if len(matches) > 1 else "nothing_pending",
                options=[f"draft number {d.draft_number}" for d in open_drafts])
            return {"actions": [action.model_dump(mode="json")]}
        target = matches[0]
        session = _session(state)
        edits: dict[str, Any] = {}
        if r.get("incident_severity"):
            edits["severity"] = r["incident_severity"]
        elif r.get("severity_unknown"):
            edits["severity_unknown"] = True
        if r.get("incident_location"):
            edits["location_text"] = r["incident_location"][:300]
        if r.get("incident_time_expression"):
            when, reason = _occurrence(r["incident_time_expression"], _reference(state), store.site_offset(session))
            if when.basis == "unresolved":  # ask again; answering only edits (it does not confirm the draft)
                return _ask(state, target.model_copy(update={"occurred_expression": when.expression,
                                                             "missing": ["occurred_time"]}), reason, confirm=False)
            edits["when"] = when
        if not edits:
            action = s.ClarificationAction(type="clarification_needed", for_action="edit_draft",
                                           reason="nothing_to_change")
            return {"actions": [action.model_dump(mode="json")]}
        fingerprint = "incident.edit:" + target.draft_id + ":" + json.dumps(_edit_key(edits), sort_keys=True)
        try:
            result, duplicate = _command(state, "incident.edit", fingerprint, Store.incident_transition(
                session, "incident.edit", target.draft_id, None, edits))
        except (InvalidTransition, NotFound):
            action = s.ClarificationAction(type="clarification_needed", for_action="edit_draft",
                                           reason="nothing_pending")
            return {"actions": [action.model_dump(mode="json")]}
        return {"actions": [_draft_action("incident.edit", result, created=not duplicate)]}

    async def unsupported(state: CocoonState) -> CocoonState:
        capability = state["route"].get("unsupported_capability") or "requested_capability"
        action = s.CapabilityUnavailableAction(type="capability_unavailable", capability=capability)
        return {"actions": [action.model_dump(mode="json")]}

    def _unclear(pending: dict[str, Any]) -> dict[str, Any]:
        question = None
        if pending.get("attempt_id"):
            row = store._one("SELECT * FROM quiz_attempts WHERE attempt_id = ?", (pending["attempt_id"],))
            if row is not None and lms is not None:
                question = lms_ops.question_view(lms_ops.Lesson.model_validate_json(store._one(
                    "SELECT content_json FROM lesson_versions WHERE lesson_id = ? AND version = ?",
                    (row["lesson_id"], row["lesson_version"]))["content_json"]), row)
        return s.LearningAction(type="learning", event="answer_unclear", question=question,
                                reason="the answer did not match exactly one choice").model_dump(mode="json")

    def _learning(state: CocoonState, kind: str, key: str, mutate) -> CocoonState:
        """Run one LMS command through the turn's command log and keep the quiz question pending when one is asked."""
        pending = state.get("pending")
        keep = None if pending and pending.get("kind") == "quiz_answer" else pending
        try:
            result, _ = _command(state, kind, f"{kind}:{key}", mutate)
        except (NotFound, InvalidTransition, InvalidInput, VersionConflict) as exc:
            action = s.LearningAction(type="learning", event="rejected", reason=str(exc))
            return {"actions": [action.model_dump(mode="json")], "pending": pending}
        action = s.LearningAction.model_validate(result["learning"])
        if action.question is not None and action.event != "assessment_finished":
            q = action.question
            keep = s.PendingQuestion(kind="quiz_answer", for_action="answer_quiz", asked_in_turn_id=state["turn_id"],
                                     attempt_id=q.attempt_id, question_id=q.question_id,
                                     choices=q.choices).model_dump()
        return {"actions": [action.model_dump(mode="json")], "pending": keep}

    async def training(state: CocoonState) -> CocoonState:
        session = _session(state)
        r = state["route"]
        act = r.get("training_action")
        lessons = store.list_lessons()
        if lms is not None and act not in (None, "assign", "status", "read"):
            if act in ("needs", "progress"):
                learner = lms_ops.learner_view(store, lms, session, planner.catalog.operator_skill.get(
                    session.operator_id) if planner is not None and planner.catalog else None)
                if learner is None:
                    action = s.LearningAction(type="learning", event="rejected",
                                              reason="lessons need a verified operator session")
                else:
                    action = s.LearningAction(type="learning", event="training_needs" if act == "needs" else "progress",
                                              learner=learner)
                return {"actions": [action.model_dump(mode="json")]}
            if act == "answer":
                pending = state.get("pending") or {}
                if pending.get("kind") != "quiz_answer":
                    action = s.LearningAction(type="learning", event="rejected", reason="no quiz question is waiting")
                    return {"actions": [action.model_dump(mode="json")]}
                if not r.get("quiz_choice_id"):
                    return {"actions": [_unclear(pending)]}
                return _learning(state, "quiz.answer", pending["question_id"], lms_ops.answer(
                    lms, session, pending["attempt_id"], pending["question_id"], r["quiz_choice_id"], "voice"))
            active = lms_ops.active_learning(store, session) or {}
            lesson_id = r.get("lesson_id") or active.get("lesson_id")
            if act == "start" and lesson_id is None:  # "start my lesson": an outstanding assignment, else a suggestion
                learner = lms_ops.learner_view(store, lms, session, None)
                lesson_id = learner.recommended[0] if learner and learner.recommended else None
            if lesson_id is None:
                action = s.LearningAction(type="learning", event="rejected", reason="no lesson is in progress")
                return {"actions": [action.model_dump(mode="json")]}
            if act == "start":
                machine = planner.catalog.machines.get(session.machine_id) if planner and planner.catalog else None
                return _learning(state, "lesson.start", lesson_id, lms_ops.start_lesson(
                    lms, session, lesson_id, machine.category if machine else None))
            if act == "quiz":
                return _learning(state, "quiz.start", lesson_id, lms_ops.start_quiz(lms, session, lesson_id))
            until = None
            if act == "defer":
                until = (store.data_clock(session.session_id) or utcnow()) + timedelta(minutes=30)
            return _learning(state, f"lesson.{act}", lesson_id, lms_ops.lesson_step(lms, session, lesson_id, act,
                                                                                     None, until))
        if r.get("training_action") == "read":
            assignments = store.list_assignments(session)
            readable = {l.lesson_id for l in lessons if l.content_text}
            # the lesson they named, else their most recent assignment that has readable text
            lesson_id = r.get("lesson_id") or next(
                (a.lesson_id for a in reversed(assignments) if a.lesson_id in readable), None)
            lesson = store.get_lesson(lesson_id) if lesson_id else None
            if lesson is None:
                action = s.ClarificationAction(type="clarification_needed", for_action="read_lesson",
                                               reason="nothing_pending")
                return {"actions": [action.model_dump(mode="json")]}
            assignment = next((a for a in assignments if a.lesson_id == lesson.lesson_id), None)
            action = s.LessonContentAction(type="lesson_content", lesson=lesson, assignment=assignment)
            return {"actions": [action.model_dump(mode="json")]}
        if r.get("training_action") == "assign":
            assigned = {a.lesson_id for a in store.list_assignments(session)}
            lesson_id = r.get("lesson_id") or next((l.lesson_id for l in lessons if l.lesson_id not in assigned), None)
            if lesson_id and store.get_lesson(lesson_id):
                assignment, created = store.assign_training(session, lesson_id, state["turn_id"])
                if assignment is not None:  # None: a withheld legacy record holds this pair; report status instead
                    action = s.TrainingAssignedAction(type="training_assigned", assignment=assignment, created=created)
                    return {"actions": [action.model_dump(mode="json")]}
        action = s.TrainingStatusAction(
            type="training_status", assignments=store.list_assignments(session), available_lessons=lessons
        )
        return {"actions": [action.model_dump(mode="json")]}

    async def explain_alert(state: CocoonState) -> CocoonState:
        """Explain the warning the operator refers to, from that episode's saved evidence (never the latest readings).
        Reference: the warning they named; else the one warning announced since their previous turn; else the one
        active announced warning; else the latest announced one. Competing candidates get a question, not a guess."""
        sid = state["session_id"]
        announced = store.announced_alerts(sid)  # newest announcement first
        hint = state["route"].get("alert_hint")
        families = {"seatbelt": {"seatbelt_unfastened"}, "idle": {"prolonged_idle", "idle_unbelted"},
                    "weather": {"working_conditions"}, "proximity": {"proximity"},
                    "motion": {"sudden_start", "sudden_stop"}, "slope": {"steep_slope"},
                    "fuel": {"abnormal_fuel_per_cycle"}, "repeat": {"repeated_violations"}}
        if hint:
            # A linked combined episode (e.g. idle + unbelted) was covered by its parent's announcement; it is still
            # explainable on its own evidence when the operator names it.
            pool = [x for x in announced + store.linked_alerts(sid) if x[0].alert_type in families[hint]]
            active = [x for x in pool if x[0].status == "active"]
            candidates = (active or pool)[:1]
        else:
            since = store.previous_turn_at(sid, state["turn_id"])
            recent = [x for x in announced if since is not None and x[2] > since]
            active = [x for x in announced if x[0].status == "active"]
            candidates = recent if recent else (active if active else announced[:1])
        if len(candidates) > 1:
            names = {"seatbelt_unfastened": "the seatbelt warning", "prolonged_idle": "the idling warning",
                     "idle_unbelted": "the idling warning"}
            options = list(dict.fromkeys(names.get(x[0].alert_type, f"the {x[0].alert_type.replace('_', ' ')} warning")
                                         for x in candidates))
            action = s.ClarificationAction(type="clarification_needed", for_action="explain_alert",
                                           reason="several_candidates", options=options)
            return {"actions": [action.model_dump(mode="json")]}
        if not candidates:
            action = s.AlertExplainedAction(type="alert_explained", alert=None)
            return {"actions": [action.model_dump(mode="json")]}
        alert, event_id, _ = candidates[0]
        action = s.AlertExplainedAction(type="alert_explained", alert=alert, announcement_event_id=event_id,
                                        deliveries=store.deliveries(event_id),
                                        related_alerts=store.related_alerts(alert.alert_id))
        return {"actions": [action.model_dump(mode="json")], "latest_alert": alert.model_dump(mode="json")}

    async def record_idle_reason(state: CocoonState) -> CocoonState:
        """Record why the operator is idling. Acknowledgement, delivery and the condition clearing stay separate facts:
        this never clears the idle or belt episode."""
        text = (state["route"].get("idle_reason") or state["user_text"]).strip()[:300]
        result, duplicate = _command(state, "idle.record_reason", "idle.record_reason",
                                     Store.idle_reason(_session(state), state["turn_id"], text))
        action = s.IdleReasonRecordedAction(
            type="idle_reason_recorded", reason_id=result["reason_id"], reason_text=result["reason_text"],
            alert_id=result["alert_id"], belt_warning_active=result["belt_warning_active"], created=not duplicate)
        return {"actions": [action.model_dump(mode="json")]}

    async def wellbeing_node(state: CocoonState) -> CocoonState:
        """Breaks (explicit operator statements only), the operator's own advice explanation and a read-only consent
        summary. Consent is never granted or revoked by voice."""
        session = _session(state)
        action = state["route"].get("wellbeing_action")
        if wellbeing is None or session.binding_status != "catalog_verified":
            unavailable = s.CapabilityUnavailableAction(type="capability_unavailable", capability="wellbeing_checks")
            return {"actions": [unavailable.model_dump(mode="json")]}
        if action in ("break_start", "break_end"):
            kind = "break.start" if action == "break_start" else "break.end"
            at = conditions.data_time(session) if conditions is not None else utcnow()
            try:
                result, duplicate = _command(state, kind, kind, wellbeing.break_mutation(session, kind, None, at))
            except InvalidTransition as exc:
                out = s.WellbeingAction(type="wellbeing", event="break_rejected", reason=str(exc))
                return {"actions": [out.model_dump(mode="json")]}
            out = s.WellbeingAction(type="wellbeing", event="break_started" if kind == "break.start" else "break_ended",
                                    break_record=result["break_record"], created=not duplicate)
        elif action == "consent_status":
            out = s.WellbeingAction(type="wellbeing", event="consent_status",
                                    consents=wellbeing.consent_state(session.operator_id).consents)
        else:
            advice = wellbeing.latest_advice(session.operator_id)
            out = s.WellbeingAction(type="wellbeing", event="advice_explained" if advice else "no_advice",
                                    advice=advice)
        return {"actions": [out.model_dump(mode="json")]}

    async def sos_node(state: CocoonState) -> CocoonState:
        """Answer the operator's own check-in (the open one, else a notified one without a late answer). The pending
        question (an incident or quiz in progress) is left untouched."""
        session = _session(state)
        response = state["route"].get("sos_response")
        saved = _saved(state, "sos.respond")
        if saved is not None:
            out = s.SosAction(type="sos", event=saved["event"], episode=saved["sos"], created=False)
            return {"actions": [out.model_dump(mode="json")]}
        target = sos.answerable(session.operator_id) if sos is not None and response else None
        if target is None:
            return {"actions": [s.SosAction(type="sos", event="no_open_checkin").model_dump(mode="json")]}
        try:
            result, duplicate = _command(state, "sos.respond", f"sos.respond:{target.episode_id}:{response}",
                                         sos.respond_mutation(session, target.episode_id, response))
        except (InvalidTransition, NotFound) as exc:
            return {"actions": [s.SosAction(type="sos", event="rejected", reason=str(exc)).model_dump(mode="json")]}
        out = s.SosAction(type="sos", event=result["event"], episode=result["sos"], created=not duplicate)
        return {"actions": [out.model_dump(mode="json")]}

    async def cancel_pending(state: CocoonState) -> CocoonState:
        """Stop asking. A report already saved as a draft stays saved (it can be finished or dismissed later)."""
        pending = state.get("pending")
        kept = store.get_draft(state["session_id"], pending["draft_id"]) if pending and pending.get("draft_id") else None
        action = s.PendingCancelledAction(
            type="pending_cancelled", cancelled=pending["for_action"] if pending else None,
            kept_draft_number=kept.draft_number if kept is not None and kept.status == "draft" else None)
        return {"actions": [action.model_dump(mode="json")], "pending": None}

    async def compose(state: CocoonState) -> CocoonState:
        speech = await brain.compose(_ctx(state), state.get("actions", []))
        return {"speech": speech, "messages": [AIMessage(content=speech, id=f"ai:{state['turn_id']}")]}

    g = StateGraph(CocoonState)
    g.add_node("load_context", load_context)
    g.add_node("route", route)
    g.add_node("tasks", tasks)
    g.add_node("log_incident", log_incident)
    g.add_node("drafts", drafts)
    g.add_node("unsupported", unsupported)
    g.add_node("record_idle_reason", record_idle_reason)
    g.add_node("training", training)
    g.add_node("explain_alert", explain_alert)
    g.add_node("cancel_pending", cancel_pending)
    g.add_node("wellbeing", wellbeing_node)
    g.add_node("sos", sos_node)
    g.add_node("compose", compose)
    g.add_edge(START, "load_context")
    g.add_edge("load_context", "route")
    g.add_conditional_edges("route", choose)
    for node in ("tasks", "log_incident", "drafts", "training", "explain_alert", "record_idle_reason", "cancel_pending",
                 "unsupported", "wellbeing", "sos"):
        g.add_edge(node, "compose")
    g.add_edge("compose", END)
    return g


_DRAFT_ACTION_TYPES = {"incident.confirm": "incident_confirmed", "incident.dismiss": "incident_dismissed",
                       "incident.edit": "incident_draft_edited"}


def _draft_action(kind: str, result: dict[str, Any], created: bool) -> dict[str, Any]:
    return s.IncidentDraftAction(type=_DRAFT_ACTION_TYPES[kind], draft=result["draft"], incident=result.get("incident"),
                                 created=created).model_dump(mode="json")


def _edit_key(edits: dict[str, Any]) -> dict[str, Any]:
    """Stable fingerprint form of a draft edit (an interpreted time is compared by its stored values)."""
    out: dict[str, Any] = {}
    for key, value in edits.items():
        if isinstance(value, OccurrenceTime):
            value = [iso(value.occurred_at), value.basis, value.expression, iso(value.reference_at)]
        out[key] = value
    return out
