from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from backend.services.walkforward import run_walk_forward
from backend.strategies.base import (
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    TrainableStrategy,
)
from backend.strategies.ml.alpha158_lgb import Alpha158LGBStrategy
from backend.strategies.ml.alpha158_xgb import Alpha158XGBStrategy
from backend.strategies.ml.lstm_rank import LSTMRankStrategy
from backend.strategies.ml.transformer_rank import TransformerRankStrategy
from backend.strategies.registry import StrategyRegistry


class RecordingStrategy(TrainableStrategy):
    frequency = RetrainFrequency.MONTHLY

    def __init__(self) -> None:
        super().__init__()
        self.fit_calls: list[tuple[str, str]] = []

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id=f"recording_{cls.frequency.value}",
            display_name="Recording strategy",
            version="1",
            category=StrategyCategory.ML,
            description="Test double",
            requires_training=True,
            retrain_frequency=cls.frequency,
        )

    def label_horizon_days(self, params: dict) -> int:
        return 2

    def fit(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
    ) -> Any:
        self.fit_calls.append((train_start, train_end))
        rows = pivot.loc[train_start:train_end]
        model = {"fit_number": len(self.fit_calls)}
        self._model = model
        self.record_train_metrics(
            n_samples=len(rows) * len(pivot.columns),
            n_features=3,
            model_type="recording",
        )
        return model

    def predict_scores(
        self,
        model: Any,
        pivot: pd.DataFrame,
        params: dict,
        as_of_date: pd.Timestamp,
    ) -> dict[str, float]:
        return {str(code): float(index + 1) for index, code in enumerate(pivot.columns)}


class TrainOnceRecordingStrategy(RecordingStrategy):
    frequency = RetrainFrequency.NEVER


@pytest.fixture
def market_data() -> pd.DataFrame:
    dates = pd.bdate_range("2019-01-02", "2023-03-31")
    time_index = np.arange(len(dates), dtype=float)
    return pd.DataFrame(
        {
            f"{code_index:06d}": 10.0
            * np.exp(
                (0.0005 + code_index * 0.00001) * time_index
            )
            for code_index in range(1, 21)
        },
        index=dates,
    )


def _periodic_params(**overrides: Any) -> dict:
    params = {
        "min_train_months": 6,
        "retrain_months": 1,
        "window_mode": "expanding",
        "rolling_train_months": 12,
        "embargo_days": 0,
        "validation_months": 1,
        "top_k_pct": 0.5,
    }
    params.update(overrides)
    return params


def test_periodic_mode_infers_training_range_and_reports_real_metrics(
    market_data: pd.DataFrame,
) -> None:
    strategy = RecordingStrategy()

    result = run_walk_forward(
        strategy,
        market_data,
        _periodic_params(),
        "2023-01-01",
        "2023-03-31",
    )

    assert len(strategy.fit_calls) == 3
    assert result.last_window == strategy.fit_calls[-1]
    assert all(cycle.retrained for cycle in result.cycles)
    assert all((cycle.n_train_samples or 0) > 0 for cycle in result.cycles)
    assert all(cycle.n_train_features == 3 for cycle in result.cycles)
    assert result.elapsed_seconds >= 0
    assert result.signals


def test_train_once_uses_fixed_window_and_fits_exactly_once(
    market_data: pd.DataFrame,
) -> None:
    strategy = TrainOnceRecordingStrategy()
    params = _periodic_params(
        window_mode="fixed",
        _train_start="2020-01-02",
        _train_end="2021-12-31",
    )

    result = run_walk_forward(
        strategy,
        market_data,
        params,
        "2023-01-01",
        "2023-03-31",
    )

    assert strategy.fit_calls == [("2020-01-02", "2021-11-26")]
    assert sum(cycle.retrained for cycle in result.cycles) == 1
    assert {(cycle.train_start, cycle.train_end) for cycle in result.cycles} == {
        ("2020-01-02", "2021-11-26")
    }
    assert {(cycle.validation_start, cycle.validation_end) for cycle in result.cycles} == {
        ("2021-12-01", "2021-12-31")
    }


def test_loaded_train_once_model_is_reused_without_fit(
    market_data: pd.DataFrame,
) -> None:
    strategy = TrainOnceRecordingStrategy()
    strategy._model = {"deployed": True}

    result = run_walk_forward(
        strategy,
        market_data,
        _periodic_params(
            window_mode="fixed",
            _train_start="2020-01-02",
            _train_end="2021-12-31",
        ),
        "2023-01-01",
        "2023-03-31",
    )

    assert strategy.fit_calls == []
    assert not any(cycle.retrained for cycle in result.cycles)
    assert result.signals


def test_embargo_and_label_horizon_are_purged_from_training_boundary(
    market_data: pd.DataFrame,
) -> None:
    strategy = RecordingStrategy()
    result = run_walk_forward(
        strategy,
        market_data,
        _periodic_params(embargo_days=3),
        "2023-01-01",
        "2023-01-31",
    )

    cycle = result.cycles[0]
    validation_end_position = market_data.index.get_loc(pd.Timestamp(cycle.validation_end))
    pred_position = market_data.index.get_loc(pd.Timestamp(cycle.pred_date))
    assert pred_position - validation_end_position - 1 == 2 + 3
    assert pd.Timestamp(cycle.train_end) < pd.Timestamp(cycle.validation_start)
    assert cycle.label_horizon_days == 2
    assert cycle.embargo_days == 3


