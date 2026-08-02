"""Recent-performance weighted strategy composite."""

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


class CompositeMomentumStrategy(RuleCompositeStrategy):
    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="composite_momentum_v1",
            display_name="动量优选策略组合",
            version="1.0.0",
            category=StrategyCategory.COMPOSITE,
            description="按子策略过去 63 日正向 Sharpe 动态分配权重，每月更新。",
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.EVENT_ORDERS,
            params=[
                SUB_STRATEGY_PARAM,
                ParamField("lookback_days", "int", 63, "策略动量窗口", min=20, max=252),
            ],
            sub_strategies=default_refs(),
            integration_method="positive_strategy_momentum",
            tags=["组合策略", "策略动量", "动态权重"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        valid, message = super().validate_params(params)
        if not valid:
            return valid, message
        lookback = params.get("lookback_days", 63)
        if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 10:
            return False, "lookback_days 必须为 >=10 的整数"
        return True, ""

    def generate_batch_signals(
        self, pivot: pd.DataFrame, params: dict, start_date: str, end_date: str
    ) -> SignalDict:
        lookback = params.get("lookback_days", 63)
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
                scores = []
                for series in returns:
                    history = series.loc[series.index < date].tail(lookback)
                    volatility = float(history.std())
                    sharpe = (
                        float(history.mean()) / volatility * np.sqrt(252)
                        if np.isfinite(volatility) and volatility > 1e-10
                        else 0.0
                    )
                    scores.append(max(0.0, sharpe))
                total = sum(scores)
                month_weights[month] = (
                    [score / total for score in scores]
                    if total > 0
                    else [1.0 / len(ids)] * len(ids)
                )
            items = merge_on_date(date_str, signals, month_weights[month])
            if items:
                result[date_str] = items
        return result
