"""Proposed identity, session binding and consent contract. Target stage I02 (auth, bindings, consent)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from ..api import schemas as s
from .common import MachineId, OperatorId, PageInfo, ShiftId, SiteId, StableId, Strict, UtcTime

PrincipalKind = Literal["service", "operator", "supervisor", "simulator"]
"""Resolved by the server from the bearer token. Never taken from a body field, utterance, consumer_id,
room name or client metadata."""


class SessionAssociation(Strict):
    operator_id: OperatorId
    machine_id: MachineId
    site_id: SiteId | None = None
    shift_id: ShiftId | None = None


class MeResponse(Strict):
    """GET /v1/me. Never echoes the bearer token or its hash."""

    subject_id: StableId
    principal_kind: PrincipalKind
    display_name: str | None = Field(default=None, max_length=128)
    site_ids: list[SiteId] = Field(description="Sites this principal may read. Empty for the global service.")
    allowed_associations: list[SessionAssociation] = Field(
        description="Operator: own operator/machine/shift bindings. Supervisor: none (supervisors never bind to "
                    "an operator session).")
    token_expires_at: UtcTime | None = Field(default=None, description="Null for the configured service token.")


class SessionCreateRequestTarget(s.SessionCreateRequest):
    """Additive target of POST /v1/sessions. The five v1 fields are unchanged; site/shift are optional.

    Target validation (I02, with the I03 catalog): unknown machine_id → 422 `unknown_machine`; unknown
    operator_id → 422 `unknown_operator`. The v1 runtime accepts any non-empty ID with 201. That is a recorded
    gap and an intentional, documented tightening, not the intended behaviour."""

    site_id: SiteId | None = None
    shift_id: ShiftId | None = None


class SessionTarget(s.Session):
    """Additive target of the session resource."""

    machine_model: str | None = Field(default=None, max_length=64, examples=["Cat 320"])
    site_id: SiteId | None = None
    shift_id: ShiftId | None = None
    service_date: date | None = Field(default=None, description="Site-local date the shift belongs to.")
    dataset_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    clock_mode: Literal["live", "replay"] | None = None


# --------------------------------------------------------------------------- consent

ConsentPurpose = Literal[
    "vitals_processing",          # receive and process the operator's own vitals for their own advice
    "risk_sharing_supervisor",    # share a derived risk level (never raw values) with scoped supervisors
    "camera_drowsiness",          # roadmap R01; recorded separately so it can never piggy-back
]


class ConsentRecord(Strict):
    purpose: ConsentPurpose
    status: Literal["granted", "revoked", "not_set"]
    notice_version: str | None = Field(default=None, max_length=32)
    effective_at: UtcTime | None = None
    revoked_at: UtcTime | None = None
    is_synthetic_demo_record: bool = Field(
        description="True for demo consents. A synthetic record is never permission for a real person.")

    @model_validator(mode="after")
    def _consistent(self) -> "ConsentRecord":
        if self.status == "granted" and (self.effective_at is None or self.notice_version is None):
            raise ValueError("a grant needs effective_at and notice_version")
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("a revocation needs revoked_at")
        if self.status != "revoked" and self.revoked_at is not None:
            raise ValueError("revoked_at is only set on a revoked record")
        return self


class ConsentState(Strict):
    """GET /v1/operators/{operator_id}/consents. Readable by the operator or a narrowly scoped service."""

    operator_id: OperatorId
    version: int = Field(ge=0, description="Increments on every grant/revocation; use as expected_version.")
    consents: list[ConsentRecord]
    history_page: PageInfo | None = None


class ConsentChangeRequest(Strict):
    """POST /v1/operators/{operator_id}/consents. Operator principal only; a supervisor cannot consent for them."""

    change_id: StableId
    purpose: ConsentPurpose
    action: Literal["grant", "revoke"]
    notice_version: str = Field(min_length=1, max_length=32)
    expected_version: int = Field(ge=0)


class ConsentChangeResult(Strict):
    change_id: StableId
    applied: bool = Field(description="False when an identical earlier change already produced this state.")
    state: ConsentState
