"""Remote-brain pieces for VOICE_BRAIN=remote_langgraph.

providers.create_brain() returns BackendBridgeLLM (a placeholder the SDK needs to run llm_node);
VoiceController.generate() sends each wake-approved final turn through TurnBridge/BackendClient.
SessionSpeaker plays backend announcements through the session's normal speech output.
CocoonAgent is the earlier phase-0 agent, kept for its tests.
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
    AgentSession,
    APIConnectOptions,
    ModelSettings,
    llm,
)

from . import contract as c
from .bridge import TurnBridge
from .config import VoiceSettings

log = logging.getLogger("cocoon_voice.remote_bridge")


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

    def produced_audio(self) -> bool | None:
        """None = unknown. Subclasses report whether the last say() actually synthesized audio."""
        return None


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


class MissingOperatorId(ValueError):
    pass


def session_request(room_name: str, participant: rtc.RemoteParticipant, meta: dict[str, str],
                    settings: VoiceSettings) -> c.SessionCreateRequest:
    """Catalog IDs from job metadata, then the participant attribute, then the configured defaults.

    The LiveKit participant identity is never used as a catalog operator ID. The client_session_key stays
    lk:<room>:<identity>, so a resumed room keeps its original backend session (the backend answers 409 if
    the same key is later sent with a different operator or machine; that is reported, never re-keyed).
    """
    attrs = participant.attributes or {}
    operator_id = meta.get("operator_id") or attrs.get("operator_id") or settings.default_operator_id
    if not operator_id:
        raise MissingOperatorId("no operator_id in job metadata or participant attributes, and "
                                "COCOON_DEFAULT_OPERATOR_ID is not set")
    return c.SessionCreateRequest(
        client_session_key=f"lk:{room_name}:{participant.identity}",
        room_name=room_name,
        participant_identity=participant.identity,
        operator_id=operator_id,
        machine_id=meta.get("machine_id") or attrs.get("machine_id") or settings.default_machine_id,
    )
