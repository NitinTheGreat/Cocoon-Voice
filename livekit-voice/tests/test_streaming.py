"""Streaming guard: incremental output, cue, bounded retries, no replay after partial output,
stalls, empty output, stale-epoch suppression, cancellation cleanup; plus fixed-phrase cache."""

from __future__ import annotations

import asyncio
import time

import pytest
from livekit.agents import AgentSession, FlushSentinel

from cocoon_voice.agent import CatAgent, session_conn_options
from cocoon_voice.phrase_cache import PhraseCache, clear_memory_cache
from cocoon_voice.streaming import EpochCounter, GenerationStats, guarded_stream

from .fakes import FakeLLM, Step, transient_error
from .test_controller import controller


def scripted(*scripts: list[Step]):
    """make_stream factory: each call replays the next script; records aclose()."""
    state = {"calls": 0, "closed": 0}

    def make():
        steps = scripts[min(state["calls"], len(scripts) - 1)]
        state["calls"] += 1

        async def gen():
            try:
                for s in steps:
                    if s.delay:
                        await asyncio.sleep(s.delay)
                    if s.error:
                        raise s.error
                    yield s.text
            finally:
                state["closed"] += 1

        return gen()

    return make, state


async def collect(stream, *, timing: bool = False):
    out, times = [], []
    t0 = time.perf_counter()
    async for chunk in stream:
        out.append(chunk)
        times.append(time.perf_counter() - t0)
    return (out, times) if timing else out


def guard(make, epochs=None, epoch=None, **kw):
    epochs = epochs or EpochCounter()
    epoch = epoch if epoch is not None else epochs.next()
    params = dict(first_chunk_timeout=1.0, stall_timeout=0.5, max_attempts=2, cue_text=None, cue_delay=0.3,
                  failure_text="FAILED", empty_text="EMPTY", sleep=lambda s: asyncio.sleep(0))
    params.update(kw)
    stats = GenerationStats(epoch=epoch)
    return guarded_stream(make, epoch=epoch, epochs=epochs, stats=stats, **params), stats


async def test_text_is_forwarded_incrementally_before_completion():
    make, _ = scripted([Step("First sentence. "), Step("Second one.", delay=0.3)])
    stream, stats = guard(make)
    out, times = await collect(stream, timing=True)
    assert out == ["First sentence. ", "Second one."]
    assert times[0] < 0.1 and times[1] >= 0.25  # first chunk not held back until the stream ends
    assert stats.outcome == "completed" and stats.attempts == 1


async def test_retry_only_before_first_chunk():
    make, state = scripted([Step(error=transient_error())], [Step("Recovered answer.")])
    stream, stats = guard(make)
    assert await collect(stream) == ["Recovered answer."]
    assert state["calls"] == 2 and stats.attempts == 2


async def test_no_replay_after_partial_output():
    make, state = scripted([Step("The answer starts "), Step(error=transient_error())], [Step("DUPLICATE")])
    stream, stats = guard(make)
    assert await collect(stream) == ["The answer starts "]  # nothing repeated, no second attempt
    assert state["calls"] == 1 and stats.outcome == "failed_after_partial"


async def test_failure_before_output_speaks_a_short_apology_after_bounded_attempts():
    make, state = scripted([Step(error=transient_error())])
    stream, stats = guard(make, max_attempts=2)
    assert await collect(stream) == ["FAILED"]
    assert state["calls"] == 2 and stats.outcome == "failed_before_output"
    assert stats.error_category == "provider_error"


async def test_stalled_first_chunk_and_mid_stream_stall_are_bounded():
    make, _ = scripted([Step("late", delay=5)])
    stream, stats = guard(make, first_chunk_timeout=0.2, max_attempts=1)
    t0 = time.perf_counter()
    assert await collect(stream) == ["FAILED"]
    assert time.perf_counter() - t0 < 1.0 and stats.error_category == "timeout:first_chunk"

    make, _ = scripted([Step("Partial "), Step("never", delay=5)])
    stream, stats = guard(make, stall_timeout=0.2)
    assert await collect(stream) == ["Partial "] and stats.error_category == "timeout:mid_stream"


async def test_empty_output_gets_a_fallback():
    make, _ = scripted([Step("")])
    stream, stats = guard(make)
    assert await collect(stream) == ["EMPTY"] and stats.outcome == "empty"


