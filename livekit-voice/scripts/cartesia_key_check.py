"""Check a Cartesia API key typed at a hidden prompt (bypasses .env and the process environment).

    python scripts/cartesia_key_check.py

The key is read with getpass (not echoed, not stored, not logged). Results report each path
separately: raw key over HTTP, raw key over the websocket, access-token minting, and synthesis
with the minted token over HTTP and websocket. Only status codes, sanitized provider messages and
request IDs are printed. Each check synthesizes one short phrase (negligible credits).
"""

from __future__ import annotations

import asyncio
import getpass
import json
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cocoon_voice.cartesia_auth import CartesiaAuthError, mint_access_token, sanitize  # noqa: E402

VOICE = "f786b574-daa5-4673-aa0c-cbe3e8534c02"  # Cartesia/LiveKit documented public voice
BODY = {"model_id": "sonic-3", "transcript": "Hi there.", "voice": {"mode": "id", "id": VOICE}, "language": "en",
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000}}


async def http_synth(http: aiohttp.ClientSession, credential: str) -> str:
    async with http.post("https://api.cartesia.ai/tts/bytes", json=BODY,
                         headers={"X-API-Key": credential, "Cartesia-Version": "2025-04-16"}) as r:
        data = await r.read()
        if r.status == 200:
            return f"200 OK ({len(data)} bytes)"
        return f"{r.status} {sanitize(data.decode(errors='replace'))} (request_id={r.headers.get('x-request-id')})"


async def ws_synth(http: aiohttp.ClientSession, credential: str) -> str:
    try:
        async with http.ws_connect("wss://api.cartesia.ai/tts/websocket?cartesia_version=2025-04-16",
                                   headers={"X-API-Key": credential}) as ws:
            await ws.send_str(json.dumps({**BODY, "context_id": "key-check", "continue": False}))
            chunks = 0
            async for msg in ws:
                ev = json.loads(msg.data) if msg.type == aiohttp.WSMsgType.TEXT else {}
                if ev.get("type") == "chunk":
                    chunks += 1
                elif ev.get("type") in ("done", "error") or not ev:
                    if ev.get("type") == "error":
                        return f"connected, error: {sanitize(json.dumps(ev))}"
                    break
            return f"connected, {chunks} audio chunk(s)"
    except aiohttp.WSServerHandshakeError as exc:
        return f"handshake {exc.status}"


async def main() -> None:
    key = getpass.getpass("Cartesia API key (hidden): ").strip()
    if not key:
        sys.exit("no key entered")
    print(f"key shape: len={len(key)} starts_with_sk_car={key.startswith('sk_car_')}")
    async with aiohttp.ClientSession() as http:
        print("raw key   HTTP /tts/bytes     :", await http_synth(http, key))
        print("raw key   WS   /tts/websocket :", await ws_synth(http, key))
        try:
            token = await mint_access_token(key, ttl_s=120)
        except CartesiaAuthError as exc:
            print(f"mint access token (tts grant): FAIL {exc.status} {exc.provider_message} "
                  f"(request_id={exc.request_id})")
            return
        print("mint access token (tts grant): OK")
        print("token     HTTP /tts/bytes     :", await http_synth(http, token.token))
        print("token     WS   /tts/websocket :", await ws_synth(http, token.token))


if __name__ == "__main__":
    asyncio.run(main())
