# livekit-voice: Cocoon standalone voice assistant ("Cat")

**Phase 1 (current):** a standalone voice assistant for trying out the Cocoon voice experience in LiveKit Playground or the Agent Console. It is **not connected to langgraph-agent yet**. Phase 2, the LangGraph integration, waits until this voice experience has been evaluated and accepted.

```
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt; pip install -e . --no-deps
python -m livekit.agents download-files
python -m cocoon_voice.doctor              # config + provider checks
python -m cocoon_voice.smoke               # small paid live checks
pytest                                     # offline tests
python -m cocoon_voice.agent dev           # worker; health on 127.0.0.1:8081
python scripts\dispatch.py token --room cat-test-1 --identity operator-7   # join link that dispatches cocoon-voice
python -m cocoon_voice.benchmark run --turns 10 --stt   # once both keys exist
python -m cocoon_voice.benchmark report                 # after live sessions
```

```
browser mic ──WebRTC──▶ LiveKit Cloud room ──▶ this worker (one AgentSession per dispatched room)
   Krisp VIVA input filter ─▶ Silero VAD + wake gate ("Hey Cat")
   ─▶ AssemblyAI streaming STT (endpointing = end-of-turn)
   ─▶ Gemini on Vertex AI via ADC (streaming text)   ◀── providers.create_brain(): the only brain
   ─▶ Cartesia streaming TTS ─▶ WebRTC ─▶ browser speaker
```

## Status: what has and has not been verified

| Area | Evidence (see `docs/work-log.md`) |
|---|---|
| Settings, doctor, provider factories | Offline tests pass. The doctor passes Vertex (ADC) and LiveKit on the dev machine. |
| Worker registration | `cocoon-voice` registered with LiveKit Cloud; `GET :8081/worker` returned its JSON. |
| Wake gate, turn policy, streaming guard, Porcupine plumbing, metrics | 127 offline tests pass, including real `AgentSession` runs with a scripted LLM. |
| Vertex latency | Measured through the real plugin: warm TTFT p50 668 ms / p95 695 ms (n=9, prewarmed). |
| VAD against synthetic cab noise | 0 false speech detections; speech kept as one segment down to −5 dB SNR (offline, synthetic). |
| AssemblyAI live | Verified: the local "Hey Cat, what should I check…" clip was transcribed exactly. |
| Cartesia TTS | Works through minted access tokens (`CARTESIA_AUTH=access_token`): plugin HTTP synthesis and websocket streaming verified live, first audio 230–355 ms. The raw key is rejected by Cartesia's TTS endpoints (see below). |
| Dispatch and real speech through LiveKit Cloud | Verified with a **synthetic operator** (`scripts/live_probe.py`: synthetic voices published as a microphone, agent audio received back), 5 sessions, 2026-09-24. **A human Playground session is still not verified.** |
| Adaptive interruption | Active live (`effective interruption handling: adaptive(active)`). Synthetic machinery/fan/impact noise during replies created no turns and no pauses (3/3 runs). Natural-voice backchannels got a `backchannel` verdict 8 of 8 times; failures and limits in `docs/work-log.md` (M10). |
| Krisp noise filtering | Filter active in live sessions (`krisp_filter_active: True`). Human listening quality not verified. |
| **Cartesia credits** | **Blocked 2026-09-24:** Cartesia answers TTS with HTTP 402 (credits exhausted); the agent cannot speak until credits are added. |
| **Acoustic wake (livekit-wakeword)** | Implemented and verified offline with LiveKit's real `hey_livekit` model (detection, rejection, threaded routing). **"Hey Cat" model not trained yet** (`wakeword/README.md`). |

## Selected models and settings

