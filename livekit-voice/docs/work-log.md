# livekit-voice work log

Newest first. Evidence only: every result below was observed on the recorded machine/commit.

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
