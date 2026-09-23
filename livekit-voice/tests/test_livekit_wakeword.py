"""WAKE_MODE=livekit_wakeword with a REAL acoustic model.

Uses LiveKit's Apache-2.0 example classifier `hey_livekit.onnx` (tests/fixtures/wakeword, see NOTICE.md)
and local Windows SAPI speech (scripts/make_speech_fixtures.ps1; tests skip without it). This validates
genuine keyword spotting through the worker's threaded router. It does NOT validate a "Hey Cat" model,
which has to be trained (livekit-voice/wakeword/README.md). Synthetic single-voice speech only.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import numpy as np
import pytest
from livekit import rtc

from cocoon_voice.acoustic_wake import AcousticRouter, LiveKitWakeWordEngine
from cocoon_voice.wake import WakeGate, WakeState

from .fakes import settings

FIXTURES = Path(__file__).parent / "fixtures"
MODEL = FIXTURES / "wakeword" / "hey_livekit.onnx"
THRESHOLD = 0.7


def speech(name: str) -> np.ndarray:
    path = FIXTURES / "audio" / f"sapi_{name}.wav"
    if not path.exists():
        pytest.skip("speech fixtures missing: run scripts/make_speech_fixtures.ps1 (Windows)")
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def max_score(engine: LiveKitWakeWordEngine, audio: np.ndarray) -> tuple[float, int]:
    padded = np.concatenate([np.zeros(16000, np.int16), audio, np.zeros(32000, np.int16)])
    hits = 0
    for i in range(0, len(padded) - engine.frame_length, engine.frame_length):
        hits += engine.process(padded[i:i + engine.frame_length]) >= 0
    return engine.max_score, hits


@pytest.mark.parametrize("name", ["hey_livekit_only", "hey_livekit_question"])
def test_real_model_detects_its_phrase_once(name):
    engine = LiveKitWakeWordEngine(MODEL, THRESHOLD)
    score, hits = max_score(engine, speech(name))
    assert score >= THRESHOLD and hits == 1  # window cleared after a hit: no double trigger


@pytest.mark.parametrize("name", ["hey_cat_question", "background_talk", "near_hey_liquid", "near_live_kit"])
def test_real_model_rejects_other_speech_at_threshold(name):
    engine = LiveKitWakeWordEngine(MODEL, THRESHOLD)
    score, hits = max_score(engine, speech(name))
    assert hits == 0 and score < THRESHOLD


def frames_24k(audio: np.ndarray) -> list[rtc.AudioFrame]:
    """16 kHz fixture -> 24 kHz 20 ms room frames (the worker's input rate), tagged by index."""
    resampler = rtc.AudioResampler(16000, 24000, num_channels=1)
    out: list[rtc.AudioFrame] = []
    padded = np.concatenate([np.zeros(16000, np.int16), audio, np.zeros(32000, np.int16)])
    for i in range(0, len(padded), 320):
        chunk = padded[i:i + 320]
        out += resampler.push(rtc.AudioFrame(chunk.tobytes(), 16000, 1, len(chunk)))
    out += resampler.flush()
    return out


async def run_router(audio: np.ndarray) -> tuple[AcousticRouter, WakeGate, list, int]:
    gate = WakeGate("Hey LiveKit", mode="acoustic", active_timeout_s=30)
    wakes: list[float] = []
    router = AcousticRouter(LiveKitWakeWordEngine(MODEL, THRESHOLD), gate, preroll_ms=600,
                            on_wake=lambda: wakes.append(1.0), threaded=True)
    frames = frames_24k(audio)

    async def feed():
        for f in frames:
            yield f
            await asyncio.sleep(f.duration / 2)  # 2x real time: the engine thread keeps up

    await router.run(feed())
    return router, gate, wakes, len(frames)


async def test_threaded_router_activates_once_and_forwards_the_question():
    router, gate, wakes, total = await run_router(speech("hey_livekit_question"))
    assert len(wakes) == 1 and router.detections == 1 and gate.state == WakeState.ACTIVE
    segment = await router.next_segment()
    forwarded = [f async for f in segment]
    # the question after the keyword reaches STT: forwarding continues to the end of the audio
    assert forwarded and router.frames_forwarded == len(forwarded)
    assert total - router.frames_forwarded < total * 0.7  # started around the keyword, not at the beginning
    assert sum(f.duration for f in forwarded) >= 2.0  # 600 ms pre-roll + question + trailing audio


async def test_threaded_router_ignores_other_speech():
    router, gate, wakes, _ = await run_router(speech("hey_cat_question"))
    assert wakes == [] and router.frames_forwarded == 0 and gate.state == WakeState.ARMED


def test_settings_for_livekit_wakeword(tmp_path):
    missing = settings(WAKE_MODE="livekit_wakeword").problems()
    assert any("LIVEKIT_WAKEWORD_MODEL_PATH" in p for p in missing)
    wrong = tmp_path / "hey_cat.ppn"
    wrong.write_bytes(b"x")
    assert any(".onnx" in p for p in settings(WAKE_MODE="livekit_wakeword",
                                               LIVEKIT_WAKEWORD_MODEL_PATH=str(wrong)).problems())
    ok = settings(WAKE_MODE="livekit_wakeword", LIVEKIT_WAKEWORD_MODEL_PATH=str(MODEL))
    assert ok.problems("offline") == [] and ok.acoustic_wake
    assert "does not match WAKE_PHRASE" in (ok.wake_model_mismatch() or "")
    assert settings(WAKE_MODE="livekit_wakeword", LIVEKIT_WAKEWORD_MODEL_PATH=str(MODEL),
                    WAKE_PHRASE="Hey LiveKit").wake_model_mismatch() is None
    prod = settings(WAKE_MODE="livekit_wakeword", LIVEKIT_WAKEWORD_MODEL_PATH=str(MODEL), VOICE_PROFILE="production")
    assert not any("WAKE_MODE" in p for p in prod.problems())  # an acoustic mode satisfies production


def test_engine_requires_a_real_model_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        LiveKitWakeWordEngine(tmp_path / "hey_cat.onnx", THRESHOLD)
