from __future__ import annotations

import asyncio
import json

import pandas as pd
import pytest

from backend.api import data as data_api
from backend.data.cache import (
    ADJUSTMENT_CONSISTENCY_VERSION,
    DailyMarketDataQualityError,
    DataCache,
    LegacyAdjustedCacheError,
    assess_daily_market_data_quality,
)
from backend.data.cache_readiness import inspect_cached_market_data
from backend.data.source_validation import (
    build_cache_source_provenance,
    build_daily_fetch_evidence,
)
from backend.data.sources.validated import build_public_research_source
from backend.data.universe import UniverseManager


def _frame(
    *,
    value: float = 10.0,
    start: str = "2024-01-01",
    end: str = "2024-01-31",
) -> pd.DataFrame:
    columns = pd.MultiIndex.from_product(
        [["000001"], ["open", "high", "low", "close", "volume"]],
        names=["code", "field"],
    )
    return pd.DataFrame(
        value,
        index=pd.bdate_range(start, end, name="date"),
        columns=columns,
    )


def _provenance(frame: pd.DataFrame, *, adjustment: str = "hfq") -> dict:
    evidence = build_daily_fetch_evidence(
        frame,
        requested_codes=["000001"],
        start=frame.index.min().strftime("%Y-%m-%d"),
        end=frame.index.max().strftime("%Y-%m-%d"),
        provider="quality-test",
        endpoint="python:quality-test",
        adjustment=adjustment,
        evidence_level="declared",
    )
    return build_cache_source_provenance(frame, [evidence])


@pytest.mark.parametrize(
    ("mutate", "issue"),
    [
        (
            lambda frame: frame.__setitem__(
                ("000001", "close"),
                -frame[("000001", "close")],
            ),
            "daily_non_positive_prices",
        ),
        (
            lambda frame: frame.__setitem__(("000001", "high"), 1.0),
            "daily_ohlc_logic_invalid",
        ),
        (
            lambda frame: frame.__setitem__(("000001", "volume"), -1.0),
            "daily_negative_volume",
        ),
    ],
)
def test_quality_gate_rejects_unsafe_market_values(
    mutate,
    issue: str,
) -> None:
    frame = _frame()
    mutate(frame)

    report = assess_daily_market_data_quality(frame)

    assert report["ready"] is False
    assert issue in report["issues"]


def test_quality_gate_rejects_duplicate_and_future_dates() -> None:
    frame = _frame(start="2035-01-01", end="2035-01-03")
    frame = pd.concat([frame, frame.iloc[[0]]])

    report = assess_daily_market_data_quality(
        frame,
        today=pd.Timestamp("2030-01-01"),
    )

    assert report["ready"] is False
    assert report["duplicate_date_count"] == 2
    assert report["future_date_count"] == len(frame)
    assert "daily_duplicate_dates" in report["issues"]
    assert "daily_future_dates" in report["issues"]


