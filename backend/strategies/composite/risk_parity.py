"""Inverse-volatility weighted strategy composite."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.core.types import SignalDict
from backend.strategies.base import (
    ParamField,
    PortfolioSignalMode,
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    StrategyMode,
)
from backend.strategies.composite._common import (
    SUB_STRATEGY_PARAM,
    RuleCompositeStrategy,
    default_refs,
)
from backend.strategies.composite._signal_perf import merge_on_date, signal_daily_returns


class CompositeRiskParityStrategy(RuleCompositeStrategy):
    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="composite_riskparity_v1",
            display_name="风险平价策略组合",
            version="1.0.0",
            category=StrategyCategory.COMPOSITE,
            description="按各子策略过去 126 日纸面收益波动率的倒数动态加权。",
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.EVENT_ORDERS,
            params=[
                SUB_STRATEGY_PARAM,
                ParamField("lookback_days", "int", 126, "波动率窗口", min=20, max=252),
            ],
            sub_strategies=default_refs(),
            integration_method="strategy_risk_parity",
            tags=["组合策略", "风险平价", "动态权重"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        valid, message = super().validate_params(params)
        if not valid:
            return valid, message
        lookback = params.get("lookback_days", 126)
        if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 10:
            return False, "lookback_days 必须为 >=10 的整数"
        return True, ""

    def generate_batch_signals(
        self, pivot: pd.DataFrame, params: dict, start_date: str, end_date: str
    ) -> SignalDict:
        lookback = params.get("lookback_days", 126)
        ids, signals = self._run_children(
            pivot, params, start_date, end_date, warmup_days=lookback
        )
        returns = [signal_daily_returns(item, pivot) for item in signals]
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        all_dates = sorted(
            date_str
            for date_str in set().union(*(item.keys() for item in signals))
            if start <= pd.Timestamp(date_str) <= end
        )
        result: SignalDict = {}
        month_weights: dict[str, list[float]] = {}
        for date_str in all_dates:
            date = pd.Timestamp(date_str)
            month = date.strftime("%Y-%m")
            if month not in month_weights:
                vol = [
                    float(series.loc[series.index < date].tail(lookback).std())
                    for series in returns
                ]
                inverse = [
                    1.0 / value if np.isfinite(value) and value > 1e-10 else 0.0
                    for value in vol
                ]
                total = sum(inverse)
                month_weights[month] = (
                    [value / total for value in inverse]
                    if total > 0
                    else [1.0 / len(ids)] * len(ids)
                )
            items = merge_on_date(date_str, signals, month_weights[month])
            if items:
                result[date_str] = items
        return result
