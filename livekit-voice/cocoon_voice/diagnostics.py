"""Runtime diagnostics for one voice session (non-secret, no transcripts).

- LoopLagMonitor: how late the asyncio event loop wakes up. Blocking work on the loop delays
  audio frames (choppy speech), STT/LLM stream reads and interruption handling alike.
- TtsPacing: per reply, how far TTS audio production stays ahead of real-time playback. A negative
  margin means the output had to wait for synthesis (a gap the listener can hear).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from .observability import percentile

log = logging.getLogger("cocoon_voice.diagnostics")


class LoopLagMonitor:
    def __init__(self, interval_s: float = 0.05, warn_ms: float = 150.0, keep: int = 20000):
        self.interval_s = interval_s
        self.warn_ms = warn_ms
        self.samples: deque[float] = deque(maxlen=keep)
        self.blocks_over_warn = 0
        self._last_warn = 0.0

    async def run(self) -> None:
        while True:
            start = time.perf_counter()
            await asyncio.sleep(self.interval_s)
            lag_ms = (time.perf_counter() - start - self.interval_s) * 1000
            self.samples.append(lag_ms)
            if lag_ms >= self.warn_ms:
                self.blocks_over_warn += 1
                if time.perf_counter() - self._last_warn > 5:
                    self._last_warn = time.perf_counter()
                    log.warning("event loop lag %.0f ms (blocking work on the audio loop)", lag_ms)

    def summary(self) -> dict:
        values = list(self.samples)
        return {"n": len(values), "p50_ms": percentile(values, 50), "p95_ms": percentile(values, 95),
                "p99_ms": percentile(values, 99), "max_ms": round(max(values), 1) if values else None,
                f"over_{int(self.warn_ms)}ms": self.blocks_over_warn}


class TtsPacing:
    """Tracks one TTS output stream: audio produced vs. wall time since the first frame."""

    def __init__(self) -> None:
        self.first_frame_at: float | None = None
        self.audio_s = 0.0
        self.min_margin_ms: float | None = None
        self.frames = 0

    def on_frame(self, duration_s: float) -> None:
        now = time.perf_counter()
        if self.first_frame_at is None:
            self.first_frame_at = now
        # margin before this frame was produced: audio already produced minus audio already due for playback
        margin_ms = (self.audio_s - (now - self.first_frame_at)) * 1000
        if self.frames > 0:
            self.min_margin_ms = margin_ms if self.min_margin_ms is None else min(self.min_margin_ms, margin_ms)
        self.audio_s += duration_s
        self.frames += 1

    @property
    def underran(self) -> bool:
        return self.min_margin_ms is not None and self.min_margin_ms < 0
