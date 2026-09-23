"""Real HTTP integration with a RUNNING langgraph-agent (opt-in; no LiveKit, no audio).

    # terminal 1 (langgraph-agent):  python -m cocoon_agent
    # terminal 2 (livekit-voice):    COCOON_INTEGRATION_BACKEND_URL=http://127.0.0.1:8000 pytest -m integration

Uses the worker's own BackendClient/TurnBridge/AnnouncementPump over the network.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from livekit.agents import llm

from cocoon_voice import contract as c
from cocoon_voice.announcements import AnnouncementPump
from cocoon_voice.backend_client import BackendClient
from cocoon_voice.bridge import TurnBridge, bind_session

URL = os.environ.get("COCOON_INTEGRATION_BACKEND_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="set COCOON_INTEGRATION_BACKEND_URL to a running backend"),
]


class Handle:
    interrupted = False

    async def wait_for_playout(self):
        return None


class Speaker:
    def __init__(self):
        self.spoken: list[str] = []

    async def wait_until_quiet(self, max_wait):
        return None

    def say(self, text):
        self.spoken.append(text)
        return Handle()


def token() -> str:
    # conftest sets a placeholder COCOON_SERVICE_TOKEN, so read the real one from livekit-voice/.env
    from dotenv import dotenv_values

    from cocoon_voice.config import ENV_FILE

    return os.environ.get("COCOON_INTEGRATION_TOKEN") or dotenv_values(ENV_FILE)["COCOON_SERVICE_TOKEN"]


async def test_voice_adapter_end_to_end_against_real_backend():
    run = uuid.uuid4().hex[:6]
    client = BackendClient(URL, token())
    try:
        req = c.SessionCreateRequest(client_session_key=f"lk:it-{run}:op", room_name=f"it-{run}",
                                     participant_identity="op", operator_id="op", machine_id="cat-320-demo")
        binding = await bind_session(client, req)
        assert (await bind_session(client, req)).session_id == binding.session_id
        bridge = TurnBridge(client, binding)
        chat = llm.ChatContext.empty()

        async def say(text: str) -> str:
            chat.add_message(role="user", content=text)
            speech = await bridge.reply_for(chat)
            chat.add_message(role="assistant", content=speech)
            return speech

        assert "next task" in (await say("What's my next task?")).lower()
        assert "what happened" in (await say("I need to report an incident")).lower()
        speech = await say("Boom hydraulic hose is leaking")
        assert "incident number" in speech.lower()

        # retry of the exact same utterance id returns the stored result, no duplicate record
        user_item = chat.items[-2]
        replay = await client.submit_turn(binding.session_id, f"lk-{user_item.id}", user_item.text_content)
        assert replay.speech == speech
        async with httpx.AsyncClient(base_url=URL, headers={"Authorization": f"Bearer {token()}"}) as raw:
            state = (await raw.get(f"/v1/sessions/{binding.session_id}/state")).json()
            assert len(state["incidents"]) == 1
            now = datetime.now(timezone.utc)
            for i, belted in enumerate([True, False, False]):
                r = await raw.post(f"/v1/sessions/{binding.session_id}/telemetry", json={
                    "event_id": f"it-{run}-{i}", "observed_at": (now + timedelta(seconds=i)).isoformat(),
                    "simulated": True, "readings": {"engine_on": True, "seatbelt_fastened": belted, "idle_seconds": 0}})
                assert r.status_code == 200

        speaker = Speaker()
        pump = AnnouncementPump(client, lambda: bridge.session_id, speaker, f"it-worker-{run}")
        await pump.poll_once()
        await pump.poll_once()
        assert len(speaker.spoken) == 1 and "seatbelt" in speaker.spoken[0].lower()
        page = await client.list_events(binding.session_id, after=0)
        assert page.events[0].deliveries[0].status == "played"

        assert "seatbelt" in (await say("Why?")).lower()
    finally:
        await client.aclose()
