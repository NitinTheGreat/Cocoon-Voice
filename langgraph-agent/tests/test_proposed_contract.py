"""CONTRACT checks for the proposed target interface in ../contracts/proposed (I01).

These validate schemas, fixtures and pure event-sequence invariants. They do not exercise any proposed route:
none is implemented yet, and a test here passing is not evidence of runtime streaming, auth, storage,
cancellation, recovery or client integration.
"""

from __future__ import annotations

import codecs
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from cocoon_agent.api.app import create_app
from cocoon_agent.contract import announcements as an
from cocoon_agent.contract import commands as cm
from cocoon_agent.contract import identity as idn
from cocoon_agent.contract import spec
from cocoon_agent.contract import supervisor as sv
from cocoon_agent.contract import telemetry as tm
from cocoon_agent.contract import turns as tr
from cocoon_agent.contract.sequences import announcement_violations, turn_stream_violations

from .conftest import CONTRACTS, REPO_ROOT, make_settings

PROPOSED = CONTRACTS / "proposed"
SERVICE_DIR = Path(__file__).resolve().parents[1]


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def runtime_spec(tmp_path_factory) -> dict:
    app = create_app(make_settings(tmp_path_factory.mktemp("data")))
    return json.loads(json.dumps(app.openapi()))


@pytest.fixture(scope="module")
def proposed() -> dict:
    return _load(PROPOSED / "openapi.json")


