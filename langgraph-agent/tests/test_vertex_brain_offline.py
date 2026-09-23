"""Gemini-on-Vertex brain, checked offline with a fake google-genai client.

Proves request configuration, load bounds, retry policy and failure mapping only. It is NOT a live provider test
(see docs/HANDOFF.md for the recorded live runs).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

from cocoon_agent.api.app import create_app
from cocoon_agent.graph.brain import (
    ComposedSpeech, LLMUnavailable, RouteDecision, TurnContext, VertexBrain, build_brain,
)

from .conftest import AUTH, make_settings, new_session

CAPACITY = genai_errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                   "message": "Resource exhausted. Please try again later."}})


class FakeModels:
    """Returns (or raises) the scripted outcomes in order; the last one repeats."""

    def __init__(self, *outcomes, delay: float = 0.0):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.delay = delay
        self.in_flight = self.max_in_flight = 0

    async def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.in_flight -= 1


def _response(parsed, finish="STOP"):
    return SimpleNamespace(parsed=parsed, candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))])


def _brain(tmp_path, *outcomes, delay: float = 0.0, **overrides) -> tuple[VertexBrain, FakeModels, list[float]]:
    settings = make_settings(tmp_path, COCOON_LLM_MODE="live", GOOGLE_CLOUD_PROJECT="test-project", **overrides)
    models = FakeModels(*outcomes, delay=delay)
    brain = VertexBrain(settings, client=SimpleNamespace(aio=SimpleNamespace(models=models)))
    sleeps: list[float] = []

    async def no_wait(seconds: float) -> None:
        sleeps.append(seconds)

    brain._sleep = no_wait
    return brain, models, sleeps


async def test_route_uses_the_configured_model_and_structured_output(tmp_path):
    brain, models, _ = _brain(tmp_path, _response(RouteDecision(intent="next_task")))
    decision = await brain.route(TurnContext(text="what's my next job"))
    assert decision.intent == "next_task"
    call = models.calls[0]
    assert call["model"] == "gemini-3.8-flash"
    cfg = call["config"]
    assert cfg.response_mime_type == "application/json" and cfg.response_schema is RouteDecision
    assert cfg.thinking_config.thinking_level.value.lower() == "low"
    assert cfg.automatic_function_calling.disable is True
    assert '"utterance": "what\'s my next job"' in call["contents"]


async def test_template_compose_makes_no_model_call(tmp_path):
    brain, models, _ = _brain(tmp_path, _response(None))
    speech = await brain.compose(TurnContext(text="x"), [{"type": "information_requested",
                                                          "for_action": "log_incident", "missing_field": "description"}])
    assert speech == "Okay, I'll log an incident. What happened?" and models.calls == []


async def test_model_compose_is_still_available_and_cleaned(tmp_path):
    brain, models, _ = _brain(tmp_path, _response(ComposedSpeech(speech="Your next task is **grading**.")),
                              COCOON_LLM_COMPOSE="model")
    assert await brain.compose(TurnContext(text="x"), []) == "Your next task is grading ."
    assert len(models.calls) == 1


async def test_capacity_errors_are_retried_with_bounded_backoff(tmp_path):
    brain, models, sleeps = _brain(tmp_path, CAPACITY, _response(RouteDecision(intent="next_task")))
    assert (await brain.route(TurnContext(text="next task"))).intent == "next_task"
    assert len(models.calls) == 2 and len(sleeps) == 1 and 0.5 <= sleeps[0] <= 1.0


async def test_retry_budget_is_bounded_and_uses_the_provider_hint(tmp_path):
    hinted = genai_errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "x",
                                                      "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                                   "retryDelay": "30s"}]}})
    brain, models, sleeps = _brain(tmp_path, hinted)
    with pytest.raises(LLMUnavailable, match="capacity is exhausted"):
        await brain.route(TurnContext(text="next task"))
    assert len(models.calls) == 3  # VERTEX_MAX_ATTEMPTS default
    assert sleeps == [4.0, 4.0]  # the 30 s hint is capped so the turn deadline still holds


@pytest.mark.parametrize("error,message", [
    (genai_errors.ClientError(403, {"error": {"message": "denied"}}), "rejected the credentials"),
    (genai_errors.ClientError(400, {"error": {"message": "bad thinking level"}}), "returned HTTP 400"),
    (genai_errors.ClientError(404, {"error": {"message": "no model"}}), "returned HTTP 404"),
])
async def test_configuration_errors_are_not_retried(tmp_path, error, message):
    brain, models, sleeps = _brain(tmp_path, error)
    with pytest.raises(LLMUnavailable, match=message):
        await brain.route(TurnContext(text="hello"))
    assert len(models.calls) == 1 and sleeps == []


@pytest.mark.parametrize("outcome,message", [
    (genai_errors.ServerError(503, {"error": {"message": "down"}}), "returned HTTP 503"),
    (genai_errors.ClientError(499, {"error": {"status": "CANCELLED", "message": "x"}}), "exceeded its deadline"),
    (TimeoutError(), "could not reach Vertex AI"),
    (_response(None, "SAFETY"), "declined"),
    (_response(None, "MAX_TOKENS"), "complete structured answer"),
    (_response({"intent": "next_task"}), "complete structured answer"),  # unparsed dict is not accepted
])
async def test_provider_failures_never_fabricate(tmp_path, outcome, message):
    brain, _, _ = _brain(tmp_path, outcome)
    with pytest.raises(LLMUnavailable, match=message):
        await brain.route(TurnContext(text="hello"))


async def test_one_call_in_flight_and_a_bounded_queue(tmp_path):
    brain, models, _ = _brain(tmp_path, _response(RouteDecision(intent="smalltalk")), delay=0.05)
    await asyncio.gather(*(brain.route(TurnContext(text=f"t{i}")) for i in range(4)))
    assert models.max_in_flight == 1 and len(models.calls) == 4

    full, full_models, _ = _brain(tmp_path, _response(RouteDecision(intent="smalltalk")), delay=0.05,
                                  COCOON_LLM_MAX_WAITING=0)
    results = await asyncio.gather(*(full.route(TurnContext(text=f"t{i}")) for i in range(2)), return_exceptions=True)
    assert [type(r).__name__ for r in results] == ["RouteDecision", "LLMUnavailable"]
    assert "queue is full" in str(results[1]) and len(full_models.calls) == 1


def test_live_vertex_needs_a_project_and_mock_needs_nothing(tmp_path):
    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
        make_settings(tmp_path, COCOON_LLM_MODE="live")
    assert build_brain(make_settings(tmp_path)).mode == "mock"


def test_missing_credentials_file_is_refused_without_reading_anything(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    settings = make_settings(tmp_path, COCOON_LLM_MODE="live", GOOGLE_CLOUD_PROJECT="p",
                             GOOGLE_APPLICATION_CREDENTIALS=str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="does not point to a file"):
        build_brain(settings)


def test_http_turn_uses_one_model_call_and_same_turn_retry_is_safe(tmp_path):
    """Through the real app: one model call per turn, record saved once, capacity failure is a retryable 503 that
    leaves nothing behind, and the same turn_id succeeds later without a second record."""
    decision = RouteDecision(intent="log_incident", incident_description="hydraulic hose leaking on the boom")
    brain, models, _ = _brain(tmp_path, CAPACITY)
    with TestClient(create_app(make_settings(tmp_path), brain=brain)) as c:
        sid = new_session(c)
        body = {"turn_id": "v-1", "text": "log an incident, the hydraulic hose is leaking", "source": "voice"}
        failed = c.post(f"/v1/sessions/{sid}/turns", json=body, headers=AUTH)
        assert failed.status_code == 503
        assert failed.json()["error"] | {"request_id": "x"} == {
            "code": "llm_unavailable", "message": "Vertex AI capacity is exhausted (429); retry shortly",
            "retryable": True, "request_id": "x"}
        assert c.get(f"/v1/sessions/{sid}/turns/v-1", headers=AUTH).json()["status"] == "failed"
        assert c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["incidents"] == []

        models.outcomes = [_response(decision)]
        calls_before = len(models.calls)
        ok = c.post(f"/v1/sessions/{sid}/turns", json=body, headers=AUTH)
        assert ok.status_code == 200, ok.text
        assert ok.json()["speech"] == "I've logged incident number 1: hydraulic hose leaking on the boom."
        assert len(models.calls) - calls_before == 1  # routing only; the reply is worded from the saved result
        again = c.post(f"/v1/sessions/{sid}/turns", json=body, headers=AUTH)
        assert again.json() == ok.json() and len(models.calls) - calls_before == 1  # replayed, no model call
        assert len(c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["incidents"]) == 1
