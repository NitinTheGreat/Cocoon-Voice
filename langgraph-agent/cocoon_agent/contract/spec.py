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
    Route("post", "/v1/sessions", "implemented", "I02a", ("voice_service", "simulator"),
          "Create or retrieve a session by client_session_key",
          idempotency="client_session_key: an existing key is compared with its stored association first "
                      "(200 or 409 session_conflict); only a new key is admitted against the verified catalog "
                      "(422 unknown_machine, then unknown_operator; 503 catalog_unavailable).",
          target_changes=(
              {"stage": "I03/I07A", "change": "Response adds machine_model, service_date and clock_mode.",
               "schema": "SessionTarget"},
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
          "Post one simulated sample. B3 (additive): optional operating_state/speed_kph/provenance; three versioned "
          "demo rules (belt, prolonged idle, idle+belt) with episodes, a linked automatic draft and announcements",
          idempotency="(session_id, event_id); same payload → duplicate:true, different payload → 409; a late or "
                      "same-time conflicting sample is recorded but ignored (stale:true, ignored_reason)",
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
    Route("get", "/v1/me", "implemented", "I02b", ("voice_service", "operator", "supervisor"),
          "Resolve the authenticated principal",
          target_changes=({"stage": "I13", "change": "site_ids from trusted supervisor site grants (none exist "
                                                     "yet; always empty in I02b)."},)),
    Route("post", "/v1/sessions/{session_id}/commands", "implemented", "B1", ("operator", "voice_service"),
          "Submit a task (task.start/complete, B1) or incident-draft command (incident.edit/confirm/dismiss, B2); "
          "the same domain service as the voice tools",
          idempotency="command_id unique per calling principal; identical retry returns the stored result "
                      "(duplicate: true); a different payload → 409 idempotency_conflict; stale expected_version → "
                      "409 version_conflict; illegal lifecycle step (e.g. confirming a confirmed incident) → 409 "
                      "invalid_transition.",
          target_changes=(
              {"stage": "I15", "change": "Full cocoon.command.v1 envelope with device binding, offline queueing "
                                         "(202) and the remaining kinds.", "schema": "Command"},
          )),
    Route("get", "/v1/sessions/{session_id}/commands/{command_id}", "implemented", "B1", ("operator",
                                                                                          "voice_service"),
          "Authoritative command result after a lost response", idempotency="Read-only; never re-executes.",
          target_changes=({"stage": "I15", "change": "Queued/failed/conflict states for offline commands.",
                           "schema": "CommandResult"},)),
    Route("post", "/v1/sessions/{session_id}/presence", "implemented", "D3", ("operator", "voice_service"),
          "Report consumer connectivity and voice/screen availability (liveness = server receipt + ttl)",
          idempotency="report_id per session: identical retry → duplicate:true; changed body → 409; an older "
                      "per-consumer sequence is recorded but ignored",
          target_changes=({"stage": "D3", "change": "Runtime ConsumerPresenceReport / PresenceResult of the "
                                                    "proposed PresenceReport / PresenceRecord.",
                           "schema": "PresenceRecord"},)),
    Route("post", "/v1/sessions/{session_id}/events/{event_id}/presentation", "implemented", "D3",
          ("operator", "voice_service"),
          "Report screen/vibration presentation of one announcement (not audio, not acknowledgement)",
          idempotency="presentation_id per session: identical retry returns the stored record; changed body → 409",
          target_changes=({"stage": "D3", "change": "Runtime PresentationReceipt / PresentationView of the proposed "
                                                    "PresentationReport / PresentationRecord.",
                           "schema": "PresentationRecord"},)),
    Route("post", "/v1/sessions/{session_id}/impacts", "implemented", "D3", ("simulator", "operator"),
          "Typed human-impact candidate (simulated in this prototype) opening or merging an SOS check-in",
          idempotency="source_event_id per operator: identical retry → status duplicate; changed body → 409",
          target_changes=({"stage": "I05A", "change": "Also a `human_impact` observation in the typed telemetry "
                                                      "v2 batch.", "schema": "HumanImpactObservation"},)),
    Route("get", "/v1/operators/{operator_id}/consents", "implemented", "D1", ("operator", "voice_service"),
          "Current purpose-specific consents (runtime OperatorConsentState; not_set is never a grant)",
          target_changes=({"stage": "D1", "change": "Runtime shape of the proposed ConsentState (adds "
                                                    "current_notice_version, notice_summary, requires).",
                           "schema": "ConsentState"},)),
    Route("post", "/v1/operators/{operator_id}/consents", "implemented", "D1", ("operator",),
          "Grant or revoke a consent purpose (the operator's own actor token only)",
          idempotency="change_id per operator: identical retry → applied:false with the current state (never "
                      "re-applied over a later revocation); changed body → 409 idempotency_conflict; stale "
                      "expected_version → 409 version_conflict. Service or supervisor credential → 403.",
          target_changes=({"stage": "D1", "change": "Runtime shape of the proposed ConsentChangeRequest/Result.",
                           "schema": "ConsentChangeResult"},)),
    Route("post", "/v1/sessions/{session_id}/wellbeing/samples", "implemented", "D1", ("simulator", "operator"),
          "Private heart-rate / skin-temperature summary for the session's operator (retained only under "
          "vitals_processing; values never echoed)",
          idempotency="sample_id per operator; identical retry → status duplicate; changed body → 409; after "
                      "retention deletion → status expired",
          target_changes=({"stage": "I05A", "change": "Also accepted as a `vitals` observation in the typed "
                                                      "cocoon.telemetry.v2 batch.", "schema": "VitalsObservation"},)),
    Route("get", "/v1/sessions/{session_id}/wellbeing", "implemented", "D1", ("operator", "voice_service"),
          "Operator-private wellbeing view: rule status, advice with explanation, breaks, consent summary"),
    Route("get", "/v1/supervisor/overview", "implemented", "D2", ("supervisor",),
          "Site-scoped supervisor overview built from explicit safe fields (risk category only, no raw vitals, no "
          "incident text); `site_id` must be a CLI-granted site",
          target_changes=({"stage": "D2", "change": "Runtime shape SupervisorSiteOverview of the proposed "
                                                    "SupervisorOverview.", "schema": "SupervisorOverview"},)),
    Route("get", "/v1/supervisor/events/stream", "implemented", "D2", ("supervisor",),
          "Replay then tail the site's supervisor change feed (SSE, projected at send time under current scope and "
          "consent)",
          idempotency="Read-only. Cursor = feed sequence (`after` or Last-Event-ID); 410 replay_expired below the "
                      "retained window; 422 invalid_cursor beyond the newest event; 429 when too many streams."),
    Route("post", "/v1/supervisor/notifications/{notification_id}/receipt", "implemented", "D2", ("supervisor",),
          "Report that an in-app notification was presented or acknowledged (never backwards)",
          idempotency="Same status again is a no-op; a lower status → 409 invalid_transition"),
    Route("get", "/v1/approvals", "implemented", "D2", ("supervisor", "operator"),
          "List authorised approvals (supervisor: granted sites; operator: own)",
          target_changes=({"stage": "D2", "change": "Runtime ApprovalPageView of the proposed ApprovalPage.",
                           "schema": "ApprovalPage"},)),
    Route("get", "/v1/approvals/{approval_id}", "implemented", "D2", ("supervisor", "operator"),
          "Proposal, decision and separate application outcome",
          target_changes=({"stage": "D2", "change": "Runtime ApprovalView of the proposed ApprovalRecord.",
                           "schema": "ApprovalRecord"},)),
    Route("post", "/v1/approvals/{approval_id}/decision", "implemented", "D2", ("supervisor",),
          "Approve or reject a proposal (scoped supervisor); application reported separately",
          idempotency="decision_id: identical retry → 200 decision_recorded:false; any other decision → 409 "
                      "decision_conflict; stale version/hash → 409 version_conflict; expired → 409 approval_expired.",
          target_changes=({"stage": "D2", "change": "Runtime ApprovalDecisionRequest/Result of the proposed "
                                                    "DecisionRequest/DecisionResult.", "schema": "DecisionResult"},)),
    Route("post", "/v1/shifts/{shift_id}/schedule-proposals", "implemented", "D2", ("voice_service", "simulator"),
          "Ask the deterministic weather re-planner for a reorder proposal (pending approval; changes nothing)",
          idempotency="Unchanged inputs return the pending proposal (status duplicate); changed inputs supersede it"),
    Route("get", "/v1/content/{asset_id}", "implemented", "C4", ("operator", "voice_service"),
          "Catalog lesson media metadata (not an arbitrary URL fetcher)",
          target_changes=({"stage": "I12A", "change": "Full ContentAsset shape (text/image kinds, not_yet_supplied "
                                                      "availability, structured provenance).",
                           "schema": "ContentAsset"},)),
    Route("get", "/v1/content/{asset_id}/file", "implemented", "C4", ("operator", "voice_service"),
          "The catalog media file itself, resolved by asset ID inside the content root"),
    Route("get", "/v1/content/{asset_id}/captions", "implemented", "C4", ("operator", "voice_service"),
          "WebVTT captions of a catalog video"),
    Route("get", "/v1/sessions/{session_id}/lessons", "implemented", "C4", ("operator", "voice_service"),
          "The verified operator's learning record: level, lesson progress, active question (no answer keys)"),
    Route("get", "/v1/sessions/{session_id}/lessons/{lesson_id}", "implemented", "C4", ("operator", "voice_service"),
          "One lesson version: speakable steps, media metadata and the assessment shape (no answer keys)"),
)

# Target schemas referenced from x-target-changes or used by fixtures that no route body names directly.
EXTRA_MODELS: tuple[type[BaseModel], ...] = (
    idn.SessionCreateRequestTarget, idn.SessionTarget, tr.TurnResultTarget, cm.SessionStateTarget,
    tm.TelemetryRequestTarget, tm.TelemetryIngestResult, an.EventsPageTarget, an.AnnouncementDeliveryReportTarget,
    tm.ConditionsSnapshot, lms.LessonVersion, lms.Course, lms.QuizAttempt, lms.ContentAsset,
    ap.ApprovalRecord, cm.Command, cm.CommandResult,
    # Proposed shapes of routes implemented in D (kept for fixtures and x-target-changes references).
    idn.ConsentState, idn.ConsentChangeRequest, idn.ConsentChangeResult, tm.VitalsObservation,
    sv.SupervisorOverview, sv.SupervisorFeedEvent, ap.ApprovalPage, ap.DecisionRequest, ap.DecisionResult,
    cm.PresenceReport, cm.PresenceRecord, cm.PresentationReport, cm.PresentationRecord,
    tm.HumanImpactObservation, sos.SosEpisode,
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


def check_schema_collisions(runtime_names: set[str], proposed_names: set[str]) -> None:
    """Runtime and proposed components share one namespace. A shared name is allowed only for a runtime (v1) model
    reused by a target model: then both sides ARE the same class. A model defined in the contract package with the
    name of a runtime component would silently be replaced by the runtime shape (a matching class name alone proves
    nothing), so generation fails and names it; give the runtime model a distinct name instead."""
    own = {name for name, model in model_index().items() if model.__module__ != s.__name__}
    clashes = sorted(runtime_names & proposed_names & own)
    if clashes:
        raise ValueError(f"proposed schema(s) {clashes} collide with runtime component(s) of the same name")


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
    check_schema_collisions(set(schemas), set(component_schemas(_models())))
    for name, schema in component_schemas(_models()).items():
        schemas.setdefault(name, schema)

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
    for mod in (idn,):  # runtime models re-exported as the contract once their route is implemented
        for name, value in vars(mod).items():
            if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == s.__name__:
                index.setdefault(name, value)
    return index
