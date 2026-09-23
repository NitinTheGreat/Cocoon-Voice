"""Catch drift between the committed contract and the running FastAPI schemas."""

from __future__ import annotations

import json

import pytest
import yaml
from jsonschema import Draft202012Validator

from cocoon_agent.api import schemas as s
from cocoon_agent.api.app import create_app

from .conftest import CONTRACTS, make_settings

EXAMPLES = {
    "session_create.request.json": s.SessionCreateRequest,
    "session.response.json": s.Session,
    "turn.request.json": s.TurnRequest,
    "turn_completed.response.json": s.TurnResult,
    "turn_information_requested.response.json": s.TurnResult,
    "turn_alert_explained.response.json": s.TurnResult,
    "turn_processing.response.json": s.TurnResult,
    "state.response.json": s.SessionState,
    "telemetry.request.json": s.TelemetryRequest,
    "telemetry.response.json": s.TelemetryResult,
    "events.response.json": s.EventsPage,
    "delivery.request.json": s.DeliveryReport,
    "delivery.response.json": s.DeliveryRecord,
    "error_idempotency_conflict.response.json": s.ErrorResponse,
    "error_validation.response.json": s.ErrorResponse,
    "error_unauthorized.response.json": s.ErrorResponse,
}


@pytest.fixture(scope="module")
def committed() -> dict:
    return yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))


def test_committed_openapi_matches_app(tmp_path, committed):
    live = json.loads(json.dumps(create_app(make_settings(tmp_path)).openapi()))
    assert committed == live, "contracts/openapi.yaml drifted: run python scripts/export_openapi.py and review"


def test_every_example_is_covered():
    on_disk = {p.name for p in (CONTRACTS / "examples").glob("*.json")}
    assert on_disk == set(EXAMPLES), "add new example files to EXAMPLES so they are validated"


@pytest.mark.parametrize("name,model", EXAMPLES.items())
def test_examples_match_pydantic_and_openapi(name, model, committed):
    data = json.loads((CONTRACTS / "examples" / name).read_text(encoding="utf-8"))
    model.model_validate(data)
    schema = {"$ref": f"#/components/schemas/{model.__name__}", "components": committed["components"]}
    Draft202012Validator(schema).validate(data)


def test_required_status_codes_are_documented(committed):
    turns = committed["paths"]["/v1/sessions/{session_id}/turns"]["post"]["responses"]
    assert {"200", "202", "401", "404", "409", "422", "503"} <= set(turns)
    assert committed["paths"]["/v1/sessions/{session_id}/events"]["get"]["parameters"][1]["name"] == "after"
    for path, ops in committed["paths"].items():
        if path.startswith("/v1/"):
            for op in ops.values():
                assert op.get("security") == [{"HTTPBearer": []}], path