| Stage | Choice | Why and evidence |
|---|---|---|
| STT | AssemblyAI `universal-3-5-pro` via `livekit-plugins-assemblyai==1.8.2` | AssemblyAI recommends it for English voice agents; it supports keyterm prompting. Domain keyterms: `ASSEMBLYAI_KEYTERMS` (Cocoon, Caterpillar, CAT, Hey Cat, excavator …). English only in this phase. Hindi and Hinglish are **not** validated. |
| End of turn | `turn_detection="stt"` with AssemblyAI `min_turn_silence=160 ms` and `max_turn_silence=1400 ms`; SDK `endpointing.min_delay=0` | One authority. In STT mode the SDK would otherwise add `min_delay` (default 0.3 s) on top of AssemblyAI's endpointing. The punctuation-based model ends a finished sentence after about 160 ms of silence and waits up to 1.4 s through mid-sentence pauses. |
| Brain | `gemini-2.5-flash` on Vertex AI, `location=global`, `thinking_budget=0`, `max_output_tokens=220`, context capped at 8 exchanges | Streaming TTFT measured on Vertex `global` (5 warm runs each): 2.5-flash p50 738 / max 967 ms; 2.5-flash-lite p50 597 / max 678 ms; 3.5-flash (minimal thinking) p50 1047 / max 11918 ms; 3-flash-preview p50 4691 / max 19133 ms. Configurable with `VERTEX_MODEL`. |
| TTS | Cartesia `sonic-3` via `livekit-plugins-cartesia==1.8.2`, voice `f786b574-daa5-4673-aa0c-cbe3e8534c02` | Plugin default and LiveKit-documented voice. **Its name, warmth and pronunciation have not been heard yet:** the doctor prints its name once a key is set, and the smoke check writes a CAT / unit-ID / number pronunciation sample. |
| VAD | Silero (`livekit-plugins-silero`), loaded in prewarm, `min_silence=0.45 s` | Bundled model, no key |
| Noise | Krisp **VIVA voice isolation** (`livekit-plugins-krisp==0.4.2`, native `win_amd64` wheel) in the worker; `NOISE_PROFILE=noise_suppression` selects Krisp NC | Current LiveKit guidance: VIVA removes competing voices; NC is background-noise suppression. Both need LiveKit Cloud auth (the job's JWT) and do not use a separate Krisp key. LiveKit documents voice isolation as an additional-cost feature, so check your plan. |
| Interruption | LiveKit adaptive barge-in (`INTERRUPTION_MODE=adaptive`, 0.5 s, 0 words); SDK resumes a false interruption after 2.0 s; one clarification when speech gets no transcript; SDK LLM retries off | See "Streaming, barge-in and recovery" below |

## Install

Python 3.11, native Windows first. Use PowerShell from `livekit-voice\`:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt        # requirements.txt = worker runtime only
pip install -e . --no-deps
python -m livekit.agents download-files    # plugin assets (google, krisp, silero)
Copy-Item .env.example .env                # then fill the keys below
```

Bash (macOS/Linux) from `livekit-voice/`:

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e . --no-deps
python -m livekit.agents download-files
cp .env.example .env
```

With uv instead: `uv venv --python 3.11 .venv`, then `uv pip sync requirements-dev.txt`, then `uv pip install -e . --no-deps`. The pins are compiled with `uv pip compile --universal --python-version 3.11` from `requirements*.in`.

### Keys and where they come from (`.env.example` is the requested env.example)

| Variable | Needed for | Source |
|---|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Worker, dispatch helper | LiveKit Cloud project → Settings → API keys. The browser (Console or Playground) must use this **same project**. |
| `ASSEMBLYAI_API_KEY` | Worker (STT), smoke, benchmark `--stt` | https://www.assemblyai.com/app/api-keys |
| `CARTESIA_API_KEY` | Worker (TTS), smoke, benchmark | https://play.cartesia.ai/keys (used to mint TTS access tokens; `CARTESIA_AUTH`) |
| Vertex AI | Worker (brain), doctor, smoke, benchmark | **No key.** Uses existing Application Default Credentials (`gcloud auth application-default login`) with `GOOGLE_CLOUD_PROJECT=orbit-507316`, `GOOGLE_CLOUD_LOCATION=global`, `GOOGLE_GENAI_USE_VERTEXAI=true`. `GOOGLE_API_KEY` and `GEMINI_API_KEY` are rejected. Nothing here reads the ADC file. |
| `LIVEKIT_WAKEWORD_MODEL_PATH` | `WAKE_MODE=livekit_wakeword` only | No key. A `.onnx` you train (`wakeword/README.md`) |
| `PICOVOICE_ACCESS_KEY`, `PORCUPINE_KEYWORD_PATH` | `WAKE_MODE=porcupine` only | Picovoice Console (needs a company email) |
| Silero VAD, Krisp | — | No key (Krisp authenticates through the LiveKit Cloud job) |

The settings are typed and validated. The worker refuses to start and lists the **names** of missing or invalid settings. `VOICE_PROFILE=production` rejects transcript wake mode, degraded audio, preemptive generation, transcript logging and recording.

`COCOON_AGENT_NAME` is a deprecated alias of `LIVEKIT_AGENT_NAME`; they must not disagree. These phase-0 names are ignored with a warning: `COCOON_STT_MODEL`, `COCOON_TTS_MODEL`, `COCOON_TTS_VOICE`, `COCOON_STT_LANGUAGE` and `COCOON_GREETING`.

### Cartesia authentication (why tokens)

Evidence from 2026-09-24:
- For our Cartesia keys, **every** synthesis path rejects the raw API key with `401 "Invalid API key"`. That covers `/tts/bytes` and `/tts/websocket`, `Cartesia-Version` 2025-04-16 and 2026-08-14, and both the `X-API-Key` and `Authorization: Bearer` headers.
- The same key authenticates `GET /voices` (a fake key gets 401 there) and `POST /access-token`.
- A TTS-granted access token minted from it synthesizes over HTTP and streams over the websocket, including when sent in the `X-API-Key` header the LiveKit plugin uses.

So with `CARTESIA_AUTH=access_token` (default), each session:
1. Mints a 1-hour token.
2. Hands the token to the plugin.
3. Refreshes it 10 minutes before expiry and recycles pooled websocket connections.

The raw key only goes to `/access-token` and is never logged. What happened was checked and ruled out:
- A stale variable in the process or Windows environment.
- A masked `SecretStr`.
- Whitespace, quotes or a `Bearer` prefix.
- A header mix-up.

The cause is on Cartesia's side, and our key tracing cannot explain it further.

To check any key without putting it in `.env` or a command line, run `python scripts\cartesia_key_check.py`. It uses a hidden prompt and reports each path separately.

## Commands (from `livekit-voice/`, venv active)

| Command | What it does |
|---|---|
| `python -m cocoon_voice.doctor` | Checks settings, VAD, the noise plugin, Porcupine (if selected), Vertex via ADC, AssemblyAI (issues a temporary streaming token, not printed), the Cartesia voice (prints its name), and LiveKit (lists rooms). Errors are categorised and sanitised. `--offline` skips the network. |
| `python -m cocoon_voice.smoke` | Small paid live checks: Vertex through the plugin, Cartesia (writes `recordings/smoke-cartesia-pronunciation.wav`), and AssemblyAI on that synthetic audio |
| `python -m cocoon_voice.agent dev` | Worker with reload. Use `start` for production-style, `console` for local mic/speaker in the terminal (not a WebRTC/Playground test). Stop with Ctrl+C. |
| `pytest` | Offline tests (no keys, no network) |
| `$env:COCOON_INTEGRATION_BACKEND_URL=...; pytest -m integration` | Phase-0 backend integration (future-only) |
| `python -m cocoon_voice.benchmark run --turns 10 [--stt] [--noise machinery --snr 10] [--no-prewarm]` | Bounded synthetic pipeline benchmark: at most 30 turns and 10 minutes. Writes `metrics/benchmark-*.json`. |
| `python -m cocoon_voice.benchmark report` | Aggregates `metrics/session-*.jsonl` from live worker sessions (p50/p95, cold/warm) |
| `powershell -ExecutionPolicy Bypass -File scripts\make_speech_fixtures.ps1` | Local SAPI speech fixtures for the VAD noise tests (git-ignored) |
| `python scripts\dispatch.py token\|dispatch\|list --room <room>` | Dev dispatch helper (explicit agent name) |
| `python scripts\make_natural_fixtures.py` | Operator clips in a second Cartesia voice (`natural_*.wav`, git-ignored; small, billed) |
| `python scripts\live_probe.py [--fixtures sapi\|natural] [--scenarios baseline,noise,backchannel,stop,correction,pause]` | Synthetic operator in a fresh LiveKit Cloud room against a running worker (a few minutes; bills STT, LLM and TTS: a full run used several thousand Cartesia characters). Writes `metrics/probe-*.json` and the received agent audio. Not a substitute for a human test. |

Health endpoint (SDK built-in, `COCOON_HEALTH_HOST:COCOON_HEALTH_PORT`, default `127.0.0.1:8081`):
- `GET /` returns `OK`. It stays `OK` while LiveKit connection retries continue, and becomes `503` only after they are exhausted.
- `GET /worker` returns JSON with `agent_name`.

A worker that starts has already passed settings validation, since startup refuses invalid configuration. Provider reachability is what `doctor` checks.

## Testing in the browser (Playground / Agent Console)

### Three milestones to tell apart

1. **Worker registered.** The log shows `registered worker … agent_name=cocoon-voice`, and `GET 127.0.0.1:8081/worker` shows the name. *Verified 2026-09-23.*
2. **Agent dispatched into a room.** The log shows `starting session room=… participant=… noise=… wake=…`.
3. **Real speech connected.** You hear the greeting "Hi, I'm Cat, your Cocoon assistant." Then "Hey Cat, can you hear me?" produces `turn route=accepted` and a spoken answer.

**Common trap:** because the worker registers with an explicit `agent_name`, LiveKit does **not** auto-dispatch it. A registered worker receives no job unless something dispatches exactly `cocoon-voice` in the **same project**. The usual causes are a name mismatch or the browser being on another project. A token-embedded dispatch fires only when the join *creates* the room.

### Remote LangGraph brain (`VOICE_BRAIN=remote_langgraph`)

- **Flow:** each final, wake-approved utterance is sent as one backend turn to `POST /v1/sessions/{id}/turns`, with
  `turn_id=lk-<chat item id>` reused for every retry and 202 poll. Only the returned `speech` is spoken. The
  backend returns the whole reply at once; there is no token streaming yet.
- **Setup:** in `livekit-voice/.env` set `VOICE_BRAIN=remote_langgraph`, `COCOON_BACKEND_URL`,
  `COCOON_SERVICE_TOKEN` (the same value as `langgraph-agent/.env`), `COCOON_DEFAULT_MACHINE_ID=EXC_DEMO_001` and
  `COCOON_DEFAULT_OPERATOR_ID=OP_DEMO_1_1`. Keep `PREEMPTIVE_GENERATION=false`.
- **Start order:** start the backend (`langgraph-agent`: `python -m cocoon_agent`, then check `/readyz` for
  `"status":"ready"` and `"catalog":true`), then the worker.
- **Log lines:**
  - `remote brain: bound to backend session ...`
  - `backend turn lk-... outcome=completed|unknown|rejected|config_error|stale|cancelled`
  - `announcement ... played` / the delivery report
- **Failures:**
  - Configuration errors (401/403, 422 unknown machine/operator, 503 catalog_unavailable) are not retried.
  - Retryable 503s back off, respecting `Retry-After`, with the same `turn_id`.
  - An unconfirmed outcome is spoken as "... I can't confirm that yet".
  - A barge-in stops playback only; the backend has no cancel route, so the turn may still complete there.

### Continuous-listening Playground mode (no wake phrase, worker enhancement off)

A local diagnostic mode: every final transcript goes to the agent, and worker-side Krisp is disabled to isolate
event-loop stalls. Set these in `livekit-voice/.env`:

```
WAKE_MODE=off
NOISE_CANCELLATION=none
ALLOW_DEGRADED_AUDIO=true
LOG_TRANSCRIPTS=true
```

Then, from `livekit-voice/`: `.\.venv\Scripts\python.exe -m cocoon_voice.agent dev` (plain-text logs, one line per
record; `start` prints the same records as JSON).

- **Startup lines to expect:**
  - `effective wake mode: off (...)`
  - `worker-side enhancement: OFF (NOISE_CANCELLATION=none)`
  - `transcript logging: ON (local only)`
  - Per session: `AUDIO DEGRADED: DEGRADED:none (...)`
- **Per final turn:**
  - `final transcript: '...'` (before any wake filtering).
  - One `turn route=accepted|wake-gated|empty|command:<stop/sleep/ack>|cancelled reason=...`.
  - `generation epoch=N outcome=...`, then `playback completed|interrupted`.
- **When STT is quiet for 15 s:** a `stt input: ...` line says whether no audio arrived, the audio was silent, or
  speech-level audio got no transcripts. AssemblyAI's own "no messages received" warning alone is normal while
  you are silent.
- **To restore Hey Cat:** set `WAKE_MODE=transcript` (the gate starts ARMED; "Hey Cat" may be followed by the
  request; follow-ups need no phrase for `WAKE_ACTIVE_TIMEOUT_SECONDS`).
- **To restore Krisp:** set `NOISE_CANCELLATION=krisp` and `ALLOW_DEGRADED_AUDIO=false`.
- **To stop logging transcripts:** set `LOG_TRANSCRIPTS=false`.
- **Rules:** keep browser noise filtering off, so enhancement is never stacked. Restart the worker after editing `.env`.

### Connecting (pick one)

- **Deterministic (recommended).**
  1. `python scripts\dispatch.py token --room cat-test-1 --identity operator-7`. Use a fresh room name.
  2. Open the printed `meet.livekit.io/custom?...` link, or paste the URL and token into the hosted Agents Playground's manual connection.
  3. For a room that already exists: `python scripts\dispatch.py dispatch --room <room>`.

  (I have not verified that the Meet link and the Playground's manual connection work.)
- **Agent Console.** LiveKit Cloud dashboard (same project) → Agents → **Launch Console**. The docs say the Console works with agents running locally, but I could not confirm how it targets an explicitly named agent. If it does not dispatch `cocoon-voice`, use the deterministic path.

**Browser audio controls:**
- Keep the browser's echo cancellation on (the default for `getUserMedia`).
- **Do not** enable Krisp or enhanced noise filtering in the browser; the worker already runs Krisp.
- Test with headphones first for a clean baseline, then with speakers separately. Headphones are not evidence of speakerphone echo performance.

### Manual checklist (record results in `docs/work-log.md`)

1. Join: expect exactly one greeting. Reconnect within 90 s: expect no second greeting and a `reconnected` log line.
2. Say something without the wake phrase ("did you see the game"): no reply, and the log shows `turn route=wake-gated reason=armed: no wake phrase`.
3. "Hey Cat." gives a short "Hey, I'm here. What do you need?" with no LLM call.
4. "Hey Cat, can you hear me?" gives one answer, and no acknowledgment over it.
5. Follow-ups without "Hey Cat": short yes/no answers, a long question, and a question with a mid-sentence pause ("what should I… check on the tracks"). It must not be cut off at the pause.
6. Near-misses: "hey cap", "okay cat", "hey cats". None of them should wake it.
7. While Cat is answering, say "stop" (then separately "wait"). Speech should stop promptly, with no reply.
   - 7a. During a long answer ("explain the pre-start walkaround"), say a soft "mm-hmm", "yeah", "okay", "right". Expected: Cat keeps talking and the log shows `overlap verdict: backchannel`. Record any `verdict: interruption` for a backchannel.
   - 7b. Clap, drop something or run a fan near the mic during an answer. Expected: no pause and no turn. If Cat pauses, it should resume the same sentence within about 2 s (`false interruption` counted in diagnostics), not restart the answer.
   - 7c. During an answer, say "okay, but I meant the other machine". Expected: Cat stops and answers about the other machine.
   - 7d. When Cat asks a question, answer only "yes" or "no". Expected: it carries on from its question.
   - 7e. While Cat is idle but active, mumble for about a second. Expected: at most one "I missed the last part. Could you say that again?".
   - 7f. Start a question, pause about a second to think, then finish it ("can you tell me ... what I should check next"). Record whether Cat starts answering the first half (seen in synthetic runs; see M10).
   - 7g. Talk for several turns without "Hey Cat". Expected: it stays active and re-arms only after the idle timeout.
   - 7h. Search the worker log for `adaptive interruption downgraded`, `HTTP 402`, `event loop lag` and `UNDERRUN`, and copy the final `session diagnostics` line.
8. "Go to sleep." gives "Okay, going quiet." and later speech is ignored. After 45 s of silence Cat re-arms by itself.
9. "What can you do?" should describe this as a voice trial with business features not yet connected.
10. Noise: play machinery, fan or impact audio from another device, and have a second person talk nearby. Check for false replies and for truncated or missed questions. Then repeat on speakers without headphones (echo).
11. Pronunciation: ask about "a CAT three-twenty" and "unit E-742 at 3,500 hours".
12. `python -m cocoon_voice.benchmark report` gives worker-side p50/p95 from the session. Note manual impressions of client playback separately.

## Wake gate ("Hey Cat")

- **States:** `ARMED` → `ACTIVE` → `ARMED` or `CLOSED`, separate from the SDK's listening/thinking/speaking states.
- **Matching:** exact and leading. It is word-bounded "hey cat" after case and punctuation normalisation. There is no fuzzy matching, so near-misses stay armed.
- **Gating:** while ARMED, utterances without the phrase are dropped in `on_user_turn_completed` with `StopResponse`, *before* the SDK appends them to chat history. `llm_node` also refuses to call the brain while armed.
- **Wake only vs wake plus question:** a wake-only utterance gets a cached "Hey, I'm here. What do you need?" A wake-plus-question utterance has the phrase removed and the question answered once.
- **Staying awake:** ACTIVE accepts follow-ups without the phrase. It re-arms after `WAKE_ACTIVE_TIMEOUT_SECONDS` of inactivity, but never while Cat is speaking, the user is speaking, or a request is in flight.
- **Commands:** `stop` and `go to sleep` only count as standalone utterances; "what does stop mean?" is a question, not a command.
- **Repeats:** repeated wakes are debounced (`WAKE_DEBOUNCE_SECONDS`). A duplicate final transcript within 1.5 s is ignored only while ARMED. While ACTIVE, a committed turn is always answered: the SDK has already cut the reply, so ignoring the turn would leave silence.
- **Echo guard:** a wake phrase heard while Cat is speaking, or within `WAKE_ECHO_GUARD_MS` after, cannot wake an armed gate. The greeting and fixed phrases never contain "Hey Cat".
- **Not security:** activation is not operator authentication. Nearby voices are mitigated by Krisp VIVA and debounce, not by speaker verification.

**`WAKE_MODE=transcript`** (default, works now without any model): AssemblyAI hears the room continuously, so idle speech is streamed and billed. It is not on-device or private keyword spotting.

**`WAKE_MODE=livekit_wakeword`** (recommended acoustic mode; open source, no account or key):
- The worker runs LiveKit's `livekit-wakeword` classifier (Apache-2.0, ONNX) on the selected participant's **room audio** (after the Krisp filter). It never opens a local microphone.
- The router is the single consumer of the audio stream. It resamples to 16 kHz, scores a rolling 2 s window every `LIVEKIT_WAKEWORD_HOP_MS` (160 ms), and clears the window after a hit so one utterance cannot fire twice.
- Inference takes about 58–67 ms per call on the dev laptop, so it runs on a **dedicated thread** with a bounded drop-oldest queue, never on the event loop.
- STT segments open only while ACTIVE and start with `WAKE_PREROLL_MS` (600 ms) of pre-roll, so "Hey Cat, <question>" is not clipped. Each frame is forwarded once.
- A wake-only acknowledgment plays only if no speech follows within `WAKE_ONLY_ACK_WAIT_MS`. There is no silent fallback to transcript mode.
- **The "Hey Cat" model still has to be trained:** follow [wakeword/README.md](wakeword/README.md) (Google Colab or WSL2; config `wakeword/hey_cat.yaml`).
- Until then, test the acoustic path with LiveKit's example model: `LIVEKIT_WAKEWORD_MODEL_PATH=tests/fixtures/wakeword/hey_livekit.onnx` and `WAKE_PHRASE=Hey LiveKit`.

  Verified offline with that real model on synthetic speech:

  | Clip | Result |
  |---|---|
  | "Hey LiveKit." | detected once (score 0.91) |
  | "Hey LiveKit, what is the track tension?" | detected once (0.76), question forwarded to STT |
  | "Hey Cat…", background talk, "hey liquid" | rejected (≤0.08) |
  | "The live kit is ready." | rejected at the 0.7 threshold (score 0.60) |

**`WAKE_MODE=porcupine`** (alternative; needs a Picovoice AccessKey, which requires a company email):
- Same router and threading, with a custom `Hey Cat` `.ppn` from https://console.picovoice.ai/ (Windows x86_64 for this machine).
- Set `PICOVOICE_ACCESS_KEY` and `PORCUPINE_KEYWORD_PATH`. "Hey Cat" is not a built-in Porcupine keyword. Nothing fakes a `.ppn`.

For the future Android client, keyword detection belongs on the device. livekit-wakeword has on-device (Swift/Rust) runtimes for the same `.onnx`. A hosted Playground worker cannot be an always-listening phone service, and server-side detection still requires the browser microphone to be published.

## Streaming, barge-in and recovery

- **Streaming path:** Vertex chunks stream into `llm_node` and then into the Cartesia streaming TTS node. The plugin's sentence tokenizer coalesces text, so audio starts after the first sentence rather than per token or after the whole reply.
- **Epochs:** each generation gets an epoch. `stop` or a new turn advances it, so late chunks from an old reply are dropped.
- **Played text:** with `use_tts_aligned_transcript` and Cartesia word timestamps, an interrupted reply keeps only the words actually played in history.
- **No replayed speech:** the SDK would retry an LLM stream even after it produced chunks, and would restart the answer. SDK LLM retries are therefore off, and `guarded_stream` retries (bounded, jittered) **only before the first chunk**. After partial output, the partial answer stands. TTS never retries after partial audio (SDK behaviour).
- **Timeouts and fallbacks:**
  - First-chunk timeout 3.5 s (up to 3 attempts, only before any text) and mid-stream stall timeout 5 s.
  - An empty reply gets a short fallback, and a failure before any output gets "Sorry, I couldn't get an answer just now…".
  - Errors are categorised as auth, permission, rate_limit, model_unavailable or timeout.
- **Thinking cue:** if no answer text has arrived after `THINKING_CUE_DELAY_MS` (1.2 s), one short cue is spoken inside that turn's speech. The cue rotates between "One moment.", "Let me think." and "Hmm, give me a second.". It plays at most once per turn across retries, is cancelled with the turn, and never follows answer text. There are no artificial delays and no background sounds.
- **Speaking style:** the prompt asks for short spoken turns with contractions and varied acknowledgments, used only when natural. For a procedure it gives the first two or three steps, then offers to continue, and it asks one question at a time.
- **Cached phrases:** the greeting, the wake acknowledgment, "Okay, going quiet.", the clarification and the failure phrase are synthesised once. They are cached in memory and in `.cache/tts`, keyed by provider, model, voice, language, speed and text.
- **Turn ownership (SDK-native):**
  - LiveKit's adaptive interruption model decides whether speech over Cat is a barge-in or a backchannel. For a backchannel the SDK drops the transcript and Cat keeps talking.
  - `min_words` stays 0 so that single-word "stop" and "wait" still interrupt. A word blacklist is deliberately not used.
  - If the SDK pauses for sound that yields no transcript, it resumes the same speech after `FALSE_INTERRUPTION_TIMEOUT_SECONDS`.
  - Any final transcript ends the paused reply (SDK behaviour). Every committed turn while ACTIVE is therefore answered, so the operator never gets silence.
- **Fallback:**
  - Adaptive needs LiveKit Cloud's inference service. On an unrecoverable detector error (observed: error 2005 "quota exceeded"), the SDK switches to VAD barge-in for the rest of the session.
  - The worker logs `adaptive interruption downgraded mid-session` and counts turns per mode in `session diagnostics`.
  - `INTERRUPTION_MODE=vad` forces the fallback.
- **Missed speech:** Cat asks once, "I missed the last part. Could you say that again?", when all of these hold:
  - The SDK reports speech with no transcript within `TRANSCRIPTION_TIMEOUT_SECONDS`.
  - The speech lasted at least `CLARIFY_MIN_SPEECH_SECONDS`.
  - No turn arrived meanwhile, and neither side is speaking.
- **Preemptive generation:** `PREEMPTIVE_GENERATION=false` by default. It is allowed only for this tool-free phase, and it must be off for the future action-capable backend.

## Privacy and logs

- No transcripts or audio are logged by default. Decisions log text **length**.
- `LOG_TRANSCRIPTS=true` and `RECORD_AUDIO=true` are explicit development opt-ins. Recordings go to `recordings/` (post-Krisp input) and are deleted after `RECORD_RETENTION_HOURS`.
- Per-turn metrics go to `metrics/session-*.jsonl` without text.
- Secrets appear only as `set` or `MISSING`.
- Future deployments should use a workload or service identity, never a copied user ADC file.

## Replacement point for phase 2 (LangGraph)

`cocoon_voice/providers.py:create_brain()` is the only place the brain is chosen. Phase 2 adds `VOICE_BRAIN=remote_langgraph`, returning a streaming `llm.LLM` adapter. The adapter should:
- Forward the completed user turn to `langgraph-agent`.
- Reuse the tested `TurnBridge` and `BackendClient` semantics (stable `turn_id`, 202 polling, unknown-outcome wording).
- Stream the returned speech.
- Keep preemptive generation disabled.

The LiveKit chat context stays the single history on this side; the backend owns business state.

Phase-0 tooling is kept, tested and **not wired in**: `bridge.py`, `backend_client.py`, `announcements.py`, `remote_bridge.py`, `mock_backend.py` and `scripts/backend_probe.py`. It uses the future-only `COCOON_BACKEND_URL` and `COCOON_SERVICE_TOKEN`; `LANGGRAPH_BASE_URL` is reserved as the phase-2 name.

## Known production-readiness gaps

- Real speech, Krisp effect, acoustic wake accuracy, speakerphone echo and human accents are unmeasured (keys and assets missing).
- One operator per room. There is no speaker verification.
- Client playback timing is not observable from the worker. Playout metrics are worker-side proxies.
- The thinking cue text becomes part of that turn's assistant message in history.
- Backchannels interrupt the current reply (see the trade-off above).
- No deployment manifest. Deploy with a service identity for Vertex and a region-appropriate Porcupine model.
