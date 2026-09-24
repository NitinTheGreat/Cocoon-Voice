from __future__ import annotations

import pytest

from cocoon_agent.graph.brain import LLMUnavailable, MockBrain, TurnContext, clean_speech

PENDING = {"kind": "incident_description", "for_action": "log_incident", "asked_in_turn_id": "t1"}


@pytest.mark.parametrize("text,intent,desc", [
    ("What's my next task?", "next_task", None),
    ("Log an incident: hydraulic hose leaking on the boom", "log_incident", "hydraulic hose leaking on the boom"),
    ("I want to report that the left track is loose", "log_incident", "the left track is loose"),
    ("report an incident", "log_incident", None),
    ("Why did you warn me?", "explain_alert", None),
    ("why?", "explain_alert", None),
    ("good morning", "smalltalk", None),
])
async def test_mock_routes(text, intent, desc):
    d = await MockBrain().route(TurnContext(text=text))
    assert d.intent == intent
    assert d.incident_description == desc


async def test_pending_answer_and_cancel():
    brain = MockBrain()
    assert (await brain.route(TurnContext(text="Tyre is flat", pending=PENDING))).intent == "answer_pending"
    assert (await brain.route(TurnContext(text="never mind", pending=PENDING))).intent == "cancel_pending"


@pytest.mark.parametrize("text,lesson,action", [
    ("assign me lesson two", "L2", "assign"),
    ("start the seatbelt training", "L1", "start"),  # C4: "start" opens the lesson; "assign" still assigns
    ("what training do I have", None, "status"),
])
async def test_mock_training(text, lesson, action):
    d = await MockBrain().route(TurnContext(text=text))
    assert (d.intent, d.lesson_id, d.training_action) == ("training", lesson, action)


def test_clean_speech_keeps_structure_out_of_tts():
    assert clean_speech("  **Done.**  Incident `3` logged ") == "Done. Incident 3 logged"
    with pytest.raises(LLMUnavailable):
        clean_speech('{"intent": "log_incident"}')
