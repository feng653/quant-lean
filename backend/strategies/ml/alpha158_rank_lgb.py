"""Alpha158 LightGBM LambdaRank strategy with monthly retraining."""
# ruff: noqa: E402 -- Windows must preload LightGBM before pandas/pyarrow.

from __future__ import annotations

from backend.strategies.ml.runtime import (
    import_lightgbm,
    preload_windows_lightgbm,
)

# Keep this call before pandas/pyarrow on Windows. Loading pyarrow's native DLL
# first can make LightGBM Dataset construction crash with an access violation.
lgb = preload_windows_lightgbm()

from typing import Any

import numpy as np
import pandas as pd

from backend.config import settings
from backend.strategies import base as strategy_base
from backend.strategies.base import (
    ParamField,
    PortfolioSignalMode,
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    StrategyMode,
    TrainableStrategy,
    TrainingWindowContext,
)
from backend.strategies.ml.alpha_factors import (
    compute_alpha_factors,
    get_available_features,
)


def _load_lightgbm():
    global lgb
    if lgb is None:
        lgb = import_lightgbm()
    return lgb


MODEL_PARAM_SCHEMA = [
    ParamField("n_estimators", "int", 200, "树数量", min=50, max=1000),
    ParamField("num_leaves", "int", 31, "叶子数量", min=7, max=255),
    ParamField("learning_rate", "float", 0.05, "学习率", min=0.005, max=0.3),
    ParamField("label_horizon_days", "int", 21, "未来收益标签周期", min=5, max=63),
    ParamField("top_k_pct", "float", 0.10, "买入预测排名比例", min=0.01, max=0.5),
]


def _periodic_training_params() -> list[ParamField]:
    """Use the platform ML schema, with a compatibility fallback for old bases."""
    factory = getattr(strategy_base, "ml_training_params", None)
    if factory is not None:
        return factory(periodic=True, min_train_months=12)
    return [
        ParamField("retrain_months", "int", 1, "重训练间隔（月）", min=1, max=12),
        ParamField(
            "window_mode",
            "choice",
            "expanding",
            "训练窗口模式",
            choices=["expanding", "rolling"],
        ),
        ParamField(
            "rolling_train_months", "int", 36, "滚动训练窗口（月）", min=1, max=120
        ),
        ParamField("embargo_days", "int", 0, "标签隔离交易日", min=0, max=60),
        ParamField("validation_months", "int", 1, "验证集月数", min=0, max=24),
        ParamField("min_train_months", "int", 12, "最小训练长度（月）", min=1, max=120),
    ]


PARAM_SCHEMA = MODEL_PARAM_SCHEMA + _periodic_training_params()


