from __future__ import annotations

import asyncio
import json

import pandas as pd
import pytest

from backend.data.cache import (
    ADJUSTMENT_CONSISTENCY_VERSION,
    DAILY_CACHE_SCHEMA_VERSION,
    DataCache,
    LegacyAdjustedCacheError,
)
from backend.data.source_validation import (
    build_cache_source_provenance,
    build_daily_fetch_evidence,
)
from backend.data.universe import UniverseManager


def _ohlcv(start: str, end: str, value: float) -> pd.DataFrame:
    index = pd.date_range(start, end, freq="D", name="date")
    columns = pd.MultiIndex.from_product(
        [["000001"], ["open", "high", "low", "close", "volume"]],
        names=["code", "field"],
    )
    return pd.DataFrame(value, index=index, columns=columns)


class FakeDailySource:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str, str]] = []

    async def fetch_daily(
        self,
        codes: list[str],
        start: str,
        end: str,
    ) -> pd.DataFrame:
        self.calls.append((tuple(codes), start, end))
        return _ohlcv(start, end, float(len(self.calls)))


class ConsistentExtensionSource(FakeDailySource):
    async def fetch_daily(
        self,
        codes: list[str],
        start: str,
        end: str,
    ) -> pd.DataFrame:
        self.calls.append((tuple(codes), start, end))
        frame = _ohlcv(start, end, 20.0)
        overlap = (frame.index >= "2024-01-03") & (
            frame.index <= "2024-01-05"
        )
        frame.loc[overlap, :] = 10.0
        return frame


async def _save_with_source_provenance(
    cache: DataCache,
    pool_id: str,
    frame: pd.DataFrame,
    source: object | None = None,
) -> None:
    codes = sorted(set(frame.columns.get_level_values(0)))
    evidence = build_daily_fetch_evidence(
        frame,
        requested_codes=codes,
        start=frame.index.min().strftime("%Y-%m-%d"),
        end=frame.index.max().strftime("%Y-%m-%d"),
        provider=type(source or FakeDailySource()).__name__,
        endpoint=(
            f"python:{type(source or FakeDailySource()).__module__}."
            f"{type(source or FakeDailySource()).__qualname__}"
        ),
        adjustment="qfq",
        evidence_level="declared",
    )
    provenance = build_cache_source_provenance(frame, [evidence])
    await cache.save_pivot(
        pool_id,
        frame,
        source_provenance=provenance,
    )


