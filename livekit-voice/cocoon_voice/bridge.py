"""The single submission path from a confirmed LiveKit user turn to the backend.

Called from the agent's llm_node (VoiceController.generate), which LiveKit runs after end-of-turn
detection on a final, wake-approved transcript. Partial transcripts never reach it, and preemptive
generation must stay disabled in remote mode (config enforces it).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from livekit.agents import llm

from . import contract as c
from .backend_client import (
    BackendClient,
    BackendConfigError,
    BackendRejected,
    BackendUnavailable,
    SessionNotFound,
    TurnOutcomeUnknown,
)

log = logging.getLogger("cocoon_voice.bridge")

UNKNOWN_OUTCOME_SPEECH = (
    "I'm having trouble reaching the site system, so I can't confirm that yet. Ask me again in a moment."
)
REJECTED_SPEECH = "Sorry, I couldn't process that request."
CONFIG_SPEECH = "I'm not connected to the site system correctly, so I can't help with that yet."
EMPTY_SPEECH = "Sorry, I didn't catch that."


@dataclass(frozen=True)
class BridgeReply:
    speech: str
    outcome: str  # completed | unknown | rejected | config_error | empty


@dataclass
class SessionBinding:
    """Keeps LiveKit room/participant identity and the backend session/thread consistent."""

    request: c.SessionCreateRequest
    session_id: str

    @property
    def room_name(self) -> str:
        return self.request.room_name

    @property
    def participant_identity(self) -> str:
        return self.request.participant_identity


async def bind_session(client: BackendClient, request: c.SessionCreateRequest,
                       trusted_session_id: str | None = None) -> SessionBinding:
    if trusted_session_id and await client.session_exists(trusted_session_id):
        log.info("using session_id from job metadata: %s", trusted_session_id)
        return SessionBinding(request, trusted_session_id)
    if trusted_session_id:
        log.warning("job metadata session_id %s not found; creating by client_session_key", trusted_session_id)
    session = await client.ensure_session(request)
    log.info("bound room=%s participant=%s -> session_id=%s", request.room_name, request.participant_identity,
             session.session_id)
    return SessionBinding(request, session.session_id)


def last_message(chat_ctx: llm.ChatContext) -> llm.ChatMessage | None:
    for item in reversed(chat_ctx.items):
        if item.type == "message":
            return item
    return None


def turn_id_for(message: llm.ChatMessage) -> str:
    """Stable per utterance: the same chat item always maps to the same turn_id."""
    return f"lk-{message.id}"


class TurnBridge:
    """binding may start as None when the backend was unreachable at job start; it is bound lazily."""

    def __init__(self, client: BackendClient, binding: SessionBinding | None,
                 request: c.SessionCreateRequest | None = None, trusted_session_id: str | None = None):
        if binding is None and request is None:
            raise ValueError("need a binding or a session request")
        self._client = client
        self.binding = binding
        self._request = binding.request if binding else request
        self._trusted_session_id = trusted_session_id

    @property
    def session_id(self) -> str | None:
        return self.binding.session_id if self.binding else None

    async def ensure_bound(self) -> SessionBinding:
        if self.binding is None:
            self.binding = await bind_session(self._client, self._request, self._trusted_session_id)
        return self.binding

    async def reply_for(self, chat_ctx: llm.ChatContext) -> str | None:
        msg = last_message(chat_ctx)
        if msg is None or msg.role != "user":
            # e.g. a reply requested without a new user utterance: never resubmit an older turn.
            log.info("no new user message at the end of the context; nothing to submit")
            return None
        text = (msg.text_content or "").strip()
        if not text:
            return EMPTY_SPEECH
        return (await self.reply_for_turn(turn_id_for(msg), text)).speech

    async def reply_for_turn(self, turn_id: str, text: str) -> BridgeReply:
        """One logical backend turn. The same (turn_id, text) is used for every retry and poll."""
        log.info("submitting turn session=%s turn=%s chars=%d", self.session_id or "(unbound)", turn_id, len(text))
        try:
            result = await self._submit(turn_id, text)
        except BackendConfigError as exc:
            log.error("backend configuration error for turn %s: %s (not retried)", turn_id, exc)
            return BridgeReply(CONFIG_SPEECH, "config_error")
        except BackendUnavailable as exc:
            log.error("backend unavailable for turn %s: %s", turn_id, exc)
            return BridgeReply(UNKNOWN_OUTCOME_SPEECH, "unknown")
        except TurnOutcomeUnknown:
            return BridgeReply(UNKNOWN_OUTCOME_SPEECH, "unknown")
        except BackendRejected as exc:
            log.error("backend rejected turn %s: %s", turn_id, exc)
            return BridgeReply(REJECTED_SPEECH, "rejected")
        if not result.speech:
            return BridgeReply(EMPTY_SPEECH, "empty")
        return BridgeReply(result.speech, "completed")

    async def _submit(self, turn_id: str, text: str) -> c.TurnResult:
        binding = await self.ensure_bound()
        try:
            return await self._client.submit_turn(binding.session_id, turn_id, text)
        except SessionNotFound:
            # Backend data was reset: re-create by the same stable key once, then retry the same turn_id.
            self.binding = await bind_session(self._client, binding.request)
            return await self._client.submit_turn(self.binding.session_id, turn_id, text)
