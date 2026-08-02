"""数据 API — 股票池、行业分类、行情数据与更新管理.

集成后端 data 层:
    - UniverseManager  → 股票池/成分股/行业筛选
    - DataCache        → Parquet 行情缓存
    - TradingCalendar  → 交易日历查询
    - AKShareSource    → AKShare 数据拉取（按需/延迟初始化）
"""

from __future__ import annotations

import asyncio
from datetime import date as calendar_date
import hashlib
import json as _json
import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.dependencies import (
    require_permission,
)
from backend.api.schemas import (
    ApiResponse,
    DataUpdateStatusResponse,
    PoolResponse,
)
from backend.data.universe import (
    IndustryClassificationUnavailableError,
    normalize_industry_codes,
)
from backend.jobs.broker import JobQueueFullError

logger = logging.getLogger("quant_platform.api.data")

router = APIRouter(prefix="/api/data", tags=["Data"])
_industry_refresh_lock = asyncio.Lock()


class ExperimentDataReadinessBody(BaseModel):
    """The exact read-only window checked before a cache-only experiment."""

    model_config = ConfigDict(extra="forbid")

    data_access_policy: Literal["cache_only"]
    research_trust_profile: Literal[
        "governed_production_pit",
        "tushare_research_trusted",
    ] = "governed_production_pit"
    price_purpose: Literal[
        "compatibility_research",
        "return_research",
        "real_tuning",
        "execution_simulation",
    ] = "compatibility_research"
    pool_preset: str = Field(min_length=1, max_length=64)
    pool_custom_codes: list[str] = Field(default_factory=list, max_length=5000)
    train_start: str | None = None
    test_start: str
    test_end: str

    @model_validator(mode="after")
    def validate_windows(self):
        import pandas as pd

        test_start = pd.Timestamp(self.test_start)
        test_end = pd.Timestamp(self.test_end)
        if test_start >= test_end:
            raise ValueError("test_start 必须早于 test_end")
        if self.train_start and pd.Timestamp(self.train_start) >= test_start:
            raise ValueError("train_start 必须早于 test_start")
        if self.pool_preset == "custom" and not self.pool_custom_codes:
            raise ValueError("自定义股票池必须提供股票代码")
        return self


class IndustryCodeScopeBody(BaseModel):
    """Explicit stock-code scope for custom/subset industry validation."""

    model_config = ConfigDict(extra="forbid")

    codes: list[str] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def validate_codes(self):
        normalized, invalid = normalize_industry_codes(self.codes)
        if invalid:
            preview = "、".join(invalid[:10])
            raise ValueError(
                f"行业范围包含无效股票代码（仅支持6位代码及可选.SH/.SZ/.BJ后缀）：{preview}"
            )
        if not normalized:
            raise ValueError("行业范围必须包含至少一个有效股票代码")
        self.codes = normalized
        return self


class ResearchDataRefreshBody(BaseModel):
    """Bounded provider refresh into the non-production research store."""

    model_config = ConfigDict(extra="forbid")

    source_id: Literal["tushare", "baostock", "activated_local"]
    from_month: str = Field(default="2016-01", pattern=r"^\d{4}-(?:0[1-9]|1[0-2])$")
    to_month: str | None = Field(default=None, pattern=r"^\d{4}-(?:0[1-9]|1[0-2])$")
    max_calls: int = Field(default=16, ge=1, le=64)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.to_month is not None and self.from_month > self.to_month:
            raise ValueError("from_month 不能晚于 to_month")
        return self


# ═══════════════════════════════════════════════════════════════════════════════
# 单例延迟初始化（避免启动时即加载 AKShare）
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class _DataServices:
    """持有数据层核心组件的单例，按需延迟初始化。"""

    source: Any = None  # AKShareSource
    cache: Any = None  # DataCache
    calendar: Any = None  # TradingCalendar
    universe: Any = None  # UniverseManager

    def _ensure(self) -> None:
        if self.source is not None:
            return
        from backend.data.sources.validated import build_public_research_source
        from backend.data.cache import DataCache
        from backend.data.calendar import TradingCalendar
        from backend.data.universe import UniverseManager

        self.source = build_public_research_source()
        self.cache = DataCache()
        self.calendar = TradingCalendar()
        self.universe = UniverseManager(self.source, self.cache)

    @property
    def s(self):
        self._ensure()
        return self.source

    @property
    def c(self):
        self._ensure()
        return self.cache

    @property
    def cal(self):
        self._ensure()
        return self.calendar

    @property
    def u(self):
        self._ensure()
        return self.universe


_data_svc = _DataServices()


# ═══════════════════════════════════════════════════════════════════════════════
# 预置股票池声明（成分证据只来自已激活 PIT master）
# ═══════════════════════════════════════════════════════════════════════════════

_PRESET_POOLS_DISPLAY: dict[str, dict[str, Any]] = {
    "csi300": {
        "id": "csi300",
        "name": "沪深300",
        "description": "沪深300指数成分股",
        "count": 300,
        "index_code": "000300",
    },
    "csi500": {
        "id": "csi500",
        "name": "中证500",
        "description": "中证500指数成分股",
        "count": 500,
        "index_code": "000905",
    },
    "csi800": {
        "id": "csi800",
        "name": "CSI 800",
        "description": "中证800指数成分股（沪深300+中证500）",
        "count": 800,
        "index_code": "000906",
    },
    "csi1000": {
        "id": "csi1000",
        "name": "中证1000",
        "description": "中证1000指数成分股",
        "count": 1000,
        "index_code": "000852",
    },
}

