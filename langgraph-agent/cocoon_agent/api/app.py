"""FastAPI application exposing the v1 contract."""

import asyncio
import logging
import re
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..auth import Principal, utcnow
from ..catalog import CatalogError, load_catalog, load_session_bindings
from ..config import Settings, get_settings
from ..conditions import Conditions
from ..estimation import load_estimator
from ..planning import Planner
from ..graph.brain import Brain, build_brain
from ..graph.builder import build_graph
from ..service import ApiError, CocoonService
from ..store import Store
from ..weather import OpenMeteoWeather, WeatherService, load_conditions_policy
from . import schemas as s

log = logging.getLogger("cocoon_agent.api")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
_bearer = HTTPBearer(auto_error=False, description=(
    "Exactly ONE of: the trusted service credential (COCOON_SERVICE_TOKEN; server-side callers only, never embedded "
    "in Android or React), or an actor token (`cct_...`) issued locally by scripts/actor_tokens.py for an operator or "
    "supervisor principal. Access per route is listed in API_CONTRACT.md."))

AUTH_UNAVAILABLE = "auth_unavailable: the token store could not be read (retryable; never treated as success)"
ERRORS = {
    401: {"model": s.ErrorResponse,
          "description": "unauthorized: missing, malformed, unknown, expired or revoked bearer token, or more than "
                         "one Authorization header (the cause is deliberately not distinguished)"},
    403: {"model": s.ErrorResponse, "description": "forbidden: authenticated, but this role may not use the route"},
    404: {"model": s.ErrorResponse,
          "description": "Unknown session or resource; also returned for a session an operator does not own"},
    422: {"model": s.ErrorResponse, "description": "Malformed request"},
    500: {"model": s.ErrorResponse, "description": "Unexpected server error (retryable)"},
    503: {"model": s.ErrorResponse, "description": AUTH_UNAVAILABLE},
}


