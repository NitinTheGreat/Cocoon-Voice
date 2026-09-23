"""Cocoon LiveKit Agents worker (Developer A).

Pipeline: WebRTC audio -> LiveKit Inference STT -> HTTP POST to the Cocoon backend
(inside llm_node) -> LiveKit Inference TTS. No realtime speech-to-speech model and
no second reasoning LLM run here.

Run from livekit-voice/:  python -m cocoon_voice.agent dev|start|console|download-files
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterable
from typing import Any

from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    Agent,
    AgentServer,
    AgentSession,
    APIConnectOptions,
    JobContext,
    ModelSettings,
    TurnHandlingOptions,
    cli,
    inference,
    llm,
    room_io,
)

from . import contract as c
from .announcements import AnnouncementPump
from .backend_client import BackendClient, BackendError
from .bridge import TurnBridge, bind_session
from .config import VoiceSettings, get_settings

log = logging.getLogger("cocoon_voice.agent")


class BackendBridgeLLM(llm.LLM):
    """Placeholder that never generates text.

    livekit-agents 1.8.x skips replying to a user turn when the session has no LLM
    (agent_activity: "skip response if no llm is set"), so the session needs an
    llm.LLM instance for llm_node to run at all. CocoonAgent.llm_node replaces the
    default generation with an HTTP call to the backend, so chat() is unreachable.
    """

    @property
    def model(self) -> str:
        return "cocoon-backend-http"

    @property
    def provider(self) -> str:
        return "cocoon"

    def chat(self, *, chat_ctx: llm.ChatContext, tools: list | None = None,
             conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS, **kwargs: Any) -> llm.LLMStream:
        raise RuntimeError("BackendBridgeLLM.chat() must not be called: CocoonAgent.llm_node calls the backend")


class CocoonAgent(Agent):
    def __init__(self, bridge: TurnBridge):
        # Instructions are unused: all reasoning happens in the backend.
        super().__init__(instructions="Replies are produced by the Cocoon backend over HTTP.")
        self.bridge = bridge

    async def llm_node(
        self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings
    ) -> AsyncIterable[str]:
        started = time.perf_counter()
        speech = await self.bridge.reply_for(chat_ctx)
        log.info("llm_node done ms=%d spoke=%s", (time.perf_counter() - started) * 1000, bool(speech))
        if speech:
            yield speech  # only final operator-facing text reaches TTS


class SessionSpeaker:
    """Adapter the announcement pump uses to speak without overlapping ordinary speech."""

    def __init__(self, session: AgentSession):
        self._session = session

    async def wait_until_quiet(self, max_wait: float) -> None:
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            if self._session.agent_state not in ("thinking", "speaking") and self._session.user_state != "speaking":
                return
            await asyncio.sleep(0.1)

    def say(self, text: str):
        # session.say() queues behind any in-progress agent speech; it never runs llm_node.
        return self._session.say(text, allow_interruptions=True, add_to_chat_ctx=True)


def parse_job_metadata(raw: str | None) -> dict[str, str]:
    """Job metadata comes from a server-side dispatch, so it is trusted for session binding."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("ignoring non-JSON job metadata")
        return {}
    return {k: str(v) for k, v in data.items() if k in ("session_id", "operator_id", "machine_id") and v}


def session_request(room_name: str, participant: rtc.RemoteParticipant, meta: dict[str, str],
                    settings: VoiceSettings) -> c.SessionCreateRequest:
    attrs = participant.attributes or {}
    return c.SessionCreateRequest(
        client_session_key=f"lk:{room_name}:{participant.identity}",
        room_name=room_name,
        participant_identity=participant.identity,
        operator_id=meta.get("operator_id") or attrs.get("operator_id") or participant.identity,
        machine_id=meta.get("machine_id") or attrs.get("machine_id") or settings.default_machine_id,
    )


def build_session(settings: VoiceSettings) -> AgentSession:
    return AgentSession(
        stt=inference.STT(model=settings.stt_model, language=settings.stt_language),
        tts=inference.TTS(model=settings.tts_model, voice=settings.tts_voice),
        llm=BackendBridgeLLM(),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            # Speculative generation would POST action-capable turns before the user finished.
            preemptive_generation={"enabled": False},
        ),
    )


def build_server(settings: VoiceSettings | None = None) -> AgentServer:
    settings = settings or get_settings()
    server = AgentServer(host=settings.health_host, port=settings.health_port)

    @server.rtc_session(agent_name=settings.agent_name)
    async def entrypoint(ctx: JobContext) -> None:
        await ctx.connect()
        # One operator per room (v1): bind to the first standard participant.
        participant = await ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD)
        meta = parse_job_metadata(ctx.job.metadata)
        client = BackendClient(
            settings.backend_url, settings.service_token.get_secret_value(),
            request_timeout=settings.backend_request_timeout, connect_timeout=settings.backend_connect_timeout,
            max_attempts=settings.backend_max_attempts, turn_deadline=settings.turn_deadline,
        )
        pump: AnnouncementPump | None = None

        async def cleanup() -> None:
            if pump is not None:
                await pump.stop()
            await client.aclose()

        ctx.add_shutdown_callback(cleanup)
        request = session_request(ctx.room.name, participant, meta, settings)
        try:
            binding = await bind_session(client, request, meta.get("session_id"))
        except BackendError as exc:
            log.error("backend unreachable at job start (%s); will bind on the first turn", exc)
            binding = None
        bridge = TurnBridge(client, binding, request, meta.get("session_id"))
        session = build_session(settings)
        await session.start(
            agent=CocoonAgent(bridge),
            room=ctx.room,
            room_options=room_io.RoomOptions(participant_identity=participant.identity),
        )
        session.say(settings.greeting if binding else
                    "Hi, I'm Cocoon. I can't reach the site system yet, but you can still ask me.",
                    allow_interruptions=True)

        pump = AnnouncementPump(
            client, lambda: bridge.session_id, SessionSpeaker(session),
            consumer_id=f"voice-{ctx.room.name}-{participant.identity}"[:120].replace(" ", "-"),
            interval=settings.event_poll_interval, max_backoff=settings.event_poll_max_backoff,
            quiet_wait=settings.announcement_quiet_wait, playout_timeout=settings.announcement_playout_timeout,
        )
        pump.start()

        @session.on("close")
        def _on_close(_ev: Any) -> None:
            asyncio.create_task(pump.stop())

    return server


def main() -> None:
    settings = get_settings()
    missing = settings.missing_livekit()
    if missing:
        log.warning("missing LiveKit settings: %s (dev/start/console need them)", ", ".join(missing))
    cli.run_app(build_server(settings))


if __name__ == "__main__":
    main()
