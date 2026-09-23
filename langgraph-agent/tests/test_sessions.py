"""Catalog-bound session creation (I02a): admission, error precedence, retries, bindings and version pinning."""

from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api import schemas as s
from cocoon_agent.api.app import create_app
from cocoon_agent.store import Conflict, NewSessionBinding, Store

from .conftest import (
    AUTH, FIXTURE_BINDINGS, FIXTURE_MANIFEST_SHA256, MACHINES, OPERATOR, make_settings, write_catalog,
)


def body(key: str = "lk:r:p", **over) -> dict:
    return {"client_session_key": key, "room_name": "r", "participant_identity": "p", "operator_id": OPERATOR,
            "machine_id": "EXC_DEMO_001", **over}


def session_rows(settings) -> list[sqlite3.Row]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------------ admission


def test_every_canonical_asset_can_start_a_verified_session(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as c:
        for i, machine in enumerate(MACHINES):
            r = c.post("/v1/sessions", json=body(f"k{i}", machine_id=machine), headers=AUTH)
            assert r.status_code == 201, r.text
            out = r.json()
            assert out["binding_status"] == "catalog_verified"
            assert out["dataset_manifest_sha256"] == FIXTURE_MANIFEST_SHA256
            assert out["context_status"] == "unavailable" and out["site_id"] is None and out["shift_id"] is None
    rows = session_rows(settings)
    assert [r["machine_id"] for r in rows] == list(MACHINES)
    assert {r["dataset_manifest_sha256"] for r in rows} == {FIXTURE_MANIFEST_SHA256}


@pytest.mark.parametrize("over,code,fields", [
    ({"machine_id": "cat-320-demo"}, "unknown_machine", ["body.machine_id"]),
    ({"machine_id": "exc_demo_001"}, "unknown_machine", ["body.machine_id"]),  # no case folding or aliasing
    ({"machine_id": "Cat 320"}, "unknown_machine", ["body.machine_id"]),  # display labels are not IDs
    ({"operator_id": "operator-7"}, "unknown_operator", ["body.operator_id"]),
    ({"machine_id": "m", "operator_id": "op"}, "unknown_machine", ["body.machine_id", "body.operator_id"]),
])
def test_unknown_ids_are_rejected_without_writing(tmp_path, over, code, fields):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as c:
        r = c.post("/v1/sessions", json=body(**over), headers=AUTH)
        assert r.status_code == 422
        err = r.json()["error"]
        assert err["code"] == code and err["retryable"] is False
        assert [d["field"] for d in err["details"]] == fields
        assert "OP_TEST" not in r.text  # the error never lists catalog contents
    assert session_rows(settings) == []


def test_authentication_is_checked_before_the_catalog(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        r = c.post("/v1/sessions", json=body(machine_id="nope"))
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
        r = c.post("/v1/sessions", json={"client_session_key": "k"}, headers=AUTH)
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


# ------------------------------------------------------------------ retries and conflicts


def test_identical_retry_returns_the_stored_session_and_changes_conflict(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as c:
        first = c.post("/v1/sessions", json=body(), headers=AUTH)
        again = c.post("/v1/sessions", json=body(), headers=AUTH)
        assert (first.status_code, again.status_code) == (201, 200)
        assert first.json() == again.json()
        for change in ({"machine_id": "DOZ_DEMO_001"}, {"operator_id": "OP_TEST_2"}, {"room_name": "other"},
                       {"machine_id": "not-even-in-catalog"}):
            r = c.post("/v1/sessions", json=body(**change), headers=AUTH)
            assert r.status_code == 409 and r.json()["error"]["code"] == "session_conflict"
    rows = session_rows(settings)
    assert len(rows) == 1 and rows[0]["machine_id"] == "EXC_DEMO_001" and rows[0]["operator_id"] == OPERATOR


def _admit(req: s.SessionCreateRequest) -> NewSessionBinding:
    return NewSessionBinding(dataset_manifest_sha256=FIXTURE_MANIFEST_SHA256, context_status="unavailable")


def _register_fixture_catalog(settings) -> None:
    from cocoon_agent.catalog import load_catalog
    store = Store(settings.db_path)
    store.init_schema()
    store.register_catalog(load_catalog(settings.dataset_root, settings.dataset_manifest_sha256))
    store.close()


def test_concurrent_creators_on_separate_connections_create_exactly_one_session(tmp_path):
    settings = make_settings(tmp_path)
    _register_fixture_catalog(settings)
    stores = [Store(settings.db_path) for _ in range(8)]
    barrier = threading.Barrier(len(stores))
    results, errors = [], []

    def create(store: Store, machine: str) -> None:
        barrier.wait()
        try:
            results.append(store.get_or_create_session(
                s.SessionCreateRequest(**body("race", machine_id=machine)), _admit))
        except Conflict as exc:
            errors.append(exc)

    # half identical, half conflicting (different machine under the same key)
    threads = [threading.Thread(target=create, args=(st, "EXC_DEMO_001" if i % 2 else "DOZ_DEMO_001"))
               for i, st in enumerate(stores)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for st in stores:
        st.close()
    rows = session_rows(settings)
    assert len(rows) == 1  # the UNIQUE key + BEGIN IMMEDIATE serialise creators across connections
    winner = rows[0]["machine_id"]
    assert sum(created for _, created in results) == 1
    assert all(sess.session_id == rows[0]["session_id"] for sess, _ in results)
    assert len(results) == 4 and len(errors) == 4  # every request naming the other machine conflicted
    assert {sess.machine_id for sess, _ in results} == {winner}


# ------------------------------------------------------------------ site/shift context


def test_trusted_binding_is_stored_and_spoofing_is_rejected(tmp_path):
    settings = make_settings(tmp_path, SESSION_BINDINGS_PATH=str(FIXTURE_BINDINGS))
    trusted = {"site_id": "SITE_FIXTURE_A", "shift_id": "SHIFT_FIXTURE_A_DAY1"}
    with TestClient(create_app(settings)) as c:
        r = c.post("/v1/sessions", json=body("bound", **trusted), headers=AUTH)
        assert r.status_code == 201, r.text
        out = r.json()
        assert (out["site_id"], out["shift_id"], out["context_status"]) == (
            "SITE_FIXTURE_A", "SHIFT_FIXTURE_A_DAY1", "trusted_binding")
        assert out["context_source"].endswith(":BIND_TEST_1")
        # retries cannot change a known binding; omitting the optional fields does not assert anything
        assert c.post("/v1/sessions", json=body("bound", site_id="SITE_FIXTURE_A", shift_id="OTHER_SHIFT"),
                      headers=AUTH).status_code == 409
        assert c.post("/v1/sessions", json=body("bound"), headers=AUTH).json() == out

        for spoof in ({"site_id": "SITE_FIXTURE_A", "shift_id": "SHIFT_FAKE"},        # no such binding
                      {"site_id": "SITE_FIXTURE_A"},                                  # half an association
                      {**trusted, "operator_id": "OP_TEST_2"}):                       # binding for someone else
            r = c.post("/v1/sessions", json=body("spoof", **spoof), headers=AUTH)
            assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error", spoof
        # an unbound session cannot acquire a site later through a retry
        assert c.post("/v1/sessions", json=body("plain"), headers=AUTH).status_code == 201
        assert c.post("/v1/sessions", json=body("plain", **trusted), headers=AUTH).status_code == 409
    rows = {r["client_session_key"]: r for r in session_rows(settings)}
    assert set(rows) == {"bound", "plain"}
    assert rows["plain"]["site_id"] is None and rows["plain"]["context_status"] == "unavailable"


def test_supplied_site_without_configured_bindings_is_rejected(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as c:
        r = c.post("/v1/sessions", json=body(site_id="SITE_FIXTURE_A", shift_id="SHIFT_FIXTURE_A_DAY1"),
                   headers=AUTH)
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert session_rows(settings) == []


# ------------------------------------------------------------------ catalog availability and versions


def test_missing_catalog_blocks_readiness_and_new_sessions_but_not_existing_ones(tmp_path):
    good = make_settings(tmp_path)
    with TestClient(create_app(good)) as c:
        sid = c.post("/v1/sessions", json=body("existing"), headers=AUTH).json()["session_id"]
        ready = c.get("/readyz").json()
        assert ready["catalog"] is True and ready["catalog_version"] == FIXTURE_MANIFEST_SHA256
        assert ready["schema_version"] == 2

    bad = make_settings(tmp_path, DATASET_MANIFEST_SHA256="0" * 64)
    with TestClient(create_app(bad)) as c:
        ready = c.get("/readyz")
        assert ready.status_code == 503
        assert ready.json()["catalog"] is False and ready.json()["catalog_issue"] == "manifest_hash_mismatch"
        r = c.post("/v1/sessions", json=body("new"), headers=AUTH)
        assert r.status_code == 503 and r.json()["error"]["code"] == "catalog_unavailable"
        # never a fabricated unknown-ID answer or a success; the public message leaks no paths or digests
        assert r.json()["error"]["message"] == \
            "the verified machine/operator catalog is not loaded; new sessions cannot be admitted"
        assert FIXTURE_MANIFEST_SHA256 not in r.text and "0" * 64 not in r.text and "fixtures" not in r.text
        again = c.post("/v1/sessions", json=body("existing"), headers=AUTH)
        assert again.status_code == 200 and again.json()["session_id"] == sid
        assert c.post(f"/v1/sessions/{sid}/turns", headers=AUTH,
                      json={"turn_id": "t1", "text": "What's my next task?", "source": "text"}).status_code == 200
    assert [r["client_session_key"] for r in session_rows(bad)] == ["existing"]


def test_a_later_catalog_never_rebinds_an_earlier_session(tmp_path):
    first = make_settings(tmp_path)
    with TestClient(create_app(first)) as c:
        old = c.post("/v1/sessions", json=body("pinned"), headers=AUTH).json()

    newer_root = tmp_path / "catalog_v2"
    newer = write_catalog(newer_root, operators="operator_id\nOP_TEST_1\nOP_TEST_4\n")
    second = make_settings(tmp_path, DATASET_ROOT=str(newer_root), DATASET_MANIFEST_SHA256=newer)
    with TestClient(create_app(second)) as c:
        retry = c.post("/v1/sessions", json=body("pinned"), headers=AUTH)
        assert retry.status_code == 200 and retry.json() == old
        assert retry.json()["dataset_manifest_sha256"] == FIXTURE_MANIFEST_SHA256  # still the earlier snapshot
        fresh = c.post("/v1/sessions", json=body("fresh", operator_id="OP_TEST_4"), headers=AUTH).json()
        assert fresh["dataset_manifest_sha256"] == newer
        gone = c.post("/v1/sessions", json=body("gone", operator_id="OP_TEST_2"), headers=AUTH)
        assert gone.status_code == 422 and gone.json()["error"]["code"] == "unknown_operator"

    conn = sqlite3.connect(first.db_path)
    versions = {r[0] for r in conn.execute("SELECT manifest_sha256 FROM catalog_versions")}
    old_ops = {r[0] for r in conn.execute("SELECT operator_id FROM catalog_version_operators WHERE manifest_sha256 = ?",
                                          (FIXTURE_MANIFEST_SHA256,))}
    conn.close()
    assert versions == {FIXTURE_MANIFEST_SHA256, newer}
    assert old_ops == {"OP_TEST_1", "OP_TEST_2", "OP_TEST_3"}  # the earlier snapshot stays interpretable


def test_catalog_is_loaded_once_and_not_reread_per_request(tmp_path):
    root = tmp_path / "cat"
    digest = write_catalog(root)
    settings = make_settings(tmp_path, DATASET_ROOT=str(root), DATASET_MANIFEST_SHA256=digest)
    with TestClient(create_app(settings)) as c:
        (root / "data" / "generated" / "operators.csv").write_text("operator_id\nOP_INJECTED\n", encoding="utf-8")
        injected = c.post("/v1/sessions", json=body("x", operator_id="OP_INJECTED"), headers=AUTH)
        assert injected.status_code == 422  # the running snapshot is immutable
        assert c.post("/v1/sessions", json=body("y"), headers=AUTH).status_code == 201
    with TestClient(create_app(settings)) as c:  # the changed file is detected at the next start
        assert c.get("/readyz").json()["catalog_issue"] == "file_hash_mismatch"
