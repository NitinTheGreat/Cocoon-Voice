"""Pure cross-event invariant checks for proposed event streams (CONTRACT checks only).

These examine a list of already-parsed envelopes. They prove nothing about database uniqueness, runtime
authorisation, cancellation, recovery or network flushing; I08 must test those against the real server.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .announcements import AnnouncementEnvelope
from .turns import TERMINAL_TURN_EVENTS, TurnStreamEvent


def turn_stream_violations(events: list[dict[str, Any]], *, partial: bool = False) -> list[str]:
    """Violations of the cocoon.turn-stream.v1 sequence rules. `partial` = a replay that starts after a cursor."""
    problems: list[str] = []
    if not events:
        return ["a stream has at least one event"]
    for event in events:
        try:
            TurnStreamEvent.model_validate(event)
        except ValidationError as exc:
            problems.append(f"invalid envelope {event.get('event_id')}: {exc.errors()[0]['msg']}")
    if problems:
        return problems

    identity = {(e["session_id"], e["turn_id"], e["response_id"]) for e in events}
    if len(identity) != 1:
        problems.append("fixed session/turn/response identity")
    sequences = [e["sequence"] for e in events]
    if any(b <= a for a, b in zip(sequences, sequences[1:])):
        problems.append("strictly increasing sequence")
    if not partial and (events[0]["type"] != "turn.accepted" or sequences[0] != 1):
        problems.append("a full transcript starts with turn.accepted at sequence 1")

    terminals = [i for i, e in enumerate(events) if e["type"] in TERMINAL_TURN_EVENTS]
    if len(terminals) > 1:
        problems.append("exactly one terminal event")
    if terminals and terminals[0] != len(events) - 1:
        problems.append("nothing after the terminal event")
    if not partial and not terminals:
        problems.append("exactly one terminal event")

    deltas = "".join(e["data"]["text"] for e in events if e["type"] == "speech.delta")
    if terminals and not partial:
        terminal = events[terminals[0]]
        if terminal["type"] == "turn.completed" and terminal["data"]["speech"] != deltas:
            problems.append("completed speech equals deltas")
        if terminal["type"] == "turn.cancelled" and terminal["data"]["generated_speech"] != deltas:
            problems.append("cancelled generated_speech equals deltas")
    return problems


def announcement_violations(events: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    for event in events:
        try:
            AnnouncementEnvelope.model_validate(event)
        except ValidationError as exc:
            problems.append(f"invalid envelope {event.get('event_id')}: {exc.errors()[0]['msg']}")
    if problems:
        return problems
    if len({e["session_id"] for e in events}) > 1:
        problems.append("one session per announcement stream")
    sequences = [e["sequence"] for e in events]
    if any(b <= a for a, b in zip(sequences, sequences[1:])):
        problems.append("strictly increasing sequence")
    ids = [e["event_id"] for e in events]
    if len(ids) != len(set(ids)):
        problems.append("unique event_id per session")
    return problems
