"""/v1/insights routes: onboarding, history and analytics. Errors use the backend's standard error envelope.

Auth reuses the backend's bearer credentials:
- The trusted service token may read and write any employee.
- An operator actor token (scripts/actor_tokens.py) may use only its own employee ID; anything else gets 404.
- GET /v1/insights/machines and the dashboard page are public (they contain no personal data).
"""

from __future__ import annotations

import functools
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg import Error as PsycopgError
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict, Field

from ..auth import Principal
from ..service import ApiError
from .settings import get_insights_settings
from .workflow import build_insights_chain

router = APIRouter(tags=["insights"])
_EMPLOYEE_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
_DASHBOARD = Path(__file__).with_name("dashboard.html")


class OnboardingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: str = Field(min_length=2, max_length=64)
    machine_id: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=120)


def _service(request: Request):
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise ApiError(503, "auth_unavailable", "authentication is temporarily unavailable", retryable=True)
    return service


def _store(request: Request):
    store = getattr(request.app.state, "insights", None)
    if store is None:
        # the backend's error envelope has a fixed code set; the message names the actual cause
        raise ApiError(503, "internal_error", "the insights database (PostgreSQL) is not connected", retryable=True)
    return store


def _principal(request: Request) -> Principal:
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else None
    return _service(request).authenticate(token, len(request.headers.getlist("authorization")),
                                          request.app.state.clock())


def _authorize(request: Request, employee_id: str) -> Principal:
    principal = _principal(request)
    if principal.kind == "service" or (principal.kind == "operator" and principal.operator_id == employee_id):
        return principal
    raise ApiError(404, "not_found", "employee not found")  # existence is not revealed


def _machines(request: Request) -> dict[str, Any]:
    catalog = getattr(_service(request), "catalog", None)
    if catalog is None:
        raise ApiError(503, "catalog_unavailable", "no verified machine catalog is loaded")
    return catalog.machines


def _db_errors(fn):
    """A PostgreSQL failure mid-request is a retryable 503, not an unhandled 500."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (PsycopgError, PoolTimeout) as exc:
            raise ApiError(503, "internal_error", "the insights database (PostgreSQL) is unavailable",
                           retryable=True) from exc

    return wrapper


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


@router.get("/v1/insights/machines")
async def list_machines(request: Request) -> dict[str, Any]:
    """The machines an employee can choose from at onboarding (the verified dataset catalog)."""
    machines = _machines(request)
    return {"machines": [{"machine_id": m.machine_id, "model": m.model, "category": m.category}
                         for m in sorted(machines.values(), key=lambda m: m.machine_id)]}


@router.post("/v1/insights/users")
@_db_errors
async def onboard(request: Request, body: OnboardingRequest) -> Any:
    """Create or update an employee's profile (first-time onboarding or a machine change)."""
    _authorize(request, body.employee_id)
    settings, service = get_insights_settings(), _service(request)
    machines = _machines(request)
    machine = machines.get(body.machine_id)
    if machine is None:
        raise ApiError(422, "unknown_machine", "machine_id is not in the verified catalog",
                       details=[{"field": "body.machine_id", "issue": "unknown machine"}])
    if not _EMPLOYEE_RE.match(body.employee_id):
        raise ApiError(422, "validation_error", "employee_id may contain only letters, digits, '_', '.', '-'",
                       details=[{"field": "body.employee_id", "issue": "invalid format"}])
    if settings.require_catalog_employee and body.employee_id not in (service.catalog.operators or ()):
        raise ApiError(422, "unknown_operator", "employee_id is not a catalog operator ID (e.g. OP_DEMO_1_1)",
                       details=[{"field": "body.employee_id", "issue": "unknown employee"}])
    user, created = await _store(request).upsert_user(body.employee_id, machine.machine_id, machine.model,
                                                      machine.category, body.display_name)
    return JSONResponse(_jsonable({**user, "created": created}), status_code=201 if created else 200)


@router.get("/v1/insights/users/{employee_id}")
@_db_errors
async def get_profile(request: Request, employee_id: str) -> dict[str, Any]:
    _authorize(request, employee_id)
    user = await _store(request).get_user(employee_id)
    if user is None:
        raise ApiError(404, "not_found", "employee not found (not onboarded)")
    return _jsonable(user)


@router.get("/v1/insights/users/{employee_id}/sessions")
@_db_errors
async def list_sessions(request: Request, employee_id: str, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    _authorize(request, employee_id)
    rows = await _store(request).list_sessions(employee_id, limit)
    return {"employee_id": employee_id, "sessions": [_jsonable(r) for r in rows]}


@router.get("/v1/insights/users/{employee_id}/interactions")
@_db_errors
async def list_interactions(request: Request, employee_id: str, limit: int = Query(100, ge=1, le=500),
                            session_id: str | None = None) -> dict[str, Any]:
    _authorize(request, employee_id)
    rows = await _store(request).list_interactions(employee_id, limit, session_id)
    return {"employee_id": employee_id, "interactions": [_jsonable(r) for r in rows]}


@router.get("/v1/insights/users/{employee_id}/analytics")
@_db_errors
async def analytics(request: Request, employee_id: str) -> dict[str, Any]:
    """Runs the LangChain analytics workflow over the stored history and saves the report."""
    _authorize(request, employee_id)
    store = _store(request)
    service = _service(request)
    user = await store.get_user(employee_id)
    sessions = await store.list_sessions(employee_id, 200)
    interactions = await store.list_interactions(employee_id, 1000)
    if user is None and not sessions:
        raise ApiError(404, "not_found", "employee not found (not onboarded and no sessions)")
    chain = getattr(request.app.state, "insights_chain", None)
    if chain is None:
        chain = request.app.state.insights_chain = build_insights_chain(service.settings)
    report = await chain.ainvoke({"employee": user, "sessions": sessions, "interactions": interactions,
                                  "now": datetime.now(timezone.utc)})
    report["employee_id"] = employee_id
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["report_id"] = await store.save_report(employee_id, service.settings.llm_mode, report)
    return report


@router.get("/insights/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD.read_text(encoding="utf-8"))
