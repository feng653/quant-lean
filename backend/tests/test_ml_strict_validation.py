from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from backend.services.walkforward import run_walk_forward
from backend.strategies.base import (
    MIN_VALIDATION_CROSS_SECTION_SIZE,
    MIN_VALIDATION_EFFECTIVE_DATES,
    DEFAULT_VALIDATION_RANK_IC,
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    TrainableStrategy,
    TrainingWindowContext,
    compute_validation_metrics,
)
from backend.strategies.ml.alpha158_lgb import Alpha158LGBStrategy
from backend.strategies.ml.alpha158_rank_lgb import Alpha158RankLGBStrategy
from backend.strategies.ml.alpha158_xgb import Alpha158XGBStrategy
from backend.strategies.ml.lstm_rank import LSTMRankStrategy
from backend.strategies.ml.transformer_rank import TransformerRankStrategy


class ValidationProbeStrategy(TrainableStrategy):
    def __init__(
        self,
        rank_ic: float = 0.5,
        *,
        validation_dates: int = MIN_VALIDATION_EFFECTIVE_DATES,
        minimum_cross_section: int = MIN_VALIDATION_CROSS_SECTION_SIZE,
    ) -> None:
        super().__init__()
        self.rank_ic = rank_ic
        self.validation_dates = validation_dates
        self.minimum_cross_section = minimum_cross_section
        self.contexts: list[TrainingWindowContext] = []
        self.predict_calls = 0

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="validation_probe",
            display_name="Validation probe",
            version="1",
            category=StrategyCategory.ML,
            description="Test double",
            requires_training=True,
            retrain_frequency=RetrainFrequency.MONTHLY,
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
        self._model = {"legacy": True}
        return self._model

    def fit_with_validation(
        self,
        pivot: pd.DataFrame,
        params: dict,
        context: TrainingWindowContext,
    ) -> Any:
        self.contexts.append(context)
        candidate = {"candidate": len(self.contexts)}
        self._model = candidate
        self.record_train_metrics(
            n_samples=100,
            n_features=2,
            n_validation_samples=(
                self.validation_dates * self.minimum_cross_section
            ),
            n_validation_candidate_dates=self.validation_dates,
            n_validation_dates=self.validation_dates,
            min_validation_cross_section_size=self.minimum_cross_section,
            validation_ic=self.rank_ic,
            validation_ic_std=0.1,
            validation_icir=self.rank_ic / 0.1,
            validation_rank_ic=self.rank_ic,
            validation_rank_ic_std=0.1,
            validation_rank_icir=self.rank_ic / 0.1,
            validation_loss=0.1,
            validation_score=0.2,
        )
        return candidate

    def predict_scores(
        self,
        model: Any,
        pivot: pd.DataFrame,
        params: dict,
        as_of_date: pd.Timestamp,
    ) -> dict[str, float]:
        self.predict_calls += 1
        return {"000001": 1.0, "000002": 0.5}


class LongHorizonValidationProbeStrategy(ValidationProbeStrategy):
    def label_horizon_days(self, params: dict) -> int:
        return 21


class TrainOnceValidationProbeStrategy(ValidationProbeStrategy):
    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="train_once_validation_probe",
            display_name="Train-once validation probe",
            version="1",
            category=StrategyCategory.ML,
            description="Test double",
            requires_training=True,
            retrain_frequency=RetrainFrequency.NEVER,
        )


@pytest.fixture
def market_data() -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-02", "2023-03-31")
    return pd.DataFrame(
        {
            "000001": np.linspace(10, 30, len(dates)),
            "000002": np.linspace(30, 10, len(dates)),
        },
        index=dates,
    )


def _params(**overrides: Any) -> dict:
    params = {
        "min_train_months": 6,
        "retrain_months": 1,
        "window_mode": "expanding",
        "rolling_train_months": 12,
        "embargo_days": 0,
        "validation_months": 1,
        "min_validation_rank_ic": DEFAULT_VALIDATION_RANK_IC,
        "top_k_pct": 0.5,
    }
    params.update(overrides)
    return params


def _assert_purged_train_validation_boundary(
    market_data: pd.DataFrame,
    *,
    train_end: str,
    validation_start: str,
    label_horizon_days: int,
    embargo_days: int,
) -> None:
    train_end_position = market_data.index.get_loc(pd.Timestamp(train_end))
    validation_start_position = market_data.index.get_loc(
        pd.Timestamp(validation_start)
    )
    assert (
        train_end_position + label_horizon_days + embargo_days
        < validation_start_position
    )


