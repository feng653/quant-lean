"""数据缓存管理 —— 以 PyArrow Parquet 格式持久化 pivot 行情数据，支持增量更新.

目录结构:
    data/cache/daily/{pool_id}.parquet   — 池级 (code, field) OHLCV 面板
    data/cache/daily/{pool_id}.meta.json — 缓存元信息（日期范围/股票数/文件大小）
    data/cache/indexes/{index_code}.parquet — 独立指数收盘序列
    data/cache/calendar.json             — 交易日历缓存
    data/cache/industries.json           — 行业分类缓存
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backend.config import settings
from backend.data.offload import run_data_integrity
from backend.data.generation_manifest import (
    GenerationManifestError,
    GenerationManifestStore,
    GenerationView,
)
from backend.data.source_validation import (
    CrossSourceConflictError,
    SourceEvidenceError,
    build_cache_source_provenance,
    fetch_daily_with_evidence,
    validate_cache_source_provenance,
)

logger = logging.getLogger("quant_platform.data.cache")

POOL_BENCHMARK_MAP: dict[str, str] = {
    "csi300": "000300",
    "csi500": "000905",
    "csi800": "000906",
    "csi1000": "000852",
    "all_a": "000300",
    "custom": "000300",
}
DEFAULT_BENCHMARK_INDEX = "000300"
_ADJUSTMENT_OVERLAP_DAYS = 45
_BENCHMARK_REQUIRED_BUFFER_DAYS = 10
_BENCHMARK_FETCH_SAFETY_DAYS = 7
# The A-share continuous session closes at 15:00 Asia/Shanghai.  Public daily
# data providers can publish later, so automatic refreshes conservatively wait
# until 18:00 local exchange time before treating the current session as
# completed.  This deliberately trades a few hours of freshness for avoiding
# partial/current-session rebuilds.
_MARKET_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_MARKET_DATA_AVAILABLE_CUTOFF = time(hour=18)
_ADJUSTED_PRICE_FIELDS = ("open", "high", "low", "close")
DAILY_CACHE_SCHEMA_VERSION = 4
LEGACY_ADJUSTED_CACHE_SCHEMA_VERSION = 3
ADJUSTMENT_CONSISTENCY_VERSION = "single-adjustment-overlap-v2"
LEGACY_QFQ_CONSISTENCY_VERSION = "qfq-overlap-v1"
DAILY_PRICE_QUALITY_SCHEMA = "daily-price-quality/v1"
SUPPORTED_PRICE_ADJUSTMENTS = frozenset({"raw", "qfq", "hfq"})


class LegacyAdjustedCacheError(RuntimeError):
    """A cache lacks a runtime-verifiable adjustment/provenance contract."""


class LegacyRuntimeDataDisabledError(RuntimeError):
    """A legacy/current-snapshot refresh was attempted in PIT-only mode."""


class DailyMarketDataQualityError(RuntimeError):
    """OHLCV bytes failed the non-negotiable research-data quality gate."""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        issues = ",".join(str(item) for item in report.get("issues", []))
        super().__init__(
            "daily market-data quality gate failed"
            + (f": {issues}" if issues else "")
        )


class BenchmarkRefreshError(RuntimeError):
    """A configured benchmark could not satisfy its persisted coverage gate."""

    def __init__(self, message: str, audit_context: dict[str, Any]):
        self.audit_context = dict(audit_context)
        super().__init__(message)


def _market_now(now: datetime | pd.Timestamp | None = None) -> datetime:
    """Return an explicitly Asia/Shanghai-localized wall clock.

    Injected naive values are interpreted as exchange-local wall time rather
    than the host timezone, keeping tests and scheduled jobs deterministic.
    """

    if now is None:
        return datetime.now(tz=_MARKET_TIME_ZONE)
    value = (
        now.to_pydatetime()
        if isinstance(now, pd.Timestamp)
        else now
    )
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime or pandas Timestamp")
    if value.tzinfo is None:
        return value.replace(tzinfo=_MARKET_TIME_ZONE)
    return value.astimezone(_MARKET_TIME_ZONE)


def _latest_completed_trading_day(
    trading_days: list[str],
    *,
    now: datetime | pd.Timestamp | None = None,
) -> str | None:
    """Select the latest exchange session whose daily bar should be complete."""

    local_now = _market_now(now)
    eligible_date = local_now.date()
    if local_now.time().replace(tzinfo=None) < _MARKET_DATA_AVAILABLE_CUTOFF:
        eligible_date -= timedelta(days=1)

    eligible_days: list[date] = []
    for raw_day in trading_days:
        try:
            trading_date = date.fromisoformat(str(raw_day))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"trading calendar contains an invalid date: {raw_day!r}"
            ) from exc
        if trading_date <= eligible_date:
            eligible_days.append(trading_date)
    if not eligible_days:
        return None
    return max(eligible_days).isoformat()


def _validate_pool_id(pool_id: str) -> str:
    if not isinstance(pool_id, str) or not re.fullmatch(
        r"[0-9A-Za-z_-]{1,64}", pool_id
    ):
        raise ValueError("Invalid pool_id")
    return pool_id


def resolve_pool_benchmark(pool_id: str | None) -> str:
    """Resolve the accepted pool-to-benchmark product decision."""
    normalized = (pool_id or "").strip().lower()
    return POOL_BENCHMARK_MAP.get(normalized, DEFAULT_BENCHMARK_INDEX)


def _validate_index_code(index_code: str) -> str:
    normalized = index_code.strip()
    if not re.fullmatch(r"\d{6}", normalized):
        raise ValueError("index_code must be exactly 6 digits")
    return normalized


def _normalize_index_series(series: pd.Series) -> pd.Series:
    if not isinstance(series, pd.Series):
        raise TypeError("index data must be a pandas Series")
    normalized = pd.Series(
        pd.to_numeric(series.to_numpy(), errors="coerce"),
        index=pd.to_datetime(series.index, errors="coerce"),
        name="close",
        dtype="float64",
    )
    valid_index = ~normalized.index.isna()
    normalized = normalized[valid_index].dropna()
    if normalized.empty:
        normalized.index = pd.DatetimeIndex([], name="date")
        return normalized
    normalized.index = pd.DatetimeIndex(normalized.index).tz_localize(None)
    normalized.index.name = "date"
    normalized = normalized[~normalized.index.duplicated(keep="last")]
    return normalized.sort_index()


def has_price_field(df: pd.DataFrame, field: str) -> bool:
    """Return whether a market-data frame contains a named field."""
    if not isinstance(df.columns, pd.MultiIndex):
        return False
    return field.lower() in {
        str(value).lower() for value in df.columns.get_level_values(-1)
    }


def _normalize_daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    if not isinstance(normalized.index, pd.DatetimeIndex):
        if "date" in normalized.columns:
            normalized["date"] = pd.to_datetime(
                normalized["date"], errors="coerce"
            )
            normalized = normalized.set_index("date")
        else:
            normalized.index = pd.to_datetime(
                normalized.index, errors="coerce"
            )
    normalized = normalized[~normalized.index.isna()]
    if not normalized.empty:
        normalized.index = normalized.index.tz_localize(None)
        normalized = normalized[
            ~normalized.index.duplicated(keep="last")
        ].sort_index()
    normalized.index.name = normalized.index.name or "date"
    return normalized


def _merge_daily_frames(chunks: list[pd.DataFrame]) -> pd.DataFrame:
    normalized = [
        _normalize_daily_frame(chunk) for chunk in chunks if not chunk.empty
    ]
    if not normalized:
        return pd.DataFrame()
    merged = normalized[0]
    for new in normalized[1:]:
        # New non-null observations win while cached values fill sparse columns.
        merged = new.combine_first(merged)
    return merged.sort_index()


def _daily_codes(frame: pd.DataFrame) -> set[str]:
    if frame.empty or not isinstance(frame.columns, pd.MultiIndex):
        return set()
    return {
        str(value).strip()
        for value in frame.columns.get_level_values(0)
        if str(value).strip()
    }


def assess_daily_market_data_quality(
    frame: pd.DataFrame,
    *,
    expected_codes: set[str] | None = None,
    today: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """Return a bounded, non-mutating OHLCV quality report.

    Missing rows are allowed for suspensions and pre-listing periods.  A row
    with any observed OHLC value must, however, contain a complete finite,
    strictly-positive OHLC tuple with a non-negative finite volume.
    """

    issues: list[str] = []
    examples: list[dict[str, str]] = []
    counters = {
        "duplicate_date_count": 0,
        "duplicate_column_count": 0,
        "future_date_count": 0,
        "non_numeric_count": 0,
        "non_finite_count": 0,
        "non_positive_price_count": 0,
        "negative_volume_count": 0,
        "partial_ohlc_row_count": 0,
        "missing_volume_row_count": 0,
        "ohlc_logic_violation_count": 0,
    }

    def record(issue: str, **context: Any) -> None:
        if issue not in issues:
            issues.append(issue)
        if context and len(examples) < 20:
            examples.append(
                {
                    key: str(value)
                    for key, value in context.items()
                    if value is not None
                }
            )

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        record("daily_frame_empty")
        return {
            "schema_version": DAILY_PRICE_QUALITY_SCHEMA,
            "ready": False,
            "n_dates": 0,
            "n_codes": 0,
            "observed_price_rows": 0,
            **counters,
            "missing_codes": sorted(expected_codes or set()),
            "issues": issues,
            "examples": examples,
        }
    if not isinstance(frame.index, pd.DatetimeIndex):
        record("daily_datetime_index_invalid")
        index = pd.to_datetime(frame.index, errors="coerce")
    else:
        index = frame.index
    invalid_dates = pd.isna(index)
    if bool(np.asarray(invalid_dates).any()):
        record(
            "daily_datetime_index_invalid",
            count=int(np.asarray(invalid_dates).sum()),
        )
    duplicate_dates = index.duplicated(keep=False)
    counters["duplicate_date_count"] = int(duplicate_dates.sum())
    if counters["duplicate_date_count"]:
        record(
            "daily_duplicate_dates",
            count=counters["duplicate_date_count"],
        )
    if not index.is_monotonic_increasing:
        record("daily_dates_not_monotonic")
    valid_index = pd.DatetimeIndex(index[~invalid_dates])
    if valid_index.tz is not None:
        valid_index = valid_index.tz_convert(None)
    today = (today or pd.Timestamp.now()).normalize()
    future_dates = valid_index.normalize() > today
    counters["future_date_count"] = int(future_dates.sum())
    if counters["future_date_count"]:
        record(
            "daily_future_dates",
            count=counters["future_date_count"],
        )

    if not isinstance(frame.columns, pd.MultiIndex) or frame.columns.nlevels < 2:
        record("daily_ohlcv_schema_invalid")
        codes: set[str] = set()
        field_columns: dict[tuple[str, str], Any] = {}
    else:
        duplicate_columns = frame.columns.duplicated(keep=False)
        counters["duplicate_column_count"] = int(duplicate_columns.sum())
        if counters["duplicate_column_count"]:
            record(
                "daily_duplicate_columns",
                count=counters["duplicate_column_count"],
            )
        field_columns = {}
        for column in frame.columns:
            key = (
                str(column[0]).strip(),
                str(column[-1]).strip().lower(),
            )
            if key[0] and key not in field_columns:
                field_columns[key] = column
        codes = {code for code, _ in field_columns}

    required_fields = {"open", "high", "low", "close", "volume"}
    missing_fields: dict[str, list[str]] = {}
    for code in sorted(codes):
        missing = sorted(
            required_fields
            - {field for candidate, field in field_columns if candidate == code}
        )
        if missing:
            missing_fields[code] = missing
    if missing_fields:
        record("daily_ohlcv_fields_missing", code_count=len(missing_fields))

    missing_codes = sorted((expected_codes or set()) - codes)
    unexpected_codes = sorted(codes - (expected_codes or codes))
    if missing_codes:
        record("daily_codes_missing", count=len(missing_codes))
    if unexpected_codes:
        record("daily_unexpected_codes", count=len(unexpected_codes))

    observed_price_rows = 0
    for code in sorted(codes):
        if code in missing_fields:
            continue
        numeric: dict[str, pd.Series] = {}
        for field in required_fields:
            original = frame[field_columns[(code, field)]]
            converted = pd.to_numeric(original, errors="coerce")
            non_numeric = original.notna() & converted.isna()
            if bool(non_numeric.any()):
                count = int(non_numeric.sum())
                counters["non_numeric_count"] += count
                record(
                    "daily_non_numeric_values",
                    code=code,
                    field=field,
                    count=count,
                )
            finite = pd.Series(
                np.isfinite(converted.to_numpy(dtype="float64", na_value=np.nan)),
                index=converted.index,
            )
            non_finite = converted.notna() & ~finite
            if bool(non_finite.any()):
                count = int(non_finite.sum())
                counters["non_finite_count"] += count
                record(
                    "daily_non_finite_values",
                    code=code,
                    field=field,
                    count=count,
                )
            numeric[field] = converted

        ohlc = pd.concat(
            [numeric[field] for field in ("open", "high", "low", "close")],
            axis=1,
            keys=("open", "high", "low", "close"),
        )
        observed = ohlc.notna().any(axis=1)
        complete = ohlc.notna().all(axis=1)
        observed_price_rows += int(observed.sum())
        partial = observed & ~complete
        if bool(partial.any()):
            count = int(partial.sum())
            counters["partial_ohlc_row_count"] += count
            record("daily_partial_ohlc_rows", code=code, count=count)
        non_positive = (ohlc <= 0).any(axis=1)
        if bool(non_positive.any()):
            count = int(non_positive.sum())
            counters["non_positive_price_count"] += count
            record("daily_non_positive_prices", code=code, count=count)
        volume_observed = numeric["volume"].notna()
        missing_volume = complete & ~volume_observed
        if bool(missing_volume.any()):
            count = int(missing_volume.sum())
            counters["missing_volume_row_count"] += count
            record("daily_volume_missing_with_prices", code=code, count=count)
        negative_volume = numeric["volume"] < 0
        if bool(negative_volume.any()):
            count = int(negative_volume.sum())
            counters["negative_volume_count"] += count
            record("daily_negative_volume", code=code, count=count)
        logical = complete & (
            (ohlc["high"] < ohlc[["open", "low", "close"]].max(axis=1))
            | (ohlc["low"] > ohlc[["open", "high", "close"]].min(axis=1))
        )
        if bool(logical.any()):
            count = int(logical.sum())
            counters["ohlc_logic_violation_count"] += count
            record("daily_ohlc_logic_invalid", code=code, count=count)

    if not observed_price_rows:
        record("daily_no_observed_prices")
    return {
        "schema_version": DAILY_PRICE_QUALITY_SCHEMA,
        "ready": not issues,
        "n_dates": len(frame.index),
        "n_codes": len(codes),
        "observed_price_rows": observed_price_rows,
        **counters,
        "missing_codes": missing_codes,
        "unexpected_codes": unexpected_codes,
        "missing_fields": missing_fields,
        "issues": issues,
        "examples": examples,
    }


def require_daily_market_data_quality(
    frame: pd.DataFrame,
    *,
    expected_codes: set[str] | None = None,
) -> dict[str, Any]:
    report = assess_daily_market_data_quality(
        frame,
        expected_codes=expected_codes,
    )
    if not report["ready"]:
        raise DailyMarketDataQualityError(report)
    return report


def _adjusted_column_map(
    frame: pd.DataFrame,
) -> dict[tuple[str, str], Any]:
    if not isinstance(frame.columns, pd.MultiIndex):
        return {}
    result: dict[tuple[str, str], Any] = {}
    for column in frame.columns:
        key = (
            str(column[0]).strip(),
            str(column[-1]).strip().lower(),
        )
        if key[1] in _ADJUSTED_PRICE_FIELDS:
            result.setdefault(key, column)
    return result


def _requires_full_adjustment_refresh(
    cached: pd.DataFrame,
    fetched: pd.DataFrame,
) -> bool:
    """Detect qfq revisions before combining observations from two fetches.

    Forward-adjusted history can change after a corporate action.  Every code
    with newly fetched observations must therefore have comparable overlap
    against the cache, and all overlapping adjusted prices must agree.
    """
    if cached.empty or fetched.empty:
        return False
    cached = _normalize_daily_frame(cached)
    fetched = _normalize_daily_frame(fetched)
    cached_columns = _adjusted_column_map(cached)
    fetched_columns = _adjusted_column_map(fetched)
    if not cached_columns or not fetched_columns:
        return True

    cache_start = cached.index.min()
    cache_end = cached.index.max()
    common_codes = sorted(
        {key[0] for key in cached_columns}
        & {key[0] for key in fetched_columns}
    )
    for code in common_codes:
        close_key = (code, "close")
        if close_key not in cached_columns or close_key not in fetched_columns:
            return True
        fetched_close = pd.to_numeric(
            fetched[fetched_columns[close_key]],
            errors="coerce",
        )
        outside_cache = (
            (fetched_close.index < cache_start)
            | (fetched_close.index > cache_end)
        ) & fetched_close.notna()
        if not bool(outside_cache.any()):
            continue

        comparable_close = pd.concat(
            {
                "cached": pd.to_numeric(
                    cached[cached_columns[close_key]],
                    errors="coerce",
                ),
                "fetched": pd.to_numeric(
                    fetched[fetched_columns[close_key]],
                    errors="coerce",
                ),
            },
            axis=1,
            join="inner",
        ).dropna()
        if comparable_close.empty:
            return True

        for field in _ADJUSTED_PRICE_FIELDS:
            key = (code, field)
            if key not in cached_columns or key not in fetched_columns:
                return True
            comparable = pd.concat(
                {
                    "cached": pd.to_numeric(
                        cached[cached_columns[key]],
                        errors="coerce",
                    ),
                    "fetched": pd.to_numeric(
                        fetched[fetched_columns[key]],
                        errors="coerce",
                    ),
                },
                axis=1,
                join="inner",
            ).dropna()
            if comparable.empty:
                return True
            if not bool(
                np.isclose(
                    comparable["cached"].to_numpy(dtype="float64"),
                    comparable["fetched"].to_numpy(dtype="float64"),
                    rtol=1e-12,
                    atol=1e-12,
                ).all()
            ):
                return True
    return False


class DataCache:
    """数据缓存管理器。

    核心职责:
    - get_or_fetch: 缓存未命中或过时时从 DataSource 拉取
    - save_pivot / load_pivot: Parquet 读写
    - get_cache_info: 返回缓存元信息
    - auto_update: 增量拉取缺失数据并合并
    """

    _pool_locks: dict[str, asyncio.Lock] = {}
    _pool_locks_guard = threading.Lock()

    def __init__(self, cache_dir: str | None = None) -> None:
        """初始化缓存管理器。

        Args:
            cache_dir: 缓存根目录。None 则使用 config.DATA_CACHE_DIR。
        """
        if cache_dir is None:
            self._root = settings.abs_path(settings.DATA_CACHE_DIR)
        else:
            self._root = Path(cache_dir)

        self._daily_dir = self._root / "daily"
        self._daily_dir.mkdir(parents=True, exist_ok=True)
        self._daily_generations = GenerationManifestStore(
            self._daily_dir,
            required_artifacts={"pivot", "metadata"},
        )
        self._index_dir = self._root / "indexes"
        self._index_dir.mkdir(parents=True, exist_ok=True)

    def _pool_lock(self, pool_id: str) -> asyncio.Lock:
        key = str(
            (self._daily_dir / f"{_validate_pool_id(pool_id)}.parquet").resolve()
        ).lower()
        with self._pool_locks_guard:
            return self._pool_locks.setdefault(key, asyncio.Lock())

    # ── 核心读写接口 ────────────────────────────────────────────────────────

    async def get_or_fetch(
        self,
        pool_id: str,
        source,   # DataSource
        force: bool = False,
        start: str = "2015-01-01",
        end: str | None = None,
    ) -> pd.DataFrame:
        """获取池数据：缓存命中直接读，否则从 source 拉取并缓存。

        Args:
            pool_id: 池标识，如 "csi300"。
            source:  DataSource 实例，用于获取数据。
            force:   True 则跳过缓存，强制重新拉取。
            start:   数据起始日期。
            end:     数据截止日期。None 则使用今天。

        Returns:
            OHLCV panel，columns 为 ``(code, field)``。
        """
        pool_id = _validate_pool_id(pool_id)
        start_ts = pd.Timestamp(start).normalize()
        end_ts = pd.Timestamp(
            end or pd.Timestamp.now().strftime("%Y-%m-%d")
        ).normalize()
        if start_ts > end_ts:
            raise ValueError("start must be on or before end")

        cached = None if force else await self.load_pivot(pool_id)
        if cached is not None and not cached.empty:
            cached = _normalize_daily_frame(cached)
            if not has_price_field(cached, "open"):
                logger.warning(
                    "Legacy close-only cache for pool '%s'; rebuilding OHLCV data",
                    pool_id,
                )
                cached = None
            elif not self._has_verified_adjustment_metadata(pool_id):
                raise LegacyAdjustedCacheError(
                    f"Pool '{pool_id}' uses a legacy adjusted cache that may mix "
                    "different corporate-action adjustment generations. "
                    "Run a controlled force refresh before research."
                )
            else:
                await run_data_integrity(
                    self._read_source_provenance,
                    pool_id,
                    frame=cached,
                )

        fetch_ranges: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        full_range_start = start_ts
        full_range_end = end_ts
        if cached is None or cached.empty:
            fetch_ranges.append((start_ts, end_ts))
        else:
            cache_start = cached.index.min().normalize()
            cache_end = cached.index.max().normalize()
            full_range_start = min(start_ts, cache_start)
            full_range_end = max(end_ts, cache_end)
            if start_ts < cache_start:
                fetch_ranges.append(
                    (
                        start_ts,
                        min(
                            cache_end,
                            cache_start
                            + pd.Timedelta(days=_ADJUSTMENT_OVERLAP_DAYS),
                        ),
                    )
                )
            if end_ts > cache_end:
                fetch_ranges.append(
                    (
                        max(
                            cache_start,
                            cache_end
                            - pd.Timedelta(days=_ADJUSTMENT_OVERLAP_DAYS),
                        ),
                        end_ts,
                    )
                )

        if not fetch_ranges:
            logger.info(
                "Cache hit for pool '%s': %d dates x %d columns",
                pool_id,
                len(cached),
                len(cached.columns),
            )
            return cached[
                (cached.index >= start_ts) & (cached.index <= end_ts)
            ]

        from .universe import UniverseManager
        universe = UniverseManager(source, self)
        codes = await universe.get_pool_codes(pool_id)

        if not codes:
            logger.warning("Pool '%s' returned empty code list", pool_id)
            if cached is None:
                return pd.DataFrame()
            return cached[
                (cached.index >= start_ts) & (cached.index <= end_ts)
            ]

        full_refresh = (
            cached is not None
            and not cached.empty
            and _daily_codes(cached) != set(codes)
        )
        if full_refresh:
            logger.warning(
                "Pool '%s' membership changed; rebuilding adjusted cache instead "
                "of combining different constituent snapshots",
                pool_id,
            )
            fetch_ranges = [(full_range_start, full_range_end)]

        fetched_chunks: list[pd.DataFrame] = []
        fetched_evidence: list[dict[str, Any]] = []
        for range_start, range_end in fetch_ranges:
            if range_start > range_end:
                continue
            range_start_text = range_start.strftime("%Y-%m-%d")
            range_end_text = range_end.strftime("%Y-%m-%d")
            logger.info(
                "Fetching daily data for pool '%s': %d codes, %s → %s",
                pool_id,
                len(codes),
                range_start_text,
                range_end_text,
            )
            fetch_result = await fetch_daily_with_evidence(
                source,
                codes,
                range_start_text,
                range_end_text,
            )
            fetched = fetch_result.frame
            if not fetched.empty:
                if not fetch_result.evidence["complete_code_coverage"]:
                    raise SourceEvidenceError(
                        "daily refresh did not cover every requested code; "
                        "existing cache was left unchanged"
                    )
                await run_data_integrity(
                    require_daily_market_data_quality,
                    fetched,
                    expected_codes=set(codes),
                )
                fetched_chunks.append(fetched)
                fetched_evidence.append(fetch_result.evidence)

        if cached is None and not fetched_chunks:
            raise SourceEvidenceError(
                "controlled daily refresh returned no validated observations; "
                "existing cache was left unchanged"
            )

        adjustment_refresh_required = False
        if not full_refresh and cached is not None and fetched_chunks:
            adjustment_refresh_required = await run_data_integrity(
                lambda: any(
                    _requires_full_adjustment_refresh(cached, fetched)
                    for fetched in fetched_chunks
                )
            )
        if adjustment_refresh_required:
            logger.warning(
                "Forward-adjusted price history changed for pool '%s'; "
                "rebuilding the complete cache to keep one adjustment basis",
                pool_id,
            )
            rebuild_result = await fetch_daily_with_evidence(
                source,
                codes,
                full_range_start.strftime("%Y-%m-%d"),
                full_range_end.strftime("%Y-%m-%d"),
            )
            rebuilt = rebuild_result.frame
            if rebuilt.empty:
                raise RuntimeError(
                    "adjusted cache rebuild returned no data; existing cache was "
                    "left unchanged"
                )
            if not rebuild_result.evidence["complete_code_coverage"]:
                raise SourceEvidenceError(
                    "full daily refresh did not cover every requested code; "
                    "existing cache was left unchanged"
                )
            await run_data_integrity(
                require_daily_market_data_quality,
                rebuilt,
                expected_codes=set(codes),
            )
            fetched_chunks = [rebuilt]
            fetched_evidence = [rebuild_result.evidence]
            full_refresh = True

        chunks = (
            fetched_chunks
            if full_refresh or cached is None
            else [cached, *fetched_chunks]
        )
        merged = await run_data_integrity(_merge_daily_frames, chunks)
        if not merged.empty and fetched_chunks:
            prior_batches: list[dict[str, Any]] = []
            if not full_refresh and cached is not None:
                prior = await run_data_integrity(
                    self._read_source_provenance,
                    pool_id,
                    frame=cached,
                )
                prior_batches = list(prior["batches"])
            provenance = await run_data_integrity(
                build_cache_source_provenance,
                merged,
                [*prior_batches, *fetched_evidence],
            )
            if not provenance["identity_consistent"]:
                raise SourceEvidenceError(
                    "cache update would mix providers or adjustment semantics; "
                    "run a controlled full refresh with one source"
                )
            await self.save_pivot(
                pool_id,
                merged,
                source_provenance=provenance,
            )
            logger.info(
                "Cached %d dates x %d columns for pool '%s'",
                len(merged),
                len(merged.columns),
                pool_id,
            )
        if merged.empty:
            logger.warning("Fetched empty data for pool '%s'", pool_id)
            return merged
        return merged[(merged.index >= start_ts) & (merged.index <= end_ts)]

    async def get_or_fetch_custom(
        self,
        pool_id: str,
        source: Any,
        codes: list[str],
        start: str,
        end: str,
        *,
        force: bool = False,
    ) -> pd.DataFrame:
        """Strict custom-universe path with the same provenance contract."""

        cached = None if force else await self.load_pivot(pool_id)
        if cached is not None:
            cached_codes = _daily_codes(cached)
            cached_fields = {
                str(value).lower()
                for value in cached.columns.get_level_values(-1)
            }
            required_fields = {"open", "high", "low", "close", "volume"}
            covers_request = (
                set(codes).issubset(cached_codes)
                and required_fields.issubset(cached_fields)
                and cached.index.min() <= pd.Timestamp(start)
                and cached.index.max() >= pd.Timestamp(end)
            )
            if covers_request:
                return cached[
                    (cached.index >= pd.Timestamp(start))
                    & (cached.index <= pd.Timestamp(end))
                ]
        result = await fetch_daily_with_evidence(source, codes, start, end)
        if result.frame.empty:
            raise SourceEvidenceError(
                "custom market-data refresh returned no validated "
                "observations; existing cache was left unchanged"
            )
        if not result.evidence["complete_code_coverage"]:
            raise SourceEvidenceError(
                "custom market-data fetch did not cover every requested code"
            )
        fetched_fields = {
            str(value).lower()
            for value in result.frame.columns.get_level_values(-1)
        }
        if not {"open", "high", "low", "close", "volume"}.issubset(
            fetched_fields
        ):
            raise SourceEvidenceError(
                "custom market-data fetch is missing required OHLCV fields"
            )
        await run_data_integrity(
            require_daily_market_data_quality,
            result.frame,
            expected_codes=set(codes),
        )
        provenance = await run_data_integrity(
            build_cache_source_provenance,
            result.frame,
            [result.evidence],
        )
        await self.save_pivot(
            pool_id,
            result.frame,
            source_provenance=provenance,
        )
        return result.frame

    async def get_or_fetch_point_in_time_universe(
        self,
        pool_id: str,
        source: Any,
        union_codes: list[str],
        start: str,
        end: str,
        *,
        force: bool = False,
    ) -> pd.DataFrame:
        """Build a preset cache from the requested PIT timeline union.

        The ordinary preset path resolves today's constituents and therefore
        cannot satisfy historical membership.  Keeping this explicit entry
        point prevents a caller from accidentally relabelling a current
        snapshot cache as point-in-time evidence.
        """

        if not union_codes:
            raise ValueError("point-in-time universe union cannot be empty")
        return await self.get_or_fetch_custom(
            pool_id,
            source,
            sorted(set(union_codes)),
            start,
            end,
            force=force,
        )

    async def get_or_fetch_research_pool(
        self,
        pool_id: str,
        source: Any,
        *,
        start: str,
        end: str,
        force: bool = False,
    ) -> pd.DataFrame:
        """Refresh a preset research pool under its real universe contract."""

        if pool_id not in {"csi300", "csi500", "csi800", "csi1000"}:
            return await self.get_or_fetch(
                pool_id,
                source,
                force=force,
                start=start,
                end=end,
            )
        from backend.data.point_in_time_master import PointInTimeMasterStore
        from backend.data.point_in_time_universe import (
            resolve_point_in_time_universe,
        )
        from backend.data.universe import PRESET_POOLS

        timeline = await asyncio.to_thread(
            resolve_point_in_time_universe,
            PointInTimeMasterStore(),
            pool_id=pool_id,
            trading_dates=pd.bdate_range(start, end),
            expected_count=PRESET_POOLS[pool_id]["expected_count"],
        )
        return await self.get_or_fetch_point_in_time_universe(
            pool_id,
            source,
            list(timeline.union_codes),
            start,
            end,
            force=force,
        )

    async def save_pivot(
        self,
        pool_id: str,
        df: pd.DataFrame,
        *,
        source_provenance: dict[str, Any] | None = None,
    ) -> None:
        """Strict runtime write requiring source provenance for OHLCV."""

        if not df.empty and has_price_field(df, "open") and source_provenance is None:
            raise SourceEvidenceError(
                "runtime OHLCV cache writes require source_provenance"
            )
        async with self._pool_lock(pool_id):
            await self._save_pivot_unlocked(
                pool_id,
                df,
                source_provenance=source_provenance,
            )

    async def save_legacy_pivot_for_audit(
        self,
        pool_id: str,
        df: pd.DataFrame,
    ) -> None:
        """Explicitly write untrusted fixtures; runtime readers will reject it."""

        async with self._pool_lock(pool_id):
            await self._save_pivot_unlocked(pool_id, df)

    async def _save_pivot_unlocked(
        self,
        pool_id: str,
        df: pd.DataFrame,
        *,
        source_provenance: dict[str, Any] | None = None,
    ) -> None:
        """保存 pivot DataFrame 为 Parquet 格式。

        使用原子写入（先写临时文件再替换），防止并发读取时读到半写文件。
        """
        await run_data_integrity(
            self._save_pivot_sync,
            pool_id,
            df,
            source_provenance=source_provenance,
        )

    def _save_pivot_sync(
        self,
        pool_id: str,
        df: pd.DataFrame,
        *,
        source_provenance: dict[str, Any] | None = None,
    ) -> None:
        """Execute validation, digesting and atomic Parquet I/O off-loop."""

        pool_id = _validate_pool_id(pool_id)
        if df.empty:
            return

        cache_path = self._daily_dir / f"{pool_id}.parquet"
        token = uuid.uuid4().hex
        tmp_path = cache_path.with_name(f"{cache_path.name}.{token}.tmp")
        meta_path = self._daily_dir / f"{pool_id}.meta.json"
        meta_tmp_path = meta_path.with_name(f"{meta_path.name}.{token}.tmp")

        try:
            # 确保 index 为 datetime 类型
            if not isinstance(df.index, pd.DatetimeIndex):
                df = df.copy()
                if "date" in df.columns:
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.set_index("date")
                elif df.index.name != "date":
                    df.index = pd.to_datetime(df.index)

            if source_provenance is not None:
                source_provenance = validate_cache_source_provenance(
                    source_provenance,
                    frame=df,
                )
            quality = (
                require_daily_market_data_quality(
                    df,
                    expected_codes=set(source_provenance["frame_codes"]),
                )
                if source_provenance is not None
                and has_price_field(df, "open")
                else None
            )
            df.to_parquet(tmp_path, compression="snappy", index=True)

            self._write_meta(
                pool_id,
                df,
                source_provenance=source_provenance,
                data_quality=quality,
                target_path=meta_tmp_path,
            )
            # Both artifacts become visible only through one atomic manifest
            # replacement.  Do not replace the legacy flat paths here: doing
            # so would reintroduce a cross-process Parquet/metadata split.
            self._daily_generations.publish_staged(
                pool_id,
                {"pivot": tmp_path, "metadata": meta_tmp_path},
            )
        except Exception:
            logger.exception("Failed to save pivot for pool '%s'", pool_id)
            # 清理临时文件
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            if meta_tmp_path.exists():
                meta_tmp_path.unlink(missing_ok=True)
            raise

    async def load_pivot(self, pool_id: str) -> pd.DataFrame | None:
        """Strict runtime load bound to schema-v4 source provenance."""

        async with self._pool_lock(pool_id):
            frame, _ = await self._load_verified_pivot_unlocked(pool_id)
            return frame

    async def load_pivot_with_provenance(
        self,
        pool_id: str,
    ) -> tuple[pd.DataFrame | None, dict[str, Any] | None]:
        """Atomically load a frame and its provenance under one pool lock.

        Cache writers replace the Parquet and metadata files while holding the
        same lock.  Returning both values from this critical section prevents
        a runtime from binding an old provenance digest to a newly replaced
        frame (or the inverse).
        """

        async with self._pool_lock(pool_id):
            return await self._load_verified_pivot_unlocked(pool_id)

    async def _load_verified_pivot_unlocked(
        self,
        pool_id: str,
    ) -> tuple[pd.DataFrame | None, dict[str, Any] | None]:
        try:
            view = await run_data_integrity(
                self._daily_generations.load,
                pool_id,
            )
        except GenerationManifestError as exc:
            raise LegacyAdjustedCacheError(
                f"Pool '{pool_id}' generation manifest is invalid; "
                "run a controlled force refresh"
            ) from exc
        if view is None:
            # Flat files predate generation publication.  They remain readable
            # through the explicit audit-only path but never become a runtime
            # input because their two-file update cannot be proven atomic.
            if (self._daily_dir / f"{pool_id}.parquet").exists():
                raise LegacyAdjustedCacheError(
                    f"Pool '{pool_id}' is not generation-bound; "
                    "run a controlled force refresh"
                )
            return None, None
        frame = await run_data_integrity(self._load_generation_pivot_sync, view)
        has_verified_metadata = await run_data_integrity(
            self._has_verified_adjustment_metadata,
            pool_id,
            metadata_path=view.artifacts["metadata"],
        )
        if not has_verified_metadata:
            raise LegacyAdjustedCacheError(
                f"Pool '{pool_id}' is not a schema-v4 provenance cache; "
                "run a controlled force refresh"
            )
        provenance = await run_data_integrity(
            self._read_source_provenance,
            pool_id,
            frame=frame,
            metadata_path=view.artifacts["metadata"],
        )
        await run_data_integrity(
            require_daily_market_data_quality,
            frame,
            expected_codes=set(provenance["frame_codes"]),
        )
        return frame, provenance

    async def load_legacy_pivot_for_audit(
        self,
        pool_id: str,
    ) -> pd.DataFrame | None:
        """Read untrusted historical bytes without granting runtime trust."""

        async with self._pool_lock(pool_id):
            return await self._load_pivot_unchecked(pool_id)

    async def _load_pivot_unchecked(self, pool_id: str) -> pd.DataFrame | None:
        """从 Parquet 文件加载 pivot DataFrame。

        Returns:
            DataFrame 或 None（文件不存在/损坏）。
        """
        return await run_data_integrity(self._load_pivot_sync, pool_id)

    def _load_pivot_sync(self, pool_id: str) -> pd.DataFrame | None:
        """Read and normalize one Parquet file on the bounded worker."""

        pool_id = _validate_pool_id(pool_id)
        try:
            view = self._daily_generations.load(pool_id)
        except GenerationManifestError:
            logger.exception("Invalid cache generation for pool '%s'", pool_id)
            return None
        if view is not None:
            return self._load_generation_pivot_sync(view)
        # Audit-only compatibility for pre-generation data. Strict runtime
        # readers are blocked earlier in _load_verified_pivot_unlocked.
        cache_path = self._daily_dir / f"{pool_id}.parquet"
        if not cache_path.exists():
            return None
        try:
            df = pd.read_parquet(cache_path)
            if df.empty:
                return None
            # 确保 index 为 datetime
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            return df
        except Exception:
            logger.exception("Failed to load cached pivot for pool '%s'", pool_id)
            return None

    @staticmethod
    def _load_generation_pivot_sync(view: GenerationView) -> pd.DataFrame:
        """Load the Parquet selected by an already verified manifest view."""

        try:
            df = pd.read_parquet(view.artifacts["pivot"])
            if df.empty:
                raise LegacyAdjustedCacheError("active cache generation is empty")
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            return df
        except LegacyAdjustedCacheError:
            raise
        except Exception as exc:
            raise LegacyAdjustedCacheError(
                "active cache generation parquet is unreadable"
            ) from exc

    async def get_or_fetch_index(
        self,
        index_code: str,
        source,
        force: bool = False,
        start: str = "2015-01-01",
        end: str | None = None,
    ) -> pd.Series:
        """Load an index close series, extending its isolated cache as needed."""
        code = _validate_index_code(index_code)
        start_ts = pd.Timestamp(start).normalize()
        end_ts = pd.Timestamp(
            end or pd.Timestamp.now().strftime("%Y-%m-%d")
        ).normalize()
        if start_ts > end_ts:
            raise ValueError("start must be on or before end")

        cached = None if force else await self.load_index(code)
        fetch_ranges: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        if cached is None or cached.empty:
            fetch_ranges.append((start_ts, end_ts))
        else:
            cache_start = cached.index.min().normalize()
            cache_end = cached.index.max().normalize()
            if start_ts < cache_start:
                fetch_ranges.append(
                    (start_ts, cache_start - pd.Timedelta(days=1))
                )
            if end_ts > cache_end:
                fetch_ranges.append(
                    (cache_end + pd.Timedelta(days=1), end_ts)
                )

        chunks: list[pd.Series] = []
        if cached is not None and not cached.empty:
            chunks.append(cached)
        for range_start, range_end in fetch_ranges:
            if range_start > range_end:
                continue
            logger.info(
                "Fetching index '%s': %s → %s",
                code,
                range_start.strftime("%Y-%m-%d"),
                range_end.strftime("%Y-%m-%d"),
            )
            fetched = await source.fetch_index_daily(
                code,
                range_start.strftime("%Y-%m-%d"),
                range_end.strftime("%Y-%m-%d"),
            )
            normalized = _normalize_index_series(fetched)
            if not normalized.empty:
                chunks.append(normalized)

        if not chunks:
            return _normalize_index_series(pd.Series(dtype="float64", name="close"))

        merged = _normalize_index_series(pd.concat(chunks))
        if fetch_ranges and not merged.empty:
            await self.save_index(code, merged)
        return merged[(merged.index >= start_ts) & (merged.index <= end_ts)]

    async def save_index(self, index_code: str, series: pd.Series) -> None:
        """Atomically persist an index close series in the index cache domain."""
        await run_data_integrity(self._save_index_sync, index_code, series)

    def _save_index_sync(self, index_code: str, series: pd.Series) -> None:
        """Write one normalized index Parquet file off the event loop."""

        code = _validate_index_code(index_code)
        normalized = _normalize_index_series(series)
        if normalized.empty:
            return

        cache_path = self._index_dir / f"{code}.parquet"
        tmp_path = cache_path.with_suffix(".parquet.tmp")
        try:
            normalized.to_frame().to_parquet(
                tmp_path,
                compression="snappy",
                index=True,
            )
            tmp_path.replace(cache_path)
        except Exception:
            logger.exception("Failed to save index cache for '%s'", code)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    async def load_index(self, index_code: str) -> pd.Series | None:
        """Load a normalized index close series from its dedicated cache."""
        return await run_data_integrity(self._load_index_sync, index_code)

    def _load_index_sync(self, index_code: str) -> pd.Series | None:
        """Read and normalize one index Parquet file off the event loop."""

        code = _validate_index_code(index_code)
        cache_path = self._index_dir / f"{code}.parquet"
        if not cache_path.exists():
            return None
        try:
            frame = pd.read_parquet(cache_path)
            if frame.empty or "close" not in frame.columns:
                return None
            return _normalize_index_series(frame["close"])
        except Exception:
            logger.exception("Failed to load cached index '%s'", code)
            return None

    async def get_cache_info(self, pool_id: str) -> dict:
        """返回缓存的元信息：日期范围、股票数、文件大小。

        Returns:
            {"pool_id", "date_start", "date_end", "n_dates", "n_stocks",
             "file_size_mb", "last_updated", "exists"}
        """
        return await run_data_integrity(self._get_cache_info_sync, pool_id)

    def _get_cache_info_sync(self, pool_id: str) -> dict:
        """Read Parquet and JSON metadata on the bounded integrity worker."""

        pool_id = _validate_pool_id(pool_id)
        try:
            view = self._daily_generations.load(pool_id)
        except GenerationManifestError:
            logger.exception("Invalid cache generation for pool '%s'", pool_id)
            view = None
        cache_path = (
            view.artifacts["pivot"]
            if view is not None
            else self._daily_dir / f"{pool_id}.parquet"
        )
        meta_path = (
            view.artifacts["metadata"]
            if view is not None
            else self._daily_dir / f"{pool_id}.meta.json"
        )

        info: dict = {
            "pool_id": pool_id,
            "exists": view is not None,
            "date_start": None,
            "date_end": None,
            "n_dates": 0,
            "n_stocks": 0,
            "file_size_mb": 0.0,
            "last_updated": None,
            "schema_version": 1,
            "fields": [],
            "price_adjustment": None,
            "adjustment_consistency": None,
            "source_provenance": None,
            "source_trust": "unverified",
            "data_quality": None,
            "price_ledger": None,
            "ready_for_return_research": False,
            "ready_for_static_adjusted_return_research": False,
            "ready_for_unbiased_return_research": False,
            "return_research_semantics": (
                "legacy_static_price_research_only_not_promotion_eligible"
            ),
            "ready_for_execution_simulation": False,
        }

        if view is None:
            return info

        try:
            file_size = cache_path.stat().st_size
            info["file_size_mb"] = round(file_size / (1024 * 1024), 2)

            # 从 parquet 元数据快速读取
            pf = pd.read_parquet(cache_path, columns=[])
            info["n_dates"] = len(pf)
            info["n_stocks"] = len(pf.columns)
            if len(pf) > 0:
                info["date_start"] = pf.index[0].strftime("%Y-%m-%d")
                info["date_end"] = pf.index[-1].strftime("%Y-%m-%d")
        except Exception:
            logger.exception("Failed to read metadata for pool '%s'", pool_id)

        # 补充 meta.json 信息
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                for key in (
                    "date_start",
                    "date_end",
                    "n_dates",
                    "n_stocks",
                    "last_updated",
                    "schema_version",
                    "fields",
                    "price_adjustment",
                    "adjustment_consistency",
                    "source_provenance",
                    "source_trust",
                    "data_quality",
                    "price_ledger",
                    "ready_for_return_research",
                    "ready_for_static_adjusted_return_research",
                    "ready_for_unbiased_return_research",
                    "return_research_semantics",
                    "ready_for_execution_simulation",
                ):
                    if meta.get(key) is not None:
                        info[key] = meta[key]
                try:
                    validated = validate_cache_source_provenance(
                        meta.get("source_provenance")
                    )
                    info["source_trust"] = self._source_trust(validated)
                except (SourceEvidenceError, TypeError):
                    info["source_trust"] = "unverified"
            except Exception:
                pass

        return info

    # ── 增量更新 ─────────────────────────────────────────────────────────────

    async def auto_update(
        self,
        source,
        pool_id: str | None = None,
        *,
        now: datetime | pd.Timestamp | None = None,
    ) -> dict[str, Any]:
        """Update market caches and their configured benchmark dependencies.

        A market cache is not sufficient for a cache-only experiment by
        itself.  Every configured benchmark is therefore checked even when
        the market cache is already current.  Its persisted series must cover
        ten calendar days before the market cache starts through the final
        persisted market date.  A seven-day fetch safety margin makes the
        first observation robust to weekends and exchange holidays.

        Session completion is evaluated in ``Asia/Shanghai`` regardless of
        the host timezone.  Before the conservative 18:00 provider-availability
        cutoff, today's calendar entry is excluded; at or after the cutoff it
        may be used.  ``now`` is injectable for deterministic verification.

        Args:
            source:  DataSource 实例。
            pool_id: 要更新的池 ID。None 则更新所有已知池。
            now: Optional clock value; naive values mean Shanghai local time.

        Returns:
            ``updated_pools`` plus auditable ``benchmark_updates`` and
            fail-closed ``errors``.
        """
        raise LegacyRuntimeDataDisabledError(
            "legacy Parquet auto_update is disabled; use the PIT governance "
            "collector, independent review and explicit activation workflow"
        )
        if pool_id is not None:
            pool_id = _validate_pool_id(pool_id)

        from .calendar import TradingCalendar
        calendar = TradingCalendar(str(self._root))
        local_now = _market_now(now)
        today = local_now.date().isoformat()

        # 获取最近已经完成、且日线通常已由公共供应商发布的交易日。
        try:
            trading_days = await calendar.load(source, "2020-01-01", today)
            if not trading_days:
                logger.warning("No trading calendar available, skipping auto_update")
                return {
                    "updated_pools": [],
                    "benchmark_updates": [],
                    "errors": ["No trading calendar"],
                }
            last_trading_day = _latest_completed_trading_day(
                trading_days,
                now=local_now,
            )
            if last_trading_day is None:
                logger.warning(
                    "Trading calendar has no completed session before the "
                    "market-data availability cutoff"
                )
                return {
                    "updated_pools": [],
                    "benchmark_updates": [],
                    "errors": ["No completed trading day"],
                }
        except Exception as exc:
            logger.exception("Failed to load trading calendar")
            return {
                "updated_pools": [],
                "benchmark_updates": [],
                "errors": [str(exc)],
            }

        # 确定要更新的池列表
        if pool_id:
            pool_ids = [pool_id]
        else:
            pool_ids = self._discover_pools()

        # 如果没有任何已知池，默认更新预设池
        if not pool_ids:
            from .universe import PRESET_POOLS
            pool_ids = list(PRESET_POOLS.keys())

        updated: list[str] = []
        benchmark_updates: list[dict[str, Any]] = []
        errors: list[dict] = []

        for pid in pool_ids:
            try:
                info = await self.get_cache_info(pid)
                if info["exists"] and (
                    info.get("schema_version", 1) < DAILY_CACHE_SCHEMA_VERSION
                    or "open" not in info.get("fields", [])
                    or not bool(
                        (info.get("data_quality") or {}).get("ready")
                    )
                ):
                    await self.get_or_fetch_research_pool(
                        pid,
                        source,
                        force=True,
                        start=str(info.get("date_start") or "2015-01-01"),
                        end=last_trading_day,
                    )
                    updated.append(pid)
                elif not info["exists"] or info["date_end"] is None:
                    # 全新拉取（或信息丢失）
                    await self.get_or_fetch_research_pool(
                        pid,
                        source,
                        force=True,
                        start="2015-01-01",
                        end=last_trading_day,
                    )
                    updated.append(pid)
                elif pid in {"csi300", "csi500", "csi800", "csi1000"}:
                    # A date-current cache may still contain only today's
                    # constituents. Re-resolve the PIT union on every
                    # controlled update; the strict cache path is a no-op when
                    # both dates and historical columns already cover it.
                    await self.get_or_fetch_research_pool(
                        pid,
                        source,
                        start=info["date_start"],
                        end=max(str(info["date_end"]), last_trading_day),
                    )
                    updated.append(pid)
                elif info["date_end"] >= last_trading_day:
                    logger.debug(
                        "Pool '%s' is up-to-date (cache_end=%s)",
                        pid,
                        info["date_end"],
                    )
                else:
                    # 通过统一读取路径更新。该路径会用重叠窗口检查前复权
                    # 历史是否发生修订，并在公司行动或成分变化时完整重建，
                    # 禁止把不同复权基准的两段行情直接拼接。
                    logger.info(
                        "Consistency-checked update for pool '%s': %s → %s",
                        pid,
                        info["date_start"],
                        last_trading_day,
                    )
                    updated_frame = await self.get_or_fetch_research_pool(
                        pid,
                        source,
                        start=info["date_start"],
                        end=last_trading_day,
                    )
                    if updated_frame.empty:
                        logger.info("No new data for pool '%s'", pid)
                    else:
                        logger.info(
                            "Updated pool '%s': %d dates x %d columns",
                            pid,
                            len(updated_frame),
                            len(updated_frame.columns),
                        )
                        updated.append(pid)

                # Re-read metadata after a force rebuild or incremental merge.
                # Benchmark readiness is a dependency of the final persisted
                # market cache, not of the stale metadata inspected above.
                final_info = await self.get_cache_info(pid)
                benchmark_updates.append(
                    await self._ensure_pool_benchmark(
                        source,
                        pool_id=pid,
                        market_info=final_info,
                        completed_through=last_trading_day,
                    )
                )

            except Exception as exc:
                logger.exception("Auto-update failed for pool '%s'", pid)
                benchmark_code = self._refresh_benchmark_code(pid)
                if not any(
                    item.get("pool_id") == pid for item in benchmark_updates
                ):
                    if isinstance(exc, BenchmarkRefreshError):
                        benchmark_updates.append(
                            {
                                "pool_id": pid,
                                "index_code": benchmark_code,
                                **exc.audit_context,
                                "action": "failed",
                                "error": str(exc)[:2048],
                            }
                        )
                    else:
                        benchmark_updates.append(
                            {
                                "pool_id": pid,
                                "index_code": benchmark_code,
                                "action": "skipped",
                                "reason": "market_update_failed",
                            }
                        )
                error_item: dict[str, Any] = {
                    "pool_id": pid,
                    "stage": (
                        "benchmark"
                        if benchmark_code is not None
                        and isinstance(exc, BenchmarkRefreshError)
                        else "market"
                    ),
                    "error": str(exc)[:2048],
                }
                if isinstance(exc, BenchmarkRefreshError):
                    error_item.update(exc.audit_context)
                if (
                    isinstance(exc, CrossSourceConflictError)
                    and exc.evidence_summary is not None
                ):
                    error_item["failure_evidence"] = exc.evidence_summary
                errors.append(error_item)
                from .sources.akshare_source import ProviderOutageError

                if isinstance(exc, ProviderOutageError) or isinstance(
                    exc.__cause__,
                    ProviderOutageError,
                ):
                    logger.error(
                        "Stopping automatic cache refresh because the market "
                        "data provider circuit is open"
                    )
                    break

        return {
            "updated_pools": updated,
            "benchmark_updates": benchmark_updates,
            "errors": errors,
        }

    @staticmethod
    def _refresh_benchmark_code(pool_id: str) -> str | None:
        """Resolve only explicit refresh dependencies.

        ``all_a`` has no accepted broad-market benchmark contract.  Unknown
        discovered cache names are also skipped instead of silently inheriting
        the legacy CSI 300 fallback used by interactive experiments.
        """

        normalized = pool_id.strip().lower()
        if normalized == "all_a" or normalized not in POOL_BENCHMARK_MAP:
            return None
        return resolve_pool_benchmark(normalized)

    async def _ensure_pool_benchmark(
        self,
        source,
        *,
        pool_id: str,
        market_info: dict[str, Any],
        completed_through: str,
    ) -> dict[str, Any]:
        """Ensure benchmark coverage matches the final persisted market range.

        ``completed_through`` is an upper safety bound, not the benchmark
        target.  The target is always ``market_info.date_end`` so a public
        market-data source that is one session late does not force a benchmark
        beyond the data actually persisted.  Conversely, a benchmark that
        ends before that persisted market date still fails closed.
        """

        index_code = self._refresh_benchmark_code(pool_id)
        if index_code is None:
            return {
                "pool_id": pool_id,
                "index_code": None,
                "action": "skipped",
                "reason": "no_benchmark_configured",
            }

        market_start = market_info.get("date_start")
        market_end = market_info.get("date_end")
        if not market_info.get("exists") or not market_start or not market_end:
            raise BenchmarkRefreshError(
                "market cache has no auditable date range",
                {
                    "index_code": index_code,
                    "market_date_start": market_start,
                    "market_date_end": market_end,
                    "completed_through": completed_through,
                },
            )

        try:
            market_start_ts = pd.Timestamp(market_start).normalize()
            market_end_ts = pd.Timestamp(market_end).normalize()
            completed_through_ts = pd.Timestamp(completed_through).normalize()
            if market_start_ts > market_end_ts:
                raise ValueError("market start exceeds market end")
            if market_end_ts > completed_through_ts:
                raise ValueError(
                    "market cache extends beyond the latest completed session"
                )
            required_start_ts = (
                market_start_ts
                - pd.Timedelta(days=_BENCHMARK_REQUIRED_BUFFER_DAYS)
            )
            fetch_start_ts = required_start_ts - pd.Timedelta(
                days=_BENCHMARK_FETCH_SAFETY_DAYS
            )
            required_end_ts = market_end_ts
        except (TypeError, ValueError) as exc:
            raise BenchmarkRefreshError(
                "benchmark refresh window is invalid",
                {
                    "index_code": index_code,
                    "market_start": str(market_start),
                    "market_end": str(market_end),
                    "completed_through": str(completed_through),
                },
            ) from exc
        required_start = required_start_ts.strftime("%Y-%m-%d")
        fetch_start = fetch_start_ts.strftime("%Y-%m-%d")
        required_end_text = required_end_ts.strftime("%Y-%m-%d")

        context: dict[str, Any] = {
            "index_code": index_code,
            "market_date_start": pd.Timestamp(market_start).strftime(
                "%Y-%m-%d"
            ),
            "market_date_end": market_end_ts.strftime("%Y-%m-%d"),
            "completed_through": completed_through_ts.strftime("%Y-%m-%d"),
            "required_start": required_start,
            "fetch_start": fetch_start,
            "required_end": required_end_text,
        }
        try:
            before = await self.load_index(index_code)
            before_range = self._index_range_report(before)
            context["before"] = before_range
            await self.get_or_fetch_index(
                index_code,
                source,
                start=fetch_start,
                end=required_end_text,
            )
            after = await self.load_index(index_code)
            after_range = self._index_range_report(after)
            context["after"] = after_range
            if not self._index_covers(
                after,
                start=required_start_ts,
                end=required_end_ts,
            ):
                raise BenchmarkRefreshError(
                    "benchmark cache does not cover the required market window",
                    context,
                )
        except BenchmarkRefreshError:
            raise
        except Exception as exc:
            raise BenchmarkRefreshError(str(exc), context) from exc

        return {
            "pool_id": pool_id,
            **context,
            "action": (
                "already_current"
                if before_range == after_range
                else "updated"
            ),
        }

    @staticmethod
    def _index_covers(
        series: pd.Series | None,
        *,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> bool:
        return bool(
            series is not None
            and not series.empty
            and series.index.min().normalize() <= start
            and series.index.max().normalize() >= end
        )

    @staticmethod
    def _index_range_report(series: pd.Series | None) -> dict[str, Any]:
        if series is None or series.empty:
            return {
                "date_start": None,
                "date_end": None,
                "observations": 0,
            }
        return {
            "date_start": series.index.min().strftime("%Y-%m-%d"),
            "date_end": series.index.max().strftime("%Y-%m-%d"),
            "observations": len(series),
        }

    # ── 内部工具 ─────────────────────────────────────────────────────────────

    def _write_meta(
        self,
        pool_id: str,
        df: pd.DataFrame,
        *,
        source_provenance: dict[str, Any] | None = None,
        data_quality: dict[str, Any] | None = None,
        target_path: Path | None = None,
    ) -> None:
        """写入缓存元信息 JSON。"""
        pool_id = _validate_pool_id(pool_id)
        meta_path = target_path or self._daily_dir / f"{pool_id}.meta.json"
        if isinstance(df.columns, pd.MultiIndex):
            codes = {str(value) for value in df.columns.get_level_values(0)}
            fields = sorted({str(value) for value in df.columns.get_level_values(-1)})
        else:
            codes = {str(value) for value in df.columns}
            fields = ["close"]
        adjustments = (
            list(source_provenance.get("adjustments", []))
            if isinstance(source_provenance, dict)
            else []
        )
        price_adjustment = (
            str(adjustments[0])
            if len(adjustments) == 1
            and str(adjustments[0]) in SUPPORTED_PRICE_ADJUSTMENTS
            else None
        )
        adjusted_research = price_adjustment in {"qfq", "hfq"}
        raw_execution = price_adjustment == "raw"
        source_trust = self._source_trust(source_provenance)
        source_raw_cross_validated = bool(
            source_provenance
            and source_provenance.get("all_batches_raw_cross_validated")
        )
        source_adjusted_factor_validated = bool(
            source_provenance
            and source_provenance.get(
                "all_batches_adjusted_factor_validated"
            )
        )
        source_research_eligible = source_trust in {
            "public_cross_validated_research_only",
            "licensed",
            "exchange_authoritative",
        } and (
            source_raw_cross_validated
            and source_adjusted_factor_validated
        )
        price_ledger = (
            {
                "schema_version": "legacy-price-cache-role/v1",
                "ledger_available": False,
                "reason": "ledger_unavailable",
                "adjustment": price_adjustment,
                "role": (
                    "adjusted_return_research"
                    if adjusted_research
                    else "raw_execution_candidate"
                    if raw_execution
                    else "unverified"
                ),
                "adjusted_return_price_available": adjusted_research,
                "raw_execution_price_available": raw_execution,
                "dual_ledger_complete": False,
                "restriction": "legacy_cache_is_not_a_dual_price_ledger",
            }
            if "open" in fields
            else None
        )
        meta = {
            "pool_id": pool_id,
            "schema_version": (
                DAILY_CACHE_SCHEMA_VERSION
                if "open" in fields and source_provenance is not None
                else (
                    LEGACY_ADJUSTED_CACHE_SCHEMA_VERSION
                    if "open" in fields
                    else 1
                )
            ),
            "fields": fields,
            "price_adjustment": price_adjustment,
            "adjustment_consistency": (
                ADJUSTMENT_CONSISTENCY_VERSION
                if "open" in fields
                else None
            ),
            "date_start": df.index[0].strftime("%Y-%m-%d") if len(df) > 0 else None,
            "date_end": df.index[-1].strftime("%Y-%m-%d") if len(df) > 0 else None,
            "n_dates": len(df),
            "n_stocks": len(codes),
            "last_updated": pd.Timestamp.now().isoformat(),
            "source_provenance": source_provenance,
            "source_trust": source_trust,
            "source_raw_cross_validated": source_raw_cross_validated,
            "source_adjusted_factor_validated": (
                source_adjusted_factor_validated
            ),
            "data_quality": data_quality,
            "price_ledger": price_ledger,
            "ready_for_return_research": bool(
                data_quality
                and data_quality.get("ready")
                and source_research_eligible
                and adjusted_research
            ),
            "ready_for_static_adjusted_return_research": bool(
                data_quality
                and data_quality.get("ready")
                and source_research_eligible
                and adjusted_research
            ),
            "ready_for_unbiased_return_research": False,
            "return_research_semantics": (
                "legacy_static_price_research_only_not_promotion_eligible"
            ),
            "ready_for_execution_simulation": bool(
                data_quality
                and data_quality.get("ready")
                and source_research_eligible
                and raw_execution
            ),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _has_verified_adjustment_metadata(
        self,
        pool_id: str,
        *,
        metadata_path: Path | None = None,
    ) -> bool:
        if metadata_path is None:
            try:
                view = self._daily_generations.load(pool_id)
            except GenerationManifestError:
                return False
            if view is None:
                return False
            metadata_path = view.artifacts["metadata"]
        meta_path = metadata_path
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return False
        try:
            provenance = validate_cache_source_provenance(
                metadata.get("source_provenance")
            )
        except (SourceEvidenceError, TypeError):
            return False
        return (
            metadata.get("schema_version") == DAILY_CACHE_SCHEMA_VERSION
            and metadata.get("price_adjustment")
            in SUPPORTED_PRICE_ADJUSTMENTS
            and (
                metadata.get("adjustment_consistency")
                == ADJUSTMENT_CONSISTENCY_VERSION
                or (
                    metadata.get("price_adjustment") == "qfq"
                    and metadata.get("adjustment_consistency")
                    == LEGACY_QFQ_CONSISTENCY_VERSION
                )
            )
            and provenance["identity_consistent"]
            and provenance["adjustments"]
            == [metadata.get("price_adjustment")]
        )

    def _read_source_provenance(
        self,
        pool_id: str,
        *,
        frame: pd.DataFrame | None = None,
        metadata_path: Path | None = None,
    ) -> dict[str, Any]:
        if metadata_path is None:
            try:
                view = self._daily_generations.load(pool_id)
            except GenerationManifestError as exc:
                raise LegacyAdjustedCacheError(
                    f"Pool '{pool_id}' generation manifest is invalid; "
                    "run a controlled force refresh"
                ) from exc
            if view is None:
                metadata_path = self._daily_dir / f"{_validate_pool_id(pool_id)}.meta.json"
            else:
                metadata_path = view.artifacts["metadata"]
        meta_path = metadata_path
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            return validate_cache_source_provenance(
                metadata.get("source_provenance"),
                frame=frame,
            )
        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
            SourceEvidenceError,
        ) as exc:
            raise LegacyAdjustedCacheError(
                f"Pool '{pool_id}' has missing or invalid source provenance; "
                "run a controlled force refresh"
            ) from exc

    def get_source_provenance(
        self,
        pool_id: str,
        *,
        frame: pd.DataFrame | None = None,
    ) -> dict[str, Any]:
        """Return verified cache provenance for runtime manifest binding."""

        try:
            view = self._daily_generations.load(pool_id)
        except GenerationManifestError as exc:
            raise LegacyAdjustedCacheError(
                f"Pool '{pool_id}' generation manifest is invalid; "
                "run a controlled force refresh"
            ) from exc
        if view is None:
            raise LegacyAdjustedCacheError(
                f"Pool '{pool_id}' is not generation-bound; "
                "run a controlled force refresh"
            )
        return self._read_source_provenance(
            pool_id,
            frame=frame,
            metadata_path=view.artifacts["metadata"],
        )

    @staticmethod
    def _source_trust(
        provenance: dict[str, Any] | None,
    ) -> str:
        if provenance is None:
            return "unverified"
        levels = set(provenance.get("evidence_levels", []))
        if not levels:
            return "unverified"
        if "declared" in levels:
            return "declared"
        if levels == {"exchange_authoritative"}:
            return "exchange_authoritative"
        if levels <= {"licensed_vendor", "exchange_authoritative"}:
            return "licensed"
        if levels <= {
            "public_aggregator",
            "licensed_vendor",
            "exchange_authoritative",
        }:
            if (
                provenance.get("all_batches_raw_cross_validated") is True
                and provenance.get("all_batches_adjusted_factor_validated")
                is True
            ):
                return "public_cross_validated_research_only"
            return "public_single_source_research_only"
        return "unverified"

    def _discover_pools(self) -> list[str]:
        """Discover refreshable named pools from daily cache files.

        ``custom_<hash>`` caches bind an explicit code list supplied by an
        experiment.  Their hash is not a universe definition, so an automatic
        refresh cannot safely reconstruct them through ``UniverseManager``.
        """
        pools: list[str] = []
        if not self._daily_dir.exists():
            return pools
        manifests = self._daily_dir / "generation-manifests"
        for f in (manifests.glob("*.json") if manifests.exists() else []):
            pool_id = f.stem
            if (
                re.fullmatch(r"[0-9A-Za-z_-]{1,64}", pool_id)
                and not pool_id.lower().startswith("custom_")
            ):
                pools.append(pool_id)
        return pools

    # ── 便捷清理 ────────────────────────────────────────────────────────────

    async def invalidate(self, pool_id: str) -> None:
        """删除指定池的缓存文件。"""
        pool_id = _validate_pool_id(pool_id)
        cache_path = self._daily_dir / f"{pool_id}.parquet"
        meta_path = self._daily_dir / f"{pool_id}.meta.json"
        manifest_path = self._daily_dir / "generation-manifests" / f"{pool_id}.json"
        async with self._pool_lock(pool_id):
            # Removing the sole active manifest is atomic from readers'
            # perspective. Generations are retained for explicit forensic
            # cleanup and are never blindly deleted during invalidation.
            for p in (manifest_path, cache_path, meta_path):
                if p.exists():
                    p.unlink()
        logger.info("Invalidated cache for pool '%s'", pool_id)

    async def clear_all(self) -> None:
        """清除所有缓存文件。"""
        for directory in (self._daily_dir, self._index_dir):
            for f in directory.glob("*"):
                if f.is_file():
                    f.unlink()
        logger.info("Cleared all data cache files")
