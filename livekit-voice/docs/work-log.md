# livekit-voice work log

Newest first. Evidence only: every result below was observed on the recorded machine/commit.

## 2026-09-23 20:30 UTC — M7: first live provider checks with real keys (branch `voice`)

- **Doctor:** settings, VAD, noise (constructs), Vertex (ADC), AssemblyAI, LiveKit PASS.
  **Cartesia FAIL: HTTP 401 "Invalid API key"** on `/tts/bytes` and on the TTS websocket. The previous doctor check
  (GET `/voices/{id}`) returned 200 for this key, so it did not prove the key works; the check now performs a one-word
  synthesis. Voice `f786b574-…` is "Katie - Friendly Fixer" (en).
- **Smoke/benchmark fix:** outside a LiveKit job the plugins need an explicit aiohttp session
  ("Attempted to use an http session outside of a job context"); `live_checks` now passes one. The worker is unaffected.
- **AssemblyAI live (universal-3-5-pro, local SAPI speech, synthetic):** "Hey Cat, what should I check before starting
  the excavator?" transcribed exactly (WER 0) → wake decision `respond` with the question; "Hey Cat." → "Hey Cat!" →
  `ack`. Finalisation latency from this run is NOT valid (fixture WAVs contain trailing silence, so the speech-end
  marker was late).
- **Porcupine:** the user cannot obtain a Picovoice AccessKey (needs a company email). Alternatives evaluated:
  `livekit-wakeword` 0.2.1 (Apache-2.0, ONNX, custom training pipeline) and `openwakeword` 0.6.0 (code Apache-2.0,
  pretrained models CC BY-NC-SA 4.0, ONNX on Windows, custom training notebook). Not implemented yet.
- **Checks run:** full `pytest` → 127 passed, 1 skipped.
- **Blocked:** Playground speech until `CARTESIA_API_KEY` is replaced with a valid key.

## 2026-09-23 19:45 UTC — M6: documentation, report, dispatch helper (branch `voice`)

- **Added/updated:** phase-1 README (install, keys, commands, Playground walkthrough + manual checklist, wake modes,
  Porcupine model creation, noise, streaming/recovery, privacy, phase-2 replacement point, gaps);
  `docs/voice-latency-report.md`; root README phase note; this folder's CLAUDE.md snapshot.
- **Worker:** logs an explicit cost warning in `WAKE_MODE=transcript` and a loud `AUDIO DEGRADED` line when
  degraded audio is allowed. `scripts/dispatch.py list` handles a missing room.
- **Checks run:** `python -m livekit.agents download-files` → finished for google, krisp and silero plugins;
  `python scripts/dispatch.py list --room cocoon-smoke-check` → authenticated, "room does not exist";
  full `pytest` → 127 passed, 1 skipped.
- **Still blocked:** Playground speech test (ASSEMBLYAI_API_KEY, CARTESIA_API_KEY), acoustic wake
  (PICOVOICE_ACCESS_KEY + `Hey Cat` .ppn). The Agent Console's way of targeting an explicitly named local agent is
  not confirmed by the docs; README gives the deterministic token/dispatch path.
- **Next step for the user:** add the two provider keys, run `python -m cocoon_voice.doctor`, `python -m
  cocoon_voice.smoke`, `python -m cocoon_voice.agent dev`, then the README checklist. Phase 2 stays pending.

## 2026-09-23 19:20 UTC — M5: smoke, benchmark, noise fixtures, metrics (branch `voice`)

- **Added:** `python -m cocoon_voice.smoke` (bounded live checks through the worker factories),
  `python -m cocoon_voice.benchmark run|report` (≤30 turns / 10 min, p50/p95 cold vs warm, usage counts, JSON written
  to `metrics/`), deterministic SYNTHETIC noise fixtures (machinery, fan, impacts, echo_babble), local Windows SAPI
  speech fixtures via `scripts/make_speech_fixtures.ps1` (git-ignored), per-turn metrics JSONL without transcripts.
