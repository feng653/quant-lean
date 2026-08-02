"""Contract tests for the factor, Donchian, ranking, and composite suite."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.types import SignalItem
from backend.core.cost_model import CostModel
from backend.core.engine import BacktestEngine
from backend.strategies.base import (
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    StrategyMode,
    TrainableStrategy,
)
from backend.strategies.composite._signal_perf import (
    merge_on_date,
    signal_daily_returns,
)
from backend.strategies.composite.equal_weight import CompositeEqualStrategy
from backend.strategies.composite.momentum import CompositeMomentumStrategy
from backend.strategies.factor.multi_factor_score import MultiFactorScoreStrategy
from backend.strategies.ml.alpha158_rank_lgb import Alpha158RankLGBStrategy
from backend.strategies.registry import StrategyRegistry
from backend.strategies.technical.donchian_breakout import (
    DonchianBreakoutStrategy,
)
from backend.services.walkforward import run_walk_forward

NEW_IDS = {
    "short_reversal_v1",
    "low_volatility_v1",
    "liquidity_factor_v1",
    "momentum_cross_v1",
    "multi_factor_score_v1",
    "donchian_breakout_v1",
    "alpha158_rank_lgb_v1",
    "composite_equal_v1",
    "composite_riskparity_v1",
    "composite_momentum_v1",
    "composite_regime_v1",
    "composite_research_weighted_v1",
}


@pytest.fixture(scope="module")
def registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    registry._instances.clear()
    registry._classes.clear()
    registry._metadata.clear()
    registry._parent_index.clear()
    count = registry.scan_directory(Path("backend/strategies"))
    assert count == 22
    return registry


@pytest.fixture(scope="module")
def pivot() -> pd.DataFrame:
    dates = pd.bdate_range("2022-01-03", periods=380)
    rng = np.random.default_rng(7)
    data: dict[tuple[str, str], np.ndarray] = {}
    for position in range(8):
        code = f"00000{position + 1}.SZ"
        drift = 0.0001 + position * 0.00012
        returns = rng.normal(drift, 0.006 + position * 0.0004, len(dates))
        close = (20 + position) * np.cumprod(1 + returns)
        if position == 0:
            close[-80:] *= np.linspace(1.0, 1.8, 80)
        open_ = close * (1 + rng.normal(0, 0.001, len(dates)))
        high = np.maximum(open_, close) * 1.003
        low = np.minimum(open_, close) * 0.997
        volume = rng.integers(500_000, 4_000_000, len(dates)).astype(float)
        data[(code, "open")] = open_
        data[(code, "high")] = high
        data[(code, "low")] = low
        data[(code, "close")] = close
        data[(code, "volume")] = volume
        data[(code, "amount")] = volume * close
    frame = pd.DataFrame(data, index=dates)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)
    return frame


def _assert_signal_contract(signals: dict[str, list[SignalItem]]) -> None:
    assert isinstance(signals, dict)
    assert signals
    for date, items in signals.items():
        assert pd.Timestamp(date).strftime("%Y-%m-%d") == date
        assert isinstance(items, list)
        for item in items:
            assert item.action in {"BUY", "SELL", "HOLD"}
            assert np.isfinite(item.score)
            assert np.isfinite(item.weight)
            assert item.code


def test_registry_has_exactly_22_strategies(registry: StrategyRegistry) -> None:
    registered = {metadata.strategy_id for metadata in registry.list_all()}
    assert len(registered) == 22
    assert NEW_IDS <= registered


@pytest.mark.parametrize("strategy_id", sorted(NEW_IDS))
def test_new_strategy_metadata_and_defaults_validate(
    registry: StrategyRegistry, strategy_id: str
) -> None:
    metadata = registry.get_metadata(strategy_id)
    defaults = {field.name: field.default for field in metadata.params}
    valid, message = registry.validate_params(strategy_id, defaults)
    assert valid, message
    assert metadata.strategy_id == strategy_id
    assert metadata.version
    assert metadata.description
    assert metadata.supported_modes
    if strategy_id == "alpha158_rank_lgb_v1":
        assert metadata.category == StrategyCategory.ML
        assert metadata.requires_training
        assert metadata.retrain_frequency == RetrainFrequency.MONTHLY
    else:
        assert not metadata.requires_training
        assert metadata.retrain_frequency == RetrainFrequency.NEVER


@pytest.mark.parametrize(
    "strategy_id",
    [
        "short_reversal_v1",
        "low_volatility_v1",
        "liquidity_factor_v1",
        "momentum_cross_v1",
        "multi_factor_score_v1",
        "donchian_breakout_v1",
        "composite_equal_v1",
        "composite_riskparity_v1",
        "composite_momentum_v1",
        "composite_regime_v1",
        "composite_research_weighted_v1",
    ],
)
def test_rule_strategy_signal_contract(
    registry: StrategyRegistry, pivot: pd.DataFrame, strategy_id: str
) -> None:
    strategy = registry.create_strategy(strategy_id)
    metadata = strategy.metadata()
    params = {field.name: field.default for field in metadata.params}
    if strategy_id == "donchian_breakout_v1":
        params = {"entry_period": 20, "exit_period": 10}
    signals = strategy.generate_batch_signals(
        pivot, params, str(pivot.index[260].date()), str(pivot.index[-1].date())
    )
    _assert_signal_contract(signals)


def test_factor_signals_are_shifted_one_day(
    registry: StrategyRegistry, pivot: pd.DataFrame
) -> None:
    strategy = registry.create_strategy("short_reversal_v1")
    params = {"lookback_days": 21, "top_k_pct": 0.25}
    target = (
        pivot.index[300:]
        .to_series()
        .groupby(pivot.index[300:].to_period("M"))
        .last()
        .iloc[0]
    )
    original = strategy.generate_batch_signals(
        pivot, params, str(target.date()), str(target.date())
    )
    mutated = pivot.copy()
    mutated.loc[target, ("000001.SZ", "close")] *= 100
    changed = strategy.generate_batch_signals(
        mutated, params, str(target.date()), str(target.date())
    )
    assert original == changed


def test_composites_reject_recursion_and_unknown_children(
    registry: StrategyRegistry,
) -> None:
    strategy = registry.create_strategy("composite_equal_v1")
    assert strategy.validate_params(
        {"sub_strategy_ids": "composite_equal_v1"}
    ) == (False, "组合策略不能递归引用自身")
    valid, message = strategy.validate_params({"sub_strategy_ids": "missing_v1"})
    assert not valid
    assert "未知子策略" in message
    valid, message = strategy.validate_params(
        {"sub_strategy_ids": "composite_momentum_v1"}
    )
    assert not valid
    assert "禁止嵌套组合策略" in message
    valid, message = strategy.validate_params(
        {"sub_strategy_ids": "lstm_rank_v1"}
    )
    assert not valid
    assert "不支持需要训练" in message


def test_alpha158_rank_interface_without_real_training(
    registry: StrategyRegistry,
    pivot: pd.DataFrame,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = registry.create_strategy("alpha158_rank_lgb_v1")
    params = {field.name: field.default for field in strategy.metadata().params}
    strategy.prepare(pivot, params)

    class FakeRanker:
        def predict(self, values: np.ndarray) -> np.ndarray:
            return values[:, :3].mean(axis=1)

    scores = strategy.predict_scores(FakeRanker(), pivot, params, pivot.index[-1])
    selected = strategy.select_signals(scores, params, pivot.index[-1].strftime("%Y-%m-%d"))
    assert scores
    assert selected
    assert all(item.action == "BUY" for item in selected)

    def fake_walk_forward(instance, frame, run_params, start, end):
        del frame, start
        day_scores = instance.predict_scores(
            FakeRanker(), pivot, run_params, pd.Timestamp(end)
        )
        return SimpleNamespace(
            signals={end: instance.select_signals(day_scores, run_params, end)}
        )

    monkeypatch.setattr(
        "backend.services.walkforward.run_walk_forward", fake_walk_forward
    )
    signals = strategy.generate_batch_signals(
        pivot,
        params,
        str(pivot.index[-2].date()),
        str(pivot.index[-1].date()),
    )
    _assert_signal_contract(signals)


def test_alpha158_rank_builds_integer_relevance_labels(
    pivot: pd.DataFrame,
) -> None:
    strategy = Alpha158RankLGBStrategy()
    params = {
        field.name: field.default
        for field in strategy.metadata().params
    }
    strategy.prepare(pivot, params)
    assert strategy._factor_df is not None

    features, labels, groups = strategy._build_rank_matrix(
        strategy._factor_df,
        pivot,
        str(pivot.index[100].date()),
        str(pivot.index[-22].date()),
        horizon=21,
        minimum_samples=2,
    )

    assert len(features) == len(labels) == sum(groups)
    assert labels.dtype == np.dtype("int64")
    assert set(labels) <= {0, 1, 2, 3}


def test_month_end_factor_signals_rebalance_multiple_stocks_on_month_first() -> None:
    dates = pd.bdate_range("2024-01-01", "2024-03-29")
    data: dict[tuple[str, str], np.ndarray] = {}
    for position, code in enumerate(("A", "B", "C", "D")):
        returns = np.full(len(dates), 0.0005 * (position + 1))
        close = 20.0 * np.cumprod(1 + returns)
        data[(code, "open")] = close
        data[(code, "close")] = close
    frame = pd.DataFrame(data, index=dates)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)
    strategy = registry_strategy("short_reversal_v1")
    signals = strategy.generate_batch_signals(
        frame,
        {"lookback_days": 5, "top_k_pct": 0.5},
        "2024-01-15",
        "2024-03-29",
    )
    assert "2024-01-31" in signals
    assert "2024-02-29" in signals
    assert "2024-02-01" not in signals
    assert "2024-01-12" in signals

    result = BacktestEngine(
        1_000_000,
        CostModel(),
        "2024-01-15",
        "2024-03-29",
        max_positions=4,
        rebalance_mode="monthly_liquidate_compat",
    ).run(signals, frame, "short_reversal_v1")
    initial_buys = [
        trade
        for trade in result.trade_log
        if trade.date == "2024-01-15" and trade.action == "BUY"
    ]
    assert len(initial_buys) == 2
    february_buys = [
        trade
        for trade in result.trade_log
        if trade.date == "2024-02-01" and trade.action == "BUY"
    ]
    assert len(february_buys) == 2
    assert {trade.code for trade in february_buys} == {"A", "B"}


class _FakeTrainableStrategy(TrainableStrategy):
    frequency = RetrainFrequency.MONTHLY

    def __init__(self) -> None:
        super().__init__()
        self.prediction_dates: list[pd.Timestamp] = []

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="fake_trainable",
            display_name="Fake",
            version="1",
            category=StrategyCategory.ML,
            description="test",
            supported_modes=[StrategyMode.BATCH],
            requires_training=True,
            retrain_frequency=cls.frequency,
        )

    def fit(self, pivot, params, train_start, train_end):
        del pivot, params, train_start, train_end
        self._model = object()
        return self._model

    def predict_scores(self, model, pivot, params, as_of_date):
        del model, pivot, params
        self.prediction_dates.append(as_of_date)
        return {"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0}


def test_walkforward_monthly_signals_use_prior_observable_session() -> None:
    dates = pd.bdate_range("2022-01-03", "2024-03-29")
    frame = pd.DataFrame(
        {
            ("A", "close"): np.linspace(10, 20, len(dates)),
            ("B", "close"): np.linspace(11, 19, len(dates)),
            ("C", "close"): np.linspace(12, 18, len(dates)),
            ("D", "close"): np.linspace(13, 17, len(dates)),
        },
        index=dates,
    )
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)
    strategy = _FakeTrainableStrategy()
    result = run_walk_forward(
        strategy,
        frame,
        {
            "top_k_pct": 0.5,
            "min_train_months": 1,
            "validation_months": 0,
        },
        "2024-02-01",
        "2024-03-29",
    )
    assert set(result.signals) == {"2024-01-31", "2024-02-29"}
    assert [cycle.pred_date for cycle in result.cycles] == [
        "2024-02-01",
        "2024-03-01",
    ]
    assert strategy.prediction_dates == [
        pd.Timestamp("2024-01-31"),
        pd.Timestamp("2024-02-29"),
    ]

    engine_frame = frame.loc["2024-01-31":"2024-03-29"].copy()
    for code in ("A", "B", "C", "D"):
        engine_frame[(code, "open")] = engine_frame[(code, "close")]
    engine_frame = engine_frame.sort_index(axis=1)
    backtest = BacktestEngine(
        1_000_000,
        CostModel(),
        "2024-02-01",
        "2024-03-29",
        max_positions=4,
        rebalance_mode="monthly_liquidate_compat",
    ).run(result.signals, engine_frame, "fake_trainable")
    for month_first in ("2024-02-01", "2024-03-01"):
        buys = [
            trade
            for trade in backtest.trade_log
            if trade.date == month_first and trade.action == "BUY"
        ]
        assert len(buys) == 2


def test_non_monthly_prediction_timestamp_is_unchanged() -> None:
    strategy = _FakeTrainableStrategy()
    _FakeTrainableStrategy.prediction_frequency = RetrainFrequency.DAILY
    dates = pd.bdate_range("2024-01-02", periods=3)
    frame = pd.DataFrame({"A": [1.0, 2.0, 3.0]}, index=dates)
    assert strategy.signal_decision_date(frame, dates[1]) == dates[1]
    _FakeTrainableStrategy.prediction_frequency = RetrainFrequency.MONTHLY


def test_train_once_lifecycle_keeps_monthly_pre_open_decision_date() -> None:
    strategy = _FakeTrainableStrategy()
    _FakeTrainableStrategy.frequency = RetrainFrequency.NEVER
    _FakeTrainableStrategy.prediction_frequency = RetrainFrequency.MONTHLY
    dates = pd.bdate_range("2024-01-31", periods=3)
    frame = pd.DataFrame({"A": [1.0, 2.0, 3.0]}, index=dates)

    assert strategy.signal_decision_date(frame, dates[1]) == dates[0]

    _FakeTrainableStrategy.frequency = RetrainFrequency.MONTHLY


def registry_strategy(strategy_id: str):
    registry = StrategyRegistry()
    if not registry.list_all():
        registry.scan_directory(Path("backend/strategies"))
    return registry.create_strategy(strategy_id)


def test_donchian_event_position_survives_months_without_synthetic_buys() -> None:
    dates = pd.bdate_range("2024-01-01", "2024-03-29")
    close = np.arange(100.0, 100.0 + len(dates))
    frame = pd.DataFrame(
        {
            ("A", "open"): close,
            ("A", "high"): close,
            ("A", "low"): close,
            ("A", "close"): close,
        },
        index=dates,
    )
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)
    signals = DonchianBreakoutStrategy().generate_batch_signals(
        frame,
        {"entry_period": 5, "exit_period": 2},
        "2024-01-08",
        "2024-03-29",
    )
    assert list(signals) == ["2024-01-09"]

    result = BacktestEngine(
        1_000_000,
        CostModel(),
        "2024-01-08",
        "2024-03-29",
    ).run(signals, frame, "donchian_breakout_v1")
    assert [trade.action for trade in result.trade_log] == ["BUY"]
    assert any(
        snapshot.date == "2024-03-29" and snapshot.code == "A"
        for snapshot in result.position_snapshots
    )


def test_paper_returns_do_not_include_rebalance_day_close() -> None:
    dates = pd.bdate_range("2024-01-01", periods=5)
    signals = {
        dates[0].strftime("%Y-%m-%d"): [SignalItem("A", "BUY", 1.0, 1.0)]
    }

    def make_frame(last_close: float) -> pd.DataFrame:
        frame = pd.DataFrame(
            {("A", "close"): [100.0, 101.0, 102.0, 103.0, last_close]},
            index=dates,
        )
        frame.columns = pd.MultiIndex.from_tuples(frame.columns)
        return frame

    first = signal_daily_returns(signals, make_frame(104.0))
    mutated = signal_daily_returns(signals, make_frame(1000.0))
    pd.testing.assert_series_equal(
        first.loc[first.index < dates[-1]],
        mutated.loc[mutated.index < dates[-1]],
    )


def test_composite_opposite_signals_net_and_engine_makes_no_trade() -> None:
    date = "2024-01-02"
    buy = {date: [SignalItem("A", "BUY", 1.0, 1.0)]}
    sell = {date: [SignalItem("A", "SELL", 1.0, 0.0)]}
    assert CompositeEqualStrategy()._merge_signals(
        [buy, sell], [0.5, 0.5]
    )[date] == []
    assert merge_on_date(date, [buy, sell], [0.5, 0.5]) == []

    dates = pd.bdate_range("2024-01-02", periods=3)
    frame = pd.DataFrame(
        {
            ("A", "open"): [10.0, 10.0, 10.0],
            ("A", "close"): [10.0, 10.0, 10.0],
        },
        index=dates,
    )
    frame.columns = pd.MultiIndex.from_tuples(frame.columns)
    result = BacktestEngine(
        100_000, CostModel(), str(dates[0].date()), str(dates[-1].date())
    ).run({date: []}, frame, "composite_equal_v1")
    assert result.trade_log == []


def test_dynamic_composite_generates_warmup_history_and_filters_output(
    pivot: pd.DataFrame,
) -> None:
    strategy = CompositeMomentumStrategy()
    start = pivot.index[-45]
    end = pivot.index[-1]
    params = {
        "sub_strategy_ids": "short_reversal_v1,low_volatility_v1",
        "lookback_days": 63,
    }
    _, child_signals = strategy._run_children(
        pivot,
        params,
        str(start.date()),
        str(end.date()),
        warmup_days=63,
    )
    assert any(
        pd.Timestamp(date_str) < start
        for signals in child_signals
        for date_str in signals
    )
    output = strategy.generate_batch_signals(
        pivot, params, str(start.date()), str(end.date())
    )
    assert output
    assert all(start <= pd.Timestamp(date_str) <= end for date_str in output)


def test_multi_factor_does_not_compute_disabled_factors(
    pivot: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args, **kwargs):
        del args, kwargs
        raise AssertionError("disabled factor was evaluated")

    monkeypatch.setattr(
        "backend.strategies.factor.multi_factor_score.low_volatility_raw", fail
    )
    monkeypatch.setattr(
        "backend.strategies.factor.multi_factor_score.liquidity_raw", fail
    )
    monkeypatch.setattr(
        "backend.strategies.factor.multi_factor_score.momentum_raw", fail
    )
    signals = MultiFactorScoreStrategy().generate_batch_signals(
        pivot,
        {
            "use_short_reversal": True,
            "use_low_volatility": False,
            "use_liquidity": False,
            "use_momentum": False,
            "short_reversal_weight": 1.0,
            "top_k_pct": 0.25,
        },
        str(pivot.index[-45].date()),
        str(pivot.index[-1].date()),
    )
    assert signals


def test_alpha158_rank_exposes_common_training_controls(
    registry: StrategyRegistry,
) -> None:
    names = {
        field.name
        for field in registry.get_metadata("alpha158_rank_lgb_v1").params
    }
    assert {
        "window_mode",
        "rolling_train_months",
        "embargo_days",
        "validation_months",
        "retrain_months",
        "min_train_months",
    } <= names


def test_alpha158_rank_fit_records_training_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = Alpha158RankLGBStrategy()
    strategy._factor_df = pd.DataFrame()
    strategy._factor_source = object()
    monkeypatch.setattr(strategy, "prepare", lambda pivot, params: None)
    monkeypatch.setattr(
        strategy,
        "_build_rank_matrix",
        lambda *args: (
            np.ones((120, 3), dtype=float),
            np.tile(np.arange(4), 30),
            [4] * 30,
        ),
    )

    class FakeRanker:
        def fit(self, features, labels, group):
            assert features.shape == (120, 3)
            assert labels.shape == (120,)
            assert group == [4] * 30

    monkeypatch.setattr(
        "backend.strategies.ml.alpha158_rank_lgb._load_lightgbm",
        lambda: SimpleNamespace(LGBMRanker=lambda **kwargs: FakeRanker()),
    )
    strategy.fit(
        pd.DataFrame(),
        {},
        "2023-01-01",
        "2023-12-31",
    )
    assert strategy._last_train_metrics == {
        "n_samples": 120,
        "n_features": 3,
        "n_groups": 30,
        "model_type": "LightGBM LambdaRank",
    }


@pytest.mark.parametrize(
    ("strategy_id", "bad_params"),
    [
        ("short_reversal_v1", {"lookback_days": 1}),
        ("low_volatility_v1", {"vol_method": "bad"}),
        ("liquidity_factor_v1", {"method": "bad"}),
        ("momentum_cross_v1", {"lookback_months": 2, "skip_months": 2}),
        (
            "multi_factor_score_v1",
            {
                "use_short_reversal": False,
                "use_low_volatility": False,
                "use_liquidity": False,
                "use_momentum": False,
            },
        ),
        ("donchian_breakout_v1", {"entry_period": 10, "exit_period": 20}),
        ("alpha158_rank_lgb_v1", {"top_k_pct": 0}),
        ("composite_riskparity_v1", {"lookback_days": 1}),
        ("composite_momentum_v1", {"lookback_days": 1}),
        ("composite_regime_v1", {"regime_ma_days": 1}),
    ],
)
def test_invalid_parameters_are_rejected(
    registry: StrategyRegistry, strategy_id: str, bad_params: dict
) -> None:
    valid, _ = registry.create_strategy(strategy_id).validate_params(bad_params)
    assert not valid
