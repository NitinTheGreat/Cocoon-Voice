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
FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Committed synthetic test catalog (NOT the Cocoon dataset): five canonical asset IDs, operators OP_TEST_1..3.
FIXTURE_CATALOG = FIXTURES / "catalog"
FIXTURE_MANIFEST_SHA256 = "024be1a5bd7ad58bc33350ef7a64ddc37269c604d89147087de06013951c4e3f"
FIXTURE_BINDINGS = FIXTURES / "session_bindings.json"
MACHINES = ("EXC_DEMO_001", "DOZ_DEMO_001", "LDR_DEMO_001", "TRK_DEMO_001", "BHL_DEMO_001")
OPERATOR = "OP_TEST_1"


def make_settings(data_dir: Path, **overrides) -> Settings:
    values = {
        "COCOON_SERVICE_TOKEN": TOKEN,
        "COCOON_DATA_DIR": str(data_dir),
        "COCOON_LLM_MODE": "mock",
        "COCOON_TURN_POLL_AFTER_MS": 50,
        # Tests are independent of the wall-clock hour: weather is off unless a test selects the fixture/live source.
        "COCOON_WEATHER_MODE": "off",
        "DATASET_ROOT": str(FIXTURE_CATALOG),
        "DATASET_MANIFEST_SHA256": FIXTURE_MANIFEST_SHA256,
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
        "operator_id": fields.get("operator_id", OPERATOR),
        "machine_id": fields.get("machine_id", "EXC_DEMO_001"),
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


def write_catalog(root: Path, machines: str | None = None, operators: str | None = None, *,
                  rows: dict | None = None, schema_version: str = "1.0") -> str:
    """Build a self-consistent synthetic catalog under `root` (copying the committed fixture by default).

    Returns the SHA-256 of the manifest bytes written. Used to derive variants for negative/version tests."""
    import hashlib
    import json

    src = FIXTURE_CATALOG / "data" / "generated"
    machines_b = (machines.encode() if machines is not None else (src / "machines.csv").read_bytes())
    operators_b = (operators.encode() if operators is not None else (src / "operators.csv").read_bytes())
    gen = root / "data" / "generated"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "machines.csv").write_bytes(machines_b)
    (gen / "operators.csv").write_bytes(operators_b)
    count = lambda b: max(len(b.decode().strip().splitlines()) - 1, 0)  # noqa: E731
    manifest = {"schema_version": schema_version, "dataset_origin": "synthetic_test_fixture",
                "rows": rows or {"machines": count(machines_b), "operators": count(operators_b)},
                "sha256": {"machines.csv": hashlib.sha256(machines_b).hexdigest(),
                           "operators.csv": hashlib.sha256(operators_b).hexdigest()}}
    data = (json.dumps(manifest, indent=2) + "\n").encode()
    (gen / "manifest.json").write_bytes(data)
    return hashlib.sha256(data).hexdigest()


DEMO_OPERATORS = ("OP_DEMO_1_1", "OP_DEMO_2_1", "OP_DEMO_3_1", "OP_DEMO_4_1", "OP_DEMO_5_1")
DEMO_PAIRS = dict(zip(MACHINES, DEMO_OPERATORS))


def seeded_demo(tmp_path: Path, service_date: str | None = None, *, machines: str | None = None,
                operators: str | None = None, **overrides):
    """A disposable catalog (fixture machines + the five demo operators), seeded with demo/demo_site_v1.json.

    Returns (settings, report). Test-only: the fixture's pinned manifest hash is replaced by this catalog's hash."""
    from cocoon_agent.catalog import load_catalog
    from cocoon_agent.demo_site import load_fixture, seed, site_today
    from cocoon_agent.store import Store

    root = tmp_path / "demo_catalog"
    ops = operators or "operator_id\n" + "".join(f"{o}\n" for o in DEMO_OPERATORS)
    digest = write_catalog(root, machines=machines, operators=ops)
    settings = make_settings(tmp_path, DATASET_ROOT=str(root), DATASET_MANIFEST_SHA256=digest,
                             SESSION_BINDINGS_PATH=str(tmp_path / "bindings.json"), **overrides)
    doc = load_fixture()
    doc["catalog_manifest_sha256"] = digest
    store = Store(settings.db_path)
    store.init_schema()
    store.seed_demo()
    catalog = load_catalog(root, digest)
    store.register_catalog(catalog)
    report = seed(store, catalog, doc, service_date or site_today(doc), tmp_path / "bindings.json")
    store.close()
    return settings, report