class Alpha158RankLGBStrategy(TrainableStrategy):
    def __init__(self) -> None:
        super().__init__()
        self._factor_df: pd.DataFrame | None = None
        self._factor_source: pd.DataFrame | None = None
        self._feature_names: list[str] = []

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="alpha158_rank_lgb_v1",
            display_name="Alpha158 + LightGBM 排序学习",
            version="1.0.0",
            category=StrategyCategory.ML,
            description=(
                "使用 Alpha158 因子和 LightGBM LambdaRank，按交易日截面分组学习"
                "未来收益相对名次，并由平台按月 Walk-Forward 重训练。"
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=True,
            retrain_frequency=RetrainFrequency.MONTHLY,
            estimated_training_seconds=180,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=PARAM_SCHEMA,
            max_position_pct=0.10,
            tags=["机器学习", "Alpha158", "LightGBM", "LambdaRank", "周期重训练"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        for key in ("n_estimators", "num_leaves", "label_horizon_days"):
            value = params.get(key, {"n_estimators": 200, "num_leaves": 31, "label_horizon_days": 21}[key])
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                return False, f"{key} 必须为正整数"
        learning_rate = params.get("learning_rate", 0.05)
        top_k = params.get("top_k_pct", 0.10)
        if (
            isinstance(learning_rate, bool)
            or not isinstance(learning_rate, (int, float))
            or learning_rate <= 0
        ):
            return False, "learning_rate 必须为正数"
        if (
            isinstance(top_k, bool)
            or not isinstance(top_k, (int, float))
            or not 0 < top_k <= 0.5
        ):
            return False, "top_k_pct 必须在 (0, 0.5] 范围内"
        window_mode = params.get("window_mode", "expanding")
        if window_mode not in {"expanding", "rolling"}:
            return False, "window_mode 必须为 expanding 或 rolling"
        integer_defaults = {
            "retrain_months": 1,
            "rolling_train_months": 36,
            "embargo_days": 0,
            "validation_months": 1,
            "min_train_months": 12,
        }
        for key, default in integer_defaults.items():
            value = params.get(key, default)
            minimum = 0 if key in {"embargo_days", "validation_months"} else 1
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < minimum
            ):
                return False, f"{key} 必须为 >= {minimum} 的整数"
        if (
            window_mode == "rolling"
            and params.get("rolling_train_months", 36)
            < params.get("min_train_months", 12)
        ):
            return False, "rolling_train_months 不能小于 min_train_months"
        return True, ""

    def prepare(self, pivot: pd.DataFrame, params: dict) -> None:
        if self._factor_df is not None and self._factor_source is pivot:
            return
        # Alpha factors at signal date T are shifted so they contain market data
        # only through T-1.
        factors = compute_alpha_factors(pivot).shift(1)
        self._factor_df = factors
        self._factor_source = pivot
        self._feature_names = get_available_features(factors)

    def fit(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
    ) -> Any:
        lightgbm = _load_lightgbm()
        self.prepare(pivot, params)
        if self._factor_df is None:
            raise ValueError("Alpha158 因子尚未准备")
        X, y, group = self._build_rank_matrix(
            self._factor_df,
            pivot,
            train_start,
            train_end,
            params.get("label_horizon_days", 21),
        )
        model = lightgbm.LGBMRanker(
            objective="lambdarank",
            metric="ndcg",
            n_estimators=params.get("n_estimators", 200),
            num_leaves=params.get("num_leaves", 31),
            learning_rate=params.get("learning_rate", 0.05),
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            verbosity=-1,
            n_jobs=max(int(settings.JOB_CPU_THREAD_BUDGET), 1),
        )
        model.fit(X, y, group=group)
        self._model = model
        metrics = {
            "n_samples": len(X),
            "n_features": X.shape[1],
            "n_groups": len(group),
            "model_type": "LightGBM LambdaRank",
        }
        self._last_train_metrics = metrics
        recorder = getattr(self, "record_train_metrics", None)
        if recorder is not None:
            recorder(**metrics)
        return model

    def fit_with_validation(
        self,
        pivot: pd.DataFrame,
        params: dict,
        context: TrainingWindowContext,
    ) -> Any:
        """Fit LambdaRank with a temporally isolated validation group."""
        lightgbm = _load_lightgbm()
        self.prepare(pivot, params)
        if self._factor_df is None:
            raise ValueError("Alpha158 因子尚未准备")
        horizon = params.get("label_horizon_days", 21)
        X_train, y_train, train_group = self._build_rank_matrix(
            self._factor_df,
            pivot,
            context.train_start,
            context.train_end,
            horizon,
        )
        X_validation = y_validation = validation_group = None
        if context.has_validation:
            validation_start, validation_sample_end = (
                self.validation_sample_window(pivot, params, context)
            )
            X_validation, y_validation, validation_group = self._build_rank_matrix(
                self._factor_df,
                pivot,
                validation_start,
                validation_sample_end,
                horizon,
                minimum_samples=2,
            )
        model = lightgbm.LGBMRanker(
            objective="lambdarank",
            metric="ndcg",
            n_estimators=params.get("n_estimators", 200),
            num_leaves=params.get("num_leaves", 31),
            learning_rate=params.get("learning_rate", 0.05),
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            verbosity=-1,
            n_jobs=max(int(settings.JOB_CPU_THREAD_BUDGET), 1),
        )
        fit_kwargs: dict[str, Any] = {}
        if (
            X_validation is not None
            and y_validation is not None
            and validation_group is not None
        ):
            fit_kwargs = {
                "eval_set": [(X_validation, y_validation)],
                "eval_group": [validation_group],
                "callbacks": [
                    lightgbm.early_stopping(
                        stopping_rounds=max(
                            10,
                            min(50, int(params.get("n_estimators", 200)) // 10),
                        ),
                        verbose=False,
                    )
                ],
            }
        model.fit(X_train, y_train, group=train_group, **fit_kwargs)
        validation_metrics = self.evaluate_validation(
            model,
            pivot,
            params,
            context,
        )
        self._model = model
        self.record_train_metrics(
            n_samples=len(X_train),
            n_features=X_train.shape[1],
            n_groups=len(train_group),
            model_type="LightGBM LambdaRank",
            **validation_metrics,
        )
        return model

    def predict_scores(
        self,
        model: Any,
        pivot: pd.DataFrame,
        params: dict,
        as_of_date: pd.Timestamp,
    ) -> dict[str, float]:
        self.prepare(pivot, params)
        if self._factor_df is None:
            return {}
        scores: dict[str, float] = {}
        for code in self.get_universe(pivot, params):
            matrix = self._factor_matrix(code)
            if matrix is None:
                continue
            available = matrix.loc[:as_of_date].dropna()
            if available.empty:
                continue
            scores[code] = float(
                model.predict(available.iloc[-1].to_numpy().reshape(1, -1))[0]
            )
        return scores

    def get_universe(self, pivot: pd.DataFrame, params: dict) -> list[str]:
        if self._factor_df is not None and isinstance(
            self._factor_df.columns, pd.MultiIndex
        ):
            return sorted({str(column[0]) for column in self._factor_df.columns})
        return super().get_universe(pivot, params)

    def _factor_matrix(self, code: str) -> pd.DataFrame | None:
        if self._factor_df is None:
            return None
        columns = [
            (code, feature)
            for feature in self._feature_names
            if (code, feature) in self._factor_df.columns
        ]
        if not columns:
            return None
        result = self._factor_df[columns].copy()
        result.columns = [column[1] for column in columns]
        return result.reindex(columns=self._feature_names)

    def _build_rank_matrix(
        self,
        factor_df: pd.DataFrame,
        pivot: pd.DataFrame,
        train_start: str,
        train_end: str,
        horizon: int,
        minimum_samples: int = 100,
    ) -> tuple[np.ndarray, np.ndarray, list[int]]:
        close = self._close_frame(pivot)
        forward_return = close.shift(-horizon).div(close).sub(1)
        dates = factor_df.index[
            (factor_df.index >= pd.Timestamp(train_start))
            & (factor_df.index <= pd.Timestamp(train_end))
        ]
        rows: list[np.ndarray] = []
        labels: list[int] = []
        groups: list[int] = []
        matrices = {
            code: self._factor_matrix(code) for code in self.get_universe(pivot, {})
        }
        for date in dates:
            day_features: list[np.ndarray] = []
            day_returns: list[float] = []
            for code, matrix in matrices.items():
                if matrix is None or date not in matrix.index or code not in forward_return:
                    continue
                values = matrix.loc[date].to_numpy(dtype=float)
                label = forward_return.at[date, code]
                if not np.isfinite(values).all() or not np.isfinite(label):
                    continue
                day_features.append(values)
                day_returns.append(float(label))
            if len(day_features) < 2:
                continue
            relevance = np.ceil(
                pd.Series(day_returns).rank(pct=True, method="average").mul(4)
            ).sub(1)
            rows.extend(day_features)
            labels.extend(relevance.clip(0, 3).astype(np.int64).tolist())
            groups.append(len(day_features))
        if not rows or sum(groups) < minimum_samples:
            raise ValueError(
                f"有效排序训练样本不足 ({sum(groups)} < {minimum_samples})"
            )
        return np.vstack(rows), np.asarray(labels, dtype=np.int64), groups

    @staticmethod
    def _close_frame(pivot: pd.DataFrame) -> pd.DataFrame:
        if isinstance(pivot.columns, pd.MultiIndex):
            values = {
                str(code): pivot[(code, "close")]
                for code in pivot.columns.get_level_values(0).unique()
                if (code, "close") in pivot.columns
            }
            return pd.DataFrame(values, index=pivot.index)
        return pivot.drop(columns=["date"], errors="ignore")
