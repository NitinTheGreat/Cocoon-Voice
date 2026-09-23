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
from .observability import SessionMetrics
from .phrase_cache import PhraseCache
from .porcupine_gate import AcousticRouter, KeywordEngine, PorcupineEngine
from .providers import NoiseSetup, build_noise_cancellation, build_stt, build_tts, create_brain, load_vad
from .recording import InputRecorder, cleanup_recordings
from .streaming import EpochCounter, GenerationStats, guarded_stream
from .wake import WakeGate, WakeState

log = logging.getLogger("cocoon_voice.agent")

VAD_MIN_SILENCE_S = 0.45  # must match providers.load_vad(min_silence_duration=...)
PARTICIPANT_ABSENCE_TIMEOUT_S = 90.0


class VoiceController:
    """Session-scoped policy: wake gate, generation guard, cues, metrics. No cross-room state."""

    def __init__(self, settings: VoiceSettings, *, phrases: PhraseCache | None, metrics: SessionMetrics,
                 keyword_engine: KeywordEngine | None = None, recorder: InputRecorder | None = None):
        self.s = settings
        self.gate = WakeGate(settings.wake_phrase, mode=settings.wake_mode,
                             active_timeout_s=settings.wake_active_timeout_s, debounce_s=settings.wake_debounce_s,
                             echo_guard_s=settings.wake_echo_guard_ms / 1000)
        self.phrases = phrases
        self.metrics = metrics
        self.epochs = EpochCounter()
        self.recorder = recorder
        self.session: AgentSession | None = None
        self.router = (AcousticRouter(keyword_engine, self.gate, preroll_ms=settings.porcupine_preroll_ms,
                                      on_wake=self._on_acoustic_wake)
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

    # ------------------------------------------------------------------ lifecycle

    def attach(self, session: AgentSession) -> None:
        self.session = session
        session.on("user_state_changed", self._on_user_state)
        session.on("agent_state_changed", self._on_agent_state)
        session.on("user_input_transcribed", self._on_transcribed)
        session.on("conversation_item_added", self._on_item_added)
        session.on("agent_false_interruption", lambda ev: self.metrics.interruption_abandoned())
        session.on("metrics_collected", self._on_sdk_metrics)
        session.on("error", self._on_error)
        self._spawn(self._timeout_loop())

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
        text = new_message.text_content or ""
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
        if self.s.log_transcripts:
            log.info("wake decision=%s reason=%s state=%s text=%r", action, reason, self.gate.state.value, text)
        else:
            log.info("wake decision=%s reason=%s state=%s chars=%d", action, reason, self.gate.state.value,
                     len(text))

    # ------------------------------------------------------------------ brain (llm_node)

    async def generate(self, agent: Agent, chat_ctx: llm.ChatContext, tools: list[llm.Tool],
                       model_settings: ModelSettings) -> AsyncIterable[Any]:
        if self.gate.state != WakeState.ACTIVE and not self._last_user_has_wake(chat_ctx):
            # e.g. a preemptive generation for idle speech: never call the brain while armed
            return
        epoch = self.epochs.next()
        ctx = sp.bounded_context(chat_ctx, self.s.max_context_turns)
        turn = self.metrics.current
        if turn:
            turn.mark("llm_request")
        stats = GenerationStats(epoch=epoch)
        self.llm_in_flight += 1
        try:
            async for chunk in guarded_stream(
                lambda: Agent.default.llm_node(agent, ctx, tools, model_settings),
                epoch=epoch, epochs=self.epochs,
                first_chunk_timeout=self.s.llm_first_chunk_timeout_s, stall_timeout=self.s.llm_stall_timeout_s,
                max_attempts=self.s.llm_max_attempts,
                cue_text=sp.THINKING_CUE if self.s.thinking_cue_enabled else None,
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
                if chunk.strip() and chunk.strip() != sp.THINKING_CUE and not substantive_at:
                    substantive_at.append(time.perf_counter())
                yield chunk

        async for frame in Agent.default.tts_node(agent, observed_text(), model_settings):
            if turn and substantive_at and "tts_first_audio" not in turn.marks:
                turn.mark("tts_first_audio")
            yield frame

    # ------------------------------------------------------------------ STT input (Porcupine gating / recording)

    async def transcribe(self, agent: Agent, audio: AsyncIterable[rtc.AudioFrame],
                         model_settings: ModelSettings) -> AsyncIterable[stt.SpeechEvent]:
        source = self.recorder.tap(audio) if self.recorder else audio
        if self.router is None:  # transcript mode: STT hears the room continuously (idle cost applies)
            async for ev in Agent.default.stt_node(agent, source, model_settings):
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
                    yield ev
                log.info("STT segment closed (gate %s)", self.gate.state.value)
        finally:
            feeder.cancel()

    def _on_acoustic_wake(self) -> None:
        self._text_since_wake = False
        log.info("acoustic keyword detected (engine=porcupine)")
        self._spawn(self._ack_if_wake_only())

    async def _ack_if_wake_only(self) -> None:
        """Porcupine mode: acknowledge only if no question follows the keyword."""
        wait = self.s.porcupine_wake_only_wait_ms / 1000
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
        if getattr(item, "role", None) == "assistant" and getattr(item, "interrupted", False):
            # With TTS-aligned transcripts the SDK stores only the words actually played.
            log.info("assistant reply interrupted; history keeps %d played chars", len(item.text_content or ""))

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

    async def _timeout_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            busy = self._agent_speaking or self._user_speaking or self.llm_in_flight > 0
            if self.gate.check_timeout(busy=busy):
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
        tts=build_tts(settings),
        llm=create_brain(settings),
        vad=vad,
        turn_handling=TurnHandlingOptions(
            # One end-of-turn authority: AssemblyAI endpointing. No extra SDK delay is stacked on top.
            turn_detection="stt",
            endpointing={"min_delay": 0.0, "max_delay": 3.0},
            interruption={"enabled": True, "mode": "vad", "min_duration": settings.interruption_min_duration_s,
                          "resume_false_interruption": True,
                          "false_interruption_timeout": settings.false_interruption_timeout_s},
            preemptive_generation={"enabled": settings.preemptive_generation},
        ),
        conn_options=session_conn_options(settings),
        use_tts_aligned_transcript=True,  # interrupted replies keep only the words actually played
    )


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
    if settings.wake_mode == "porcupine":
        engine = PorcupineEngine(settings.picovoice_access_key.get_secret_value(),  # type: ignore[union-attr]
                                 settings.porcupine_keyword_path, settings.porcupine_sensitivity)  # type: ignore[arg-type]
        log.info("porcupine engine ready sample_rate=%d frame_length=%d", engine.sample_rate, engine.frame_length)
    recorder = InputRecorder(settings.record_audio_dir, ctx.room.name) if settings.record_audio else None

    session = build_session(settings, ctx.proc.userdata["vad"])
    phrases = PhraseCache(session.tts, cache_dir=settings.tts_cache_dir, provider="cartesia",
                          model=settings.cartesia_model, voice=settings.cartesia_voice_id,
                          language=settings.voice_language, speed=settings.cartesia_speed)
    controller = VoiceController(settings, phrases=phrases, metrics=metrics, keyword_engine=engine,
                                 recorder=recorder)
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
            build_noise_cancellation(settings)
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
    cli.run_app(build_server(settings))


if __name__ == "__main__":
    main()
