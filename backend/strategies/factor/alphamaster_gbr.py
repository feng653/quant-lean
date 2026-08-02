"""AlphaMaster GBR 因子排序策略 —— 13维特征 + GradientBoostingRegressor.

参考 project2 AlphaMaster 策略的 13 维因子 + GBDT 排序框架。
默认月频调仓（rb=30），因为日频已被 project2 验证报告证实为亏损。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from backend.core.types import SignalDict, SignalItem
from backend.strategies.base import (
    ParamField,
    PortfolioSignalMode,
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    StrategyMode,
    StrategyProtocol,
    TrainedModel,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# 参数 Schema
# ═══════════════════════════════════════════════════════════════════════════

PARAM_SCHEMA: list[ParamField] = [
    ParamField(
        name="n_estimators",
        type="int",
        default=200,
        description="GBR 树的数量",
        min=50,
        max=500,
        step=10,
    ),
    ParamField(
        name="max_depth",
        type="int",
        default=4,
        description="树的最大深度",
        min=2,
        max=8,
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
        name="top_k",
        type="int",
        default=30,
        description="买入股票数量",
        min=5,
        max=100,
        step=5,
    ),
    ParamField(
        name="rebalance_days",
        type="int",
        default=30,
        description="调仓周期（交易日）",
        min=5,
        max=60,
        step=5,
    ),
    ParamField(
        name="min_train_months",
        type="int",
        default=12,
        description="最小训练数据长度（月）",
        min=6,
        max=36,
        step=1,
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# AlphaMaster 13 维因子
# ═══════════════════════════════════════════════════════════════════════════

FACTOR_NAMES = [
    "momentum_1m",    # 过去20日收益率（动量）
    "momentum_3m",    # 过去60日收益率
    "momentum_6m",    # 过去120日收益率
    "volatility_1m",  # 过去20日波动率
    "volatility_3m",  # 过去60日波动率
    "turnover_1m",    # 过去20日换手率均值
    "turnover_3m",    # 过去60日换手率均值
    "rsi_14",         # 14日 RSI
    "ma_dev_20",      # 20日均线偏离
    "ma_dev_60",      # 60日均线偏离
    "volume_ratio_5", # 5日量比
    "max_drawdown_1m",# 过去20日最大回撤
    "alpha_momentum", # Alpha动量: 收益率/波动率
]


def _compute_alpha_master_factors(pivot: pd.DataFrame) -> pd.DataFrame:
    """计算 AlphaMaster 的 13 维因子.

    Args:
        pivot: 日线宽表 DataFrame.
            - index: 日期
            - columns: MultiIndex (code, field)
            - 需要: close, volume (可选: high)

    Returns:
        DataFrame with MultiIndex columns (code, factor), index=date
    """
    codes = _extract_codes(pivot)
    if not codes:
        raise ValueError("无法提取股票代码")

    all_factors: dict[tuple, pd.Series] = {}

    for code in codes:
        close = _get_field(pivot, code, "close")
        if close is None or len(close) < 120:
            continue

        close = close.dropna()
        volume = _get_field(pivot, code, "volume")

        idx = close.index
        rets = close.pct_change()

        # 1-3. 动量因子
        all_factors[(code, "momentum_1m")] = close.pct_change(20).reindex(idx)
        all_factors[(code, "momentum_3m")] = close.pct_change(60).reindex(idx)
        all_factors[(code, "momentum_6m")] = close.pct_change(120).reindex(idx)

        # 4-5. 波动率因子
        all_factors[(code, "volatility_1m")] = rets.rolling(20).std().reindex(idx)
        all_factors[(code, "volatility_3m")] = rets.rolling(60).std().reindex(idx)

        # 6-7. 换手率因子
        if volume is not None and volume.sum() > 0:
            volume = volume.reindex(idx).fillna(0)
            all_factors[(code, "turnover_1m")] = volume.rolling(20).mean().reindex(idx)
            all_factors[(code, "turnover_3m")] = volume.rolling(60).mean().reindex(idx)
            # 量比
            vol_5 = volume.rolling(5).mean()
            vol_20 = volume.rolling(20).mean()
            all_factors[(code, "volume_ratio_5")] = (vol_5 / vol_20.replace(0, np.nan)).reindex(idx)

        # 8. RSI
        delta = rets.reindex(idx)
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
        rs = avg_gain / (avg_loss.replace(0, np.nan))
        all_factors[(code, "rsi_14")] = (100.0 - 100.0 / (1.0 + rs)).reindex(idx)

        # 9-10. 均线偏离
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()
        all_factors[(code, "ma_dev_20")] = (close / ma20.replace(0, np.nan) - 1).reindex(idx)
        all_factors[(code, "ma_dev_60")] = (close / ma60.replace(0, np.nan) - 1).reindex(idx)

        # 11. 量比 (已在上面处理)

        # 12. 最大回撤 (20日)
        rolling_max = close.rolling(20).max()
        all_factors[(code, "max_drawdown_1m")] = ((close - rolling_max) / rolling_max.replace(0, np.nan)).reindex(idx)

        # 13. Alpha动量: 20日收益 / 20日波动率
        ret_20 = close.pct_change(20)
        std_20 = rets.rolling(20).std()
        all_factors[(code, "alpha_momentum")] = (ret_20 / std_20.replace(0, np.nan)).reindex(idx)

    factor_df = pd.DataFrame(all_factors)
    factor_df.index.name = "date"

    # 截面排名归一化
    factor_df = _cross_sectional_rank(factor_df)

    return factor_df


def _extract_codes(pivot: pd.DataFrame) -> list[str]:
    if isinstance(pivot.columns, pd.MultiIndex):
        return list({c[0] for c in pivot.columns if isinstance(c, tuple)})
    codes = [str(c) for c in pivot.columns if c != "date"]
    if codes:
        return codes
    return []


def _get_field(pivot: pd.DataFrame, code: str, field: str) -> pd.Series | None:
    if isinstance(pivot.columns, pd.MultiIndex):
        if (code, field) in pivot.columns:
            return pivot[(code, field)]
        return None
    if field == "close" and code in pivot.columns:
        return pivot[code]
    return None


def _cross_sectional_rank(factor_df: pd.DataFrame) -> pd.DataFrame:
    """截面排名归一化到 [0, 1]（向量化实现）."""
    stacked = factor_df.stack(level=0)
    ranked = stacked.groupby(level=0).rank(pct=True)
    result = ranked.unstack(level=1).swaplevel(axis=1)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# 策略实现
# ═══════════════════════════════════════════════════════════════════════════


class AlphaMasterGBRStrategy(StrategyProtocol):
    """AlphaMaster GBR 因子排序策略。

    策略原理:
        参考 project2 的 AlphaMaster 策略框架，使用 13 维量化因子
        （动量、波动率、换手率、RSI、均线偏离、量比、最大回撤、Alpha动量）
        作为特征输入，训练 GradientBoostingRegressor 模型预测下周期收益率。
        默认月频调仓（30 个交易日），因为日频调仓在 project2 的严格验证中
        已被证实在真实执行语义+全成本下亏损 -75.64%。
        但同一信号源改为月频后累计 +60.30%，Sharpe +0.944。
        该策略验证了 Alpha13+GBDT 截面选股能力的真实存在。

    适用范围:
        多因子量化选股，需要至少 1 年以上历史数据；
        月频调仓适合中低频策略场景。
    """

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None
        self._training_cycles: list[dict[str, Any]] = []

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="alphamaster_gbr_v1",
            display_name="AlphaMaster GBR 因子策略",
            version="1.0.0",
            category=StrategyCategory.FACTOR,
            description=(
                "参考 AlphaMaster 策略框架的 13 维因子 + GBDT 排序策略。"
                "计算 13 个核心量化因子（动量/波动/换手/RSI/均线偏离/回撤等），"
                "用 GradientBoostingRegressor 训练截面排序模型。"
                "默认月频调仓（30 交易日），买入预测分数最高的 Top K 只股票。"
                "日频已被验证报告证伪（-75.64%），月频则展现真实 alpha（+60.30%）。"
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=True,
            retrain_frequency=RetrainFrequency.MONTHLY,
            estimated_training_seconds=90,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=PARAM_SCHEMA,
            max_position_pct=0.05,
            supported_position_modes=["equal_weight"],
            tags=["因子投资", "GBR", "AlphaMaster", "截面选股", "GradientBoosting", "月频"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        top_k = params.get("top_k", 30)
        if not isinstance(top_k, int) or top_k < 1:
            return False, "top_k 必须为正整数"
        rebalance = params.get("rebalance_days", 30)
        if not isinstance(rebalance, int) or rebalance < 1:
            return False, "rebalance_days 必须为正整数"
        return True, ""

    # ── 提取的训练/拟合辅助方法（因子复用） ─────────────────────────────────

    def _build_training_matrix(
        self,
        factor_df: pd.DataFrame,
        pivot: pd.DataFrame,
        train_start: str,
        train_end: str,
        params: dict | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """从已计算的因子构建训练矩阵（避免重复计算因子）.

        Args:
            params: 传入以获取 rebalance_days；None 时使用 30 默认值。
        """
        codes = _extract_codes(pivot)
        X_list, y_list = [], []
        rb = (params or {}).get("rebalance_days", 30)

        for code in codes:
            feats = self._get_factor_matrix(factor_df, code)
            if feats is None:
                continue

            close = _get_field(pivot, code, "close")
            if close is None:
                continue

            common_dates = feats.index.intersection(close.index)
            feats = feats.loc[common_dates]
            close = close.loc[common_dates]

            fwd_ret = close.pct_change(rb).shift(-rb)

            train_mask = (feats.index >= train_start) & (feats.index <= train_end)
            feats = feats[train_mask]
            fwd_ret = fwd_ret[train_mask]

            aligned = feats.join(fwd_ret.rename("label"), how="inner").dropna()
            if len(aligned) < 21:
                continue

            common_features = [f for f in FACTOR_NAMES if f in aligned.columns]
            X_list.append(aligned[common_features].values)
            y_list.append(aligned["label"].values)

        if not X_list:
            raise ValueError("训练数据为空")

        X_train = np.vstack(X_list)
        y_train = np.concatenate(y_list)

        finite_mask = np.isfinite(X_train).all(axis=1) & np.isfinite(y_train)
        X_train = X_train[finite_mask]
        y_train = y_train[finite_mask]

        if len(X_train) < 100:
            raise ValueError(f"有效训练样本不足 ({len(X_train)} < 100)")

        return X_train, y_train

    @staticmethod
    def _fit_gbr(X_train: np.ndarray, y_train: np.ndarray, params: dict):
        """拟合 GradientBoostingRegressor.

        Returns:
            (model, feature_importance_dict)
        """
        from sklearn.ensemble import GradientBoostingRegressor

        model = GradientBoostingRegressor(
            n_estimators=params.get("n_estimators", 200),
            max_depth=params.get("max_depth", 4),
            learning_rate=params.get("learning_rate", 0.05),
            subsample=0.8,
            min_samples_leaf=10,
            random_state=42,
        )
        model.fit(X_train, y_train)
        importance = dict(zip(FACTOR_NAMES, model.feature_importances_))
        return model, importance

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
        """训练 GradientBoostingRegressor."""
        if progress_callback:
            progress_callback(0.05, "正在计算 AlphaMaster 13 维因子...")

        factor_df = _compute_alpha_master_factors(pivot)

        if progress_callback:
            progress_callback(0.15, "正在构建训练标签...")

        X_train, y_train = self._build_training_matrix(
            factor_df, pivot, train_start, train_end, params
        )

        if progress_callback:
            progress_callback(0.30, f"正在训练 GBR (样本数: {len(X_train)})...")

        model, importance = self._fit_gbr(X_train, y_train, params)

        if progress_callback:
            progress_callback(0.90, "GBR 训练完成，计算特征重要性...")

        self._model = model

        return TrainedModel(
            model=model,
            feature_importance=importance,
            train_metrics={
                "n_samples": len(X_train),
                "n_features": len(FACTOR_NAMES),
                "model_type": "GradientBoostingRegressor",
            },
            metadata={
                "train_start": train_start,
                "train_end": train_end,
                "params": params,
            },
        )

    # ── 批量信号生成（Walk-Forward）─────────────────────────────────

    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
    ) -> SignalDict:
        """Walk-Forward 方式生成批量信号.

        使用全量历史数据训练，在调仓日预测并选股.
        """
        top_k: int = params.get("top_k", 30)
        rebalance_days: int = params.get("rebalance_days", 30)
        min_train_months: int = params.get("min_train_months", 12)
        min_train_days = min_train_months * 21  # 约21个交易日/月

        # 计算所有日期的因子（一次性，复用）
        factor_df = _compute_alpha_master_factors(pivot)
        codes = _extract_codes(pivot)

        if not codes:
            return {}

        all_factor_dates = factor_df.index.sort_values()
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)

        # 预测区间（test窗口）
        pred_dates = all_factor_dates[(all_factor_dates >= start) & (all_factor_dates <= end)]
        if len(pred_dates) == 0:
            logger.warning(f"测试区间 [{start_date}, {end_date}] 无可用数据")
            return {}

        # 检查历史数据是否足够
        pred_start_pos = list(all_factor_dates).index(pred_dates[0])
        if pred_start_pos < min_train_days:
            logger.warning(
                f"测试起点前历史数据不足 {min_train_days} 交易日(仅 {pred_start_pos})，无法生成信号"
            )
            return {}

        # 确定调仓日（在预测窗口内）
        rebalance_indices = []
        for i in range(len(all_factor_dates)):
            if all_factor_dates[i] < start:
                continue
            if all_factor_dates[i] > end:
                break
            # 每 rebalance_days 个交易日调仓一次
            pred_pos = list(pred_dates).index(all_factor_dates[i])
            if pred_pos % rebalance_days == 0:
                rebalance_indices.append(i)

        if not rebalance_indices:
            logger.warning(f"测试区间无调仓日 (rebalance_days={rebalance_days})")
            return {}

        signals: SignalDict = {}
        last_training_window: tuple[str, str] | None = None
        if self._model is not None and params.get("_train_start") and params.get("_train_end"):
            last_training_window = (
                str(pd.Timestamp(params["_train_start"]).date()),
                str(pd.Timestamp(params["_train_end"]).date()),
            )

        for rb_idx in rebalance_indices:
            pred_date = all_factor_dates[rb_idx]
            train_end_idx = max(0, rb_idx - rebalance_days)
            train_end_date = str(all_factor_dates[train_end_idx].date())
            requested_start = pd.Timestamp(params.get("_train_start", all_factor_dates[0]))
            requested_end = pd.Timestamp(params.get("_train_end", all_factor_dates[train_end_idx]))
            train_start_date = str(max(all_factor_dates[0], requested_start).date())
            train_end_date = str(min(all_factor_dates[train_end_idx], requested_end).date())
            if pd.Timestamp(train_start_date) >= pd.Timestamp(train_end_date):
                raise ValueError("训练窗口必须早于预测窗口")

            training_window = (train_start_date, train_end_date)
            if self._model is None or training_window != last_training_window:
                # 训练窗口未变化时复用同一模型，避免重复拟合相同样本。
                fit_started = time.perf_counter()
                try:
                    X_train, y_train = self._build_training_matrix(
                        factor_df, pivot, train_start_date, train_end_date, params
                    )
                    self._model, importance = self._fit_gbr(
                        X_train, y_train, params
                    )
                    last_training_window = training_window
                    self._training_cycles.append(
                        {
                            "pred_date": str(pred_date.date()),
                            "train_start": train_start_date,
                            "train_end": train_end_date,
                            "retrained": True,
                            "fit_seconds": round(
                                time.perf_counter() - fit_started, 3
                            ),
                            "n_train_samples": len(X_train),
                            "n_train_features": len(FACTOR_NAMES),
                            "error": None,
                            "train_metrics": {
                                "n_samples": len(X_train),
                                "n_features": len(FACTOR_NAMES),
                                "model_type": "GradientBoostingRegressor",
                                "feature_importance": importance,
                            },
                        }
                    )
                except Exception as e:
                    self._training_cycles.append(
                        {
                            "pred_date": str(pred_date.date()),
                            "train_start": train_start_date,
                            "train_end": train_end_date,
                            "retrained": False,
                            "fit_seconds": round(
                                time.perf_counter() - fit_started, 3
                            ),
                            "n_train_samples": None,
                            "n_train_features": len(FACTOR_NAMES),
                            "error": str(e),
                            "train_metrics": {},
                        }
                    )
                    logger.warning(
                        f"Walk-Forward 训练失败 [{train_start_date}, {train_end_date}]: {e}"
                    )
                    continue

            if self._model is None:
                continue

            # 预测
            scores: dict[str, float] = {}
            for code in codes:
                feats = self._get_factor_matrix(factor_df, code)
                if feats is None:
                    continue
                avail = feats.loc[:pred_date]
                if len(avail) == 0:
                    continue
                latest = avail.iloc[-1]
                if latest.isna().any():
                    continue
                try:
                    pred = float(self._model.predict(latest.values.reshape(1, -1))[0])
                    scores[code] = pred
                except Exception:
                    continue

            if not scores:
                continue

            k = min(top_k, len(scores))
            sorted_codes = sorted(scores, key=scores.get, reverse=True)[:k]

            date_str = pred_date.strftime("%Y-%m-%d")
            signals.setdefault(date_str, [])

            max_score = max(scores.values()) if scores else 1.0
            if max_score <= 0:
                max_score = 1.0

            for code in sorted_codes:
                score = max(0.0, min(1.0, scores[code] / max_score))
                signals[date_str].append(
                    SignalItem(code=code, action="BUY", score=score, weight=1.0)
                )

        return signals

    def get_training_telemetry(self) -> dict[str, Any]:
        """Expose legacy self-managed fit details for the persisted model card."""
        successful = [
            cycle
            for cycle in self._training_cycles
            if cycle["retrained"] and cycle["error"] is None
        ]
        total_samples = sum(
            int(cycle["n_train_samples"])
            for cycle in successful
            if cycle["n_train_samples"] is not None
        )
        elapsed_seconds = sum(
            float(cycle["fit_seconds"]) for cycle in self._training_cycles
        )
        last_window = (
            [successful[-1]["train_start"], successful[-1]["train_end"]]
            if successful
            else None
        )
        return {
            "retrain_count": len(successful),
            "total_fit_samples": total_samples or None,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "summary": (
                f"{len(successful)} 次训练, {total_samples} 个训练样本, "
                f"训练耗时 {elapsed_seconds:.2f}s, "
                f"{len(self._training_cycles) - len(successful)} 次失败"
            ),
            "last_training_window": last_window,
            "last_validation_window": None,
            "cycles": list(self._training_cycles),
        }

    # ── 内部辅助 ─────────────────────────────────────────────────────

    def _get_factor_matrix(
        self, factor_df: pd.DataFrame, code: str
    ) -> pd.DataFrame | None:
        cols = [(code, fn) for fn in FACTOR_NAMES if (code, fn) in factor_df.columns]
        if len(cols) < 5:
            return None
        df = factor_df[cols].copy()
        df.columns = [c[1] for c in cols]
        return df