# 行业分类标准
_INDUSTRY_CLASSIFICATIONS: dict[str, str] = {
    "cninfo_008001": "巨潮 008001 行业",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 股票池
# ═══════════════════════════════════════════════════════════════════════════════


def _governed_pool_observation(
    pool_id: str,
    requested_as_of: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Read a governed CSI pool from activated PIT evidence only.

    ``UniverseManager`` intentionally remains available for explicitly
    labelled current/static research utilities.  It must never be initialized
    by the four governed-pool display/query endpoints.
    """

    from backend.data.pit_runtime import PIT_RUNTIME_POOLS
    from backend.data.point_in_time_master import PointInTimeMasterStore

    normalized_pool = str(pool_id or "").strip().lower()
    if normalized_pool not in PIT_RUNTIME_POOLS:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_in_time_pool_unsupported",
                "message": "股票池成分读取仅支持已治理的 PIT 指数池",
                "pool_id": normalized_pool,
            },
        )
    requested = str(requested_as_of or calendar_date.today().isoformat())
    observation = PointInTimeMasterStore().resolve_display_observation(
        domain="index_membership",
        scope_id=normalized_pool,
        requested_as_of=requested,
    )
    return (
        normalized_pool,
        _PRESET_POOLS_DISPLAY[normalized_pool],
        observation,
        observation["query"],
    )


def _pool_availability(observation: dict[str, Any], membership: dict[str, Any]) -> dict[str, Any]:
    return {
        "ready": bool(membership.get("available") and membership.get("records")),
        "reason": membership.get("reason"),
        "requested_as_of": observation["requested_as_of"],
        "resolved_as_of": observation["resolved_as_of"],
        "resolution": observation["resolution"],
        "staleness_calendar_days": observation["staleness_calendar_days"],
        "network_accessed": False,
        "source_batches": membership.get("source_batches", []),
    }


def _pool_lineage(
    pool_id: str,
    observation: dict[str, Any],
    membership: dict[str, Any],
    codes: list[str],
) -> dict[str, Any]:
    source_batches = membership.get("source_batches", [])
    return {
        "requested_as_of": observation["requested_as_of"],
        "resolved_as_of": observation["resolved_as_of"],
        "resolution": observation["resolution"],
        "staleness_calendar_days": observation["staleness_calendar_days"],
        "source_as_of": max(
            (str(item.get("source", {}).get("retrieved_at") or "") for item in source_batches),
            default=None,
        ),
        "point_in_time": bool(membership.get("available") and membership.get("records")),
        "snapshot_hash": (
            hashlib.sha256(
                _json.dumps(
                    {
                        "pool_id": pool_id,
                        "requested_as_of": observation["requested_as_of"],
                        "resolved_as_of": observation["resolved_as_of"],
                        "codes": codes,
                        "source_batches": source_batches,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if codes
            else None
        ),
    }


@router.get("/pools", response_model=ApiResponse[list[PoolResponse]])
async def list_pools(
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """List the four governed pools using activated PIT observations only."""
    from backend.data.point_in_time_master import (
        PointInTimeIntegrityError,
        PointInTimeValidationError,
    )

    pools: list[dict[str, Any]] = []
    requested = calendar_date.today().isoformat()
    for pid, info in _PRESET_POOLS_DISPLAY.items():
        entry = {**info, "declared_count": info["count"]}
        try:
            _pool, _preset, observation, membership = _governed_pool_observation(pid, requested)
            codes = [str(item["security_code"]) for item in membership.get("records", [])]
            entry.update(
                count=len(codes),
                availability=_pool_availability(observation, membership),
                lineage=_pool_lineage(pid, observation, membership, codes),
                risk_warnings=list(observation["risk_warnings"]),
            )
        except (PointInTimeValidationError, PointInTimeIntegrityError) as exc:
            entry.update(
                count=0,
                availability={
                    "ready": False,
                    "reason": "point_in_time_integrity_invalid",
                    "requested_as_of": requested,
                    "resolved_as_of": None,
                    "resolution": "unavailable",
                    "staleness_calendar_days": None,
                    "network_accessed": False,
                    "source_batches": [],
                },
                lineage={
                    "requested_as_of": requested,
                    "resolved_as_of": None,
                    "point_in_time": False,
                    "snapshot_hash": None,
                },
                risk_warnings=["point_in_time_integrity_invalid", str(exc)],
            )
        pools.append(entry)

    return {"data": pools}


@router.post("/experiment-readiness")
async def inspect_experiment_data_readiness(
    body: ExperimentDataReadinessBody,
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """Validate daily and benchmark caches without network access or writes."""

    import pandas as pd

    from backend.data.cache import DataCache, resolve_pool_benchmark
    from backend.data.cache_readiness import (
        custom_cache_key,
        inspect_cached_benchmark,
        inspect_cached_market_data,
        normalize_requested_codes,
    )
    from backend.data.universe import POOL_NAME_ALIASES
    from backend.services.experiment_readiness import (
        build_experiment_readiness_contract,
    )

    pool_id = POOL_NAME_ALIASES.get(
        body.pool_preset,
        body.pool_preset,
    )
    requested_codes = normalize_requested_codes(body.pool_custom_codes)
    cache_key = custom_cache_key(requested_codes) if pool_id == "custom" else pool_id
    # Match the worker/submission lookback exactly.  Checking only test_start
    # can miss the immutable warm-up input and also fails to locate an exact
    # canonical runtime price binding.
    required_start = (
        pd.Timestamp(body.train_start or body.test_start) - pd.Timedelta(days=400)
    ).strftime("%Y-%m-%d")
    benchmark_code = resolve_pool_benchmark(pool_id)
    benchmark_start = (pd.Timestamp(body.test_start) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")

    if body.research_trust_profile == "tushare_research_trusted":
        from backend.services.research_runtime import (
            ResearchRuntimeError,
            build_research_trust,
            load_research_benchmark,
            load_research_market,
        )

        try:
            market_result = await load_research_market(
                pool_id=pool_id,
                required_start=required_start,
                required_end=body.test_end,
            )
        except ResearchRuntimeError as exc:
            market_report = {
                **exc.report,
                "ready": False,
                "pool_id": pool_id,
                "issues": list(exc.report.get("issues") or [exc.code]),
            }
            benchmark_report = {
                "ready": False,
                "issues": ["benchmark_not_checked_without_market_generation"],
                "warnings": [],
            }
            trust = {
                "schema_version": "tushare-research-trust/v1",
                "profile": "tushare_research_trusted",
                "eligible": False,
                "blockers": [exc.code],
                "warnings": [],
                "known_limitations": ["research_market_not_computable"],
            }
        else:
            market_report = dict(market_result["report"])
            market_report["pool_id"] = pool_id
            generation_id = str(market_report["generation_id"])
            try:
                benchmark_result = await load_research_benchmark(
                    index_code=benchmark_code,
                    required_start=benchmark_start,
                    required_end=body.test_end,
                    generation_id=generation_id,
                )
            except ResearchRuntimeError as exc:
                benchmark_report = {
                    **exc.report,
                    "ready": False,
                    "issues": list(exc.report.get("issues") or [exc.code]),
                }
                trust = build_research_trust(
                    market_result=market_result,
                    required_start=required_start,
                    required_end=body.test_end,
                    purpose=body.price_purpose,
                    benchmark_report=benchmark_report,
                )
                trust["eligible"] = False
                trust["blockers"] = [exc.code]
            else:
                benchmark_report = dict(benchmark_result.get("report") or {})
                trust = build_research_trust(
                    market_result=market_result,
                    required_start=required_start,
                    required_end=body.test_end,
                    purpose=body.price_purpose,
                    benchmark_report=benchmark_report,
                )
        contract = build_experiment_readiness_contract(
            price_purpose=body.price_purpose,
            market_report=market_report,
            benchmark_report=benchmark_report,
            research_trust=trust,
        )
        return {
            "data": {
                **contract,
                "data_access_policy": "pit_cache_only",
                "price_purpose": body.price_purpose,
                "research_trust_profile": body.research_trust_profile,
                "market_data": market_report,
                "benchmark": benchmark_report,
            }
        }

    # Instantiate only the local cache.  Do not initialize a public source or
    # UniverseManager on this read-only endpoint.
    cache = DataCache()
    market = await inspect_cached_market_data(
        cache,
        cache_key=cache_key,
        pool_id=pool_id,
        requested_codes=requested_codes,
        required_start=required_start,
        required_end=body.test_end,
    )
    benchmark = await inspect_cached_benchmark(
        cache,
        index_code=benchmark_code,
        required_start=benchmark_start,
        required_end=body.test_end,
    )
    contract = build_experiment_readiness_contract(
        price_purpose=body.price_purpose,
        market_report=market.report,
        benchmark_report=benchmark.report,
        research_trust=(
            _tushare_research_trust(
                required_start=required_start,
                required_end=body.test_end,
                purpose=body.price_purpose,
                market_report=market.report,
            )
            if body.research_trust_profile == "tushare_research_trusted"
            else None
        ),
    )
    return {
        "data": {
            **contract,
            "data_access_policy": "pit_cache_only",
            "price_purpose": body.price_purpose,
            "research_trust_profile": body.research_trust_profile,
            "market_data": market.report,
            "benchmark": benchmark.report,
        }
    }


def _tushare_research_trust(
    *,
    required_start: str,
    required_end: str,
    purpose: str,
    market_report: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the latest local candidate report for an explicit request."""

    from backend.config import settings
    from backend.services.tushare_research_trust import (
        assess_tushare_research_trust,
        load_latest_tushare_backfill_report,
    )

    report, digest = load_latest_tushare_backfill_report(
        settings.abs_path(settings.PIT_EVIDENCE_DIR)
    )
    assessment = assess_tushare_research_trust(
        report=report,
        report_object_sha256=digest,
        required_start=required_start,
        required_end=required_end,
        purpose=purpose,
    )
    if market_report.get("source_providers") != ["tushare"]:
        assessment["blockers"].append("runtime_cache_not_exclusively_tushare")
        assessment["eligible"] = False
        assessment["claims"]["eligible_for_conditional_research"] = False
        assessment["claims"]["eligible_for_real_tuning"] = False
        assessment["claims"]["eligible_for_paper_trading"] = False
    return assessment


@router.get("/pools/{pool_id}")
async def get_pool_detail(
    pool_id: str,
    date: str | None = Query(
        None,
        description="研究目标日期；仅从已激活 PIT 时间线读取",
    ),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """Get governed pool detail and industry distribution from PIT evidence."""
    from collections import Counter

    from backend.data.point_in_time_master import (
        PointInTimeIntegrityError,
        PointInTimeMasterStore,
        PointInTimeValidationError,
    )

    try:
        normalized_pool, preset, observation, membership = _governed_pool_observation(pool_id, date)
    except PointInTimeValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "point_in_time_as_of_invalid",
                "message": "股票池查询日期必须是 YYYY-MM-DD",
            },
        ) from exc
    except PointInTimeIntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_in_time_integrity_invalid",
                "message": "PIT 股票池证据完整性校验失败",
            },
        ) from exc

    codes = [str(item["security_code"]) for item in membership.get("records", [])]
    availability = _pool_availability(observation, membership)
    warnings = list(observation["risk_warnings"])
    industries: list[dict[str, Any]] = []
    if codes and observation["resolved_as_of"]:
        try:
            industry = PointInTimeMasterStore().query_as_of(
                domain="industry",
                scope_id="cninfo_008001",
                as_of=observation["resolved_as_of"],
                security_codes=codes,
            )
            if industry.get("available"):
                counts = Counter(
                    str(item.get("attributes", {}).get("industry_name") or "未分类")
                    for item in industry.get("records", [])
                )
                industries = [
                    {"name": name, "count": count}
                    for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
                ]
            else:
                warnings.append(str(industry.get("reason") or "pit_industry_missing"))
        except (PointInTimeIntegrityError, PointInTimeValidationError):
            warnings.append("pit_industry_invalid")

    return {
        "data": {
            "pool_id": normalized_pool,
            "name": preset["name"],
            "description": preset["description"],
            "count": len(codes),
            "declared_count": preset["count"],
            "industries": industries,
            "availability": availability,
            "lineage": _pool_lineage(normalized_pool, observation, membership, codes),
            "quality": {
                "ready": availability["ready"],
                "expected_count": preset["count"],
                "unique_count": len(codes),
            },
            "risk_warnings": warnings,
        }
    }


