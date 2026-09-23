"""Tiny shared helper for the HTTP scripts in this folder (httpx only, no LiveKit)."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import dotenv_values

SERVICE_DIR = Path(__file__).resolve().parent.parent


def client(base_url: str | None = None, token: str | None = None) -> httpx.Client:
    env = {**dotenv_values(SERVICE_DIR / ".env"), **os.environ}
    base_url = base_url or env.get("COCOON_BACKEND_URL") or f"http://127.0.0.1:{env.get('COCOON_PORT') or '8000'}"
    token = token or env.get("COCOON_SERVICE_TOKEN")
    if not token:
        raise SystemExit("COCOON_SERVICE_TOKEN is not set (in langgraph-agent/.env or the environment)")
    return httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=60)


def ensure_session(c: httpx.Client, room: str, identity: str, operator: str, machine: str) -> str:
    r = c.post("/v1/sessions", json={
        "client_session_key": f"lk:{room}:{identity}", "room_name": room, "participant_identity": identity,
        "operator_id": operator, "machine_id": machine,
    })
    r.raise_for_status()
    return r.json()["session_id"]
