"""Wake gate state machine: exact 'Hey Cat' matching, commands, debounce, echo, timeout."""

from __future__ import annotations

import pytest

from cocoon_voice.wake import WakeGate, WakeMatcher, WakeState, normalize

from .conftest import FakeClock


def make(mode: str = "transcript", **kw) -> tuple[WakeGate, FakeClock]:
    clock = FakeClock()
    return WakeGate("Hey Cat", mode=mode, clock=clock, active_timeout_s=45, debounce_s=2, **kw), clock


def wake(gate: WakeGate, clock: FakeClock) -> None:
    assert gate.on_utterance("Hey Cat").action == "ack"
    clock.now += 3  # past debounce and duplicate window


def test_normalize_case_punctuation():
    assert normalize("  Hey, CAT!  What's up? ") == "hey cat what's up"


def test_idle_background_speech_is_ignored_while_armed():
    gate, _ = make()
    for text in ["what's the weather", "cat food is on the table", "the operator said stop", "go to sleep"]:
        d = gate.on_utterance(text)
        assert d.action == "ignore" and gate.state == WakeState.ARMED


def test_wake_only_acknowledges_once_and_activates():
    gate, _ = make()
    d = gate.on_utterance("Hey Cat.")
    assert (d.action, d.activated, gate.state) == ("ack", True, WakeState.ACTIVE)


@pytest.mark.parametrize("text,question", [
    ("Hey Cat, can you hear me?", "can you hear me?"),
    ("hey cat what should I check before starting", "what should I check before starting"),
    ("HEY CAT! How's the hydraulic pressure?", "How's the hydraulic pressure?"),
    ("Hey, Cat - tell me about track tension.", "tell me about track tension."),
    ("“Hey Cat” what's next", "what's next"),
])
def test_wake_plus_question_keeps_the_question_once(text, question):
    gate, _ = make()
    d = gate.on_utterance(text)
    assert d.action == "respond" and d.text == question and d.activated


@pytest.mark.parametrize("text", [
    "hey cap", "hey cats", "hey catherine what time is it", "okay cat", "they cat", "hey", "cat",
    "hey there cat", "a hey cat", "I told him hey cat yesterday", "hay cat", "hey kat",
])
def test_similar_but_wrong_phrases_do_not_wake(text):
    gate, _ = make()
    assert gate.on_utterance(text).action == "ignore"
    assert gate.state == WakeState.ARMED


def test_active_follow_ups_do_not_need_the_wake_phrase():
    gate, clock = make()
    wake(gate, clock)
    d = gate.on_utterance("what's the recommended track tension")
    assert d.action == "respond" and d.text == "what's the recommended track tension"
    assert gate.on_utterance("yes").action == "respond"  # short answer after the assistant finished


def test_repeated_wake_is_debounced_and_duplicate_finals_ignored():
    gate, clock = make()
    assert gate.on_utterance("Hey Cat").action == "ack"
    clock.now += 0.5
    assert gate.on_utterance("hey cat.").reason == "duplicate final transcript"
    clock.now += 1.2  # 1.7 s: past duplicate window, inside 2 s debounce
    assert gate.on_utterance("Hey Cat!").action == "ignore"
    clock.now += 5
    assert gate.on_utterance("Hey Cat").action == "ack"  # a later deliberate re-wake is acknowledged


def test_stop_and_sleep_commands_are_exact_and_standalone():
    gate, clock = make()
    wake(gate, clock)
    assert gate.on_utterance("Stop.").action == "stop"
    clock.now += 2
    assert gate.on_utterance("what does stop mean?").action == "respond"
    clock.now += 2
    assert gate.on_utterance("what does 'go to sleep' mean").action == "respond"
    clock.now += 2
    d = gate.on_utterance("Go to sleep.")
    assert d.action == "sleep" and gate.state == WakeState.ARMED
    clock.now += 2
    assert gate.on_utterance("tell me a joke").action == "ignore"


def test_wake_plus_command_while_armed():
    gate, _ = make()
    d = gate.on_utterance("Hey Cat, stop")
    assert d.action == "stop" and gate.state == WakeState.ACTIVE


def test_idle_timeout_measured_from_activity_and_paused_while_busy():
    gate, clock = make()
    wake(gate, clock)
    gate.note_activity()  # last relevant speech/activity
    clock.now += 44
    assert gate.check_timeout(busy=False) is False
    clock.now += 100  # long reply / request in flight
    assert gate.check_timeout(busy=True) is False and gate.state == WakeState.ACTIVE
    clock.now += 44
    assert gate.check_timeout(busy=False) is False
    clock.now += 2
    assert gate.check_timeout(busy=False) is True and gate.state == WakeState.ARMED


def test_own_playback_echo_cannot_wake_the_armed_gate():
    gate, _ = make()
    d = gate.on_utterance("Hey Cat", overlapped_agent_speech=True)
    assert d.action == "ignore" and "echo" in d.reason and gate.state == WakeState.ARMED


def test_backchannels_during_assistant_speech_do_not_trigger_replies():
    gate, clock = make()
    wake(gate, clock)
    assert gate.on_utterance("yeah", overlapped_agent_speech=True).action == "ignore"
    clock.now += 2
    assert gate.on_utterance("no, the left track", overlapped_agent_speech=True).action == "respond"


def test_porcupine_mode_activates_only_acoustically():
    gate, clock = make("porcupine")
    assert gate.on_utterance("Hey Cat what time is it").action == "ignore"
    assert gate.on_acoustic_wake() is True and gate.state == WakeState.ACTIVE
    clock.now += 2
    d = gate.on_utterance("Hey Cat what time is it")
    assert d.action == "respond" and d.text == "what time is it"
    clock.now += 2
    assert gate.on_utterance("hey cat").action == "ignore"  # ack is decided by the acoustic path
    assert gate.on_acoustic_wake() is True  # 4 s after the first detection: debounce elapsed
    clock.now += 0.5
    assert gate.on_acoustic_wake() is False  # debounced


def test_closed_gate_ignores_everything():
    gate, _ = make()
    gate.close()
    assert gate.on_utterance("Hey Cat").action == "ignore"
    assert gate.on_acoustic_wake() is False


def test_matcher_rejects_empty_phrase():
    with pytest.raises(ValueError):
        WakeMatcher("  !! ")
