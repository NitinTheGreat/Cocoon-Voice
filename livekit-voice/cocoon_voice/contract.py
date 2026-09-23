"""The voice worker's own copy of the v1 backend contract (subset it uses).

Deliberately NOT imported from langgraph-agent. Requests are strict; responses
ignore unknown fields so additive backend changes do not break the worker.
tests/test_contract.py validates these against ../contracts/openapi.yaml.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SessionCreateRequest(_Request):
    client_session_key: str
    room_name: str
    participant_identity: str
    operator_id: str
    machine_id: str


class Session(_Response):
    session_id: str
    client_session_key: str
    room_name: str
    participant_identity: str
    operator_id: str
    machine_id: str
    state_version: int
    created_at: datetime


class TurnRequest(_Request):
    turn_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$")
    text: str = Field(min_length=1, max_length=2000)
    source: Literal["voice", "text"] = "voice"


class ErrorBody(_Response):
    code: str
    message: str
    retryable: bool
    request_id: str


class ErrorResponse(_Response):
    error: ErrorBody


class TurnResult(_Response):
    session_id: str
    turn_id: str
    status: Literal["processing", "completed", "failed"]
    speech: str | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)  # typed by the backend; the worker only speaks
    state_version: int | None = None
    llm_mode: str | None = None
    error: ErrorBody | None = None
    retry_after_ms: int | None = None
    poll_url: str | None = None


class DeliveryRecord(_Response):
    event_id: str
    consumer_id: str
    status: Literal["played", "interrupted", "failed", "expired"]
    detail: str | None = None
    recorded_at: datetime


class Announcement(_Response):
    event_id: str
    sequence: int
    type: str
    priority: Literal["low", "normal", "high", "critical"]
    speech: str
    alert_id: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    deliveries: list[DeliveryRecord] = Field(default_factory=list)


class EventsPage(_Response):
    session_id: str
    events: list[Announcement]
    next_cursor: int
    has_more: bool


DeliveryStatus = Literal["played", "interrupted", "failed", "expired"]


class DeliveryReport(_Request):
    consumer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$")
    status: DeliveryStatus
    detail: str | None = Field(default=None, max_length=500)
