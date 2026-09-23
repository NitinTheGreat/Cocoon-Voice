"""Exercise the worker's BackendClient + TurnBridge against a running backend (real or mock). No LiveKit, no audio.

    python scripts/backend_probe.py ["What's my next task?"] [--base-url http://127.0.0.1:8010]

Uses the same code path as llm_node (TurnBridge.reply_for) with a synthetic chat context.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit.agents import llm  # noqa: E402

from cocoon_voice import contract as c  # noqa: E402
from cocoon_voice.backend_client import BackendClient  # noqa: E402
from cocoon_voice.bridge import TurnBridge, bind_session  # noqa: E402
from cocoon_voice.config import get_settings  # noqa: E402


async def run(texts: list[str], base_url: str | None) -> None:
    settings = get_settings()
    if settings.service_token is None:
        raise SystemExit("COCOON_SERVICE_TOKEN is not set (future-only remote bridge tooling)")
    client = BackendClient(base_url or settings.backend_url, settings.service_token.get_secret_value())
    try:
        run_id = uuid.uuid4().hex[:6]
        req = c.SessionCreateRequest(client_session_key=f"lk:probe-{run_id}:probe-operator",
                                     room_name=f"probe-{run_id}", participant_identity="probe-operator",
                                     operator_id="probe-operator", machine_id=settings.default_machine_id)
        bridge = TurnBridge(client, await bind_session(client, req))
        ctx = llm.ChatContext.empty()
        for text in texts:
            ctx.add_message(role="user", content=text)
            speech = await bridge.reply_for(ctx)
            print(f"> {text}\n< {speech}")
            ctx.add_message(role="assistant", content=speech or "")
        page = await client.list_events(bridge.binding.session_id, after=0)
        print(f"events: {len(page.events)} (session {bridge.binding.session_id})")
    finally:
        await client.aclose()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("texts", nargs="*", default=["What's my next task?"])
    ap.add_argument("--base-url")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(run(args.texts, args.base_url))


if __name__ == "__main__":
    main()