def test_rolling_window_moves_start_for_each_retrain(
    market_data: pd.DataFrame,
) -> None:
    strategy = RecordingStrategy()
    result = run_walk_forward(
        strategy,
        market_data,
        _periodic_params(
            window_mode="rolling",
            rolling_train_months=12,
        ),
        "2023-01-01",
        "2023-03-31",
    )

    starts = [pd.Timestamp(cycle.train_start) for cycle in result.cycles]
    ends = [pd.Timestamp(cycle.train_end) for cycle in result.cycles]
    assert starts == sorted(starts)
    assert len(set(starts)) == 3
    assert len(set(ends)) == 3
    assert all(
        11 <= (end.to_period("M") - start.to_period("M")).n <= 12
        for start, end in zip(starts, ends)
    )


def test_train_once_requires_explicit_fixed_training_dates(
    market_data: pd.DataFrame,
) -> None:
    with pytest.raises(ValueError, match="_train_start"):
        run_walk_forward(
            TrainOnceRecordingStrategy(),
            market_data,
            _periodic_params(window_mode="fixed"),
            "2023-01-01",
            "2023-01-31",
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"window_mode": "unknown"}, "window_mode"),
        ({"embargo_days": -1}, "embargo_days"),
        ({"rolling_train_months": 0}, "rolling_train_months"),
        (
            {
                "window_mode": "rolling",
                "rolling_train_months": 3,
                "min_train_months": 6,
            },
            "rolling_train_months",
        ),
    ],
)
def test_invalid_training_parameters_fail_clearly(
    market_data: pd.DataFrame,
    overrides: dict,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        run_walk_forward(
            RecordingStrategy(),
            market_data,
            _periodic_params(**overrides),
            "2023-01-01",
            "2023-01-31",
        )


def test_empty_or_missing_test_data_fails_clearly(
    market_data: pd.DataFrame,
) -> None:
    with pytest.raises(RuntimeError, match="行情数据为空"):
        run_walk_forward(
            RecordingStrategy(),
            market_data.iloc[0:0],
            _periodic_params(),
            "2023-01-01",
            "2023-01-31",
        )

    with pytest.raises(RuntimeError, match="无可用行情数据"):
        run_walk_forward(
            RecordingStrategy(),
            market_data,
            _periodic_params(),
            "2025-01-01",
            "2025-01-31",
        )


def test_ml_metadata_exposes_consistent_platform_controls_and_modes() -> None:
    periodic = [Alpha158LGBStrategy.metadata(), Alpha158XGBStrategy.metadata()]
    train_once = [LSTMRankStrategy.metadata(), TransformerRankStrategy.metadata()]
    required_names = {
        "window_mode",
        "rolling_train_months",
        "embargo_days",
        "validation_months",
    }

    for metadata in periodic + train_once:
        names = [field.name for field in metadata.params]
        assert required_names <= set(names)
        assert len(names) == len(set(names))

    assert all(metadata.retrain_frequency == RetrainFrequency.MONTHLY for metadata in periodic)
    assert all(metadata.retrain_frequency == RetrainFrequency.NEVER for metadata in train_once)


@pytest.fixture(scope="module")
def strategy_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.scan_directory(Path(__file__).parents[1] / "strategies")
    return registry


def _metadata_defaults(registry: StrategyRegistry, strategy_id: str) -> dict:
    return {
        field.name: field.default
        for field in registry.get_metadata(strategy_id).params
        if field.default is not None
    }


def test_registry_rejects_rolling_window_shorter_than_minimum(
    strategy_registry: StrategyRegistry,
) -> None:
    params = _metadata_defaults(strategy_registry, "alpha158_lgb_v1")
    params.update(
        window_mode="rolling",
        rolling_train_months=6,
        min_train_months=12,
    )

    valid, message = strategy_registry.validate_params(
        "alpha158_lgb_v1", params
    )

    assert valid is False
    assert message == "rolling_train_months 不能小于 min_train_months"


def test_registry_accepts_valid_periodic_rolling_window(
    strategy_registry: StrategyRegistry,
) -> None:
    params = _metadata_defaults(strategy_registry, "alpha158_lgb_v1")
    params.update(
        window_mode="rolling",
        rolling_train_months=24,
        min_train_months=12,
    )

    assert strategy_registry.validate_params("alpha158_lgb_v1", params) == (
        True,
        "",
    )


def test_registry_rejects_non_fixed_train_once_window(
    strategy_registry: StrategyRegistry,
) -> None:
    params = _metadata_defaults(strategy_registry, "lstm_rank_v1")
    params["window_mode"] = "expanding"
    valid, message = strategy_registry.validate_params(
        "lstm_rank_v1", params
    )

    assert valid is False
    assert "window_mode" in message
