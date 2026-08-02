"""Low-volatility cross-sectional factor."""

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
    low_volatility_raw,
    ranked_monthly_signals,
    validate_top_k,
)

PARAM_SCHEMA = [
    ParamField("lookback_days", "int", 120, "波动率回看交易日", min=20, max=252),
    ParamField(
        "vol_method",
        "choice",
        "standard",
        "波动率口径",
        choices=["standard", "downside"],
    ),
    ParamField("top_k_pct", "float", 0.10, "买入截面排名比例", min=0.01, max=1.0),
]


class LowVolatilityStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_cross_section"

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="low_volatility_v1",
            display_name="低波动因子",
            version="1.0.0",
            category=StrategyCategory.FACTOR,
            description="按截至前一交易日的历史波动率逆序排名，每月买入低波动股票。",
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=PARAM_SCHEMA,
            tags=["单因子", "低波动", "月度调仓", "无前视"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        valid, message = validate_top_k(params, 120, "lookback_days")
        if not valid:
            return valid, message
        if params.get("vol_method", "standard") not in {"standard", "downside"}:
            return False, "vol_method 必须为 standard 或 downside"
        return True, ""

    def generate_batch_signals(
        self, pivot: pd.DataFrame, params: dict, start_date: str, end_date: str
    ) -> SignalDict:
        raw = low_volatility_raw(
            pivot,
            params.get("lookback_days", 120),
            params.get("vol_method", "standard"),
        )
        return ranked_monthly_signals(raw, params, start_date, end_date)
