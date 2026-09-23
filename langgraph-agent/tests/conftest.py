from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.config import Settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "contracts"


def make_settings(data_dir: Path, **overrides) -> Settings:
    values = {
        "COCOON_SERVICE_TOKEN": TOKEN,
        "COCOON_DATA_DIR": str(data_dir),
        "COCOON_LLM_MODE": "mock",
        "COCOON_TURN_POLL_AFTER_MS": 50,
        **overrides,
    }
    return Settings(**values, _env_file=None)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client_factory(data_dir: Path) -> Callable:
    @contextmanager
    def _make(brain=None, **overrides) -> Iterator[TestClient]:
        app = create_app(make_settings(data_dir, **overrides), brain=brain)
        with TestClient(app) as client:
            yield client

    return _make


@pytest.fixture
def client(client_factory) -> Iterator[TestClient]:
    with client_factory() as c:
        yield c


def new_session(client: TestClient, key: str = "lk:room-a:op-1", **fields) -> str:
    body = {
        "client_session_key": key,
        "room_name": fields.get("room_name", key.split(":")[1] if key.count(":") >= 2 else "room-a"),
        "participant_identity": fields.get("participant_identity", "op-1"),
        "operator_id": fields.get("operator_id", "op-1"),
        "machine_id": fields.get("machine_id", "cat-320-demo"),
    }
    r = client.post("/v1/sessions", json=body, headers=AUTH)
    assert r.status_code in (200, 201), r.text
    return r.json()["session_id"]


def turn(client: TestClient, session_id: str, turn_id: str, text: str, source: str = "voice"):
    return client.post(
        f"/v1/sessions/{session_id}/turns", json={"turn_id": turn_id, "text": text, "source": source}, headers=AUTH
    )


def telemetry(client: TestClient, session_id: str, event_id: str, observed_at: str, *, engine_on: bool,
              seatbelt: bool, idle: int = 0):
    return client.post(
        f"/v1/sessions/{session_id}/telemetry",
        json={
            "event_id": event_id,
            "observed_at": observed_at,
            "simulated": True,
            "readings": {"engine_on": engine_on, "seatbelt_fastened": seatbelt, "idle_seconds": idle},
        },
        headers=AUTH,
    )
