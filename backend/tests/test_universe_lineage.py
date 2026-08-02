from __future__ import annotations

import asyncio
import json

from backend.api import data as data_api
from backend.config import settings
from backend.data.lineage import (
    COUNT_MISMATCH,
    DUPLICATE_CODES,
    MISSING_INDUSTRY_MAPPING,
    NON_POINT_IN_TIME,
    STATIC_UNIVERSE,
    SURVIVORSHIP_BIAS,
    build_universe_snapshot,
    research_risk_warnings,
)
from backend.data.universe import UniverseManager


class CurrentOnlySource:
    def __init__(self, codes: list[str]) -> None:
        self.codes = codes
        self.index_calls: list[str] = []

    async def fetch_index_components(self, index_code: str) -> list[str]:
        self.index_calls.append(index_code)
        return list(self.codes)


def _manager(tmp_path, monkeypatch, codes: list[str]) -> UniverseManager:
    monkeypatch.setattr(settings, "DATA_CACHE_DIR", str(tmp_path))
    return UniverseManager(CurrentOnlySource(codes), object())


def test_csi300_snapshot_exposes_shortfall_duplicates_and_missing_industries(
    tmp_path,
    monkeypatch,
) -> None:
    codes = [f"{number:06d}" for number in range(1, 300)] + ["000001"]
    universe = _manager(tmp_path, monkeypatch, codes)
    universe._industry_map = {}

    async def scenario():
        first = await universe.get_pool_snapshot("csi300", "2020-01-31")
        compatible_codes = await universe.get_pool_codes("csi300", "2020-01-31")
        second = await universe.get_pool_snapshot("csi300", "2020-01-31")
        return first, compatible_codes, second

    first, compatible_codes, second = asyncio.run(scenario())

    assert first.requested_as_of == "2020-01-31"
    assert first.point_in_time is False
    assert first.requested_count == 300
    assert first.unique_count == 299
    assert first.quality.expected_count == 300
    assert first.quality.count_difference == -1
    assert first.quality.duplicate_codes == ("000001",)
    assert first.quality.missing_industry_count == 299
    assert set(first.risk_warnings) >= {
        COUNT_MISMATCH,
        DUPLICATE_CODES,
        MISSING_INDUSTRY_MAPPING,
        NON_POINT_IN_TIME,
        SURVIVORSHIP_BIAS,
    }
    assert compatible_codes == sorted(set(codes))
    assert first.snapshot_hash == second.snapshot_hash
    assert universe._source.index_calls == ["000300"]


def test_pool_cache_preserves_raw_count_duplicates_and_source_timestamp(
    tmp_path,
    monkeypatch,
) -> None:
    universe = _manager(tmp_path, monkeypatch, [])
    universe._save_pool_cache("custom_pool", ["000002", "000001", "000001", ""])

    record = universe._load_pool_cache_record("custom_pool")
    cache_payload = json.loads((tmp_path / "pool_custom_pool.json").read_text(encoding="utf-8"))

    assert record["codes"] == ["000002", "000001", "000001", ""]
    assert record["count"] == 4
    assert record["unique_count"] == 2
    assert record["updated_at"]
    assert record["source_as_of"]
    assert cache_payload["count"] == 4
    assert cache_payload["unique_count"] == 2


def test_legacy_deduplicated_cache_uses_preserved_original_count(
    tmp_path,
    monkeypatch,
) -> None:
    universe = _manager(tmp_path, monkeypatch, [])
    (tmp_path / "pool_csi300.json").write_text(
        json.dumps(
            {
                "pool_id": "csi300",
                "codes": ["000001", "000002"],
                "count": 3,
                "updated_at": "2026-07-28T09:00:00",
            }
        ),
        encoding="utf-8",
    )
    universe._industry_map = {"000001": "银行", "000002": "电子"}

    snapshot = asyncio.run(universe.get_pool_snapshot("csi300", "2020-01-31"))

    assert snapshot.requested_count == 3
    assert snapshot.unique_count == 2
    assert snapshot.quality.duplicate_count == 1
    assert snapshot.quality.duplicate_codes == ()
    assert DUPLICATE_CODES in snapshot.risk_warnings


