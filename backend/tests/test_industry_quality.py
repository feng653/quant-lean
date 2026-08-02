from __future__ import annotations

import asyncio
import json

import pandas as pd
import pytest
from fastapi import HTTPException

from backend.api import data as data_api
from backend.data.sources.akshare_source import _parse_eastmoney_industries
from backend.data.sources.validated import build_public_research_source
from backend.data.universe import (
    INDUSTRY_CLASSIFICATION,
    INDUSTRY_MAP_SCHEMA,
    INDUSTRY_SOURCE,
    IndustryClassificationUnavailableError,
    UniverseManager,
    _industry_map_hash,
    normalize_industry_codes,
)


class EmptyIndustrySource:
    calls = 0

    async def fetch_industry_list(self) -> list[dict]:
        self.calls += 1
        return []


class ListOnlyIndustrySource:
    async def fetch_industry_list(self) -> list[dict]:
        return [{"code": "BK0475", "name": "银行"}]


class ConflictingIndustrySource:
    async def fetch_industry_list(self) -> list[dict]:
        return [
            {"code": "BK0001", "name": "银行"},
            {"code": "BK0002", "name": "电子"},
        ]

    async def fetch_industry_components(self, name: str) -> list[str]:
        del name
        return ["000001"]


class UnavailableIndustryManager:
    async def get_industry_map(self, *, strict: bool = False):
        del strict
        raise IndustryClassificationUnavailableError("empty")


def test_eastmoney_industry_columns_do_not_use_board_code_as_name() -> None:
    frame = pd.DataFrame(
        {
            "板块名称": ["银行", "电子"],
            "板块代码": ["BK0475", "BK0473"],
            "最新价": [1.0, 2.0],
        }
    )

    assert _parse_eastmoney_industries(frame) == [
        {"code": "BK0475", "name": "银行"},
        {"code": "BK0473", "name": "电子"},
    ]


def test_eastmoney_industry_code_as_name_is_rejected() -> None:
    attachment_shape = pd.DataFrame(
        {
            "板块代码": ["BK1298", "BK1297"],
            "板块名称": ["BK1298", "BK1297"],
        }
    )

    with pytest.raises(ValueError, match="invalid EastMoney industry name"):
        _parse_eastmoney_industries(attachment_shape)


def test_legacy_or_empty_industry_map_cache_is_not_filterable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "industry_map.json"
    cache_path.write_text(
        json.dumps({"map": {}, "n_stocks": 0}),
        encoding="utf-8",
    )
    manager = UniverseManager(EmptyIndustrySource(), object())
    monkeypatch.setattr(
        manager,
        "_industry_map_cache_path",
        lambda: cache_path,
    )

    assert manager._load_industry_map_cache() is None
    with pytest.raises(
        IndustryClassificationUnavailableError,
        match="empty_or_source_unavailable",
    ):
        asyncio.run(manager.get_industry_map(strict=True))
    assert json.loads(cache_path.read_text(encoding="utf-8")) == {
        "map": {},
        "n_stocks": 0,
    }


def test_industry_filter_fails_when_map_coverage_is_too_low() -> None:
    manager = UniverseManager(EmptyIndustrySource(), object())
    manager._industry_map = {"000001": "银行"}

    with pytest.raises(
        IndustryClassificationUnavailableError,
        match="coverage_insufficient",
    ):
        asyncio.run(
            manager.filter_by_industry(
                ["000001", "000002"],
                ["银行"],
            )
        )
    readiness = asyncio.run(
        manager.get_industry_readiness(["000001", "000002"])
    )
    assert readiness == {
        "filterable": False,
        "reason": "industry_map_coverage_insufficient",
        "source": INDUSTRY_SOURCE,
        "classification": INDUSTRY_CLASSIFICATION,
        "mapped_stocks": 1,
        "requested_stocks": 2,
        "requested_mapped_stocks": 1,
        "invalid_requested_codes": [],
        "map_coverage": 0.5,
        "coverage_scope": "requested_codes",
        "minimum_coverage": 0.95,
    }
    catalog_readiness = asyncio.run(manager.get_industry_readiness())
    assert catalog_readiness["filterable"] is False
    assert catalog_readiness["reason"] == "coverage_not_evaluated"
    assert catalog_readiness["map_coverage"] is None


def test_industry_scope_normalizes_exchange_suffixes_and_rejects_guesses() -> None:
    assert normalize_industry_codes(
        ["000001.SZ", "600000.sh", "430001.BJ", "000001"]
    ) == (
        ["000001", "430001", "600000"],
        [],
    )
    assert normalize_industry_codes(
        ["SZ000001", "00001", "not-a-code", ""]
    ) == (
        [],
        ["00001", "<empty>", "SZ000001", "not-a-code"],
    )