@router.get("/pools/{pool_id}/stocks")
async def get_pool_stocks(
    pool_id: str,
    industry: str | None = Query(None, description="按行业筛选"),
    date: str | None = Query(
        None,
        description="研究目标日期；仅从已激活 PIT 时间线读取",
    ),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """从已激活 PIT master 获取指定日期的股票池成分。

    Args:
        pool_id:  池标识。
        industry: 可选行业名，如 "银行"，传入后仅返回该行业股票。
    """
    from backend.data.point_in_time_master import (
        PointInTimeIntegrityError,
        PointInTimeValidationError,
    )

    try:
        normalized_pool, preset, observation, membership = _governed_pool_observation(pool_id, date)
    except PointInTimeValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "point_in_time_as_of_invalid",
                "message": "股票池查询日期必须是 YYYY-MM-DD",
            },
        ) from exc
    except PointInTimeIntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_in_time_integrity_invalid",
                "message": "PIT 股票池证据完整性校验失败",
            },
        ) from exc

    codes = [str(item["security_code"]) for item in membership.get("records", [])]
    availability = _pool_availability(observation, membership)

    if not codes:
        return {
            "data": {
                "pool_id": normalized_pool,
                "name": preset["name"],
                "stocks": [],
                "count": 0,
                "note": "该日期没有已激活的 PIT 成分证据",
                "availability": availability,
                "lineage": _pool_lineage(normalized_pool, observation, membership, []),
                "quality": {"ready": False, "reason": membership.get("reason")},
                "risk_warnings": [
                    *observation["risk_warnings"],
                    str(membership.get("reason") or "point_in_time_universe_missing"),
                ],
            }
        }

    if industry:
        try:
            from backend.data.point_in_time_master import PointInTimeMasterStore

            industry_result = PointInTimeMasterStore().query_as_of(
                domain="industry",
                scope_id="cninfo_008001",
                as_of=str(observation["resolved_as_of"]),
                security_codes=codes,
            )
        except (PointInTimeIntegrityError, PointInTimeValidationError) as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "point_in_time_industry_invalid",
                    "message": "PIT 行业证据不可用",
                },
            ) from exc
        if not industry_result.get("available"):
            return {
                "data": {
                    "pool_id": normalized_pool,
                    "stocks": [],
                    "count": 0,
                    "industry_filter": industry,
                    "availability": {
                        **availability,
                        "ready": False,
                        "reason": industry_result.get("reason"),
                    },
                    "lineage": _pool_lineage(normalized_pool, observation, membership, []),
                    "quality": {
                        "ready": False,
                        "reason": industry_result.get("reason"),
                    },
                    "risk_warnings": [
                        *observation["risk_warnings"],
                        str(industry_result.get("reason") or "pit_industry_missing"),
                    ],
                }
            }
        selected = {
            str(item["security_code"])
            for item in industry_result["records"]
            if str(item.get("attributes", {}).get("industry_name") or "") == industry
            or str(item.get("attributes", {}).get("industry_code") or "") == industry
        }
        codes = [code for code in codes if code in selected]

    return {
        "data": {
            "pool_id": normalized_pool,
            "stocks": codes,
            "count": len(codes),
            "industry_filter": industry,
            "availability": availability,
            "lineage": _pool_lineage(normalized_pool, observation, membership, codes),
            "quality": {"ready": True, "reason": None},
            "risk_warnings": list(observation["risk_warnings"]),
        }
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 行业分类
# ═══════════════════════════════════════════════════════════════════════════════


def _industry_catalog_unavailable(
    classification: str,
    reason: str,
    *,
    source: str | None = "akshare:cninfo",
) -> dict[str, Any]:
    return {
        "schema_version": "industry-catalog/v2",
        "classification": classification,
        "classifications_available": list(_INDUSTRY_CLASSIFICATIONS.keys()),
        "industries": [],
        "count": 0,
        "filterable": False,
        "source": source,
        "reason": reason,
        "map_coverage": 0.0,
    }


def _valid_industry_catalog_entries(industries: Any) -> bool:
    return (
        isinstance(industries, list)
        and bool(industries)
        and all(
            isinstance(item, dict)
            and item.get("code")
            and item.get("name")
            and item.get("name") != item.get("code")
            and not str(item.get("name")).upper().startswith("BK")
            for item in industries
        )
    )


def _load_cached_industry_catalog(
    classification: str,
) -> dict[str, Any] | None:
    """Read and integrity-check the global catalog without network or writes."""

    from backend.config import settings
    import pandas as pd

    cache_file = settings.abs_path(settings.DATA_CACHE_DIR) / f"industries_{classification}.json"
    if not cache_file.exists():
        return None
    try:
        data = _json.loads(cache_file.read_text(encoding="utf-8"))
        industries = data.get("industries", [])
        age = pd.Timestamp.now(tz="UTC") - pd.Timestamp(data["fetched_at"])
        content_sha256 = hashlib.sha256(
            _json.dumps(
                industries,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            data.get("schema_version") == "industry-catalog/v2"
            and data.get("classification") == "cninfo_008001"
            and data.get("source") == "akshare:cninfo"
            and data.get("content_sha256") == content_sha256
            and age >= pd.Timedelta(0)
            and age <= pd.Timedelta(days=7)
            and _valid_industry_catalog_entries(industries)
        ):
            return data
    except (KeyError, TypeError, ValueError, OSError, _json.JSONDecodeError):
        logger.warning(
            "Industry catalog cache failed validation for %s",
            classification,
        )
    return None


async def _scope_industry_catalog(
    catalog: dict[str, Any],
    codes: list[str] | None,
) -> dict[str, Any]:
    """Attach exact-scope readiness and expose only represented industries."""

    scoped = dict(catalog)
    readiness = await _data_svc.u.get_industry_readiness(codes)
    scoped.update(readiness)
    if not readiness["filterable"] or not codes:
        return scoped

    mapping = await _data_svc.u.get_industry_map(strict=True)
    normalized, invalid = normalize_industry_codes(codes)
    if invalid:
        scoped.update(
            filterable=False,
            reason="industry_scope_invalid_codes",
            industries=[],
            count=0,
        )
        return scoped

    represented_names = {mapping[code] for code in normalized if code in mapping}
    catalog_names = {str(item["name"]).strip() for item in catalog["industries"]}
    unknown_names = sorted(represented_names - catalog_names)
    if unknown_names:
        scoped.update(
            filterable=False,
            reason="industry_catalog_map_mismatch",
            industries=[],
            count=0,
            unrecognized_mapped_industries=unknown_names,
        )
        return scoped

    industries = [
        item for item in catalog["industries"] if str(item["name"]).strip() in represented_names
    ]
    scoped["catalog_industry_count"] = len(catalog["industries"])
    scoped["industries"] = industries
    scoped["count"] = len(industries)
    if not industries:
        scoped.update(
            filterable=False,
            reason="industry_scope_has_no_catalog_entries",
        )
    return scoped


async def _read_scoped_industry_catalog(
    classification: str,
    codes: list[str] | None,
) -> dict[str, Any]:
    if classification != "cninfo_008001":
        return _industry_catalog_unavailable(
            classification,
            "classification_not_supported_by_configured_source",
            source=None,
        )
    catalog = _load_cached_industry_catalog(classification)
    if catalog is None:
        return _industry_catalog_unavailable(
            classification,
            "industry_cache_missing_stale_or_invalid",
        )
    return await _scope_industry_catalog(catalog, codes)


@router.get("/industries")
async def list_industries(
    classification: str = Query("cninfo_008001", description="分类标准: cninfo_008001"),
    pool_id: str | None = Query(
        None,
        description="可选股票池；提供后计算该池行业映射覆盖率",
    ),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """获取行业分类列表。

    从巨潮 008001 目录与证券行业变更记录构建；不可用时明确失败关闭。
    """
    # 读取路径只允许读取本地、完整性校验通过的缓存，不隐式联网或写盘。
    readiness_codes: list[str] | None = None
    if pool_id:
        pool_result = await get_pool_stocks(
            pool_id,
            industry=None,
            date=calendar_date.today().isoformat(),
            user=user,
        )
        readiness_codes = list(pool_result["data"]["stocks"])
    return {
        "data": await _read_scoped_industry_catalog(
            classification,
            readiness_codes,
        )
    }


@router.post("/industries/readiness")
async def inspect_industry_readiness(
    body: IndustryCodeScopeBody,
    classification: str = Query(
        "cninfo_008001",
        description="分类标准: cninfo_008001",
    ),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """Read-only catalog validation for a custom stock-code scope."""

    return {
        "data": await _read_scoped_industry_catalog(
            classification,
            body.codes,
        )
    }


@router.post("/industries/refresh")
async def refresh_industries(
    body: IndustryCodeScopeBody | None = None,
    pool_id: str | None = Query(
        None,
        description="用于校验覆盖率的股票池；与请求体 codes 二选一",
    ),
    classification: str = Query(
        "cninfo_008001",
        description="分类标准: cninfo_008001",
    ),
    user: dict[str, Any] = Depends(require_permission("data:update")),
) -> dict[str, Any]:
    """显式刷新行业目录和指定股票池/自定义代码范围映射。

    该操作会访问外部数据源并写入缓存，因此与只读查询分离并要求
    ``data:update`` 权限。进程内锁避免重复刷新同时击穿外部源。
    """
    if classification != "cninfo_008001":
        raise HTTPException(status_code=400, detail="不支持的行业分类标准")
    if bool(pool_id) == bool(body):
        raise HTTPException(
            status_code=422,
            detail="pool_id 与请求体 codes 必须且只能提供一个",
        )

    from backend.config import settings

    async with _industry_refresh_lock:
        try:
            if pool_id:
                pool_result = await get_pool_stocks(
                    pool_id,
                    date=calendar_date.today().isoformat(),
                    user=user,
                )
                scope_codes = list(pool_result["data"]["stocks"])
                if not scope_codes:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "point_in_time_pool_unavailable",
                            "message": "没有可用于行业映射的已激活 PIT 股票池成分",
                            "availability": pool_result["data"]["availability"],
                        },
                    )
            else:
                assert body is not None
                scope_codes = body.codes
            industries = await _data_svc.s.fetch_industry_list()
            if not _valid_industry_catalog_entries(industries):
                raise IndustryClassificationUnavailableError("industry_catalog_empty_or_invalid")
            await _data_svc.u.get_industry_readiness(
                scope_codes,
                refresh_missing=True,
            )
            fetched_at = __import__("pandas").Timestamp.now(tz="UTC").isoformat()
            cache_payload = {
                "schema_version": "industry-catalog/v2",
                "classification": classification,
                "classifications_available": list(_INDUSTRY_CLASSIFICATIONS.keys()),
                "industries": industries,
                "count": len(industries),
                "source": "akshare:cninfo",
                "fetched_at": fetched_at,
                "filterable": False,
                "reason": "coverage_not_evaluated",
                "map_coverage": None,
                "content_sha256": hashlib.sha256(
                    _json.dumps(
                        industries,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
            cache_file = (
                settings.abs_path(settings.DATA_CACHE_DIR) / f"industries_{classification}.json"
            )
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = cache_file.with_name(f".{cache_file.name}.{os.getpid()}.tmp")
            temp_file.write_text(
                _json.dumps(cache_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_file, cache_file)
            return {
                "data": await _scope_industry_catalog(
                    cache_payload,
                    scope_codes,
                )
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to refresh industries from source")
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "industry_refresh_failed",
                    "reason": type(exc).__name__,
                    "source": "akshare:cninfo",
                    "classification": classification,
                },
            ) from exc


@router.get("/industries/map")
async def get_industry_map(
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """获取代码→行业映射表。

    注意: 首次调用会触发行业映射构建（遍历所有行业板块），后续命中缓存。
    """
    try:
        mapping = await _data_svc.u.get_industry_map(strict=True)
        readiness = await _data_svc.u.get_industry_readiness()
        return {
            "data": {
                "n_stocks": len(mapping),
                "sample": dict(list(mapping.items())[:10]),
                "note": "完整映射通过 /api/data/industries/map/full 获取",
                **readiness,
            }
        }
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "industry_map_unavailable",
                "reason": type(exc).__name__,
                "source": "akshare:cninfo",
                "classification": "cninfo_008001",
            },
        ) from exc


@router.get("/industries/map/full")
async def get_industry_map_full(
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """获取完整的代码→行业映射表。"""
    try:
        mapping = await _data_svc.u.get_industry_map(strict=True)
        readiness = await _data_svc.u.get_industry_readiness()
        return {
            "data": {
                "map": mapping,
                "n_stocks": len(mapping),
                **readiness,
            }
        }
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "industry_map_unavailable",
                "reason": type(exc).__name__,
                "source": "akshare:cninfo",
                "classification": "cninfo_008001",
            },
        ) from exc


# ═══════════════════════════════════════════════════════════════════════════════
# 股票数据
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/stocks/batch")
async def get_batch_stock_data(
    codes: str = Query(..., description="逗号分隔的股票代码列表"),
    start: str | None = Query(None, description="开始日期 YYYY-MM-DD"),
    end: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    only_close: bool = Query(True, description="仅返回收盘价 pivot，false 返回完整OHLCV"),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """批量行情端点必须在 ``/stocks/{code}`` 之前注册。"""
    return await _get_batch_stock_data_impl(codes, start, end, only_close, user)


@router.get("/stocks/{code}")
async def get_stock_data(
    code: str,
    start: str | None = Query(None, description="开始日期 YYYY-MM-DD"),
    end: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    resolution: str = Query("daily", description="daily|weekly|monthly"),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """获取单只股票的行情数据。

    优先从 Parquet 缓存读取，缓存未命中则尝试 JSON 缓存，最后降级为拉取。
    """
    import re

    if not re.fullmatch(r"[0-9A-Za-z._-]{1,32}", code):
        raise HTTPException(status_code=400, detail="股票代码格式无效")
    code_normalized = code.replace(".", "_")

    # 1. 尝试 Parquet 缓存（通过 DataCache 的 pivot 查询）
    try:
        import pandas as pd
        from backend.config import settings

        # 先检查是否有单股缓存
        parquet_file = settings.abs_path(f"data/cache/{code_normalized}.parquet")
        if parquet_file.exists():
            df = pd.read_parquet(parquet_file)
            records = _df_to_records(df, start, end)
            if records:
                return {
                    "data": {
                        "code": code,
                        "resolution": resolution,
                        "records": records,
                        "source": "parquet_cache",
                    }
                }
    except Exception:
        pass

    # 2. 尝试 JSON 缓存（旧版兼容）
    try:
        from backend.config import settings

        cache_file = settings.abs_path(f"data/cache/stock_{code_normalized}_{resolution}.json")
        if cache_file.exists():
            raw_data = _json.loads(cache_file.read_text(encoding="utf-8"))
            filtered = raw_data
            if start or end:
                filtered = [
                    r
                    for r in raw_data
                    if (start is None or r["date"] >= start) and (end is None or r["date"] <= end)
                ]
            return {
                "data": {
                    "code": code,
                    "resolution": resolution,
                    "records": filtered,
                    "source": "json_cache",
                }
            }
    except Exception:
        pass

    # 3. 从数据源拉取
    try:
        s = start or "2020-01-01"
        e = end or pd.Timestamp.now().strftime("%Y-%m-%d") if "pd" in dir() else "2026-12-31"
        import pandas as _pd

        e_date = end or _pd.Timestamp.now().strftime("%Y-%m-%d")
        pivot = await _data_svc.s.fetch_daily([code], s, e_date if not end else e)
        if not pivot.empty:
            records = _records_for_code(pivot, code)
        else:
            records = []
        if records:
            return {
                "data": {
                    "code": code,
                    "resolution": resolution,
                    "records": records,
                    "source": "live_fetch",
                }
            }
    except Exception:
        logger.exception("Failed to fetch live data for %s", code)

    return {
        "data": {
            "code": code,
            "resolution": resolution,
            "records": [],
            "detail": "数据尚未缓存，请先执行数据更新",
        }
    }


async def _get_batch_stock_data_impl(
    codes: str,
    start: str | None,
    end: str | None,
    only_close: bool,
    user: dict[str, Any],
) -> dict[str, Any]:
    """批量获取多只股票数据，返回 pivot 格式。

    数据来源：优先 Parquet 缓存，缓存未命中则从 AKShare 拉取并缓存。
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        raise HTTPException(status_code=400, detail="请提供至少一个股票代码")
    if len(code_list) > 100:
        raise HTTPException(status_code=400, detail="单次最多查询 100 只股票")
    import re

    invalid_codes = [code for code in code_list if not re.fullmatch(r"[0-9A-Za-z._-]{1,32}", code)]
    if invalid_codes:
        raise HTTPException(status_code=400, detail=f"股票代码格式无效: {invalid_codes[:5]}")

    s = start or "2020-01-01"
    import pandas as _pd

    e = end or _pd.Timestamp.now().strftime("%Y-%m-%d")

    try:
        pivot = await _data_svc.s.fetch_daily(code_list, s, e)
        if pivot.empty:
            return {"data": {"codes": code_list, "records": [], "detail": "无数据"}}

        view = pivot
        if isinstance(view.columns, _pd.MultiIndex):
            if only_close:
                try:
                    view = view.xs("close", axis=1, level=-1)
                except KeyError:
                    raise HTTPException(status_code=502, detail="行情源未返回收盘价")
            else:
                view = view.copy()
                view.columns = [f"{code}.{field}" for code, field in view.columns]
        view = view.copy()
        view.index = view.index.map(lambda value: str(value)[:10])
        view.index.name = "date"
        records = view.reset_index().to_dict(orient="records")

        return {
            "data": {
                "codes": code_list,
                "date_start": str(view.index[0]) if len(view) > 0 else None,
                "date_end": str(view.index[-1]) if len(view) > 0 else None,
                "n_dates": len(view),
                "records": records,
                "columns": ["date"] + [str(column) for column in view.columns],
                "fields": ["close"]
                if only_close
                else ["open", "close", "high", "low", "volume", "amount"],
            }
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"批量数据获取失败: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# 交易日历
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/calendar")
async def get_trading_calendar(
    start: str = Query("2020-01-01", description="开始日期"),
    end: str | None = Query(None, description="结束日期，默认今天"),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """获取交易日历。"""
    import pandas as _pd

    e = end or _pd.Timestamp.now().strftime("%Y-%m-%d")

    try:
        days = await _data_svc.cal.load(_data_svc.s, start, e)
        return {
            "data": {
                "start": start,
                "end": e,
                "count": len(days),
                "trading_days": days,
            }
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取交易日历失败: {exc}")


@router.get("/calendar/check/{date}")
async def check_trading_day(
    date: str,
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """检查指定日期是否为交易日，并返回相邻交易日。"""
    try:
        # 确保日历已加载
        await _data_svc.cal.ensure_loaded(_data_svc.s, date)

        is_td = _data_svc.cal.is_trading_day(date)
        next_td = _data_svc.cal.next_trading_day(date)
        prev_td = _data_svc.cal.prev_trading_day(date)

        return {
            "data": {
                "date": date,
                "is_trading_day": is_td,
                "next_trading_day": next_td,
                "prev_trading_day": prev_td,
            }
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"交易日检查失败: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# 数据更新
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/research-sources")
async def get_research_sources(
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """List source capabilities and the active research generation."""

    from backend.data.research_data_store import research_source_report

    return {"data": await asyncio.to_thread(research_source_report)}


@router.get("/research-sources/conflicts")
async def get_research_source_conflicts(
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """Return concrete per-pool differences between retained sources."""

    from backend.data.research_data_store import ResearchDataStore

    report = await asyncio.to_thread(ResearchDataStore().conflict_report)
    return {"data": report}


@router.get("/research-pools/{pool_id}/stocks")
async def get_research_pool_stocks(
    pool_id: str,
    date: str | None = Query(None, description="研究目标日期 YYYY-MM-DD"),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """Read a warning-labelled monthly provider snapshot for research/paper."""

    from backend.data.research_data_store import (
        ResearchDataStore,
        ResearchDataStoreError,
    )

    try:
        result = await asyncio.to_thread(
            ResearchDataStore().query_pool,
            pool_id.strip().lower(),
            date or calendar_date.today().isoformat(),
        )
    except ResearchDataStoreError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "research_pool_query_invalid", "message": str(exc)},
        ) from exc
    return {"data": result}


@router.post("/research-sources/refresh")
async def trigger_research_data_refresh(
    body: ResearchDataRefreshBody,
    user: dict[str, Any] = Depends(require_permission("data:update")),
) -> dict[str, Any]:
    """Queue a bounded refresh that never mutates production PIT tables."""

    if body.source_id != "tushare":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "research_source_not_refreshable",
                "message": "该数据源目前仅用于交叉验证或本地读取",
                "source_id": body.source_id,
            },
        )
    try:
        from backend.dependencies import get_job_broker

        job_id = await get_job_broker().submit_job(
            job_type="research_data_refresh",
            params={
                "user_id": user["id"],
                **body.model_dump(),
            },
            user_id=user["id"],
            resource_type="research_data_source",
            resource_id=body.source_id,
            deduplicate_active=True,
        )
    except JobQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unable to submit research data refresh")
        raise HTTPException(status_code=503, detail="研究数据刷新队列暂不可用") from exc
    return {
        "data": {
            "job_id": job_id,
            "message": "研究数据刷新已提交；可用于研究和模拟，所有风险将保留为告警",
            "mode": "async_research_data_warning_only",
            "automatic_production_activation": False,
        }
    }


@router.post("/update")
async def trigger_data_update(
    pool_id: str | None = Query(None, description="更新指定池，不传则更新全部"),
    user: dict[str, Any] = Depends(require_permission("data:update")),
) -> dict[str, Any]:
    """Queue a real PIT dual-price market-data update attempt.

    This endpoint is deliberately *not* a shortcut for constituent-evidence
    refresh.  Until a licensed, activated PIT dual-price updater is installed,
    the queued job terminates failed/blocked with a machine-readable reason
    rather than reporting a false ``0/0`` completion.
    """
    from backend.data.pit_runtime import PIT_RUNTIME_POOLS

    normalized_pool = pool_id.strip().lower() if pool_id is not None else None
    if normalized_pool is not None and normalized_pool not in PIT_RUNTIME_POOLS:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_in_time_pool_unsupported",
                "message": (
                    "PIT-only 自动更新仅支持 csi300/csi500/csi800/csi1000；"
                    "任务只采集隔离证据，不会自动批准或激活"
                ),
            },
        )
    # 优先使用任务队列
    try:
        from backend.dependencies import get_job_broker

        broker = get_job_broker()
        job_id = await broker.submit_job(
            job_type="data_update",
            params={
                "user_id": user["id"],
                "pool_id": normalized_pool,
            },
            user_id=user["id"],
            resource_type="data_pool",
            resource_id="all_governed_csi",
            deduplicate_active=True,
        )
        return {
            "data": {
                "job_id": job_id,
                "message": "PIT 行情/双价格账本更新任务已提交；未满足门禁将明确阻断",
                "mode": "async_pit_market_data",
                "automatic_activation": False,
            }
        }
    except JobQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        # Fail closed. A synchronous fallback would bypass the scheduler's
        # global/per-pool cache mutex exactly when the durable queue is
        # unhealthy or contended.
        logger.exception("Unable to submit data update job")
        raise HTTPException(status_code=503, detail="数据更新队列暂不可用") from exc


@router.post("/pit-governance/refresh")
async def trigger_pit_governance_refresh(
    pool_id: str | None = Query(None, description="刷新指定池的治理证据；不传则刷新全部"),
    user: dict[str, Any] = Depends(require_permission("data:update")),
) -> dict[str, Any]:
    """Refresh official constituent evidence into quarantine, never prices."""
    from backend.data.pit_runtime import PIT_RUNTIME_POOLS

    normalized_pool = pool_id.strip().lower() if pool_id is not None else None
    if normalized_pool is not None and normalized_pool not in PIT_RUNTIME_POOLS:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_in_time_pool_unsupported",
                "message": "PIT 治理证据仅支持 csi300/csi500/csi800/csi1000",
            },
        )
    try:
        from backend.dependencies import get_job_broker

        broker = get_job_broker()
        job_id = await broker.submit_job(
            job_type="pit_governance_refresh",
            params={"user_id": user["id"], "pool_id": normalized_pool},
            user_id=user["id"],
            resource_type="pit_governance",
            resource_id=normalized_pool or "all_governed_csi",
            deduplicate_active=True,
        )
        return {
            "data": {
                "job_id": job_id,
                "message": "PIT 治理证据刷新任务已提交；仅写入隔离区，不更新行情",
                "mode": "async_pit_governance_quarantine",
                "automatic_activation": False,
            }
        }
    except JobQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unable to submit PIT governance refresh")
        raise HTTPException(status_code=503, detail="PIT 治理刷新队列暂不可用") from exc


@router.get(
    "/update/status",
    response_model=ApiResponse[DataUpdateStatusResponse],
)
async def get_update_status(
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """获取数据更新状态（含各池缓存信息）。"""
    # Keep the two operations separate.  A completed governance refresh only
    # proves that quarantine evidence exists; it says nothing about prices.
    broker_status: dict[str, Any] = {"status": "unknown"}
    governance_refresh_status: dict[str, Any] = {"status": "unknown"}
    research_refresh_status: dict[str, Any] = {"status": "unknown"}
    try:
        from backend.dependencies import get_job_broker

        broker = get_job_broker()
        # 查询最近一次 data_update 任务的状态
        latest_job = await broker.get_latest_job("data_update", user["id"])
        if latest_job:
            broker_status = latest_job
        latest_governance = await broker.get_latest_job(
            "pit_governance_refresh", user["id"]
        )
        if latest_governance:
            governance_refresh_status = latest_governance
        latest_research = await broker.get_latest_job(
            "research_data_refresh", user["id"]
        )
        system_lookup = getattr(broker, "get_latest_system_job", None)
        latest_system_research = (
            await system_lookup("research_data_refresh")
            if callable(system_lookup)
            else None
        )
        visible_research = max(
            [item for item in (latest_research, latest_system_research) if item],
            key=lambda item: int(item.get("id") or 0),
            default=None,
        )
        if visible_research:
            research_refresh_status = visible_research
    except Exception:
        pass

    # 获取各池缓存信息
    pools_cache: list[dict] = []
    for pid in _PRESET_POOLS_DISPLAY:
        try:
            info = await _data_svc.c.get_cache_info(pid)
            pools_cache.append(info)
        except Exception:
            pools_cache.append({"pool_id": pid, "exists": False, "error": "读取失败"})

    research_pools: list[dict[str, Any]] = []
    research_data_contract: dict[str, Any] = {
        "available": False,
        "classification": "single_source_research",
        "research_trust_profile": "single_source_research_warning_only",
        "allowed_uses": ["exploratory_research", "paper_simulation"],
        "risk_policy": "warning_only",
        "live_eligible": False,
    }
    try:
        from backend.data.research_data_store import ResearchDataStore

        research_store = ResearchDataStore()
        research_status = await asyncio.to_thread(research_store.status)
        if research_status["available"]:
            research_pools = await asyncio.to_thread(
                research_store.pool_statuses
            )
            research_data_contract.update(
                available=True,
                classification=research_status.get("classification"),
                research_trust_profile=research_status.get("research_trust_profile"),
                generation_id=research_status["generation_id"],
                date_start=research_status["date_start"],
                date_end=research_status["date_end"],
                warnings=research_status["warnings"],
                market=research_status.get("market"),
                coverage=research_status.get("coverage"),
            )
    except Exception as exc:
        research_data_contract["warnings"] = [type(exc).__name__]

    return {
        "data": {
            "broker_status": broker_status,
            "governance_refresh_status": governance_refresh_status,
            "research_refresh_status": research_refresh_status,
            "market_data_update_contract": {
                "scope": "activated_pit_membership_union",
                "available": False,
                "reason": "pit_dual_price_update_not_authorized",
                "requires": [
                    "licensed_provider",
                    "activated_pit_membership",
                    "raw_execution_prices",
                    "research_adjusted_prices",
                    "exact_runtime_binding",
                ],
            },
            "research_data_contract": research_data_contract,
            "research_pools": research_pools,
            "pools_cache": pools_cache,
        }
    }


@router.get("/cache/status")
async def get_cache_status(
    pool_id: str = Query(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[0-9A-Za-z_-]+$",
        description="只读检查的股票池缓存键",
    ),
    user: dict[str, Any] = Depends(require_permission("data:read")),
) -> dict[str, Any]:
    """Validate one local cache without fetching data or exposing file paths.

    This endpoint deliberately calls only ``get_cache_info`` and
    ``load_pivot``.  A missing, legacy or integrity-invalid cache is reported
    as unavailable; it never falls through to a network source.
    """
    info = await _data_svc.c.get_cache_info(pool_id)

    def safe_nonnegative_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0

    info_fields = info.get("fields")
    response: dict[str, Any] = {
        "pool_id": pool_id,
        "available": False,
        "runtime_readable": False,
        "error_code": "cache_missing",
        "schema_version": safe_nonnegative_int(info.get("schema_version")),
        "date_start": info.get("date_start"),
        "date_end": info.get("date_end"),
        "n_dates": safe_nonnegative_int(info.get("n_dates")),
        "n_stocks": safe_nonnegative_int(info.get("n_stocks")),
        "fields": sorted(str(item) for item in info_fields)
        if isinstance(info_fields, list)
        else [],
        "price_adjustment": (
            info.get("price_adjustment") if isinstance(info.get("price_adjustment"), str) else None
        ),
        "source_trust": (
            info.get("source_trust") if isinstance(info.get("source_trust"), str) else "unverified"
        ),
        "source_providers": [],
        "source_evidence_levels": [],
        "source_frame_digest": None,
        "source_identity_consistent": False,
        "source_complete_code_coverage": False,
        "source_all_batches_cross_validated": False,
        "source_all_batches_raw_cross_validated": False,
        "source_all_batches_adjusted_factor_validated": False,
        "source_validation_ready": False,
        "codes_sha256": None,
        "data_quality": None,
        "price_ledger": info.get("price_ledger"),
        "ready_for_return_research": False,
        "ready_for_static_adjusted_return_research": False,
        "ready_for_unbiased_return_research": False,
        "return_research_semantics": ("legacy_static_price_research_only_not_promotion_eligible"),
        "ready_for_execution_simulation": False,
        "universe_point_in_time": False,
        "survivorship_bias_risk": True,
        "ready_for_unbiased_tuning": False,
        "ready_for_real_tuning": False,
        "research_limitations": ["point_in_time_universe_missing"],
        "recommended_action": (
            "submit_controlled_data_update" if info.get("exists") else "submit_initial_data_update"
        ),
    }
    if not info.get("exists"):
        return {"data": response}

    from backend.data.cache import (
        DailyMarketDataQualityError,
        LegacyAdjustedCacheError,
        assess_daily_market_data_quality,
    )

    try:
        frame = await _data_svc.c.load_pivot(pool_id)
    except DailyMarketDataQualityError as exc:
        response["error_code"] = "cache_price_quality_invalid"
        response["data_quality"] = exc.report
        return {"data": response}
    except LegacyAdjustedCacheError:
        response["error_code"] = "cache_schema_or_provenance_invalid"
        return {"data": response}
    except Exception:
        response["error_code"] = "cache_not_runtime_readable"
        return {"data": response}
    if frame is None or frame.empty:
        response["error_code"] = "cache_empty"
        return {"data": response}

    if (
        not hasattr(frame.index, "hasnans")
        or frame.index.hasnans
        or not frame.index.is_unique
        or not frame.index.is_monotonic_increasing
        or not hasattr(frame.columns, "nlevels")
        or frame.columns.nlevels < 2
    ):
        response["error_code"] = "cache_structure_invalid"
        return {"data": response}
    codes = sorted(
        {str(item).strip() for item in frame.columns.get_level_values(0) if str(item).strip()}
    )
    fields = sorted(
        {str(item).strip() for item in frame.columns.get_level_values(-1) if str(item).strip()}
    )
    if not codes or not fields:
        response["error_code"] = "cache_structure_invalid"
        return {"data": response}
    quality = assess_daily_market_data_quality(
        frame,
        expected_codes=set(codes),
    )
    response["data_quality"] = quality
    if not quality["ready"]:
        response["error_code"] = "cache_price_quality_invalid"
        return {"data": response}
    provenance = info.get("source_provenance")
    if isinstance(provenance, dict):
        response["source_providers"] = sorted(
            str(item) for item in (provenance.get("providers") or [])
        )
        response["source_evidence_levels"] = sorted(
            str(item) for item in (provenance.get("evidence_levels") or [])
        )
        frame_digest = provenance.get("frame_digest")
        response["source_frame_digest"] = (
            str(frame_digest) if isinstance(frame_digest, str) else None
        )
        response["source_identity_consistent"] = bool(provenance.get("identity_consistent"))
        response["source_complete_code_coverage"] = bool(provenance.get("complete_code_coverage"))
        response["source_all_batches_cross_validated"] = bool(
            provenance.get("all_batches_cross_validated")
        )
        response["source_all_batches_raw_cross_validated"] = bool(
            provenance.get("all_batches_raw_cross_validated")
        )
        response["source_all_batches_adjusted_factor_validated"] = bool(
            provenance.get("all_batches_adjusted_factor_validated")
        )
        response["source_validation_ready"] = bool(
            response["source_all_batches_raw_cross_validated"]
            and response["source_all_batches_adjusted_factor_validated"]
        )
    source_research_eligible = (
        response["source_trust"]
        in {
            "public_cross_validated_research_only",
            "licensed",
            "exchange_authoritative",
        }
        and response["source_validation_ready"]
    )
    if not source_research_eligible:
        response["research_limitations"].append("research_grade_source_evidence_missing")
    response.update(
        {
            "available": True,
            "runtime_readable": True,
            "error_code": None,
            "date_start": frame.index.min().strftime("%Y-%m-%d"),
            "date_end": frame.index.max().strftime("%Y-%m-%d"),
            "n_dates": len(frame.index),
            "n_stocks": len(codes),
            "fields": fields,
            "codes_sha256": hashlib.sha256(",".join(codes).encode("utf-8")).hexdigest(),
            "ready_for_return_research": (
                source_research_eligible and response["price_adjustment"] in {"qfq", "hfq"}
            ),
            "ready_for_static_adjusted_return_research": (
                source_research_eligible and response["price_adjustment"] in {"qfq", "hfq"}
            ),
            "ready_for_execution_simulation": False,
            "ready_for_unbiased_tuning": False,
            "ready_for_real_tuning": False,
            "recommended_action": None,
        }
    )
    from backend.data.price_ledger import (
        PriceLedgerIntegrityError,
        PriceLedgerStore,
        PriceLedgerValidationError,
    )

    try:
        ledger = await asyncio.to_thread(
            PriceLedgerStore().inspect_readiness,
            scope_id=pool_id,
            start=response["date_start"],
            end=response["date_end"],
            security_codes=codes,
        )
    except PriceLedgerIntegrityError:
        ledger = PriceLedgerStore.unavailable_readiness(
            scope_id=pool_id,
            start=response["date_start"],
            end=response["date_end"],
            reason="price_ledger_integrity_invalid",
        )
    except PriceLedgerValidationError:
        ledger = PriceLedgerStore.unavailable_readiness(
            scope_id=pool_id,
            start=response["date_start"],
            end=response["date_end"],
            reason="price_ledger_identity_invalid",
        )
    if not ledger["ledger_available"]:
        ledger["legacy_cache_compatibility"] = {
            "available": True,
            "adjustment": response["price_adjustment"],
            "role": (
                "adjusted_return_research"
                if response["price_adjustment"] in {"qfq", "hfq"}
                else "raw_execution_unverified"
                if response["price_adjustment"] == "raw"
                else "unverified"
            ),
            "restriction": "legacy_cache_is_not_a_dual_price_ledger",
        }
    response["price_ledger"] = ledger
    response["ready_for_execution_simulation"] = bool(ledger["ready_for_execution_simulation"])
    response["ready_for_real_tuning"] = bool(ledger["ready_for_real_tuning"])
    # This endpoint has not bound the cache bytes to both the canonical price
    # batch and a verified PIT timeline. Keep the new gate closed even when the
    # legacy adjusted-price check is green.
    response["ready_for_unbiased_return_research"] = False
    return {"data": response}


@router.post("/cache/invalidate")
async def invalidate_cache(
    pool_id: str = Query(..., description="要失效的池 ID"),
    user: dict[str, Any] = Depends(require_permission("data:update")),
) -> dict[str, Any]:
    """手动失效指定池的缓存，下次访问时将重新拉取。"""
    import re

    if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", pool_id):
        raise HTTPException(status_code=400, detail="股票池 ID 格式无效")
    try:
        await _data_svc.c.invalidate(pool_id)
        return {"data": {"pool_id": pool_id, "message": f"缓存 {pool_id} 已失效"}}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"缓存失效失败: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════════════


def _df_to_records(df, start: str | None = None, end: str | None = None) -> list[dict]:
    """将 DataFrame 转为 API 返回的 records 格式。"""
    import pandas as pd

    records: list[dict] = []
    try:
        df_temp = df.reset_index()
        # 统一日期列名
        date_col = None
        for c in ("date", "日期", "index", "trade_date"):
            if c in df_temp.columns:
                date_col = c
                break
        if date_col is None:
            date_col = df_temp.columns[0]

        for _, row in df_temp.iterrows():
            d = str(row[date_col])[:10]
            if start and d < start:
                continue
            if end and d > end:
                continue

            rec: dict[str, Any] = {"date": d}
            for col in df_temp.columns:
                if col == date_col:
                    continue
                val = row[col]
                if isinstance(val, (pd.Timestamp,)):
                    val = str(val)[:10]
                elif isinstance(val, float) and pd.isna(val):
                    val = None
                rec[str(col)] = val
            records.append(rec)
    except Exception:
        pass
    return records


def _records_for_code(pivot, code: str) -> list[dict[str, Any]]:
    """Serialize one symbol from either OHLCV panel or legacy close pivot."""
    import pandas as pd

    if isinstance(pivot.columns, pd.MultiIndex):
        available_codes = {str(value) for value in pivot.columns.get_level_values(0)}
        if code not in available_codes:
            return []
        stock = pivot[code].copy()
        records: list[dict[str, Any]] = []
        for index, row in stock.iterrows():
            record: dict[str, Any] = {"date": str(index)[:10]}
            for field, value in row.items():
                if not pd.isna(value):
                    record[str(field)] = float(value)
            records.append(record)
        return records
    if code not in pivot.columns:
        return []
    return [
        {"date": str(index)[:10], "close": float(value)}
        for index, value in pivot[code].dropna().items()
    ]
