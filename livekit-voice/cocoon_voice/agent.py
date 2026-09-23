"""Cocoon standalone voice worker ("Cat"): LiveKit WebRTC -> AssemblyAI -> Gemini (Vertex) -> Cartesia.

Run from livekit-voice/:  python -m cocoon_voice.agent dev | start | console

One long-lived LiveKit Agents worker; one AgentSession + VoiceController per dispatched room.
The conversation brain comes from providers.create_brain() (the only replacement point for the
future LangGraph adapter). The wake gate decides which finalized utterances reach the brain; idle
speech is dropped in on_user_turn_completed via StopResponse before it enters chat history.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import AsyncIterable
from typing import Any

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    FlushSentinel,
    APIConnectOptions,
    JobContext,
    JobProcess,
    ModelSettings,
    StopResponse,
    TurnHandlingOptions,
    cli,
    llm,
    room_io,
    stt,
)
from livekit.agents.voice.agent_session import SessionConnectOptions

from . import speech_policy as sp
from .config import ConfigError, VoiceSettings, get_settings
from .diagnostics import LoopLagMonitor, SttInputMonitor, TtsPacing
from .observability import SessionMetrics
from .phrase_cache import PhraseCache
from .cartesia_auth import CartesiaTokenRefresher, describe_status
from .acoustic_wake import AcousticRouter, KeywordEngine, LiveKitWakeWordEngine, PorcupineEngine
from .announcements import AnnouncementPump
from .backend_client import BackendClient, BackendConfigError
from .bridge import CONFIG_SPEECH, TurnBridge, last_message, turn_id_for
from .remote_bridge import MissingOperatorId, SessionSpeaker, parse_job_metadata, session_request
from .providers import (NoiseSetup, build_noise_cancellation, build_stt, build_tts, check_noise_cancellation,
                        create_brain, krisp_filter_active, load_vad)
from .recording import InputRecorder, cleanup_recordings
from .streaming import EpochCounter, GenerationStats, guarded_stream
from .wake import WakeGate, WakeState, normalize

log = logging.getLogger("cocoon_voice.agent")

VAD_MIN_SILENCE_S = 0.45  # must match providers.load_vad(min_silence_duration=...)
PARTICIPANT_ABSENCE_TIMEOUT_S = 90.0


class VoiceController:
    """Session-scoped policy: wake gate, generation guard, cues, metrics. No cross-room state."""

    def __init__(self, settings: VoiceSettings, *, phrases: PhraseCache | None, metrics: SessionMetrics,
                 keyword_engine: KeywordEngine | None = None, recorder: InputRecorder | None = None,
                 threaded_engine: bool = True):
        self.s = settings
        gate_mode = "off" if settings.wake_mode == "off" else ("acoustic" if settings.acoustic_wake else "transcript")
        self.gate = WakeGate(settings.wake_phrase, mode=gate_mode,
                             active_timeout_s=settings.wake_active_timeout_s, debounce_s=settings.wake_debounce_s,
                             echo_guard_s=settings.wake_echo_guard_ms / 1000)
        self.phrases = phrases
        self.metrics = metrics
        self.epochs = EpochCounter()
        self.recorder = recorder
        self.session: AgentSession | None = None
        self.router = (AcousticRouter(keyword_engine, self.gate, preroll_ms=settings.wake_preroll_ms,
                                      on_wake=self._on_acoustic_wake, threaded=threaded_engine)
                       if keyword_engine is not None else None)
        self.keyword_engine = keyword_engine
        self.greeted = False
        self.llm_in_flight = 0
        self._agent_speaking = False
        self._agent_speech_ended_at = 0.0
        self._user_speaking = False
        self._overlapped = False
        self._text_since_wake = False
        self._tasks: set[asyncio.Task] = set()
        self.loop_lag = LoopLagMonitor()
        self.tts_replies = 0
        self.tts_underruns = 0
        self.false_interruptions = {"resumed": 0, "not_resumed": 0}
        self.transcription_timeouts = 0
        self.clarifications = 0
        self.turns_committed = 0
        self.interruption_by_turn: dict[str, int] = {}  # effective mode sampled at each committed turn
        self.overlap_verdicts = {"interruption": 0, "backchannel": 0, "agent_ended": 0}
        self.interruption_pending = False  # user audio overlapped our speech; SDK decides resume vs. new turn
        self.noise_processor = None
        self.stt_input = SttInputMonitor()
        self.tts_audio_s = 0.0  # total synthesized audio (announcement delivery checks it)
        # remote brain (VOICE_BRAIN=remote_langgraph): set by run_session; None = not connected
        self.bridge: TurnBridge | None = None
        self._remote_last: tuple[str, str, str, float] | None = None  # (normalized text, turn_id, text, time)

    # ------------------------------------------------------------------ lifecycle

    def attach(self, session: AgentSession) -> None:
        self.session = session
        session.on("user_state_changed", self._on_user_state)
        session.on("agent_state_changed", self._on_agent_state)
        session.on("user_input_transcribed", self._on_transcribed)
        session.on("conversation_item_added", self._on_item_added)
        session.on("agent_false_interruption", self._on_false_interruption)
        session.on("user_transcription_timeout", self._on_transcription_timeout)
        session.on("overlapping_speech", self._on_overlap_verdict)
        session.on("metrics_collected", self._on_sdk_metrics)
        session.on("error", self._on_error)
        self._spawn(self._timeout_loop())
        self._spawn(self.loop_lag.run())
        self._spawn(self._stt_input_loop())

    def effective_interruption(self) -> str:
        """What the SDK actually runs (a configured 'adaptive' is not proof the detector is active)."""
        activity = getattr(self.session, "_activity", None)
        active = bool(getattr(activity, "_interruption_detection_enabled", False))
        return "adaptive(active)" if active else f"vad(configured={self.s.interruption_mode})"

    def _sample_interruption_mode(self) -> None:
        # The SDK silently degrades to VAD after an unrecoverable detector error; make that visible.
        if self.session is None:
            return
        mode = self.effective_interruption()
        if mode != "adaptive(active)" and self.interruption_by_turn.get("adaptive(active)"):
            if mode not in self.interruption_by_turn:
                log.warning("adaptive interruption downgraded mid-session: now %s", mode)
        self.interruption_by_turn[mode] = self.interruption_by_turn.get(mode, 0) + 1

    def _on_overlap_verdict(self, ev) -> None:
        # Adaptive detector verdict for speech over the agent (no audio or text is logged).
        key = "agent_ended" if ev.agent_ended else ("interruption" if ev.is_interruption else "backchannel")
        self.overlap_verdicts[key] += 1
        log.info("overlap verdict: %s p=%.2f delay=%.0f ms", key, ev.probability, ev.detection_delay * 1000)
        self.metrics.event("overlap_verdict", verdict=key, probability=round(float(ev.probability), 3),
                           detection_delay_ms=round(ev.detection_delay * 1000))

    def _on_false_interruption(self, ev) -> None:
        # The SDK resumed (or dropped) the paused speech itself. We only record it: no new generation here.
        self.interruption_pending = False
        self.metrics.interruption_abandoned()
        key = "resumed" if ev.resumed else "not_resumed"
        self.false_interruptions[key] += 1
        self.metrics.event("false_interruption", resumed=bool(ev.resumed))
        log.info("false interruption: agent speech %s", "resumed" if ev.resumed else "NOT resumed")

    def _on_transcription_timeout(self, ev) -> None:
        self.transcription_timeouts += 1
        self.metrics.event("transcription_timeout", speech_duration_s=round(ev.speech_duration, 2))
        log.info("speech detected for %.2fs but no transcript arrived", ev.speech_duration)
        if (self.gate.state == WakeState.ACTIVE and ev.speech_duration >= self.s.clarify_min_speech_s
                and self.session is not None):
            self._spawn(self._clarify(self.turns_committed))

    async def _clarify(self, turns_at_timeout: int) -> None:
        """One short request to repeat, only if nothing else happened since the missed speech."""
        await asyncio.sleep(0.3)
        if (self.turns_committed != turns_at_timeout or self._user_speaking or self._agent_speaking
                or self.llm_in_flight or self.gate.state != WakeState.ACTIVE):
            return
        self.clarifications += 1
        log.info("asking the user to repeat (speech without a usable transcript)")
        await self.say_fixed(sp.CLARIFY)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def aclose(self) -> None:
        self.gate.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.recorder:
            self.recorder.close()
        if self.keyword_engine is not None:
            self.keyword_engine.delete()
        diag = {"loop_lag": self.loop_lag.summary(), "tts_replies": self.tts_replies,
                "tts_underruns": self.tts_underruns, "false_interruptions": self.false_interruptions,
                "transcription_timeouts": self.transcription_timeouts, "clarifications": self.clarifications,
                "krisp_filter_active": krisp_filter_active(self.noise_processor) if self.noise_processor else None,
                "interruption_by_turn": self.interruption_by_turn, "overlap_verdicts": self.overlap_verdicts}
        self.metrics.event("diagnostics", **diag)
        log.info("session diagnostics %s", diag)
        summary = self.metrics.close()
        log.info("session summary %s", summary)

    # ------------------------------------------------------------------ speaking fixed phrases

    async def say_fixed(self, text: str, *, allow_interruptions: bool = True, add_to_chat_ctx: bool = False):
        assert self.session is not None
        audio = None
        if self.phrases is not None:
            try:
                audio = (await self.phrases.get(text)).frames()
            except Exception as exc:
                log.warning("cached phrase unavailable (%s); synthesizing live", type(exc).__name__)
        kwargs: dict[str, Any] = {"audio": audio} if audio is not None else {}
        return self.session.say(text, allow_interruptions=allow_interruptions, add_to_chat_ctx=add_to_chat_ctx,
                                **kwargs)

    async def on_enter(self) -> None:
        if self.greeted:  # ordinary reconnects keep the same session: never greet twice
            return
        self.greeted = True
        # The greeting never contains the wake phrase, so our own audio cannot wake us.
        await self.say_fixed(self.s.voice_greeting, allow_interruptions=False, add_to_chat_ctx=True)
        if self.phrases is not None:
            self._spawn(self.phrases.prefetch(list(sp.CACHED_PHRASES)))
        log.info("greeted; wake gate %s in %s mode", self.gate.state.value, self.s.wake_mode)

    # ------------------------------------------------------------------ user turns (single submission path)

    async def on_user_turn(self, new_message: llm.ChatMessage) -> None:
        try:
            await self._route_turn(new_message)
        except asyncio.CancelledError:
            # the SDK replaced this turn (a newer final arrived) before routing finished
            self._log_route("cancelled", "superseded before routing completed", new_message.text_content or "")
            raise

    async def _route_turn(self, new_message: llm.ChatMessage) -> None:
        text = new_message.text_content or ""
        self.turns_committed += 1
        self.interruption_pending = False
        self._sample_interruption_mode()
        turn = self.metrics.current or self.metrics.begin_turn(VAD_MIN_SILENCE_S)
        turn.mark("turn_committed")
        decision = self.gate.on_utterance(text, overlapped_agent_speech=self._overlapped)
        self._log_decision(decision.action, decision.reason, text)
        if decision.action == "respond":
            if decision.text is not None and decision.text != text.strip():
                new_message.content = [decision.text]  # wake phrase removed; question kept once
            self._text_since_wake = True
            return  # the SDK now runs llm_node for this message exactly once
        self.metrics.end_turn(f"gated:{decision.action}")
        if decision.action == "ack":
            await self.say_fixed(sp.WAKE_ACK)
        elif decision.action == "stop":
            await self._stop_output()
        elif decision.action == "sleep":
            await self._stop_output()
            await self.say_fixed(sp.SLEEP_ACK)
        raise StopResponse()  # nothing from this utterance enters history or the brain

    async def _stop_output(self) -> None:
        assert self.session is not None
        self.epochs.next()  # any late chunk of the current reply is now stale
        try:
            await self.session.interrupt(force=True)
        except Exception as exc:
            log.debug("interrupt: %s", exc)

    def _log_decision(self, action: str, reason: str, text: str) -> None:
        if action == "respond":
            route = "accepted"  # handed to the brain; generation and playback are logged separately
        elif action == "ignore":
            route = "empty" if reason == "empty" else "wake-gated"
        else:
            route = f"command:{action}"  # ack / stop / sleep: handled locally, never sent to the brain
        self._log_route(route, reason, text, action=action)

    def _log_route(self, route: str, reason: str, text: str, *, action: str | None = None) -> None:
        detail = f"text={text!r}" if self.s.log_transcripts else f"chars={len(text)}"
        log.info("turn route=%s reason=%s action=%s state=%s wake=%s %s", route, reason, action or "-",
                 self.gate.state.value, self.s.wake_mode, detail)

    # ------------------------------------------------------------------ brain (llm_node)

    async def generate(self, agent: Agent, chat_ctx: llm.ChatContext, tools: list[llm.Tool],
                       model_settings: ModelSettings) -> AsyncIterable[Any]:
        if self.gate.state != WakeState.ACTIVE and not self._last_user_has_wake(chat_ctx):
            # e.g. a preemptive generation for idle speech: never call the brain while armed
            return
        epoch = self.epochs.next()
        turn = self.metrics.current
        if turn:
            turn.mark("llm_request")
        if self.s.voice_brain == "remote_langgraph":
            async for chunk in self._remote_reply(chat_ctx, epoch, turn):
                yield chunk
            return
        ctx = sp.bounded_context(chat_ctx, self.s.max_context_turns)
        stats = GenerationStats(epoch=epoch)
        self.llm_in_flight += 1
        try:
            async for chunk in guarded_stream(
                lambda: Agent.default.llm_node(agent, ctx, tools, model_settings),
                epoch=epoch, epochs=self.epochs,
                first_chunk_timeout=self.s.llm_first_chunk_timeout_s, stall_timeout=self.s.llm_stall_timeout_s,
                max_attempts=self.s.llm_max_attempts,
                cue_text=sp.thinking_cue(epoch) if self.s.thinking_cue_enabled else None,
                cue_delay=self.s.thinking_cue_delay_ms / 1000,
                failure_text=sp.LLM_FAILED, empty_text=sp.EMPTY_REPLY, stats=stats,
            ):
                if turn and stats.first_chunk_at and "llm_first_text" not in turn.marks:
                    turn.mark("llm_first_text", stats.first_chunk_at)
                yield chunk
        finally:
            self.llm_in_flight -= 1
            if turn:
                turn.cue_used = stats.cue_at is not None
                turn.llm_attempts = stats.attempts
                turn.error_category = stats.error_category
                turn.outcome_hint = stats.outcome
            if stats.error_category:
                self.metrics.provider_error(f"llm:{stats.error_category}")
            log.info("generation epoch=%d outcome=%s attempts=%d first_text_ms=%s cue=%s chars=%d", epoch,
                     stats.outcome, stats.attempts, stats.ms(stats.first_chunk_at), stats.cue_at is not None,
                     stats.chars)

    async def _remote_reply(self, chat_ctx: llm.ChatContext, epoch: int, turn) -> AsyncIterable[Any]:
        """One wake-approved final utterance -> one logical backend turn; only its speech reaches TTS.

        The backend returns the whole reply in one JSON response (no token streaming). No worker-side model
        is called and no answer is invented: failures map to fixed, honest phrases.
        """
        msg = last_message(chat_ctx)
        text = (msg.text_content or "").strip() if msg is not None and msg.role == "user" else ""
        if not text:
            log.info("remote brain: no new user message at the end of the context; nothing submitted")
            return
        if self.bridge is None:
            log.error("remote brain: not connected to the backend (see startup log); turn not submitted")
            yield CONFIG_SPEECH
            return
        turn_id, norm, now = turn_id_for(msg), normalize(text), time.monotonic()
        last = self._remote_last
        if last is not None and last[0] == norm and now - last[3] < 3.0:
            # the same words committed twice (duplicate final): replay the stored backend result under the
            # original turn_id and payload instead of creating a second backend turn
            turn_id, text = last[1], last[2]
            log.info("remote brain: duplicate final transcript; replaying backend turn %s", turn_id)
        else:
            self._remote_last = (norm, turn_id, text, now)
        started = time.perf_counter()
        self.llm_in_flight += 1
        task = asyncio.ensure_future(self.bridge.reply_for_turn(turn_id, text))
        outcome = "cancelled"
        cue = False
        try:
            if self.s.thinking_cue_enabled:
                done, _ = await asyncio.wait({task}, timeout=self.s.thinking_cue_delay_ms / 1000)
                if not done and self.epochs.current == epoch:
                    cue = True
                    yield sp.thinking_cue(epoch)
                    yield FlushSentinel()
            reply = await task
            outcome = reply.outcome
            if self.epochs.current != epoch:
                outcome = "stale"
                log.info("remote brain: dropping reply of superseded turn %s", turn_id)
                return
            if turn and "llm_first_text" not in turn.marks:
                turn.mark("llm_first_text")
            yield reply.speech
        finally:
            self.llm_in_flight -= 1
            if not task.done():
                # local barge-in or a newer turn: stop waiting. The backend has no cancel route, so the turn
                # may still complete there (and may save records); nothing is claimed as cancelled.
                task.cancel()
                log.info("remote brain: stopped waiting for backend turn %s; the backend may still complete it",
                         turn_id)
            if turn:
                turn.cue_used = cue
                turn.outcome_hint = outcome
            log.info("backend turn %s outcome=%s ms=%d cue=%s", turn_id, outcome,
                     (time.perf_counter() - started) * 1000, cue)

    def _last_user_has_wake(self, chat_ctx: llm.ChatContext) -> bool:
        for item in reversed(chat_ctx.items):
            if item.type == "message" and item.role == "user":
                return self.gate.matcher.split(item.text_content or "")[0]
        return False

    # ------------------------------------------------------------------ TTS (observe first substantive audio)

    async def synthesize(self, agent: Agent, text: AsyncIterable[str],
                         model_settings: ModelSettings) -> AsyncIterable[rtc.AudioFrame]:
        turn = self.metrics.current
        substantive_at: list[float] = []

        async def observed_text() -> AsyncIterable[str]:
            async for chunk in text:
                if chunk.strip() and chunk.strip() not in sp.THINKING_CUES and not substantive_at:
                    substantive_at.append(time.perf_counter())
                yield chunk

        pacing = TtsPacing()
        try:
            async for frame in Agent.default.tts_node(agent, observed_text(), model_settings):
                pacing.on_frame(frame.duration)
                if turn and substantive_at and "tts_first_audio" not in turn.marks:
                    turn.mark("tts_first_audio")
                yield frame
        finally:
            self.tts_audio_s += pacing.audio_s
            if pacing.frames:
                self.tts_replies += 1
                self.tts_underruns += pacing.underran
                self.metrics.event("tts_pacing", audio_s=round(pacing.audio_s, 2),
                                   min_margin_ms=round(pacing.min_margin_ms or 0.0, 1), underran=pacing.underran)
                log.info("tts pacing: %.2fs audio, min margin %.0f ms%s", pacing.audio_s,
                         pacing.min_margin_ms or 0.0, " (UNDERRUN)" if pacing.underran else "")

    # ------------------------------------------------------------------ STT input (Porcupine gating / recording)

    async def transcribe(self, agent: Agent, audio: AsyncIterable[rtc.AudioFrame],
                         model_settings: ModelSettings) -> AsyncIterable[stt.SpeechEvent]:
        source = self._tap_stt_input(self.recorder.tap(audio) if self.recorder else audio)
        if self.router is None:  # transcript/off mode: STT hears the room continuously, also while ARMED
            async for ev in Agent.default.stt_node(agent, source, model_settings):
                self._count_stt_event(ev)
                yield ev
            return
        feeder = self._spawn(self.router.run(source))
        try:
            while True:
                segment = await self.router.next_segment()
                if segment is None:
                    return
                log.info("acoustic wake: STT segment opened")
                async for ev in Agent.default.stt_node(agent, segment, model_settings):
                    self._count_stt_event(ev)
                    yield ev
                log.info("STT segment closed (gate %s)", self.gate.state.value)
        finally:
            feeder.cancel()

    async def _tap_stt_input(self, audio: AsyncIterable[rtc.AudioFrame]) -> AsyncIterable[rtc.AudioFrame]:
        async for frame in audio:
            self.stt_input.on_frame(frame)
            yield frame

    def _count_stt_event(self, ev) -> None:
        if isinstance(ev, stt.SpeechEvent):
            self.stt_input.on_event(final=ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT)

    async def _stt_input_loop(self) -> None:
        last_kind: str | None = None
        while True:
            await asyncio.sleep(self.stt_input.window_s)
            diagnosis = self.stt_input.close_window()
            if diagnosis is None or (self.router is not None and self.gate.state != WakeState.ACTIVE):
                last_kind = None  # STT produced events, or an acoustic mode keeps STT closed while ARMED
                continue
            kind, message = diagnosis
            # a silent operator is normal: report each kind once until it changes; always report lost transcripts
            if kind != last_kind or kind == "no_transcripts":
                log.info("stt input: %s", message)
            last_kind = kind

    def _on_acoustic_wake(self) -> None:
        self._text_since_wake = False
        log.info("acoustic keyword detected (engine=%s)", self.s.wake_mode)
        self._spawn(self._ack_if_wake_only())

    async def _ack_if_wake_only(self) -> None:
        """Acoustic modes: acknowledge only if no question follows the keyword."""
        wait = self.s.wake_only_ack_wait_ms / 1000
        deadline = time.monotonic() + 4.0
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if self._text_since_wake or self.llm_in_flight:
                return
            if self._user_speaking:
                quiet_since = None
            else:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= wait:
                    break
        if not self._text_since_wake and self.gate.state == WakeState.ACTIVE:
            await self.say_fixed(sp.WAKE_ACK)

    # ------------------------------------------------------------------ session events

    def _on_user_state(self, ev) -> None:
        now = time.monotonic()
        if ev.new_state == "speaking":
            self._user_speaking = True
            self.gate.note_activity()
            self._overlapped = self._agent_speaking or (now - self._agent_speech_ended_at) < self.gate.echo_guard_s
            if self._agent_speaking:
                self.interruption_pending = True
            if self._agent_speaking and self.gate.state == WakeState.ACTIVE:
                self.metrics.interruption_detected()
        elif ev.old_state == "speaking":
            self._user_speaking = False
            self.gate.note_activity()
            turn = self.metrics.begin_turn(VAD_MIN_SILENCE_S)
            turn.mark("vad_speech_end")

    def _on_agent_state(self, ev) -> None:
        if ev.new_state == "speaking":
            self._agent_speaking = True
            if self.metrics.current:
                self.metrics.current.mark("playout_start")
        elif ev.old_state == "speaking":
            self._agent_speaking = False
            self._agent_speech_ended_at = time.monotonic()
            self.gate.note_activity()
            self.metrics.output_stopped()
            turn = self.metrics.current
            if turn is not None and "llm_request" in turn.marks:
                self.metrics.end_turn(turn.outcome_hint)

    def _on_transcribed(self, ev) -> None:
        if ev.is_final and self.metrics.current is not None:
            self.metrics.current.mark("stt_final")
        if self.s.log_transcripts and ev.is_final:
            log.info("final transcript: %r", ev.transcript)

    def _on_item_added(self, ev) -> None:
        item = ev.item
        if getattr(item, "role", None) != "assistant":
            return
        if getattr(item, "interrupted", False):
            self.metrics.interruption_confirmed()
            # With TTS-aligned transcripts the SDK stores only the words actually played.
            log.info("playback interrupted: history keeps %d played chars", len(item.text_content or ""))
        else:  # finished (or resumed after a false interruption): an overlap was not an interruption
            self.metrics.interruption_abandoned()
            log.info("playback completed: %d chars", len(item.text_content or ""))

    def _on_sdk_metrics(self, ev) -> None:
        m = ev.metrics
        fields = {k: getattr(m, k) for k in ("type", "ttft", "ttfb", "end_of_utterance_delay",
                                               "transcription_delay", "duration", "audio_duration")
                  if getattr(m, k, None) is not None}
        self.metrics.event("sdk_metrics", **fields)

    def _on_error(self, ev) -> None:
        err = ev.error
        source = type(getattr(ev, "source", None)).__name__
        category = f"{source}:{type(err).__name__}:{'recoverable' if getattr(err, 'recoverable', False) else 'fatal'}"
        self.metrics.provider_error(category)
        log.warning("session error %s", category)
        status = getattr(getattr(err, "error", err), "status_code", None)
        if status in (401, 402, 403, 429):
            # Account-level rejections are not transient: say so plainly instead of a silent agent.
            hint = " (account credits or plan exhausted: the agent cannot speak until this is fixed)"                 if status == 402 else ""
            log.error("%s provider rejected the request: HTTP %s %s%s", source, status, describe_status(status), hint)

    def is_busy(self) -> bool:
        """Never expire the wake window while listening, generating, speaking or recovering an interruption."""
        sdk_state = getattr(self.session, "agent_state", None)
        return (self._agent_speaking or self._user_speaking or self.llm_in_flight > 0
                or self.interruption_pending or sdk_state in ("thinking", "speaking"))

    async def _timeout_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            if self.gate.check_timeout(busy=self.is_busy()):
                log.info("wake gate re-armed after %.0fs of inactivity", self.s.wake_active_timeout_s)


class CatAgent(Agent):
    def __init__(self, controller: VoiceController):
        super().__init__(instructions=sp.INSTRUCTIONS)
        self.controller = controller

    async def on_enter(self) -> None:
        await self.controller.on_enter()

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        await self.controller.on_user_turn(new_message)

    async def llm_node(self, chat_ctx, tools, model_settings):
        async for chunk in self.controller.generate(self, chat_ctx, tools, model_settings):
            yield chunk

    async def tts_node(self, text, model_settings):
        async for frame in self.controller.synthesize(self, text, model_settings):
            yield frame

    async def stt_node(self, audio, model_settings):
        async for ev in self.controller.transcribe(self, audio, model_settings):
            yield ev


def session_conn_options(settings: VoiceSettings) -> SessionConnectOptions:
    return SessionConnectOptions(
        stt_conn_options=APIConnectOptions(max_retry=3, retry_interval=1.0, timeout=10.0),
        # The SDK would retry an LLM stream even after chunks were spoken (_retry_on_chunk_sent=True),
        # so SDK retries are off; streaming.guarded_stream retries only before the first chunk.
        llm_conn_options=APIConnectOptions(max_retry=0, timeout=settings.llm_first_chunk_timeout_s + 4),
        # TTS never retries after partial audio (SDK behaviour); this only covers failures before audio.
        tts_conn_options=APIConnectOptions(max_retry=2, retry_interval=0.5, timeout=10.0),
    )


def build_session(settings: VoiceSettings, vad) -> AgentSession:
    return AgentSession(
        stt=build_stt(settings),
        tts=build_tts(settings),  # credential replaced by CartesiaTokenRefresher when CARTESIA_AUTH=access_token
        llm=create_brain(settings),
        vad=vad,
        turn_handling=TurnHandlingOptions(
            # One end-of-turn authority: AssemblyAI endpointing. No extra SDK delay is stacked on top.
            turn_detection="stt",
            endpointing={"min_delay": 0.0, "max_delay": 3.0},
            interruption={"enabled": True, "mode": settings.interruption_mode,
                          "min_duration": settings.interruption_min_duration_s,
                          "min_words": settings.interruption_min_words,
                          "resume_false_interruption": True,
                          "false_interruption_timeout": settings.false_interruption_timeout_s},
            preemptive_generation={"enabled": settings.preemptive_generation},
        ),
        conn_options=session_conn_options(settings),
        use_tts_aligned_transcript=True,  # interrupted replies keep only the words actually played
        transcription_timeout=settings.transcription_timeout_s or None,
    )


class ControllerSpeaker(SessionSpeaker):
    """Announcement output through the session, reporting whether audio was actually synthesized."""

    def __init__(self, session: AgentSession, controller: VoiceController):
        super().__init__(session)
        self._controller = controller
        self._mark = 0.0

    def say(self, text: str):
        self._mark = self._controller.tts_audio_s
        return super().say(text)

    def produced_audio(self) -> bool | None:
        return self._controller.tts_audio_s > self._mark


async def connect_backend(ctx: JobContext, settings: VoiceSettings, controller: VoiceController,
                          session: AgentSession, participant: rtc.RemoteParticipant) -> None:
    """Bind this room to a backend session and start announcements. Never falls back to a local model."""
    try:
        request = session_request(ctx.room.name, participant, parse_job_metadata(ctx.job.metadata), settings)
    except MissingOperatorId as exc:
        log.error("remote brain disabled for this session: %s", exc)
        return
    client = BackendClient(settings.backend_url, settings.service_token.get_secret_value(),  # type: ignore[union-attr]
                           request_timeout=settings.backend_request_timeout,
                           connect_timeout=settings.backend_connect_timeout,
                           max_attempts=settings.backend_max_attempts, turn_deadline=settings.turn_deadline)
    ctx.add_shutdown_callback(client.aclose)
    bridge = TurnBridge(client, None, request=request,
                        trusted_session_id=parse_job_metadata(ctx.job.metadata).get("session_id"))
    controller.bridge = bridge
    log.info("remote brain: backend=%s machine=%s operator=%s key=%s", settings.backend_url, request.machine_id,
             request.operator_id, request.client_session_key)
    try:
        binding = await asyncio.wait_for(bridge.ensure_bound(), timeout=settings.backend_request_timeout)
        log.info("remote brain: bound to backend session %s", binding.session_id)
    except BackendConfigError as exc:
        log.error("remote brain: backend rejected the session binding (configuration): %s", exc)
    except Exception as exc:  # unreachable/slow backend: bind lazily on the first turn
        log.warning("remote brain: backend not reachable yet (%s); will bind on the first turn",
                    type(exc).__name__)
    pump = AnnouncementPump(client, lambda: bridge.session_id, ControllerSpeaker(session, controller),
                            consumer_id=f"cocoon-voice:{ctx.room.name}")
    pump.start()
    ctx.add_shutdown_callback(pump.stop)


def prewarm(proc: JobProcess) -> None:
    """Runs once per job process, off the first-turn path: load local models."""
    proc.userdata["vad"] = load_vad()


def build_server(settings: VoiceSettings | None = None) -> AgentServer:
    settings = settings or get_settings()
    server = AgentServer(host=settings.health_host, port=settings.health_port, setup_fnc=prewarm)

    @server.rtc_session(agent_name=settings.agent_name)
    async def entrypoint(ctx: JobContext) -> None:
        await run_session(ctx, settings)

    return server


async def run_session(ctx: JobContext, settings: VoiceSettings) -> None:
    await ctx.connect()
    participant = await ctx.wait_for_participant()  # one operator per room: first standard participant
    noise: NoiseSetup = build_noise_cancellation(settings)
    metrics = SessionMetrics(session_label=ctx.room.name, config={**settings.safe_summary(),
                                                                   "noise_effective": noise.effective},
                             metrics_dir=settings.metrics_dir, log_transcripts=settings.log_transcripts)
    engine: KeywordEngine | None = None
    if settings.wake_mode == "livekit_wakeword":
        engine = LiveKitWakeWordEngine(settings.wakeword_model_path, settings.wakeword_threshold,  # type: ignore[arg-type]
                                       settings.wakeword_hop_ms)
        log.info("livekit-wakeword engine ready model=%s threshold=%.2f hop=%dms", engine.name,
                 settings.wakeword_threshold, settings.wakeword_hop_ms)
        if (mismatch := settings.wake_model_mismatch()):
            log.warning(mismatch)
    elif settings.wake_mode == "porcupine":
        engine = PorcupineEngine(settings.picovoice_access_key.get_secret_value(),  # type: ignore[union-attr]
                                 settings.porcupine_keyword_path, settings.porcupine_sensitivity)  # type: ignore[arg-type]
        log.info("porcupine engine ready sample_rate=%d frame_length=%d", engine.sample_rate, engine.frame_length)
    recorder = InputRecorder(settings.record_audio_dir, ctx.room.name) if settings.record_audio else None

    session = build_session(settings, ctx.proc.userdata["vad"])
    if settings.cartesia_auth == "access_token":
        refresher = CartesiaTokenRefresher(session.tts, settings.cartesia_api_key.get_secret_value())  # type: ignore
        await refresher.start()  # raises CartesiaAuthError with status/request_id if the key cannot mint
        ctx.add_shutdown_callback(refresher.aclose)
    phrases = PhraseCache(session.tts, cache_dir=settings.tts_cache_dir, provider="cartesia",
                          model=settings.cartesia_model, voice=settings.cartesia_voice_id,
                          language=settings.voice_language, speed=settings.cartesia_speed)
    controller = VoiceController(settings, phrases=phrases, metrics=metrics, keyword_engine=engine,
                                 recorder=recorder)
    controller.noise_processor = noise.processor
    controller.attach(session)
    ctx.add_shutdown_callback(controller.aclose)

    absence: dict[str, asyncio.TimerHandle] = {}

    def _gone(p: rtc.RemoteParticipant) -> None:
        if p.identity == participant.identity:
            log.info("operator disconnected; keeping session for %.0fs to allow reconnect",
                     PARTICIPANT_ABSENCE_TIMEOUT_S)
            absence["t"] = asyncio.get_running_loop().call_later(
                PARTICIPANT_ABSENCE_TIMEOUT_S, lambda: ctx.shutdown("operator did not reconnect"))

    def _back(p: rtc.RemoteParticipant) -> None:
        if p.identity == participant.identity and "t" in absence:
            absence.pop("t").cancel()
            metrics.reconnects += 1
            metrics.event("reconnect")
            log.info("operator reconnected; same session, no second greeting")

    ctx.room.on("participant_disconnected", _gone)
    ctx.room.on("participant_connected", _back)

    if settings.wake_mode == "transcript":
        log.warning("WAKE_MODE=transcript: room audio is streamed to AssemblyAI even while armed (billed); "
                    "this is Playground test mode, not on-device keyword spotting")
    elif settings.wake_mode == "off":
        log.warning("WAKE_MODE=off: no wake gating; every final transcript is answered (local Playground mode)")
    if noise.degraded:
        log.warning("AUDIO DEGRADED: %s", noise.effective)
    log.info("starting session room=%s participant=%s noise=%s wake=%s brain=%s:%s", ctx.room.name,
             participant.identity, noise.effective, settings.wake_mode, settings.voice_brain, settings.vertex_model)
    await session.start(
        agent=CatAgent(controller),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=participant.identity,
            audio_input=room_io.AudioInputOptions(noise_cancellation=noise.processor),
            close_on_disconnect=False,
        ),
    )
    log.info("effective interruption handling: %s (stt aligned_transcript=%s)", controller.effective_interruption(),
             session.stt.capabilities.aligned_transcript if session.stt else None)
    if settings.voice_brain == "remote_langgraph":
        await connect_backend(ctx, settings, controller, session, participant)
    prewarm_tts = getattr(session.tts, "prewarm", None)
    if callable(prewarm_tts):
        prewarm_tts()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command in ("dev", "start", "console", "connect"):
        problems = settings.problems("worker")
        try:
            check_noise_cancellation(settings)
        except ConfigError as exc:
            problems.append(str(exc))
        if problems:
            print("Cocoon voice worker cannot start:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            print("Run `python -m cocoon_voice.doctor` for details.", file=sys.stderr)
            raise SystemExit(2)
        for obsolete in settings.obsolete_settings_present():
            log.warning("ignoring obsolete setting %s", obsolete)
        if settings.record_audio:
            removed = cleanup_recordings(settings.record_audio_dir, settings.record_retention_hours)
            log.warning("RECORD_AUDIO=true: participant input will be written locally (%d old file(s) removed)",
                        removed)
        log.info("effective config %s", settings.safe_summary())
        log.info("effective wake mode: %s", settings.wake_mode_description())
        log.info("worker-side enhancement: %s", "OFF (NOISE_CANCELLATION=none)" if settings.noise_cancellation == "none"
                 else f"krisp {settings.noise_profile}")
        log.info("transcript logging: %s", "ON (local only)" if settings.log_transcripts else "off")
        # LiveKit's CLI installs its own console handler (text for `dev`, JSON for `start`). Drop the startup
        # handler so every record is printed once, in one format.
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        logging.getLogger("cocoon_voice").setLevel(settings.log_level)
    cli.run_app(build_server(settings))


if __name__ == "__main__":
    main()
