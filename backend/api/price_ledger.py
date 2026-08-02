"""Authenticated dual-price-ledger import, readiness and query endpoints."""

from __future__ import annotations

import asyncio
from datetime import date
import hashlib
import json
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.data.cache import DataCache
from backend.data.price_cache_audit import (
    DEFAULT_AUDIT_SCOPES,
    audit_legacy_price_caches,
)
from backend.data.price_ledger import (
    IMPORT_SCHEMA_VERSION,
    PriceLedgerConflictError,
    PriceLedgerIntegrityError,
    PriceLedgerStore,
    PriceLedgerValidationError,
)
from backend.dependencies import require_permission

router = APIRouter(prefix="/api/data/price-ledger", tags=["Data"])


class PriceLedgerSourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=80)
    dataset: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=80)
    adjustment: Literal["raw", "hfq", "corporate_action"]
    evidence_level: Literal[
        "declared",
        "public_cross_validated",
        "licensed",
        "exchange_authoritative",
    ]
    retrieved_at: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    available_at: str | None = None


class PriceLedgerPriceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security_code: str = Field(pattern=r"^[0-9]{6}$")
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class CorporateActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    security_code: str = Field(pattern=r"^[0-9]{6}$")
    effective_date: str
    action_type: Literal[
        "cash_dividend",
        "split",
        "bonus",
        "rights_issue",
        "merger",
        "other",
    ]
    adjustment_multiplier: float | None = Field(default=None, gt=0)
    reference_id: str = Field(min_length=1, max_length=80)


