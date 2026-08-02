"""Alpha158 + LightGBM 因子排序策略 —— 多因子训练 + Walk-Forward 预测."""
# ruff: noqa: E402 -- Windows must preload LightGBM before pandas/pyarrow.

from __future__ import annotations

from backend.strategies.ml.runtime import (
    import_lightgbm,
    preload_windows_lightgbm,
)

# ⚠️ 必须在任何 pandas/pyarrow 导入之前加载 LightGBM。
# Windows 下若 pyarrow 的原生 DLL 先于 lib_lightgbm.dll 加载，
# LightGBM 后续所有 Dataset 操作都会崩溃
# (OSError: exception: access violation reading 0x0000000000000000)。
# 本模块由注册中心在应用启动时导入，可保证 LightGBM 先于任何 parquet 读取加载。
# Windows needs the eager import order above. Other platforms load LightGBM
# only when that strategy is trained, so its OpenMP runtime cannot interfere
# with PyTorch strategies merely because the registry scanned this module.
lgb = preload_windows_lightgbm()

import logging
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from backend.config import settings
from backend.strategies.base import (
    ParamField,
    PortfolioSignalMode,
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    StrategyMode,
    TrainableStrategy,
    TrainedModel,
    TrainingWindowContext,
    ml_training_params,
)
from backend.strategies.ml.alpha_factors import (
    compute_alpha_factors,
    get_available_features,
    get_feature_names,
)
logger = logging.getLogger(__name__)


def _load_lightgbm():
    """Load LightGBM on demand outside Windows and provide macOS guidance."""
    global lgb
    if lgb is not None:
        return lgb
    lgb = import_lightgbm()
    return lgb

# ═══════════════════════════════════════════════════════════════════════════
# 参数 Schema
# ═══════════════════════════════════════════════════════════════════════════

PARAM_SCHEMA: list[ParamField] = [
    ParamField(
        name="n_estimators",
        type="int",
        default=200,
        description="LightGBM 树的数量",
        min=50,
        max=500,
        step=10,
    ),
    ParamField(
        name="max_depth",
        type="int",
        default=5,
        description="树的最大深度",
        min=3,
        max=10,
        step=1,
    ),
    ParamField(
        name="learning_rate",
        type="float",
        default=0.05,
        description="学习率",
        min=0.01,
        max=0.3,
        step=0.01,
    ),
    ParamField(
        name="top_k_pct",
        type="float",
        default=0.10,
        description="买入股票比例（Top K%）",
        min=0.05,
        max=0.3,
        step=0.01,
    ),
] + ml_training_params(periodic=True, min_train_months=12)


# ═══════════════════════════════════════════════════════════════════════════
# 策略实现
# ═══════════════════════════════════════════════════════════════════════════


