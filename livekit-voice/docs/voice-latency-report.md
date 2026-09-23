# Cocoon standalone voice: architecture and latency report

**Recorded:** 2026-09-23. **Machine:** native Windows 11, Python 3.11.9, livekit-agents 1.8.2. **Branch:** `voice`.
Every number below was measured; anything not measured is marked as such. Targets are engineering goals, not claims.

## Architecture (one AgentSession per dispatched room)

| Stage | Implementation | Owner module |
|---|---|---|
| Transport | LiveKit Cloud WebRTC; worker registered as `cocoon-voice` (explicit dispatch) | `agent.py` |
| Input filter | Krisp VIVA voice isolation (LiveKit Cloud auth) on the participant AudioStream | `providers.build_noise_cancellation` |
| VAD / barge-in | Silero (prewarmed), VAD interruption `min_duration` 0.4 s, false-interruption resume 1.5 s | `providers.load_vad`, `agent.build_session` |
| Wake gate | `WakeGate` ARMED/ACTIVE/CLOSED; transcript or Porcupine acoustic mode | `wake.py`, `porcupine_gate.py` |
| STT + end of turn | AssemblyAI `universal-3-5-pro` streaming; `turn_detection="stt"`, SDK `min_delay=0` | `providers.build_stt` |
| Brain | Gemini `gemini-2.5-flash` on Vertex AI (`global`, ADC, thinking off), streamed through `guarded_stream` | `providers.create_brain`, `streaming.py` |
| TTS | Cartesia `sonic-3` streaming, sentence-tokenized; fixed phrases served from an audio cache | `providers.build_tts`, `phrase_cache.py` |
| Metrics | Monotonic per-turn timeline → `metrics/session-*.jsonl`; `benchmark report` | `observability.py` |

The single replacement point for phase 2 is `providers.create_brain()`: it must return a streaming `llm.LLM`.

## Measured results

### Brain (Vertex AI, `global`, through the real LiveKit Google plugin), synthetic prompts

| Run | Sample | TTFT p50 / p95 | First speakable sentence p50 / p95 |
|---|---|---|---|
| Prewarmed (as the worker does), warm turns | n=9 | **668 / 695 ms** | **805 / 912 ms** |
| Prewarmed, first request | n=1 | 733 ms | 812 ms |
| Not prewarmed, warm turns | n=9 | 685 / 1022 ms | 845 / 1123 ms |
| Not prewarmed, first request (cold) | n=1 | 2975 ms | 3333 ms |
| Prewarm A/B, single request each | n=1+1 | no prewarm 2939 ms vs prewarm 648 ms | — |

Model selection probe, raw google-genai streaming, 5 warm runs each:

| Model | TTFT p50 | TTFT max |
|---|---|---|
| gemini-2.5-flash (budget 0) | 738 ms | 967 ms |
| gemini-2.5-flash-lite | 597 ms | 678 ms |
| gemini-3.5-flash (minimal) | 1047 ms | 11918 ms |
| gemini-3-flash-preview | 4691 ms | 19133 ms |

### VAD versus synthetic cab noise (offline, Silero with worker settings, no Krisp)

- Noise-only fixtures produced **0** speech detections. The fixtures were machinery, fan, impacts and speech-rate babble-like noise, at −30, −24, −20 and −12 dBFS, 8–20 s each.
- Windows SAPI speech (4.87 s) mixed with each noise at 10, 0 and −5 dB SNR produced **exactly 1** segment of 3.90–4.10 s in every case.
- These fixtures are synthetic. They do not represent real machines, real background talk, speaker echo, or the effect of Krisp.

### Porcupine routing (offline, stand-in engine)

- The resampler (24 kHz to 16 kHz) emits ~525-sample blocks with one push of delay. With 512-sample framing, the engine sees the keyword end about **40–60 ms** after the audio frame arrives.
- The 400 ms pre-roll covers this with margin.
- Keyword accuracy is **not measured** (no AccessKey or `.ppn`).

## Latency budget toward the targets (warm, speech end to first substantive audio)

Target: p50 ≤ 1.2 s, p95 ≤ 2.5 s.

| Stage | Budget estimate | Status |
|---|---|---|
| AssemblyAI end of turn after speech ends | `min_turn_silence` 160 ms plus model/network time | **Unmeasured** (no key) |
| Vertex first speakable sentence | p50 805 ms / p95 912 ms | Measured (n=9) |
| Cartesia first audio after the first sentence | typically low hundreds of ms | **Unmeasured** (no key) |
| Room publication / client playback | worker proxy only | **Unmeasured** |

**Assessment:**
- The brain alone uses about two thirds of the p50 target, so it is the slowest measured stage.
- Hitting p50 ≤ 1.2 s needs AssemblyAI finalisation plus Cartesia first audio together under about 400 ms. That is plausible but unproven.
- If it misses, the first levers are `VERTEX_MODEL=gemini-2.5-flash-lite` (p50 597 ms, not yet quality-tested for this persona) and a lower `ASSEMBLYAI_MIN_TURN_SILENCE_MS`, which risks cutting off hesitant speakers.
- The cold first turn is protected by the LLM prewarm at session construction and by the cached greeting.

**Interruption** (target p50 ≤ 250 ms / p95 ≤ 500 ms from detection to output stop): the metrics are implemented (`interruption_to_stop_ms`) but **unmeasured**, because they need live audio output.

## How to reproduce

```powershell
python -m cocoon_voice.doctor
python -m cocoon_voice.benchmark run --turns 10              # add --stt once both keys exist
python -m cocoon_voice.benchmark run --turns 10 --no-prewarm # cold behaviour
python -m cocoon_voice.benchmark report                      # after live Playground sessions
```

## Remaining measurements (require keys or assets)

1. `benchmark run --stt`, then with `--noise machinery --snr 10` and `--noise impacts --snr 0`. This gives AssemblyAI finalisation, WER and Cartesia first audio, on synthetic speech.
2. Live Playground sessions of 10–30 turns each: warm/cold worker-side timelines, interruption-to-stop, and false interruptions. Use headphones, then speakers.
3. Human listening: voice warmth, pronunciation of CAT / IDs / numbers, and whether the cue or greeting is annoying.
4. Porcupine: false accepts per hour of room noise and background talk, misses across speakers and accents, and truncation of "Hey Cat, <question>".