def test_walkforward_passes_non_overlapping_explicit_windows(
    market_data: pd.DataFrame,
) -> None:
    strategy = ValidationProbeStrategy()
    result = run_walk_forward(
        strategy,
        market_data,
        _params(embargo_days=3),
        "2023-01-01",
        "2023-03-31",
    )

    assert len(strategy.contexts) == 3
    for context, cycle in zip(strategy.contexts, result.cycles):
        assert context.validation_start is not None
        _assert_purged_train_validation_boundary(
            market_data,
            train_end=context.train_end,
            validation_start=context.validation_start,
            label_horizon_days=cycle.label_horizon_days,
            embargo_days=cycle.embargo_days,
        )
    assert all(cycle.validation_metrics for cycle in result.cycles)
    assert all(
        cycle.validation_metrics["n_validation_dates"]
        == MIN_VALIDATION_EFFECTIVE_DATES
        for cycle in result.cycles
    )
    assert all(
        cycle.validation_metrics["min_validation_cross_section_size"]
        == MIN_VALIDATION_CROSS_SECTION_SIZE
        for cycle in result.cycles
    )
    assert result.signals


def test_short_month_validation_is_extended_without_using_future_data(
    market_data: pd.DataFrame,
) -> None:
    strategy = LongHorizonValidationProbeStrategy()
    result = run_walk_forward(
        strategy,
        market_data,
        _params(embargo_days=4),
        "2023-03-01",
        "2023-03-31",
    )

    cycle = result.cycles[0]
    start_position = market_data.index.get_loc(pd.Timestamp(cycle.validation_start))
    end_position = market_data.index.get_loc(pd.Timestamp(cycle.validation_end))
    prediction_position = market_data.index.get_loc(pd.Timestamp(cycle.pred_date))
    assert end_position - start_position + 1 >= 22
    assert (
        end_position + cycle.label_horizon_days + cycle.embargo_days
        < prediction_position
    )
    _assert_purged_train_validation_boundary(
        market_data,
        train_end=cycle.train_end,
        validation_start=cycle.validation_start,
        label_horizon_days=cycle.label_horizon_days,
        embargo_days=cycle.embargo_days,
    )


def test_quality_gate_rejects_and_discards_candidate_model(
    market_data: pd.DataFrame,
) -> None:
    strategy = ValidationProbeStrategy(rank_ic=-0.25)

    with pytest.raises(RuntimeError, match="质量门"):
        run_walk_forward(
            strategy,
            market_data,
            _params(min_validation_rank_ic=0.1),
            "2023-01-01",
            "2023-03-31",
        )

    assert strategy._model is None
    assert strategy.predict_calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"validation_dates": MIN_VALIDATION_EFFECTIVE_DATES - 1},
        {"minimum_cross_section": MIN_VALIDATION_CROSS_SECTION_SIZE - 1},
    ],
)
def test_quality_gate_rejects_insufficient_cross_sectional_evidence(
    market_data: pd.DataFrame,
    overrides: dict[str, int],
) -> None:
    strategy = ValidationProbeStrategy(**overrides)

    with pytest.raises(RuntimeError, match="验证集"):
        run_walk_forward(
            strategy,
            market_data,
            _params(),
            "2023-01-01",
            "2023-03-31",
        )

    assert strategy._model is None
    assert strategy.predict_calls == 0


@pytest.mark.parametrize("threshold", [-1.0, 0.0, 0.009])
def test_quality_gate_rejects_rank_ic_threshold_below_server_floor(
    market_data: pd.DataFrame,
    threshold: float,
) -> None:
    with pytest.raises(ValueError, match="min_validation_rank_ic"):
        run_walk_forward(
            ValidationProbeStrategy(),
            market_data,
            _params(min_validation_rank_ic=threshold),
            "2023-01-01",
            "2023-01-31",
        )


def test_quality_gate_accepts_default_rank_ic_threshold(
    market_data: pd.DataFrame,
) -> None:
    result = run_walk_forward(
        ValidationProbeStrategy(rank_ic=DEFAULT_VALIDATION_RANK_IC),
        market_data,
        _params(),
        "2023-01-01",
        "2023-01-31",
    )

    assert result.last_model == {"candidate": 1}
    assert result.cycles[0].validation_metrics["validation_rank_ic"] == (
        DEFAULT_VALIDATION_RANK_IC
    )