class Alpha158LGBStrategy(TrainableStrategy):
    """Alpha158 因子 + LightGBM 排序策略。

    策略原理:
        首先计算 50+ 个 Alpha158 量化因子，覆盖动量、波动率、换手率、
        量价相关性、价格形态、高阶矩等维度。
        然后使用 LightGBM（叶子优先的梯度提升树）以因子为特征、
        下月收益率为标签进行训练，学习因子与收益之间的非线性关系。
        采用 Walk-Forward 方式：每月用历史数据重新训练模型，
        再用最新因子值预测下月收益排名，买入预测分数最高的 Top K% 股票。
        LightGBM 训练速度快、内存效率高，适合中大规模因子数据集。

    适用范围:
        多因子量化选股，需要足够的股票数量（50+）和历史数据（1年+）。
    """

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None
        self._feature_names: list[str] = []
        self._factor_df: Optional[pd.DataFrame] = None
        self._factor_source: Optional[pd.DataFrame] = None

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="alpha158_lgb_v1",
            display_name="Alpha158 + LightGBM 因子策略",
            version="1.0.0",
            category=StrategyCategory.ML,
            description=(
                "基于 Alpha158 量化因子集和 LightGBM 梯度提升树的机器学习策略。"
                "计算 50+ 个核心因子（动量/波动/换手/相关性等），用 LightGBM 训练"
                "排序模型预测下月收益率。采用 Walk-Forward 方式每月重新训练，"
                "买入预测排名 Top K% 的股票。LightGBM 的叶子优先生长策略使训练"
                "速度快且对大数据集效率高。"
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=True,
            retrain_frequency=RetrainFrequency.MONTHLY,
            estimated_training_seconds=120,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=PARAM_SCHEMA,
            max_position_pct=0.10,
            supported_position_modes=["equal_weight"],
            tags=["机器学习", "LightGBM", "Alpha158", "因子投资", "排序学习", "Walk-Forward"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        n_est = params.get("n_estimators", 200)
        if not isinstance(n_est, int) or n_est < 10:
            return False, "n_estimators 必须为 >=10 的整数"
        lr = params.get("learning_rate", 0.05)
        if not isinstance(lr, (int, float)) or lr <= 0:
            return False, "learning_rate 必须为正数"
        top_k = params.get("top_k_pct", 0.10)
        if not isinstance(top_k, (int, float)) or not (0 < top_k <= 0.5):
            return False, "top_k_pct 必须在 (0, 0.5] 范围内"
        return True, ""

    # ── 训练 ────────────────────────────────────────────────────────

    def train(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
        progress_callback: Optional[Callable[[float, str], None]] = None,
        existing_model: Optional[Any] = None,
    ) -> TrainedModel:
        """用指定时间范围的数据训练 LightGBM 模型."""
        _load_lightgbm()

        if progress_callback:
            progress_callback(0.05, "正在计算 Alpha158 因子...")

        # 计算因子（prepare 内部缓存，同一 pivot 不重复计算）
        self.prepare(pivot, params)

        if progress_callback:
            progress_callback(0.15, "正在构建训练标签...")

        X_train, y_train = self._build_training_matrix(
            self._factor_df,
            pivot,
            train_start,
            train_end,
            horizon_days=self.label_horizon_days(params),
        )

        if progress_callback:
            progress_callback(0.30, f"正在训练 LightGBM (样本数: {len(X_train)})...")

        model = self._fit_lgb(X_train, y_train, params)

        if progress_callback:
            progress_callback(0.90, "LightGBM 训练完成，正在计算特征重要性...")

        # 特征重要性
        importance = dict(
            zip(self._feature_names, model.feature_importances_)
        )

        if progress_callback:
            progress_callback(1.0, "训练完成")

        self._model = model

        return TrainedModel(
            model=model,
            feature_importance=importance,
            train_metrics={
                "n_samples": len(X_train),
                "n_features": len(self._feature_names),
                "model_type": "LightGBM",
            },
            metadata={
                "train_start": train_start,
                "train_end": train_end,
                "params": params,
            },
        )

    # ── TrainableStrategy 钩子（平台驱动 Walk-Forward）────────────────

    def prepare(self, pivot: pd.DataFrame, params: dict) -> None:
        """一次性计算 Alpha158 因子并缓存（同一 pivot 不重复计算）."""
        if self._factor_df is not None and self._factor_source is pivot:
            return
        factor_df = compute_alpha_factors(pivot)
        self._factor_df = factor_df
        self._factor_source = pivot
        self._feature_names = get_available_features(factor_df) or get_feature_names()

    def get_universe(self, pivot: pd.DataFrame, params: dict) -> list[str]:
        if self._factor_df is not None:
            return self._extract_codes_from_factors(self._factor_df)
        return super().get_universe(pivot, params)

    def fit(
        self,
        pivot: pd.DataFrame,
        params: dict,
        train_start: str,
        train_end: str,
    ) -> Any:
        """在指定窗口训练 LightGBM 模型（复用 prepare 缓存的因子）."""
        _load_lightgbm()
        self.prepare(pivot, params)
        X_train, y_train = self._build_training_matrix(
            self._factor_df,
            pivot,
            train_start,
            train_end,
            horizon_days=self.label_horizon_days(params),
        )
        model = self._fit_lgb(X_train, y_train, params)
        self.record_train_metrics(
            n_samples=len(X_train),
            n_features=len(self._feature_names),
            model_type="LightGBM",
        )
        self._model = model
        return model

    def fit_with_validation(
        self,
        pivot: pd.DataFrame,
        params: dict,
        context: TrainingWindowContext,
    ) -> Any:
        """Fit with an untouched validation matrix and native early stopping."""
        _load_lightgbm()
        self.prepare(pivot, params)
        X_train, y_train = self._build_training_matrix(
            self._factor_df,
            pivot,
            context.train_start,
            context.train_end,
            horizon_days=self.label_horizon_days(params),
        )
        X_validation = y_validation = None
        if context.has_validation:
            validation_start, validation_sample_end = (
                self.validation_sample_window(pivot, params, context)
            )
            X_validation, y_validation = self._build_training_matrix(
                self._factor_df,
                pivot,
                validation_start,
                validation_sample_end,
                minimum_samples=2,
                horizon_days=self.label_horizon_days(params),
            )
        model = self._fit_lgb(
            X_train,
            y_train,
            params,
            X_validation,
            y_validation,
        )
        validation_metrics = self.evaluate_validation(
            model,
            pivot,
            params,
            context,
        )
        self.record_train_metrics(
            n_samples=len(X_train),
            n_features=len(self._feature_names),
            model_type="LightGBM",
            **validation_metrics,
        )
        self._model = model
        return model

    def predict_scores(
        self,
        model: Any,
        pivot: pd.DataFrame,
        params: dict,
        as_of_date: pd.Timestamp,
    ) -> dict[str, float]:
        """用模型对截至 as_of_date 的最新因子值打分."""
        scores: dict[str, float] = {}
        for code in self.get_universe(pivot, params):
            feats = self._get_factor_matrix(self._factor_df, code, self._feature_names)
            if feats is None:
                continue
            # 使用预测日之前的最新因子值
            avail = feats.loc[:as_of_date]
            if len(avail) == 0:
                continue
            latest = avail.iloc[-1]
            if latest.isna().any():
                continue
            try:
                scores[code] = float(model.predict(latest.values.reshape(1, -1))[0])
            except Exception:
                continue
        return scores

    # ── 内部辅助 ─────────────────────────────────────────────────────

    def _build_training_matrix(
        self,
        factor_df: pd.DataFrame,
        pivot: pd.DataFrame,
        train_start: str,
        train_end: str,
        minimum_samples: int = 100,
        horizon_days: int = 21,
    ) -> tuple[np.ndarray, np.ndarray]:
        """从已计算的因子构建训练矩阵（避免重复计算因子）.

        Returns:
            (X_train, y_train) 已过滤无穷值，且样本数 >= 100。

        Raises:
            ValueError: 训练数据为空或有效样本不足。
        """
        codes = self._extract_codes_from_factors(factor_df)
        X_list, y_list = [], []

        for code in codes:
            feats = self._get_factor_matrix(factor_df, code, self._feature_names)
            if feats is None:
                continue

            # 标签: 下月收益率 (forward 21-day return)
            close = self._get_close_from_pivot(pivot, code)
            if close is None:
                continue

            # 对齐日期
            common_dates = feats.index.intersection(close.index)
            feats = feats.loc[common_dates]
            close = close.loc[common_dates]

            fwd_ret = close.pct_change(horizon_days).shift(-horizon_days)

            # 按日期范围过滤
            train_mask = (feats.index >= train_start) & (feats.index <= train_end)
            feats = feats[train_mask]
            fwd_ret = fwd_ret[train_mask]

            # 合并
            aligned = feats.join(fwd_ret.rename("label"), how="inner").dropna()
            if len(aligned) < min(21, minimum_samples):
                continue

            common_features = [f for f in self._feature_names if f in aligned.columns]
            X_list.append(aligned[common_features].values)
            y_list.append(aligned["label"].values)

        if not X_list:
            raise ValueError("训练数据为空，无法训练模型")

        X_train = np.vstack(X_list)
        y_train = np.concatenate(y_list)

        # 过滤无穷值和极值
        finite_mask = np.isfinite(X_train).all(axis=1) & np.isfinite(y_train)
        X_train = X_train[finite_mask]
        y_train = y_train[finite_mask]

        if len(X_train) < minimum_samples:
            raise ValueError(
                f"有效训练样本不足 ({len(X_train)} < {minimum_samples})"
            )

        return X_train, y_train

    @staticmethod
    def _fit_lgb(
        X_train: np.ndarray,
        y_train: np.ndarray,
        params: dict,
        X_validation: np.ndarray | None = None,
        y_validation: np.ndarray | None = None,
    ):
        """拟合 LightGBM 回归模型."""
        lightgbm = _load_lightgbm()
        model = lightgbm.LGBMRegressor(
            n_estimators=params.get("n_estimators", 200),
            max_depth=params.get("max_depth", 5),
            learning_rate=params.get("learning_rate", 0.05),
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            verbose=-1,
            n_jobs=max(int(settings.JOB_CPU_THREAD_BUDGET), 1),
        )
        fit_kwargs: dict[str, Any] = {}
        if X_validation is not None and y_validation is not None:
            fit_kwargs = {
                "eval_set": [(X_validation, y_validation)],
                "eval_metric": "l2",
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
        model.fit(X_train, y_train, **fit_kwargs)
        return model

    @staticmethod
    def _extract_codes_from_factors(factor_df: pd.DataFrame) -> list[str]:
        """从因子 DataFrame 提取股票代码."""
        if isinstance(factor_df.columns, pd.MultiIndex):
            return sorted({c[0] for c in factor_df.columns if isinstance(c, tuple)})
        return []

    def _get_factor_matrix(
        self, factor_df: pd.DataFrame, code: str, feature_names: list[str]
    ) -> pd.DataFrame | None:
        """获取单只股票的因子矩阵."""
        cols = [(code, fn) for fn in feature_names if (code, fn) in factor_df.columns]
        if not cols:
            return None
        df = factor_df[cols].copy()
        df.columns = [c[1] for c in cols]
        return df

    @staticmethod
    def _get_close_from_pivot(pivot: pd.DataFrame, code: str) -> pd.Series | None:
        """获取收盘价.

        支持两种 pivot 格式:
          - MultiIndex columns: (code, field) 高层索引，支持多个字段
          - 简单列名: 列名就是股票代码，值为收盘价
        """
        if isinstance(pivot.columns, pd.MultiIndex):
            for field in ["close", "Close", "CLOSE", "收盘"]:
                if (code, field) in pivot.columns:
                    s = pivot[(code, field)].copy()
                    s.name = code
                    return s
        # 非 MultiIndex 兜底：列名直接是股票代码（单字段 pivot，仅收盘价）
        elif code in pivot.columns:
            s = pivot[code].copy()
            s.name = code
            return s
        return None
