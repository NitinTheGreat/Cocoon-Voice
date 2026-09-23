from __future__ import annotations

import asyncio
import os
from pathlib import Path

# Tests never need real LiveKit credentials; set harmless placeholders before imports read them.
os.environ.setdefault("COCOON_SERVICE_TOKEN", "test-token")
os.environ.setdefault("LIVEKIT_URL", "wss://placeholder.invalid")
os.environ.setdefault("LIVEKIT_API_KEY", "placeholder")
os.environ.setdefault("LIVEKIT_API_SECRET", "placeholder")

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "contracts"


class FakeClock:
    """Deterministic time: sleeping advances the clock instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)  # still yield to the event loop like a real sleep