async def test_thinking_cue_only_when_slow_and_only_once():
    make, _ = scripted([Step("Answer.", delay=0.5)])
    stream, stats = guard(make, cue_text="One moment.", cue_delay=0.2, first_chunk_timeout=2.0)
    out = await collect(stream)
    assert out[0] == "One moment." and isinstance(out[1], FlushSentinel) and out[2:] == ["Answer."]
    assert stats.cue_at is not None and stats.first_chunk_at > stats.cue_at

    make, _ = scripted([Step("Fast answer.", delay=0.05)])
    stream, stats = guard(make, cue_text="One moment.", cue_delay=0.3)
    assert await collect(stream) == ["Fast answer."] and stats.cue_at is None


async def test_superseded_epoch_drops_late_chunks():
    epochs = EpochCounter()
    first = epochs.next()
    make, _ = scripted([Step("old reply part one "), Step("old reply part two", delay=0.2)])
    stream, stats = guard(make, epochs=epochs, epoch=first)
    got = []
    async for chunk in stream:
        got.append(chunk)
        epochs.next()  # a newer generation started (barge-in / new turn)
    assert got == ["old reply part one "] and stats.outcome == "stale"


async def test_cancellation_closes_the_provider_stream():
    make, state = scripted([Step("a "), Step("b", delay=5)])
    stream, stats = guard(make, stall_timeout=10)

    async def consume():
        async for _ in stream:
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert state["closed"] == 1 and stats.outcome == "cancelled"


async def test_real_session_retries_before_output_without_duplicate_text():
    fake = FakeLLM([Step(error=transient_error())], [Step("Check the "), Step("tracks.")])
    c, _, _ = controller(THINKING_CUE_ENABLED="false")
    c.greeted = True
    c.gate.on_acoustic_wake()  # active
    async with AgentSession(llm=fake, conn_options=session_conn_options(c.s)) as session:
        c.session = session
        await session.start(CatAgent(c))
        result = await session.run(user_input="what should I inspect")
        texts = [e.item.text_content for e in result.events if e.type == "message" and e.item.role == "assistant"]
        assert texts == ["Check the tracks."] and fake.calls == 2 and fake.closed_streams == 2


async def test_real_session_does_not_repeat_after_partial_failure():
    fake = FakeLLM([Step("Keep the bucket low "), Step(error=transient_error())], [Step("REPEATED")])
    c, _, _ = controller(THINKING_CUE_ENABLED="false")
    c.greeted = True
    c.gate.on_acoustic_wake()
    async with AgentSession(llm=fake, conn_options=session_conn_options(c.s)) as session:
        c.session = session
        await session.start(CatAgent(c))
        result = await session.run(user_input="how do I travel on a slope")
        texts = [e.item.text_content for e in result.events if e.type == "message" and e.item.role == "assistant"]
        assert fake.calls == 1 and [t.strip() for t in texts] == ["Keep the bucket low"] and "REPEATED" not in " ".join(texts)


# ---------------------------------------------------------------------- fixed-phrase audio cache


class FakeTTS:
    sample_rate = 24000
    num_channels = 1

    def __init__(self):
        self.calls = 0

    def synthesize(self, text):
        self.calls += 1
        from livekit import rtc

        class _Stream:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            def __aiter__(self_inner):
                async def gen():
                    class Ev:
                        frame = rtc.AudioFrame(b"\x01\x00" * 2400, 24000, 1, 2400)
                    yield Ev()
                return gen()

        return _Stream()


async def test_phrase_cache_is_keyed_persistent_and_invalidated_by_voice(tmp_path):
    clear_memory_cache()
    tts = FakeTTS()
    make_cache = lambda voice: PhraseCache(tts, cache_dir=tmp_path, provider="cartesia", model="sonic-3",
                                           voice=voice, language="en", speed=None)
    cache = make_cache("voice-a")
    audio = await cache.get("I'm listening.")
    await cache.get("I'm listening.")
    assert tts.calls == 1 and audio.duration_s == pytest.approx(0.1)
    frames = [f async for f in audio.frames()]
    assert sum(f.samples_per_channel for f in frames) == 2400
    clear_memory_cache()  # simulate a worker restart: served from disk, no new synthesis
    await make_cache("voice-a").get("I'm listening.")
    assert tts.calls == 1
    await make_cache("voice-b").get("I'm listening.")  # configuration change invalidates
    assert tts.calls == 2
    assert cache.key("I'm listening.") != make_cache("voice-b").key("I'm listening.")
