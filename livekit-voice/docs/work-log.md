# livekit-voice work log

Newest first. Evidence only: every result below was observed on the recorded machine/commit.

## 2026-09-24 — M12: remote LangGraph brain wired (branch `voice`)

- **Revisions:** the `voice` branch already contains the backend (`6ecc131` merges `origin/backend` `c779e26`), so
  both services run from one checkout. No worktree was needed.
- **Worker:** `VOICE_BRAIN=remote_langgraph` now drives `llm_node` through `TurnBridge` (commit `94fb4b0`). Details
  are in the README "Remote LangGraph brain" section.
- **Environment:**
  - Port 8000 is held by an unrelated app (`uvicorn server.api:app`), so the backend runs on `COCOON_PORT=8010`
    and the worker uses `COCOON_BACKEND_URL=http://127.0.0.1:8010`.
  - The backend `.env` had the example service token. A new random token was generated and set in both `.env`
    files; it was never printed or committed.
  - `VERTEX_MODEL=gemini-3.8-flash` comes from the handoff. It is not verified in mock mode.
  - The ADC path was checked for existence only.
- **Readiness (observed):** backend `GET /readyz` → `status ready`, `llm_mode mock`, `catalog true`, 5 machines and
  15 operators. The worker registered `cocoon-voice` with brain `remote_langgraph`, wake `off` and noise `none`.
- **Checks run:** configuration validation and a syntax compile only. No tests, smoke or simulator, by request.
- **Pending (manual, user):**
  - Playground: "What's my next task?"
  - Incident creation.
  - Simulator warning and "why?".
  - Stopping the backend mid-conversation.
  - The live-mode pass: ADC, model availability and quota are unverified.

## 2026-09-24 — M11: wake gating, Krisp isolation and STT diagnosis (branch `voice`)

**User's run:** `wake decision=ignore reason=armed: no wake phrase state=ARMED chars=29`; `outcome=gated:ignore`
with STT timings; AssemblyAI "no messages received for 15s / 30s"; event-loop blocking inside
`livekit.plugins.krisp.viva_filter` → `krisp_internal._ffi`; loop lag up to 315 ms.

**Code-level findings:**
- **Wake gate.** In `WAKE_MODE=transcript` a final turn that does not start with the wake phrase is dropped while
  ARMED, by design (`WakeGate.on_utterance`). The 29-character turn was dropped this way. The log had no text, so
  it is unknown whether "Hey Cat" was said or recognised; no misrecognition is claimed.
- **Audio path.** STT receives room audio continuously in transcript mode, including while ARMED. Nothing in the
  worker resets the gate apart from the inactivity timer (busy states exempt) and the sleep command.
- **Krisp.** One VIVA filter is built per session and passed to the SDK room input. It runs through
  `rtc.AudioStream.from_track` per frame on the event loop, with no second worker filter and no per-frame
  construction. Startup validation built an extra, unused native filter in the main process. The M10 executor
  offload of the credential update was unverified cross-thread use of the native object.
- **AssemblyAI.** The plugin (1.8.2) warns every 15 s without provider messages and does not reconnect. It warns
  separately ("no audio frames sent") only when audio stops reaching it.
- **Logging.** `main()` added a plain-text root handler, and LiveKit's CLI adds its own (JSON for `start`), so
  every record printed twice.

**Changes:** `WAKE_MODE=off`; normalised whole-word phrase matching; routing line per turn; "not answered" for
gated turns; playback lines; `stt input:` diagnosis; startup lines for wake mode, enhancement and transcript
logging; Krisp offload removed; startup check builds no filter; one log handler. Versions inspected:
livekit-agents 1.8.2, livekit 1.1.18, livekit-plugins-krisp 0.4.2, krisp-internal 0.2.0,
livekit-plugins-assemblyai 1.8.2.

**Checks run:** none by request (syntax compile only). The removed Krisp-offload test covered deleted code.
**Next:** the user's Playground run in continuous-listening mode (README), then re-enable Krisp separately.

## 2026-09-23 19:40–20:10 UTC — M10: interruptions, recovery and conversational delivery (branch `voice`)

