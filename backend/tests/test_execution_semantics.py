"""Execution Engine V2 contract tests."""

from __future__ import annotations

import pandas as pd
import pytest

from backend.core.cost_model import CostModel
from backend.core.engine import BacktestEngine, ExecutionConstraints
from backend.core.metrics import compute_all_metrics
from backend.core.types import SignalItem, TradeRecord


CODE = "000001.SZ"


def _zero_cost_model() -> CostModel:
    return CostModel(
        commission_rate=0.0,
        slippage_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )


def _pivot(
    dates: pd.DatetimeIndex,
    *,
    opens: list[float],
    closes: list[float] | None = None,
    volumes: list[float] | None = None,
) -> pd.DataFrame:
    values: dict[tuple[str, str], list[float]] = {
        (CODE, "open"): opens,
        (CODE, "close"): closes or opens,
    }
    if volumes is not None:
        values[(CODE, "volume")] = volumes
    frame = pd.DataFrame(values, index=dates)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)
    return frame


def test_position_survives_month_boundary_without_signal() -> None:
    dates = pd.to_datetime(
        ["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02"]
    )
    result = BacktestEngine(
        initial_capital=100_000,
        cost_model=_zero_cost_model(),
        start_date="2024-01-30",
        end_date="2024-02-02",
    ).run(
        {
            "2024-01-30": [
                SignalItem(CODE, "BUY", score=1.0, weight=0.5)
            ]
        },
        _pivot(dates, opens=[10.0] * 4, volumes=[100_000.0] * 4),
        strategy_id="hold_across_month",
    )

    assert [trade.action for trade in result.trade_log] == ["BUY"]
    final_positions = [
        snapshot
        for snapshot in result.position_snapshots
        if snapshot.date == "2024-02-02"
    ]
    assert len(final_positions) == 1
    assert final_positions[0].shares == result.trade_log[0].shares


def test_explicit_sell_signal_closes_position() -> None:
    dates = pd.to_datetime(
        ["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02"]
    )
    result = BacktestEngine(
        initial_capital=100_000,
        cost_model=_zero_cost_model(),
        start_date="2024-01-30",
        end_date="2024-02-02",
    ).run(
        {
            "2024-01-30": [
                SignalItem(CODE, "BUY", score=1.0, weight=0.5)
            ],
            "2024-02-01": [
                SignalItem(CODE, "SELL", score=1.0, weight=0.0)
            ],
        },
        _pivot(dates, opens=[10.0, 10.0, 10.0, 11.0]),
        strategy_id="explicit_exit",
    )

    assert [trade.action for trade in result.trade_log] == ["BUY", "SELL"]
    assert [trade.date for trade in result.trade_log] == [
        "2024-01-31",
        "2024-02-02",
    ]
    assert not [
        snapshot
        for snapshot in result.position_snapshots
        if snapshot.date == "2024-02-02"
    ]


def test_explicit_raw_execution_role_controls_fills_and_valuation() -> None:
    dates = pd.to_datetime(["2024-01-01", "2024-01-02"])
    research = _pivot(
        dates,
        opens=[100.0, 120.0],
        closes=[100.0, 125.0],
        volumes=[100_000.0, 100_000.0],
    )
    raw = _pivot(
        dates,
        opens=[10.0, 12.0],
        closes=[10.0, 12.5],
        volumes=[100_000.0, 100_000.0],
    )

    result = BacktestEngine(
        initial_capital=100_000,
        cost_model=_zero_cost_model(),
        start_date="2024-01-02",
        end_date="2024-01-02",
    ).run(
        {
            "2024-01-01": [
                SignalItem(CODE, "BUY", score=1.0, weight=1.0)
            ]
        },
        research,
        execution_pivot=raw,
    )

    assert result.trade_log[0].price == 12.0
    assert result.position_snapshots[-1].close_price == 12.5


