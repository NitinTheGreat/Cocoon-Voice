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
    """POST /v1/sessions body. Since I02a the runtime model already carries everything targeted here: catalog
    admission (unknown_machine / unknown_operator) and optional site_id/shift_id checked against trusted bindings.
    Kept as a named target so the proposed spec and fixtures stay stable."""


class SessionTarget(s.Session):
    """Additive target of the session resource. site/shift, dataset_manifest_sha256 and binding status are
    implemented since I02a (inherited from the runtime model); these fields remain proposed."""

    machine_model: str | None = Field(default=None, max_length=64, examples=["Cat 320"])
    service_date: date | None = Field(default=None, description="Site-local date the shift belongs to.")
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
