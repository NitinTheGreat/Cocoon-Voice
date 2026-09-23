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


class _Ev:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def test_false_interruption_event_records_only_and_never_generates():
    c, session, _ = controller()
    c.gate.on_acoustic_wake()
    c.interruption_pending = True
    c._on_false_interruption(_Ev(resumed=True))
    c._on_false_interruption(_Ev(resumed=False))
    assert session.said == [] and c.interruption_pending is False
    assert c.false_interruptions == {"resumed": 1, "not_resumed": 1}


async def test_missed_speech_asks_to_repeat_once_but_not_for_blips_or_after_a_turn():
    import asyncio

    c, session, _ = controller()
    c.gate.on_acoustic_wake()  # ACTIVE
    c._on_transcription_timeout(_Ev(speech_duration=0.3))  # short blip: treated as noise
    await asyncio.sleep(0.4)
    assert session.said == []
    c._on_transcription_timeout(_Ev(speech_duration=1.5))
    await asyncio.sleep(0.4)
    assert [s["text"] for s in session.said] == [sp.CLARIFY]
    c._on_transcription_timeout(_Ev(speech_duration=1.5))
    await c.on_user_turn(msg("what about the tracks"))  # a (late) transcript arrived: no stale prompt
    await asyncio.sleep(0.4)
    assert [s["text"] for s in session.said] == [sp.CLARIFY]


async def test_wake_window_never_expires_during_a_pending_interruption():
    c, _, _ = controller()
    c.gate.on_acoustic_wake()
    c.interruption_pending = True
    assert c.is_busy()
    c.interruption_pending = False
    assert not c.is_busy()


async def test_krisp_credential_rotation_runs_off_the_event_loop():
    import asyncio
    import threading
    import time

    from cocoon_voice.providers import offload_krisp_credential_updates

    calls = []

    class Proc:
        def _on_credentials_updated(self, *, token, url):
            time.sleep(0.3)  # the measured native call blocked the loop for 105-339 ms
            calls.append((token, url, threading.current_thread() is threading.main_thread()))

    proc = Proc()
    offload_krisp_credential_updates(proc)
    t0 = time.perf_counter()
    proc._on_credentials_updated(token="jwt", url="wss://x")
    assert time.perf_counter() - t0 < 0.05  # returns immediately
    await asyncio.sleep(0.5)
    assert calls == [("jwt", "wss://x", False)]


async def test_interruption_mode_is_sampled_per_turn_and_a_downgrade_is_reported(caplog):
    c, session, _ = controller()
    c.gate.on_acoustic_wake()

    class Activity:
        _interruption_detection_enabled = True

    session._activity = Activity()
    await c.on_user_turn(msg("what about the tracks"))
    Activity._interruption_detection_enabled = False  # SDK _fallback_to_vad_interruption
    with caplog.at_level("WARNING"):
        await c.on_user_turn(msg("and the boom"))
    assert c.interruption_by_turn == {"adaptive(active)": 1, "vad(configured=adaptive)": 1}
    assert "downgraded" in caplog.text


async def test_overlap_verdicts_are_counted_without_generating():
    c, session, _ = controller()
    c._on_overlap_verdict(_Ev(agent_ended=False, is_interruption=False, probability=0.1, detection_delay=0.3))
    c._on_overlap_verdict(_Ev(agent_ended=False, is_interruption=True, probability=0.9, detection_delay=0.4))
    c._on_overlap_verdict(_Ev(agent_ended=True, is_interruption=False, probability=0.0, detection_delay=0.0))
    assert c.overlap_verdicts == {"interruption": 1, "backchannel": 1, "agent_ended": 1} and session.said == []


async def test_payment_required_from_tts_is_reported_plainly(caplog):
    c, _, _ = controller()

    class ApiErr(Exception):
        status_code = 402

    class TTSError(Exception):
        recoverable = False
        error = ApiErr()

    class CartesiaTTS:
        pass

    with caplog.at_level("ERROR"):
        c._on_error(_Ev(error=TTSError(), source=CartesiaTTS()))
    assert "HTTP 402 payment_required" in caplog.text and "credits" in caplog.text