class PriceLedgerImportBody(BaseModel):
    """One atomic bundle; partial raw/research imports are not accepted."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        "dual-price-ledger-import/v1",
        "dual-price-ledger-import/v2",
    ]
    scope_id: str = Field(min_length=1, max_length=80)
    coverage_from: str
    coverage_to: str
    raw_source: PriceLedgerSourceBody
    research_source: PriceLedgerSourceBody
    corporate_action_source: PriceLedgerSourceBody | None = None
    raw_prices: list[PriceLedgerPriceBody] = Field(
        min_length=1,
        max_length=20_000,
    )
    research_prices: list[PriceLedgerPriceBody] = Field(
        min_length=1,
        max_length=20_000,
    )
    corporate_actions: list[CorporateActionBody] = Field(
        default_factory=list,
        max_length=5_000,
    )
    revision: int | None = Field(default=None, ge=1)
    supersedes_batch_id: str | None = Field(default=None, max_length=80)


class CorporateActionEvidenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_id: str = Field(min_length=1, max_length=80)
    security_code: str = Field(pattern=r"^[0-9]{6}$")
    evidence_kind: Literal["event", "confirmed_no_event"]
    effective_at: str
    effective_to: str | None = None
    available_at: str
    revision: int = Field(ge=1)
    supersedes_evidence_id: str | None = Field(default=None, max_length=80)
    action_type: Literal[
        "cash_dividend",
        "split",
        "bonus",
        "rights_issue",
        "merger",
        "other",
    ] | None = None
    adjustment_multiplier: float | None = Field(default=None, gt=0)
    reference_id: str = Field(min_length=1, max_length=80)
    source: PriceLedgerSourceBody


_LEGACY_AUDIT_CACHE_SECONDS = 60.0
_legacy_audit_lock = asyncio.Lock()
_legacy_audit_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_CROSS_SCOPE_AUDIT_CACHE_SECONDS = 60.0
_cross_scope_audit_lock = asyncio.Lock()
_cross_scope_audit_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _store(*, initialize: bool = False) -> PriceLedgerStore:
    return PriceLedgerStore(initialize=initialize)


def _raise_api_error(exc: Exception) -> None:
    if isinstance(exc, PriceLedgerConflictError):
        evidence = exc.evidence
        raise HTTPException(
            status_code=409,
            detail={
                "code": "price_ledger_immutable_conflict",
                "message": "价格账本身份与已认证的不可变批次冲突",
                **({"evidence": evidence} if evidence else {}),
            },
        ) from exc
    if isinstance(exc, PriceLedgerIntegrityError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "price_ledger_integrity_invalid",
                "message": "价格账本完整性验证失败",
            },
        ) from exc
    raise HTTPException(
        status_code=422,
        detail={
            "code": "price_ledger_contract_invalid",
            "message": str(exc),
        },
    ) from exc


@router.get("/import-contract")
async def get_price_ledger_import_contract(
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """Describe the append-only import contract without storage details."""

    del user
    return {
        "data": {
            "schema_version": IMPORT_SCHEMA_VERSION,
            "required_price_roles": {
                "raw_execution": {
                    "adjustment": "raw",
                    "fields": ["open", "high", "low", "close", "volume"],
                    "usage": "execution_and_fill_prices_only",
                },
                "research_adjusted": {
                    "adjustment": "hfq",
                    "fields": ["open", "high", "low", "close", "volume"],
                    "usage": "return_and_factor_research_only",
                },
            },
            "identity": [
                "security_code",
                "date",
                "source_provider",
                "source_dataset",
                "source_version",
                "adjustment",
            ],
            "scope_semantics": (
                "scope_id binds usage/coverage only and is not part of the "
                "canonical security price identity"
            ),
            "anchor_semantics": {
                "raw": "unadjusted_exchange_price",
                "hfq": "hfq_absolute_anchor_bound_to_source_version",
            },
            "atomic": True,
            "immutable_after_acceptance": True,
            "administrator_permission": "admin:users",
            "quality_gates": [
                "complete_matching_raw_hfq_code_date_identity",
                "finite_positive_ohlc_and_nonnegative_volume",
                "valid_ohlc_relationship",
                "same_day_ohlc_implied_factor_consistency",
                "positive_adjustment_factor",
                "authoritative_evidence_for_abnormal_factor_jump",
                "cross_scope_canonical_price_consistency",
                "hfq_absolute_anchor_and_return_geometry_separation",
            ],
            "limitations": [
                "Only declared evidence can be staged through the direct "
                "endpoint. Public-cross-validated, licensed and "
                "exchange-authoritative levels remain blocked until a "
                "managed artifact/review/receipt workflow exists.",
                "The synchronous API accepts at most 20,000 rows per role; "
                "larger PIT-union imports use the checkpointed backfill job.",
                "Real tuning and execution simulation require raw, hfq and "
                "authoritative corporate-action evidence together.",
            ],
        }
    }


@router.post("/imports")
async def import_price_ledger_batch(
    body: PriceLedgerImportBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """Validate, audit and atomically build one immutable ledger batch.

    Operators with only ``data:update`` cannot import.  An administrator may
    stage declared evidence only; stronger trust levels remain blocked until
    an artifact-bound approval workflow exists.  This endpoint performs no
    network access and returns no filesystem paths.
    """

    if any(
        source is not None and source.evidence_level != "declared"
        for source in (
            body.raw_source,
            body.research_source,
            body.corporate_action_source,
        )
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "price_evidence_governance_required",
                "message": (
                    "non-declared price evidence requires an approved managed "
                    "artifact receipt; that workflow is not available"
                ),
            },
        )
    try:
        result = await asyncio.to_thread(
            _store(initialize=True).import_batch,
            schema_version=body.schema_version,
            scope_id=body.scope_id,
            coverage_from=body.coverage_from,
            coverage_to=body.coverage_to,
            raw_source=body.raw_source.model_dump(),
            research_source=body.research_source.model_dump(),
            corporate_action_source=(
                body.corporate_action_source.model_dump()
                if body.corporate_action_source is not None
                else None
            ),
            raw_prices=[item.model_dump() for item in body.raw_prices],
            research_prices=[
                item.model_dump() for item in body.research_prices
            ],
            corporate_actions=[
                item.model_dump() for item in body.corporate_actions
            ],
            imported_by_user_id=int(user["id"]),
            revision=body.revision,
            supersedes_batch_id=body.supersedes_batch_id,
        )
    except (
        PriceLedgerConflictError,
        PriceLedgerIntegrityError,
        PriceLedgerValidationError,
    ) as exc:
        _raise_api_error(exc)
    return {"data": {"schema_version": body.schema_version, **result}}


@router.post("/corporate-action-evidence")
async def import_corporate_action_evidence(
    body: CorporateActionEvidenceBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """Stage declared event/no-event evidence; stronger levels stay governed."""

    if body.source.evidence_level != "declared":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "price_evidence_governance_required",
                "message": "non-declared evidence requires a managed artifact receipt",
            },
        )
    try:
        result = await asyncio.to_thread(
            _store(initialize=True).import_corporate_action_evidence,
            **body.model_dump(),
            imported_by_user_id=int(user["id"]),
        )
    except (
        PriceLedgerConflictError,
        PriceLedgerIntegrityError,
        PriceLedgerValidationError,
    ) as exc:
        _raise_api_error(exc)
    return {"data": result}


@router.get("/corporate-action-evidence")
async def query_corporate_action_evidence(
    scope_id: str = Query(min_length=1, max_length=80),
    security_code: str = Query(pattern=r"^[0-9]{6}$"),
    effective_start: str = Query(),
    effective_end: str = Query(),
    as_known_at: str = Query(),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    del user
    try:
        result = await asyncio.to_thread(
            _store().query_corporate_action_evidence_as_known,
            scope_id=scope_id,
            security_code=security_code,
            effective_start=effective_start,
            effective_end=effective_end,
            as_known_at=as_known_at,
        )
    except (PriceLedgerIntegrityError, PriceLedgerValidationError) as exc:
        _raise_api_error(exc)
    return {"data": result}


@router.get("/readiness")
async def inspect_price_ledger_readiness(
    scope_id: str = Query(min_length=1, max_length=80),
    start: str = Query(),
    end: str = Query(),
    security_code: list[str] = Query(default=[]),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    del user
    try:
        result = await asyncio.to_thread(
            _store().inspect_readiness,
            scope_id=scope_id,
            start=start,
            end=end,
            security_codes=security_code,
        )
    except (PriceLedgerIntegrityError, PriceLedgerValidationError) as exc:
        _raise_api_error(exc)
    return {"data": result}


@router.get("/runtime-bindings/{binding_id}/validate")
async def validate_price_runtime_binding(
    binding_id: str,
    expected_scope_id: str | None = Query(default=None, max_length=80),
    expected_binding_digest: str | None = Query(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    ),
    require_bitemporal: bool = Query(default=True),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    del user
    try:
        result = await asyncio.to_thread(
            _store().validate_runtime_binding,
            binding_id=binding_id,
            expected_scope_id=expected_scope_id,
            expected_binding_digest=expected_binding_digest,
            require_bitemporal=require_bitemporal,
        )
    except (
        PriceLedgerConflictError,
        PriceLedgerIntegrityError,
        PriceLedgerValidationError,
    ) as exc:
        _raise_api_error(exc)
    return {"data": result}


@router.get("/cross-scope-audit")
async def audit_cross_scope_price_consistency(
    start: str = Query(),
    end: str = Query(),
    security_code: list[str] = Query(default=[]),
    limit: int = Query(default=1_000, ge=1, le=10_000),
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """Report canonical price conflicts without modifying legacy evidence."""

    del user
    try:
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
    except ValueError as exc:
        _raise_api_error(
            PriceLedgerValidationError("audit dates must be ISO dates")
        )
        raise AssertionError("unreachable") from exc
    if end_day < start_day or (end_day - start_day).days > 5_500:
        _raise_api_error(
            PriceLedgerValidationError(
                "audit range must be ordered and no longer than 5500 days"
            )
        )
    if len(security_code) > 1_000:
        _raise_api_error(
            PriceLedgerValidationError(
                "audit accepts at most 1000 security codes"
            )
        )
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "start": start,
                "end": end,
                "security_codes": sorted(set(security_code)),
                "limit": limit,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    async with _cross_scope_audit_lock:
        cached = _cross_scope_audit_cache.get(cache_key)
        if (
            cached is not None
            and time.monotonic() - cached[0]
            <= _CROSS_SCOPE_AUDIT_CACHE_SECONDS
        ):
            result = cached[1]
        else:
            try:
                result = await asyncio.to_thread(
                    _store().audit_cross_scope_consistency,
                    start=start,
                    end=end,
                    security_codes=security_code,
                    limit=limit,
                )
            except (
                PriceLedgerIntegrityError,
                PriceLedgerValidationError,
            ) as exc:
                _raise_api_error(exc)
            _cross_scope_audit_cache.clear()
            _cross_scope_audit_cache[cache_key] = (
                time.monotonic(),
                result,
            )
    return {"data": result}


@router.get("/legacy-cache-audit")
async def audit_legacy_cross_pool_caches(
    start: str = Query(),
    end: str = Query(),
    scope_id: list[str] = Query(default=list(DEFAULT_AUDIT_SCOPES)),
    security_code: list[str] = Query(default=[]),
    limit: int = Query(default=1_000, ge=1, le=10_000),
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    """Audit current Parquet caches without fetching or rewriting them."""

    del user
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "start": start,
                "end": end,
                "scope_id": sorted(scope_id),
                "security_code": sorted(security_code),
                "limit": limit,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    now = time.monotonic()
    cached = _legacy_audit_cache.get(cache_key)
    if cached is not None and now - cached[0] <= _LEGACY_AUDIT_CACHE_SECONDS:
        return {"data": cached[1], "cached": True}
    try:
        async with _legacy_audit_lock:
            cached = _legacy_audit_cache.get(cache_key)
            now = time.monotonic()
            if (
                cached is not None
                and now - cached[0] <= _LEGACY_AUDIT_CACHE_SECONDS
            ):
                return {"data": cached[1], "cached": True}
            result = await audit_legacy_price_caches(
                DataCache(),
                start=start,
                end=end,
                scope_ids=scope_id,
                security_codes=security_code,
                limit=limit,
            )
            _legacy_audit_cache.clear()
            _legacy_audit_cache[cache_key] = (time.monotonic(), result)
    except ValueError as exc:
        _raise_api_error(PriceLedgerValidationError(str(exc)))
    return {"data": result, "cached": False}


@router.get("/prices")
async def query_price_ledger(
    scope_id: str = Query(min_length=1, max_length=80),
    role: Literal["raw_execution", "research_adjusted"] = Query(),
    start: str = Query(),
    end: str = Query(),
    security_code: list[str] = Query(default=[]),
    limit: int = Query(default=10_000, ge=1, le=100_000),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    del user
    try:
        result = await asyncio.to_thread(
            _store().query_prices,
            scope_id=scope_id,
            role=role,
            start=start,
            end=end,
            security_codes=security_code,
            limit=limit,
        )
    except (PriceLedgerIntegrityError, PriceLedgerValidationError) as exc:
        _raise_api_error(exc)
    return {"data": result}
