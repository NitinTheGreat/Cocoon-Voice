"""Duplicate-in-flight turns, failed turns, partial-failure retries and restart durability."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx

from cocoon_agent.api.app import create_app
from cocoon_agent.graph.brain import LLMUnavailable, MockBrain
from cocoon_agent.service import payload_hash
from cocoon_agent.store import Store

from .conftest import AUTH, make_settings, new_session, telemetry, turn


@asynccontextmanager
async def running_app(data_dir, brain=None):
    app = create_app(make_settings(data_dir), brain=brain)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AUTH) as client:
            yield client


class GatedBrain(MockBrain):
    """Blocks routing until released, to hold a turn in 'processing'."""

    def __init__(self):
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def route(self, ctx):
        self.entered.set()
        await self.release.wait()
        return await super().route(ctx)


class FlakyComposeBrain(MockBrain):
    """Fails wording after the action node has already saved its record."""

    def __init__(self, failures: int):
        self.failures = failures

    async def compose(self, ctx, actions):
        if self.failures > 0:
            self.failures -= 1
            raise LLMUnavailable("simulated provider outage")
        return await super().compose(ctx, actions)


async def _session(client: httpx.AsyncClient) -> str:
    r = await client.post("/v1/sessions", json={
        "client_session_key": "lk:r:p", "room_name": "r", "participant_identity": "p", "operator_id": "OP_TEST_1",
        "machine_id": "EXC_DEMO_001"})
    return r.json()["session_id"]


async def test_duplicate_while_processing_returns_202_then_result(data_dir):
    brain = GatedBrain()
    async with running_app(data_dir, brain) as client:
        sid = await _session(client)
        body = {"turn_id": "slow-1", "text": "What's my next task?", "source": "voice"}
        first = asyncio.create_task(client.post(f"/v1/sessions/{sid}/turns", json=body))
        await brain.entered.wait()

        dup = await client.post(f"/v1/sessions/{sid}/turns", json=body)
        assert dup.status_code == 202
        assert dup.json()["status"] == "processing" and dup.json()["retry_after_ms"] == 50
        assert dup.headers["Location"] == f"/v1/sessions/{sid}/turns/slow-1"
        assert dup.headers["Retry-After"] == "1"
        polled = await client.get(f"/v1/sessions/{sid}/turns/slow-1")
        assert polled.json()["status"] == "processing"

        brain.release.set()
        done = await first
        assert done.status_code == 200 and done.json()["status"] == "completed"
        polled = await client.get(f"/v1/sessions/{sid}/turns/slow-1")
        assert polled.json() == done.json()


async def test_failure_after_action_saved_is_retryable_without_duplicates(data_dir):
    brain = FlakyComposeBrain(failures=1)
    async with running_app(data_dir, brain) as client:
        sid = await _session(client)
        body = {"turn_id": "t-flaky", "text": "Log an incident: bucket tooth missing, medium severity", "source": "voice"}
        failed = await client.post(f"/v1/sessions/{sid}/turns", json=body)
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "llm_unavailable" and failed.json()["error"]["retryable"] is True
        status = (await client.get(f"/v1/sessions/{sid}/turns/t-flaky")).json()
        assert status["status"] == "failed" and status["speech"] is None
        # The record was saved before wording failed; the outcome was unknown to the client, not "cancelled".
        state = (await client.get(f"/v1/sessions/{sid}/state")).json()
        assert len(state["incidents"]) == 1

        retry = await client.post(f"/v1/sessions/{sid}/turns", json=body)
        assert retry.status_code == 200
        action = retry.json()["actions"][0]
        assert action["type"] == "incident_logged" and action["created"] is False
        state = (await client.get(f"/v1/sessions/{sid}/state")).json()
        assert len(state["incidents"]) == 1


async def test_orphaned_processing_turn_is_rerun_after_restart(data_dir):
    async with running_app(data_dir) as client:
        sid = await _session(client)
    # Simulate a crash mid-turn: a 'processing' row exists but no process owns it.
    store = Store(make_settings(data_dir).db_path)
    store.claim_turn(sid, "t-orphan", payload_hash({"text": "What's my next task?", "source": "voice"}))
    store.close()
    async with running_app(data_dir) as client:
        assert (await client.get(f"/v1/sessions/{sid}/turns/t-orphan")).json()["status"] == "processing"
        r = await client.post(f"/v1/sessions/{sid}/turns",
                              json={"turn_id": "t-orphan", "text": "What's my next task?", "source": "voice"})
        assert r.status_code == 200 and r.json()["actions"][0]["type"] == "next_task"


def test_records_and_context_survive_restart(client_factory):
    with client_factory() as c:
        sid = new_session(c)
        first = turn(c, sid, "t1", "Log an incident: fuel cap is missing, low severity").json()
        turn(c, sid, "t2", "Report an incident")  # leaves a pending question in graph memory
        telemetry(c, sid, "s1", "2026-09-23T10:00:01Z", engine_on=True, seatbelt=False)
        version = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["state_version"]

    with client_factory() as c:  # new app + new connections on the same SQLite files
        assert new_session(c) == sid  # same client_session_key resolves to the same session
        state = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()
        assert state["state_version"] == version
        assert state["pending_question"]["kind"] == "incident_description"
        assert len(state["active_alerts"]) == 1
        replay = turn(c, sid, "t1", "Log an incident: fuel cap is missing, low severity").json()
        assert replay == first  # completed turn replays from storage
        answer = turn(c, sid, "t3", "Mirror bracket snapped off, low severity").json()
        assert answer["actions"][0]["type"] == "incident_logged"
        why = turn(c, sid, "t4", "why?").json()
        assert why["actions"][0]["alert"]["status"] == "active"
        incidents = c.get(f"/v1/sessions/{sid}/state", headers=AUTH).json()["incidents"]
        assert [i["incident_number"] for i in incidents] == [1, 2]
        # a fresh telemetry sample of the same condition does not re-announce after restart
        again = telemetry(c, sid, "s2", "2026-09-23T10:00:02Z", engine_on=True, seatbelt=False).json()
        assert again["announcements_created"] == []
