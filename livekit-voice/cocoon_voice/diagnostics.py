"""Runtime diagnostics for one voice session (non-secret, no transcripts).

- LoopLagMonitor: how late the asyncio event loop wakes up. Blocking work on the loop delays
  audio frames (choppy speech), STT/LLM stream reads and interruption handling alike.
- TtsPacing: per reply, how far TTS audio production stays ahead of real-time playback. A negative
  margin means the output had to wait for synthesis (a gap the listener can hear).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque

import numpy as np

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


class SttInputMonitor:
    """Classifies a quiet STT window so the three failure kinds are not confused.

    - no audio frames reached the STT node (not subscribed, track muted/unpublished, input stalled)
    - audio frames arrived but were silent (muted microphone, nobody speaking, or filtered to silence)
    - speech-level audio was sent but the STT returned no events (provider side)
    "AssemblyAI no messages received" from the plugin alone only means the provider sent nothing, which is
    normal while the operator is silent; the plugin does not reconnect because of it.
    """

    SILENT_DBFS = -50.0

    def __init__(self, window_s: float = 15.0) -> None:
        self.window_s = window_s
        self._reset()

    def _reset(self) -> None:
        self.frames = 0
        self.audio_s = 0.0
        self.peak = 0
        self.events = 0
        self.finals = 0

    def on_frame(self, frame) -> None:
        self.frames += 1
        self.audio_s += frame.samples_per_channel / max(1, frame.sample_rate)
        data = np.frombuffer(frame.data, dtype=np.int16)
        if data.size:
            self.peak = max(self.peak, int(data.max()), -int(data.min()))

    def on_event(self, *, final: bool) -> None:
        self.events += 1
        self.finals += int(final)

    def close_window(self) -> tuple[str, str] | None:
        """(kind, message) for a window without STT events, or None if STT produced events; then resets.

        kind: "no_audio" | "silent_audio" | "no_transcripts"
        """
        try:
            if self.events:
                return None
            if self.frames == 0:
                return "no_audio", (f"no audio frames reached STT in {self.window_s:.0f}s "
                                    "(participant track not subscribed, muted/unpublished, or input stalled)")
            peak_dbfs = 20 * math.log10(max(self.peak, 1) / 32768)
            if peak_dbfs < self.SILENT_DBFS:
                return "silent_audio", (f"{self.audio_s:.0f}s of audio reached STT but it was silent "
                                        f"(peak {peak_dbfs:.0f} dBFS): microphone muted, nobody speaking, "
                                        "or filtered to silence")
            return "no_transcripts", (f"{self.audio_s:.0f}s of audio with speech-level peaks ({peak_dbfs:.0f} dBFS) "
                                      "was sent to STT but no transcripts or speech events came back")
        finally:
            self._reset()


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

    # Flag only a lag of more than one 20 ms frame (a heuristic, not a listening result). Live runs after the
    # Krisp fix measured -1..-71 ms; the earlier -404/-685 ms runs coincided with 105-339 ms loop blocks.
    UNDERRUN_MS = -20.0

    @property
    def underran(self) -> bool:
        return self.min_margin_ms is not None and self.min_margin_ms < self.UNDERRUN_MS