def test_target_weight_batch_replaces_stale_holdings_without_date_heuristic() -> None:
    dates = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])
    pivot = pd.DataFrame(
        {
            ("A", "open"): [10.0, 10.0, 10.0],
            ("A", "close"): [10.0, 10.0, 10.0],
            ("B", "open"): [10.0, 10.0, 10.0],
            ("B", "close"): [10.0, 10.0, 10.0],
        },
        index=dates,
    )
    pivot.columns = pd.MultiIndex.from_tuples(pivot.columns)
    signals = {
        "2024-01-01": [
            SignalItem("A", "BUY", score=1.0, weight=1.0)
        ],
        "2024-01-02": [
            SignalItem("B", "BUY", score=1.0, weight=1.0)
        ],
    }

    event_result = BacktestEngine(
        initial_capital=100_000,
        cost_model=_zero_cost_model(),
        start_date="2024-01-01",
        end_date="2024-01-03",
        max_positions=2,
    ).run(signals, pivot)
    target_result = BacktestEngine(
        initial_capital=100_000,
        cost_model=_zero_cost_model(),
        start_date="2024-01-01",
        end_date="2024-01-03",
        max_positions=2,
        portfolio_signal_mode="target_weights",
    ).run(signals, pivot)

    assert [trade.action for trade in event_result.trade_log] == ["BUY"]
    assert event_result.trade_log[0].code == "A"
    assert [
        (trade.action, trade.code)
        for trade in target_result.trade_log
    ] == [("BUY", "A"), ("SELL", "A"), ("BUY", "B")]
    target_final_codes = {
        snapshot.code
        for snapshot in target_result.position_snapshots
        if snapshot.date == "2024-01-03"
    }
    assert target_final_codes == {"B"}


def test_initial_capital_baseline_captures_first_session_cost() -> None:
    dates = pd.to_datetime(["2024-01-01", "2024-01-02"])
    cost_model = CostModel(
        commission_rate=0.001,
        slippage_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    result = BacktestEngine(
        initial_capital=100_000,
        cost_model=cost_model,
        start_date="2024-01-02",
        end_date="2024-01-02",
    ).run(
        {
            "2024-01-01": [
                SignalItem(CODE, "BUY", score=1.0, weight=1.0)
            ]
        },
        _pivot(dates, opens=[10.0, 10.0], closes=[10.0, 10.0]),
    )

    assert len(result.equity_curve) == 2
    assert result.equity_curve["equity"].iloc[0] == 100_000
    assert result.equity_curve["equity"].iloc[1] < 100_000
    metrics = compute_all_metrics(result.equity_curve)
    assert metrics["initial_equity"] == 100_000
    assert metrics["total_return"] < 0


def test_fifo_metrics_can_turn_gross_profit_into_net_loss() -> None:
    trades = [
        TradeRecord(
            date="2024-01-02",
            code=CODE,
            action="BUY",
            price=10.0,
            shares=100,
            amount=1_000.0,
            cost=10.0,
        ),
        TradeRecord(
            date="2024-01-03",
            code=CODE,
            action="SELL",
            price=10.05,
            shares=100,
            amount=1_005.0,
            cost=10.0,
        ),
    ]
    equity = pd.DataFrame(
        {"equity": [1_000.0, 985.0]},
        index=pd.to_datetime(["2024-01-01", "2024-01-03"]),
    )

    metrics = compute_all_metrics(equity, trade_log=trades)

    assert metrics["total_trades"] == 1
    assert metrics["win_rate"] == 0.0
    assert metrics["avg_loss"] == pytest.approx(-15.0)
    assert metrics["profit_factor"] == 0.0
    assert metrics["avg_trade_return"] == pytest.approx(-15.0 / 1_010.0)


def test_volume_participation_caps_and_partially_fills_orders() -> None:
    dates = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])
    result = BacktestEngine(
        initial_capital=100_000,
        cost_model=_zero_cost_model(),
        start_date="2024-01-01",
        end_date="2024-01-03",
        execution_constraints=ExecutionConstraints(
            volume_participation=0.10,
        ),
    ).run(
        {
            "2024-01-01": [
                SignalItem(CODE, "BUY", score=1.0, weight=1.0)
            ],
            "2024-01-02": [
                SignalItem(CODE, "SELL", score=1.0, weight=0.0)
            ],
        },
        _pivot(
            dates,
            opens=[10.0, 10.0, 10.0],
            volumes=[10_000.0, 10_000.0, 5_000.0],
        ),
    )

    assert [(trade.action, trade.shares) for trade in result.trade_log] == [
        ("BUY", 1_000),
        ("SELL", 500),
    ]
    assert all(trade.shares % 100 == 0 for trade in result.trade_log)
    final_position = [
        snapshot
        for snapshot in result.position_snapshots
        if snapshot.date == "2024-01-03"
    ]
    assert len(final_position) == 1
    assert final_position[0].shares == 500


def test_zero_volume_session_is_not_executable() -> None:
    dates = pd.to_datetime(["2024-01-01", "2024-01-02"])
    result = BacktestEngine(
        initial_capital=100_000,
        cost_model=_zero_cost_model(),
        start_date="2024-01-01",
        end_date="2024-01-02",
    ).run(
        {
            "2024-01-01": [
                SignalItem(CODE, "BUY", score=1.0, weight=1.0)
            ]
        },
        _pivot(
            dates,
            opens=[10.0, 10.0],
            volumes=[10_000.0, 0.0],
        ),
    )

    assert result.trade_log == []
