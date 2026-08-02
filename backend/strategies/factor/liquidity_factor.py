"""Liquidity cross-sectional factor."""

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
    liquidity_raw,
    ranked_monthly_signals,
    validate_top_k,
)

PARAM_SCHEMA = [
    ParamField("lookback_days", "int", 21, "流动性回看交易日", min=5, max=126),
    ParamField(
        "method",
        "choice",
        "amihud",
        "流动性口径",
        choices=["amihud", "amount"],
    ),
    ParamField("top_k_pct", "float", 0.10, "买入截面排名比例", min=0.01, max=1.0),
]


class LiquidityFactorStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_cross_section"

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="liquidity_factor_v1",
            display_name="流动性因子",
            version="1.0.0",
            category=StrategyCategory.FACTOR,
            description="以 Amihud 非流动性或成交额衡量流动性，每月选择高流动性股票。",
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=PARAM_SCHEMA,
            tags=["单因子", "流动性", "Amihud", "无前视"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        valid, message = validate_top_k(params, 21, "lookback_days")
        if not valid:
            return valid, message
        if params.get("method", "amihud") not in {"amihud", "amount"}:
            return False, "method 必须为 amihud 或 amount"
        return True, ""

    def generate_batch_signals(
        self, pivot: pd.DataFrame, params: dict, start_date: str, end_date: str
    ) -> SignalDict:
        raw = liquidity_raw(
            pivot, params.get("lookback_days", 21), params.get("method", "amihud")
        )
        return ranked_monthly_signals(raw, params, start_date, end_date)
