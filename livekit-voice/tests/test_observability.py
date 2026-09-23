"""Per-turn timing math, cold/warm p50/p95 summaries and the JSONL record format (no transcripts)."""

from __future__ import annotations

import json

from cocoon_voice.observability import SessionMetrics, TurnTimeline, percentile, summarize_jsonl


def test_percentiles():
    assert percentile([], 50) is None
    assert percentile([100.0], 95) == 100.0
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert abs(percentile(list(range(1, 101)), 95) - 95.05) <= 0.06  # linear interpolation, 1-decimal rounding


def test_timeline_measures_from_acoustic_speech_end_and_excludes_cue_turns_from_playout():
    t = TurnTimeline(turn_id="t1", warm=True, vad_silence_s=0.45)
    base = 100.0
    t.marks = {"vad_speech_end": base + 0.45, "stt_final": base + 0.30, "turn_committed": base + 0.50,
               "llm_request": base + 0.52, "llm_first_text": base + 1.20, "tts_first_audio": base + 1.35,
               "playout_start": base + 1.40}
    d = t.durations_ms()
    assert d["speech_end_to_first_audio"] == 1350.0 and d["speech_end_to_playout"] == 1400.0
    assert d["llm_request_to_first_text"] == 680.0 and d["speech_end_to_stt_final"] == 300.0
    t.cue_used = True  # "One moment." is not the substantive answer
    assert "speech_end_to_playout" not in t.durations_ms()


def test_session_metrics_jsonl_and_summary(tmp_path):
    m = SessionMetrics(session_label="room-a", config={"profile": "development"}, metrics_dir=tmp_path,
                       log_transcripts=False)
    for i in range(3):
        turn = m.begin_turn(0.45)
        turn.mark("vad_speech_end", 10.0 + i)
        turn.mark("tts_first_audio", 11.0 + i + 0.1 * i)
        m.end_turn("completed")
    ignored = m.begin_turn(0.45)
    ignored.mark("vad_speech_end", 50.0)
    m.end_turn("gated:ignore")
    m.interruption_detected()
    m.output_stopped()
    m.interruption_confirmed()
    m.interruption_detected()  # user spoke as the reply ended naturally: not an interruption sample
    m.output_stopped()
    m.interruption_abandoned()
    m.interruption_confirmed()
    summary = m.close()
    assert summary["cold"]["turns"] == 1 and summary["warm"]["turns"] == 2  # gated turns are not latency samples
    assert summary["interruption_to_stop_ms"]["n"] == 1
    files = list(tmp_path.glob("session-*.jsonl"))
    assert len(files) == 1
    records = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert {r["type"] for r in records} >= {"session_start", "turn", "interruption", "session_summary"}
    assert not any("transcript" in json.dumps(r).lower() for r in records)
    replay = summarize_jsonl(files)
    assert replay["warm"]["speech_end_to_first_audio"]["n"] == 2
