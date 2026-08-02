from __future__ import annotations

import asyncio

import pandas as pd

from backend.data.cache_readiness import (
    custom_cache_key,
    inspect_cached_benchmark,
    inspect_cached_market_data,
)


def _frame(
    *,
    codes: tuple[str, ...] = ("000001", "000002"),
    fields: tuple[str, ...] = ("open", "high", "low", "close", "volume"),
) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", "2025-01-31", name="date")
    columns = pd.MultiIndex.from_product(
        [codes, fields],
        names=["code", "field"],
    )
    return pd.DataFrame(1.0, index=dates, columns=columns)


class _LocalCache:
    def __init__(
        self,
        frame: pd.DataFrame | None,
        benchmark: pd.Series | None = None,
    ) -> None:
        self.frame = frame
        self.benchmark = benchmark
        self.loads: list[tuple[str, str]] = []

    async def load_pivot_with_provenance(
        self,
        cache_key: str,
    ) -> tuple[pd.DataFrame | None, dict | None]:
        self.loads.append(("daily", cache_key))
        return (
            None if self.frame is None else self.frame.copy(),
            None
            if self.frame is None
            else {
                "providers": ["local-synthetic-acceptance"],
                "evidence_levels": ["declared"],
                "adjustments": ["qfq"],
                "frame_digest": "dv2|test|sha256:" + "a" * 64,
                "identity_consistent": True,
                "complete_code_coverage": True,
            },
        )

    async def load_index(self, index_code: str) -> pd.Series | None:
        self.loads.append(("benchmark", index_code))
        return None if self.benchmark is None else self.benchmark.copy()


def test_custom_cache_key_is_order_independent_and_deduplicated() -> None:
    assert custom_cache_key(["000002", "000001", "000001"]) == (
        custom_cache_key(["000001", "000002"])
    )


def test_daily_readiness_reports_code_field_and_date_gaps() -> None:
    cache = _LocalCache(
        _frame(
            codes=("000001",),
            fields=("open", "high", "low", "close"),
        )
    )

    result = asyncio.run(
        inspect_cached_market_data(
            cache,  # type: ignore[arg-type]
            cache_key="custom_key",
            pool_id="custom",
            requested_codes=["000001", "000002"],
            required_start="2024-12-31",
            required_end="2025-02-01",
        )
    )

    assert result.report["ready"] is False
    assert result.report["missing_codes"] == ["000002"]
    assert result.report["missing_fields"] == {
        "000001": ["volume"],
        "000002": ["close", "high", "low", "open", "volume"],
    }
    assert set(result.report["issues"]) == {
        "daily_cache_codes_missing",
        "daily_cache_ohlcv_fields_missing",
        "daily_cache_start_not_covered",
        "daily_cache_end_not_covered",
    }
    assert cache.loads == [("daily", "custom_key")]


def test_daily_readiness_binds_atomic_frame_and_provenance_identity() -> None:
    cache = _LocalCache(_frame())

    result = asyncio.run(
        inspect_cached_market_data(
            cache,  # type: ignore[arg-type]
            cache_key="custom_key",
            pool_id="custom",
            requested_codes=["000002", "000001"],
            required_start="2025-01-01",
            required_end="2025-01-31",
        )
    )

    assert result.report["ready"] is True
    assert result.report["schema_version"] == 4
    assert result.report["n_dates"] == 23
    assert result.report["available_code_count"] == 2
    assert result.report["fields"] == [
        "close",
        "high",
        "low",
        "open",
        "volume",
    ]
    assert result.report["codes_sha256"] == (
        "cc5271aea76916c8dc5120c74cc510c8d9f03b2052b49f45"
        "9bba449f849a17a4"
    )
    assert result.report["price_adjustment"] == "qfq"
    assert result.report["source_trust"] == "declared"
    assert result.report["source_providers"] == [
        "local-synthetic-acceptance"
    ]
    assert result.report["source_evidence_levels"] == ["declared"]
    assert result.report["source_frame_digest"] == (
        "dv2|test|sha256:" + "a" * 64
    )
    assert result.report["source_identity_consistent"] is True
    assert result.report["source_complete_code_coverage"] is True


def test_benchmark_readiness_is_local_and_reports_window_gap() -> None:
    dates = pd.bdate_range("2025-01-10", "2025-01-20", name="date")
    cache = _LocalCache(
        None,
        pd.Series(range(len(dates)), index=dates, dtype=float, name="close"),
    )

    result = asyncio.run(
        inspect_cached_benchmark(
            cache,  # type: ignore[arg-type]
            index_code="000300",
            required_start="2025-01-01",
            required_end="2025-01-31",
        )
    )

    assert result.report["ready"] is False
    assert result.report["issues"] == [
        "benchmark_start_not_covered",
        "benchmark_end_not_covered",
    ]
    assert cache.loads == [("benchmark", "000300")]
