"""Analytics workflow (LangChain runnables): stats and topics in parallel, then a summary.

    input {employee, sessions, interactions, now}
      -> RunnableParallel(stats=compute_stats, topics=classify_topics)
      -> summary (mock: deterministic template | live: prompt -> Gemini on Vertex AI -> text)
      -> report (JSON-safe dict, stored in insights_reports)

Mode follows COCOON_LLM_MODE, like the rest of the backend. Mock makes no provider calls. Live never falls back
to mock: a provider failure leaves summary.status = "unavailable" with the reason, and the charts still render
from the measured stats.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda, RunnableParallel

from ..config import Settings

log = logging.getLogger("cocoon_agent.insights.workflow")

# Backend action types map straight to a topic; otherwise keywords decide (first match wins).
ACTION_TOPICS = {
    "next_task": "tasks", "incident_logged": "incidents", "information_requested": "incidents",
    "training_assigned": "training", "training_status": "training", "alert_explained": "safety_alerts",
    "pending_cancelled": "tasks",
}
KEYWORD_TOPICS: list[tuple[str, tuple[str, ...]]] = [
    ("incidents", ("incident", "accident", "damage", "injur", "report a", "report an", "broke", "spill")),
    ("safety_alerts", ("warn", "alert", "seatbelt", "seat belt", "safety", "hazard", "danger", "why did")),
    ("training", ("training", "lesson", "course", "learn", "certif", "quiz")),
    ("tasks", ("task", "next", "schedule", "assign", "job", "shift", "work order")),
    ("maintenance", ("inspect", "check", "maintenance", "service", "oil", "hydraulic", "tire", "tyre", "track",
                     "engine", "fluid", "filter", "grease", "walkaround", "walk around", "coolant", "fuel")),
    ("operation", ("how do", "how to", "operate", "dig", "load", "lift", "bucket", "blade", "start", "drive")),
]
TOPIC_LABELS = {
    "tasks": "Tasks & schedule", "incidents": "Incidents", "safety_alerts": "Safety & alerts",
    "training": "Training", "maintenance": "Maintenance & inspection", "operation": "Operating the machine",
    "other": "Other",
}


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def topic_of(question: str, action_types: list[str]) -> str:
    for action in action_types:
        if action in ACTION_TOPICS:
            return ACTION_TOPICS[action]
    text = question.lower()
    for topic, words in KEYWORD_TOPICS:
        if any(w in text for w in words):
            return topic
    return "other"


def compute_stats(inp: dict[str, Any]) -> dict[str, Any]:
    interactions, sessions, now = inp["interactions"], inp["sessions"], inp["now"]
    days = [now.date() - timedelta(days=d) for d in range(13, -1, -1)]
    per_day = Counter(i["asked_at"].astimezone(timezone.utc).date() for i in interactions)
    per_hour = Counter(i["asked_at"].astimezone(timezone.utc).hour for i in interactions)
    actions = Counter(a for i in interactions for a in i["action_types"])
    statuses = Counter(i["status"] for i in interactions)
    normalized = Counter(re.sub(r"[^a-z0-9 ]", "", i["question"].lower()).strip() for i in interactions)
    total = len(interactions)
    return {
        "total_sessions": len(sessions),
        "total_questions": total,
        "answered": statuses.get("completed", 0),
        "not_confirmed": total - statuses.get("completed", 0),
        "days_active": len(per_day),
        "avg_questions_per_session": round(total / len(sessions), 1) if sessions else 0.0,
        "first_seen": _iso(min((s["started_at"] for s in sessions), default=None)),
        "last_seen": _iso(max((i["asked_at"] for i in interactions), default=None)),
        "questions_per_day": [{"date": d.isoformat(), "count": per_day.get(d, 0)} for d in days],
        "questions_by_hour_utc": [{"hour": h, "count": per_hour.get(h, 0)} for h in range(24)],
        "actions": [{"type": k, "count": v} for k, v in actions.most_common()],
        "top_questions": [{"question": q, "count": c} for q, c in normalized.most_common(5) if q],
    }


def classify_topics(inp: dict[str, Any]) -> dict[str, Any]:
    counts = Counter(topic_of(i["question"], i["action_types"]) for i in inp["interactions"])
    total = sum(counts.values()) or 1
    return {
        "distribution": [{"topic": t, "label": TOPIC_LABELS[t], "count": c, "share": round(c / total, 3)}
                         for t, c in counts.most_common()],
        "top_topic": counts.most_common(1)[0][0] if counts else None,
    }


def mock_summary(inp: dict[str, Any]) -> dict[str, Any]:
    stats, topics, employee = inp["stats"], inp["topics"], inp["employee"]
    if stats["total_questions"] == 0:
        text = "No questions recorded yet. Start a voice session to build a history."
        recs: list[str] = []
    else:
        top = topics["distribution"][0]
        text = (f"{stats['total_questions']} questions across {stats['total_sessions']} session(s) on "
                f"{stats['days_active']} day(s); most were about {top['label'].lower()} ({round(top['share'] * 100)}%).")
        shares = {d["topic"]: d["share"] for d in topics["distribution"]}
        recs = []
        if shares.get("safety_alerts", 0) >= 0.2:
            recs.append("Frequent safety-alert questions: review the seatbelt and alert procedures together.")
        if shares.get("incidents", 0) > 0:
            recs.append("Incidents were reported: check that each one has been followed up.")
        if shares.get("training", 0) == 0:
            recs.append("No training questions yet: consider assigning a lesson for the selected machine.")
        if stats["not_confirmed"]:
            recs.append(f"{stats['not_confirmed']} question(s) were not confirmed by the system; ask them again.")
    machine = employee.get("machine_model") or employee.get("machine_id") if employee else None
    return {"status": "ok", "source": "mock (deterministic template, no model call)", "text": text,
            "recommendations": recs, "machine": machine}


SUMMARY_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You write short, factual usage summaries for a heavy-equipment operator's voice assistant. "
               "Use only the numbers and questions given. No invented facts. Plain text."),
    ("human", "Operator machine: {machine}\nStats: {stats}\nTopics: {topics}\nRecent questions:\n{questions}\n\n"
              "Write 2-3 sentences summarising what the operator asks about, then up to 3 lines starting with "
              "'- ' giving practical recommendations."),
])


def build_live_summary(settings: Settings) -> Runnable:
    from google import genai
    from google.genai import types

    client = genai.Client(vertexai=True, project=settings.google_cloud_project, location=settings.google_cloud_location,
                          http_options=types.HttpOptions(timeout=int(settings.llm_timeout_seconds * 1000)))

    async def call_vertex(prompt_value) -> str:
        messages = prompt_value.to_messages()
        system = "\n".join(m.content for m in messages if m.type == "system")
        user = "\n".join(m.content for m in messages if m.type != "system")
        resp = await client.aio.models.generate_content(
            model=settings.vertex_model, contents=user,
            config=types.GenerateContentConfig(system_instruction=system, temperature=0.3, max_output_tokens=400))
        return resp.text or ""

    def prompt_inputs(inp: dict[str, Any]) -> dict[str, Any]:
        employee = inp["employee"] or {}
        questions = "\n".join(f"- {i['question']}" for i in inp["interactions"][:30]) or "(none)"
        stats = {k: inp["stats"][k] for k in ("total_sessions", "total_questions", "answered", "days_active")}
        topics = [(d["label"], d["count"]) for d in inp["topics"]["distribution"]]
        return {"machine": employee.get("machine_model") or employee.get("machine_id") or "unknown",
                "stats": stats, "topics": topics, "questions": questions}

    chain = RunnableLambda(prompt_inputs) | SUMMARY_PROMPT | RunnableLambda(call_vertex) | StrOutputParser()

    async def summarize(inp: dict[str, Any]) -> dict[str, Any]:
        if not inp["interactions"]:
            return {"status": "ok", "source": f"live ({settings.vertex_model})", "text": "No questions recorded yet.",
                    "recommendations": []}
        try:
            text = (await chain.ainvoke(inp)).strip()
        except Exception as exc:  # quota (429), auth, timeout...: reported, never replaced by mock text
            log.warning("insights live summary unavailable: %s", type(exc).__name__)
            return {"status": "unavailable", "source": f"live ({settings.vertex_model})",
                    "reason": f"{type(exc).__name__}: {str(exc)[:160]}", "text": None, "recommendations": []}
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        recs = [ln[2:].strip() for ln in lines if ln.startswith("- ")]
        body = " ".join(ln for ln in lines if not ln.startswith("- "))
        return {"status": "ok", "source": f"live ({settings.vertex_model})", "text": body, "recommendations": recs}

    return RunnableLambda(summarize)


def build_insights_chain(settings: Settings) -> Runnable:
    summary = RunnableLambda(mock_summary) if settings.llm_mode != "live" else build_live_summary(settings)
    analyse = RunnableParallel(
        employee=RunnableLambda(lambda x: x["employee"]),
        interactions=RunnableLambda(lambda x: x["interactions"]),
        stats=RunnableLambda(compute_stats),
        topics=RunnableLambda(classify_topics),
    )

    async def assemble(inp: dict[str, Any]) -> dict[str, Any]:
        result = await summary.ainvoke(inp)
        employee = inp["employee"]
        return {
            "employee": {k: _iso(v) for k, v in employee.items()} if employee else None,
            "stats": inp["stats"],
            "topics": inp["topics"],
            "summary": result,
            "recent_questions": [
                {"question": i["question"], "reply": i["reply"], "status": i["status"], "session_id": i["session_id"],
                 "asked_at": _iso(i["asked_at"]), "topic": topic_of(i["question"], i["action_types"])}
                for i in inp["interactions"][:10]],
            "llm_mode": settings.llm_mode,
        }

    return analyse | RunnableLambda(assemble)