- **Live results (Vertex only; Cartesia/AssemblyAI skipped — keys missing):**
  - `smoke`: vertex PASS through the LiveKit Google plugin (cold, no prewarm: TTFT 3164 ms).
  - Prewarm effect (1 request each): no prewarm TTFT 2939 ms; `llm.prewarm()` + 3 s → 648 ms. AgentSession prewarms
    automatically at construction, before the greeting.
  - `benchmark run --turns 10` without prewarm (metrics/benchmark-20260923T231059.json): warm (n=9) TTFT p50 685 /
    p95 1022 ms; first speakable sentence p50 845 / p95 1123 ms; cold (n=1) TTFT 2975 ms.
  - `benchmark run --turns 10` with prewarm (metrics/benchmark-20260923T231241.json): first request TTFT 733 ms,
    first sentence 812 ms; warm (n=9) TTFT p50 668 / p95 695 ms; first sentence p50 805 / p95 912 ms.
  - Usage per 10-turn run: 10 Vertex requests, ~1.2–1.3k output characters.
- **Offline noise results (Silero VAD, worker settings, no Krisp):** noise-only fixtures (8–20 s, −30/−24/−20/−12 dBFS)
  produced 0 speech detections; SAPI speech mixed with each noise at 10, 0 and −5 dB SNR was detected as exactly one
  segment (3.90–4.10 s of a 4.87 s clip). `pytest tests/test_noise_vad.py` → 16 passed.
- **Checks run:** full suite 127 passed, 1 skipped.
- **Not verified:** Cartesia first audio, AssemblyAI finalisation latency, Krisp effect, real background talk, speaker
  echo, human voices/accents. Synthetic noise is not real machinery audio.

## 2026-09-23 18:45 UTC — M4: Porcupine acoustic routing (branch `voice`)

- **Scope:** `AcousticRouter` is the single consumer of the agent audio input in `WAKE_MODE=porcupine`: frames
  (already filtered by the configured Krisp processor — RoomIO applies it on the participant `AudioStream`
  before frames reach the agent input, VAD and `stt_node`) are resampled to the engine rate, cut into the engine's
  frame length, and forwarded to STT only inside ACTIVE segments that start with a bounded pre-roll.
- **Measured (offline):** `rtc.AudioResampler` 24 kHz→16 kHz emits ~525-sample blocks with one push of delay;
  with 512-sample engine framing, routing adds ~40–60 ms before the engine sees the keyword end. Default
  `PORCUPINE_PREROLL_MS=400` covers it with margin.
- **Checks run:** `pytest tests/test_porcupine_router.py` → 9 passed (exact 512-sample engine frames at 16/24/48 kHz
  input, nothing lost; ARMED audio never reaches STT; one segment per activation with ~400 ms pre-roll, every frame
  forwarded once in order; segment closes on re-arm and reopens on next detection; bounded drop-oldest buffer;
  controller feeds STT once per activation). Full suite 108 passed, 1 skipped.
- **Not validated:** acoustic keyword spotting itself (false accepts/misses, truncation). A fake engine was used.
  BLOCKED on `PICOVOICE_ACCESS_KEY` and a real custom `Hey Cat` .ppn for Windows (x86_64); none is faked.

## 2026-09-23 18:25 UTC — M3: streaming, interruption and recovery (branch `voice`)

- **Finding:** a real `AgentSession` with default connection options retried a failed LLM stream three times
  inside the SDK (`llm.py` "retrying in 0.1s/2.0s"), including after chunks were produced. Production options are now
  a shared `session_conn_options()` (LLM `max_retry=0`) and tests use the same function.
- **Behaviour now covered:** text forwarded chunk-by-chunk before the stream ends; retries (bounded, jittered) only
  before the first chunk; no replay after partial output (real session: 1 call, partial answer kept, retry text
  never spoken); first-chunk and mid-stream stall timeouts; empty output fallback; one-off thinking cue only when the
  first text is later than `THINKING_CUE_DELAY_MS`; stale-epoch suppression; provider stream closed on
  cancellation; fixed-phrase audio cache keyed by provider/model/voice/language/speed and persisted on disk.
