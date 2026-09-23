"""The existing backend app plus insights, built without modifying the existing app factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..api.app import create_app as create_core_app
from .api import router
from .capture import InsightsCaptureMiddleware
from .db import InsightsStore
from .settings import get_insights_settings

log = logging.getLogger("cocoon_agent.insights")


def install(app: FastAPI) -> FastAPI:
    """Attach insights routes, capture and the PostgreSQL pool to an existing backend app."""
    settings = get_insights_settings()
    app.state.insights = None
    if not settings.enabled:
        log.info("insights disabled (INSIGHTS_ENABLED=false)")
        return app
    app.include_router(router)
    app.add_middleware(InsightsCaptureMiddleware)
    core_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        async with core_lifespan(a) as state:
            try:
                a.state.insights = await InsightsStore.open(settings)
                log.info("insights ready: PostgreSQL %s (schema ok)", settings.describe())
            except Exception as exc:  # the core API must keep serving voice turns without PostgreSQL
                a.state.insights = None
                log.error("insights NOT available: cannot use PostgreSQL %s (%s: %s); core API unaffected",
                          settings.describe(), type(exc).__name__, str(exc).splitlines()[0][:200] if str(exc) else "")
            try:
                yield state
            finally:
                if a.state.insights is not None:
                    await a.state.insights.close()

    app.router.lifespan_context = lifespan
    return app


def create_app() -> FastAPI:
    return install(create_core_app())
