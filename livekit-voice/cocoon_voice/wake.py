"""Wake gate: ARMED -> ACTIVE -> ARMED/CLOSED, independent of LiveKit's agent/user states.

Pure logic with an injected clock so it can be tested without audio. The worker feeds it
finalized user utterances (transcript mode) or acoustic detections (livekit-wakeword or Porcupine) and acts
on the returned Decision. Matching is exact after case/punctuation normalisation: no fuzzy
matching, so "hey cap" or "hey cats" never wake the assistant. Activation is not authentication.
"""

from __future__ import annotations

import enum
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger("cocoon_voice.wake")


class WakeState(str, enum.Enum):
    ARMED = "ARMED"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


Action = Literal["ignore", "ack", "respond", "stop", "sleep"]

_WORD = re.compile(r"[a-z0-9']+")

STOP_COMMANDS = {"stop", "stop talking", "stop please", "please stop", "stop it", "cat stop", "okay stop",
                 "ok stop", "that's enough", "be quiet", "quiet"}
SLEEP_COMMANDS = {"go to sleep", "cat go to sleep", "go to sleep cat", "sleep now", "go to sleep now",
                  "goodbye cat", "bye cat"}
BACKCHANNELS = {"yeah", "yes", "yep", "ok", "okay", "uh huh", "mm hmm", "mhm", "mm", "right", "sure", "got it",
                "alright", "all right", "i see"}


def normalize(text: str) -> str:
    """Lowercase words only: punctuation and repeated spaces removed, apostrophes kept."""
    return " ".join(_WORD.findall(text.lower().replace("’", "'")))


@dataclass(frozen=True)
class Decision:
    action: Action
    text: str | None = None  # text to hand to the brain (wake phrase removed) when action == "respond"
    reason: str = ""
    activated: bool = False  # this utterance moved ARMED -> ACTIVE


class WakeMatcher:
    def __init__(self, phrase: str):
        self.words = normalize(phrase).split()
        if not self.words:
            raise ValueError("wake phrase has no words")
        sep = r"[\s,.!?;:\-\"'“”]+"
        body = sep.join(re.escape(w) for w in self.words)
        # leading, word-bounded phrase followed by end or a separator (so "hey cats" does not match)
        self._re = re.compile(rf"^[\s,.!?;:\-\"'“”]*{body}(?=$|[\s,.!?;:\-\"'“”])", re.IGNORECASE)

    def split(self, text: str) -> tuple[bool, str]:
        """(matched, remainder) where remainder keeps original wording minus the leading phrase."""
        m = self._re.match(text.replace("’", "'"))
        if not m:
            return False, text.strip()
        rest = text[m.end():].lstrip(" \t,.!?;:-\"'“”").strip()
        return True, rest


class WakeGate:
    def __init__(
        self,
        phrase: str,
        *,
        mode: Literal["transcript", "acoustic", "porcupine"] = "transcript",
        active_timeout_s: float = 45.0,
        debounce_s: float = 2.0,
        echo_guard_s: float = 0.6,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.matcher = WakeMatcher(phrase)
        self.mode = "transcript" if mode == "transcript" else "acoustic"  # "porcupine" kept as an alias
        self.active_timeout_s = active_timeout_s
        self.debounce_s = debounce_s
        self.echo_guard_s = echo_guard_s
        self._clock = clock
        self.state = WakeState.ARMED
        self._last_activity = clock()
        self._last_activation: float | None = None
        self._last_utterance: tuple[str, float] | None = None
        self.transitions: list[tuple[WakeState, WakeState, str]] = []

    # ------------------------------------------------------------------ state

    def _set(self, new: WakeState, reason: str) -> None:
        if new != self.state:
            self.transitions.append((self.state, new, reason))
            log.info("wake state %s -> %s (%s)", self.state.value, new.value, reason)
            self.state = new

    def close(self) -> None:
        self._set(WakeState.CLOSED, "session closed")

    def note_activity(self) -> None:
        self._last_activity = self._clock()

    def check_timeout(self, *, busy: bool) -> bool:
        """ACTIVE -> ARMED after inactivity. Never while the agent speaks/thinks or a request is in flight."""
        now = self._clock()
        if self.state != WakeState.ACTIVE:
            return False
        if busy:
            self._last_activity = now
            return False
        if now - self._last_activity >= self.active_timeout_s:
            self._set(WakeState.ARMED, f"inactive {self.active_timeout_s:.0f}s")
            return True
        return False

    def _activate(self, reason: str) -> bool:
        now = self._clock()
        self._last_activity = now
        self._last_activation = now
        was_armed = self.state == WakeState.ARMED
        self._set(WakeState.ACTIVE, reason)
        return was_armed

    def _debounced(self) -> bool:
        return self._last_activation is not None and self._clock() - self._last_activation < self.debounce_s

    # ------------------------------------------------------------------ acoustic (livekit-wakeword / Porcupine)

    def on_acoustic_wake(self) -> bool:
        """Keyword engine fired. Returns True when this is a new activation (not debounced)."""
        if self.state == WakeState.CLOSED:
            return False
        if self._debounced():
            self._last_activity = self._clock()
            return False
        self._activate("acoustic keyword")
        return True

    # ------------------------------------------------------------------ transcripts

    def on_utterance(self, text: str, *, overlapped_agent_speech: bool = False) -> Decision:
        """Decide what to do with one finalized user utterance.

        overlapped_agent_speech: the user spoke while (or just after) the assistant's own audio,
        so the text may be echo of our playback or a backchannel.
        """
        if self.state == WakeState.CLOSED:
            return Decision("ignore", reason="closed")
        now = self._clock()
        norm = normalize(text)
        if not norm:
            return Decision("ignore", reason="empty")
        if self._last_utterance and self._last_utterance[0] == norm and now - self._last_utterance[1] < 1.5:
            return Decision("ignore", reason="duplicate final transcript")
        self._last_utterance = (norm, now)

        woke, rest = self.matcher.split(text)
        rest_norm = normalize(rest)

        if self.state == WakeState.ARMED:
            if not woke:
                return Decision("ignore", reason="armed: no wake phrase")
            if overlapped_agent_speech:
                return Decision("ignore", reason="armed: wake phrase overlapped assistant audio (echo guard)")
            if self.mode == "acoustic":
                # acoustic mode activates only from the keyword engine, never from transcripts
                return Decision("ignore", reason="armed: acoustic mode ignores transcript wake")
            self._activate("wake phrase")
            return self._command_or_request(rest, rest_norm, activated=True)

        # ACTIVE
        self._last_activity = now
        if woke:
            if not rest_norm:
                if self.mode == "acoustic" or self._debounced():
                    return Decision("ignore", reason="wake phrase already handled (debounce/acoustic)")
                self._last_activation = now
                return Decision("ack", reason="wake phrase while active")
            return self._command_or_request(rest, rest_norm, activated=False)
        if overlapped_agent_speech and norm in BACKCHANNELS:
            return Decision("ignore", reason="backchannel during assistant speech")
        return self._command_or_request(text.strip(), norm, activated=False)

    def _command_or_request(self, text: str, norm: str, *, activated: bool) -> Decision:
        if not norm:
            return Decision("ack", reason="wake only", activated=activated)
        if norm in STOP_COMMANDS:
            return Decision("stop", reason="stop command", activated=activated)
        if norm in SLEEP_COMMANDS:
            self._set(WakeState.ARMED, "sleep command")
            return Decision("sleep", reason="sleep command", activated=activated)
        return Decision("respond", text=text, reason="request", activated=activated)
