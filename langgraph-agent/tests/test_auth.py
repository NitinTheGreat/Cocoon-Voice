"""Actor authentication and authorisation (I02b): token lifecycle, CLI, access matrix and ownership isolation."""

from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cocoon_agent.api.app import create_app
from cocoon_agent.auth import token_digest
from cocoon_agent.catalog import load_catalog
from cocoon_agent.migrations import MIGRATIONS, migrate
from cocoon_agent.store import NewSessionBinding, Store
from cocoon_agent.token_admin import main as admin

from .conftest import AUTH, FIXTURE_MANIFEST_SHA256, make_settings

# The CLI stamps tokens with the real wall clock, so the injected test clock starts from real time too.
T0 = datetime.now(timezone.utc)


class Clock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def run_admin(settings, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = admin(list(argv), settings=settings, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def issue(settings, tmp_path: Path, role: str, *extra: str, name: str | None = None) -> tuple[str, str]:
    """Issue through the real CLI; read the token from its private file without printing it."""
    path = tmp_path / "tokens" / f"{name or role}-{len(list((tmp_path / 'tokens').glob('*'))) if (tmp_path / 'tokens').exists() else 0}.token"
    code, out, err = run_admin(settings, "issue", "--role", role, *extra, "--out", str(path))
    assert code == 0, err
    token = path.read_text(encoding="ascii").strip()
    assert token not in out and token_digest(token) not in out
    token_id = next(w for w in out.split() if w.startswith("tok_"))
    return token, token_id


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_session(c: TestClient, key: str, operator: str, machine: str = "EXC_DEMO_001") -> str:
    r = c.post("/v1/sessions", headers=AUTH, json={
        "client_session_key": key, "room_name": key, "participant_identity": key, "operator_id": operator,
        "machine_id": machine})
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


def turn(c: TestClient, sid: str, tid: str, text: str, headers: dict):
    return c.post(f"/v1/sessions/{sid}/turns", headers=headers, json={"turn_id": tid, "text": text, "source": "text"})


def count(settings, sql: str, *args) -> int:
    conn = sqlite3.connect(settings.db_path)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def world(tmp_path):
    """Two operators (A=OP_TEST_1, B=OP_TEST_2) with their own verified sessions and private records, a supervisor,
    and a legacy session whose free-text operator_id happens to equal A's catalog ID."""
    settings = make_settings(tmp_path)
    clock = Clock()
    a_token, a_id = issue(settings, tmp_path, "operator", "--operator-id", "OP_TEST_1", name="a")
    b_token, _ = issue(settings, tmp_path, "operator", "--operator-id", "OP_TEST_2", name="b")
    s_token, _ = issue(settings, tmp_path, "supervisor", "--principal-id", "sup-1", name="s")
    app = create_app(settings, clock=clock)
    with TestClient(app) as c:
        a_sid = new_session(c, "room-a", "OP_TEST_1")
        b_sid = new_session(c, "room-b", "OP_TEST_2", "DOZ_DEMO_001")
        # B's private records: an incident, a lesson assignment and an alert with an announcement
        assert turn(c, b_sid, "b1", "Log an incident: B private leak", AUTH).status_code == 200
        assert turn(c, b_sid, "b2", "Assign me the seatbelt lesson", AUTH).status_code == 200
        c.post(f"/v1/sessions/{b_sid}/telemetry", headers=AUTH, json={
            "event_id": "b-t1", "observed_at": "2026-09-24T11:00:00Z", "simulated": True,
            "readings": {"engine_on": True, "seatbelt_fastened": False, "idle_seconds": 0}})
        # a legacy row (pre-I02a shape) whose operator_id equals A's catalog ID, with a legacy lesson assignment
        conn = sqlite3.connect(settings.db_path)
        conn.execute("INSERT INTO sessions(session_id, client_session_key, room_name, participant_identity,"
                     " operator_id, machine_id, created_at) VALUES ('ses_legacy', 'legacy-key', 'old', 'old',"
                     " 'OP_TEST_1', 'cat-320-demo', '2026-09-01T00:00:00+00:00')")
        conn.execute("INSERT INTO training_assignments(assignment_id, operator_id, lesson_id, session_id,"
                     " source_turn_id, status, assigned_at) VALUES ('TA-legacy', 'OP_TEST_1', 'L2', 'ses_legacy', 'x',"
                     " 'assigned', '2026-09-01T00:00:01+00:00')")
        conn.commit()
        conn.close()
        yield {"c": c, "settings": settings, "clock": clock, "a": bearer(a_token), "b": bearer(b_token),
               "sup": bearer(s_token), "a_sid": a_sid, "b_sid": b_sid, "a_token_id": a_id, "tmp": tmp_path}


# ------------------------------------------------------------------ schema upgrade


def test_populated_v2_database_upgrades_to_v3_without_changes(tmp_path):
    settings = make_settings(tmp_path)
    conn = sqlite3.connect(settings.db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    assert migrate(conn, MIGRATIONS[:2]) == ["applied:1", "applied:2"]
    conn.close()
    store = Store(settings.db_path)
    store.seed_demo()
    catalog = load_catalog(settings.dataset_root, settings.dataset_manifest_sha256)
    store.register_catalog(catalog)
    from cocoon_agent.api import schemas as s
    req = s.SessionCreateRequest(client_session_key="k", room_name="r", participant_identity="p",
                                 operator_id="OP_TEST_1", machine_id="EXC_DEMO_001")
    session, _ = store.get_or_create_session(req, lambda _r: NewSessionBinding(FIXTURE_MANIFEST_SHA256, "unavailable"))
    store.create_incident(session, "hose", "t1")

    tables = ("sessions", "incidents", "catalog_versions", "catalog_version_machines", "catalog_version_operators",
              "tasks", "lessons")
    c = sqlite3.connect(settings.db_path)
    v2_columns = {t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")] for t in tables}
    c.close()

    def snapshot():  # the v2 columns only; later migrations may add columns but never change these values
        c = sqlite3.connect(settings.db_path)
        try:
            return {t: c.execute(f"SELECT {', '.join(v2_columns[t])} FROM {t} ORDER BY 1").fetchall() for t in tables}
        finally:
            c.close()

    before = snapshot()
    assert store.init_schema() == [f"applied:{m.version}" for m in MIGRATIONS[2:]]
    assert store.init_schema() == []
    store.close()
    assert snapshot() == before
    c = sqlite3.connect(settings.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE sessions SET operator_id = 'OP_TEST_2'")  # I02a immutability rule survives
    c.close()


# ------------------------------------------------------------------ CLI and storage


def test_cli_issue_list_show_revoke_and_no_plaintext_storage(tmp_path):
    settings = make_settings(tmp_path)
    token, token_id = issue(settings, tmp_path, "operator", "--operator-id", "OP_TEST_1")
    raw = b"".join(p.read_bytes() for p in settings.data_dir.glob("cocoon.db*"))
    assert token.encode() not in raw  # only the digest is stored
    assert count(settings, "SELECT COUNT(*) FROM actor_tokens WHERE token_sha256 = ?", token_digest(token)) == 1

    for argv in (("list",), ("show", token_id)):
        code, out, _ = run_admin(settings, *argv)
        assert code == 0 and token_id in out and "status=active" in out
        assert token not in out and token_digest(token) not in out
    code, out, _ = run_admin(settings, "revoke", token_id, "--reason", "test")
    assert code == 0 and out.splitlines()[1].startswith("revoked ")
    code, out, _ = run_admin(settings, "revoke", token_id)
    assert code == 0 and "already revoked" in out  # idempotent
    assert "status=revoked" in run_admin(settings, "show", token_id)[1]
    assert run_admin(settings, "revoke", "tok_nope")[0] == 1


@pytest.mark.parametrize("argv,message", [
    (("--role", "operator", "--operator-id", "OP_NOBODY"), "not in the verified catalog"),
    (("--role", "operator"), "--operator-id is required"),
    (("--role", "supervisor"), "--principal-id is required"),
    (("--role", "supervisor", "--principal-id", "s", "--operator-id", "OP_TEST_1"), "has no operator_id"),
    (("--role", "operator", "--operator-id", "OP_TEST_1", "--ttl-hours", "0.01"), "lifetime"),
    (("--role", "operator", "--operator-id", "OP_TEST_1", "--ttl-hours", "721"), "lifetime"),
    (("--role", "supervisor", "--principal-id", "bad id!"), "principal_id must match"),
])
def test_cli_refuses_bad_issuance(tmp_path, argv, message):
    settings = make_settings(tmp_path)
    out_file = tmp_path / "t.token"
    code, out, err = run_admin(settings, "issue", *argv, "--out", str(out_file))
    assert code == 1 and message in err
    assert not out_file.exists()
    assert count(settings, "SELECT COUNT(*) FROM actor_tokens") == 0


def test_cli_never_overwrites_or_widens(tmp_path):
    settings = make_settings(tmp_path)
    existing = tmp_path / "keep.token"
    existing.write_text("do not touch", encoding="ascii")
    code, _, err = run_admin(settings, "issue", "--role", "supervisor", "--principal-id", "sup-1", "--out",
                             str(existing))
    assert code == 1 and "refusing to overwrite" in err and existing.read_text(encoding="ascii") == "do not touch"

    issue(settings, tmp_path, "supervisor", "--principal-id", "sup-1")
    # the same principal_id cannot become an operator, and an operator cannot get a second principal
    code, _, err = run_admin(settings, "issue", "--role", "operator", "--operator-id", "OP_TEST_1",
                             "--principal-id", "sup-1", "--out", str(tmp_path / "x.token"))
    assert code == 1 and "different role or operator" in err
    issue(settings, tmp_path, "operator", "--operator-id", "OP_TEST_1")
    issue(settings, tmp_path, "operator", "--operator-id", "OP_TEST_1")  # second token, same principal
    code, _, err = run_admin(settings, "issue", "--role", "operator", "--operator-id", "OP_TEST_1",
                             "--principal-id", "alias-for-op1", "--out", str(tmp_path / "y.token"))
    assert code == 1 and "already has a principal" in err
    assert count(settings, "SELECT COUNT(*) FROM principals") == 2
    assert count(settings, "SELECT COUNT(*) FROM actor_tokens") == 3
    with pytest.raises(sqlite3.IntegrityError):
        c = sqlite3.connect(settings.db_path)
        try:
            c.execute("UPDATE principals SET kind = 'supervisor'")
        finally:
            c.close()


def test_operator_issuance_needs_a_verified_catalog_but_supervisors_do_not(tmp_path):
    bad = make_settings(tmp_path, DATASET_MANIFEST_SHA256="0" * 64)
    code, _, err = run_admin(bad, "issue", "--role", "operator", "--operator-id", "OP_TEST_1", "--out",
                             str(tmp_path / "o.token"))
    assert code == 1 and "catalog unavailable (manifest_hash_mismatch)" in err
    issue(bad, tmp_path, "supervisor", "--principal-id", "sup-1")


# ------------------------------------------------------------------ credentials


def test_credential_validation(world):
    c, a = world["c"], world["a"]
    token = a["Authorization"].split()[1]
    assert c.get("/v1/me", headers=a).status_code == 200
    for headers in ({}, {"Authorization": "Bearer"}, {"Authorization": f"Basic {token}"},
                    {"Authorization": "Bearer cct_" + "x" * 43}, {"Authorization": "Bearer " + token[:-1]},
                    {"Authorization": "Bearer not-a-cct-token"}, {"Authorization": "Bearer " + "a" * 300}):
        r = c.get("/v1/me", headers=headers)
        assert r.status_code == 401 and r.json()["error"]["message"] == "missing or invalid bearer token", headers
    # two Authorization headers are ambiguous even if both are valid
    r = c.get("/v1/me", headers=[("Authorization", a["Authorization"]), ("Authorization", AUTH["Authorization"])])
    assert r.status_code == 401


def test_expiry_uses_the_wall_clock_not_data_time(world):
    c, a, clock = world["c"], world["a"], world["clock"]
    # data time far in the future does not expire anything
    c.post(f"/v1/sessions/{world['a_sid']}/telemetry", headers=AUTH, json={
        "event_id": "future", "observed_at": "2031-01-01T00:00:00Z", "simulated": True,
        "readings": {"engine_on": False, "seatbelt_fastened": True, "idle_seconds": 0}})
    assert c.get(f"/v1/sessions/{world['a_sid']}/state", headers=a).status_code == 200
    clock.now = T0 + timedelta(hours=12, minutes=5)  # default lifetime 12 h (+ margin for issuance time)
    assert c.get("/v1/me", headers=a).status_code == 401
    before = count(world["settings"], "SELECT COUNT(*) FROM turns")
    assert turn(c, world["a_sid"], "late", "Log an incident: after expiry", a).status_code == 401
    assert count(world["settings"], "SELECT COUNT(*) FROM turns") == before  # nothing ran
    assert c.get("/v1/me", headers=AUTH).status_code == 200  # the service credential is unaffected


def test_revocation_applies_to_the_next_request(world):
    c, a = world["c"], world["a"]
    assert c.get("/v1/me", headers=a).status_code == 200
    assert run_admin(world["settings"], "revoke", world["a_token_id"])[0] == 0
    assert c.get("/v1/me", headers=a).status_code == 401
    assert turn(c, world["a_sid"], "after-revoke", "What's my next task?", a).status_code == 401


def test_auth_store_failure_fails_closed(world, monkeypatch):
    c = world["c"]
    store = c.app.state.service.store

    def broken(_digest):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "resolve_actor_token", broken)
    r = c.get("/v1/me", headers=world["a"])
    assert r.status_code == 503 and r.json()["error"]["code"] == "auth_unavailable"
    assert "disk" not in r.text  # sanitized
    assert c.get("/v1/me", headers=AUTH).status_code == 200  # service credential does not need the token store


# ------------------------------------------------------------------ access matrix


def _routes(sid: str) -> dict[str, tuple[str, str, dict | None]]:
    return {
        "create_session": ("post", "/v1/sessions", {"client_session_key": "m-new", "room_name": "r",
                                                     "participant_identity": "p", "operator_id": "OP_TEST_1",
                                                     "machine_id": "EXC_DEMO_001"}),
        "turn": ("post", f"/v1/sessions/{sid}/turns", {"turn_id": "m1", "text": "What's my next task?",
                                                       "source": "text"}),
        "get_turn": ("get", f"/v1/sessions/{sid}/turns/m1", None),
        "state": ("get", f"/v1/sessions/{sid}/state", None),
        "telemetry": ("post", f"/v1/sessions/{sid}/telemetry", {
            "event_id": "m-t", "observed_at": "2026-09-24T11:30:00Z", "simulated": True,
            "readings": {"engine_on": True, "seatbelt_fastened": True, "idle_seconds": 0}}),
        "events": ("get", f"/v1/sessions/{sid}/events", None),
        "delivery": ("post", f"/v1/sessions/{sid}/events/ann_x/delivery", {"consumer_id": "w", "status": "played"}),
    }


EXPECTED = {  # route -> (operator on own session, supervisor)
    "create_session": (403, 403), "turn": (200, 403), "get_turn": (200, 403), "state": (200, 403),
    "telemetry": (403, 403), "events": (200, 403), "delivery": (403, 403),
}


def test_access_matrix(world):
    c, sid = world["c"], world["a_sid"]
    routes = _routes(sid)
    for name in ("turn", "get_turn", "state", "events", "create_session", "telemetry", "delivery"):
        method, path, body = routes[name]
        op_status, sup_status = EXPECTED[name]
        r = c.request(method, path, headers=world["a"], json=body)
        assert r.status_code == op_status, (name, r.text)
        r = c.request(method, path, headers=world["sup"], json=body)
        assert r.status_code == sup_status, (name, r.text)
        if sup_status == 403:
            assert r.json()["error"]["code"] == "forbidden"
    # the trusted service keeps its existing behaviour on the same routes
    for name, expected in (("create_session", 201), ("turn", 200), ("get_turn", 200), ("state", 200),
                           ("telemetry", 200), ("events", 200), ("delivery", 404)):
        method, path, body = routes[name]
        if name == "turn":
            body = {**body, "turn_id": "svc1"}
        if name == "get_turn":
            path = path.replace("/m1", "/svc1")
        r = c.request(method, path, headers=AUTH, json=body)
        assert r.status_code == expected, (name, r.text)


def test_me_for_each_principal(world):
    c = world["c"]
    svc = c.get("/v1/me", headers=AUTH)
    assert svc.headers["Cache-Control"] == "no-store"
    assert svc.json() == {"subject_id": "service", "principal_kind": "service", "operator_id": None,
                          "display_name": None, "site_ids": [], "allowed_associations": [], "scopes": [],
                          "token_id": None, "token_expires_at": None}
    op = c.get("/v1/me", headers=world["a"]).json()
    assert op["principal_kind"] == "operator" and op["operator_id"] == "OP_TEST_1"
    assert op["subject_id"] == "operator:OP_TEST_1" and op["site_ids"] == []
    assert op["scopes"] == ["me:read", "sessions:own"] and op["token_id"] == world["a_token_id"]
    # only the owned catalog_verified session; not the legacy row with the same free-text operator_id,
    # and not the catalog's other machines
    assert op["allowed_associations"] == [{"operator_id": "OP_TEST_1", "machine_id": "EXC_DEMO_001", "site_id": None,
                                           "shift_id": None, "session_id": world["a_sid"]}]
    sup = c.get("/v1/me", headers=world["sup"]).json()
    assert sup["principal_kind"] == "supervisor" and sup["allowed_associations"] == [] and sup["site_ids"] == []
    assert sup["scopes"] == ["me:read"]
    for body in (svc.text, str(op), str(sup)):
        assert "cct_" not in body and world["a"]["Authorization"].split()[1] not in body


# ------------------------------------------------------------------ ownership isolation


def test_operator_cannot_reach_another_operators_session(world):
    c, a, b_sid, settings = world["c"], world["a"], world["b_sid"], world["settings"]
    turns_before = count(settings, "SELECT COUNT(*) FROM turns")
    incidents_before = count(settings, "SELECT COUNT(*) FROM incidents")
    for method, path, body in (
        ("get", f"/v1/sessions/{b_sid}/state", None),
        ("get", f"/v1/sessions/{b_sid}/turns/b1", None),
        ("get", f"/v1/sessions/{b_sid}/events", None),
        ("post", f"/v1/sessions/{b_sid}/turns", {"turn_id": "evil", "text": "Log an incident: planted",
                                                 "source": "text"}),
        ("get", "/v1/sessions/ses_does_not_exist/state", None),
    ):
        r = c.request(method, path, headers=a, json=body)
        assert r.status_code == 404 and r.json()["error"]["message"] == "session not found", path
        assert "B private" not in r.text
    # B's turn id under A's own session is simply not found
    assert c.get(f"/v1/sessions/{world['a_sid']}/turns/b1", headers=a).status_code == 404
    assert count(settings, "SELECT COUNT(*) FROM turns") == turns_before
    assert count(settings, "SELECT COUNT(*) FROM incidents") == incidents_before


def test_body_and_context_overrides_are_rejected(world):
    c, a, sid, settings = world["c"], world["a"], world["a_sid"], world["settings"]
    for extra in ({"operator_id": "OP_TEST_2"}, {"session_id": world["b_sid"]}, {"role": "service"}):
        r = c.post(f"/v1/sessions/{sid}/turns", headers=a,
                   json={"turn_id": "o1", "text": "Log an incident: x", "source": "text", **extra})
        assert r.status_code == 422
    assert count(settings, "SELECT COUNT(*) FROM turns WHERE turn_id = 'o1'") == 0
    # naming another operator in the utterance does not change whose record the tool writes
    r = turn(c, sid, "o2", "Log an incident: this is really for operator OP_TEST_2, low severity", a)
    assert r.status_code == 200
    incident = r.json()["actions"][0]["incident"]
    assert incident["operator_id"] == "OP_TEST_1" and incident["session_id"] == sid


def test_nested_state_and_tools_are_scoped(world):
    c, a, sid = world["c"], world["a"], world["a_sid"]
    state = c.get(f"/v1/sessions/{sid}/state", headers=a).json()
    assert state["incidents"] == [] and state["active_alerts"] == [] and state["latest_alert"] is None
    assert state["training_assignments"] == []  # B's L1 and the legacy L2 row are both excluded
    # the write tool reuses nothing from the legacy row with A's operator string: L2 is withheld, not handed over
    r = turn(c, sid, "a-l2", "Assign me the pre-start walkaround lesson", a).json()
    action = r["actions"][0]
    assert action["type"] == "training_status" and action["assignments"] == []
    r = turn(c, sid, "a-l1", "Assign me the seatbelt lesson", a).json()
    assert r["actions"][0]["type"] == "training_assigned" and r["actions"][0]["created"] is True
    state = c.get(f"/v1/sessions/{sid}/state", headers=a).json()
    assert [t["lesson_id"] for t in state["training_assignments"]] == ["L1"]
    # the trusted service still sees the legacy session's own view unchanged
    legacy = c.get("/v1/sessions/ses_legacy/state", headers=AUTH).json()
    assert [t["lesson_id"] for t in legacy["training_assignments"]] == ["L2"]


def test_legacy_session_stays_service_only(world):
    c = world["c"]
    assert c.get("/v1/sessions/ses_legacy/state", headers=world["a"]).status_code == 404
    r = c.post("/v1/sessions", headers=AUTH, json={
        "client_session_key": "legacy-key", "room_name": "old", "participant_identity": "old",
        "operator_id": "OP_TEST_1", "machine_id": "cat-320-demo"})
    assert r.status_code == 200 and r.json()["binding_status"] == "legacy_unverified"
    assert count(world["settings"], "SELECT COUNT(*) FROM sessions WHERE session_id = 'ses_legacy'"
                                    " AND binding_status = 'legacy_unverified'") == 1


def test_catalog_loss_after_issuance_neither_rewrites_nor_widens(world):
    settings, a = world["settings"], world["a"]
    # a second app instance on the SAME database whose pinned manifest no longer matches the catalog
    bad = make_settings(settings.data_dir, DATASET_MANIFEST_SHA256="0" * 64)
    with TestClient(create_app(bad, clock=world["clock"])) as c:
        assert c.get("/readyz").json()["catalog"] is False
        me = c.get("/v1/me", headers=a)
        assert me.status_code == 200, me.text
        assert [x["session_id"] for x in me.json()["allowed_associations"]] == [world["a_sid"]]
        assert c.get(f"/v1/sessions/{world['a_sid']}/state", headers=a).status_code == 200
        assert c.get(f"/v1/sessions/{world['b_sid']}/state", headers=a).status_code == 404
        assert c.post("/v1/sessions", headers=AUTH, json={
            "client_session_key": "new-after-loss", "room_name": "r", "participant_identity": "p",
            "operator_id": "OP_TEST_1", "machine_id": "EXC_DEMO_001"}).status_code == 503
    code, _, err = run_admin(bad, "issue", "--role", "operator", "--operator-id", "OP_TEST_1", "--out",
                             str(world["tmp"] / "after-loss.token"))
    assert code == 1 and "catalog unavailable" in err
    assert count(settings, "SELECT COUNT(*) FROM principals WHERE operator_id = 'OP_TEST_1'") == 1