Commits: `6c872b2` (input, interruption ownership, recovery, diagnostics), `d7fb547` (spoken style, cue), and this
documentation commit. Base `a1b0e10`.

**Proven causes (before):**
- *Background sounds interrupted Cat.* `INTERRUPTION_MODE` forced `vad`. `AgentActivity._resolve_interruption_detection()`
  then returns no detector, although AssemblyAI reports `aligned_transcript=word`. Every VAD onset of 0.4 s paused the reply.
- *Silence after an interruption.* The SDK interrupts the paused reply permanently on any final transcript
  (`on_final_transcript` → `_cancel_speech_pause(interrupt=True)`). It also interrupts before `on_user_turn_completed`
  runs. The wake gate then ignored backchannels ("yeah", "okay") and duplicate finals, so the operator got silence.
- *Choppy audio (part).* The native Krisp credential update ran on the event loop at room-token refresh and blocked it
  for 105–339 ms mid-session. TTS pacing showed underruns of −404 and −685 ms in those sessions.
- *Other observations.* An earlier Playground session had 7 first-chunk timeouts (> 6 s). The root cause of those
  Vertex stalls is not proven.

**Changes:**
- `INTERRUPTION_MODE=adaptive` (min 0.5 s, `min_words` 0, resume after 2.0 s). The effective mode is logged at start
  and sampled per committed turn, and a mid-session downgrade is logged.
- Adaptive verdicts are logged and counted.
- Committed turns while ACTIVE are always answered. Backchannel handling is left to the SDK; there is no word blacklist.
- "wait", "hold on" and "hang on" are stop commands.
- One cached clarification on `user_transcription_timeout` (speech ≥ 0.8 s, no turn, nobody speaking).
- The wake window never expires during a pending interruption.
- Krisp credential updates run in an executor. Metrics JSONL is written by a background thread.
- Loop-lag monitor and TTS pacing. An underrun is now a lag of more than one 20 ms frame.
- Interruption latency samples count only when the SDK stores the reply as interrupted.
- An account-level 401/402/403/429 is logged as a plain error.
- Spoken-style prompt. The delay cue rotates between three phrases, fires at most once per turn and is never sent
  after answer text.
- LLM first-chunk timeout 3.5 s, 3 attempts.

**Checks run:**
- `pytest`: 154 passed, 1 skipped.
- Live probe (`scripts/live_probe.py`): a synthetic operator in fresh LiveKit Cloud rooms, against an isolated worker
  (`LIVEKIT_AGENT_NAME=cocoon-voice-probe`, `COCOON_HEALTH_PORT=8082`, `LOG_TRANSCRIPTS=true`). It used 5 sessions with
  SAPI clips and natural clips (second Cartesia voice "Daniel", `scripts/make_natural_fixtures.py`).
  Results, all synthetic:
  - **Adaptive:** `adaptive(active)` with `aligned_transcript=word` in every session.
  - **Noise:** impacts −18 dBFS for 1.2 s, fan −26 dBFS for 2 s and machinery −22 dBFS for 2 s, played during replies.
    They produced 0 turns, 0 adaptive verdicts and 0 SDK pauses in 3/3 runs (1 of the 9 bursts fell between
    replies). False interruptions stayed 0, so the SDK
    resume path was **not exercised live**.
  - **Backchannels (natural voice, 0.65–0.88 s of speech):**
    - 8 of 8 verdicts were `backchannel` (p = 0.17–0.35), and Cat kept talking.
    - 2 more fell in the last half-second of a reply that ended naturally (`agent_ended`); they were answered as normal
      turns.
    - 1 "Right." paused Cat with no verdict and became a turn. The cause was not determined.
    - 1 was lost to an SDK fallback (below).
  - **Backchannels (SAPI):** "Mm-hmm" (transcribed "Millimeter home.") and "Okay." got `interruption` verdicts in 4 of 4
    cases (p = 0.83–0.92). They were answered, not ignored.
  - **Adaptive fallback:** once, LiveKit Adaptive Interruption returned error 2005 "quota exceeded". The SDK disabled
    adaptive for the rest of that session (3 turns adaptive, 7 turns VAD). The next session had adaptive again.
  - **Stop:** "Stop." silenced output before the clip ended and produced no reply in 2/2 adaptive runs. In the
    VAD-fallback run, output stopped 0.67 s after the clip ended, also with no reply.
  - **Correction:** "Okay, but I meant the other machine, the wheel loader." was kept as one turn in 2 of 3 runs. The
    third was affected by a probe bug (fixed) and split into two finals; the final answer still covered the wheel loader.
    New answer audio started 1.71–1.9 s after the clip ended.
  - **Thinking pause (1.0 s between "Can you tell me" and "what I should do next?"):** AssemblyAI ended the turn after
    part 1 in 3 of 3 runs. Cat began ("Yes, I"), was interrupted by part 2, then answered the whole question.
    **Not fixed** (`ASSEMBLYAI_MIN_TURN_SILENCE_MS=160` unchanged).
  - **Clarification:** not exercised live (0 transcription timeouts).
  - **Timings** (34 turns; speech end is estimated from VAD):

    | Stage | Warm p50 | Warm p95 | Cold p50 (n=3) |
    | --- | --- | --- | --- |
    | Speech end → STT final | 607 ms | 707 ms | — |
    | LLM first text | 813 ms | 1161 ms | 1190 ms |
    | First text → TTS audio | 264 ms | 454 ms | — |
    | Speech end → first agent audio | 1.70 s | 2.08 s | 1.92 s |

    Earlier interruption-latency samples mixed in natural reply ends (metric fixed), so they are not reported.
  - **Event loop:** p95 12.5–13.9 ms and p99 14–21 ms per session. The worst stalls were at session start: SSL context
    creation 387–1542 ms, Krisp filter init 152–417 ms. After the fix there was 1 mid-session Krisp native call of
    214 ms in 1 of 6 sessions; it was not the credential refresh.
  - **TTS pacing:** worst minimum margin per reply −71 ms (first reply of one session), otherwise −50 to +10 ms, with
    no more −400 ms class underruns. Audibility is not verified.
