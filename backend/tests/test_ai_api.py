"""AI API routing, cache context, ownership, and JSON contract tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.ai.service import AiInvocationResult
from backend.api import ai as ai_api
from backend.strategies.technical.ma_cross import MACrossStrategy


class FakeRegistry:
    @staticmethod
    def get_metadata(strategy_id: str):
        if strategy_id != "ma_cross_v1":
            raise KeyError(strategy_id)
        return MACrossStrategy.metadata()


class MockAiService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = {
            "analyze-backtest": "backtest analysis",
            "suggest-params": (
                '{"suggestions":[{"param_name":"fast_period","current_value":5,'
                '"suggested_value":10,"reason":"降低短期噪声"}]}'
            ),
            "market-insight": "market insight",
            "diagnose-error": (
                '{"category":"data","root_cause":"行情缺失",'
                '"evidence":"错误日志显示 empty pivot",'
                '"fix_suggestion":"补齐行情缓存","auto_fixable":false}'
            ),
            "explain-signal": "signal explanation",
        }

    async def invoke(
        self,
        endpoint: str,
        user_id: int | None,
        prompt_template: str,
        **kwargs: Any,
    ) -> AiInvocationResult:
        validator = kwargs.get("validator")
        text = self.responses[endpoint]
        structured = validator(text) if validator is not None else None
        self.calls.append(
            {
                "endpoint": endpoint,
                "user_id": user_id,
                "prompt_template": prompt_template,
                **kwargs,
            }
        )
        return AiInvocationResult(
            text=text,
            model="deepseek-test",
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            cached=False,
            cache_key=f"key-{endpoint}",
            latency_ms=12.5,
            structured=structured,
        )


async def _init_databases(experiment_db: Path, trading_db: Path) -> None:
    async with aiosqlite.connect(experiment_db) as conn:
        await conn.executescript(
            """
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                strategy_id TEXT NOT NULL,
                status TEXT,
                params TEXT,
                pool_preset TEXT,
                pool_custom_codes TEXT,
                train_start TEXT,
                train_end TEXT,
                test_start TEXT,
                test_end TEXT,
                error_log TEXT,
                ai_diagnosis TEXT,
                data_version TEXT,
                created_at TEXT,
                started_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE experiment_metrics (
                experiment_id INTEGER,
                sharpe_ratio REAL,
                annual_return REAL,
                max_drawdown REAL,
                volatility REAL,
                calmar_ratio REAL,
                sortino_ratio REAL,
                win_rate REAL,
                profit_loss_ratio REAL,
                total_trades INTEGER,
                alpha REAL,
                beta REAL,
                information_ratio REAL
            );
            """
        )
        await conn.executemany(
            """
            INSERT INTO experiments
                (id, user_id, strategy_id, status, params, pool_preset,
                 pool_custom_codes, train_start, train_end, test_start,
                 test_end, error_log, data_version, created_at, started_at,
                 completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    7,
                    "ma_cross_v1",
                    "completed",
                    '{"fast_period":5,"slow_period":30}',
                    "csi300",
                    None,
                    None,
                    None,
                    "2024-01-01",
                    "2024-06-30",
                    None,
                    "prices-v2",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-07-01T10:00:00",
                ),
                (
                    2,
                    7,
                    "ma_cross_v1",
                    "failed",
                    '{"fast_period":5,"slow_period":30}',
                    "csi300",
                    None,
                    None,
                    None,
                    "2024-01-01",
                    "2024-06-30",
                    "empty pivot",
                    "prices-v2",
                    "2024-01-01",
                    "2024-07-01T09:00:00",
                    "2024-07-01T09:30:00",
                ),
                (
                    3,
                    99,
                    "ma_cross_v1",
                    "completed",
                    '{"fast_period":9,"slow_period":30}',
                    "csi300",
                    None,
                    None,
                    None,
                    "2024-01-01",
                    "2024-06-30",
                    None,
                    "other-version",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-07-02",
                ),
            ],
        )
        await conn.executemany(
            """
            INSERT INTO experiment_metrics
                (experiment_id, sharpe_ratio, annual_return, max_drawdown,
                 volatility, win_rate, total_trades)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, 1.5, 0.2, -0.1, 0.15, 0.55, 20),
                (2, None, None, None, None, None, None),
                (3, 9.9, 0.9, -0.01, 0.1, 0.9, 5),
            ],
        )
        await conn.commit()

    async with aiosqlite.connect(trading_db) as conn:
        await conn.executescript(
            """
            CREATE TABLE portfolios (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT,
                total_capital REAL,
                rebalance_frequency TEXT,
                allocations TEXT
            );
            CREATE TABLE position_snapshots (
                portfolio_id INTEGER,
                date TEXT,
                code TEXT,
                market_value REAL
            );
            CREATE TABLE nav_history (
                portfolio_id INTEGER,
                date TEXT,
                nav REAL,
                daily_return REAL
            );
            """
        )
        await conn.executemany(
            "INSERT INTO portfolios VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 7, "owner portfolio", 1_000_000, "monthly", "[]"),
                (2, 99, "other portfolio", 1_000_000, "monthly", "[]"),
            ],
        )
        await conn.executemany(
            "INSERT INTO position_snapshots VALUES (?, ?, ?, ?)",
            [
                (1, "2024-07-01", "A", 600_000),
                (1, "2024-07-01", "B", 400_000),
            ],
        )
        await conn.executemany(
            "INSERT INTO nav_history VALUES (?, ?, ?, ?)",
            [
                (1, "2024-07-01", 1.03, 0.01),
                (1, "2024-06-28", 1.02, 0.005),
            ],
        )
        await conn.commit()


@pytest.fixture
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    experiment_db = tmp_path / "experiment.db"
    trading_db = tmp_path / "trading.db"
    asyncio.run(_init_databases(experiment_db, trading_db))

    async def fake_get_db(name: str):
        path = experiment_db if name == "experiment" else trading_db
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    service = MockAiService()
    monkeypatch.setattr(ai_api, "get_db", fake_get_db)
    monkeypatch.setattr(ai_api, "get_strategy_registry", lambda: FakeRegistry())
    monkeypatch.setattr(ai_api, "AiService", lambda: service)

    class FakeUniverseManager:
        def __init__(self, source, cache) -> None:
            del source, cache

        async def get_industry_map(self) -> dict[str, str]:
            return {"A": "科技", "B": "金融"}

    monkeypatch.setattr(ai_api, "UniverseManager", FakeUniverseManager)
    monkeypatch.setattr(ai_api, "AKShareSource", lambda: object())
    monkeypatch.setattr(ai_api, "DataCache", lambda: object())
    return service, experiment_db


def test_all_five_endpoints_use_service_and_stable_cache_context(api_env) -> None:
    service, experiment_db = api_env
    user = {"id": 7, "is_admin": False}

    async def scenario() -> list[dict[str, Any]]:
        return [
            await ai_api.analyze_backtest(
                ai_api.AnalyzeBacktestBody(experiment_id=1), user
            ),
            await ai_api.suggest_params(
                ai_api.SuggestParamsBody(
                    strategy_id="ma_cross_v1",
                    current_params={"fast_period": 5, "slow_period": 30},
                ),
                user,
            ),
            await ai_api.market_insight(
                ai_api.MarketInsightBody(portfolio_id=1), user
            ),
            await ai_api.diagnose_error(
                ai_api.DiagnoseErrorBody(
                    experiment_id=2, error_log="empty pivot"
                ),
                user,
            ),
            await ai_api.explain_signal(
                ai_api.ExplainSignalBody(
                    strategy_id="ma_cross_v1",
                    signal={"code": "A", "action": "BUY", "score": 0.8},
                    context={"date": "2024-07-01"},
                ),
                user,
            ),
        ]

    responses = asyncio.run(scenario())
    assert [call["endpoint"] for call in service.calls] == [
        "analyze-backtest",
        "suggest-params",
        "market-insight",
        "diagnose-error",
        "explain-signal",
    ]
    assert service.calls[0]["cache_context"] == {
        "experiment_id": 1,
        "completed_at": "2024-07-01T10:00:00",
        "data_version": "prices-v2",
    }
    suggestion_context = service.calls[1]["cache_context"]
    assert suggestion_context["strategy"]["strategy_id"] == "ma_cross_v1"
    assert suggestion_context["current_params"]["fast_period"] == 5
    assert [item["experiment_id"] for item in suggestion_context["historical_best"]] == [1]
    market_context = service.calls[2]["cache_context"]
    assert market_context["latest_nav_date"] == "2024-07-01"
    assert market_context["latest_position_date"] == "2024-07-01"
    assert market_context["industry_exposure"] == [
        {"industry": "科技", "market_value": 600000.0, "weight_pct": 60.0},
        {"industry": "金融", "market_value": 400000.0, "weight_pct": 40.0},
    ]
    assert '"industry": "科技"' in service.calls[2]["industry_exposure"]
    assert service.calls[2]["position_count"] == 2
    assert service.calls[3]["cache_context"] == {
        "experiment_id": 2,
        "updated_at": "2024-07-01T09:30:00",
        "completed_at": "2024-07-01T09:30:00",
        "stored_error": "empty pivot",
        "reported_error": "empty pivot",
    }
    assert service.calls[4]["cache_context"]["signal"]["action"] == "BUY"
    assert service.calls[4]["cache_context"]["context"] == {
        "date": "2024-07-01"
    }

    data = [response["data"] for response in responses]
    assert data[0]["analysis"] == "backtest analysis"
    assert data[1]["suggestion"].startswith('{"suggestions"')
    assert data[1]["suggestions"][0]["suggested_value"] == 10
    assert data[2]["insight"] == "market insight"
    assert data[3]["diagnosis"].startswith("[data]")
    assert data[3]["structured"]["auto_fixable"] is False
    assert data[4]["explanation"] == "signal explanation"
    for item in data:
        assert item["cached"] is False
        assert item["model"] == "deepseek-test"
        assert item["usage"] == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "latency_ms": item["usage"]["latency_ms"],
        }
        assert isinstance(item["usage"]["latency_ms"], float)
        assert item["usage"]["latency_ms"] >= 0

    async def stored_diagnosis() -> str:
        async with aiosqlite.connect(experiment_db) as conn:
            cursor = await conn.execute(
                "SELECT ai_diagnosis FROM experiments WHERE id = 2"
            )
            return (await cursor.fetchone())[0]

    assert asyncio.run(stored_diagnosis()) == data[3]["diagnosis"]


@pytest.mark.parametrize(
    "response",
    [
        "fast_period should be 10",
        '{"suggestions":[{"param_name":"missing","current_value":5,'
        '"suggested_value":10,"reason":"x"}]}',
        '{"suggestions":[{"param_name":"fast_period","current_value":5,'
        '"suggested_value":999,"reason":"x"}]}',
        '{"suggestions":[{"param_name":"fast_period","current_value":20,'
        '"suggested_value":10,"reason":"x"}]}',
    ],
)
def test_suggest_params_rejects_non_contract_model_output(
    api_env, response: str
) -> None:
    service, _ = api_env
    service.responses["suggest-params"] = response

    async def scenario() -> None:
        with pytest.raises(HTTPException) as caught:
            await ai_api.suggest_params(
                ai_api.SuggestParamsBody(
                    strategy_id="ma_cross_v1",
                    current_params={"fast_period": 5, "slow_period": 30},
                ),
                {"id": 7, "is_admin": False},
            )
        assert caught.value.status_code == 502

    asyncio.run(scenario())


def test_diagnosis_rejects_unknown_category_without_persisting(api_env) -> None:
    service, experiment_db = api_env
    service.responses["diagnose-error"] = (
        '{"category":"magic","root_cause":"x","evidence":"y",'
        '"fix_suggestion":"z","auto_fixable":false}'
    )

    async def scenario() -> None:
        with pytest.raises(HTTPException) as caught:
            await ai_api.diagnose_error(
                ai_api.DiagnoseErrorBody(
                    experiment_id=2, error_log="empty pivot"
                ),
                {"id": 7, "is_admin": False},
            )
        assert caught.value.status_code == 502
        async with aiosqlite.connect(experiment_db) as conn:
            cursor = await conn.execute(
                "SELECT ai_diagnosis FROM experiments WHERE id = 2"
            )
            assert (await cursor.fetchone())[0] is None

    asyncio.run(scenario())


def test_diagnosis_rejects_auto_fix_for_non_strategy_category(api_env) -> None:
    service, _ = api_env
    service.responses["diagnose-error"] = (
        '{"category":"data","root_cause":"x","evidence":"y",'
        '"fix_suggestion":"z","auto_fixable":true}'
    )

    async def scenario() -> None:
        with pytest.raises(HTTPException) as caught:
            await ai_api.diagnose_error(
                ai_api.DiagnoseErrorBody(
                    experiment_id=2, error_log="empty pivot"
                ),
                {"id": 7, "is_admin": False},
            )
        assert caught.value.status_code == 502
        assert "strategy_interface/strategy_code" in caught.value.detail

    asyncio.run(scenario())


def test_diagnosis_allows_auto_fix_for_strategy_code() -> None:
    parsed = ai_api._parse_diagnosis(
        '{"category":"strategy_code","root_cause":"接口实现错误",'
        '"evidence":"堆栈指向策略文件","fix_suggestion":"修复方法签名",'
        '"auto_fixable":true}'
    )
    assert parsed["auto_fixable"] is True


def test_industry_exposure_no_positions_and_mapping_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_positions() -> None:
        text, summary = await ai_api._build_industry_exposure([])
        assert text == "当前无持仓"
        assert summary == []

    asyncio.run(no_positions())

    class FailingUniverseManager:
        def __init__(self, source, cache) -> None:
            del source, cache

        async def get_industry_map(self) -> dict[str, str]:
            raise RuntimeError("source unavailable")

    monkeypatch.setattr(ai_api, "UniverseManager", FailingUniverseManager)
    monkeypatch.setattr(ai_api, "AKShareSource", lambda: object())
    monkeypatch.setattr(ai_api, "DataCache", lambda: object())

    async def mapping_failure() -> None:
        text, summary = await ai_api._build_industry_exposure(
            [{"code": "A", "market_value": 100.0}]
        )
        assert summary == [
            {"industry": "未知", "market_value": 100.0, "weight_pct": 100.0}
        ]
        assert '"industry": "未知"' in text

    asyncio.run(mapping_failure())


def test_resource_endpoints_enforce_ownership_before_ai_call(api_env) -> None:
    service, _ = api_env
    other_user = {"id": 7, "is_admin": False}

    async def scenario() -> None:
        requests = [
            ai_api.analyze_backtest(
                ai_api.AnalyzeBacktestBody(experiment_id=3), other_user
            ),
            ai_api.market_insight(
                ai_api.MarketInsightBody(portfolio_id=2), other_user
            ),
            ai_api.diagnose_error(
                ai_api.DiagnoseErrorBody(experiment_id=3, error_log="x"),
                other_user,
            ),
        ]
        for request in requests:
            with pytest.raises(HTTPException) as caught:
                await request
            assert caught.value.status_code == 404

    asyncio.run(scenario())
    assert service.calls == []
