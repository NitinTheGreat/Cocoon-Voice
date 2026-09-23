"""Cached audio for short, fixed, non-personal phrases (greeting, wake ack, sleep ack, error).

Keyed by provider/model/voice/language/speed/sample-rate/text, so changing any of those
invalidates the entry. Stored in memory and under TTS_CACHE_DIR as raw PCM16 + JSON metadata,
so reconnects and worker restarts do not pay for re-synthesis. Personal or LLM text is never cached.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from livekit import rtc

log = logging.getLogger("cocoon_voice.phrase_cache")

FRAME_MS = 20


@dataclass(frozen=True)
class CachedAudio:
    pcm: bytes
    sample_rate: int
    num_channels: int

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / 2 / self.num_channels / self.sample_rate

    async def frames(self) -> AsyncIterator[rtc.AudioFrame]:
        step = self.sample_rate * FRAME_MS // 1000 * self.num_channels * 2
        for i in range(0, len(self.pcm), step):
            chunk = self.pcm[i:i + step]
            yield rtc.AudioFrame(chunk, self.sample_rate, self.num_channels, len(chunk) // 2 // self.num_channels)


_memory: dict[str, CachedAudio] = {}
_memory_lock = threading.Lock()  # jobs may run as threads in one worker process


def cache_key(*, provider: str, model: str, voice: str, language: str, speed: float | None, sample_rate: int,
              text: str) -> str:
    raw = json.dumps([provider, model, voice, language, speed, sample_rate, text], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class PhraseCache:
    def __init__(self, tts, *, cache_dir: Path, provider: str, model: str, voice: str, language: str,
                 speed: float | None):
        self._tts = tts
        self._dir = cache_dir
        self._meta = dict(provider=provider, model=model, voice=voice, language=language, speed=speed,
                          sample_rate=tts.sample_rate)
        self._inflight: dict[str, asyncio.Future[CachedAudio]] = {}

    def key(self, text: str) -> str:
        return cache_key(text=text, **self._meta)

    async def get(self, text: str) -> CachedAudio:
        key = self.key(text)
        with _memory_lock:
            hit = _memory.get(key)
        if hit is not None:
            return hit
        disk = self._load(key)
        if disk is not None:
            with _memory_lock:
                _memory[key] = disk
            return disk
        if key in self._inflight:
            return await self._inflight[key]
        fut: asyncio.Future[CachedAudio] = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            audio = await self._synthesize(text)
            with _memory_lock:
                _memory[key] = audio
            self._save(key, text, audio)
            fut.set_result(audio)
            return audio
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved
            raise
        finally:
            self._inflight.pop(key, None)

    async def prefetch(self, texts: list[str]) -> None:
        """Warm the cache outside the critical path; failures are logged, not fatal."""
        for text in texts:
            try:
                await self.get(text)
            except Exception as exc:
                log.warning("could not pre-synthesize a fixed phrase (%s)", type(exc).__name__)

    async def _synthesize(self, text: str) -> CachedAudio:
        pcm = bytearray()
        sample_rate, channels = self._tts.sample_rate, self._tts.num_channels
        async with self._tts.synthesize(text) as stream:
            async for ev in stream:
                frame = ev.frame
                sample_rate, channels = frame.sample_rate, frame.num_channels
                pcm += bytes(frame.data.cast("B"))
        if not pcm:
            raise RuntimeError("TTS returned no audio for a fixed phrase")
        return CachedAudio(bytes(pcm), sample_rate, channels)

    def _load(self, key: str) -> CachedAudio | None:
        meta_path, pcm_path = self._dir / f"{key}.json", self._dir / f"{key}.pcm"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return CachedAudio(pcm_path.read_bytes(), int(meta["sample_rate"]), int(meta["num_channels"]))
        except (OSError, ValueError, KeyError):
            return None

    def _save(self, key: str, text: str, audio: CachedAudio) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / f"{key}.pcm").write_bytes(audio.pcm)
            meta = {**self._meta, "text": text, "sample_rate": audio.sample_rate, "num_channels": audio.num_channels}
            (self._dir / f"{key}.json").write_text(json.dumps(meta), encoding="utf-8")
        except OSError as exc:
            log.warning("phrase cache not persisted (%s)", type(exc).__name__)


def clear_memory_cache() -> None:
    with _memory_lock:
        _memory.clear()
