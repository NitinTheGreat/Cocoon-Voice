"""The llm_node bridge: one submission path, stable turn ids, speech-only output."""

from __future__ import annotations

from livekit.agents import ModelSettings, llm

from cocoon_voice import contract as c
from cocoon_voice.remote_bridge import BackendBridgeLLM, CocoonAgent, parse_job_metadata
from cocoon_voice.backend_client import BackendRejected, SessionNotFound, TurnOutcomeUnknown
from cocoon_voice.bridge import UNKNOWN_OUTCOME_SPEECH, SessionBinding, TurnBridge

REQ = c.SessionCreateRequest(client_session_key="lk:room:op", room_name="room", participant_identity="op",
                             operator_id="op", machine_id="m")


class FakeClient:
    def __init__(self, outcomes=None):
        self.calls: list[tuple[str, str, str]] = []
        self.outcomes = list(outcomes or [])
        self.sessions_created = 0

    async def submit_turn(self, session_id, turn_id, text, source="voice"):
        self.calls.append((session_id, turn_id, text))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return c.TurnResult(session_id=session_id, turn_id=turn_id, status="completed",
                            speech=outcome or f"ok: {text}", actions=[{"type": "incident_logged", "x": 1}])

    async def ensure_session(self, req):
        self.sessions_created += 1
        return c.Session(session_id=f"ses_new{self.sessions_created}", state_version=0,
                         created_at="2026-09-23T10:00:00Z", **req.model_dump())

    async def session_exists(self, session_id):
        return False


def bridge(client) -> TurnBridge:
    return TurnBridge(client, SessionBinding(REQ, "ses_1"))


def ctx(*messages: tuple[str, str]) -> llm.ChatContext:
    chat = llm.ChatContext.empty()
    for role, text in messages:
        chat.add_message(role=role, content=text)
    return chat


async def test_submits_final_user_text_once_with_stable_turn_id():
    client = FakeClient()
    chat = ctx(("assistant", "Hi, I'm Cocoon."), ("user", "  What's my next task? "))
    speech = await bridge(client).reply_for(chat)
    assert speech == "ok: What's my next task?"
    msg_id = chat.items[-1].id
    assert client.calls == [("ses_1", f"lk-{msg_id}", "What's my next task?")]
    # the same utterance always maps to the same turn_id (backend dedupes any repeat)
    await bridge(client).reply_for(chat)
    assert client.calls[1][1] == client.calls[0][1]


async def test_does_not_resubmit_old_turn_when_last_item_is_not_user():
    client = FakeClient()
    chat = ctx(("user", "log an incident"), ("assistant", "What happened?"))
    assert await bridge(client).reply_for(chat) is None
    assert client.calls == []


async def test_unknown_outcome_never_claims_failure_or_cancellation():
    client = FakeClient([TurnOutcomeUnknown("t")])
    speech = await bridge(client).reply_for(ctx(("user", "log an incident: leak")))
    assert speech == UNKNOWN_OUTCOME_SPEECH
    lowered = speech.lower()
    assert "fail" not in lowered and "cancel" not in lowered and "not saved" not in lowered


async def test_rejected_request_gets_short_apology():
    client = FakeClient([BackendRejected("bad", status=409)])
    assert "couldn't process" in (await bridge(client).reply_for(ctx(("user", "x")))).lower()


async def test_missing_session_rebinds_by_stable_key_and_retries_same_turn():
    client = FakeClient([SessionNotFound("gone", status=404), "after rebind"])
    b = bridge(client)
    speech = await b.reply_for(ctx(("user", "next task")))
    assert speech == "after rebind"
    assert b.binding.session_id == "ses_new1"
    assert client.calls[0][1] == client.calls[1][1]


async def test_llm_node_yields_only_speech():
    agent = CocoonAgent(bridge(FakeClient()))
    chunks = [chunk async for chunk in agent.llm_node(ctx(("user", "hello")), [], ModelSettings())]
    assert chunks == ["ok: hello"]  # no action JSON, no classifier output


def test_bridge_llm_is_never_used_for_generation():
    import pytest

    with pytest.raises(RuntimeError):
        BackendBridgeLLM().chat(chat_ctx=llm.ChatContext.empty())


def test_job_metadata_parsing():
    assert parse_job_metadata('{"session_id": "ses_9", "operator_id": "op", "junk": 1}') == {
        "session_id": "ses_9", "operator_id": "op"}
    assert parse_job_metadata("not json") == {}
    assert parse_job_metadata(None) == {}


async def test_lazy_binding_when_backend_was_down_at_job_start():
    client = FakeClient()
    b = TurnBridge(client, None, REQ)
    assert b.session_id is None
    assert await b.reply_for(ctx(("user", "next task"))) == "ok: next task"
    assert b.session_id == "ses_new1" and client.calls[0][0] == "ses_new1"


async def test_backend_down_before_submission_is_reported_as_unknown():
    from cocoon_voice.backend_client import BackendUnavailable

    class DownClient(FakeClient):
        async def ensure_session(self, req):
            raise BackendUnavailable("down")

    speech = await TurnBridge(DownClient(), None, REQ).reply_for(ctx(("user", "log an incident: leak")))
    assert speech == UNKNOWN_OUTCOME_SPEECH
