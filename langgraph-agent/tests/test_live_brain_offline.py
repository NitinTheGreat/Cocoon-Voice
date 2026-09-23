"""Live (Claude) brain request/response handling, checked offline with a fake HTTP transport.

This proves request shape and error mapping only. It is NOT a live provider test.
"""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from cocoon_agent.graph.brain import AnthropicBrain, LLMUnavailable, TurnContext

from .conftest import make_settings


def _message(text: str, stop_reason: str = "end_turn") -> dict:
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": text}] if text else [], "stop_reason": stop_reason,
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}


def _brain(tmp_path, handler, **overrides) -> AnthropicBrain:
    settings = make_settings(tmp_path, COCOON_LLM_MODE="live", ANTHROPIC_API_KEY="sk-test-not-real", **overrides)
    client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    return AnthropicBrain(settings, http_client=client)


async def test_route_request_shape_and_parse(tmp_path):
    seen: dict = {}

    def handler(request):
        seen["beta"] = request.headers.get("anthropic-beta", "")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=_message('{"intent": "log_incident", "incident_description": "hose leak"}'))

    decision = await _brain(tmp_path, handler).route(TurnContext(text="log a hose leak"))
    assert decision.intent == "log_incident" and decision.incident_description == "hose leak"
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    body = seen["body"]
    assert body["model"] == "claude-opus-5" and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert json.loads(body["messages"][0]["content"])["utterance"] == "log a hose leak"


async def test_fallbacks_can_be_disabled(tmp_path):
    seen: dict = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=_message('{"speech": "Your next task is grading."}'))

    speech = await _brain(tmp_path, handler, COCOON_LLM_FALLBACKS="off").compose(TurnContext(text="x"), [])
    assert speech == "Your next task is grading."
    assert "fallbacks" not in seen["body"]


@pytest.mark.parametrize("response", [
    httpx2.Response(200, json=_message("", stop_reason="refusal")),
    httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}),
    httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad"}}),
])
async def test_provider_failures_never_fabricate(tmp_path, response):
    with pytest.raises(LLMUnavailable):
        await _brain(tmp_path, lambda request: response).route(TurnContext(text="hello"))


def test_live_mode_requires_key(tmp_path):
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        make_settings(tmp_path, COCOON_LLM_MODE="live")