def test_validation_months_zero_preserves_legacy_fit(
    market_data: pd.DataFrame,
) -> None:
    strategy = ValidationProbeStrategy()
    result = run_walk_forward(
        strategy,
        market_data,
        _params(validation_months=0, embargo_days=3),
        "2023-01-01",
        "2023-01-31",
    )

    cycle = result.cycles[0]
    assert cycle.validation_start is None
    assert cycle.retrained is True
    assert strategy.contexts[0].has_validation is False
    train_end_position = market_data.index.get_loc(pd.Timestamp(cycle.train_end))
    prediction_position = market_data.index.get_loc(pd.Timestamp(cycle.pred_date))
    assert (
        train_end_position + cycle.label_horizon_days + cycle.embargo_days
        < prediction_position
    )


def test_rolling_window_preserves_duration_after_validation_purge(
    market_data: pd.DataFrame,
) -> None:
    strategy = ValidationProbeStrategy()
    result = run_walk_forward(
        strategy,
        market_data,
        _params(
            window_mode="rolling",
            rolling_train_months=12,
            embargo_days=2,
        ),
        "2023-01-01",
        "2023-03-31",
    )

    starts = [pd.Timestamp(cycle.train_start) for cycle in result.cycles]
    assert starts == sorted(starts)
    assert len(set(starts)) == 3
    for cycle in result.cycles:
        assert cycle.validation_start is not None
        _assert_purged_train_validation_boundary(
            market_data,
            train_end=cycle.train_end,
            validation_start=cycle.validation_start,
            label_horizon_days=cycle.label_horizon_days,
            embargo_days=cycle.embargo_days,
        )
        covered_months = (
            pd.Period(cycle.train_end, freq="M")
            - pd.Period(cycle.train_start, freq="M")
        ).n
        assert 11 <= covered_months <= 12


def test_train_once_fixed_window_is_purged_before_validation(
    market_data: pd.DataFrame,
) -> None:
    strategy = TrainOnceValidationProbeStrategy()
    result = run_walk_forward(
        strategy,
        market_data,
        _params(
            window_mode="fixed",
            embargo_days=2,
            _train_start="2020-01-02",
            _train_end="2021-12-31",
        ),
        "2023-01-01",
        "2023-03-31",
    )

    assert len(strategy.contexts) == 1
    assert result.cycles[0].train_start == "2020-01-02"
    assert result.cycles[0].validation_end == "2021-12-31"
    for cycle in result.cycles:
        assert cycle.validation_start is not None
        _assert_purged_train_validation_boundary(
            market_data,
            train_end=cycle.train_end,
            validation_start=cycle.validation_start,
            label_horizon_days=cycle.label_horizon_days,
            embargo_days=cycle.embargo_days,
        )


def test_validation_metrics_are_deterministic() -> None:
    labels = np.array([0.3, -0.2, 0.1, 0.4])
    predictions = np.array([0.2, -0.1, 0.05, 0.5])

    first = compute_validation_metrics(labels, predictions)
    second = compute_validation_metrics(labels.copy(), predictions.copy())

    assert first == second
    assert first["n_validation_samples"] == 4
    assert first["n_validation_dates"] == 1
    assert first["min_validation_cross_section_size"] == 4
    assert first["validation_rank_ic"] == pytest.approx(1.0)
    assert first["validation_loss"] == pytest.approx(0.008125)


def test_validation_metrics_do_not_flatten_simpson_cross_sections() -> None:
    labels = np.array([0.0, 1.0, 100.0, 101.0])
    predictions = np.array([1.0, 0.0, 101.0, 100.0])
    prediction_dates = np.array(
        ["2024-01-02", "2024-01-02", "2024-01-03", "2024-01-03"]
    )

    flattened = float(np.corrcoef(labels, predictions)[0, 1])
    flattened_rank_ic = float(
        pd.Series(labels).rank().corr(pd.Series(predictions).rank())
    )
    metrics = compute_validation_metrics(
        labels,
        predictions,
        prediction_dates,
    )

    assert flattened == pytest.approx(0.9998000199980002)
    assert flattened_rank_ic == pytest.approx(0.6)
    assert metrics["validation_ic"] == pytest.approx(-1.0)
    assert metrics["validation_rank_ic"] == pytest.approx(-1.0)
    assert metrics["n_validation_dates"] == 2
    assert metrics["min_validation_cross_section_size"] == 2


