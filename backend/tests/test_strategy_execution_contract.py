from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from backend.core.cost_model import CostModel
from backend.core.engine import BacktestEngine
from backend.core.types import SignalItem
from backend.strategies.base import (
    PortfolioSignalMode,
    StrategyCategory,
    StrategyMetadata,
    StrategyProtocol,
    split_platform_params,
)
from backend.strategies.ml.alpha158_lgb import Alpha158LGBStrategy
from backend.strategies.portfolio.risk_parity import RiskParityStrategy
from backend.strategies.registry import StrategyRegistry
from backend.strategies.technical.ma_cross import MACrossStrategy


TARGET_WEIGHT_STRATEGIES = {
    "alpha158_lgb_v1",
    "alpha158_rank_lgb_v1",
    "alpha158_xgb_v1",
    "alphamaster_gbr_v1",
    "liquidity_factor_v1",
    "low_volatility_v1",
    "lstm_rank_v1",
    "momentum_cross_v1",
    "multi_factor_score_v1",
    "risk_parity_v1",
    "short_reversal_v1",
    "transformer_rank_v1",
}


def _zero_cost_model() -> CostModel:
    return CostModel(
        commission_rate=0.0,
        slippage_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )


def _two_code_pivot(dates: pd.DatetimeIndex) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            ("A", "open"): [10.0] * len(dates),
            ("A", "close"): [10.0] * len(dates),
            ("B", "open"): [10.0] * len(dates),
            ("B", "close"): [10.0] * len(dates),
        },
        index=dates,
    )
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)
    return frame


def test_all_registered_strategies_expose_explicit_signal_mode() -> None:
    registry = StrategyRegistry()
    registry.scan_directory(Path(__file__).parents[1] / "strategies")
    metadata = registry.list_all()

    assert metadata
    assert {
        item.strategy_id
        for item in metadata
        if item.portfolio_signal_mode == PortfolioSignalMode.TARGET_WEIGHTS
    } == TARGET_WEIGHT_STRATEGIES
    assert all(
        item.portfolio_signal_mode
        in {
            PortfolioSignalMode.EVENT_ORDERS,
            PortfolioSignalMode.TARGET_WEIGHTS,
        }
        for item in metadata
    )
    assert all(
        item.portfolio_signal_mode == PortfolioSignalMode.EVENT_ORDERS
        for item in metadata
        if item.category
        in {StrategyCategory.TECHNICAL, StrategyCategory.COMPOSITE}
    )


def test_registry_rejects_invalid_signal_mode() -> None:
    class InvalidModeStrategy(StrategyProtocol):
        @classmethod
        def metadata(cls) -> StrategyMetadata:
            return StrategyMetadata(
                strategy_id="invalid_mode",
                display_name="Invalid",
                version="1",
                category=StrategyCategory.TECHNICAL,
                description="Invalid test double",
                portfolio_signal_mode="calendar_guess",  # type: ignore[arg-type]
            )

        def generate_batch_signals(
            self,
            pivot: pd.DataFrame,
            params: dict,
            start_date: str,
            end_date: str,
        ) -> dict:
            return {}

    with pytest.raises(ValueError, match="portfolio_signal_mode"):
        StrategyRegistry._validate_metadata(
            InvalidModeStrategy,
            InvalidModeStrategy.metadata(),
        )


def test_platform_execution_config_is_reserved_and_fail_closed() -> None:
    strategy_params, execution = split_platform_params(
        {
            "lookback_days": 21,
            "_execution": {
                "initial_capital": 2_000_000,
                "max_positions": 12,
                "lot_size": 200,
                "volume_participation": 0.15,
                "commission_rate": 0.0002,
                "slippage_rate": 0.0005,
                "stamp_duty_rate": 0.0005,
                "min_commission": 3.0,
            },
        }
    )

    assert strategy_params == {"lookback_days": 21}
    assert execution.initial_capital == 2_000_000
    assert execution.max_positions == 12
    assert execution.lot_size == 200
    assert execution.volume_participation == 0.15
    assert execution.commission_rate == 0.0002

    for invalid in (
        {"volume_participation": 0},
        {"lot_size": True},
        {"commission_rate": -0.1},
        {"unknown": 1},
    ):
        with pytest.raises(ValueError):
            split_platform_params({"_execution": invalid})


def test_registry_validates_execution_without_strategy_param_collision() -> None:
    registry = StrategyRegistry()
    registry.scan_directory(Path(__file__).parents[1] / "strategies")
    params = {
        field.name: field.default
        for field in registry.get_metadata("short_reversal_v1").params
    }
    params["_execution"] = {
        "lot_size": 100,
        "volume_participation": 0.1,
    }

    assert registry.validate_params("short_reversal_v1", params) == (True, "")


def test_ml_second_target_batch_sells_previous_top_k() -> None:
    dates = pd.to_datetime(
        ["2024-01-31", "2024-02-01", "2024-02-29", "2024-03-01"]
    )
    mode = Alpha158LGBStrategy.metadata().portfolio_signal_mode.value
    result = BacktestEngine(
        initial_capital=100_000,
        cost_model=_zero_cost_model(),
        start_date="2024-01-31",
        end_date="2024-03-01",
        max_positions=2,
        portfolio_signal_mode=mode,
    ).run(
        {
            "2024-01-31": [SignalItem("A", "BUY", 1.0, 1.0)],
            "2024-02-29": [SignalItem("B", "BUY", 1.0, 1.0)],
        },
        _two_code_pivot(dates),
    )

    assert [(trade.action, trade.code) for trade in result.trade_log] == [
        ("BUY", "A"),
        ("SELL", "A"),
        ("BUY", "B"),
    ]


def test_technical_event_position_survives_month_boundary() -> None:
    dates = pd.to_datetime(
        ["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02"]
    )
    mode = MACrossStrategy.metadata().portfolio_signal_mode.value
    result = BacktestEngine(
        initial_capital=100_000,
        cost_model=_zero_cost_model(),
        start_date="2024-01-30",
        end_date="2024-02-02",
        portfolio_signal_mode=mode,
    ).run(
        {"2024-01-30": [SignalItem("A", "BUY", 1.0, 0.5)]},
        _two_code_pivot(dates),
    )

    assert [(trade.action, trade.code) for trade in result.trade_log] == [
        ("BUY", "A")
    ]
    assert any(
        snapshot.code == "A" and snapshot.date == "2024-02-02"
        for snapshot in result.position_snapshots
    )


def test_risk_parity_target_applies_on_non_month_boundary() -> None:
    dates = pd.to_datetime(["2024-01-09", "2024-01-10", "2024-01-11"])
    mode = RiskParityStrategy.metadata().portfolio_signal_mode.value
    result = BacktestEngine(
        initial_capital=100_000,
        cost_model=_zero_cost_model(),
        start_date="2024-01-09",
        end_date="2024-01-11",
        max_positions=2,
        portfolio_signal_mode=mode,
    ).run(
        {
            "2024-01-09": [SignalItem("A", "BUY", 1.0, 1.0)],
            "2024-01-10": [SignalItem("B", "BUY", 1.0, 1.0)],
        },
        _two_code_pivot(dates),
    )

    assert [(trade.action, trade.code) for trade in result.trade_log] == [
        ("BUY", "A"),
        ("SELL", "A"),
        ("BUY", "B"),
    ]
