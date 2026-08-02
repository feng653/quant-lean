from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.data.cache import (
    POOL_BENCHMARK_MAP,
    DataCache,
    resolve_pool_benchmark,
)
from backend.data.sources.akshare_source import AKShareSource
from backend.data.sources.base import DataSource


class LegacyFakeSource(DataSource):
    async def fetch_daily(
        self,
        codes: list[str],
        start: str,
        end: str,
    ) -> pd.DataFrame:
        return pd.DataFrame()

    async def fetch_index_components(
        self,
        index_code: str,
        date: str | None = None,
    ) -> list[str]:
        return []

    async def fetch_trading_calendar(self, start: str, end: str) -> list[str]:
        return []

    async def fetch_industry_list(self) -> list[dict]:
        return []


class FakeIndexSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def fetch_index_daily(
        self,
        index_code: str,
        start: str,
        end: str,
    ) -> pd.Series:
        self.calls.append((index_code, start, end))
        dates = pd.date_range(start, end, freq="D")
        return pd.Series(
            range(100, 100 + len(dates)),
            index=dates,
            dtype="float64",
            name="close",
        )


def test_optional_index_method_does_not_break_legacy_fake_sources():
    source = LegacyFakeSource()

    with pytest.raises(NotImplementedError, match="does not support"):
        asyncio.run(source.fetch_index_daily("000300", "2024-01-01", "2024-01-02"))


@pytest.mark.parametrize(
    ("date_column", "close_column"),
    [("date", "close"), ("日期", "收盘")],
)
def test_akshare_index_daily_normalizes_columns_and_clips_range(
    monkeypatch,
    date_column,
    close_column,
):
    calls: list[str] = []

    def stock_zh_index_daily(*, symbol: str) -> pd.DataFrame:
        calls.append(symbol)
        return pd.DataFrame(
            {
                date_column: [
                    "2024-01-04",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-01",
                ],
                close_column: ["104.5", "102.5", "103.5", "101.5"],
            }
        )

    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_index_daily=stock_zh_index_daily),
    )

    result = asyncio.run(
        AKShareSource().fetch_index_daily(
            "000905",
            "2024-01-02",
            "2024-01-03",
        )
    )

    assert calls == ["sh000905"]
    assert result.name == "close"
    assert isinstance(result.index, pd.DatetimeIndex)
    assert result.index.is_monotonic_increasing
    assert list(result.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    assert result.tolist() == [102.5, 103.5]


def test_akshare_index_daily_returns_stable_empty_series_on_failure(monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(
        "backend.data.sources.akshare_source._retry_call",
        fail,
    )
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        SimpleNamespace(stock_zh_index_daily=lambda **kwargs: pd.DataFrame()),
    )

    result = asyncio.run(
        AKShareSource().fetch_index_daily(
            "000300",
            "2024-01-01",
            "2024-01-02",
        )
    )

    assert result.empty
    assert result.name == "close"
    assert isinstance(result.index, pd.DatetimeIndex)


def test_index_cache_hits_without_refetch_and_stays_separate(tmp_path):
    cache = DataCache(str(tmp_path))
    source = FakeIndexSource()

    first = asyncio.run(
        cache.get_or_fetch_index(
            "000300",
            source,
            start="2024-01-01",
            end="2024-01-03",
        )
    )
    second = asyncio.run(
        cache.get_or_fetch_index(
            "000300",
            source,
            start="2024-01-02",
            end="2024-01-03",
        )
    )

    assert len(first) == 3
    assert len(second) == 2
    assert source.calls == [("000300", "2024-01-01", "2024-01-03")]
    assert (tmp_path / "indexes" / "000300.parquet").exists()
    assert not (tmp_path / "daily" / "000300.parquet").exists()


def test_index_cache_extends_both_sides_and_merges(tmp_path):
    cache = DataCache(str(tmp_path))
    source = FakeIndexSource()

    asyncio.run(
        cache.get_or_fetch_index(
            "000852",
            source,
            start="2024-01-03",
            end="2024-01-05",
        )
    )
    result = asyncio.run(
        cache.get_or_fetch_index(
            "000852",
            source,
            start="2024-01-01",
            end="2024-01-07",
        )
    )
    cached = asyncio.run(cache.load_index("000852"))

    assert source.calls == [
        ("000852", "2024-01-03", "2024-01-05"),
        ("000852", "2024-01-01", "2024-01-02"),
        ("000852", "2024-01-06", "2024-01-07"),
    ]
    assert list(result.index.strftime("%Y-%m-%d")) == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-06",
        "2024-01-07",
    ]
    assert cached is not None
    assert cached.index.equals(result.index)


def test_index_code_rejects_path_traversal(tmp_path):
    cache = DataCache(str(tmp_path))

    with pytest.raises(ValueError, match="6 digits"):
        asyncio.run(cache.load_index("../000300"))
    with pytest.raises(ValueError, match="6 digits"):
        asyncio.run(
            cache.get_or_fetch_index(
                "000300/../../secret",
                FakeIndexSource(),
                start="2024-01-01",
                end="2024-01-02",
            )
        )


def test_pool_benchmark_mapping_and_unknown_default():
    assert POOL_BENCHMARK_MAP == {
        "csi300": "000300",
        "csi500": "000905",
        "csi800": "000906",
        "csi1000": "000852",
        "all_a": "000300",
        "custom": "000300",
    }
    assert resolve_pool_benchmark("CSI500") == "000905"
    assert resolve_pool_benchmark("unknown") == "000300"
    assert resolve_pool_benchmark(None) == "000300"
