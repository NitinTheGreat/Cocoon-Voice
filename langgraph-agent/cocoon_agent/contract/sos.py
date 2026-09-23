"""Proposed man-down check-in and SOS contract. Target stage I14.

"No response" is only meaningful after a check-in was actually presented on some channel. If no channel could
present it, the outcome is `operator_unreachable`, never "ignored" and never "all clear".
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import MachineId, OperatorId, StableId, Strict, UtcTime


class ChannelOffer(Strict):
    channel: Literal["voice", "screen", "vibration"]
    status: Literal["queued", "offered", "presented", "failed", "unavailable"] = Field(
        description="`presented` needs client evidence (voice playback or screen presentation report).")
    reported_at: UtcTime
    evidence_event_id: StableId | None = Field(default=None, description="Delivery/presentation record ID.")

    @model_validator(mode="after")
    def _evidence(self) -> "ChannelOffer":
        if self.status == "presented" and self.evidence_event_id is None:
            raise ValueError("presented needs the delivery/presentation record that proves it")
        return self


class CheckInResponse(Strict):
    response_id: StableId
    response: Literal["ok", "need_help"]
    channel: Literal["voice", "screen"]
    responded_at: UtcTime
    late: bool = Field(description="True if recorded after the deadline; recorded as an update, never erasing an "
                                   "already issued alert.")


class SupervisorNotification(Strict):
    notification_id: StableId
    policy_id: Literal["preauthorised_emergency_in_app.v1"] = Field(
        description="Documented preauthorised in-app emergency notification. Not SMS/call/external dispatch.")
    reason: Literal["no_response", "operator_unreachable", "help_requested"]
    created_at: UtcTime
    delivery_status: Literal["created", "delivered_to_feed", "acknowledged"]


class SosEpisode(Strict):
    episode_id: StableId
    impact_observation_id: StableId = Field(description="A human_impact observation; never a machine control count.")
    operator_id: OperatorId
    machine_id: MachineId | None = None
    state: Literal["candidate", "check_in_queued", "check_in_offered", "awaiting_response", "responded_ok",
                   "help_requested", "unresolved_no_response", "operator_unreachable", "supervisor_notified",
                   "resolved"]
    check_in_id: StableId
    offers: list[ChannelOffer]
    response_deadline_at: UtcTime | None = Field(
        default=None, description="Starts only when an offer is presented; persisted so restarts resume it.")
    response: CheckInResponse | None = None
    notification: SupervisorNotification | None = None
    created_at: UtcTime

    @model_validator(mode="after")
    def _consistent(self) -> "SosEpisode":
        presented = any(o.status == "presented" for o in self.offers)
        if self.response_deadline_at is not None and not presented:
            raise ValueError("the response window starts only after a presented offer")
        if self.state == "unresolved_no_response" and not presented:
            raise ValueError("no-response requires a presented check-in; otherwise the state is operator_unreachable")
        if self.state == "operator_unreachable" and presented:
            raise ValueError("an operator shown the check-in is not unreachable")
        if self.state in ("responded_ok", "help_requested") and self.response is None:
            raise ValueError("a response state needs the recorded response")
        if self.state == "supervisor_notified" and self.notification is None:
            raise ValueError("supervisor_notified needs its notification record")
        return self


class SosResponsePayload(Strict):
    """Payload of the `sos.respond` command. Resolves only the named episode/check-in, never a newer one."""

    episode_id: StableId
    check_in_id: StableId
    response: Literal["ok", "need_help"]
