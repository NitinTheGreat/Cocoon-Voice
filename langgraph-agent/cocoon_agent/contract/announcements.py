"""Proposed independent announcement contract (`cocoon.announcements.v1`). Target stages I08A/I11.

Polling (existing GET .../events) and SSE (proposed GET .../events/stream) read the same persisted order.
Announcements never require a turn.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ..api import schemas as s
from .common import Origin, StableId, Strict, UtcTime
from .turns import PositionConfidence

ANNOUNCEMENT_SCHEMA = "cocoon.announcements.v1"

AnnouncementType = Literal[
    # existing v1 values, unchanged
    "alert_started", "alert_cleared",
    # additive target values (the worker treats type as opaque)
    "shift_briefing", "condition_advisory", "sos_check_in", "lesson_assigned", "schedule_updated",
]


class AnnouncementCorrelation(Strict):
    """Links the spoken text to the persisted record that justified it. At least one reference is required."""

    alert_id: StableId | None = None
    episode_id: StableId | None = None
    rule_id: str | None = Field(default=None, max_length=128)
    rule_version: str | None = Field(default=None, max_length=32)
    briefing_id: StableId | None = None
    check_in_id: StableId | None = None
    assignment_id: StableId | None = None
    approval_id: StableId | None = None

    @model_validator(mode="after")
    def _some_reference(self) -> "AnnouncementCorrelation":
        if not any(v is not None for v in self.model_dump().values()):
            raise ValueError("an announcement must reference the persisted record behind it")
        if (self.rule_id is None) != (self.rule_version is None):
            raise ValueError("rule_id and rule_version go together")
        return self


class AnnouncementProvenance(Strict):
    origin: Origin
    is_simulated: bool
    observed_at: UtcTime | None = Field(default=None, description="Original observation time of the trigger.")


class AnnouncementEnvelope(Strict):
    """One `cocoon.announcements.v1` event, identical over polling and SSE. SSE `id` equals `event_id`."""

    schema_version: Literal["cocoon.announcements.v1"]
    session_id: StableId
    event_id: StableId = Field(description="Stable; the delivery key across polling and streaming.")
    sequence: int = Field(ge=1, description="Monotonic per session; the exclusive cursor for ?after=.")
    type: AnnouncementType
    priority: Literal["low", "normal", "high", "critical"]
    speech: str = Field(min_length=1, max_length=600, description="Approved template or persisted explanation.")
    speakable: bool = Field(description="False once expired. Clients never announce an unspeakable event as current.")
    created_at: UtcTime
    expires_at: UtcTime
    correlation: AnnouncementCorrelation
    provenance: AnnouncementProvenance

    @model_validator(mode="after")
    def _times(self) -> "AnnouncementEnvelope":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self


class AnnouncementTarget(s.Announcement):
    """Additive target of one polled announcement (existing GET .../events). v1 fields keep their meaning; the new
    fields let a polling client read the same identity, expiry and correlation as the SSE envelope."""

    type: AnnouncementType
    schema_version: Literal["cocoon.announcements.v1"] | None = None
    speakable: bool | None = None
    correlation: AnnouncementCorrelation | None = None
    provenance: AnnouncementProvenance | None = None


class EventsPageTarget(s.EventsPage):
    """Additive target of the existing polling page. Same exclusive `after` cursor and non-destructive reads."""

    events: list[AnnouncementTarget]


class AnnouncementDeliveryReportTarget(s.DeliveryReport):
    """Additive target body for the existing POST .../events/{event_id}/delivery.

    `consumer_id`, `status` and `detail` keep their v1 meaning. `delivery_id` is OPTIONAL for compatibility:
    without it the v1 rule (one record per consumer, last write wins) still applies; with it, attempts become
    immutable and idempotent exactly as for turn delivery."""

    delivery_id: StableId | None = None
    position_confidence: PositionConfidence | None = None
