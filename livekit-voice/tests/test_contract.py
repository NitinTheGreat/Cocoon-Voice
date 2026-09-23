"""The worker's own contract models and the mock backend, checked against ../contracts (no langgraph-agent import)."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from cocoon_voice import contract as c
from cocoon_voice.backend_client import BackendClient
from cocoon_voice.bridge import TurnBridge, bind_session
from cocoon_voice.mock_backend import create_mock_app

from .conftest import CONTRACTS

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def components() -> dict:
    return yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))["components"]


def check(components: dict, schema_name: str, data: dict) -> None:
    Draft202012Validator({"$ref": f"#/components/schemas/{schema_name}", "components": components}).validate(data)


def example(name: str) -> dict:
    return json.loads((CONTRACTS / "examples" / name).read_text(encoding="utf-8"))


def test_worker_requests_match_contract(components):
    check(components, "SessionCreateRequest", c.SessionCreateRequest(
        client_session_key="lk:r:p", room_name="r", participant_identity="p", operator_id="o",
        machine_id="m").model_dump())
    check(components, "TurnRequest", c.TurnRequest(turn_id="lk-item_abc", text="hi").model_dump())
    check(components, "DeliveryReport", c.DeliveryReport(consumer_id="voice-r-p", status="played").model_dump())


@pytest.mark.parametrize("name,model", [
    ("session.response.json", c.Session),
    ("turn_completed.response.json", c.TurnResult),
    ("turn_processing.response.json", c.TurnResult),
    ("events.response.json", c.EventsPage),
    ("delivery.response.json", c.DeliveryRecord),
    ("error_idempotency_conflict.response.json", c.ErrorResponse),
])
def test_worker_parses_contract_examples(name, model):
    model.model_validate(example(name))


def test_mock_backend_matches_contract(components):
    client = TestClient(create_mock_app(TOKEN))
    assert client.get("/v1/sessions/x/state").status_code == 401
    body = example("session_create.request.json")
    r = client.post("/v1/sessions", json=body, headers=H)
    assert r.status_code == 201
    check(components, "Session", r.json())
    assert client.post("/v1/sessions", json=body, headers=H).status_code == 200
    sid = r.json()["session_id"]

    r = client.post(f"/v1/sessions/{sid}/turns", json={"turn_id": "t1", "text": "hello", "source": "voice"}, headers=H)
    assert r.status_code == 200 and r.json()["speech"].startswith("Mock backend")
    check(components, "TurnResult", r.json())
    assert client.post(f"/v1/sessions/{sid}/turns", json={"turn_id": "t1", "text": "other", "source": "voice"},
                       headers=H).status_code == 409
    check(components, "ErrorResponse", client.get("/v1/sessions/nope/state", headers=H).json())
    check(components, "SessionState", client.get(f"/v1/sessions/{sid}/state", headers=H).json())

    tel = example("telemetry.request.json")
    r = client.post(f"/v1/sessions/{sid}/telemetry", json=tel, headers=H)
    check(components, "TelemetryResult", r.json())
    events = client.get(f"/v1/sessions/{sid}/events?after=0", headers=H).json()
    check(components, "EventsPage", events)
    ev = events["events"][0]["event_id"]
    r = client.post(f"/v1/sessions/{sid}/events/{ev}/delivery", json={"consumer_id": "w", "status": "played"},
                    headers=H)
    check(components, "DeliveryRecord", r.json())


async def test_worker_bridge_against_mock_backend_including_202_polling(monkeypatch):
    monkeypatch.setattr("cocoon_voice.mock_backend.SLOW_SECONDS", 0.3)
    app = create_mock_app(TOKEN)
    client = BackendClient("http://mock", TOKEN, transport=httpx.ASGITransport(app=app))
    try:
        binding = await bind_session(client, c.SessionCreateRequest(**example("session_create.request.json")))
        fast = await client.submit_turn(binding.session_id, "t-fast", "What's my next task?")
        assert fast.status == "completed" and "Mock backend" in fast.speech
        slow = await client.submit_turn(binding.session_id, "t-slow", "a slow request")
        assert slow.status == "completed" and "slow request" in slow.speech

        from livekit.agents import llm
        chat = llm.ChatContext.empty()
        chat.add_message(role="user", content="via llm_node path")
        assert "via llm_node path" in await TurnBridge(client, binding).reply_for(chat)
    finally:
        await client.aclose()
