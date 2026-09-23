"""Guarded streaming from the brain into TTS.

- Every generation gets an epoch; chunks from a superseded generation are dropped.
- Retries (bounded, jittered backoff) only happen before the first chunk is emitted, so a
  partially spoken answer is never restarted from the beginning.
- First-chunk and inter-chunk (stall) timeouts end a hung provider stream.
- Optional one-off thinking cue if the first substantive text is slow; skipped once text is ready.
- The provider stream task is always cancelled and closed on exit (interruption/shutdown).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import APIError, FlushSentinel, llm


log = logging.getLogger("cocoon_voice.streaming")

_END = object()


class StreamStalled(Exception):
    def __init__(self, phase: str):
        super().__init__(f"brain stream stalled ({phase})")
        self.phase = phase


class EpochCounter:
    def __init__(self) -> None:
        self.current = 0

    def next(self) -> int:
        self.current += 1
        return self.current


@dataclass
class GenerationStats:
    epoch: int
    started: float = field(default_factory=time.perf_counter)
    first_chunk_at: float | None = None
    cue_at: float | None = None
    attempts: int = 0
    chars: int = 0
    outcome: str = "running"  # completed | empty | failed_before_output | failed_after_partial | cancelled | stale
    error_category: str | None = None

    def ms(self, t: float | None) -> float | None:
        return None if t is None else round((t - self.started) * 1000, 1)


def chunk_text(chunk: Any) -> str:
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, llm.ChatChunk) and chunk.delta is not None:
        return chunk.delta.content or ""
    return ""


def classify_error(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    text = str(exc).lower()
    if isinstance(exc, StreamStalled):
        return f"timeout:{exc.phase}"
    if status in (401,) or "unauthenticated" in text or "default credentials" in text:
        return "auth"
    if status == 403 or "permission" in text:
        return "permission"
    if status == 429 or "resource_exhausted" in text or "quota" in text:
        return "rate_limit"
    if status == 404:
        return "model_unavailable"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, APIError):
        return "provider_error"
    return type(exc).__name__


async def guarded_stream(
    make_stream: Callable[[], AsyncIterable[Any]],
    *,
    epoch: int,
    epochs: EpochCounter,
    first_chunk_timeout: float,
    stall_timeout: float,
    max_attempts: int,
    cue_text: str | None,
    cue_delay: float,
    failure_text: str,
    empty_text: str,
    stats: GenerationStats | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> AsyncIterator[Any]:
    """Yield brain chunks (str / ChatChunk / FlushSentinel) with the guarantees above."""
    stats = stats or GenerationStats(epoch=epoch)
    emitted = False
    cue_sent = False
    try:
        while True:
            stats.attempts += 1
            queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)
            producer = asyncio.create_task(_pump(make_stream, queue))
            try:
                waited = 0.0
                while True:
                    if epochs.current != epoch:
                        stats.outcome = "stale"
                        log.info("dropping output of superseded generation epoch=%d", epoch)
                        return
                    if not emitted:
                        budget = first_chunk_timeout - waited
                        wait = min(budget, cue_delay - waited) if (cue_text and not cue_sent) else budget
                    else:
                        wait = stall_timeout
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=max(0.0, wait))
                    except asyncio.TimeoutError:
                        if not emitted:
                            waited += max(0.0, wait)
                            if cue_text and not cue_sent and waited >= cue_delay - 1e-6 and waited < first_chunk_timeout - 1e-6:
                                cue_sent = True
                                stats.cue_at = time.perf_counter()
                                yield cue_text
                                yield FlushSentinel()
                                continue
                            raise StreamStalled("first_chunk")
                        raise StreamStalled("mid_stream")
                    if item is _END:
                        break
                    if isinstance(item, BaseException):
                        raise item
                    if epochs.current != epoch:
                        stats.outcome = "stale"
                        return
                    text = chunk_text(item)
                    if text and not emitted:
                        emitted = True
                        stats.first_chunk_at = time.perf_counter()
                    stats.chars += len(text)
                    yield item
            except (APIError, StreamStalled, asyncio.TimeoutError, ConnectionError, OSError) as exc:
                stats.error_category = classify_error(exc)
                if emitted:
                    stats.outcome = "failed_after_partial"
                    log.warning("brain failed after partial output (%s); not retrying to avoid repeating speech",
                                stats.error_category)
                    return
                if stats.attempts >= max_attempts:
                    stats.outcome = "failed_before_output"
                    log.error("brain failed before output after %d attempt(s) (%s)", stats.attempts,
                              stats.error_category)
                    yield failure_text
                    return
                delay = min(2.0, 0.3 * 2 ** (stats.attempts - 1)) * random.uniform(0.7, 1.3)
                log.warning("brain attempt %d failed (%s); retrying in %.2fs", stats.attempts,
                            stats.error_category, delay)
                await sleep(delay)
                continue
            finally:
                producer.cancel()
                try:
                    await producer
                except (asyncio.CancelledError, Exception):
                    pass
            if not emitted:
                stats.outcome = "empty"
                yield empty_text
            else:
                stats.outcome = "completed"
            return
    except (asyncio.CancelledError, GeneratorExit):
        stats.outcome = "cancelled"
        raise


async def _pump(make_stream: Callable[[], AsyncIterable[Any]], queue: asyncio.Queue[Any]) -> None:
    stream = make_stream()
    try:
        async for chunk in stream:
            await queue.put(chunk)
        await queue.put(_END)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # handed to the consumer, which decides on retry
        await queue.put(exc)
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass
