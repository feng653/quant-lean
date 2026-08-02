from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.trading import router
from backend.config import settings
from backend.data.cache import (
    DailyMarketDataQualityError,
    LegacyAdjustedCacheError,
)
from backend.dependencies import get_current_user
from backend.main import _init_databases


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7,
        "is_admin": False,
        "permissions": ["trading:read"],
    }
    with TestClient(app) as test_client:
        yield test_client


def test_simulation_calendar_uses_verified_csi500_dates(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    async def inspect(**kwargs) -> SimpleNamespace:
        captured.append(str(kwargs["pool_id"]))
        pivot = pd.DataFrame(
            {"close": [1.0, 2.0, 3.0]},
            index=pd.to_datetime(["2026-07-03", "2026-07-01", "2026-07-02"]),
        )
        return SimpleNamespace(market=SimpleNamespace(frame=pivot))

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        inspect,
    )

    response = client.get("/api/trading/simulate/calendar")

    assert response.status_code == 200
    assert captured == ["csi500"]
    assert response.json() == {
        "data": {
            "pool_id": "csi500",
            "min_date": "2026-07-01",
            "max_date": "2026-07-03",
                "suggested_start": "2026-07-01",
                "trading_days": 3,
                "trust_tier": "governed_production_pit",
                "warning_severity": "none",
                "live_eligible": False,
        }
    }


@pytest.mark.parametrize(
    ("failure", "status_code", "code", "detail", "action"),
    [
        (
            LegacyAdjustedCacheError("/private/cache/csi500.parquet"),
            409,
            "simulation_calendar_cache_integrity_invalid",
            "中证500行情缓存完整性校验失败，请先在数据中心受控重建",
            "refresh_in_data_center",
        ),
        (
            DailyMarketDataQualityError(
                {"issues": ["daily_non_positive_prices"]}
            ),
            409,
            "simulation_calendar_cache_integrity_invalid",
            "中证500行情缓存完整性校验失败，请先在数据中心受控重建",
            "refresh_in_data_center",
        ),
        (
            RuntimeError("/private/cache/csi500.meta.json is corrupt"),
            409,
            "simulation_calendar_cache_integrity_invalid",
            "中证500行情缓存完整性校验失败，请先在数据中心受控重建",
            "refresh_in_data_center",
        ),
    ],
)
def test_simulation_calendar_cache_failures_are_structured_and_safe(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    status_code: int,
    code: str,
    detail: str,
    action: str,
) -> None:
    async def inspect(**kwargs) -> None:
        assert kwargs["pool_id"] == "csi500"
        raise failure

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        inspect,
    )

    response = client.get("/api/trading/simulate/calendar")

    assert response.status_code == status_code
    assert response.json() == {
        "detail": detail,
        "code": code,
        "pool_id": "csi500",
        "action": action,
    }
    assert "/private/" not in response.text


@pytest.mark.parametrize(
    "pivot",
    [
        None,
        pd.DataFrame(),
    ],
)
def test_simulation_calendar_missing_cache_is_structured(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    pivot: pd.DataFrame | None,
) -> None:
    async def inspect(**kwargs) -> SimpleNamespace:
        assert kwargs["pool_id"] == "csi500"
        return SimpleNamespace(market=SimpleNamespace(frame=pivot))

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        inspect,
    )

    response = client.get("/api/trading/simulate/calendar")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "中证500行情缓存尚未下载，请先在数据中心更新数据",
        "code": "simulation_calendar_cache_missing",
        "pool_id": "csi500",
        "action": "update_in_data_center",
    }


def test_simulation_calendar_rejects_invalid_date_index(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def inspect(**kwargs) -> SimpleNamespace:
        assert kwargs["pool_id"] == "csi500"
        pivot = pd.DataFrame(
            {"close": [1.0, 2.0]},
            index=pd.to_datetime(["2026-07-01", "2026-07-01"]),
        )
        return SimpleNamespace(market=SimpleNamespace(frame=pivot))

    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input",
        inspect,
    )

    response = client.get("/api/trading/simulate/calendar")

    assert response.status_code == 409
    assert response.json()["code"] == (
        "simulation_calendar_cache_integrity_invalid"
    )


def test_portfolio_calendar_uses_actual_pool_intersection(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services import simulation

    async def bindings(_user_id, _portfolio_id):
        return [
            {"pool_id": "csi300", "generation_id": "a" * 64},
            {"pool_id": "csi1000", "generation_id": "b" * 64},
        ]

    async def load(pool_id, _end, _cache, **_kwargs):
        dates = (
            ["2026-07-01", "2026-07-02", "2026-07-03"]
            if pool_id == "csi300"
            else ["2026-07-02", "2026-07-03", "2026-07-04"]
        )
        return pd.DataFrame({"close": [1.0] * 3}, index=pd.to_datetime(dates))

    monkeypatch.setattr(simulation, "simulation_pool_bindings", bindings)
    monkeypatch.setattr(simulation, "_load_pivot", load)

    response = client.get("/api/trading/simulate/calendar?portfolio_id=11")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["pool_ids"] == ["csi1000", "csi300"]
    assert payload["min_date"] == "2026-07-02"
    assert payload["max_date"] == "2026-07-03"
    assert payload["trading_days"] == 2
    assert "single_source_tushare_research" in payload["warnings"]


@pytest.mark.parametrize(
    ("owner_id", "status"),
    [(8, "active"), (7, "stopped")],
)
def test_portfolio_calendar_does_not_fallback_for_unavailable_portfolio(
    client: TestClient,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    owner_id: int,
    status: str,
) -> None:
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(tmp_path / "experiment.db"))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    asyncio.run(_init_databases())
    with sqlite3.connect(tmp_path / "trading.db") as connection:
        portfolio_id = connection.execute(
            """
            INSERT INTO portfolios
            (user_id, name, total_capital, rebalance_frequency, allocations,
             status, cash_balance, current_revision)
            VALUES (?, 'scope fixture', 100000, 'daily', '[]', ?, 100000, 1)
            """,
            (owner_id, status),
        ).lastrowid

    response = client.get(
        f"/api/trading/simulate/calendar?portfolio_id={portfolio_id}"
    )

    assert response.status_code == 404
    assert response.json()["code"] == "simulation_portfolio_not_found"
    assert response.json()["portfolio_id"] == portfolio_id


def test_legacy_portfolio_calendar_uses_strict_runtime_gate(
    client: TestClient,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setattr(settings, "EXPERIMENT_DB", str(tmp_path / "experiment.db"))
    monkeypatch.setattr(settings, "TRADING_SIM_DB", str(tmp_path / "trading.db"))
    asyncio.run(_init_databases())
    with sqlite3.connect(tmp_path / "trading.db") as connection:
        deployment_id = connection.execute(
            """
            INSERT INTO deployments
            (user_id, strategy_id, strategy_category, display_name, params,
             params_hash, mode, status, pool_preset)
            VALUES (7, 'ma_cross_v1', 'technical', 'legacy', '{}', 'hash',
                    'batch', 'active', 'csi300')
            """
        ).lastrowid
        portfolio_id = connection.execute(
            """
            INSERT INTO portfolios
            (user_id, name, total_capital, rebalance_frequency, allocations,
             status, cash_balance, current_revision)
            VALUES (7, 'legacy JSON', 100000, 'daily', ?, 'active', 100000, 1)
            """,
            (json.dumps([{"deployment_id": deployment_id, "weight": 1.0}]),),
        ).lastrowid

    async def strict(**kwargs):
        assert kwargs["pool_id"] == "csi300"
        pivot = pd.DataFrame(
            {"close": [1.0, 2.0]},
            index=pd.to_datetime(["2026-07-01", "2026-07-02"]),
        )
        return SimpleNamespace(market=SimpleNamespace(frame=pivot))

    async def legacy_cache(*_args, **_kwargs):
        raise AssertionError("calendar must not bypass strict runtime gate")

    monkeypatch.setattr(
        "backend.services.simulation._load_pivot", legacy_cache
    )
    monkeypatch.setattr(
        "backend.data.pit_runtime.require_pit_runtime_input", strict
    )

    response = client.get(
        f"/api/trading/simulate/calendar?portfolio_id={portfolio_id}"
    )

    assert response.status_code == 200
    assert response.json()["data"]["pool_ids"] == ["csi300"]
