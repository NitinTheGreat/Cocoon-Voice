"""Dispatch the named Cocoon agent and/or mint a DEV join token (uses LIVEKIT_* from livekit-voice/.env).

    python scripts/dispatch.py token    --room cocoon-demo --identity operator-7   # token that dispatches on room creation
    python scripts/dispatch.py dispatch --room cocoon-demo                          # explicit dispatch into a room (API)
    python scripts/dispatch.py list     --room cocoon-demo

Dev/test helper only. Real clients (Cocoon-App) must get tokens from an authenticated
server-side token endpoint; never ship LIVEKIT_API_SECRET to a browser or phone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit import api  # noqa: E402

from cocoon_voice.config import get_settings  # noqa: E402


def _require(settings) -> None:
    missing = settings.missing_livekit()
    if missing:
        raise SystemExit(f"missing in livekit-voice/.env: {', '.join(missing)}")


def _metadata(args) -> str:
    meta = {k: v for k, v in (("operator_id", args.operator), ("machine_id", args.machine),
                              ("session_id", args.session_id)) if v}
    return json.dumps(meta) if meta else ""


def cmd_token(args, settings) -> None:
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret.get_secret_value())
        .with_identity(args.identity)
        .with_name(args.identity)
        .with_ttl(timedelta(hours=2))
        .with_grants(api.VideoGrants(room_join=True, room=args.room, can_publish=True, can_subscribe=True,
                                     can_publish_data=True))
        .with_room_config(api.RoomConfiguration(
            agents=[api.RoomAgentDispatch(agent_name=settings.agent_name, metadata=_metadata(args))]))
        .to_jwt()
    )
    print(f"LiveKit URL : {settings.livekit_url}")
    print(f"Room        : {args.room}   identity: {args.identity}   agent: {settings.agent_name}")
    print("Token dispatch only fires when the room is CREATED by this join; use a fresh room name.")
    print(f"Token       : {token}")
    print("Join with any LiveKit client that accepts URL + token, e.g.:")
    print("  https://meet.livekit.io/custom?" + urlencode({"liveKitUrl": settings.livekit_url, "token": token}))


async def cmd_dispatch(args, settings) -> None:
    async with api.LiveKitAPI(settings.livekit_url, settings.livekit_api_key,
                              settings.livekit_api_secret.get_secret_value()) as lk:
        d = await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
            agent_name=settings.agent_name, room=args.room, metadata=_metadata(args)))
        print(f"created dispatch {d.id} agent={d.agent_name} room={d.room}")


async def cmd_list(args, settings) -> None:
    async with api.LiveKitAPI(settings.livekit_url, settings.livekit_api_key,
                              settings.livekit_api_secret.get_secret_value()) as lk:
        for d in await lk.agent_dispatch.list_dispatch(room_name=args.room):
            print(f"{d.id} agent={d.agent_name} room={d.room} jobs={len(d.state.jobs)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["token", "dispatch", "list"])
    ap.add_argument("--room", required=True)
    ap.add_argument("--identity", default="operator-7")
    ap.add_argument("--operator", help="operator_id passed as trusted job metadata")
    ap.add_argument("--machine", help="machine_id passed as trusted job metadata")
    ap.add_argument("--session-id", help="existing backend session_id passed as trusted job metadata")
    args = ap.parse_args()
    settings = get_settings()
    _require(settings)
    if args.command == "token":
        cmd_token(args, settings)
    elif args.command == "dispatch":
        asyncio.run(cmd_dispatch(args, settings))
    else:
        asyncio.run(cmd_list(args, settings))


if __name__ == "__main__":
    main()