def test_validation_metrics_exclude_nan_and_degenerate_dates() -> None:
    metrics = compute_validation_metrics(
        [1.0, 2.0, np.nan, 4.0, 4.0],
        [2.0, 1.0, 3.0, 7.0, 7.0],
        ["a", "a", "a", "b", "b"],
    )

    assert metrics["n_validation_samples"] == 4
    assert metrics["n_validation_candidate_dates"] == 2
    assert metrics["n_validation_dates"] == 1
    assert metrics["validation_rank_ic"] == pytest.approx(-1.0)


@pytest.mark.parametrize("strategy_type", [LSTMRankStrategy, TransformerRankStrategy])
def test_neural_validation_sequences_never_read_past_window_end(
    strategy_type: type[TrainableStrategy],
) -> None:
    dates = pd.bdate_range("2022-01-03", periods=150)
    base = np.linspace(10.0, 30.0, len(dates))
    pivot = pd.DataFrame(
        {"000001": base, "000002": base[::-1] + 20.0},
        index=dates,
    )
    strategy = strategy_type()
    start = str(dates[20].date())
    sample_start = str(dates[50].date())
    end = str(dates[100].date())

    X_before, y_before = strategy._build_sequences(  # type: ignore[attr-defined]
        pivot,
        10,
        start,
        end,
        sample_start=sample_start,
    )
    changed = pivot.copy()
    changed.loc[dates[101]:, :] *= 1000
    X_after, y_after = strategy._build_sequences(  # type: ignore[attr-defined]
        changed,
        10,
        start,
        end,
        sample_start=sample_start,
    )

    assert len(X_before) > 0
    np.testing.assert_array_equal(X_before, X_after)
    np.testing.assert_array_equal(y_before, y_after)


@pytest.mark.parametrize(
    ("strategy_type", "fit_method"),
    [
        (Alpha158LGBStrategy, "_fit_lgb"),
        (Alpha158XGBStrategy, "_fit_xgb"),
    ],
)
def test_tree_models_use_validation_labels_that_end_before_prediction(
    strategy_type: type[TrainableStrategy],
    fit_method: str,
) -> None:
    dates = pd.bdate_range("2022-01-03", periods=100)
    pivot = pd.DataFrame({"000001": np.arange(100.0)}, index=dates)
    strategy = strategy_type()
    strategy._feature_names = ["factor"]
    matrix_calls: list[tuple[str, str, int]] = []
    validation_calls: list[TrainingWindowContext] = []

    def prepare(_pivot: pd.DataFrame, _params: dict) -> None:
        strategy._factor_df = pd.DataFrame(index=dates)

    def build(
        _factors: pd.DataFrame,
        _pivot: pd.DataFrame,
        start: str,
        end: str,
        minimum_samples: int = 100,
        horizon_days: int = 21,
    ) -> tuple[np.ndarray, np.ndarray]:
        matrix_calls.append((start, end, minimum_samples))
        size = 100 if minimum_samples == 100 else 4
        return np.arange(size, dtype=float).reshape(-1, 1), np.arange(size, dtype=float)

    class FakeModel:
        def predict(self, features: np.ndarray) -> np.ndarray:
            return features[:, 0]

    def fit(
        X_train: np.ndarray,
        y_train: np.ndarray,
        params: dict,
        X_validation: np.ndarray | None = None,
        y_validation: np.ndarray | None = None,
    ) -> FakeModel:
        assert len(X_train) == len(y_train) == 100
        assert X_validation is not None and y_validation is not None
        assert len(X_validation) == len(y_validation) == 4
        return FakeModel()

    def evaluate_validation(
        model: Any,
        _pivot: pd.DataFrame,
        _params: dict,
        validation_context: TrainingWindowContext,
    ) -> dict[str, Any]:
        assert isinstance(model, FakeModel)
        validation_calls.append(validation_context)
        labels = np.tile(
            np.arange(MIN_VALIDATION_CROSS_SECTION_SIZE, dtype=float),
            MIN_VALIDATION_EFFECTIVE_DATES,
        )
        prediction_dates = np.repeat(
            np.arange(MIN_VALIDATION_EFFECTIVE_DATES),
            MIN_VALIDATION_CROSS_SECTION_SIZE,
        )
        return compute_validation_metrics(
            labels,
            labels,
            prediction_dates,
        )

    strategy.prepare = prepare  # type: ignore[method-assign]
    strategy._build_training_matrix = build  # type: ignore[method-assign]
    strategy.evaluate_validation = evaluate_validation  # type: ignore[method-assign]
    setattr(strategy, fit_method, fit)
    context = TrainingWindowContext(
        train_start=str(dates[0].date()),
        train_end=str(dates[39].date()),
        validation_start=str(dates[40].date()),
        validation_end=str(dates[79].date()),
    )

    strategy.fit_with_validation(pivot, {}, context)

    expected_last_sample = dates[79 - strategy.label_horizon_days({})]
    assert pd.Timestamp(matrix_calls[1][1]) == expected_last_sample
    assert pd.Timestamp(matrix_calls[1][1]) < pd.Timestamp(context.validation_end)
    assert validation_calls == [context]
    assert strategy.last_train_metrics["n_validation_samples"] == 100
    assert strategy.last_train_metrics["n_validation_dates"] == 5


