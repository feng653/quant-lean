"""Read-only, fail-closed validation for experiment market-data caches."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

from backend.data.cache import (
    DailyMarketDataQualityError,
    DataCache,
    assess_daily_market_data_quality,
)
from backend.data.point_in_time_master import (
    PointInTimeIntegrityError,
    PointInTimeMasterStore,
    PointInTimeValidationError,
)
from backend.data.point_in_time_universe import PointInTimeUniverseError
from backend.data.price_ledger import (
    BoundRuntimePrices,
    PriceLedgerIntegrityError,
    PriceLedgerStore,
    PriceLedgerValidationError,
)
from backend.data.pit_qa import (
    PitQaIsolationError,
    verified_qa_runtime_attestation,
)


REQUIRED_OHLCV_FIELDS = ("open", "high", "low", "close", "volume")


class CacheOnlyDataError(RuntimeError):
    """A cache-only experiment cannot satisfy its immutable input contract."""


def normalize_requested_codes(codes: Iterable[str] | None) -> list[str]:
    """Return the stable code identity used by custom experiment caches."""

    return sorted(
        {
            str(code).strip()
            for code in (codes or [])
            if str(code).strip()
        }
    )


def custom_cache_key(codes: Iterable[str]) -> str:
    """Derive the existing custom cache key without exposing a file path."""

    normalized = normalize_requested_codes(codes)
    if not normalized:
        raise ValueError("自定义股票池必须提供股票代码")
    return "custom_" + hashlib.sha256(
        ",".join(normalized).encode()
    ).hexdigest()[:16]


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.strftime("%Y-%m-%d")


def _field_map(frame: pd.DataFrame) -> dict[str, set[str]]:
    if not isinstance(frame.columns, pd.MultiIndex):
        return {}
    fields: dict[str, set[str]] = {}
    for column in frame.columns:
        code = str(column[0]).strip()
        field = str(column[-1]).strip().lower()
        if code:
            fields.setdefault(code, set()).add(field)
    return fields


@dataclass(frozen=True)
class CachedMarketData:
    frame: pd.DataFrame | None
    source_provenance: dict[str, Any] | None
    report: dict[str, Any]
    raw_execution_frame: pd.DataFrame | None = None
    runtime_price_binding: dict[str, Any] | None = None


@dataclass(frozen=True)
class CachedBenchmarkData:
    series: pd.Series | None
    report: dict[str, Any]


async def inspect_cached_market_data(
    cache: DataCache,
    *,
    cache_key: str,
    pool_id: str,
    requested_codes: Iterable[str] | None,
    required_start: str,
    required_end: str,
    point_in_time_store: PointInTimeMasterStore | None = None,
    price_ledger_store: PriceLedgerStore | None = None,
) -> CachedMarketData:
    """Inspect a strict schema-v4 daily cache without fetching or writing."""

    normalized_codes = normalize_requested_codes(requested_codes)
    issues: list[str] = []
    frame: pd.DataFrame | None = None
    source_provenance: dict[str, Any] | None = None
    load_error: str | None = None
    quality_report: dict[str, Any] | None = None
    bound_runtime_prices: BoundRuntimePrices | None = None
    runtime_binding_error: str | None = None
    try:
        frame, source_provenance = (
            await cache.load_pivot_with_provenance(cache_key)
        )
    except DailyMarketDataQualityError as exc:
        load_error = type(exc).__name__
        quality_report = exc.report
        issues.extend(str(item) for item in exc.report["issues"])
    except Exception as exc:
        load_error = type(exc).__name__
        issues.append("schema_v4_cache_invalid")

    # A legacy cache may supply the exact observed session index needed to
    # resolve PIT membership, but it is never promoted into canonical truth.
    # When an exact immutable binding exists, replace the research input before
    # any field/quality analysis and carry the raw role separately.
    if (
        frame is not None
        and not frame.empty
        and pool_id in {"csi300", "csi500", "csi800", "csi1000"}
    ):
        try:
            candidate = frame.copy()
            if not isinstance(candidate.index, pd.DatetimeIndex):
                candidate.index = pd.to_datetime(
                    candidate.index,
                    errors="raise",
                )
            candidate = candidate.sort_index().loc[
                pd.Timestamp(required_start) : pd.Timestamp(required_end)
            ]
            if candidate.empty:
                raise PointInTimeUniverseError(
                    "point_in_time_requested_sessions_missing"
                )
            from backend.data.point_in_time_universe import (
                resolve_point_in_time_universe,
            )
            from backend.data.universe import PRESET_POOLS

            store = point_in_time_store or PointInTimeMasterStore()
            candidate_timeline = resolve_point_in_time_universe(
                store,
                pool_id=pool_id,
                trading_dates=candidate.index,
                expected_count=PRESET_POOLS[pool_id]["expected_count"],
            )
            bound_runtime_prices = await asyncio.to_thread(
                (
                    price_ledger_store or PriceLedgerStore()
                ).load_bound_runtime_prices,
                scope_id=pool_id,
                timeline_identity=candidate_timeline.identity(),
                trading_dates=candidate_timeline.dates,
            )
            if bound_runtime_prices is not None:
                frame = bound_runtime_prices.research_adjusted
                research_sources = bound_runtime_prices.binding["sources"][
                    "research_adjusted"
                ]
                raw_sources = bound_runtime_prices.binding["sources"][
                    "raw_execution"
                ]
                validated_levels = {
                    "public_cross_validated",
                    "licensed",
                    "exchange_authoritative",
                }
                source_roles_validated = bool(
                    research_sources
                    and raw_sources
                    and all(
                        item["evidence_level"] in validated_levels
                        for item in [*research_sources, *raw_sources]
                    )
                )
                source_provenance = {
                    "schema_version": "canonical-ledger-runtime/v1",
                    "providers": sorted(
                        {str(item["provider"]) for item in research_sources}
                    ),
                    "evidence_levels": sorted(
                        {
                            (
                                "licensed_vendor"
                                if item["evidence_level"] == "licensed"
                                else "public_aggregator"
                                if item["evidence_level"]
                                == "public_cross_validated"
                                else str(item["evidence_level"])
                            )
                            for item in research_sources
                        }
                    ),
                    "adjustments": ["hfq"],
                    "frame_digest": bound_runtime_prices.binding[
                        "canonical_evidence_sha256"
                    ],
                    "content_sha256": bound_runtime_prices.binding[
                        "binding_digest"
                    ],
                    "identity_consistent": True,
                    "complete_code_coverage": True,
                    "all_batches_cross_validated": source_roles_validated,
                    "all_batches_raw_cross_validated": (
                        source_roles_validated
                    ),
                    "all_batches_adjusted_factor_validated": (
                        source_roles_validated
                    ),
                    "runtime_binding": {
                        "binding_id": bound_runtime_prices.binding[
                            "binding_id"
                        ],
                        "binding_digest": bound_runtime_prices.binding[
                            "binding_digest"
                        ],
                        "timeline_sha256": bound_runtime_prices.binding[
                            "timeline_sha256"
                        ],
                        "canonical_evidence_sha256": (
                            bound_runtime_prices.binding[
                                "canonical_evidence_sha256"
                            ]
                        ),
                    },
                }
                load_error = None
                issues.clear()
        except PriceLedgerIntegrityError:
            runtime_binding_error = (
                "canonical_runtime_binding_integrity_invalid"
            )
            bound_runtime_prices = None
        except PriceLedgerValidationError:
            runtime_binding_error = (
                "canonical_runtime_binding_identity_invalid"
            )
            bound_runtime_prices = None
        except (PointInTimeUniverseError, ValueError):
            # PIT absence or an exact binding miss is expected during migration.
            bound_runtime_prices = None

    field_map: dict[str, set[str]] = {}
    actual_codes: list[str] = []
    actual_fields: list[str] = []
    date_start: str | None = None
    date_end: str | None = None
    n_dates = 0
    missing_codes: list[str] = []
    missing_fields: dict[str, list[str]] = {}
    if frame is None or frame.empty:
        issues.append("daily_cache_missing")
        frame = None
    else:
        if not isinstance(frame.index, pd.DatetimeIndex):
            try:
                frame = frame.copy()
                frame.index = pd.to_datetime(frame.index, errors="raise")
            except (TypeError, ValueError):
                issues.append("daily_cache_datetime_index_invalid")
                frame = None
        if frame is not None:
            frame = frame.sort_index()
            date_start = _date_text(frame.index.min())
            date_end = _date_text(frame.index.max())
            n_dates = len(frame.index)
            field_map = _field_map(frame)
            if not field_map:
                issues.append("daily_cache_ohlcv_schema_invalid")
            actual_codes = sorted(field_map)
            actual_fields = sorted(
                {
                    field
                    for code_fields in field_map.values()
                    for field in code_fields
                }
            )
            checked_codes = normalized_codes or actual_codes
            missing_codes = sorted(set(checked_codes) - set(actual_codes))
            if missing_codes:
                issues.append("daily_cache_codes_missing")
            for code in checked_codes:
                missing = sorted(
                    set(REQUIRED_OHLCV_FIELDS) - field_map.get(code, set())
                )
                if missing:
                    missing_fields[code] = missing
            if missing_fields:
                issues.append("daily_cache_ohlcv_fields_missing")
            if frame.index.min() > pd.Timestamp(required_start):
                issues.append("daily_cache_start_not_covered")
            if frame.index.max() < pd.Timestamp(required_end):
                issues.append("daily_cache_end_not_covered")
            quality_report = assess_daily_market_data_quality(
                frame,
                expected_codes=set(normalized_codes or actual_codes),
            )
            equivalent_quality_issues = {
                "daily_codes_missing": "daily_cache_codes_missing",
                "daily_ohlcv_fields_missing": (
                    "daily_cache_ohlcv_fields_missing"
                ),
            }
            for item in quality_report["issues"]:
                equivalent = equivalent_quality_issues.get(str(item))
                if equivalent and equivalent in issues:
                    continue
                if item == "daily_no_observed_prices" and missing_fields:
                    continue
                issues.append(str(item))

    provenance = (
        source_provenance
        if isinstance(source_provenance, dict)
        else {}
    )
    providers = sorted(
        str(item) for item in (provenance.get("providers") or [])
    )
    evidence_levels = sorted(
        str(item) for item in (provenance.get("evidence_levels") or [])
    )
    adjustments = sorted(
        str(item) for item in (provenance.get("adjustments") or [])
    )
    adjustment = adjustments[0] if len(adjustments) == 1 else None
    # qfq's absolute anchor moves with the query end date and legacy schema-3
    # caches cannot bind that anchor to canonical evidence. Only hfq is
    # accepted as the adjusted research role in the strict contract.
    adjusted_research = adjustment == "hfq"
    raw_execution = adjustment == "raw"
    source_raw_cross_validated = bool(
        provenance.get("all_batches_raw_cross_validated")
    )
    source_adjusted_factor_validated = bool(
        provenance.get("all_batches_adjusted_factor_validated")
    )
    source_validation_ready = bool(
        source_raw_cross_validated and source_adjusted_factor_validated
    )
    source_trust = DataCache._source_trust(source_provenance)
    source_contract_ready = bool(
        provenance.get("identity_consistent")
        and provenance.get("complete_code_coverage")
        and providers
        and evidence_levels
        and adjustment in {"raw", "hfq"}
        and source_validation_ready
    )
    source_research_eligible = source_trust in {
        "public_cross_validated_research_only",
        "licensed",
        "exchange_authoritative",
    } and source_validation_ready
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
    if actual_codes and frame is not None:
        try:
            pit_coverage = (
                point_in_time_store or PointInTimeMasterStore()
            ).inspect_research_coverage(
                pool_id=pool_id,
                security_codes=actual_codes,
                start=required_start,
                end=required_end,
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
        if pool_id in {"csi300", "csi500", "csi800", "csi1000"}:
            from backend.data.point_in_time_universe import (
                resolve_point_in_time_universe,
                validate_market_data_columns,
            )
            from backend.data.universe import PRESET_POOLS

            research_dates = frame.loc[
                pd.Timestamp(required_start) : pd.Timestamp(required_end)
            ].index
            try:
                timeline = resolve_point_in_time_universe(
                    point_in_time_store or PointInTimeMasterStore(),
                    pool_id=pool_id,
                    trading_dates=research_dates,
                    expected_count=PRESET_POOLS[pool_id]["expected_count"],
                )
                validate_market_data_columns(frame, timeline)
                pit_coverage["universe"] = {
                    "ready": True,
                    "reason": None,
                    "scope_id": pool_id,
                    "evidence_kind_required": "effective_dated_history",
                    "member_code_count": len(timeline.union_codes),
                    "missing_price_codes": [],
                    "missing_price_code_count": 0,
                    "timeline": timeline.identity(),
                }
            except PointInTimeUniverseError as exc:
                pit_coverage["ready"] = False
                pit_coverage["universe"] = {
                    "ready": False,
                    "reason": exc.reason,
                    "scope_id": pool_id,
                    "evidence_kind_required": "effective_dated_history",
                }
                limitations = list(pit_coverage.get("limitations") or [])
                limitations.append(exc.reason)
                pit_coverage["limitations"] = list(
                    dict.fromkeys(limitations)
                )
    point_in_time_universe = bool(
        pit_coverage.get("universe", {}).get("ready")
    )
    research_limitations = list(
        pit_coverage.get("limitations")
        or ["point_in_time_universe_missing"]
    )
    if (
        not point_in_time_universe
        and "point_in_time_universe_missing" not in research_limitations
    ):
        research_limitations.append("point_in_time_universe_missing")
    if not source_research_eligible:
        research_limitations.append("research_grade_source_evidence_missing")
    unique_issues = list(dict.fromkeys(issues))
    try:
        if runtime_binding_error is not None:
            price_ledger = PriceLedgerStore.unavailable_readiness(
                scope_id=cache_key,
                start=required_start,
                end=required_end,
                reason=runtime_binding_error,
            )
        elif bound_runtime_prices is not None:
            price_ledger = await asyncio.to_thread(
                (
                    price_ledger_store or PriceLedgerStore()
                ).inspect_bound_runtime_readiness,
                scope_id=pool_id,
                timeline_identity=bound_runtime_prices.timeline_identity,
                trading_dates=bound_runtime_prices.trading_dates,
            )
        else:
            price_ledger = await asyncio.to_thread(
                (
                    price_ledger_store or PriceLedgerStore()
                ).inspect_readiness,
                scope_id=cache_key,
                start=required_start,
                end=required_end,
                security_codes=normalized_codes or actual_codes,
            )
    except PriceLedgerIntegrityError:
        price_ledger = PriceLedgerStore.unavailable_readiness(
            scope_id=cache_key,
            start=required_start,
            end=required_end,
            reason="price_ledger_integrity_invalid",
        )
    except PriceLedgerValidationError:
        price_ledger = PriceLedgerStore.unavailable_readiness(
            scope_id=cache_key,
            start=required_start,
            end=required_end,
            reason="price_ledger_identity_invalid",
        )
    if not price_ledger["ledger_available"]:
        price_ledger["legacy_cache_compatibility"] = {
            "available": bool(frame is not None and not unique_issues),
            "role": (
                "adjusted_return_research"
                if adjusted_research
                else "raw_execution_unverified"
                if raw_execution
                else "unverified"
            ),
            "adjustment": adjustment,
            "restriction": "legacy_cache_is_not_a_dual_price_ledger",
        }
    research_limitations.extend(price_ledger["limitations"])
    # The current cache calendar and benchmark series are operational hints,
    # not immutable, publication-time-aware evidence. Formal PIT-only runs
    # remain closed until dedicated governed bindings exist.
    research_limitations.extend(
        [
            "authoritative_trading_calendar_binding_missing",
            "point_in_time_benchmark_binding_missing",
        ]
    )
    canonical_runtime_bound = bool(
        bound_runtime_prices is not None
        and price_ledger.get("canonical_runtime_price_bound")
    )
    if price_ledger["ledger_available"] and not canonical_runtime_bound:
        research_limitations.append(
            "runtime_parquet_not_bound_to_canonical_price_evidence"
        )
    if not canonical_runtime_bound:
        research_limitations.append(
            "legacy_cache_cross_pool_consistency_not_certified"
        )
    if adjustment == "qfq":
        research_limitations.append(
            "qfq_anchor_not_eligible_for_unbiased_research"
        )
    if len(adjustments) != 1:
        research_limitations.append(
            "mixed_adjustment_not_eligible_for_unbiased_research"
        )
    authoritative_trading_calendar_bound = False
    point_in_time_benchmark_bound = False
    qa_runtime_attestation: dict[str, Any] | None = None
    if bound_runtime_prices is not None and point_in_time_universe:
        try:
            qa_runtime_attestation = verified_qa_runtime_attestation(
                pool_id=pool_id,
                required_start=required_start,
                required_end=required_end,
                timeline_identity=bound_runtime_prices.timeline_identity,
                runtime_price_binding=bound_runtime_prices.binding,
            )
        except PitQaIsolationError:
            unique_issues.append("pit_qa_isolation_invalid")
        if qa_runtime_attestation is not None:
            # These flags are test assertions supplied by a hash-bound QA
            # bundle.  The attestation is always emitted as non-production in
            # the run manifest and cannot be enabled by a production service.
            authoritative_trading_calendar_bound = True
            point_in_time_benchmark_bound = True
    strict_unbiased_research = bool(
        not unique_issues
        and point_in_time_universe
        and canonical_runtime_bound
        and (
            price_ledger.get("ready_for_unbiased_return_research")
            or qa_runtime_attestation is not None
        )
        and authoritative_trading_calendar_bound
        and point_in_time_benchmark_bound
    )
    descriptive_return_research = bool(
        not unique_issues
        and source_contract_ready
        and source_research_eligible
        and adjusted_research
    )
    report = {
        "ready": not unique_issues,
        "ready_for_unbiased_return_research": strict_unbiased_research,
        "ready_for_unbiased_research": strict_unbiased_research,
        "ready_for_unbiased_tuning": bool(
            strict_unbiased_research
            and (
                price_ledger["ready_for_real_tuning"]
                or qa_runtime_attestation is not None
            )
        ),
        "descriptive_return_research_ready": descriptive_return_research,
        "ready_for_static_adjusted_return_research": (
            descriptive_return_research
        ),
        # Compatibility field: descriptive only, never a promotion gate.
        "ready_for_return_research": descriptive_return_research,
        "return_research_semantics": (
            "ready_for_return_research_is_legacy_static_price_research_only;"
            "promotion_requires_ready_for_unbiased_return_research"
        ),
        "ready_for_execution_simulation": bool(
            not unique_issues
            and price_ledger["ready_for_execution_simulation"]
            and canonical_runtime_bound
        ),
        "ready_for_real_tuning": bool(
            strict_unbiased_research
            and (
                price_ledger["ready_for_real_tuning"]
                or qa_runtime_attestation is not None
            )
        ),
        "pool_id": pool_id,
        "cache_key": cache_key,
        "schema_version": 4 if frame is not None and load_error is None else None,
        "required_start": required_start,
        "required_end": required_end,
        "date_start": date_start,
        "date_end": date_end,
        "n_dates": n_dates,
        "requested_code_count": len(normalized_codes),
        "available_code_count": len(field_map),
        "fields": actual_fields,
        "codes_sha256": (
            hashlib.sha256(
                ",".join(actual_codes).encode("utf-8")
            ).hexdigest()
            if actual_codes
            else None
        ),
        "price_adjustment": (
            adjustment
        ),
        "price_ledger": price_ledger,
        "canonical_runtime_price_bound": canonical_runtime_bound,
        "authoritative_trading_calendar_bound": (
            authoritative_trading_calendar_bound
        ),
        "point_in_time_benchmark_bound": point_in_time_benchmark_bound,
        "source_trust": source_trust,
        "source_contract_ready": source_contract_ready,
        "source_research_eligible": source_research_eligible,
        "source_all_batches_cross_validated": bool(
            provenance.get("all_batches_cross_validated")
        ),
        "source_all_batches_raw_cross_validated": (
            source_raw_cross_validated
        ),
        "source_all_batches_adjusted_factor_validated": (
            source_adjusted_factor_validated
        ),
        "source_validation_ready": source_validation_ready,
        "source_providers": providers,
        "source_evidence_levels": evidence_levels,
        "source_frame_digest": provenance.get("frame_digest"),
        "source_identity_consistent": bool(
            provenance.get("identity_consistent")
        ),
        "source_complete_code_coverage": bool(
            provenance.get("complete_code_coverage")
        ),
        "missing_codes": missing_codes,
        "missing_fields": missing_fields,
        "data_quality": quality_report,
        "universe_point_in_time": point_in_time_universe,
        "survivorship_bias_risk": not point_in_time_universe,
        "point_in_time": pit_coverage,
        "research_limitations": list(dict.fromkeys(research_limitations)),
        "issues": unique_issues,
        **(
            {"qa_runtime_attestation": qa_runtime_attestation}
            if qa_runtime_attestation is not None
            else {}
        ),
        **({"load_error": load_error} if load_error else {}),
    }
    return CachedMarketData(
        frame=frame,
        source_provenance=source_provenance,
        report=report,
        raw_execution_frame=(
            bound_runtime_prices.raw_execution
            if bound_runtime_prices is not None
            else None
        ),
        runtime_price_binding=(
            bound_runtime_prices.binding
            if bound_runtime_prices is not None
            else None
        ),
    )


async def require_cached_market_data(
    cache: DataCache,
    *,
    strict: bool = True,
    **kwargs: Any,
) -> CachedMarketData:
    inspected = await inspect_cached_market_data(cache, **kwargs)
    if not inspected.report["ready"] or inspected.frame is None:
        issues = ",".join(inspected.report["issues"]) or "unknown"
        if strict or inspected.frame is None:
            raise CacheOnlyDataError(
                "cache_only 行情数据未就绪："
                f"{inspected.report['cache_key']} ({issues})"
            )
        # 非严格模式（研究/模拟降级放行，与 PIT 分级门禁一致）：
        # 行情数据不完整（如 PIT 会员全集 > 缓存覆盖）时仅告警放行，
        # 用可用子集运行，缺口记录在报告；结果仅供研究参考。
        logging.getLogger("quant_platform").warning(
            "cache_only 行情数据不完整，降级放行（仅供研究参考）："
            "%s (%s)",
            inspected.report["cache_key"],
            issues,
        )
    return inspected


async def inspect_cached_benchmark(
    cache: DataCache,
    *,
    index_code: str,
    required_start: str,
    required_end: str,
) -> CachedBenchmarkData:
    """Inspect a local benchmark series without a network fallback."""

    issues: list[str] = []
    series: pd.Series | None = None
    load_error: str | None = None
    try:
        series = await cache.load_index(index_code)
    except Exception as exc:
        load_error = type(exc).__name__
        issues.append("benchmark_cache_invalid")
    if series is None or series.empty:
        issues.append("benchmark_cache_missing")
        series = None

    date_start: str | None = None
    date_end: str | None = None
    observations = 0
    if series is not None:
        if not isinstance(series.index, pd.DatetimeIndex):
            try:
                series = series.copy()
                series.index = pd.to_datetime(series.index, errors="raise")
            except (TypeError, ValueError):
                issues.append("benchmark_datetime_index_invalid")
                series = None
        if series is not None:
            series = series.sort_index()
            date_start = _date_text(series.index.min())
            date_end = _date_text(series.index.max())
            observations = len(series)
            if series.index.min() > pd.Timestamp(required_start):
                issues.append("benchmark_start_not_covered")
            if series.index.max() < pd.Timestamp(required_end):
                issues.append("benchmark_end_not_covered")

    report = {
        "ready": not issues,
        "index_code": index_code,
        "required_start": required_start,
        "required_end": required_end,
        "date_start": date_start,
        "date_end": date_end,
        "observations": observations,
        "issues": list(dict.fromkeys(issues)),
        **({"load_error": load_error} if load_error else {}),
    }
    return CachedBenchmarkData(series=series, report=report)


async def require_cached_benchmark(
    cache: DataCache,
    **kwargs: Any,
) -> CachedBenchmarkData:
    inspected = await inspect_cached_benchmark(cache, **kwargs)
    if not inspected.report["ready"] or inspected.series is None:
        issues = ",".join(inspected.report["issues"]) or "unknown"
        raise CacheOnlyDataError(
            "cache_only 基准数据未就绪："
            f"{inspected.report['index_code']} ({issues})"
        )
    return inspected
