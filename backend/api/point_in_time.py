"""Authenticated point-in-time master-data import and query endpoints."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from backend.data.point_in_time_master import (
    BITEMPORAL_IMPORT_SCHEMA_VERSION,
    PointInTimeConflictError,
    PointInTimeIntegrityError,
    PointInTimeMasterStore,
    PointInTimeValidationError,
)
from backend.data.pit_evidence_governance import (
    PitEvidenceConflictError,
    PitEvidenceGovernance,
    PitEvidenceIntegrityError,
    PitEvidenceStateError,
)
from backend.data.sources.csindex_pit import (
    ArtifactEvidence,
    CsindexEvidenceError,
)
from backend.dependencies import require_permission

router = APIRouter(prefix="/api/data/point-in-time", tags=["Data"])


class PointInTimeSourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=80)
    dataset: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=80)
    evidence_level: str = Field(min_length=1, max_length=80)
    retrieved_at: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    available_at: str | None = None
    revision: int | None = Field(default=None, ge=1)
    supersedes_batch_id: str | None = Field(default=None, max_length=80)


class PointInTimeRecordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security_code: str = Field(pattern=r"^[0-9]{6}$")
    effective_from: str
    effective_to: str
    effective_at: str | None = None
    available_at: str | None = None
    name: str | None = Field(default=None, max_length=160)
    exchange: str | None = Field(default=None, max_length=80)
    listing_status: str | None = Field(default=None, max_length=80)
    member_name: str | None = Field(default=None, max_length=160)
    industry_code: str | None = Field(default=None, max_length=80)
    industry_name: str | None = Field(default=None, max_length=160)


class PointInTimeImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        "point-in-time-master-import/v1",
        "point-in-time-master-import/v2",
    ]
    domain: Literal["security", "index_membership", "industry"]
    scope_id: str = Field(min_length=1, max_length=80)
    evidence_kind: Literal[
        "current_snapshot",
        "effective_dated_history",
    ]
    coverage_from: str
    coverage_to: str
    source: PointInTimeSourceBody
    records: list[PointInTimeRecordBody] = Field(
        min_length=1,
        max_length=100_000,
    )

    @field_validator("scope_id")
    @classmethod
    def reject_path_like_scope(cls, value: str) -> str:
        if "/" in value or "\\" in value or ".." in value:
            raise ValueError("scope_id must be an opaque identifier")
        return value

    @model_validator(mode="after")
    def reject_oversized_import(self) -> "PointInTimeImportBody":
        if self.schema_version == BITEMPORAL_IMPORT_SCHEMA_VERSION:
            if self.source.available_at is None or self.source.revision is None:
                raise ValueError(
                    "v2 source requires available_at and revision"
                )
            if any(
                item.effective_at is None or item.available_at is None
                for item in self.records
            ):
                raise ValueError(
                    "v2 records require effective_at and available_at"
                )
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > 20 * 1024 * 1024:
            raise ValueError("point-in-time import exceeds 20 MiB")
        return self


class GovernedArtifactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal[
        "current_anchor",
        "archive_page",
        "announcement",
        "attachment",
    ]
    url: str = Field(min_length=1, max_length=2048)
    retrieved_at: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_base64: str = Field(min_length=1, max_length=34_952_540)
    announcement_id: str | None = Field(
        default=None,
        pattern=r"^[0-9]{1,20}$",
    )
    published_on: date | None = None
    request_payload_base64: str | None = Field(
        default=None,
        max_length=87_400,
    )
    request_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    def to_evidence(self) -> ArtifactEvidence:
        try:
            payload = base64.b64decode(self.payload_base64, validate=True)
            request_payload = (
                base64.b64decode(
                    self.request_payload_base64,
                    validate=True,
                )
                if self.request_payload_base64 is not None
                else None
            )
        except (binascii.Error, ValueError) as exc:
            raise CsindexEvidenceError(
                "artifact payload encoding is invalid"
            ) from exc
        return ArtifactEvidence(
            role=self.role,
            url=self.url,
            retrieved_at=self.retrieved_at,
            content_sha256=self.content_sha256,
            payload=payload,
            announcement_id=self.announcement_id,
            published_on=self.published_on,
            request_payload=request_payload,
            request_sha256=self.request_sha256,
        )


class GovernedPackageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package: dict[str, Any]

    @model_validator(mode="after")
    def reject_oversized_package(self) -> "GovernedPackageBody":
        encoded = json.dumps(
            self.package,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > 20 * 1024 * 1024:
            raise ValueError("governed package exceeds 20 MiB")
        return self


class GovernedAuxiliaryArtifactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["trading_calendar", "review_decisions"]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_base64: str = Field(min_length=1, max_length=34_952_540)

    def payload(self) -> bytes:
        try:
            return base64.b64decode(self.payload_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "auxiliary artifact payload encoding is invalid"
            ) from exc


class GovernedApprovalAttestationsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["pit-evidence-attestation/v1"]
    all_adjustment_rows_reviewed: Literal[True]
    archive_completeness_reviewed: Literal[True]
    source_terms_acknowledged: Literal[True]
    local_research_only: Literal[True]
    redistribution_not_authorized: Literal[True]


class GovernedDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=500)
    attestations: GovernedApprovalAttestationsBody | None = None

    @model_validator(mode="after")
    def enforce_decision_attestation_boundary(self) -> "GovernedDecisionBody":
        if self.decision == "approved" and self.attestations is None:
            raise ValueError("approval attestations are required")
        if self.decision == "rejected" and self.attestations is not None:
            raise ValueError("rejection must not carry approval attestations")
        return self


class PitAutomationRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


def _store(*, initialize: bool = False) -> PointInTimeMasterStore:
    return PointInTimeMasterStore(initialize=initialize)


def _governance() -> PitEvidenceGovernance:
    return PitEvidenceGovernance()


def _governance_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PitEvidenceConflictError):
        code = "pit_evidence_cas_conflict"
    elif isinstance(exc, PitEvidenceStateError):
        code = "pit_evidence_state_invalid"
    else:
        code = "pit_evidence_integrity_invalid"
    return HTTPException(
        status_code=409,
        detail={"code": code, "message": str(exc)},
    )


@router.post("/imports")
async def import_point_in_time_batch(
    body: PointInTimeImportBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """Atomically import one immutable domain batch.

    Network adapters must first materialize this versioned request and its
    source content digest.  The authenticated research-data administrator
    attests to the supplied source identity and evidence level; the digest
    identifies the upstream payload but does not independently certify it.
    This endpoint never fetches a provider itself.
    """

    if (
        (
            body.domain == "index_membership"
            and body.scope_id
            in {"csi300", "csi500", "csi800", "csi1000"}
        )
        or
        body.source.provider == "csindex_official"
        or body.source.evidence_level == "index_provider_authoritative"
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pit_evidence_governance_required",
                "message": (
                    "official CSI imports require an approved evidence package"
                ),
            },
        )

    try:
        result = _store(initialize=True).import_batch(
            schema_version=body.schema_version,
            domain=body.domain,
            scope_id=body.scope_id,
            evidence_kind=body.evidence_kind,
            coverage_from=body.coverage_from,
            coverage_to=body.coverage_to,
            source=body.source.model_dump(),
            records=[
                record.model_dump(exclude_none=True)
                for record in body.records
            ],
            imported_by_user_id=int(user["id"]),
        )
    except PointInTimeConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_in_time_interval_conflict",
                "message": str(exc),
            },
        ) from exc
    except PointInTimeValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "point_in_time_import_invalid",
                "message": str(exc),
            },
        ) from exc
    except PointInTimeIntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_in_time_integrity_invalid",
                "message": str(exc),
            },
        ) from exc
    return {
        "data": {
            "schema_version": body.schema_version,
            **result,
        }
    }


@router.post("/governance/packages")
async def stage_governed_point_in_time_package(
    body: GovernedPackageBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    try:
        result = _governance().stage_package(
            package=body.package,
            actor_user_id=int(user["id"]),
        )
    except (
        CsindexEvidenceError,
        PitEvidenceIntegrityError,
        PitEvidenceConflictError,
        PitEvidenceStateError,
    ) as exc:
        raise _governance_error(exc) from exc
    return {"data": result}


@router.post("/governance/artifacts")
async def record_governed_point_in_time_artifact(
    body: GovernedArtifactBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    try:
        result = _governance().record_artifact(
            artifact=body.to_evidence(),
            actor_user_id=int(user["id"]),
        )
    except (
        CsindexEvidenceError,
        PitEvidenceIntegrityError,
        PitEvidenceConflictError,
        PitEvidenceStateError,
    ) as exc:
        raise _governance_error(exc) from exc
    return {"data": result}


@router.post("/governance/auxiliary-artifacts")
async def record_governed_point_in_time_auxiliary_artifact(
    body: GovernedAuxiliaryArtifactBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """Record a signed calendar or authenticated reviewer decision file."""

    try:
        result = _governance().record_auxiliary_artifact(
            kind=body.kind,
            payload=body.payload(),
            expected_sha256=body.content_sha256,
            actor_user_id=int(user["id"]),
        )
    except (
        ValueError,
        PitEvidenceIntegrityError,
        PitEvidenceConflictError,
        PitEvidenceStateError,
    ) as exc:
        raise _governance_error(exc) from exc
    return {"data": result}


@router.get("/governance/packages/{package_id}")
async def get_governed_point_in_time_package(
    package_id: str,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    del user
    try:
        result = _governance().get_package(package_id)
    except (
        PitEvidenceIntegrityError,
        PitEvidenceConflictError,
        PitEvidenceStateError,
    ) as exc:
        raise _governance_error(exc) from exc
    return {"data": result}


@router.get("/governance/packages/{package_id}/events")
async def get_governed_point_in_time_events(
    package_id: str,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    del user
    try:
        result = _governance().get_events(package_id)
    except (
        PitEvidenceIntegrityError,
        PitEvidenceConflictError,
        PitEvidenceStateError,
    ) as exc:
        raise _governance_error(exc) from exc
    return {"data": result}


@router.post("/governance/packages/{package_id}/decision")
async def decide_governed_point_in_time_package(
    package_id: str,
    body: GovernedDecisionBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    try:
        result = _governance().decide(
            package_id=package_id,
            expected_revision=body.expected_revision,
            decision=body.decision,
            actor_user_id=int(user["id"]),
            reason=body.reason,
            attestations=(
                body.attestations.model_dump()
                if body.attestations is not None
                else None
            ),
        )
    except (
        PitEvidenceIntegrityError,
        PitEvidenceConflictError,
        PitEvidenceStateError,
    ) as exc:
        raise _governance_error(exc) from exc
    return {"data": result}


@router.post("/governance/packages/{package_id}/import")
async def import_governed_point_in_time_package(
    package_id: str,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    try:
        result = _governance().import_approved_package(
            package_id=package_id,
            actor_user_id=int(user["id"]),
        )
    except (
        PointInTimeConflictError,
        PointInTimeIntegrityError,
        PointInTimeValidationError,
        PitEvidenceIntegrityError,
        PitEvidenceConflictError,
        PitEvidenceStateError,
    ) as exc:
        raise _governance_error(exc) from exc
    return {"data": result}


@router.post("/automation/runs")
async def trigger_pit_automation_run(
    body: PitAutomationRunBody,
    user: dict[str, Any] = Depends(require_permission("data:update")),
) -> dict[str, Any]:
    """Queue an independent PIT state-machine run under its service identity."""

    del user
    from backend.jobs.broker import JobQueueFullError
    from backend.services.pit_automation_scheduler import enqueue_pit_durable_update
    from backend.services.pit_durable_update import (
        PitAutomationIdentityError,
        configured_policy,
    )

    key = body.idempotency_key or f"manual:{uuid.uuid4().hex}"
    try:
        job_uuid = await enqueue_pit_durable_update(idempotency_key=key)
    except PitAutomationIdentityError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "pit_automation_actor_invalid", "message": str(exc)},
        ) from exc
    except JobQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    policy = configured_policy()
    return {
        "data": {
            "job_id": job_uuid,
            "idempotency_key": key,
            "automatic_activation_policy_enabled": bool(
                policy.personal_mode and policy.auto_activate_green
            ),
            "note": "green activation remains subject to configured policy and governance evidence",
        }
    }


@router.get("/automation/runs")
async def inspect_pit_automation_runs(
    limit: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """Return bounded durable stage/retry/lease state for observability."""

    del user
    from backend.services.pit_durable_update import (
        PitDurableUpdateStore,
        configured_policy,
    )

    policy = configured_policy()
    runs = PitDurableUpdateStore().latest(limit)
    return {
        "data": {
            "schema_version": "pit-durable-update/v1",
            "policy_sha256": policy.policy_sha256,
            "personal_mode": policy.personal_mode,
            "auto_activate_green": policy.auto_activate_green,
            "runs": runs,
        }
    }


@router.get("/as-of")
async def query_point_in_time_as_of(
    domain: Literal["security", "index_membership", "industry"],
    scope_id: str = Query(min_length=1, max_length=80),
    date: str = Query(),
    security_code: list[str] = Query(default=[]),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    del user
    try:
        result = _store().query_as_of(
            domain=domain,
            scope_id=scope_id,
            as_of=date,
            security_codes=security_code,
        )
    except PointInTimeValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "point_in_time_query_unavailable",
                "message": str(exc),
            },
        ) from exc
    except PointInTimeIntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_in_time_integrity_invalid",
                "message": str(exc),
            },
        ) from exc
    return {"data": result}

@router.get("/coverage")
async def inspect_point_in_time_coverage(
    pool_id: str = Query(min_length=1, max_length=80),
    start: str = Query(),
    end: str = Query(),
    security_code: list[str] = Query(default=[]),
    industry_scope: str = Query(default="cninfo_008001", max_length=80),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    del user
    try:
        result = _store().inspect_research_coverage(
            pool_id=pool_id,
            security_codes=security_code,
            start=start,
            end=end,
            industry_scope=industry_scope,
        )
    except PointInTimeValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "point_in_time_query_invalid",
                "message": str(exc),
            },
        ) from exc
    except PointInTimeIntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_in_time_integrity_invalid",
                "message": str(exc),
            },
        ) from exc
    return {"data": result}
