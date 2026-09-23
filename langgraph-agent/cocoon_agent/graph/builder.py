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
from ..store import Store
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
        "next_task", "log_incident", "training", "explain_alert", "cancel_pending", "compose"
    ]:
        intent = state["route"]["intent"]
        if intent == "answer_pending":
            return "log_incident"  # the only pending question kind in v1
        if intent == "smalltalk":
            return "compose"
        return intent

    async def next_task(state: CocoonState) -> CocoonState:
        task = store.next_task()
        action = s.NextTaskAction(type="next_task", task=task)
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
            assigned = {a.lesson_id for a in store.list_assignments(session.operator_id)}
            lesson_id = r.get("lesson_id") or next((l.lesson_id for l in lessons if l.lesson_id not in assigned), None)
            if lesson_id and store.get_lesson(lesson_id):
                assignment, created = store.assign_training(session, lesson_id, state["turn_id"])
                action = s.TrainingAssignedAction(type="training_assigned", assignment=assignment, created=created)
                return {"actions": [action.model_dump(mode="json")]}
        action = s.TrainingStatusAction(
            type="training_status", assignments=store.list_assignments(session.operator_id), available_lessons=lessons
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
    g.add_node("next_task", next_task)
    g.add_node("log_incident", log_incident)
    g.add_node("training", training)
    g.add_node("explain_alert", explain_alert)
    g.add_node("cancel_pending", cancel_pending)
    g.add_node("compose", compose)
    g.add_edge(START, "load_context")
    g.add_edge("load_context", "route")
    g.add_conditional_edges("route", choose)
    for node in ("next_task", "log_incident", "training", "explain_alert", "cancel_pending"):
        g.add_edge(node, "compose")
    g.add_edge("compose", END)
    return g