- **Cartesia 402:** at 20:04 UTC Cartesia started answering every TTS websocket with HTTP 402 Payment Required, so
  the agent could not speak. The probe runs most likely used up the credits: each "in detail" answer was about 970
  characters, and there were about 40 plus fixtures.

**Not verified:**
- Any human Playground session: listening quality, choppiness, and naturalness of pauses, acknowledgments and cues.
- The SDK resume after a false interruption.
- The clarification prompt.
- The style prompt's effect on real answers. It was committed after the last live run.

**Next steps:**
1. Add Cartesia credits.
2. Run README checklist items 7–7h in Playground with a person; record verdict lines and `session diagnostics`.
3. Decide on `ASSEMBLYAI_MIN_TURN_SILENCE_MS` (for example, trial 400 ms against latency) from the 7f result.

## 2026-09-24 — M9: Cartesia 401 root cause and fix (branch `voice`)

- **Where the message came from:** `doctor.check_cartesia` mapped any 401/403 from `POST /tts/bytes` to "API key
  rejected" and discarded the body. The 401 itself comes directly from Cartesia (JSON body "Invalid API key.",
  `x-request-id` header); on the websocket it is a handshake 401.
- **Key trace (booleans only):** no `CARTESIA_API_KEY` in the process environment before `load_dotenv`, nor in HKCU or
  HKLM environment; `.env` (resolved `livekit-voice/.env`, no BOM) has one assignment, no inline comment; value length 29,
  `sk_car_` prefix, `[A-Za-z0-9_-]` only, no whitespace/quotes/`Bearer`; `.env` value == settings `get_secret_value()`
  == `cartesia.TTS._opts.api_key`; not the masked `**********`; doctor and worker share `get_settings()`/`build_tts()`.
- **Provider matrix with the resolved key:**
  - raw key → 401 "Invalid API key" on `/tts/bytes` (versions 2025-04-16, 2026-08-14; `X-API-Key` and Bearer) and
    websocket handshake 401 (both versions, both headers) — identical to a fabricated `sk_car_` key;
  - raw key → `GET /voices` 200 (a fabricated key gets 401 "You must be logged in"), `POST /access-token` 200;
  - minted TTS access token → `/tts/bytes` 200 (with `X-API-Key` or Bearer, both versions) and websocket streaming
    7–11 audio chunks per request (header or `access_token` query, both versions). Max token TTL is 3600 s
    (provider 400 for larger values).
