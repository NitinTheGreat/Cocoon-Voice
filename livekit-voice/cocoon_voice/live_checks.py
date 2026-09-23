"""Credentialed provider stages shared by `smoke` and `benchmark` (opt-in, bounded, paid calls).

Every stage uses the same factories as the worker (providers.py), so a pass here exercises the
exact plugin configuration. Timings use time.perf_counter() in this process only.
"""

from __future__ import annotations

import asyncio
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from livekit import rtc
from livekit.agents import llm, stt, tokenize

from . import speech_policy as sp
from .config import VoiceSettings
from .providers import build_stt, build_tts, create_brain

OPERATOR_PROMPTS = [
    "What should I check before starting the excavator?",
    "How tight should the track tension be?",
    "Can you hear me okay?",
    "What does it mean if the hydraulic oil looks milky?",
    "Is it safe to travel across a slope with the bucket raised?",
    "Give me one tip for saving fuel while idling.",
    "What can you do in this trial?",
    "How often should I grease the boom pins?",
    "yes",
    "What's the difference between a dozer and a wheel loader?",
]

PRONUNCIATION_TEXT = ("CAT three-twenty excavator, unit E seven four two. Check the hydraulic hose at "
                      "three thousand five hundred hours, then walk around the machine.")


@dataclass
class BrainTiming:
    ttft_ms: float | None
    first_sentence_ms: float | None
    total_ms: float
    chars: int
    text: str = field(repr=False, default="")


async def brain_turn(brain: llm.LLM, prompt: str) -> BrainTiming:
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="system", content=sp.INSTRUCTIONS)
    ctx.add_message(role="user", content=prompt)
    tokenizer = tokenize.blingfire.SentenceTokenizer()
    t0 = time.perf_counter()
    first = first_sentence = None
    text = ""
    async with brain.chat(chat_ctx=ctx) as stream:
        async for chunk in stream:
            delta = chunk.delta.content if chunk.delta else None
            if not delta:
                continue
            if first is None:
                first = time.perf_counter()
            text += delta
            if first_sentence is None and len(tokenizer.tokenize(text)) > 1:
                first_sentence = time.perf_counter()  # first complete speakable sentence available to TTS
    end = time.perf_counter()
    if first_sentence is None and text:
        first_sentence = end
    ms = lambda t: round((t - t0) * 1000, 1) if t else None  # noqa: E731
    return BrainTiming(ms(first), ms(first_sentence), round((end - t0) * 1000, 1), len(text), text)


@dataclass
class TtsTiming:
    first_audio_ms: float | None
    total_ms: float
    audio_s: float
    pcm: bytes = field(repr=False, default=b"")
    sample_rate: int = 24000


async def tts_stream_turn(tts, text_chunks: list[str]) -> TtsTiming:
    """Streaming synthesis the way the worker uses it: text pushed incrementally, audio pulled."""
    t0 = time.perf_counter()
    first = None
    pcm = bytearray()
    rate = tts.sample_rate
    stream = tts.stream()

    async def push():
        for chunk in text_chunks:
            stream.push_text(chunk)
        stream.end_input()

    pusher = asyncio.create_task(push())
    try:
        async for ev in stream:
            if first is None:
                first = time.perf_counter()
            rate = ev.frame.sample_rate
            pcm += bytes(ev.frame.data.cast("B"))
    finally:
        await pusher
        await stream.aclose()
    end = time.perf_counter()
    return TtsTiming(round((first - t0) * 1000, 1) if first else None, round((end - t0) * 1000, 1),
                     len(pcm) / 2 / rate, bytes(pcm), rate)


@dataclass
class SttTiming:
    transcript: str
    final_after_audio_end_ms: float | None
    audio_s: float


async def stt_realtime(stt_impl, pcm: bytes, sample_rate: int, *, noise: bytes | None = None) -> SttTiming:
    """Stream PCM16 mono to STT at real-time pace (optionally mixed with noise); time the final transcript."""
    samples_per_frame = sample_rate // 50
    step = samples_per_frame * 2
    if noise:
        pcm = mix(pcm, noise)
    tail_silence = b"\x00\x00" * sample_rate * 2  # 2 s so endpointing can finalize
    audio = pcm + tail_silence
    audio_end: float | None = None
    finals: list[str] = []
    final_at: float | None = None
    stream = stt_impl.stream()

    async def feed():
        nonlocal audio_end
        start = time.perf_counter()
        for i in range(0, len(audio), step):
            chunk = audio[i:i + step]
            stream.push_frame(rtc.AudioFrame(chunk, sample_rate, 1, len(chunk) // 2))
            if i + step >= len(pcm) and audio_end is None:
                audio_end = time.perf_counter()
            await asyncio.sleep(max(0.0, start + (i + step) / 2 / sample_rate - time.perf_counter()))
        stream.end_input()

    feeder = asyncio.create_task(feed())
    try:
        async for ev in stream:
            if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives:
                finals.append(ev.alternatives[0].text)
                final_at = time.perf_counter()
    finally:
        feeder.cancel()
        await stream.aclose()
    latency = round((final_at - audio_end) * 1000, 1) if (final_at and audio_end) else None
    return SttTiming(" ".join(finals).strip(), latency, len(pcm) / 2 / sample_rate)


def mix(speech: bytes, noise: bytes) -> bytes:
    import numpy as np

    s = np.frombuffer(speech, dtype=np.int16).astype(np.float32)
    n = np.frombuffer(noise, dtype=np.int16).astype(np.float32)
    n = np.resize(n, s.shape)
    return np.clip(s + n, -32768, 32767).astype(np.int16).tobytes()


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return path


def word_error_rate(reference: str, hypothesis: str) -> float:
    from .wake import normalize

    r, h = normalize(reference).split(), normalize(hypothesis).split()
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return round(d[len(h)] / max(1, len(r)), 3)


def make_brain(settings: VoiceSettings) -> llm.LLM:
    return create_brain(settings)


def make_tts(settings: VoiceSettings):
    return build_tts(settings)


def make_stt(settings: VoiceSettings):
    return build_stt(settings)
