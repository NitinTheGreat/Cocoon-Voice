"""Run the backend with insights: `python -m cocoon_agent.insights` (same host, port and .env as `cocoon_agent`)."""

from __future__ import annotations

import logging

import uvicorn

from ..config import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # One worker, as for the core backend: per-session ordering locks are in-process.
    uvicorn.run("cocoon_agent.insights.app:create_app", factory=True, host=settings.host, port=settings.port,
                workers=1, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
