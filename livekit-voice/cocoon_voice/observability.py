"""Per-turn latency timelines, interruption timing and session summaries.

All times are time.perf_counter() in the worker process (monotonic, never mixed with the
browser's clock). "Playout" here is the worker-side proxy: the agent state switching to
"speaking" when audio frames start flowing to the room. Actual client playback is not
observable from the worker and is reported separately from manual listening checks.
No transcript text or audio is written unless LOG_TRANSCRIPTS is explicitly enabled.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("cocoon_voice.metrics")

STAGES = (
    "vad_speech_end",       # VAD reported end of user speech (after its silence window)
    "stt_final",            # final transcript event received
    "turn_committed",       # on_user_turn_completed entered (end of turn decided)
    "llm_request",          # llm_node started
    "llm_first_text",       # first substantive text chunk from the brain
    "tts_first_audio",      # first synthesized audio frame for substantive text
    "playout_start",        # agent state -> speaking (worker-side publication proxy)
)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    lo, hi = math.floor(rank), math.ceil(rank)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo), 1)


@dataclass
class TurnTimeline:
    turn_id: str
    warm: bool
    marks: dict[str, float] = field(default_factory=dict)
    vad_silence_s: float = 0.0  # VAD end-of-speech fires this long after the last voiced audio
    cue_used: bool = False
    outcome: str = "open"
    error_category: str | None = None
    llm_attempts: int = 0
    outcome_hint: str = "completed"  # generation outcome reported by the streaming guard

    def mark(self, stage: str, at: float | None = None) -> None:
        self.marks.setdefault(stage, at if at is not None else time.perf_counter())

    def durations_ms(self) -> dict[str, float]:
        m = self.marks
        out: dict[str, float] = {}
        speech_end = m.get("vad_speech_end")
        acoustic_end = speech_end - self.vad_silence_s if speech_end is not None else None

        def span(name: str, a: float | None, b: float | None) -> None:
            if a is not None and b is not None and b >= a:
                out[name] = round((b - a) * 1000, 1)

        span("speech_end_to_stt_final", acoustic_end, m.get("stt_final"))
        span("speech_end_to_turn_committed", acoustic_end, m.get("turn_committed"))
        span("llm_request_to_first_text", m.get("llm_request"), m.get("llm_first_text"))
        span("first_text_to_tts_audio", m.get("llm_first_text"), m.get("tts_first_audio"))
        span("speech_end_to_first_audio", acoustic_end, m.get("tts_first_audio"))
        span("speech_end_to_playout", acoustic_end, m.get("playout_start") if not self.cue_used else None)
        return out


@dataclass
class InterruptionSample:
    detected_to_stop_ms: float


class SessionMetrics:
    """Collects timelines for one voice session and writes them as JSONL."""

    def __init__(self, *, session_label: str, config: dict, metrics_dir: Path | None, log_transcripts: bool):
        self.session_id = f"{session_label}-{uuid.uuid4().hex[:8]}"
        self.config = config
        self.turns: list[TurnTimeline] = []
        self.interruptions: list[InterruptionSample] = []
        self.provider_errors: dict[str, int] = {}
        self.reconnects = 0
        self._current: TurnTimeline | None = None
        self._pending_interrupt: float | None = None
        self._stop_candidate_ms: float | None = None  # output stopped during user speech; cause not yet known
        self._dir = metrics_dir
        self._log_transcripts = log_transcripts
        self._file: Path | None = None
        self._queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._writer: threading.Thread | None = None
        if metrics_dir is not None:
            metrics_dir.mkdir(parents=True, exist_ok=True)
            self._file = metrics_dir / f"session-{time.strftime('%Y%m%dT%H%M%S')}-{self.session_id}.jsonl"
            self._writer = threading.Thread(target=self._write_loop, name="metrics-writer", daemon=True)
            self._writer.start()
            self._write({"type": "session_start", "config": config})

    # ------------------------------------------------------------------ turns

    def begin_turn(self, vad_silence_s: float) -> TurnTimeline:
        if self._current is not None and self._current.outcome == "open":
            self._current.outcome = "superseded"
            self._flush(self._current)
        turn = TurnTimeline(turn_id=f"t{len(self.turns) + 1}", warm=len(self.turns) > 0, vad_silence_s=vad_silence_s)
        self.turns.append(turn)
        self._current = turn
        return turn

    @property
    def current(self) -> TurnTimeline | None:
        return self._current

    def end_turn(self, outcome: str) -> None:
        if self._current is not None and self._current.outcome == "open":
            self._current.outcome = outcome
            self._flush(self._current)
            if outcome.startswith("gated:"):
                # routed away before the brain: not an answered turn (the route line says why)
                log.info("turn %s not answered (%s)", self._current.turn_id, outcome)
            else:
                # generation outcome only; playback status is logged separately ("playback ...")
                log.info("turn %s generation=%s timings_ms=%s", self._current.turn_id, outcome,
                         self._current.durations_ms())
        self._current = None

    def _flush(self, turn: TurnTimeline) -> None:
        self._write({"type": "turn", "turn_id": turn.turn_id, "warm": turn.warm, "outcome": turn.outcome,
                     "cue_used": turn.cue_used, "llm_attempts": turn.llm_attempts,
                     "error_category": turn.error_category, "durations_ms": turn.durations_ms()})

    # ------------------------------------------------------------------ interruptions / errors

    def interruption_detected(self) -> None:
        self._pending_interrupt = time.perf_counter()

    def output_stopped(self) -> None:
        # Playout also stops when a reply ends naturally or the SDK pauses it for a possible false
        # interruption; only interruption_confirmed() (reply stored as interrupted) makes it a sample.
        if self._pending_interrupt is not None:
            self._stop_candidate_ms = round((time.perf_counter() - self._pending_interrupt) * 1000, 1)
            self._pending_interrupt = None

    def interruption_confirmed(self) -> None:
        if self._stop_candidate_ms is None:
            return
        ms, self._stop_candidate_ms = self._stop_candidate_ms, None
        self.interruptions.append(InterruptionSample(ms))
        self._write({"type": "interruption", "detected_to_stop_ms": ms})
        log.info("interruption: output stopped %.0f ms after user speech onset", ms)

    def interruption_abandoned(self) -> None:
        self._pending_interrupt = None
        self._stop_candidate_ms = None

    def provider_error(self, category: str) -> None:
        self.provider_errors[category] = self.provider_errors.get(category, 0) + 1
        self._write({"type": "provider_error", "category": category})

    def event(self, kind: str, **fields) -> None:
        self._write({"type": kind, **fields})

    # ------------------------------------------------------------------ summary

    def summary(self) -> dict:
        return summarize([t for t in self.turns if t.outcome in ("completed", "empty", "failed_after_partial")],
                         [i.detected_to_stop_ms for i in self.interruptions], self.provider_errors, self.config)

    def close(self) -> dict:
        if self._current is not None:
            self.end_turn("closed")
        summary = self.summary()
        self._write({"type": "session_summary", **summary})
        if self._writer is not None:
            self._queue.put(None)
            self._writer.join(timeout=2)
        return summary

    def _write(self, record: dict) -> None:
        """Queue a record; a writer thread does the file I/O so the audio event loop never blocks on disk."""
        if self._file is None:
            return
        self._queue.put(json.dumps({"ts": time.time(), "session": self.session_id, **record}, default=str))

    def _write_loop(self) -> None:
        assert self._file is not None
        with self._file.open("a", encoding="utf-8") as fh:
            while True:
                line = self._queue.get()
                if line is None:
                    fh.flush()
                    return
                fh.write(line + "\n")
                if self._queue.empty():
                    fh.flush()


def summarize(turns: list[TurnTimeline], interruptions_ms: list[float], errors: dict[str, int],
              config: dict) -> dict:
    def stats(values: list[float]) -> dict:
        return {"n": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95)}

    out: dict = {"config": config, "provider_errors": errors, "interruption_to_stop_ms": stats(interruptions_ms)}
    for label, subset in (("cold", [t for t in turns if not t.warm]), ("warm", [t for t in turns if t.warm])):
        keys: dict[str, list[float]] = {}
        for t in subset:
            for k, v in t.durations_ms().items():
                keys.setdefault(k, []).append(v)
        out[label] = {k: stats(v) for k, v in sorted(keys.items())}
        out[label]["turns"] = len(subset)
        out[label]["cue_turns"] = sum(1 for t in subset if t.cue_used)
    return out


def summarize_jsonl(paths: list[Path]) -> dict:
    """Aggregate worker metrics files (used by `python -m cocoon_voice.benchmark report`)."""
    turns: list[TurnTimeline] = []
    interruptions: list[float] = []
    errors: dict[str, int] = {}
    config: dict = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["type"] == "session_start":
                config = rec.get("config", config)
            elif rec["type"] == "turn" and rec["outcome"] in ("completed", "empty", "failed_after_partial"):
                t = _Replayed(rec)
                turns.append(t)  # type: ignore[arg-type]
            elif rec["type"] == "interruption":
                interruptions.append(rec["detected_to_stop_ms"])
            elif rec["type"] == "provider_error":
                errors[rec["category"]] = errors.get(rec["category"], 0) + 1
    return summarize(turns, interruptions, errors, config)  # type: ignore[arg-type]


@dataclass
class _Replayed:
    rec: dict

    @property
    def warm(self) -> bool:
        return bool(self.rec["warm"])

    @property
    def cue_used(self) -> bool:
        return bool(self.rec.get("cue_used"))

    def durations_ms(self) -> dict[str, float]:
        return dict(self.rec["durations_ms"])


def as_dict(obj) -> dict:
    return asdict(obj)
