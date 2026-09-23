"""Generate natural-sounding operator fixtures with Cartesia in a voice different from the agent's.

    python scripts/make_natural_fixtures.py

SAPI renders "Mm-hmm" as a slow, articulated phrase (AssemblyAI heard "Millimeter home"), which is not a
realistic backchannel. These clips are closer to human delivery but still synthetic: they do not replace a
real microphone test. Output: tests/fixtures/audio/natural_<name>.wav (16 kHz mono, git-ignored), used by
`scripts/live_probe.py --fixtures natural`. Uses the normal Cartesia TTS API (small, billed).
"""

from __future__ import annotations

import asyncio
import sys
import wave
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cocoon_voice.cartesia_auth import API_BASE, TOKEN_API_VERSION, mint_access_token, sanitize  # noqa: E402
from cocoon_voice.config import get_settings  # noqa: E402

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "audio"
RATE = 16000
LEAD_S, TAIL_S = 0.3, 0.6  # silence around each clip, like the SAPI fixtures

PHRASES = {
    "mm_hmm": "Mm-hmm.",
    "okay": "Okay.",
    "yeah": "Yeah.",
    "right": "Right.",
    "stop": "Stop.",
    "wait": "Wait.",
    "hey_cat_long_request": "Hey Cat, explain step by step how to do a full pre-start walkaround inspection "
                            "on an excavator, in detail.",
    "long_request": "Explain step by step how to do a full pre-start walkaround inspection on an excavator, "
                    "in detail.",
    "okay_but_correction": "Okay, but I meant the other machine, the wheel loader.",
    "question_part1": "Can you tell me",
    "question_part2": "what I should do next?",
}


async def pick_voice(client: httpx.AsyncClient, api_key: str, agent_voice: str) -> str:
    r = await client.get(f"{API_BASE}/voices", params={"limit": 50},
                         headers={"Authorization": f"Bearer {api_key}", "Cartesia-Version": TOKEN_API_VERSION})
    r.raise_for_status()
    body = r.json()
    voices = body.get("data", body) if isinstance(body, dict) else body
    for v in voices:  # a male English voice contrasts with the agent's female voice
        if v.get("id") != agent_voice and v.get("language") == "en" and v.get("gender") in ("masculine", "male"):
            print(f"operator voice: {v.get('name')} ({v.get('id')})")
            return v["id"]
    raise SystemExit("no suitable second English voice found")


async def main() -> None:
    s = get_settings()
    if s.cartesia_api_key is None:
        raise SystemExit("CARTESIA_API_KEY is not set")
    key = s.cartesia_api_key.get_secret_value()
    FIX.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30) as client:
        voice = await pick_voice(client, key, s.cartesia_voice_id)
        token = await mint_access_token(key, ttl_s=300, client=client)
        for name, text in PHRASES.items():
            r = await client.post(
                f"{API_BASE}/tts/bytes",
                headers={"Authorization": f"Bearer {token.token}", "Cartesia-Version": TOKEN_API_VERSION},
                json={"model_id": s.cartesia_model, "transcript": text, "voice": {"mode": "id", "id": voice},
                      "language": "en",
                      "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": RATE}})
            if r.status_code != 200:
                raise SystemExit(f"{name}: HTTP {r.status_code} {sanitize(r.text)}")
            pcm = np.frombuffer(r.content, dtype=np.int16)
            pad = lambda secs: np.zeros(int(secs * RATE), dtype=np.int16)  # noqa: E731
            audio = np.concatenate([pad(LEAD_S), pcm, pad(TAIL_S)])
            with wave.open(str(FIX / f"natural_{name}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(audio.tobytes())
            print(f"natural_{name}.wav {len(pcm) / RATE:.2f}s speech")


if __name__ == "__main__":
    asyncio.run(main())
