"""Deterministic SYNTHETIC noise fixtures (seeded numpy), PCM16 mono.

These approximate cab conditions for repeatable tests; they are not recordings of real machines
and do not replace microphone tests on site or with speakerphone echo.

- machinery: diesel-like firing hum (~30 Hz fundamental + harmonics), slow load modulation, rumble
- fan: broadband low-passed noise with a blade-pass tone
- impacts: low background with random decaying metallic transients (bucket/track knocks)
- echo_babble: speech-rate (~4 Hz) amplitude-modulated band-limited noise, a stand-in for distant talk
"""

from __future__ import annotations

import numpy as np

KINDS = ("machinery", "fan", "impacts", "echo_babble")


def _lowpass(x: np.ndarray, sample_rate: int, cutoff: float) -> np.ndarray:
    alpha = np.exp(-2 * np.pi * cutoff / sample_rate)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc = alpha * acc + (1 - alpha) * v
        y[i] = acc
    return y


def generate(kind: str, *, seconds: float = 5.0, sample_rate: int = 16000, level_dbfs: float = -24.0,
             seed: int = 7) -> bytes:
    rng = np.random.default_rng(seed)
    n = int(seconds * sample_rate)
    t = np.arange(n) / sample_rate
    if kind == "machinery":
        load = 1 + 0.25 * np.sin(2 * np.pi * 0.3 * t)
        hum = sum((1 / k) * np.sin(2 * np.pi * 30 * k * t + rng.uniform(0, 6.28)) for k in range(1, 12))
        rumble = _lowpass(rng.normal(0, 1, n), sample_rate, 250)
        x = load * hum + 3 * rumble
    elif kind == "fan":
        x = _lowpass(rng.normal(0, 1, n), sample_rate, 1200) + 0.3 * np.sin(2 * np.pi * 120 * t)
    elif kind == "impacts":
        x = 0.05 * rng.normal(0, 1, n)
        pos = 0
        while pos < n:
            pos += int(rng.uniform(0.5, 1.8) * sample_rate)
            if pos >= n:
                break
            length = min(n - pos, int(0.25 * sample_rate))
            tt = np.arange(length) / sample_rate
            burst = np.exp(-tt * 30) * (np.sin(2 * np.pi * rng.uniform(400, 1500) * tt) + rng.normal(0, 0.5, length))
            x[pos:pos + length] += 8 * burst
    elif kind == "echo_babble":
        carrier = _lowpass(rng.normal(0, 1, n), sample_rate, 3000) - _lowpass(rng.normal(0, 1, n), sample_rate, 300)
        syllables = np.clip(np.sin(2 * np.pi * 4.2 * t + 2 * np.sin(2 * np.pi * 0.7 * t)), 0, None)
        x = carrier * syllables
    else:
        raise ValueError(f"unknown noise kind {kind!r}; choose from {KINDS}")
    x = x / (np.sqrt(np.mean(x ** 2)) + 1e-9)
    x *= 32768 * 10 ** (level_dbfs / 20)
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def rms(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0


def scale_to_snr(speech: bytes, noise: bytes, snr_db: float) -> bytes:
    """Scale noise so that rms(speech)/rms(noise) equals snr_db."""
    n = np.frombuffer(noise, dtype=np.int16).astype(np.float64)
    target = rms(speech) / (10 ** (snr_db / 20))
    n *= target / (rms(noise) + 1e-9)
    return np.clip(n, -32768, 32767).astype(np.int16).tobytes()
