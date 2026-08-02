"""Equal-weight signal composite."""

from __future__ import annotations

import pandas as pd

from backend.core.types import SignalDict
from backend.strategies.base import (
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


class CompositeEqualStrategy(RuleCompositeStrategy):
    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="composite_equal_v1",
            display_name="等权策略组合",
            version="1.0.0",
            category=StrategyCategory.COMPOSITE,
            description="将多个规则型子策略的信号等权聚合。",
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.EVENT_ORDERS,
            params=[SUB_STRATEGY_PARAM],
            sub_strategies=default_refs(),
            integration_method="equal_weight",
            tags=["组合策略", "等权", "规则型"],
        )

    def generate_batch_signals(
        self, pivot: pd.DataFrame, params: dict, start_date: str, end_date: str
    ) -> SignalDict:
        ids, signals = self._run_children(pivot, params, start_date, end_date)
        return self._merge_signals(signals, [1.0 / len(ids)] * len(ids))