def _component_validator(doc: dict, name: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": doc["components"]})


# ------------------------------------------------------------------ artifacts and runtime isolation


def test_committed_proposed_artifacts_match_the_builder(runtime_spec):
    for rel, doc in spec.build_all(runtime_spec).items():
        assert _load(PROPOSED / rel) == doc, f"{rel} drifted: run python scripts/export_proposed_contract.py"


def test_importing_the_contract_package_does_not_load_the_app():
    code = "import sys, cocoon_agent.contract.spec; print('cocoon_agent.api.app' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=SERVICE_DIR, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_no_proposed_route_is_registered_in_the_running_app(tmp_path):
    app = create_app(make_settings(tmp_path))
    registered = {(r.path, m.lower()) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    implemented = {(r.path, r.method) for r in spec.ROUTES if r.status == "implemented"}
    proposed = {(r.path, r.method) for r in spec.ROUTES if r.status == "proposed"}
    assert registered == implemented
    assert not registered & proposed
    # The runtime export must stay free of target-only schemas.
    runtime_names = set(app.openapi()["components"]["schemas"])
    assert not runtime_names & {"TurnStreamEvent", "AnnouncementEnvelope", "Command", "SupervisorOverview"}


def test_implemented_operations_are_copied_unchanged(runtime_spec, proposed):
    for route in spec.ROUTES:
        if route.status != "implemented":
            continue
        target = proposed["paths"][route.path][route.method]
        assert target["x-implementation-status"] == "implemented"
        stripped = {k: v for k, v in target.items() if not k.startswith("x-")}
        assert stripped == runtime_spec["paths"][route.path][route.method], route.path


def _refs(node):
    if isinstance(node, dict):
        if "$ref" in node:
            yield node["$ref"]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def test_proposed_openapi_structure(proposed):
    assert proposed["openapi"] == "3.1.0"
    schemas = proposed["components"]["schemas"]
    for ref in _refs(proposed):
        assert ref.startswith("#/components/schemas/") and ref.rsplit("/", 1)[1] in schemas, ref
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
    operation_ids = []
    for path, ops in proposed["paths"].items():
        declared = set(re.findall(r"{([a-z_]+)}", path))
        for method, op in ops.items():
            operation_ids.append(op["operationId"])
            assert op["x-implementation-status"] in ("implemented", "proposed")
            assert op["x-target-stage"] and op["responses"]
            params = {p["name"] for p in op.get("parameters", []) if p["in"] == "path"}
            assert params == declared, f"{method.upper()} {path}"
            if path.startswith("/v1/"):
                assert op["security"] == [{"HTTPBearer": []}]
            for code, resp in op["responses"].items():
                for media in resp.get("content", {}):
                    if media == "text/event-stream":
                        assert code == "200" and "x-sse-framing" in resp["content"][media]
    assert len(operation_ids) == len(set(operation_ids))


SEVEN_V1 = {("post", "/v1/sessions"), ("post", "/v1/sessions/{session_id}/turns"),
            ("get", "/v1/sessions/{session_id}/turns/{turn_id}"), ("get", "/v1/sessions/{session_id}/state"),
            ("post", "/v1/sessions/{session_id}/telemetry"), ("get", "/v1/sessions/{session_id}/events"),
            ("post", "/v1/sessions/{session_id}/events/{event_id}/delivery")}
FIVE_STREAMING = {("post", "/v1/sessions/{session_id}/turns/stream"),
                  ("get", "/v1/sessions/{session_id}/turns/{turn_id}/stream"),
                  ("post", "/v1/sessions/{session_id}/turns/{turn_id}/cancel"),
                  ("post", "/v1/sessions/{session_id}/turns/{turn_id}/delivery"),
                  ("get", "/v1/sessions/{session_id}/events/stream")}


def test_seven_json_routes_are_implemented_and_five_streaming_routes_are_proposed():
    status = {(r.method, r.path): r.status for r in spec.ROUTES}
    assert all(status[k] == "implemented" for k in SEVEN_V1)
    assert all(status[k] == "proposed" for k in FIVE_STREAMING)


STANDALONE_FILES = sorted(f"{d}/{p.name}" for d in ("schemas", "internal") for p in (PROPOSED / d).glob("*.json"))


@pytest.mark.parametrize("rel", STANDALONE_FILES)
def test_standalone_schemas_are_valid_json_schema(rel):
    doc = _load(PROPOSED / rel)
    Draft202012Validator.check_schema(doc)
    assert doc["x-implementation-status"] == "proposed"


# ------------------------------------------------------------------ fixtures

INDEX = _load(PROPOSED / "examples" / "index.json")["fixtures"]


def test_fixture_index_is_complete():
    on_disk = {f"{d}/{p.name}" for d in ("valid", "invalid") for p in (PROPOSED / "examples" / d).glob("*.json")}
    assert on_disk == {f["file"] for f in INDEX}
    assert {f["expect"] for f in INDEX} == {"valid", "invalid"}


def _internal_validator(name: str) -> Draft202012Validator:
    slug = re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()
    return Draft202012Validator(_load(PROPOSED / "internal" / f"{slug}.schema.json"))


@pytest.mark.parametrize("entry", INDEX, ids=[f["file"] for f in INDEX])
def test_fixture_matches_expectation(entry, proposed):
    data = _load(PROPOSED / "examples" / entry["file"])
    model = spec.model_index()[entry["schema"]]
    validator = (_internal_validator(entry["schema"]) if entry["layer"] == "internal"
                 else _component_validator(proposed, entry["schema"]))
    if entry["expect"] == "valid":
        model.model_validate(data)
        validator.validate(data)
        return
    with pytest.raises(ValidationError):
        model.model_validate(data)
    if entry["rejected_by"] == "both":
        assert not validator.is_valid(data), "JSON Schema should reject this fixture too"


V1_TO_TARGET = {
    "session_create.request.json": idn.SessionCreateRequestTarget,
    "session.response.json": idn.SessionTarget,
    "turn.request.json": tr.TurnRequestTarget,
    "turn_completed.response.json": tr.TurnResultTarget,
    "turn_information_requested.response.json": tr.TurnResultTarget,
    "turn_alert_explained.response.json": tr.TurnResultTarget,
    "turn_processing.response.json": tr.TurnResultTarget,
    "state.response.json": cm.SessionStateTarget,
    "telemetry.request.json": tm.TelemetryRequestTarget,
    "events.response.json": an.EventsPageTarget,
    "delivery.request.json": an.AnnouncementDeliveryReportTarget,
}


@pytest.mark.parametrize("name,model", V1_TO_TARGET.items())
def test_every_v1_example_is_still_valid_under_the_target(name, model, proposed):
    data = _load(CONTRACTS / "examples" / name)
    model.model_validate(data)
    _component_validator(proposed, model.__name__).validate(data)


# ------------------------------------------------------------------ sequences and exchanges

SEQUENCES = sorted((PROPOSED / "sequences").glob("*.json"))


@pytest.mark.parametrize("path", SEQUENCES, ids=[p.stem for p in SEQUENCES])
def test_event_sequence_invariants(path):
    doc = _load(path)
    if doc["stream"] == "turn":
        problems = turn_stream_violations(doc["events"], partial=doc["partial"])
    else:
        problems = announcement_violations(doc["events"])
    if doc["expect"] == "valid":
        assert problems == []
    else:
        assert doc["violation"] in problems


EXCHANGES = sorted((PROPOSED / "exchanges").glob("*.json"))
ROUTE_INDEX = {f"{r.method.upper()} {r.path}": r for r in spec.ROUTES}


@pytest.mark.parametrize("path", EXCHANGES, ids=[p.stem for p in EXCHANGES])
def test_exchange_examples_use_documented_statuses_and_schemas(path, proposed):
    doc = _load(path)
    route = ROUTE_INDEX[doc["route"]]
    method, template = doc["route"].split(" ", 1)
    documented = proposed["paths"][template][method.lower()]["responses"]
    for step in doc["steps"]:
        response = step["response"]
        assert str(response["status"]) in documented, f"{response['status']} not documented for {doc['route']}"
        if response.get("media") == "text/event-stream":
            for event in response["events"]:
                tr.TurnStreamEvent.model_validate(event)
        if "schema" in response:
            spec.model_index()[response["schema"]].model_validate(response["body"])
            _component_validator(proposed, response["schema"]).validate(response["body"])
        if route.request is not None and "body" in step["request"]:
            route.request.model_validate(step["request"]["body"])


# ------------------------------------------------------------------ SSE transport fixture


def _reference_parse(chunks: list[bytes]) -> tuple[list[dict], list[str]]:
    """TEST-ONLY minimal SSE reader used to check the fixture's own expectations. Not a production parser."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer, events, comments, fields = "", [], [], {"data": []}
    for chunk in chunks + [b""]:
        buffer += decoder.decode(chunk, final=chunk == b"")
        while True:
            match = re.search(r"\r\n|\n|\r", buffer)
            if not match:
                break
            line, buffer = buffer[:match.start()], buffer[match.end():]
            if line == "":
                if fields["data"]:
                    events.append({"id": fields.get("id"), "event": fields.get("event"),
                                   "data": json.loads("\n".join(fields["data"]))})
                fields = {"data": []}
            elif line.startswith(":"):
                comments.append(line[1:].strip())
            else:
                name, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if name == "data":
                    fields["data"].append(value)
                elif name in ("id", "event"):
                    fields[name] = value
    return events, comments


def test_sse_byte_fixture_is_self_consistent():
    doc = _load(PROPOSED / "sse" / "turn_stream_chunked_unicode.json")
    chunks = [bytes.fromhex(h) for h in doc["chunks_hex"]]
    raw = b"".join(chunks)
    assert b"\r\n" in raw and b"\n\n" in raw, "fixture mixes CRLF and LF"
    assert any(0x80 <= c[0] < 0xC0 for c in chunks[1:]), "a chunk boundary must split a UTF-8 character"
    events, comments = _reference_parse(chunks)
    assert events == doc["expected"]["events"]
    assert comments == doc["expected"]["comments"]
    assert "".join(e["data"]["data"]["text"] for e in events if e["event"] == "speech.delta") == \
        doc["expected"]["speech_text"]
    for event in events:
        parsed = tr.TurnStreamEvent.model_validate(event["data"]).root
        assert event["id"] == parsed.event_id and event["event"] == parsed.type


# ------------------------------------------------------------------ privacy and documentation

PRIVATE_FIELDS = {"heart_rate_bpm", "skin_temp_c", "sleep", "trigger_evidence", "answer_text", "played_text",
                  "generated_speech", "speech", "what"}


def _properties(schema: dict, defs: dict, seen: set[str]) -> set[str]:
    names: set[str] = set()
    if "$ref" in schema:
        key = schema["$ref"].rsplit("/", 1)[1]
        if key in seen:
            return names
        seen.add(key)
        return _properties(defs[key], defs, seen)
    if schema.get("type") == "object" or "properties" in schema:
        assert schema.get("additionalProperties") is False, "supervisor models must be closed"
    for name, sub in schema.get("properties", {}).items():
        names.add(name)
        names |= _properties(sub, defs, seen)
    for key in ("items", "anyOf", "oneOf", "allOf"):
        value = schema.get(key)
        for sub in (value if isinstance(value, list) else [value] if value else []):
            names |= _properties(sub, defs, seen)
    return names


@pytest.mark.parametrize("model", [sv.SupervisorOverview, sv.SupervisorFeedEvent])
def test_supervisor_projections_cannot_carry_private_fields(model):
    schema = model.model_json_schema()
    names = _properties(schema, schema.get("$defs", {}), set())
    assert not names & PRIVATE_FIELDS


def test_api_contract_capability_table_lists_every_route():
    text = (REPO_ROOT / "API_CONTRACT.md").read_text(encoding="utf-8")
    for route in spec.ROUTES:
        row = re.search(rf"^\| `{route.method.upper()} {re.escape(route.path)}` \| (\w+) \|", text, re.MULTILINE)
        assert row, f"{route.method.upper()} {route.path} missing from API_CONTRACT.md"
        assert row.group(1) == route.status


def test_a_proposed_model_can_never_share_a_runtime_component_name():
    # reused runtime classes (same class on both sides) are fine; a contract-package model with a runtime name is not
    spec.check_schema_collisions({"Alert", "Session"}, {"Alert", "Session"})
    with pytest.raises(ValueError, match="ConsentState"):
        spec.check_schema_collisions({"ConsentState", "Alert"}, {"ConsentState", "Alert"})
