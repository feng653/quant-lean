"""Short-term reversal cross-sectional factor."""

from __future__ import annotations

import pandas as pd

from backend.core.types import SignalDict
from backend.strategies.base import (
    ParamField,
    PortfolioSignalMode,
    RetrainFrequency,
    StrategyCategory,
    StrategyMetadata,
    StrategyMode,
    StrategyProtocol,
)
from backend.strategies.factor._common import (
    ranked_monthly_signals,
    short_reversal_raw,
    validate_top_k,
)

PARAM_SCHEMA = [
    ParamField("lookback_days", "int", 21, "反转收益回看交易日", min=5, max=126),
    ParamField("top_k_pct", "float", 0.10, "买入截面排名比例", min=0.01, max=1.0),
]


class ShortReversalStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_cross_section"

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="short_reversal_v1",
            display_name="短期反转因子",
            version="1.0.0",
            category=StrategyCategory.FACTOR,
            description="按截至前一交易日的短期跌幅截面排序，每月买入跌幅最大的股票。",
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=PARAM_SCHEMA,
            tags=["单因子", "反转", "月度调仓", "无前视"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        return validate_top_k(params, 21, "lookback_days")

    def generate_batch_signals(
        self, pivot: pd.DataFrame, params: dict, start_date: str, end_date: str
    ) -> SignalDict:
        raw = short_reversal_raw(pivot, params.get("lookback_days", 21))
        return ranked_monthly_signals(raw, params, start_date, end_date)
