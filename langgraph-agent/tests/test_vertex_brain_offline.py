"""Gemini-on-Vertex brain, checked offline with a fake google-genai client.

Proves request configuration and failure mapping only. It is NOT a live provider test (see docs/HANDOFF.md for the
recorded live run).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

from cocoon_agent.graph.brain import (
    ComposedSpeech, LLMUnavailable, RouteDecision, TurnContext, VertexBrain, build_brain,
)

from .conftest import make_settings


class FakeModels:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[dict] = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _response(parsed, finish="STOP"):
    return SimpleNamespace(parsed=parsed, candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))])


def _brain(tmp_path, outcome, **overrides) -> tuple[VertexBrain, FakeModels]:
    settings = make_settings(tmp_path, COCOON_LLM_MODE="live", GOOGLE_CLOUD_PROJECT="test-project", **overrides)
    models = FakeModels(outcome)
    return VertexBrain(settings, client=SimpleNamespace(aio=SimpleNamespace(models=models))), models


async def test_route_uses_the_configured_model_and_structured_output(tmp_path):
    brain, models = _brain(tmp_path, _response(RouteDecision(intent="next_task")))
    decision = await brain.route(TurnContext(text="what's my next job"))
    assert decision.intent == "next_task"
    call = models.calls[0]
    assert call["model"] == "gemini-3.8-flash"
    cfg = call["config"]
    assert cfg.response_mime_type == "application/json" and cfg.response_schema is RouteDecision
    assert cfg.thinking_config.thinking_level.value.lower() == "low"
    assert cfg.automatic_function_calling.disable is True
    assert '"utterance": "what\'s my next job"' in call["contents"]


async def test_compose_cleans_speech(tmp_path):
    brain, _ = _brain(tmp_path, _response(ComposedSpeech(speech="Your next task is **grading**.")))
    assert await brain.compose(TurnContext(text="x"), []) == "Your next task is grading ."


@pytest.mark.parametrize("outcome,message", [
    (genai_errors.ClientError(429, {"error": {"message": "exhausted"}}), "quota is exhausted"),
    (genai_errors.ClientError(403, {"error": {"message": "denied"}}), "rejected the credentials"),
    (genai_errors.ServerError(503, {"error": {"message": "down"}}), "returned HTTP 503"),
    (TimeoutError(), "could not reach Vertex AI"),
    (_response(None, "SAFETY"), "declined"),
    (_response(None, "MAX_TOKENS"), "complete structured answer"),
    (_response({"intent": "next_task"}), "complete structured answer"),  # unparsed dict is not accepted
])
async def test_provider_failures_never_fabricate(tmp_path, outcome, message):
    brain, _ = _brain(tmp_path, outcome)
    with pytest.raises(LLMUnavailable, match=message):
        await brain.route(TurnContext(text="hello"))


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
