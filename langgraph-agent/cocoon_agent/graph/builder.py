"""Typed LangGraph StateGraph: load_context -> route -> action | ask -> compose.

Thread id == application session_id. Messages carry ids derived from turn_id, so
re-running an interrupted turn replaces rather than duplicates graph memory.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from ..api import schemas as s
from ..store import InvalidTransition, NotFound, Store
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


def build_graph(store: Store, brain: Brain):
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
        )

    async def load_context(state: CocoonState) -> CocoonState:
        # The database is authoritative for alerts; mirror the latest into graph state.
        alert = store.latest_alert(state["session_id"])
        return {"latest_alert": alert.model_dump(mode="json") if alert else None}

    async def route(state: CocoonState) -> CocoonState:
        decision = await brain.route(_ctx(state))
        if decision.intent in ("answer_pending", "cancel_pending") and not state.get("pending"):
            decision.intent = "smalltalk"
        return {"route": decision.model_dump()}

    def choose(state: CocoonState) -> Literal[
        "tasks", "log_incident", "training", "explain_alert", "cancel_pending", "compose"
    ]:
        intent = state["route"]["intent"]
        if intent == "answer_pending":
            return "log_incident"  # the only pending question kind in v1
        if intent in ("next_task", "list_tasks", "start_task", "complete_task"):
            return "tasks"
        if intent == "smalltalk":
            return "compose"
        return intent

    async def tasks(state: CocoonState) -> CocoonState:
        """Tasks branch. Bound sessions use their trusted shift's assignments; unbound/legacy sessions keep the
        shared demo queue for next_task. Writes go through the same command executor as the tap route."""
        session = _session(state)
        intent = state["route"]["intent"]
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
        if intent in ("next_task", "list_tasks"):
            if intent == "next_task":
                in_progress = [t for t in assigned if t.status == "in_progress"]
                chosen = (in_progress or [t for t in assigned if t.status == "scheduled"])[:1]
            else:
                chosen = assigned
            action = s.AssignedTasksAction(type="assigned_tasks", scope="next" if intent == "next_task" else "all",
                                           tasks=chosen, shift_bound=True)
            return {"actions": [action.model_dump(mode="json")]}
        kind = "task.start" if intent == "start_task" else "task.complete"
        command_id = f"{state['turn_id']}:{kind}"
        try:
            result, duplicate = store.run_command(
                scope=f"turn:{session.session_id}", command_id=command_id, kind=kind,
                fingerprint=f"{kind}:selector", session_id=session.session_id, turn_id=state["turn_id"],
                mutate=Store.task_transition(session, kind, None, None))
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

    async def log_incident(state: CocoonState) -> CocoonState:
        description = (state["route"].get("incident_description") or "").strip()
        if not description:
            action = s.InformationRequestedAction(
                type="information_requested", for_action="log_incident", missing_field="description"
            )
            pending = s.PendingQuestion(
                kind="incident_description", for_action="log_incident", asked_in_turn_id=state["turn_id"]
            )
            return {"actions": [action.model_dump(mode="json")], "pending": pending.model_dump()}
        incident, created = store.create_incident(_session(state), description[:1000], state["turn_id"])
        action = s.IncidentLoggedAction(type="incident_logged", incident=incident, created=created)
        return {"actions": [action.model_dump(mode="json")], "pending": None}

    async def training(state: CocoonState) -> CocoonState:
        session = _session(state)
        r = state["route"]
        lessons = store.list_lessons()
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
        alert = store.latest_alert(state["session_id"])
        action = s.AlertExplainedAction(type="alert_explained", alert=alert)
        return {"actions": [action.model_dump(mode="json")]}

    async def cancel_pending(state: CocoonState) -> CocoonState:
        pending = state.get("pending")
        action = s.PendingCancelledAction(type="pending_cancelled", cancelled=pending["for_action"] if pending else None)
        return {"actions": [action.model_dump(mode="json")], "pending": None}

    async def compose(state: CocoonState) -> CocoonState:
        speech = await brain.compose(_ctx(state), state.get("actions", []))
        return {"speech": speech, "messages": [AIMessage(content=speech, id=f"ai:{state['turn_id']}")]}

    g = StateGraph(CocoonState)
    g.add_node("load_context", load_context)
    g.add_node("route", route)
    g.add_node("tasks", tasks)
    g.add_node("log_incident", log_incident)
    g.add_node("training", training)
    g.add_node("explain_alert", explain_alert)
    g.add_node("cancel_pending", cancel_pending)
    g.add_node("compose", compose)
    g.add_edge(START, "load_context")
    g.add_edge("load_context", "route")
    g.add_conditional_edges("route", choose)
    for node in ("tasks", "log_incident", "training", "explain_alert", "cancel_pending"):
        g.add_edge(node, "compose")
    g.add_edge("compose", END)
    return g
