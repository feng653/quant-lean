from __future__ import annotations

import asyncio

import pytest

from backend.data.cache import DataCache, LegacyRuntimeDataDisabledError
from backend.services import maintenance


def test_automatic_pool_discovery_uses_only_atomic_generation_manifests(tmp_path) -> None:
    cache = DataCache(str(tmp_path))
    for pool_id in (
        "csi300",
        "custom_0123456789abcdef",
        "CUSTOM_deadbeef",
        "research_fixture",
    ):
        (tmp_path / "daily" / f"{pool_id}.parquet").parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        (tmp_path / "daily" / f"{pool_id}.parquet").write_bytes(b"fixture")

    manifests = tmp_path / "daily" / "generation-manifests"
    manifests.mkdir(exist_ok=True)
    for pool_id in ("csi300", "research_fixture", "custom_0123456789abcdef"):
        (manifests / f"{pool_id}.json").write_text("{}", encoding="utf-8")

    assert sorted(cache._discover_pools()) == ["csi300", "research_fixture"]


def test_legacy_auto_update_fails_before_calendar_or_provider_access(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = DataCache(str(tmp_path))

    async def forbidden(*_args, **_kwargs):
        pytest.fail("legacy updater must fail before external access")

    monkeypatch.setattr(
        "backend.data.calendar.TradingCalendar.load",
        forbidden,
    )
    with pytest.raises(
        LegacyRuntimeDataDisabledError,
        match="PIT governance collector",
    ):
        asyncio.run(cache.auto_update(object(), "csi300"))


@pytest.mark.parametrize("pool_id", ["all_a", "custom", "research_fixture"])
def test_governance_refresh_rejects_non_governed_scope(pool_id: str) -> None:
    with pytest.raises(
        maintenance.DataUpdateFailedError,
        match="market data update failed",
    ) as captured:
        asyncio.run(maintenance.run_pit_governance_refresh(pool_id, actor_user_id=7))
    assert captured.value.result["errors"][0]["code"] == (
        "point_in_time_pool_unsupported"
    )


def test_governance_refresh_requires_attributed_actor() -> None:
    with pytest.raises(
        maintenance.DataUpdateFailedError,
        match="market data update failed",
    ) as captured:
        asyncio.run(
            maintenance.run_pit_governance_refresh("csi300", actor_user_id=0)
        )
    assert captured.value.result["errors"][0]["code"] == (
        "pit_update_actor_required"
    )
