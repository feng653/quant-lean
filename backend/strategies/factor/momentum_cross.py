"""Cross-sectional momentum factor with a skip month."""

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
    momentum_raw,
    ranked_monthly_signals,
    validate_top_k,
)

PARAM_SCHEMA = [
    ParamField("lookback_months", "int", 12, "动量回看月数", min=2, max=24),
    ParamField("skip_months", "int", 1, "跳过最近月数", min=0, max=6),
    ParamField("top_k_pct", "float", 0.10, "买入截面排名比例", min=0.01, max=1.0),
]


class MomentumCrossStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_cross_section"

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="momentum_cross_v1",
            display_name="截面动量因子",
            version="1.0.0",
            category=StrategyCategory.FACTOR,
            description="计算跳过最近一个月的 12 个月动量，每月买入截面强势股票。",
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=PARAM_SCHEMA,
            tags=["单因子", "截面动量", "月度调仓", "无前视"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        valid, message = validate_top_k(params, 12, "lookback_months")
        if not valid:
            return valid, message
        skip = params.get("skip_months", 1)
        lookback = params.get("lookback_months", 12)
        if isinstance(skip, bool) or not isinstance(skip, int) or skip < 0:
            return False, "skip_months 必须为非负整数"
        if skip >= lookback:
            return False, "skip_months 必须小于 lookback_months"
        return True, ""

    def generate_batch_signals(
        self, pivot: pd.DataFrame, params: dict, start_date: str, end_date: str
    ) -> SignalDict:
        raw = momentum_raw(
            pivot,
            params.get("lookback_months", 12),
            params.get("skip_months", 1),
        )
        return ranked_monthly_signals(raw, params, start_date, end_date)
