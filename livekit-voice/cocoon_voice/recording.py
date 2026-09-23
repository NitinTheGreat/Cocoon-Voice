"""Opt-in local test recordings (RECORD_AUDIO=true, development only) with retention cleanup.

Records the participant audio exactly as the worker hears it (after the Krisp input filter),
as 16-bit WAV. Nothing is recorded by default and nothing is uploaded.
"""

from __future__ import annotations

import logging
import time
import wave
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path

from livekit import rtc

log = logging.getLogger("cocoon_voice.recording")


def cleanup_recordings(directory: Path, retention_hours: float) -> int:
    if not directory.exists():
        return 0
    cutoff = time.time() - retention_hours * 3600
    removed = 0
    for path in directory.glob("*.wav"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


class InputRecorder:
    def __init__(self, directory: Path, label: str):
        directory.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)[:60]
        self.path = directory / f"{time.strftime('%Y%m%dT%H%M%S')}-{safe}.wav"
        self._wav: wave.Wave_write | None = None

    async def tap(self, audio: AsyncIterable[rtc.AudioFrame]) -> AsyncIterator[rtc.AudioFrame]:
        async for frame in audio:
            self.write(frame)
            yield frame

    def write(self, frame: rtc.AudioFrame) -> None:
        if self._wav is None:
            self._wav = wave.open(str(self.path), "wb")
            self._wav.setnchannels(frame.num_channels)
            self._wav.setsampwidth(2)
            self._wav.setframerate(frame.sample_rate)
            log.info("recording participant input to %s (opt-in)", self.path.name)
        self._wav.writeframes(bytes(frame.data.cast("B")))

    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None