def test_custom_pool_is_identified_as_static_not_historical_index(
    tmp_path,
    monkeypatch,
) -> None:
    universe = _manager(tmp_path, monkeypatch, [])
    universe._save_pool_cache("my_research_set", ["000002", "000001"])
    universe._industry_map = {
        "000001": "银行",
        "000002": "电子",
    }

    snapshot = asyncio.run(universe.get_pool_snapshot("my_research_set", "2021-06-30"))

    assert snapshot.codes == ("000001", "000002")
    assert STATIC_UNIVERSE in snapshot.risk_warnings
    assert NON_POINT_IN_TIME in snapshot.risk_warnings
    assert SURVIVORSHIP_BIAS in snapshot.risk_warnings
    assert snapshot.quality.missing_industry_count == 0


def test_pool_info_returns_lineage_without_local_cache_path(
    tmp_path,
    monkeypatch,
) -> None:
    universe = _manager(tmp_path, monkeypatch, ["000001", "000002"])
    universe._industry_map = {"000001": "银行", "000002": "电子"}

    info = asyncio.run(universe.get_pool_info("csi300", "2020-01-31"))
    serialized = json.dumps(info, ensure_ascii=False)

    assert info["lineage"]["requested_as_of"] == "2020-01-31"
    assert info["lineage"]["point_in_time"] is False
    assert info["lineage"]["snapshot_hash"]
    assert info["quality"]["expected_count"] == 300
    assert NON_POINT_IN_TIME in info["risk_warnings"]
    assert str(tmp_path) not in serialized
    assert "snapshot_path" not in serialized
    assert "cache_path" not in serialized


def test_pool_detail_api_forwards_research_date_and_returns_lineage(
    monkeypatch,
) -> None:
    from backend.data.point_in_time_master import PointInTimeMasterStore

    def resolved(self, **kwargs):
        assert kwargs["scope_id"] == "csi300"
        assert kwargs["requested_as_of"] == "2020-01-31"
        return {
            "requested_as_of": "2020-01-31",
            "resolved_as_of": "2020-01-31",
            "resolution": "exact_activated_observation",
            "staleness_calendar_days": 0,
            "risk_warnings": [],
            "query": {
                "available": True,
                "reason": None,
                "records": [
                    {"security_code": "000001"},
                    {"security_code": "000002"},
                ],
                "source_batches": [],
            },
        }

    monkeypatch.setattr(PointInTimeMasterStore, "resolve_display_observation", resolved)
    response = asyncio.run(
        data_api.get_pool_detail(
            "csi300",
            "2020-01-31",
            {"id": 7},
        )
    )

    assert response["data"]["lineage"]["requested_as_of"] == "2020-01-31"
    assert response["data"]["lineage"]["resolved_as_of"] == "2020-01-31"
    assert response["data"]["quality"]["unique_count"] == 2
    assert response["data"]["lineage"]["point_in_time"] is True


def test_research_warning_helper_is_conservative_and_validates_snapshot() -> None:
    assert set(research_risk_warnings("csi300", "2018-01-01")) == {
        NON_POINT_IN_TIME,
        SURVIVORSHIP_BIAS,
    }
    assert research_risk_warnings("custom", "2018-01-01") == ()

    custom = build_universe_snapshot(
        "custom",
        ["000001"],
        requested_as_of="2018-01-01",
        source_as_of="2026-01-01",
        point_in_time=False,
        risk_warnings=(STATIC_UNIVERSE,),
    )
    assert STATIC_UNIVERSE in research_risk_warnings(
        "custom",
        "2018-01-01",
        custom,
    )

    try:
        research_risk_warnings("other", "2018-01-01", custom)
    except ValueError as exc:
        assert "pool_id" in str(exc)
    else:
        raise AssertionError("mismatched snapshot must be rejected")
