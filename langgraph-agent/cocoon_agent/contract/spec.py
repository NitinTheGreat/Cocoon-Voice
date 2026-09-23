"""Route registry and builder for the PROPOSED target contract (contracts/proposed/).

`build_proposed_openapi(runtime_spec)` starts from the committed runtime export (contracts/openapi.yaml): the
implemented operations are copied unchanged and annotated; proposed routes and target schemas are added with
`x-implementation-status: proposed`. Nothing here registers a FastAPI route.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Literal

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from ..api import schemas as s
from . import announcements as an
from . import approvals as ap
from . import commands as cm
from . import common as cmn
from . import identity as idn
from . import incidents as inc
from . import internal as inn
from . import lms
from . import rules as rl
from . import sos
from . import supervisor as sv
from . import tasks as tk
from . import telemetry as tm
from . import turns as tr

Caller = Literal["public", "voice_service", "simulator", "operator", "supervisor"]
SSE = "text/event-stream"
JSON = "application/json"

PROPOSED_VERSION = "2.0.0-proposed.1"

SSE_FRAMING = (
    "UTF-8 SSE. One event = optional `id:` line (= event_id), `event:` line (= envelope type), one or more "
    "`data:` lines (joined with LF) holding one JSON envelope, then a blank line. LF or CRLF line endings. "
    "Heartbeats are comment lines (`: keep-alive`) every SSE_HEARTBEAT_SECONDS (proposed default 10); they have "
    "no id/sequence and are neither progress nor speech. Clients decode incrementally: network chunks do not "
    "align with frames or UTF-8 code points. Headers: Content-Type text/event-stream; charset=utf-8, "
    "Cache-Control no-cache, no-transform; X-Accel-Buffering no; no response compression."
)


@dataclass(frozen=True)
class Param:
    name: str
    location: Literal["query", "header", "path"]
    schema: dict[str, Any]
    description: str
    required: bool = False


@dataclass(frozen=True)
class Resp:
    description: str
    model: type[BaseModel] | tuple[type[BaseModel], ...] | None = None
    media: str = JSON
    headers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Route:
    method: Literal["get", "post"]
    path: str
    status: Literal["implemented", "proposed"]
    stage: str
    callers: tuple[Caller, ...]
    summary: str
    idempotency: str = ""
    request: type[BaseModel] | None = None
    responses: dict[int, Resp] = field(default_factory=dict)
    params: tuple[Param, ...] = ()
    target_changes: tuple[dict[str, str], ...] = ()
    tag: str = "target"


ERR = cmn.ProposedErrorResponse
AFTER_SEQ = Param("after", "query", {"type": "integer", "minimum": 0},
                  "Exclusive cursor: replay events with sequence > after. Negative, future (beyond the last "
                  "committed sequence) or foreign cursors are 422 invalid_cursor.")
LAST_EVENT_ID = Param("Last-Event-ID", "header", {"type": "string", "maxLength": 160},
                      "Equivalent to `after` (the event's sequence). If both are sent they must agree, "
                      "otherwise 422 invalid_cursor. An ID from another session/turn is 422 invalid_cursor.")
REQUEST_ID = Param("X-Request-ID", "header", {"type": "string", "pattern": r"^[A-Za-z0-9._:\-]{1,128}$"},
                   "Tracing only; echoed back. Never part of idempotency identity.")
COMMON_ERRORS = {
    401: Resp("Missing or invalid bearer token", ERR),
    403: Resp("Authenticated principal lacks scope (`forbidden`, `consent_required`)", ERR),
    404: Resp("Unknown or out-of-scope resource", ERR),
    422: Resp("Validation error (`validation_error`, `invalid_cursor`, `unknown_machine`, ...)", ERR),
    429: Resp("Admission limit reached (`rate_limited`); Retry-After set", ERR, headers=("Retry-After",)),
}


def _errs(*codes: int) -> dict[int, Resp]:
    return {c: COMMON_ERRORS[c] for c in codes}


ROUTES: tuple[Route, ...] = (
    # ------------------------------------------------------------------ implemented v1 (runtime today)
    Route("get", "/healthz", "implemented", "v1", ("public",), "Liveness"),
    Route("get", "/readyz", "implemented", "v1", ("public",), "Readiness"),
    Route("post", "/v1/sessions", "implemented", "v1", ("voice_service", "simulator"),
          "Create or retrieve a session by client_session_key",
          idempotency="client_session_key; conflicting immutable association → 409 session_conflict",
          target_changes=(
              {"stage": "I02", "change": "Reject an unknown machine_id with 422 `unknown_machine` and an unknown "
                                         "operator_id with 422 `unknown_operator` (catalog from I03). v1 accepts "
                                         "any non-empty ID with 201; this is an intentional, documented tightening.",
               "schema": "SessionCreateRequestTarget"},
              {"stage": "I02", "change": "Optional site_id/shift_id; response adds model, site, shift, service date, "
                                         "dataset manifest hash and clock mode.", "schema": "SessionTarget"},
          )),
    Route("post", "/v1/sessions/{session_id}/turns", "implemented", "v1", ("voice_service", "operator"),
          "Submit one finalized utterance (JSON result)",
          idempotency="(session_id, turn_id); 200 completed, 202 processing, 409 conflicting payload",
          target_changes=(
              {"stage": "I04/I08", "change": "Optional language, client_context, supersedes_turn_id.",
               "schema": "TurnRequestTarget"},
              {"stage": "I04", "change": "Response adds action_results (PublicActionResult), response_id and "
                                         "cancelled status for runs cancelled via the cancel route.",
               "schema": "TurnResultTarget"},
          )),
    Route("get", "/v1/sessions/{session_id}/turns/{turn_id}", "implemented", "v1", ("voice_service", "operator"),
          "Authoritative turn status and saved result",
          target_changes=({"stage": "I08", "change": "Adds recovery data (last_sequence, replay window, partial "
                                                     "generated speech).", "schema": "TurnResultTarget"},)),
    Route("get", "/v1/sessions/{session_id}/state", "implemented", "v1", ("voice_service", "operator"),
          "Bound operator state snapshot",
          target_changes=({"stage": "I02-I15", "change": "Additive dashboard, structured incidents, operator alerts, "
                                                         "learner progress, approvals, SOS, wellbeing, server/data "
                                                         "time. Operator projection only; supervisors use "
                                                         "/v1/supervisor/overview.", "schema": "SessionStateTarget"},)),
    Route("post", "/v1/sessions/{session_id}/telemetry", "implemented", "v1", ("simulator", "voice_service"),
          "Post one simulated sample",
          idempotency="(session_id, event_id); same payload → duplicate:true, different payload → 409",
          target_changes=({"stage": "I05A", "change": "Also accept the typed cocoon.telemetry.v2 batch; per-"
                                                      "observation accepted/duplicate/ignored/rejected outcomes.",
                           "schema": "TelemetryRequestTarget"},
                          {"stage": "I05A", "change": "v2 result", "schema": "TelemetryIngestResult"})),
    Route("get", "/v1/sessions/{session_id}/events", "implemented", "v1", ("voice_service", "operator"),
          "Poll retained announcements after a cursor",
          target_changes=({"stage": "I08A/I11", "change": "Announcements gain schema_version, speakable, "
                                                          "correlation and provenance; more announcement types.",
                           "schema": "EventsPageTarget"},)),
    Route("post", "/v1/sessions/{session_id}/events/{event_id}/delivery", "implemented", "v1", ("voice_service",),
          "Report announcement playback per consumer",
          idempotency="v1: one record per consumer, last write wins. Target: optional delivery_id makes attempts "
                      "immutable; without it v1 semantics are kept.",
          target_changes=({"stage": "I08B", "change": "Optional delivery_id and position_confidence.",
                           "schema": "AnnouncementDeliveryReportTarget"},)),
    # ------------------------------------------------------------------ proposed voice streaming/control (I08)
    Route("post", "/v1/sessions/{session_id}/turns/stream", "proposed", "I08A", ("voice_service",),
          "Submit a finalized utterance and stream the persisted turn events",
          idempotency="(session_id, turn_id) shared with the JSON route: identical retry attaches to the same run "
                      "and replays from the start; a conflicting semantic payload is 409 before the stream opens.",
          request=tr.TurnRequestTarget, params=(REQUEST_ID,),
          responses={200: Resp("Stream opened after validation and durable acceptance. 200 is not action "
                               "success; read the terminal event.", tr.TurnStreamEvent, SSE),
                     409: Resp("idempotency_conflict (turn_id reused with a different payload)", ERR),
                     **_errs(401, 403, 404, 422, 429),
                     503: Resp("Not accepted (storage unavailable, capability_unavailable)", ERR)}),
    Route("get", "/v1/sessions/{session_id}/turns/{turn_id}/stream", "proposed", "I08A", ("voice_service",),
          "Replay retained turn events after a cursor, then tail the same run",
          idempotency="Read-only. Never starts or re-runs a graph execution or tool.",
          params=(AFTER_SEQ, LAST_EVENT_ID, REQUEST_ID),
          responses={200: Resp("Replay then tail. Ends after the terminal event; reading past it ends at once.",
                               tr.TurnStreamEvent, SSE),
                     410: Resp("replay_expired: history past retention (proposed 900 s after terminal). "
                               "recovery.status_url gives the saved result.", ERR),
                     **_errs(401, 403, 404, 422, 429)}),
    Route("post", "/v1/sessions/{session_id}/turns/{turn_id}/cancel", "proposed", "I08B", ("voice_service",),
          "Persist an idempotent cancellation intent",
          idempotency="cancel_id: same id + same body returns the stored result; same id + different body → 409. "
                      "Closing a socket never cancels a run.",
          request=tr.CancelRequest, params=(REQUEST_ID,),
          responses={200: Resp("Run already terminal (completion may have won the race)", tr.CancelResult),
                     202: Resp("Cancellation recorded; run not yet terminal", tr.CancelResult),
                     409: Resp("idempotency_conflict (cancel_id reused with a different reason)", ERR),
                     **_errs(401, 403, 404, 422)}),
    Route("post", "/v1/sessions/{session_id}/turns/{turn_id}/delivery", "proposed", "I08B", ("voice_service",),
          "Report response playback separately from generation and actions",
          idempotency="delivery_id: identical retry returns the stored record; changed outcome → 409.",
          request=tr.TurnDeliveryReport, params=(REQUEST_ID,),
          responses={200: Resp("Stored delivery attempt", tr.TurnDeliveryRecord),
                     409: Resp("idempotency_conflict", ERR), **_errs(401, 403, 404, 422)}),
    Route("get", "/v1/sessions/{session_id}/events/stream", "proposed", "I08A/I11", ("voice_service", "operator"),
          "Replay then tail session announcements (same order as polling)",
          idempotency="Read-only; non-destructive; shares event_id and delivery identity with polling.",
          params=(Param("after", "query", {"type": "integer", "minimum": 0},
                        "Exclusive session sequence cursor, same as GET .../events?after="), LAST_EVENT_ID,
                  REQUEST_ID),
          responses={200: Resp("Announcement stream", an.AnnouncementEnvelope, SSE),
                     410: Resp("replay_expired; poll GET .../events for retained history", ERR),
                     **_errs(401, 403, 404, 422, 429)}),
    # ------------------------------------------------------------------ proposed operator/supervisor/sync
    Route("get", "/v1/me", "proposed", "I02", ("voice_service", "operator", "supervisor"),
          "Resolve the authenticated principal", responses={200: Resp("Principal", idn.MeResponse), **_errs(401)}),
    Route("post", "/v1/sessions/{session_id}/commands", "proposed", "I02/I15", ("operator", "voice_service"),
          "Submit a typed command (tap, voice-confirmed or offline sync)",
          idempotency="command_id unique per issuing subject across sessions and reconnects. Identical retry "
                      "returns the stored CommandResult (duplicate:true); a different payload under the same id "
                      "→ 409 idempotency_conflict; a stale expected_version → 409 with status conflict.",
          request=cm.Command, params=(REQUEST_ID,),
          responses={200: Resp("Completed (or identical duplicate)", cm.CommandResult),
                     202: Resp("Durably queued; not completed", cm.CommandResult),
                     409: Resp("Version conflict (CommandResult) or reused command_id (error)",
                               (cm.CommandResult, ERR)),
                     **_errs(401, 403, 404, 422, 429)}),
    Route("get", "/v1/sessions/{session_id}/commands/{command_id}", "proposed", "I02/I15", ("operator",
                                                                                          "voice_service"),
          "Authoritative command result after a lost response", idempotency="Read-only; never re-executes.",
          responses={200: Resp("Command result", cm.CommandResult), **_errs(401, 403, 404)}),
    Route("post", "/v1/sessions/{session_id}/presence", "proposed", "I15", ("operator", "voice_service"),
          "Report consumer connectivity and voice availability",
          idempotency="report_id; older per-consumer sequence ignored", request=cm.PresenceReport,
          responses={200: Resp("Current presence", cm.PresenceRecord), **_errs(401, 403, 404, 422)}),
    Route("post", "/v1/sessions/{session_id}/events/{event_id}/presentation", "proposed", "I15", ("operator",),
          "Report screen/vibration presentation (not audio, not acknowledgement)",
          idempotency="presentation_id: identical retry returns the stored record; changed body → 409",
          request=cm.PresentationReport,
          responses={200: Resp("Stored presentation", cm.PresentationRecord), 409: Resp("idempotency_conflict", ERR),
                     **_errs(401, 403, 404, 422)}),
    Route("get", "/v1/operators/{operator_id}/consents", "proposed", "I02", ("operator", "voice_service"),
          "Current purpose-specific consents", responses={200: Resp("Consent state", idn.ConsentState),
                                                          **_errs(401, 403, 404)}),
    Route("post", "/v1/operators/{operator_id}/consents", "proposed", "I02", ("operator",),
          "Grant or revoke a consent purpose (operator only)",
          idempotency="change_id; expected_version stale → 409 version_conflict. A supervisor token → 403.",
          request=idn.ConsentChangeRequest,
          responses={200: Resp("Resulting consent state", idn.ConsentChangeResult),
                     409: Resp("version_conflict / idempotency_conflict", ERR), **_errs(401, 403, 404, 422)}),
    Route("get", "/v1/supervisor/overview", "proposed", "I13", ("supervisor",),
          "Site-scoped supervisor overview (risk-only wellbeing, no raw vitals)",
          params=(Param("site_id", "query", {"type": "string"}, "Authorised site", required=True),
                  Param("cursor", "query", {"type": "string"}, "Opaque page cursor")),
          responses={200: Resp("Overview", sv.SupervisorOverview), **_errs(401, 403, 422)}),
    Route("get", "/v1/supervisor/events/stream", "proposed", "I13", ("supervisor",),
          "Replay then tail the role-filtered supervisor change feed",
          params=(Param("site_id", "query", {"type": "string"}, "Authorised site", required=True),
                  Param("after", "query", {"type": "integer", "minimum": 0}, "Exclusive feed cursor"),
                  LAST_EVENT_ID),
          responses={200: Resp("Supervisor feed", sv.SupervisorFeedEvent, SSE),
                     410: Resp("replay_expired; reload the overview", ERR), **_errs(401, 403, 422)}),
    Route("get", "/v1/approvals", "proposed", "I13", ("supervisor", "operator"),
          "List authorised approvals",
          params=(Param("status", "query", {"type": "string", "enum": ["pending", "approved", "rejected", "expired",
                                                                         "cancelled"]}, "Filter"),
                  Param("cursor", "query", {"type": "string"}, "Opaque page cursor"),
                  Param("limit", "query", {"type": "integer", "minimum": 1, "maximum": 100}, "Page size")),
          responses={200: Resp("Page", ap.ApprovalPage), **_errs(401, 403, 422)}),
    Route("get", "/v1/approvals/{approval_id}", "proposed", "I13", ("supervisor", "operator"),
          "Proposal, decision and separate application outcome",
          responses={200: Resp("Approval", ap.ApprovalRecord), **_errs(401, 403, 404)}),
    Route("post", "/v1/approvals/{approval_id}/decision", "proposed", "I13", ("supervisor",),
          "Approve or reject a proposal (scoped supervisor)",
          idempotency="decision_id: identical retry → 200 decision_recorded:false; contradictory decision → 409 "
                      "decision_conflict; stale version/hash → 409 version_conflict; expired → 409 approval_expired.",
          request=ap.DecisionRequest,
          responses={200: Resp("Saved decision; execution reported separately", ap.DecisionResult),
                     409: Resp("decision_conflict / version_conflict / approval_expired", ERR),
                     **_errs(401, 403, 404, 422)}),
    Route("get", "/v1/content/{asset_id}", "proposed", "I12A", ("operator", "voice_service"),
          "Approved lesson text/media metadata (not an arbitrary URL fetcher)",
          responses={200: Resp("Asset metadata", lms.ContentAsset), **_errs(401, 403, 404)}),
)

# Target schemas referenced from x-target-changes or used by fixtures that no route body names directly.
EXTRA_MODELS: tuple[type[BaseModel], ...] = (
    idn.SessionCreateRequestTarget, idn.SessionTarget, tr.TurnResultTarget, cm.SessionStateTarget,
    tm.TelemetryRequestTarget, tm.TelemetryIngestResult, an.EventsPageTarget, an.AnnouncementDeliveryReportTarget,
    tm.ConditionsSnapshot, lms.LessonVersion, lms.Course, lms.QuizAttempt,
    ap.ApprovalRecord,
)
INTERNAL_MODELS: tuple[type[BaseModel], ...] = (inn.ClassifierDecision, inn.ActionPlan)
STANDALONE: dict[str, type[BaseModel]] = {
    "cocoon.turn-stream.v1": tr.TurnStreamEvent,
    "cocoon.announcements.v1": an.AnnouncementEnvelope,
    "cocoon.supervisor-feed.v1": sv.SupervisorFeedEvent,
    "cocoon.command.v1": cm.Command,
}


def _models() -> list[type[BaseModel]]:
    seen: dict[str, type[BaseModel]] = {}
    for route in ROUTES:
        if route.status != "proposed":
            continue
        candidates: list[type[BaseModel]] = [route.request] if route.request else []
        for resp in route.responses.values():
            if isinstance(resp.model, tuple):
                candidates.extend(resp.model)
            elif resp.model is not None:
                candidates.append(resp.model)
        for model in candidates:
            seen.setdefault(model.__name__, model)
    for model in EXTRA_MODELS:
        seen.setdefault(model.__name__, model)
    return list(seen.values())


def component_schemas(models: list[type[BaseModel]]) -> dict[str, Any]:
    _, top = models_json_schema([(m, "validation") for m in models], ref_template="#/components/schemas/{model}")
    return top.get("$defs", {})


def _ref(model: type[BaseModel]) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{model.__name__}"}


def _content(resp: Resp) -> dict[str, Any]:
    if isinstance(resp.model, tuple):
        schema: dict[str, Any] = {"oneOf": [_ref(m) for m in resp.model]}
    else:
        schema = _ref(resp.model)  # type: ignore[arg-type]
    media: dict[str, Any] = {"schema": schema}
    if resp.media == SSE:
        media["x-sse-framing"] = SSE_FRAMING
        media["x-sse-event-schema"] = schema
    return {resp.media: media}


def _operation_id(route: Route) -> str:
    words = re.sub(r"[{}]", "", route.path).replace("/v1/", "").replace("/", "_").replace("-", "_")
    return f"{route.method}_{words}_target"


def _path_params(path: str) -> list[dict[str, Any]]:
    return [{"name": n, "in": "path", "required": True, "schema": {"type": "string", "maxLength": 128}}
            for n in re.findall(r"{([a-z_]+)}", path)]


def _proposed_operation(route: Route) -> dict[str, Any]:
    op: dict[str, Any] = {
        "tags": [route.tag],
        "summary": route.summary,
        "operationId": _operation_id(route),
        "x-implementation-status": "proposed",
        "x-target-stage": route.stage,
        "x-callers": list(route.callers),
        "security": [{"HTTPBearer": []}],
        "parameters": _path_params(route.path) + [
            {"name": p.name, "in": p.location, "required": p.required, "schema": p.schema,
             "description": p.description} for p in route.params
        ],
    }
    if route.idempotency:
        op["x-idempotency"] = route.idempotency
    if route.request is not None:
        op["requestBody"] = {"required": True, "content": {JSON: {"schema": _ref(route.request)}}}
    responses: dict[str, Any] = {}
    for code, resp in sorted(route.responses.items()):
        body: dict[str, Any] = {"description": resp.description}
        if resp.model is not None:
            body["content"] = _content(resp)
        if resp.headers:
            body["headers"] = {h: {"schema": {"type": "string"}} for h in resp.headers}
        responses[str(code)] = body
    op["responses"] = responses
    return op


def build_proposed_openapi(runtime_spec: dict[str, Any]) -> dict[str, Any]:
    """Target contract = runtime export (unchanged operations, annotated) + proposed routes and schemas."""
    runtime = copy.deepcopy(runtime_spec)
    paths: dict[str, Any] = {}
    for route in ROUTES:
        if route.status == "implemented":
            op = runtime["paths"][route.path][route.method]
            op["x-implementation-status"] = "implemented"
            op["x-target-stage"] = route.stage
            op["x-callers"] = list(route.callers)
            if route.idempotency:
                op["x-idempotency"] = route.idempotency
            if route.target_changes:
                op["x-target-changes"] = [
                    {**c, "schema": f"#/components/schemas/{c['schema']}"} if "schema" in c else dict(c)
                    for c in route.target_changes
                ]
            paths.setdefault(route.path, {})[route.method] = op
        else:
            paths.setdefault(route.path, {})[route.method] = _proposed_operation(route)
    missing = {(p, m) for p, ops in runtime["paths"].items() for m in ops} - {
        (r.path, r.method) for r in ROUTES if r.status == "implemented"}
    if missing:
        raise ValueError(f"runtime routes missing from the registry: {sorted(missing)}")

    schemas = dict(runtime["components"]["schemas"])
    runtime_models = {n for n, v in vars(s).items() if isinstance(v, type) and issubclass(v, BaseModel)}
    for name, schema in component_schemas(_models()).items():
        if name in schemas:
            # A v1 model reused by a target model keeps its runtime component unchanged. Any other name clash is
            # a bug: a proposed model must never redefine a runtime schema.
            if name not in runtime_models:
                raise ValueError(f"proposed schema {name!r} collides with a runtime component")
            continue
        schemas[name] = schema

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Cocoon backend API: target contract (PROPOSED)",
            "version": PROPOSED_VERSION,
            "summary": "Frozen I01 interface for the voice, Android, supervisor and data streams.",
            "description": (
                "NOT the running API. Operations marked x-implementation-status: implemented are copied unchanged "
                "from contracts/openapi.yaml (the runtime export). Operations marked proposed are specified for "
                "the stage in x-target-stage and are NOT served yet. x-target-changes lists additive or "
                "documented tightening changes planned for implemented routes. Fixture values under "
                "contracts/proposed/examples are synthetic contract examples, not measurements, sourced "
                "thresholds, real videos or WESAD evidence."
            ),
        },
        "x-runtime-contract": "contracts/openapi.yaml",
        "x-standalone-schemas": {name: f"schemas/{name}.schema.json" for name in STANDALONE},
        "paths": paths,
        "components": {"schemas": dict(sorted(schemas.items())),
                       "securitySchemes": runtime["components"]["securitySchemes"]},
    }


def standalone_schema(name: str, model: type[BaseModel], visibility: str = "public") -> dict[str, Any]:
    schema = model.model_json_schema(mode="validation")
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"urn:cocoon:schema:{name}",
            "x-implementation-status": "proposed", "x-visibility": visibility, **schema}


def build_all(runtime_spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Relative path under contracts/proposed/ → JSON document."""
    out = {"openapi.json": build_proposed_openapi(runtime_spec)}
    for name, model in STANDALONE.items():
        out[f"schemas/{name}.schema.json"] = standalone_schema(name, model)
    for model in INTERNAL_MODELS:
        slug = re.sub(r"(?<!^)(?=[A-Z])", "-", model.__name__).lower()
        out[f"internal/{slug}.schema.json"] = standalone_schema(f"internal.{slug}", model, "backend_internal")
    return out


@cache
def model_index() -> dict[str, type[BaseModel]]:
    """Name → proposed model, for fixture validation."""
    index: dict[str, type[BaseModel]] = {}
    for mod in (an, ap, cm, cmn, idn, inc, inn, lms, rl, sos, sv, tk, tm, tr):
        for value in vars(mod).values():
            if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == mod.__name__:
                index[value.__name__] = value
    return index
