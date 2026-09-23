"""Porcupine acoustic wake mode: keyword spotting on the selected participant's room audio.

The router is the ONLY consumer of the agent's audio input stream in this mode (the frames
arrive after the configured Krisp input filter). Every frame is fed to the keyword engine at
the engine's own sample rate/frame length; frames reach STT only inside an ACTIVE segment,
starting with a short bounded pre-roll so "Hey Cat, <question>" is not clipped. Each frame is
forwarded at most once. Nothing here opens the worker machine's microphone.
"""

from __future__ import annotations

import array
import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Protocol

from livekit import rtc

from .wake import WakeGate, WakeState

log = logging.getLogger("cocoon_voice.porcupine")


class KeywordEngine(Protocol):
    sample_rate: int
    frame_length: int

    def process(self, pcm: Sequence[int]) -> int: ...

    def delete(self) -> None: ...


class PorcupineEngine:
    """Thin wrapper over pvporcupine with a real custom keyword file (.ppn). No built-in fallback."""

    def __init__(self, access_key: str, keyword_path: Path, sensitivity: float):
        import pvporcupine

        if not keyword_path.is_file():
            raise FileNotFoundError("PORCUPINE_KEYWORD_PATH does not exist")
        self._porcupine = pvporcupine.create(
            access_key=access_key, keyword_paths=[str(keyword_path)], sensitivities=[sensitivity]
        )
        self.sample_rate: int = self._porcupine.sample_rate
        self.frame_length: int = self._porcupine.frame_length
        self.version: str = getattr(self._porcupine, "version", "unknown")

    def process(self, pcm: Sequence[int]) -> int:
        return self._porcupine.process(pcm)

    def delete(self) -> None:
        self._porcupine.delete()


class _Segment:
    """Bounded frame queue for one ACTIVE period; ends with a sentinel."""

    def __init__(self, max_frames: int):
        self.queue: asyncio.Queue[rtc.AudioFrame | None] = asyncio.Queue(maxsize=max_frames + 1)
        self.max_frames = max_frames
        self.dropped = 0
        self.closed = False

    def put(self, frame: rtc.AudioFrame) -> None:
        if self.closed:
            return
        if self.queue.qsize() >= self.max_frames:
            self.queue.get_nowait()  # drop oldest: STT is behind; keep latency bounded
            self.dropped += 1
        self.queue.put_nowait(frame)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self.queue.full():
                self.queue.get_nowait()
            self.queue.put_nowait(None)

    async def frames(self) -> AsyncIterator[rtc.AudioFrame]:
        while True:
            frame = await self.queue.get()
            if frame is None:
                return
            yield frame


class AcousticRouter:
    def __init__(
        self,
        engine: KeywordEngine,
        gate: WakeGate,
        *,
        preroll_ms: int,
        on_wake: Callable[[], None],
        max_segment_s: float = 12.0,
    ):
        self.engine = engine
        self.gate = gate
        self.preroll_ms = preroll_ms
        self.on_wake = on_wake
        self._preroll: deque[rtc.AudioFrame] = deque()
        self._preroll_s = 0.0
        self._pcm = array.array("h")
        self._resampler: rtc.AudioResampler | None = None
        self._resample_from: int | None = None
        self._segment: _Segment | None = None
        self._segments: asyncio.Queue[_Segment | None] = asyncio.Queue()
        self._max_segment_frames = max(10, int(max_segment_s * 50))  # ~20 ms frames
        self.frames_in = 0
        self.frames_forwarded = 0
        self.engine_frames = 0
        self.detections = 0

    # ------------------------------------------------------------------ consumer side

    async def next_segment(self) -> AsyncIterable[rtc.AudioFrame] | None:
        segment = await self._segments.get()
        return segment.frames() if segment is not None else None

    # ------------------------------------------------------------------ producer side

    async def run(self, audio: AsyncIterable[rtc.AudioFrame]) -> None:
        try:
            async for frame in audio:
                self.handle_frame(frame)
        finally:
            self._close_segment()
            self._segments.put_nowait(None)

    def handle_frame(self, frame: rtc.AudioFrame) -> None:
        self.frames_in += 1
        forwarding = self._segment is not None
        if not forwarding:
            self._remember(frame)
        detected = self._detect(frame)
        if detected and self.gate.on_acoustic_wake():
            if self._segment is None:
                self._open_segment(with_preroll=True)
                forwarding = False  # this frame was delivered via pre-roll
            self.on_wake()
        if self.gate.state == WakeState.ACTIVE:
            if self._segment is None:
                self._open_segment(with_preroll=False)
                forwarding = True
            if forwarding:
                self._segment.put(frame)
                self.frames_forwarded += 1
        elif self._segment is not None:
            self._close_segment()

    def _remember(self, frame: rtc.AudioFrame) -> None:
        self._preroll.append(frame)
        self._preroll_s += frame.duration
        while self._preroll and self._preroll_s - self._preroll[0].duration >= self.preroll_ms / 1000:
            self._preroll_s -= self._preroll.popleft().duration

    def _open_segment(self, *, with_preroll: bool) -> None:
        self._segment = _Segment(self._max_segment_frames)
        if with_preroll:
            for f in self._preroll:
                self._segment.put(f)
                self.frames_forwarded += 1
        self._preroll.clear()
        self._preroll_s = 0.0
        self._segments.put_nowait(self._segment)

    def _close_segment(self) -> None:
        if self._segment is not None:
            if self._segment.dropped:
                log.warning("STT segment dropped %d frame(s) under backpressure", self._segment.dropped)
            self._segment.close()
            self._segment = None

    def _detect(self, frame: rtc.AudioFrame) -> bool:
        detected = False
        self._pcm.extend(self._to_engine_pcm(frame))
        n = self.engine.frame_length
        while len(self._pcm) >= n:
            chunk = self._pcm[:n]
            del self._pcm[:n]
            self.engine_frames += 1
            if self.engine.process(chunk) >= 0:
                self.detections += 1
                detected = True
        return detected

    def _to_engine_pcm(self, frame: rtc.AudioFrame) -> array.array:
        samples = array.array("h", bytes(frame.data.cast("B")))
        if frame.num_channels > 1:
            samples = samples[::frame.num_channels]  # first channel
            frame = rtc.AudioFrame(samples.tobytes(), frame.sample_rate, 1, len(samples))
        if frame.sample_rate == self.engine.sample_rate:
            return samples
        if self._resampler is None or self._resample_from != frame.sample_rate:
            self._resampler = rtc.AudioResampler(frame.sample_rate, self.engine.sample_rate, num_channels=1)
            self._resample_from = frame.sample_rate
        out = array.array("h")
        for resampled in self._resampler.push(frame):
            out.frombytes(bytes(resampled.data.cast("B")))
        return out