def test_invalid_force_refresh_preserves_existing_cache(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _frame()
    invalid = _frame()
    invalid[("000001", "open")] = 0.0

    class Source:
        async def fetch_daily(self, codes, start, end):
            del codes, start, end
            return invalid.copy()

    async def pool_codes(self, pool_id: str, date: str | None = None):
        del self, pool_id, date
        return ["000001"]

    monkeypatch.setattr(UniverseManager, "get_pool_codes", pool_codes)
    cache = DataCache(str(tmp_path))

    async def scenario() -> pd.DataFrame:
        await cache.save_legacy_pivot_for_audit("csi300", original)
        with pytest.raises(
            DailyMarketDataQualityError,
            match="daily_non_positive_prices",
        ):
            await cache.get_or_fetch(
                "csi300",
                Source(),
                force=True,
                start="2024-01-01",
                end="2024-01-31",
            )
        unchanged = await cache.load_legacy_pivot_for_audit("csi300")
        assert unchanged is not None
        return unchanged

    assert asyncio.run(scenario()).equals(original)


def test_hfq_cache_does_not_upgrade_declared_fixture_to_research(
    tmp_path,
) -> None:
    cache = DataCache(str(tmp_path))
    frame = _frame()

    async def scenario() -> dict:
        await cache.save_pivot(
            "csi300",
            frame,
            source_provenance=_provenance(frame, adjustment="hfq"),
        )
        view = cache._daily_generations.load("csi300")
        assert view is not None
        return json.loads(
            view.artifacts["metadata"].read_text(encoding="utf-8")
        )

    metadata = asyncio.run(scenario())
    assert metadata["price_adjustment"] == "hfq"
    assert (
        metadata["adjustment_consistency"]
        == ADJUSTMENT_CONSISTENCY_VERSION
    )
    assert metadata["data_quality"]["ready"] is True
    assert metadata["ready_for_return_research"] is False
    assert metadata["ready_for_execution_simulation"] is False
    assert metadata["price_ledger"] == {
        "schema_version": "legacy-price-cache-role/v1",
        "ledger_available": False,
        "reason": "ledger_unavailable",
        "adjustment": "hfq",
        "role": "adjusted_return_research",
        "adjusted_return_price_available": True,
        "raw_execution_price_available": False,
        "dual_ledger_complete": False,
        "restriction": "legacy_cache_is_not_a_dual_price_ledger",
    }


@pytest.mark.parametrize("pool_id", ["csi300", "custom", "desk_watchlist"])
def test_readiness_never_treats_static_universe_as_point_in_time(
    pool_id: str,
) -> None:
    frame = _frame()

    class LocalCache:
        async def load_pivot_with_provenance(self, cache_key):
            del cache_key
            provenance = _provenance(frame)
            return frame.copy(), provenance

    result = asyncio.run(
        inspect_cached_market_data(
            LocalCache(),  # type: ignore[arg-type]
            cache_key=pool_id,
            pool_id=pool_id,
            requested_codes=[],
            required_start="2024-01-01",
            required_end="2024-01-31",
        )
    )

    assert result.report["ready"] is True
    assert result.report["ready_for_return_research"] is False
    assert result.report["ready_for_unbiased_return_research"] is False
    assert "static_price_research_only" in result.report[
        "return_research_semantics"
    ]
    assert result.report["ready_for_unbiased_tuning"] is False
    assert result.report["universe_point_in_time"] is False
    assert result.report["survivorship_bias_risk"] is True
    assert "point_in_time_universe_missing" in (
        result.report["research_limitations"]
    )
    assert "research_grade_source_evidence_missing" in (
        result.report["research_limitations"]
    )


def test_production_public_research_source_uses_hfq_on_both_feeds() -> None:
    source = build_public_research_source()

    assert source.primary.price_adjustment == "raw"
    assert source.reference.price_adjustment == "raw"
    assert source.adjusted_reference is not None
    assert source.adjusted_reference.price_adjustment == "hfq"


@pytest.mark.parametrize(
    ("raw_cross_validated", "adjusted_factor_validated", "eligible"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, True),
    ],
)
def test_public_source_requires_raw_and_factor_validation_for_research(
    raw_cross_validated: bool,
    adjusted_factor_validated: bool,
    eligible: bool,
) -> None:
    frame = _frame()

    class LocalCache:
        async def load_pivot_with_provenance(self, cache_key):
            del cache_key
            return frame.copy(), {
                "providers": ["akshare:eastmoney"],
                "evidence_levels": ["public_aggregator"],
                "adjustments": ["hfq"],
                "frame_digest": "dv2|test|sha256:" + "a" * 64,
                "identity_consistent": True,
                "complete_code_coverage": True,
                "all_batches_cross_validated": True,
                "all_batches_raw_cross_validated": raw_cross_validated,
                "all_batches_adjusted_factor_validated": (
                    adjusted_factor_validated
                ),
            }

    result = asyncio.run(
        inspect_cached_market_data(
            LocalCache(),  # type: ignore[arg-type]
            cache_key="research",
            pool_id="custom",
            requested_codes=["000001"],
            required_start="2024-01-01",
            required_end="2024-01-31",
        )
    )

    assert result.report["ready"] is True
    assert result.report["source_research_eligible"] is eligible
    assert result.report["ready_for_return_research"] is eligible
    assert result.report["ready_for_static_adjusted_return_research"] is eligible
    assert result.report["ready_for_unbiased_return_research"] is False
    assert (
        result.report["source_all_batches_raw_cross_validated"]
        is raw_cross_validated
    )
    assert (
        result.report["source_all_batches_adjusted_factor_validated"]
        is adjusted_factor_validated
    )
    assert result.report["source_validation_ready"] is eligible
    assert result.report["ready_for_unbiased_tuning"] is False


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (
            LegacyAdjustedCacheError("controlled force refresh required"),
            "cache_schema_or_provenance_invalid",
        ),
        (
            DailyMarketDataQualityError(
                {
                    "ready": False,
                    "issues": ["daily_non_positive_prices"],
                    "non_positive_price_count": 4,
                }
            ),
            "cache_price_quality_invalid",
        ),
    ],
)
def test_cache_status_returns_structured_unavailable_reason(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    error_code: str,
) -> None:
    class Cache:
        async def get_cache_info(self, pool_id):
            return {
                "pool_id": pool_id,
                "exists": True,
                "schema_version": 3,
                "fields": ["open", "high", "low", "close", "volume"],
                "source_trust": "unverified",
            }

        async def load_pivot(self, pool_id):
            del pool_id
            raise error

    monkeypatch.setattr(data_api._data_svc, "source", object())
    monkeypatch.setattr(data_api._data_svc, "cache", Cache())

    response = asyncio.run(
        data_api.get_cache_status(
            pool_id="csi300",
            user={"id": 1},
        )
    )["data"]

    assert response["available"] is False
    assert response["error_code"] == error_code
    assert response["recommended_action"] == (
        "submit_controlled_data_update"
    )
    if isinstance(error, DailyMarketDataQualityError):
        assert response["data_quality"]["non_positive_price_count"] == 4