def test_industry_filter_preserves_submitted_code_shape_after_suffix_lookup() -> None:
    manager = UniverseManager(EmptyIndustrySource(), object())
    manager._industry_map = {
        "000001": "银行",
        "600000": "货币金融服务",
    }

    result = asyncio.run(
        manager.filter_by_industry(
            ["000001.SZ", "600000.SH"],
            ["银行"],
        )
    )

    assert result == ["000001.SZ"]
    with pytest.raises(
        IndustryClassificationUnavailableError,
        match="industry_scope_invalid_codes",
    ):
        asyncio.run(
            manager.filter_by_industry(
                ["SZ000001"],
                ["银行"],
            )
        )


def test_industry_list_without_matching_member_endpoint_is_not_filterable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = UniverseManager(ListOnlyIndustrySource(), object())
    monkeypatch.setattr(
        manager,
        "_industry_map_cache_path",
        lambda: tmp_path / "missing.json",
    )

    with pytest.raises(
        IndustryClassificationUnavailableError,
        match="source_unavailable",
    ):
        asyncio.run(manager.get_industry_map(strict=True))


def test_industry_read_path_never_fetches_external_source(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = EmptyIndustrySource()
    manager = UniverseManager(source, object())
    monkeypatch.setattr(
        manager,
        "_industry_map_cache_path",
        lambda: tmp_path / "missing.json",
    )

    readiness = asyncio.run(manager.get_industry_readiness(["000001"]))

    assert source.calls == 0
    assert readiness["filterable"] is False
    assert readiness["reason"] == "industry_map_empty_or_source_unavailable"


def test_valid_industry_map_cache_requires_schema_source_and_classification(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "industry_map.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": INDUSTRY_MAP_SCHEMA,
                "classification": INDUSTRY_CLASSIFICATION,
                "source": INDUSTRY_SOURCE,
                "filterable": True,
                "map": {"000001": "银行"},
                "content_sha256": _industry_map_hash({"000001": "银行"}),
                "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manager = UniverseManager(EmptyIndustrySource(), object())
    monkeypatch.setattr(
        manager,
        "_industry_map_cache_path",
        lambda: cache_path,
    )

    assert manager._load_industry_map_cache() == {"000001": "银行"}

    tampered = json.loads(cache_path.read_text(encoding="utf-8"))
    tampered["map"]["000001"] = "软件"
    cache_path.write_text(
        json.dumps(tampered, ensure_ascii=False),
        encoding="utf-8",
    )
    assert manager._load_industry_map_cache() is None


@pytest.mark.parametrize("offset_days", [-8, 1])
def test_stale_or_future_industry_map_cache_is_rejected(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    offset_days: int,
) -> None:
    cache_path = tmp_path / "industry_map.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": INDUSTRY_MAP_SCHEMA,
                "classification": INDUSTRY_CLASSIFICATION,
                "source": INDUSTRY_SOURCE,
                "filterable": True,
                "map": {"000001": "银行"},
                "updated_at": (
                    pd.Timestamp.now(tz="UTC")
                    + pd.Timedelta(days=offset_days)
                ).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manager = UniverseManager(EmptyIndustrySource(), object())
    monkeypatch.setattr(
        manager,
        "_industry_map_cache_path",
        lambda: cache_path,
    )
    assert manager._load_industry_map_cache() is None


def test_conflicting_industry_membership_fails_closed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = UniverseManager(ConflictingIndustrySource(), object())
    monkeypatch.setattr(
        manager,
        "_industry_map_cache_path",
        lambda: tmp_path / "missing.json",
    )
    with pytest.raises(IndustryClassificationUnavailableError):
        asyncio.run(manager.get_industry_map(strict=True, refresh=True))


def test_industry_map_api_returns_machine_readable_unavailable_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(data_api._data_svc, "source", object())
    monkeypatch.setattr(
        data_api._data_svc,
        "universe",
        UnavailableIndustryManager(),
    )

    with pytest.raises(HTTPException) as captured:
        asyncio.run(data_api.get_industry_map(user={"id": "test"}))
    assert captured.value.status_code == 503
    assert captured.value.detail["code"] == "industry_map_unavailable"


def test_public_research_source_can_build_uncached_industry_map(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list(self):
        del self
        return [{"code": "BK0475", "name": "银行"}]

    async def fake_map(self, codes: list[str]):
        del self
        assert codes == ["000001"]
        return {"000001": "银行"}

    monkeypatch.setattr(
        "backend.data.sources.akshare_source.AKShareSource.fetch_industry_list",
        fake_list,
    )
    monkeypatch.setattr(
        "backend.data.sources.akshare_source.AKShareSource.fetch_industry_map",
        fake_map,
    )
    manager = UniverseManager(build_public_research_source(), object())
    monkeypatch.setattr(
        manager,
        "_industry_map_cache_path",
        lambda: tmp_path / "industry_map.json",
    )

    readiness = asyncio.run(
        manager.get_industry_readiness(["000001"], refresh_missing=True)
    )
    assert readiness["filterable"] is True
    assert asyncio.run(manager.get_industry_map(strict=True)) == {"000001": "银行"}
