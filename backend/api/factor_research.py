"""Audited factor research endpoints with durable, user-isolated evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.config import settings
from backend.data.cache import DataCache, LegacyAdjustedCacheError
from backend.data.factor_governance import (
    FactorGovernanceError,
    FactorGovernanceStore,
)
from backend.data.factor_research_runs import FactorResearchRunStore
from backend.data.point_in_time_master import (
    PointInTimeIntegrityError,
    PointInTimeMasterStore,
    PointInTimeValidationError,
)
from backend.data.price_ledger import (
    PriceLedgerIntegrityError,
    PriceLedgerStore,
    PriceLedgerValidationError,
)
from backend.dependencies import get_job_broker, require_permission
from backend.jobs.broker import JobQueueFullError
from backend.research.factor_catalog import FACTOR_CATALOG
from backend.services.factor_neutralization import inspect_size_capability
from backend.services.factor_research import (
    RESEARCH_TRUST,
    FactorResearchBody,
    FactorResearchExecutionError,
    execute_factor_research,
    factor_cache_key,
)
from backend.services.factor_evidence_export import (
    JSON_MEDIA_TYPE,
    ZIP_MEDIA_TYPE,
    FactorEvidenceExportError,
    build_factor_csv_zip,
    prepare_factor_evidence,
    stream_binary_file,
    stream_factor_json,
)
from backend.strategies.factor._configured_factor import (
    make_factor_strategy_class,
)
from backend.strategies.registry import get_registry

router = APIRouter(prefix="/api/factor-research", tags=["Factor Research"])

_FACTOR_IDS = {str(item["factor_id"]) for item in FACTOR_CATALOG}
_SAFE_CACHE_KEY = re.compile(r"(?:csi(?:300|500|800|1000)|all_a|custom_[0-9a-f]{16})")


class FactorComponentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factor_id: str
    weight: float = Field(gt=0, le=100)


class ExportFactorStrategyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=80)
    components: list[FactorComponentBody] = Field(min_length=1, max_length=20)
    top_k_pct: float = Field(default=0.1, gt=0, le=1)
    research_run_ids: list[str] = Field(min_length=1, max_length=20)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    strategy_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_components(self) -> "ExportFactorStrategyBody":
        ids = [item.factor_id for item in self.components]
        if len(set(ids)) != len(ids) or any(item not in _FACTOR_IDS for item in ids):
            raise ValueError("components 包含未知或重复因子")
        if len(set(self.research_run_ids)) != len(self.research_run_ids) or any(
            not re.fullmatch(r"frun_[0-9a-f]{32}", item)
            for item in self.research_run_ids
        ):
            raise ValueError("research_run_ids 包含无效或重复标识")
        if self.strategy_id is not None and not re.fullmatch(
            r"factor_combo_[0-9a-f]{12}", self.strategy_id
        ):
            raise ValueError("strategy_id 无效")
        if (self.strategy_id is None) != (self.expected_version is None):
            raise ValueError("发布新版本须同时提供 strategy_id 和 expected_version")
        return self


class FactorLifecycleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class FactorStrategyRollbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class CompareFactorRunsBody(BaseModel):
    run_ids: list[str] = Field(min_length=2, max_length=20)

    @model_validator(mode="after")
    def validate_run_ids(self) -> "CompareFactorRunsBody":
        if len(set(self.run_ids)) != len(self.run_ids) or any(
            not re.fullmatch(r"frun_[0-9a-f]{32}", item)
            for item in self.run_ids
        ):
            raise ValueError("run_ids 包含无效或重复标识")
        return self


def _run_store() -> FactorResearchRunStore:
    return FactorResearchRunStore()


def _governance_store() -> FactorGovernanceStore:
    return FactorGovernanceStore()


def _governance_http_error(exc: FactorGovernanceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


def _candidate_cache_keys() -> list[str]:
    known = ["csi300", "csi500", "csi800", "csi1000", "all_a"]
    daily = settings.abs_path(settings.DATA_CACHE_DIR) / "daily"
    if daily.exists():
        for path in daily.glob("custom_*.parquet"):
            if (
                path.is_file()
                and path.parent == daily
                and _SAFE_CACHE_KEY.fullmatch(path.stem)
            ):
                known.append(path.stem)
    return list(dict.fromkeys(known))


async def _cache_capability(cache: DataCache, cache_key: str) -> dict[str, Any]:
    info = await cache.get_cache_info(cache_key)
    fields = {str(item) for item in info.get("fields") or []}
    trust = str(info.get("source_trust") or "unverified")
    price_adjustment = str(info.get("price_adjustment") or "").lower()
    schema_ready = info.get("schema_version") == 4
    reason: str | None = None
    if not info.get("exists"):
        reason = "daily_cache_missing"
    elif not schema_ready:
        reason = "legacy_or_unverified_schema"
    elif trust not in RESEARCH_TRUST:
        reason = "source_not_trusted_for_research"
    elif price_adjustment and price_adjustment not in {"qfq", "hfq"}:
        reason = "adjusted_research_price_missing"
    elif "close" not in fields:
        reason = "required_price_field_missing"

    provenance = (
        info.get("source_provenance")
        if isinstance(info.get("source_provenance"), dict)
        else {}
    )
    actual_codes: list[str] = []
    if reason is None:
        try:
            frame, provenance_value = await cache.load_pivot_with_provenance(
                cache_key
            )
            provenance = provenance_value or provenance
            if frame is None or frame.empty:
                reason = "daily_cache_missing_or_invalid"
            elif getattr(frame.columns, "nlevels", 1) > 1:
                actual_codes = sorted(
                    {
                        str(column[0]).strip()
                        for column in frame.columns
                        if str(column[0]).strip()
                    }
                )
        except LegacyAdjustedCacheError:
            reason = "legacy_or_unverified_schema"
        except Exception:
            reason = "daily_cache_integrity_invalid"

    pit_coverage: dict[str, Any] = {
        "schema_version": "point-in-time-readiness/v1",
        "ready": False,
        "universe": {
            "ready": False,
            "reason": "price_cache_unavailable",
        },
        "security_master": {
            "ready": False,
            "reason": "price_cache_unavailable",
        },
        "industry": {
            "ready": False,
            "neutralization_ready": False,
            "reason": "price_cache_unavailable",
            "scope_id": "cninfo_008001",
        },
        "limitations": ["price_cache_unavailable"],
    }
    if (
        reason is None
        and actual_codes
        and info.get("date_start")
        and info.get("date_end")
    ):
        try:
            pit_coverage = PointInTimeMasterStore().inspect_research_coverage(
                pool_id=cache_key,
                security_codes=actual_codes,
                start=str(info["date_start"]),
                end=str(info["date_end"]),
            )
        except (PointInTimeIntegrityError, PointInTimeValidationError) as exc:
            pit_reason = (
                "point_in_time_integrity_invalid"
                if isinstance(exc, PointInTimeIntegrityError)
                else "point_in_time_identity_invalid"
            )
            pit_coverage = {
                **pit_coverage,
                "universe": {
                    "ready": False,
                    "reason": pit_reason,
                },
                "security_master": {
                    "ready": False,
                    "reason": pit_reason,
                },
                "industry": {
                    "ready": False,
                    "neutralization_ready": False,
                    "reason": pit_reason,
                    "scope_id": "cninfo_008001",
                },
                "limitations": [pit_reason],
            }

    factor_ids = [
        str(item["factor_id"])
        for item in FACTOR_CATALOG
        if set(item.get("required_fields") or ["close"]) <= fields
    ]
    size_capability = inspect_size_capability(sorted(fields), provenance)
    industry_ready = bool(
        pit_coverage.get("industry", {}).get("neutralization_ready")
    )
    industry_reason = pit_coverage.get("industry", {}).get("reason")
    neutralization_modes = {
        "none": {"ready": reason is None, "reason": reason},
        "industry": {
            "ready": reason is None and industry_ready,
            "reason": reason or (None if industry_ready else industry_reason),
        },
        "size": {
            "ready": reason is None and size_capability["ready"],
            "reason": reason or size_capability["reason"],
        },
        "industry+size": {
            "ready": (
                reason is None
                and industry_ready
                and size_capability["ready"]
            ),
            "reason": (
                reason
                or (None if industry_ready else industry_reason)
                or size_capability["reason"]
            ),
        },
    }
    if info.get("date_start") and info.get("date_end"):
        try:
            price_ledger = await asyncio.to_thread(
                PriceLedgerStore().inspect_readiness,
                scope_id=cache_key,
                start=str(info["date_start"]),
                end=str(info["date_end"]),
                security_codes=actual_codes,
            )
        except PriceLedgerIntegrityError:
            price_ledger = PriceLedgerStore.unavailable_readiness(
                scope_id=cache_key,
                start=str(info["date_start"]),
                end=str(info["date_end"]),
                reason="price_ledger_integrity_invalid",
            )
        except PriceLedgerValidationError:
            price_ledger = PriceLedgerStore.unavailable_readiness(
                scope_id=cache_key,
                start=str(info["date_start"]),
                end=str(info["date_end"]),
                reason="price_ledger_identity_invalid",
            )
    else:
        price_ledger = PriceLedgerStore.unavailable_readiness(
            scope_id=cache_key,
            start=str(info.get("date_start") or "1970-01-01"),
            end=str(info.get("date_end") or "1970-01-01"),
        )
    return {
        "pool_id": cache_key,
        "label": (
            "自定义缓存 " + cache_key[-6:]
            if cache_key.startswith("custom_")
            else cache_key.upper()
        ),
        "ready": reason is None,
        "ready_for_unbiased_research": False,
        "ready_for_unbiased_return_research": False,
        "descriptive_return_research_ready": bool(
            reason is None
            and price_ledger.get("ready_for_return_research", False)
        ),
        "ready_for_static_adjusted_return_research": bool(
            reason is None
            and price_ledger.get("ready_for_return_research", False)
        ),
        "canonical_runtime_price_bound": False,
        "return_research_semantics": (
            "factor_runtime_reads_legacy_parquet_not_canonical_ledger;"
            "pit_or_price_readiness_alone_cannot_promote"
        ),
        "ready_for_real_tuning": False,
        "neutralization_ready": bool(
            pit_coverage.get("industry", {}).get("neutralization_ready")
        ),
        "neutralization": {
            "schema_version": "factor-neutralization-readiness/v1",
            "modes": neutralization_modes,
            "industry": {
                "ready": industry_ready,
                "reason": industry_reason,
                "scope_id": pit_coverage.get("industry", {}).get("scope_id"),
                "query_semantics": (
                    "one_verified_as_of_query_per_trading_date"
                ),
            },
            "size": size_capability,
        },
        "disabled_reason": reason,
        "date_start": info.get("date_start"),
        "date_end": info.get("date_end"),
        "n_dates": int(info.get("n_dates") or 0),
        "n_stocks": int(info.get("n_stocks") or 0),
        "fields": sorted(fields),
        "available_factor_ids": factor_ids,
        "schema_version": info.get("schema_version"),
        "source_trust": trust,
        "price_role": {
            "adjustment": price_adjustment or None,
            "role": (
                "adjusted_return_research"
                if price_adjustment in {"qfq", "hfq"}
                else "raw_execution_not_valid_for_factor_returns"
                if price_adjustment == "raw"
                else "unverified"
            ),
        },
        "source_providers": sorted(
            str(item) for item in provenance.get("providers") or []
        ),
        "source_evidence_levels": sorted(
            str(item) for item in provenance.get("evidence_levels") or []
        ),
        "price_ledger": price_ledger,
        "point_in_time": pit_coverage,
    }


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    cache_key: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": message,
            "code": code,
            "cache_key": cache_key,
            "action": "refresh_in_data_center",
        },
    )


@router.get("/catalog")
async def factor_catalog(
    include_deprecated: bool = True,
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    del user
    catalog = await asyncio.to_thread(
        _governance_store().list_catalog,
        include_deprecated=include_deprecated,
    )
    return {"data": catalog}


@router.post("/catalog/{factor_id}/versions/{version}/publish")
async def publish_factor_version(
    factor_id: str,
    version: str,
    body: FactorLifecycleBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            _governance_store().set_factor_status,
            factor_id=factor_id,
            version=version,
            definition_digest=body.definition_digest,
            status="published",
            expected_revision=body.expected_revision,
            actor_user_id=int(user["id"]),
            idempotency_key=body.idempotency_key,
        )
    except FactorGovernanceError as exc:
        raise _governance_http_error(exc) from exc
    return {"data": result}


@router.post("/catalog/{factor_id}/versions/{version}/deprecate")
async def deprecate_factor_version(
    factor_id: str,
    version: str,
    body: FactorLifecycleBody,
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            _governance_store().set_factor_status,
            factor_id=factor_id,
            version=version,
            definition_digest=body.definition_digest,
            status="deprecated",
            expected_revision=body.expected_revision,
            actor_user_id=int(user["id"]),
            idempotency_key=body.idempotency_key,
        )
    except FactorGovernanceError as exc:
        raise _governance_http_error(exc) from exc
    return {"data": result}


@router.get("/readiness")
async def factor_readiness(
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    del user
    cache = DataCache()
    pools = [
        await _cache_capability(cache, key)
        for key in _candidate_cache_keys()
    ]
    return {
        "data": {
            "schema_version": "factor-research-readiness/v1",
            "ready": any(item["ready"] for item in pools),
            "pools": pools,
            "limits": {
                "max_horizons": 12,
                "max_horizon": 252,
                "max_window_days": 3653,
                "quantiles": {"min": 2, "max": 10},
            },
        }
    }


@router.get("/runs")
async def list_factor_runs(
    include_archived: bool = False,
    factor_id: str | None = Query(default=None, min_length=1, max_length=80),
    query: str | None = Query(default=None, max_length=128),
    sort: Literal["newest", "oldest", "factor", "horizon"] = "newest",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    runs, total = await asyncio.to_thread(
        _run_store().query,
        owner_user_id=int(user["id"]),
        include_archived=include_archived,
        factor_id=factor_id,
        query=query,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    return {
        "data": {
            "items": runs,
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    }


@router.get("/runs/{run_id}")
async def get_factor_run(
    run_id: str,
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    run = await asyncio.to_thread(
        _run_store().get,
        owner_user_id=int(user["id"]),
        run_id=run_id,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="研究运行不存在")
    return {"data": run}


@router.get("/runs/{run_id}/export")
async def export_factor_run_evidence(
    run_id: str,
    format: Literal["json", "csv"] = Query(  # noqa: A002
        "json",
        description="json 为流式证据；csv 为多表 CSV ZIP",
    ),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> StreamingResponse:
    """Export one owned, completed and integrity-verified research run."""
    try:
        evidence = await asyncio.to_thread(
            prepare_factor_evidence,
            _run_store(),
            run_id,
            user,
        )
        safe_run_id = (
            run_id if re.fullmatch(r"frun_[0-9a-f]{32}", run_id) else "factor-run"
        )
        generated_stamp = str(evidence["generated_at"]).replace(
            "-", ""
        ).replace(":", "")[:15]
        basename = f"factor-research-evidence-{safe_run_id}-{generated_stamp}"
        headers = {
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="{basename}.'
                f'{"json" if format == "json" else "zip"}"'
            ),
            "X-Content-Type-Options": "nosniff",
        }
        if format == "json":
            return StreamingResponse(
                stream_factor_json(evidence),
                media_type=JSON_MEDIA_TYPE,
                headers=headers,
            )
        archive = await asyncio.to_thread(build_factor_csv_zip, evidence)
        return StreamingResponse(
            stream_binary_file(archive),
            media_type=ZIP_MEDIA_TYPE,
            headers=headers,
        )
    except FactorEvidenceExportError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        ) from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503,
            detail="因子研究证据数据库暂不可用",
        ) from exc


@router.delete("/runs/{run_id}")
async def archive_factor_run(
    run_id: str,
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    archived = await asyncio.to_thread(
        _run_store().archive,
        owner_user_id=int(user["id"]),
        run_id=run_id,
    )
    if not archived:
        raise HTTPException(status_code=404, detail="研究运行不存在或已归档")
    return {"data": {"run_id": run_id, "archived": True}}


@router.post("/export-strategy")
async def export_strategy(
    body: ExportFactorStrategyBody,
    user: dict[str, Any] = Depends(require_permission("strategies:scan")),
) -> dict[str, Any]:
    selected_runs: list[dict[str, Any]] = []
    for run_id in body.research_run_ids:
        run = await asyncio.to_thread(
            _run_store().get,
            owner_user_id=int(user["id"]),
            run_id=run_id,
        )
        if run is None:
            raise HTTPException(
                status_code=404,
                detail="研究运行不存在或当前账号无权访问",
            )
        selected_runs.append(run)
    protocol_groups: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for run in selected_runs:
        review = (run.get("result") or {}).get("protocol_review")
        if not isinstance(review, dict):
            continue
        rules = review.get("export_rules") or {}
        if rules.get("allow_strategy_export") is False:
            raise HTTPException(
                status_code=422,
                detail="所选研究运行的预注册协议禁止导出策略",
            )
        if rules.get("require_all_thresholds") and review.get("passed") is not True:
            raise HTTPException(
                status_code=422,
                detail="所选研究运行未通过预注册阈值，协议禁止导出策略",
            )
        key = (
            str(review.get("protocol_id")),
            int(review.get("version") or 0),
            str(review.get("payload_digest")),
        )
        protocol_groups.setdefault(key, []).append(run)
    for group in protocol_groups.values():
        review = group[0]["result"]["protocol_review"]
        rules = review["export_rules"]
        if len(group) < int(rules.get("minimum_evidence_runs") or 1):
            raise HTTPException(
                status_code=422,
                detail="所选证据数量未达到预注册协议的导出门槛",
            )
        if rules.get("require_dataset_consistency") and len(
            {item["dataset_digest"] for item in group}
        ) != 1:
            raise HTTPException(
                status_code=422,
                detail="预注册协议要求使用一致数据版本，所选运行摘要不一致",
            )
    request_without_key = body.model_dump(exclude={"idempotency_key"})
    idempotency_key = body.idempotency_key or (
        "auto-"
        + hashlib.sha256(
            json.dumps(
                request_without_key,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:40]
    )
    try:
        definition = await asyncio.to_thread(
            _governance_store().publish_strategy,
            name=body.name,
            components=[item.model_dump() for item in body.components],
            top_k_pct=body.top_k_pct,
            research_run_ids=body.research_run_ids,
            owner_user_id=int(user["id"]),
            actor_user_id=int(user["id"]),
            idempotency_key=idempotency_key,
            strategy_id=body.strategy_id,
            expected_version=body.expected_version,
        )
        get_registry().replace_strategy_class(
            make_factor_strategy_class(definition)
        )
    except FactorGovernanceError as exc:
        raise _governance_http_error(exc) from exc
    return {"data": definition}


@router.get("/strategies/{strategy_id}/versions")
async def list_factor_strategy_versions(
    strategy_id: str,
    user: dict[str, Any] = Depends(require_permission("strategies:scan")),
) -> dict[str, Any]:
    result = await asyncio.to_thread(
        _governance_store().list_strategy_versions,
        strategy_id=strategy_id,
        owner_user_id=int(user["id"]),
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="策略不存在；legacy_unbound 策略没有可晋级版本链",
        )
    return {"data": result}


@router.post("/strategies/{strategy_id}/rollback")
async def rollback_factor_strategy(
    strategy_id: str,
    body: FactorStrategyRollbackBody,
    user: dict[str, Any] = Depends(require_permission("strategies:scan")),
) -> dict[str, Any]:
    try:
        definition = await asyncio.to_thread(
            _governance_store().rollback_strategy,
            strategy_id=strategy_id,
            target_version=body.target_version,
            expected_version=body.expected_version,
            owner_user_id=int(user["id"]),
            actor_user_id=int(user["id"]),
            idempotency_key=body.idempotency_key,
        )
        get_registry().replace_strategy_class(
            make_factor_strategy_class(definition)
        )
    except FactorGovernanceError as exc:
        raise _governance_http_error(exc) from exc
    return {"data": definition}


@router.get("/governance/audit")
async def factor_governance_audit(
    limit: int = Query(default=100, ge=1, le=500),
    user: dict[str, Any] = Depends(require_permission("admin:users")),
) -> dict[str, Any]:
    del user
    rows = await asyncio.to_thread(
        _governance_store().list_audit_events,
        limit=limit,
    )
    return {"data": rows}


@router.post("/compare")
async def compare_factor_runs(
    body: CompareFactorRunsBody,
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for run_id in body.run_ids:
        run = await asyncio.to_thread(
            _run_store().get,
            owner_user_id=int(user["id"]),
            run_id=run_id,
        )
        if run is None:
            raise HTTPException(status_code=404, detail="研究运行不存在")
        result = run["result"]
        horizon = str(result["request"]["primary_horizon"])
        rank_ic = result["ic"][horizon]["summary"]["rank_ic"]
        rows.append(
            {
                "run_id": run["run_id"],
                "factor_id": run["factor_id"],
                "created_at": run["created_at"],
                "dataset_digest": run["dataset_digest"],
                "primary_horizon": int(horizon),
                "rank_ic_mean": rank_ic["mean"],
                "rank_ic_ir": rank_ic["icir"],
                "rank_ic_positive_ratio": rank_ic["positive_ratio"],
                "long_short_mean": result["quantile_returns"]["long_short"]["mean"],
                "monotonicity": result["quantile_returns"]["monotonicity"],
            }
        )
    return {
        "data": {
            "schema_version": "factor-research-comparison/v1",
            "runs": rows,
            "dataset_consistent": len(
                {item["dataset_digest"] for item in rows}
            ) == 1,
        }
    }


@router.post("/jobs", status_code=202)
async def submit_factor_research_job(
    body: FactorResearchBody,
    user: dict[str, Any] = Depends(require_permission("data:read")),
    broker: Any = Depends(get_job_broker),
) -> dict[str, Any]:
    """Validate and enqueue a restart-safe factor research calculation."""
    from backend.data.pit_runtime import (
        PitRuntimeDataError,
        require_pit_runtime_input,
    )

    cache_key, requested_codes = factor_cache_key(body)
    try:
        await require_pit_runtime_input(
            pool_id=cache_key,
            required_start=body.start,
            required_end=body.end,
            purpose="research",
            requested_codes=requested_codes,
            require_benchmark=False,
        )
    except PitRuntimeDataError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": exc.message,
                "data_policy": "pit_cache_only",
            },
        ) from exc
    if body.neutralization != "none":
        capability = await _cache_capability(DataCache(), cache_key)
        mode = capability["neutralization"]["modes"][body.neutralization]
        if not mode["ready"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": str(
                        mode["reason"] or "neutralization_unavailable"
                    ),
                    "message": (
                        "所选中性化模式缺少完整的点时暴露或可信来源证据"
                    ),
                    "mode": body.neutralization,
                },
            )
    try:
        job_id = await broker.submit_job(
            "factor_research",
            body.model_dump(),
            user_id=int(user["id"]),
            display_name=f"因子研究 · {body.factor_id}",
            resource_type="factor_research",
            resource_id=body.factor_id,
        )
    except JobQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"data": {"job_id": job_id, "status": "pending"}}


@router.post("/analyze", response_model=None)
async def analyze_factor(
    body: FactorResearchBody,
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any] | JSONResponse:
    """Compatibility endpoint; new clients should submit ``/jobs``."""
    try:
        result = await execute_factor_research(
            body,
            owner_user_id=int(user["id"]),
            cache=DataCache(),
            store=_run_store(),
        )
    except FactorResearchExecutionError as exc:
        if exc.cache_key is not None:
            return _error_response(
                status_code=exc.status_code,
                code=exc.code,
                message=exc.message,
                cache_key=exc.cache_key,
            )
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        ) from exc
    return {"data": result}
