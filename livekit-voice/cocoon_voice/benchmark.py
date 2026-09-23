"""Bounded latency benchmark and report.

    python -m cocoon_voice.benchmark run [--turns 10] [--stt] [--noise machinery --snr 10]
    python -m cocoon_voice.benchmark report [metrics/*.jsonl ...]

`run` drives the worker's provider factories outside a LiveKit room (pipeline-only, synthetic):
brain streaming -> first speakable sentence -> Cartesia streaming first audio; with --stt the question
is first synthesized by Cartesia and streamed in real time through AssemblyAI, giving a synthetic
speech-end -> final transcript -> first answer audio chain. Stages without keys are reported as skipped.
Hard limits: at most 30 turns and 10 minutes. Results are labelled synthetic; they do not replace human
microphone tests. `report` aggregates the per-turn JSONL written by live worker sessions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from pathlib import Path

from .config import get_settings
from .live_checks import (close_http_session, OPERATOR_PROMPTS, brain_turn, make_brain, make_stt, make_tts, stt_realtime,
                          tts_stream_turn, word_error_rate)
from .observability import percentile, summarize_jsonl

MAX_TURNS = 30
MAX_SECONDS = 600


def _stats(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    return {"n": len(vals), "p50": percentile(vals, 50), "p95": percentile(vals, 95),
            "min": min(vals) if vals else None, "max": max(vals) if vals else None}


async def run(turns: int, use_stt: bool, noise_kind: str | None, snr_db: float, prewarm: bool = True) -> dict:
    s = get_settings()
    turns = min(turns, MAX_TURNS)
    started = time.monotonic()
    brain = make_brain(s)
    if prewarm:  # what the worker does: AgentSession construction prewarms the LLM (auth + connection)
        brain.prewarm()
        await asyncio.sleep(3)
    tts = await make_tts(s) if s.cartesia_api_key else None
    stt_impl = make_stt(s) if (use_stt and s.assemblyai_api_key and tts) else None
    skipped = []
    if tts is None:
        skipped.append("cartesia (CARTESIA_API_KEY not set)")
    if use_stt and stt_impl is None:
        skipped.append("assemblyai (needs ASSEMBLYAI_API_KEY and CARTESIA_API_KEY for synthetic questions)")
    rows: list[dict] = []
    usage = {"llm_requests": 0, "llm_output_chars": 0, "tts_chars": 0, "tts_audio_s": 0.0, "stt_audio_s": 0.0}
    noise_pcm = None
    for i in range(turns):
        if time.monotonic() - started > MAX_SECONDS:
            print(f"stopping: {MAX_SECONDS}s limit reached", file=sys.stderr)
            break
        prompt = OPERATOR_PROMPTS[i % len(OPERATOR_PROMPTS)]
        row: dict = {"turn": i + 1, "warm": i > 0, "prompt_chars": len(prompt)}
        speech_end_to_text_ms = 0.0
        if stt_impl is not None and tts is not None:
            q = await tts_stream_turn(tts, [prompt])
            usage["tts_chars"] += len(prompt)
            if noise_kind and noise_pcm is None:
                from .noise_fixtures import generate, scale_to_snr

                noise_pcm = scale_to_snr(q.pcm, generate(noise_kind, seconds=max(1, int(q.audio_s) + 3),
                                                         sample_rate=q.sample_rate), snr_db)
            st = await stt_realtime(stt_impl, q.pcm, q.sample_rate, noise=noise_pcm)
            usage["stt_audio_s"] += st.audio_s
            row.update(stt_final_after_speech_end_ms=st.final_after_audio_end_ms,
                       stt_wer=word_error_rate(prompt, st.transcript))
            speech_end_to_text_ms = st.final_after_audio_end_ms or 0.0
            prompt = st.transcript or prompt
        b = await brain_turn(brain, prompt)
        usage["llm_requests"] += 1
        usage["llm_output_chars"] += b.chars
        row.update(llm_ttft_ms=b.ttft_ms, llm_first_sentence_ms=b.first_sentence_ms, llm_total_ms=b.total_ms,
                   reply_chars=b.chars)
        if tts is not None and b.text:
            # the worker feeds TTS as soon as the first sentence exists; approximate with sentence chunks
            from livekit.agents import tokenize

            sentences = tokenize.blingfire.SentenceTokenizer().tokenize(b.text) or [b.text]
            a = await tts_stream_turn(tts, [x + " " for x in sentences])
            usage["tts_chars"] += len(b.text)
            usage["tts_audio_s"] += a.audio_s
            row.update(tts_first_audio_ms=a.first_audio_ms)
            if b.first_sentence_ms is not None and a.first_audio_ms is not None:
                row["text_in_to_first_audio_ms"] = round(b.first_sentence_ms + a.first_audio_ms, 1)
                if stt_impl is not None:
                    row["speech_end_to_first_audio_ms"] = round(speech_end_to_text_ms + b.first_sentence_ms
                                                                + a.first_audio_ms, 1)
        rows.append(row)
        print(json.dumps(row))
    report: dict = {
        "label": "SYNTHETIC pipeline benchmark (no LiveKit room, no human speech, no client playback)",
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "vertex_location": s.google_cloud_location},
        "models": {"vertex": s.vertex_model, "thinking": s.vertex_thinking, "cartesia": s.cartesia_model,
                   "voice": s.cartesia_voice_id, "assemblyai": s.assemblyai_model if stt_impl else None},
        "noise": {"kind": noise_kind, "snr_db": snr_db} if noise_kind and stt_impl else None,
        "prewarm": prewarm, "skipped": skipped, "usage": usage, "turns": len(rows),
    }
    keys = sorted({k for r in rows for k in r if k.endswith("_ms") or k.endswith("_wer")})
    for label, subset in (("cold", [r for r in rows if not r["warm"]]), ("warm", [r for r in rows if r["warm"]])):
        report[label] = {k: _stats([r.get(k) for r in subset]) for k in keys}
    s.metrics_dir.mkdir(parents=True, exist_ok=True)
    out = s.metrics_dir / f"benchmark-{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps({"report": report, "rows": rows}, indent=2), encoding="utf-8")
    report["written_to"] = str(out)
    await close_http_session()
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--turns", type=int, default=10, help=f"max {MAX_TURNS}")
    r.add_argument("--stt", action="store_true", help="also time AssemblyAI on Cartesia-synthesized questions")
    r.add_argument("--noise", choices=["machinery", "fan", "impacts", "echo_babble"], default=None)
    r.add_argument("--snr", type=float, default=10.0, help="speech-to-noise ratio in dB for --noise")
    r.add_argument("--no-prewarm", action="store_true", help="measure a truly cold first request")
    rep = sub.add_parser("report")
    rep.add_argument("files", nargs="*", type=Path)
    args = ap.parse_args()
    if args.cmd == "run":
        report = asyncio.run(run(args.turns, args.stt, args.noise, args.snr, prewarm=not args.no_prewarm))
        print(json.dumps(report, indent=2))
    else:
        files = args.files or sorted(get_settings().metrics_dir.glob("session-*.jsonl"))
        if not files:
            sys.exit("no worker metrics files found (run a live session first)")
        print(json.dumps({"label": "LIVE worker sessions (worker-side timings; client playback not observable)",
                          "files": [str(f) for f in files], **summarize_jsonl(files)}, indent=2))


if __name__ == "__main__":
    main()