def test_rank_model_uses_date_wise_validation_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = pd.bdate_range("2022-01-03", periods=100)
    pivot = pd.DataFrame({"000001": np.arange(100.0)}, index=dates)
    strategy = Alpha158RankLGBStrategy()
    strategy._feature_names = ["factor"]
    validation_calls: list[TrainingWindowContext] = []

    def prepare(_pivot: pd.DataFrame, _params: dict) -> None:
        strategy._factor_df = pd.DataFrame(index=dates)

    def build(
        _factors: pd.DataFrame,
        _pivot: pd.DataFrame,
        _start: str,
        _end: str,
        _horizon: int,
        minimum_samples: int = 100,
    ) -> tuple[np.ndarray, np.ndarray, list[int]]:
        size = 100 if minimum_samples == 100 else 40
        return (
            np.arange(size, dtype=float).reshape(-1, 1),
            np.arange(size, dtype=int) % 4,
            [20] * (size // 20),
        )

    class FakeRanker:
        def fit(
            self,
            X_train: np.ndarray,
            y_train: np.ndarray,
            *,
            group: list[int],
            **fit_kwargs: Any,
        ) -> None:
            assert len(X_train) == len(y_train) == sum(group)
            assert fit_kwargs["eval_group"] == [[20, 20]]

    class FakeLightGBM:
        @staticmethod
        def LGBMRanker(**_kwargs: Any) -> FakeRanker:
            return FakeRanker()

        @staticmethod
        def early_stopping(**_kwargs: Any) -> object:
            return object()

    def evaluate_validation(
        model: Any,
        _pivot: pd.DataFrame,
        _params: dict,
        validation_context: TrainingWindowContext,
    ) -> dict[str, Any]:
        assert isinstance(model, FakeRanker)
        validation_calls.append(validation_context)
        labels = np.tile(
            np.arange(MIN_VALIDATION_CROSS_SECTION_SIZE, dtype=float),
            MIN_VALIDATION_EFFECTIVE_DATES,
        )
        dates_for_rows = np.repeat(
            np.arange(MIN_VALIDATION_EFFECTIVE_DATES),
            MIN_VALIDATION_CROSS_SECTION_SIZE,
        )
        return compute_validation_metrics(labels, labels, dates_for_rows)

    monkeypatch.setattr(
        "backend.strategies.ml.alpha158_rank_lgb._load_lightgbm",
        lambda: FakeLightGBM(),
    )
    strategy.prepare = prepare  # type: ignore[method-assign]
    strategy._build_rank_matrix = build  # type: ignore[method-assign]
    strategy.evaluate_validation = evaluate_validation  # type: ignore[method-assign]
    context = TrainingWindowContext(
        train_start=str(dates[0].date()),
        train_end=str(dates[39].date()),
        validation_start=str(dates[40].date()),
        validation_end=str(dates[79].date()),
    )

    strategy.fit_with_validation(pivot, {}, context)

    assert validation_calls == [context]
    assert strategy.last_train_metrics["n_validation_samples"] == 100
    assert strategy.last_train_metrics["n_validation_dates"] == 5
