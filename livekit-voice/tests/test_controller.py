"""VoiceController turn policy: one submission path, idle speech kept out of history, acks, stop/sleep.

The on_user_turn_completed hook is driven directly (the SDK's text-mode run() bypasses it);
the brain guard is exercised through a real AgentSession with a scripted streaming LLM.
"""

from __future__ import annotations

import pytest
from livekit.agents import AgentSession, StopResponse, llm

from cocoon_voice import speech_policy as sp
from cocoon_voice.agent import CatAgent, VoiceController
from cocoon_voice.wake import WakeState

from .fakes import FakeLLM, FakePhraseCache, FakeSession, Step, metrics, settings


def controller(**overrides) -> tuple[VoiceController, FakeSession, FakePhraseCache]:
    phrases = FakePhraseCache()
    c = VoiceController(settings(**overrides), phrases=phrases, metrics=metrics())  # type: ignore[arg-type]
    session = FakeSession()
    c.session = session  # type: ignore[assignment]
    return c, session, phrases


def msg(text: str) -> llm.ChatMessage:
    return llm.ChatMessage(role="user", content=[text])


async def test_idle_speech_is_dropped_before_history_and_brain():
    c, session, _ = controller()
    m = msg("did you see the game last night")
    with pytest.raises(StopResponse):  # SDK returns before appending the message to chat history
        await c.on_user_turn(m)
    assert m.text_content == "did you see the game last night" and session.said == []


async def test_wake_only_plays_cached_ack_and_keeps_it_out_of_history():
    c, session, phrases = controller()
    with pytest.raises(StopResponse):
        await c.on_user_turn(msg("Hey Cat"))
    assert [s["text"] for s in session.said] == [sp.WAKE_ACK]
    assert session.said[0]["add_to_chat_ctx"] is False and "audio" in session.said[0]  # cached audio used
    assert phrases.requested == [sp.WAKE_ACK] and c.gate.state == WakeState.ACTIVE


async def test_wake_plus_question_is_answered_once_without_an_ack():
    c, session, _ = controller()
    m = msg("Hey Cat, can you hear me?")
    await c.on_user_turn(m)  # no StopResponse: the SDK will call llm_node once for this message
    assert m.text_content == "can you hear me?"
    assert session.said == []


async def test_stop_interrupts_and_invalidates_the_current_generation():
    c, session, _ = controller()
    c.gate.on_acoustic_wake()  # any activation
    epoch_before = c.epochs.current
    with pytest.raises(StopResponse):
        await c.on_user_turn(msg("stop"))
    assert session.interrupts == 1 and c.epochs.current == epoch_before + 1 and session.said == []


async def test_sleep_stops_output_says_short_ack_and_rearms():
    c, session, _ = controller()
    c.gate.on_acoustic_wake()
    with pytest.raises(StopResponse):
        await c.on_user_turn(msg("go to sleep"))
    assert session.interrupts == 1 and [s["text"] for s in session.said] == [sp.SLEEP_ACK]
    assert c.gate.state == WakeState.ARMED


async def test_greeting_once_non_interruptible_and_without_wake_phrase():
    c, session, _ = controller()
    await c.on_enter()
    await c.on_enter()  # ordinary reconnect / agent re-entry
    greetings = [s for s in session.said if s["text"] == c.s.voice_greeting]
    assert len(greetings) == 1 and greetings[0]["allow_interruptions"] is False
    assert "hey cat" not in c.s.voice_greeting.lower()


async def test_two_sessions_are_isolated():
    a, _, _ = controller()
    b, _, _ = controller()
    await a.on_user_turn(msg("Hey Cat, what's up"))
    assert a.gate.state == WakeState.ACTIVE and b.gate.state == WakeState.ARMED
    a.epochs.next()
    assert b.epochs.current == 0
    with pytest.raises(StopResponse):
        await b.on_user_turn(msg("what's up"))


async def test_brain_is_never_called_while_armed_real_session():
    fake = FakeLLM([Step("Sure, "), Step("track tension matters.")])
    c, _, _ = controller(THINKING_CUE_ENABLED="false")
    c.greeted = True  # text-only test session has no audio output for the greeting
    async with AgentSession(llm=fake) as session:
        c.session = session
        await session.start(CatAgent(c))
        await session.run(user_input="what's the weather like")
        assert fake.calls == 0  # armed: llm_node refuses without the wake phrase
        result = await session.run(user_input="Hey Cat what about the tracks")
        assert fake.calls == 1
        result.expect.contains_message(role="assistant")
        assistant = [e.item for e in result.events if e.type == "message" and e.item.role == "assistant"]
        assert assistant and assistant[-1].text_content == "Sure, track tension matters."


async def test_context_is_bounded():
    c, _, _ = controller(MAX_CONTEXT_TURNS="2")
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="system", content=sp.INSTRUCTIONS)
    for i in range(10):
        ctx.add_message(role="user", content=f"q{i}")
        ctx.add_message(role="assistant", content=f"a{i}")
    bounded = sp.bounded_context(ctx, c.s.max_context_turns)
    texts = [i.text_content for i in bounded.items]
    assert texts[0] == sp.INSTRUCTIONS and texts[-1] == "a9" and len(texts) <= 6