def test_daily_cache_checks_overlap_before_extending_both_sides(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def pool_codes(self, pool_id: str, date: str | None = None):
        del self, pool_id, date
        return ["000001"]

    monkeypatch.setattr(UniverseManager, "get_pool_codes", pool_codes)
    cache = DataCache(str(tmp_path))
    source = ConsistentExtensionSource()

    async def scenario() -> tuple[pd.DataFrame, pd.DataFrame]:
        await _save_with_source_provenance(
            cache,
            "csi300",
            _ohlcv("2024-01-03", "2024-01-05", 10.0),
            source,
        )
        result = await cache.get_or_fetch(
            "csi300",
            source,
            start="2024-01-01",
            end="2024-01-07",
        )
        cached = await cache.load_pivot("csi300")
        assert cached is not None
        return result, cached

    result, cached = asyncio.run(scenario())

    assert source.calls == [
        (("000001",), "2024-01-01", "2024-01-05"),
        (("000001",), "2024-01-03", "2024-01-07"),
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
    assert cached.index.equals(result.index)
    assert result.loc["2024-01-03", ("000001", "open")] == 10.0
    assert result.loc["2024-01-07", ("000001", "open")] == 20.0


def test_qfq_overlap_drift_forces_full_cache_rebuild(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def pool_codes(self, pool_id: str, date: str | None = None):
        del self, pool_id, date
        return ["000001"]

    monkeypatch.setattr(UniverseManager, "get_pool_codes", pool_codes)
    cache = DataCache(str(tmp_path))
    source = FakeDailySource()

    async def scenario() -> tuple[pd.DataFrame, pd.DataFrame]:
        await _save_with_source_provenance(
            cache,
            "csi300",
            _ohlcv("2024-01-03", "2024-01-05", 10.0),
            source,
        )
        result = await cache.get_or_fetch(
            "csi300",
            source,
            start="2024-01-03",
            end="2024-01-07",
        )
        cached = await cache.load_pivot("csi300")
        assert cached is not None
        return result, cached

    result, cached = asyncio.run(scenario())

    assert source.calls == [
        (("000001",), "2024-01-03", "2024-01-07"),
        (("000001",), "2024-01-03", "2024-01-07"),
    ]
    assert (result == 2.0).to_numpy().all()
    assert cached.equals(result)


def test_daily_cache_hit_requires_coverage_and_returns_requested_slice(
    tmp_path,
) -> None:
    cache = DataCache(str(tmp_path))
    source = FakeDailySource()

    async def scenario() -> pd.DataFrame:
        await _save_with_source_provenance(
            cache,
            "csi300",
            _ohlcv("2024-01-01", "2024-01-07", 10.0),
        )
        return await cache.get_or_fetch(
            "csi300",
            source,
            start="2024-01-03",
            end="2024-01-04",
        )

    result = asyncio.run(scenario())

    assert source.calls == []
    assert list(result.index.strftime("%Y-%m-%d")) == [
        "2024-01-03",
        "2024-01-04",
    ]


def test_declared_cache_is_not_promoted_to_public_research_trust(
    tmp_path,
) -> None:
    cache = DataCache(str(tmp_path))

    async def scenario() -> dict:
        await _save_with_source_provenance(
            cache,
            "acceptance_fixture",
            _ohlcv("2024-01-01", "2024-01-07", 10.0),
        )
        return await cache.get_cache_info("acceptance_fixture")

    info = asyncio.run(scenario())
    assert info["source_trust"] == "declared"


def test_legacy_qfq_cache_is_blocked_until_controlled_force_refresh(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def pool_codes(self, pool_id: str, date: str | None = None):
        del self, pool_id, date
        return ["000001"]

    monkeypatch.setattr(UniverseManager, "get_pool_codes", pool_codes)
    cache = DataCache(str(tmp_path))
    source = FakeDailySource()

    async def scenario() -> tuple[pd.DataFrame, dict]:
        original = _ohlcv("2024-01-01", "2024-01-07", 10.0)
        await cache.save_legacy_pivot_for_audit("csi300", original)
        view = cache._daily_generations.load("csi300")
        assert view is not None
        meta_path = view.artifacts["metadata"]
        legacy_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        legacy_meta["schema_version"] = 2
        legacy_meta.pop("price_adjustment")
        legacy_meta.pop("adjustment_consistency")
        meta_path.write_text(json.dumps(legacy_meta), encoding="utf-8")

        with pytest.raises(LegacyAdjustedCacheError, match="force refresh"):
            await cache.get_or_fetch(
                "csi300",
                source,
                start="2024-01-03",
                end="2024-01-04",
            )
        unchanged = await cache.load_legacy_pivot_for_audit("csi300")
        # The out-of-band metadata edit now invalidates the generation hash;
        # even the audit facade must not return a partially trusted view.
        assert unchanged is None
        assert source.calls == []

        refreshed = await cache.get_or_fetch(
            "csi300",
            source,
            force=True,
            start="2024-01-01",
            end="2024-01-07",
        )
        refreshed_view = cache._daily_generations.load("csi300")
        assert refreshed_view is not None
        meta_path = refreshed_view.artifacts["metadata"]
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        return refreshed, metadata

    refreshed, metadata = asyncio.run(scenario())

    assert (refreshed == 1.0).to_numpy().all()
    assert metadata["schema_version"] == DAILY_CACHE_SCHEMA_VERSION
    assert metadata["price_adjustment"] == "qfq"
    assert (
        metadata["adjustment_consistency"]
        == ADJUSTMENT_CONSISTENCY_VERSION
    )


def test_runtime_save_and_load_cannot_bypass_schema_v4(tmp_path) -> None:
    cache = DataCache(str(tmp_path))
    frame = _ohlcv("2024-01-01", "2024-01-07", 10.0)

    async def scenario() -> None:
        with pytest.raises(ValueError, match="require source_provenance"):
            await cache.save_pivot("csi300", frame)
        await cache.save_legacy_pivot_for_audit("csi300", frame)
        with pytest.raises(LegacyAdjustedCacheError, match="schema-v4"):
            await cache.load_pivot("csi300")
        legacy = await cache.load_legacy_pivot_for_audit("csi300")
        assert legacy is not None and legacy.equals(frame)

    asyncio.run(scenario())


def test_same_pool_concurrent_writes_leave_bound_frame_and_meta(tmp_path) -> None:
    cache = DataCache(str(tmp_path))
    first = _ohlcv("2024-01-01", "2024-01-07", 10.0)
    second = _ohlcv("2024-01-01", "2024-01-07", 20.0)

    async def scenario() -> pd.DataFrame:
        await asyncio.gather(
            _save_with_source_provenance(cache, "csi300", first),
            _save_with_source_provenance(cache, "csi300", second),
        )
        loaded = await cache.load_pivot("csi300")
        assert loaded is not None
        return loaded

    loaded = asyncio.run(scenario())
    assert loaded.equals(first) or loaded.equals(second)
    assert not list((tmp_path / "daily").glob("*.tmp"))


def test_custom_cache_hit_rebuilds_when_range_is_too_short(tmp_path) -> None:
    cache = DataCache(str(tmp_path))
    source = FakeDailySource()

    async def scenario() -> pd.DataFrame:
        await _save_with_source_provenance(
            cache,
            "custom_test",
            _ohlcv("2024-01-03", "2024-01-05", 10.0),
            source,
        )
        return await cache.get_or_fetch_custom(
            "custom_test",
            source,
            ["000001"],
            "2024-01-01",
            "2024-01-07",
        )

    result = asyncio.run(scenario())
    assert source.calls == [
        (("000001",), "2024-01-01", "2024-01-07")
    ]
    assert result.index.min() == pd.Timestamp("2024-01-01")
    assert result.index.max() == pd.Timestamp("2024-01-07")


@pytest.mark.parametrize(
    "pool_id",
    ["../secret", "nested/pool", r"..\secret", "", "a" * 65],
)
def test_daily_and_universe_cache_reject_path_traversal(
    tmp_path,
    pool_id: str,
) -> None:
    cache = DataCache(str(tmp_path))
    universe = UniverseManager(object(), cache)

    async def scenario() -> None:
        with pytest.raises(ValueError, match="Invalid pool_id"):
            await cache.load_pivot(pool_id)
        with pytest.raises(ValueError, match="Invalid pool_id"):
            await cache.save_pivot(pool_id, pd.DataFrame())
        with pytest.raises(ValueError, match="Invalid pool_id"):
            await cache.get_cache_info(pool_id)
        with pytest.raises(ValueError, match="Invalid pool_id"):
            await universe.get_pool_codes(pool_id)

    asyncio.run(scenario())
    with pytest.raises(ValueError, match="Invalid pool_id"):
        universe._pool_cache_path(pool_id)
def test_atomic_frame_and_provenance_read_blocks_concurrent_writer(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = DataCache(str(tmp_path))
    first = _ohlcv("2024-01-01", "2024-01-05", 10.0)
    second = _ohlcv("2024-01-01", "2024-01-05", 20.0)

    async def scenario():
        await _save_with_source_provenance(cache, "csi300", first)
        original = cache._load_generation_pivot_sync
        import threading
        read_started = threading.Event()
        release_read = threading.Event()

        def paused_read(view):
            frame = original(view)
            read_started.set()
            # The event loop remains free because this runs in the bounded
            # integrity thread; the test exercises the manifest view held by
            # the reader while a writer prepares a later generation.
            import time
            while not release_read.is_set():
                time.sleep(0.005)
            return frame

        monkeypatch.setattr(cache, "_load_generation_pivot_sync", paused_read)
        reader = asyncio.create_task(cache.load_pivot_with_provenance("csi300"))
        assert await asyncio.to_thread(read_started.wait, 5)
        writer = asyncio.create_task(
            _save_with_source_provenance(cache, "csi300", second)
        )
        await asyncio.sleep(0)
        assert writer.done() is False
        release_read.set()
        loaded_frame, loaded_provenance = await reader
        await writer
        monkeypatch.setattr(cache, "_load_generation_pivot_sync", original)
        current_frame, current_provenance = (
            await cache.load_pivot_with_provenance("csi300")
        )
        return (
            loaded_frame,
            loaded_provenance,
            current_frame,
            current_provenance,
        )

    old_frame, old_provenance, new_frame, new_provenance = asyncio.run(
        scenario()
    )
    assert old_frame is not None
    assert new_frame is not None
    assert old_provenance is not None
    assert new_provenance is not None
    assert float(old_frame.iloc[0, 0]) == 10.0
    assert float(new_frame.iloc[0, 0]) == 20.0
    assert old_provenance["content_sha256"] != (
        new_provenance["content_sha256"]
    )


def test_atomic_frame_and_provenance_read_rejects_mismatched_files(
    tmp_path,
) -> None:
    cache = DataCache(str(tmp_path))
    asyncio.run(
        _save_with_source_provenance(
            cache,
            "csi300",
            _ohlcv("2024-01-01", "2024-01-05", 10.0),
        )
    )
    # Simulate an out-of-band/partial replacement that did not update the
    # provenance metadata. The reader must reject, not combine both versions.
    view = cache._daily_generations.load("csi300")
    assert view is not None
    _ohlcv("2024-01-01", "2024-01-05", 20.0).to_parquet(
        view.artifacts["pivot"], index=True
    )

    with pytest.raises(LegacyAdjustedCacheError, match="generation manifest"):
        asyncio.run(cache.load_pivot_with_provenance("csi300"))