- **Checks run:** `pytest tests/test_streaming.py` → 12 passed; full suite 99 passed, 1 skipped.
  *(Corrected in M4: this entry originally said 13/100, which did not match the observed run.)*
- **Limits:** barge-in stop latency and played-text truncation depend on live audio output (SDK
  `use_tts_aligned_transcript` with Cartesia word timestamps); not measurable until Cartesia/AssemblyAI keys exist.

## 2026-09-23 18:05 UTC — M2: wake gate and turn policy tests (branch `voice`)

- **Scope:** exact leading "Hey Cat" matcher (case/punctuation normalisation only, no fuzzy matching), ARMED/ACTIVE/
  CLOSED gate, stop/sleep commands (standalone utterances only), debounce, duplicate-final suppression, echo guard,
  backchannel handling, idle timeout paused while busy; controller hook behaviour.
- **Checks run:** `pytest tests/test_wake.py` → 30 passed; `pytest tests/test_controller.py` → 9 passed, including a
  real `AgentSession` (text mode) with a scripted streaming LLM: 0 brain calls while ARMED, exactly 1 after
  "Hey Cat …". Full suite: 87 passed, 1 skipped.
- **Limits:** the SDK's text-mode `run()` bypasses `on_user_turn_completed`, so hook gating is tested at controller
  level; STT-originated turns in a live room are still unverified (no AssemblyAI key). Nearby-voice rejection relies
  on Krisp VIVA + wake debounce only; activation is not speaker authentication.
- **Next:** streaming/interruption tests, Porcupine router tests.

## 2026-09-23 17:30 UTC — M1: standalone foundation (branch `voice`, base `521d318`)

- **Scope:** phase-1 standalone voice. Typed settings (`cocoon_voice/config.py`), provider factories with the
  single brain replacement point (`providers.create_brain`), doctor, pinned plugins, worker rewritten for
  AssemblyAI → Gemini/Vertex → Cartesia with Krisp input filtering. Phase-0 bridge moved to `remote_bridge.py`
  (unwired, tests kept).
- **SDK findings (livekit-agents 1.8.2, inspected source):** plugins must be imported on the main thread
  (lazy imports inside jobs crashed with "Plugins must be registered on the main thread" — fixed);
  `LLMStream` retries by default even after chunks were sent (`_retry_on_chunk_sent=True`) → session LLM
  `max_retry=0`, retries only before first chunk in `streaming.guarded_stream`; STT-mode end-of-turn still
  sleeps `endpointing.min_delay` (default 0.3 s) → set to 0 so AssemblyAI endpointing is the single authority;
  `StopResponse` in `on_user_turn_completed` returns before the user message enters history.
- **Model selection (measured, Vertex `global`, ADC, 5 warm runs each, streaming TTFT):**
  gemini-2.5-flash (thinking_budget=0) p50 738 ms / max 967 ms; gemini-2.5-flash-lite p50 597 / max 678;
  gemini-3.5-flash (thinking_level=minimal) p50 1047 / max 11918; gemini-3-flash-preview p50 4691 / max 19133.
  Default `VERTEX_MODEL=gemini-2.5-flash`.
- **Checks run:** `pytest` (livekit-voice) → 48 passed, 1 skipped (opt-in backend integration);
  `python -m cocoon_voice.doctor` → settings PASS, silero_vad PASS, noise PASS (Krisp VIVA constructs on
  native Windows; filtering only inside a LiveKit Cloud session — not validated), vertex PASS (ADC),
  livekit PASS, assemblyai FAIL / cartesia FAIL (keys not set);
  `python -m cocoon_voice.agent start` (dummy provider keys, no dispatch) → "registered worker"
  agent_name=cocoon-voice, region India South; `GET 127.0.0.1:8081/worker` returned the JSON.
- **Not verified:** dispatch, STT, TTS, noise filtering, acoustic wake, any speech. **Blocked on:**
  ASSEMBLYAI_API_KEY, CARTESIA_API_KEY (and PICOVOICE_ACCESS_KEY + custom `Hey Cat` .ppn for porcupine mode).
- **Next:** focused tests for the wake gate, streaming guard and Porcupine router; benchmark/smoke harness.