- **Correction:** entries M7/M8 said `GET /voices` "does not validate keys". That was wrong: it validates keys; the
  raw key is authentic but rejected specifically by the TTS endpoints.
- **Fix:** `cocoon_voice/cartesia_auth.py` (`CARTESIA_AUTH=access_token` default): mint a TTS-granted token, give it to
  the plugin, refresh 10 min before expiry and invalidate the plugin's websocket pool; worker, smoke and benchmark use
  it; doctor reports raw-key HTTP (WARN with provider message + request_id), token mint, token HTTP and plugin
  websocket streaming separately. `scripts/cartesia_key_check.py` tests a key from a hidden prompt.
- **Checks run:** `pytest` → 143 passed, 1 skipped (6 new in `test_cartesia_auth.py`). Doctor: vertex PASS (2968 ms;
  one earlier run took 45872 ms, not reproduced), assemblyai PASS, cartesia_raw_key WARN 401, cartesia_token PASS,
  cartesia_http PASS, cartesia_stream PASS (first audio 288 / 355 ms), livekit PASS. Plugin `synthesize()`: raw key
  401, token 2.19 s audio in 473 ms. Smoke: vertex PASS, cartesia PASS (first audio 229.9 ms, 7.5 s audio),
  assemblyai PASS (transcript "CAT 320 excavator, unit E742. Check the hydraulic hose at 3,500 hours, then walk around
  the machine."; WER 0.417 only from digits vs spelled numbers; final 762.6 ms after audio end).
- **Remaining provider-side question:** why Cartesia rejects raw keys on TTS for these accounts is not explained by
  their docs; request IDs are in this log for a support ticket. Next: Playground checklist.

## 2026-09-23 21:30 UTC — M8: livekit-wakeword acoustic mode; Cartesia key diagnosis (branch `voice`)

- **Decision (user):** use `livekit-wakeword` instead of Porcupine (Picovoice needs a company email).
- **Implemented:** `WAKE_MODE=livekit_wakeword` (`acoustic_wake.LiveKitWakeWordEngine`), engine inference on a dedicated
  thread with a bounded drop-oldest queue, engine-neutral `WAKE_PREROLL_MS` / `WAKE_ONLY_ACK_WAIT_MS` (renamed from
  the Porcupine-specific names), doctor check with per-call inference time and phrase/model mismatch warning,
  `wakeword/hey_cat.yaml` training config (validated against `livekit.wakeword.config.WakeWordConfig`) and
  `wakeword/README.md` (Colab / WSL2 training). `porcupine_gate.py` renamed to `acoustic_wake.py`.
- **Measured with LiveKit's real example model `hey_livekit.onnx` (Apache-2.0 fixture) on SAPI speech:**
  max scores "Hey LiveKit." 0.912, "Hey LiveKit, what is the track tension?" 0.761, "The live kit is ready." 0.598,
  "Hey liquid, pour it." 0.078, "Hey Cat, what should I check…" 0.038, background talk 0.021.
  `predict()` p50 57.7 ms / p95 94.3 ms per call (n=474, 80 ms hop) → 160 ms hop + worker thread; default threshold
  0.7. Doctor (offline) reported 67 ms/call.
- **Cartesia:** the new key (29 chars, `sk_car_` prefix, single `.env` entry, not overridden by the OS environment)
  is rejected with 401 "Invalid API key" by `/tts/bytes` and the TTS websocket, with API versions 2025-04-16 and
  2026-08-14 and with both `X-API-Key` and Bearer auth. `GET /voices` returns 200 for it (that endpoint does not
  validate keys). Cause is on the Cartesia account/key side; no usage is recorded because requests fail authentication.
- **Checks run:** `pytest` → 137 passed, 1 skipped (10 new real-model wake tests); `pip check` clean after pinning
  `livekit-wakeword==0.2.1`.
- **Not verified:** a "Hey Cat" model (not trained), real human voices, Playground speech (Cartesia).

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
