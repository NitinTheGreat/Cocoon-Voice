"""Acoustic wake routing: sample-rate conversion, engine framing, pre-roll, single forwarding, bounds.

A fake keyword engine stands in for Porcupine and "detects" a marker amplitude. This validates the
audio plumbing only; it does NOT validate acoustic keyword spotting (needs a real key + .ppn).
"""

from __future__ import annotations

import array

import pytest
from livekit import rtc

from cocoon_voice.porcupine_gate import AcousticRouter, PorcupineEngine
from cocoon_voice.wake import WakeGate, WakeState

from .conftest import FakeClock

MARK = 12345  # sample value that makes the fake engine fire


class FakeEngine:
    sample_rate = 16000
    frame_length = 512

    def __init__(self):
        self.frames: list[int] = []
        self.deleted = False

    def process(self, pcm) -> int:
        assert len(pcm) == self.frame_length
        self.frames.append(len(pcm))
        return 0 if max(pcm) >= MARK - 500 else -1

    def delete(self):
        self.deleted = True


def frame(seq: int, rate: int = 24000, ms: int = 20, value: int = 100) -> rtc.AudioFrame:
    n = rate * ms // 1000
    samples = array.array("h", [value] * n)
    samples[0] = seq  # tag the frame so forwarding order/duplicates can be checked
    return rtc.AudioFrame(samples.tobytes(), rate, 1, n)


def tag(f: rtc.AudioFrame) -> int:
    return array.array("h", bytes(f.data.cast("B")))[0]


def router(preroll_ms: int = 400):
    clock = FakeClock()
    gate = WakeGate("Hey Cat", mode="porcupine", clock=clock, active_timeout_s=10, debounce_s=2)
    wakes = []
    r = AcousticRouter(FakeEngine(), gate, preroll_ms=preroll_ms, on_wake=lambda: wakes.append(clock.now))
    return r, gate, clock, wakes


def drain(segment_iter_queue) -> list[rtc.AudioFrame]:
    seg = segment_iter_queue
    out = []
    while not seg.queue.empty():
        f = seg.queue.get_nowait()
        if f is None:
            break
        out.append(f)
    return out


@pytest.mark.parametrize("rate", [24000, 48000, 16000])
def test_engine_gets_exact_frames_at_its_own_rate(rate):
    r, _, _, _ = router()
    for i in range(100):  # 2 s of audio
        r.handle_frame(frame(i + 1, rate=rate))
    assert set(r.engine.frames) == {512}
    produced = r.engine_frames * 512 + len(r._pcm)
    assert produced == pytest.approx(2 * 16000, rel=0.02)  # resampled to 16 kHz, nothing lost


def test_armed_audio_never_reaches_stt_and_preroll_is_bounded():
    r, gate, _, _ = router(preroll_ms=400)
    for i in range(200):
        r.handle_frame(frame(i + 1))
    assert r.frames_forwarded == 0 and r._segment is None and gate.state == WakeState.ARMED
    assert r._preroll_s <= 0.4 + 0.02 and len(r._preroll) <= 21


def test_detection_opens_one_segment_with_preroll_and_no_duplicate_frames():
    r, gate, _, wakes = router(preroll_ms=400)
    for i in range(1, 51):
        r.handle_frame(frame(i))
    r.handle_frame(frame(51, value=MARK))  # the keyword ends in this frame
    for i in range(52, 62):  # "... what's the track tension?"
        r.handle_frame(frame(i))
        if i == 54:  # resampler block + 512-sample engine framing: detection within ~3 frames (60 ms)
            assert gate.state == WakeState.ACTIVE and len(wakes) == 1
    segment = r._segment
    tags = [tag(f) for f in drain(segment)]
    assert tags == sorted(set(tags)) and tags == list(range(tags[0], 62)), "each frame once, in order, no gaps"
    assert 51 in tags and tags[-1] == 61
    assert 51 - 20 <= tags[0] <= 51 - 16  # ~400 ms of pre-roll reaches back before the keyword end
    assert r.frames_forwarded == len(tags)


def test_segment_closes_when_gate_rearms_and_reopens_on_next_detection():
    r, gate, clock, wakes = router()
    for i in range(1, 4):
        r.handle_frame(frame(i, value=MARK))
    for i in range(4, 10):  # trailing speech flushes the keyword audio through resampler + framing
        r.handle_frame(frame(i))
    first = r._segment
    assert first is not None and gate.state == WakeState.ACTIVE and len(wakes) == 1
    forwarded = r.frames_forwarded
    clock.now += 11
    assert gate.check_timeout(busy=False)  # inactivity
    r.handle_frame(frame(10))
    assert first.closed and r._segment is None and r.frames_forwarded == forwarded
    frames = drain(first)
    assert len(frames) == forwarded  # queued frames followed by the end marker
    clock.now += 3
    for i in range(11, 16):
        r.handle_frame(frame(i, value=MARK))
    assert r._segment is not None and r._segment is not first and len(wakes) == 2


def test_segment_buffer_is_bounded_under_backpressure():
    r, _, _, _ = router()
    r._max_segment_frames = 10
    for i in range(1, 4):
        r.handle_frame(frame(i, value=MARK))
    for i in range(4, 60):
        r.handle_frame(frame(i))
    seg = r._segment
    assert seg.queue.qsize() <= 10 and seg.dropped > 0


async def test_controller_feeds_stt_once_per_activation(monkeypatch):
    """transcribe(): single consumer of the audio iterator; STT sees only ACTIVE segments."""
    from livekit.agents import Agent

    from cocoon_voice.agent import VoiceController

    from .fakes import metrics, settings

    seen_segments: list[list[int]] = []

    async def fake_default_stt(agent, audio, model_settings):
        frames = [tag(f) async for f in audio]
        seen_segments.append(frames)
        if False:
            yield None

    monkeypatch.setattr(Agent.default, "stt_node", staticmethod(fake_default_stt))
    c = VoiceController(settings(WAKE_MODE="porcupine"), phrases=None, metrics=metrics(), keyword_engine=FakeEngine())

    async def audio():
        for i in range(1, 31):
            yield frame(i)
        for i in range(31, 34):
            yield frame(i, value=MARK)
        for i in range(34, 40):
            yield frame(i)
        c.gate.close()  # session ends
        yield frame(40)

    async for _ in c.transcribe(object(), audio(), None):  # type: ignore[arg-type]
        pass
    assert len(seen_segments) == 1
    seg = seen_segments[0]
    assert seg == sorted(set(seg)) and 31 in seg and 39 in seg and 40 not in seg and seg[0] > 1
    await c.aclose()


def test_real_porcupine_engine_requires_a_real_keyword_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        PorcupineEngine("not-a-real-key", tmp_path / "hey_cat.ppn", 0.5)
