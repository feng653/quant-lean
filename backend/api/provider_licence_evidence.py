"""Administrator-only provider licence/archive evidence metadata API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.config import settings
from backend.data.provider_licence_evidence import (
    LicenceEvidenceConflict,
    LicenceEvidenceError,
    LicenceEvidenceValidationError,
    ProviderLicenceEvidenceRegistry,
)
from backend.dependencies import require_permission


router = APIRouter(
    prefix="/api/data/provider-licence-evidence",
    tags=["Data"],
)


class LicenceEvidenceRegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1, max_length=128)
    source_scope: str = Field(min_length=1, max_length=128)
    licence_scope: str = Field(min_length=1, max_length=128)
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_size_bytes: int | None = Field(
        default=None,
        ge=1,
        le=1024 * 1024 * 1024,
    )
    # This value is immediately reduced to a safe fingerprint. It is never
    # echoed or retained as a path/URL by the service.
    document_reference: str | None = Field(default=None, max_length=4096)
    claimed_effective_from: str
    claimed_effective_to: str
    claimed_available_from: str
    claimed_available_to: str
    obtained_at: datetime


class LicenceEvidenceReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approved", "rejected"]
    reason_code: str = Field(min_length=1, max_length=128)


def _registry() -> ProviderLicenceEvidenceRegistry:
    return ProviderLicenceEvidenceRegistry(
        settings.abs_path(settings.PIT_LICENCE_EVIDENCE_DB)
    )


def _raise_api_error(exc: Exception) -> None:
    if isinstance(exc, LicenceEvidenceConflict):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "licence_evidence_immutable_conflict",
                "message": str(exc),
            },
        ) from exc
    if isinstance(exc, LicenceEvidenceValidationError):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "licence_evidence_contract_invalid",
                "message": str(exc),
            },
        ) from exc
    raise HTTPException(
        status_code=409,
        detail={
            "code": "licence_evidence_integrity_invalid",
            "message": "许可/留存证据登记完整性验证失败",
        },
    ) from exc


@router.get("/contract")
async def get_licence_evidence_contract(
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    del user
    return {
        "data": {
            "schema_version": "provider-licence-evidence-api/v1",
            "administrator_permission": "admin:users",
            "stored_document_contents": False,
            "stored_raw_reference": False,
            "append_only": True,
            "independent_review_required": True,
            "states": ["unverified", "approved", "rejected"],
            "production_release_authorized": False,
            "remaining_release_requirements": [
                "approved_signed_provider_artifacts",
                "independent_official_reconciliation",
                "production_release_dry_run_without_blockers",
            ],
        }
    }


@router.post("/records", status_code=201)
async def register_licence_evidence(
    body: LicenceEvidenceRegisterBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    try:
        result = _registry().register(
            **body.model_dump(mode="json"),
            actor_user_id=int(user["id"]),
        )
    except LicenceEvidenceError as exc:
        _raise_api_error(exc)
    return {"data": result}


@router.post("/records/{record_sha256}/reviews")
async def review_licence_evidence(
    record_sha256: str,
    body: LicenceEvidenceReviewBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    try:
        result = _registry().review(
            record_sha256=record_sha256,
            **body.model_dump(),
            reviewer_user_id=int(user["id"]),
        )
    except LicenceEvidenceError as exc:
        _raise_api_error(exc)
    return {"data": result}


@router.get("/records")
async def list_licence_evidence(
    provider_id: str | None = Query(default=None, max_length=128),
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    del user
    try:
        records = _registry().list(provider_id=provider_id)
    except LicenceEvidenceError as exc:
        _raise_api_error(exc)
    return {
        "data": {
            "items": records,
            "total": len(records),
            "production_release_authorized": False,
        }
    }


@router.get("/records/{record_sha256}")
async def get_licence_evidence(
    record_sha256: str,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    del user
    try:
        result = _registry().get(record_sha256)
    except LicenceEvidenceError as exc:
        _raise_api_error(exc)
    return {"data": result}
