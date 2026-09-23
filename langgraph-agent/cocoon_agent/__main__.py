"""Run the backend: `python -m cocoon_agent` (or the `cocoon-agent` script)."""

from __future__ import annotations

import logging

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # One worker: per-session ordering locks and the in-flight turn registry are in-process.
    uvicorn.run(
        "cocoon_agent.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
