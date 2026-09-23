"""Small live smoke checks through the worker's own provider factories (a few paid calls).

    python -m cocoon_voice.smoke            # vertex, cartesia, assemblyai (each skipped if its key is missing)
    python -m cocoon_voice.smoke --only vertex

Cartesia writes recordings/smoke-cartesia-pronunciation.wav so a human can check how CAT, unit IDs
and numbers sound. AssemblyAI transcribes that synthetic audio (labelled synthetic, not a human test).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import get_settings
from .live_checks import (close_http_session, PRONUNCIATION_TEXT, brain_turn, make_brain, make_stt, make_tts, stt_realtime,
                          tts_stream_turn, word_error_rate, write_wav)


async def run(only: str | None) -> int:
    s = get_settings()
    failed = 0
    tts_audio = None

    if only in (None, "vertex"):
        try:
            brain = make_brain(s)
            t = await brain_turn(brain, "Say hello to a CAT excavator operator in one short sentence.")
            ok = t.chars > 0
            print(f"[{'PASS' if ok else 'FAIL'}] vertex    model={s.vertex_model} ttft={t.ttft_ms}ms "
                  f"first_sentence={t.first_sentence_ms}ms total={t.total_ms}ms chars={t.chars} reply={t.text!r}")
            failed += not ok
        except Exception as exc:
            print(f"[FAIL] vertex    {type(exc).__name__}: {str(exc)[:160]} (run python -m cocoon_voice.doctor)")
            failed += 1

    if only in (None, "cartesia"):
        if s.cartesia_api_key is None:
            print("[SKIP] cartesia  CARTESIA_API_KEY is not set")
        else:
            try:
                tts = await make_tts(s)
                t = await tts_stream_turn(tts, [PRONUNCIATION_TEXT])
                path = write_wav(s.record_audio_dir / "smoke-cartesia-pronunciation.wav", t.pcm, t.sample_rate)
                tts_audio = (t.pcm, t.sample_rate)
                print(f"[PASS] cartesia  model={s.cartesia_model} voice={s.cartesia_voice_id} "
                      f"first_audio={t.first_audio_ms}ms total={t.total_ms}ms audio={t.audio_s:.1f}s -> {path}")
            except Exception as exc:
                print(f"[FAIL] cartesia  {type(exc).__name__}: {str(exc)[:160]}")
                failed += 1

    if only in (None, "assemblyai"):
        if s.assemblyai_api_key is None:
            print("[SKIP] assemblyai ASSEMBLYAI_API_KEY is not set")
        elif tts_audio is None:
            print("[SKIP] assemblyai needs the Cartesia smoke audio as synthetic input (set CARTESIA_API_KEY)")
        else:
            try:
                t = await stt_realtime(make_stt(s), *tts_audio)
                wer = word_error_rate(PRONUNCIATION_TEXT, t.transcript)
                print(f"[PASS] assemblyai model={s.assemblyai_model} final_after_audio_end="
                      f"{t.final_after_audio_end_ms}ms WER={wer} (synthetic speech) transcript={t.transcript!r}")
            except Exception as exc:
                print(f"[FAIL] assemblyai {type(exc).__name__}: {str(exc)[:160]}")
                failed += 1
    await close_http_session()
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["vertex", "cartesia", "assemblyai"])
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args.only)))


if __name__ == "__main__":
    main()