def _error_response(status: int, code: str, message: str, retryable: bool, request_id: str,
                    details: list[dict[str, str]] | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    body = s.ErrorResponse(error=s.ErrorBody(
        code=code, message=message, retryable=retryable, request_id=request_id,
        details=[s.ErrorDetail(**d) for d in details] if details else None,
    ))
    return JSONResponse(body.model_dump(mode="json", exclude_none=True), status_code=status,
                        headers={"X-Request-ID": request_id, **(headers or {})})


def create_app(settings: Settings | None = None, brain: Brain | None = None,
               clock: Callable[[], datetime] | None = None, weather: WeatherService | None = None) -> FastAPI:
    """`brain` overrides the configured router/composer (tests inject slow or failing brains).
    `clock` overrides the wall clock used for token expiry (tests); it is never the data/replay clock.
    `weather` overrides the configured weather source (tests inject a live adapter with a fake transport)."""
    settings = settings or get_settings()
    injected_brain = brain
    injected_weather = weather

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.db_path)
        # A migration error aborts startup (the database is left at its previous version, never wiped).
        applied = store.init_schema()
        if applied:
            log.info("cocoon.db migrations: %s (schema version %d)", ", ".join(applied), store.schema_version())
        store.seed_demo()
        # A catalog error does NOT abort startup: existing sessions stay readable, /readyz reports not_ready and
        # new sessions get 503 catalog_unavailable. There is no fallback to accepting arbitrary IDs.
        catalog = bindings = catalog_issue = None
        try:
            catalog = load_catalog(settings.dataset_root, settings.dataset_manifest_sha256)
            if settings.session_bindings_path is not None:
                bindings = load_session_bindings(settings.session_bindings_path, catalog)
            store.register_catalog(catalog)
            log.info("catalog verified manifest_sha256=%s machines=%d operators=%d bindings=%s",
                     catalog.manifest_sha256, len(catalog.machines), len(catalog.operators),
                     len(bindings.bindings) if bindings else 0)
        except CatalogError as exc:
            catalog = bindings = None
            catalog_issue = exc.issue
            log.error("catalog NOT loaded (issue=%s): %s; new sessions will be refused", exc.issue, exc.message)
        brain = injected_brain or build_brain(settings)
        live = None
        if settings.weather_mode == "live":
            live = OpenMeteoWeather(timeout=settings.weather_timeout_seconds,
                                    fresh_seconds=settings.weather_fresh_seconds,
                                    stale_limit_seconds=settings.weather_stale_limit_seconds,
                                    misalign_seconds=settings.weather_misalign_seconds)
        weather = injected_weather or WeatherService(settings.weather_mode, fixture_path=settings.weather_fixture_path,
                                                     live=live)
        conditions = Conditions(store, weather, load_conditions_policy(settings.conditions_policy_path))
        planner = Planner(store, conditions, load_estimator(settings.estimator_config_path), catalog)
        refresher = None
        if weather.live is not None:  # bounded background refresh; lookups never wait for it
            refresher = asyncio.create_task(weather.refresher(store.located_sites(), settings.weather_refresh_seconds,
                                                              utcnow))
        log.info("weather mode=%s policy=%s", settings.weather_mode, conditions.policy.policy_version)
        async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
            graph = build_graph(store, brain, conditions=conditions, planner=planner).compile(checkpointer=saver)
            app.state.service = CocoonService(settings, store, graph, brain, catalog=catalog, bindings=bindings,
                                              catalog_issue=catalog_issue, conditions=conditions, planner=planner)
            app.state.saver = saver
            log.info("cocoon backend ready llm_mode=%s%s db=%s", brain.mode,
                     " (MOCK: deterministic responses, no provider calls)" if brain.mode == "mock" else "",
                     settings.db_path)
            try:
                yield
            finally:
                if refresher is not None:
                    refresher.cancel()
                if hasattr(brain, "aclose"):
                    await brain.aclose()
                store.close()

    app = FastAPI(
        title="Cocoon backend API",
        version="1.0.0",
        summary="LangGraph application backend for the Cocoon voice assistant (Team Butterfly).",
        lifespan=lifespan,
    )
    app.state.clock = clock or utcnow  # wall-clock UTC for token expiry

    # ------------------------------------------------------------------ plumbing

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        request.state.request_id = incoming if _REQUEST_ID_RE.match(incoming) else "req_" + uuid.uuid4().hex[:16]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def rid(request: Request) -> str:
        return getattr(request.state, "request_id", "req_unknown")

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _error_response(exc.status, exc.code, exc.message, exc.retryable, rid(request), exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"]), "issue": e["msg"]} for e in exc.errors()]
        return _error_response(422, "validation_error", "request did not match the v1 contract", False,
                               rid(request), details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = {401: "unauthorized", 404: "not_found"}.get(exc.status_code, "internal_error")
        return _error_response(exc.status_code, code, str(exc.detail), exc.status_code >= 500, rid(request))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error request_id=%s", rid(request))
        return _error_response(500, "internal_error", "unexpected server error", True, rid(request))

    def svc(request: Request) -> CocoonService:
        return request.app.state.service

    Service = Annotated[CocoonService, Depends(svc)]

    async def authenticate(
        request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> Principal:
        """One immutable principal per request, resolved on every request (no cache that outlives revocation)."""
        service: CocoonService | None = getattr(request.app.state, "service", None)
        if service is None:
            raise ApiError(503, "auth_unavailable", "authentication is temporarily unavailable", retryable=True)
        return service.authenticate(creds.credentials if creds else None,
                                    len(request.headers.getlist("authorization")), request.app.state.clock())

    Caller = Annotated[Principal, Depends(authenticate)]

    async def service_only(principal: Caller, service: Service) -> Principal:
        service.require_service(principal)
        return principal

    async def session_access(session_id: str, principal: Caller, service: Service) -> s.Session:
        return service.authorize_session(principal, session_id)

    # Dependencies are resolved before the request body is parsed or any tool runs.
    ServiceCaller = Depends(service_only)
    OwnedSession = Depends(session_access)

    def v1(extra: dict[int | str, Any] | None = None, *, access: Any = None) -> dict[str, Any]:
        deps = [Depends(authenticate)] + ([access] if access is not None else [])
        responses = {**ERRORS, **(extra or {})}
        if 503 in (extra or {}):
            responses[503] = {**extra[503], "description": extra[503]["description"] + "; or " + AUTH_UNAVAILABLE}
        return {"dependencies": deps, "responses": responses}

    # ------------------------------------------------------------------ health

    @app.get("/healthz", response_model=s.HealthResponse, tags=["health"])
    async def healthz() -> s.HealthResponse:
        return s.HealthResponse(status="ok")

    @app.get("/readyz", response_model=s.ReadyResponse, tags=["health"],
             responses={503: {"model": s.ReadyResponse, "description": "Not ready"}})
    async def readyz(request: Request, response: Response) -> s.ReadyResponse:
        service: CocoonService | None = getattr(request.app.state, "service", None)
        db_ok = False
        try:
            db_ok = bool(service and service.store.ping())
        except Exception:
            db_ok = False
        cp_ok = getattr(request.app.state, "saver", None) is not None
        catalog = service.catalog if service else None
        schema_version = None
        if db_ok:
            try:
                schema_version = service.store.schema_version()
            except Exception:
                schema_version = None
        ready = db_ok and cp_ok and catalog is not None
        if not ready:
            response.status_code = 503
        return s.ReadyResponse(status="ready" if ready else "not_ready", llm_mode=settings.llm_mode,
                               database=db_ok, checkpointer=cp_ok, version=__version__, catalog=catalog is not None,
                               catalog_version=catalog.manifest_sha256 if catalog else None,
                               catalog_issue=None if catalog else (service.catalog_issue if service else None),
                               schema_version=schema_version)

    # ------------------------------------------------------------------ current principal

    @app.get("/v1/me", response_model=s.MeResponse, tags=["identity"], **v1())
    async def me(principal: Caller, service: Service, response: Response) -> s.MeResponse:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return service.describe(principal)

    # ------------------------------------------------------------------ sessions

    @app.post("/v1/sessions", response_model=s.Session, status_code=201, tags=["sessions"],
              **v1({200: {"model": s.Session, "description": "Existing session for this client_session_key"},
                    409: {"model": s.ErrorResponse,
                          "description": "Key already bound to a different room/participant/operator/machine/"
                                         "site/shift"},
                    422: {"model": s.ErrorResponse,
                          "description": "Malformed request (validation_error), or a NEW session whose machine_id "
                                         "(unknown_machine, checked first) or operator_id (unknown_operator) is not "
                                         "in the verified catalog, or whose site/shift has no trusted binding"},
                    503: {"model": s.ErrorResponse,
                          "description": "catalog_unavailable: no verified catalog loaded; new sessions refused"}},
                   access=ServiceCaller))
    async def create_session(body: s.SessionCreateRequest, service: Service, response: Response) -> s.Session:
        session, created = service.create_session(body)
        response.status_code = 201 if created else 200
        return session

    # ------------------------------------------------------------------ turns

    @app.post(
        "/v1/sessions/{session_id}/turns", response_model=s.TurnResult, tags=["turns"],
        **v1({
            202: {"model": s.TurnResult, "description": (
                "Same turn_id is still processing. Wait retry_after_ms (also the Retry-After header, seconds) "
                "then GET poll_url (also the Location header). Do not resubmit with a new turn_id.")},
            409: {"model": s.ErrorResponse, "description": "turn_id reused with a different text or source"},
            503: {"model": s.ErrorResponse, "description": "LLM unavailable or turn timed out (retryable, same turn_id)"},
        }, access=OwnedSession),
    )
    async def submit_turn(session_id: str, body: s.TurnRequest, service: Service, request: Request,
                          response: Response) -> s.TurnResult:
        status, result = await service.submit_turn(session_id, body, rid(request))
        response.status_code = status
        if status == 202:
            response.headers["Retry-After"] = str(max(1, round(service.settings.turn_poll_after_ms / 1000)))
            response.headers["Location"] = result.poll_url or ""
        return result

    @app.get("/v1/sessions/{session_id}/turns/{turn_id}", response_model=s.TurnResult, tags=["turns"],
             **v1(access=OwnedSession))
    async def get_turn(session_id: str, turn_id: str, service: Service) -> s.TurnResult:
        return service.get_turn(session_id, turn_id)

    # ------------------------------------------------------------------ commands (taps; same service as the graph tools)

    @app.post("/v1/sessions/{session_id}/commands", response_model=s.SessionCommandResult, tags=["commands"],
              **v1({409: {"model": s.ErrorResponse, "description": (
                  "idempotency_conflict (command_id reused with a different payload), version_conflict "
                  "(expected_version is stale; details give the current version) or invalid_transition "
                  "(the task is not in a state this command accepts)")}}, access=OwnedSession))
    async def submit_command(session_id: str, body: s.SessionCommand, service: Service, principal: Caller,
                             session: Annotated[s.Session, OwnedSession]) -> s.SessionCommandResult:
        return service.execute_command(principal, session, body)

    @app.get("/v1/sessions/{session_id}/commands/{command_id}", response_model=s.SessionCommandResult,
             tags=["commands"], **v1(access=OwnedSession))
    async def get_command(session_id: str, command_id: str, service: Service, principal: Caller,
                          session: Annotated[s.Session, OwnedSession]) -> s.SessionCommandResult:
        return service.get_command(principal, session, command_id)

    # ------------------------------------------------------------------ state

    @app.get("/v1/sessions/{session_id}/state", response_model=s.SessionState, tags=["state"],
             **v1(access=OwnedSession))
    async def get_state(session_id: str, service: Service) -> s.SessionState:
        return await service.get_state(session_id)

    # ------------------------------------------------------------------ telemetry

    @app.post("/v1/sessions/{session_id}/telemetry", response_model=s.TelemetryResult, tags=["telemetry"],
              **v1({409: {"model": s.ErrorResponse, "description": "event_id reused with a different payload"}},
                   access=ServiceCaller))
    async def submit_telemetry(session_id: str, body: s.TelemetryRequest, service: Service) -> s.TelemetryResult:
        return await service.submit_telemetry(session_id, body)

    # ------------------------------------------------------------------ announcements

    @app.get("/v1/sessions/{session_id}/events", response_model=s.EventsPage, tags=["announcements"],
             **v1(access=OwnedSession))
    async def list_events(
        session_id: str, service: Service,
        after: Annotated[int, Query(ge=0, description="Return announcements with sequence > after")] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> s.EventsPage:
        return service.list_events(session_id, after, limit)

    @app.post("/v1/sessions/{session_id}/events/{event_id}/delivery", response_model=s.DeliveryRecord,
              tags=["announcements"], **v1(access=ServiceCaller))
    async def record_delivery(session_id: str, event_id: str, body: s.DeliveryReport,
                              service: Service) -> s.DeliveryRecord:
        return service.record_delivery(session_id, event_id, body)

    return app
