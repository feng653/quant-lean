"""Configurable equal/risk-budgeted score across four rule factors."""

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
    cross_sectional_rank,
    liquidity_raw,
    low_volatility_raw,
    momentum_raw,
    ranked_monthly_signals,
    short_reversal_raw,
)

FACTOR_NAMES = ("short_reversal", "low_volatility", "liquidity", "momentum")
PARAM_SCHEMA = [
    ParamField("use_short_reversal", "bool", True, "启用短期反转"),
    ParamField("use_low_volatility", "bool", True, "启用低波动"),
    ParamField("use_liquidity", "bool", True, "启用流动性"),
    ParamField("use_momentum", "bool", True, "启用截面动量"),
    ParamField("short_reversal_weight", "float", 1.0, "短期反转权重", min=0),
    ParamField("low_volatility_weight", "float", 1.0, "低波动权重", min=0),
    ParamField("liquidity_weight", "float", 1.0, "流动性权重", min=0),
    ParamField("momentum_weight", "float", 1.0, "截面动量权重", min=0),
    ParamField("top_k_pct", "float", 0.10, "买入截面排名比例", min=0.01, max=1),
]


class MultiFactorScoreStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_cross_section"

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="multi_factor_score_v1",
            display_name="多因子综合评分",
            version="1.0.0",
            category=StrategyCategory.FACTOR,
            description="将反转、低波动、流动性和动量的截面排名按配置权重合成。",
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=PARAM_SCHEMA,
            tags=["多因子", "截面排名", "可解释", "无前视"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        enabled = []
        total_weight = 0.0
        for name in FACTOR_NAMES:
            use = params.get(f"use_{name}", True)
            weight = params.get(f"{name}_weight", 1.0)
            if not isinstance(use, bool):
                return False, f"use_{name} 必须为布尔值"
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
                return False, f"{name}_weight 必须为非负数"
            if use:
                enabled.append(name)
                total_weight += float(weight)
        if not enabled or total_weight <= 0:
            return False, "至少启用一个权重大于 0 的因子"
        top_k = params.get("top_k_pct", 0.10)
        if isinstance(top_k, bool) or not isinstance(top_k, (int, float)):
            return False, "top_k_pct 必须为数字"
        if not 0 < float(top_k) <= 1:
            return False, "top_k_pct 必须在 (0, 1] 范围内"
        return True, ""

    def generate_batch_signals(
        self, pivot: pd.DataFrame, params: dict, start_date: str, end_date: str
    ) -> SignalDict:
        factor_builders = {
            "short_reversal": lambda: short_reversal_raw(pivot, 21),
            "low_volatility": lambda: low_volatility_raw(pivot, 120, "standard"),
            "liquidity": lambda: liquidity_raw(pivot, 21, "amihud"),
            "momentum": lambda: momentum_raw(pivot, 12, 1),
        }
        weighted: pd.DataFrame | None = None
        total = 0.0
        for name, build_factor in factor_builders.items():
            if not params.get(f"use_{name}", True):
                continue
            weight = float(params.get(f"{name}_weight", 1.0))
            if weight <= 0:
                continue
            raw = build_factor()
            component = cross_sectional_rank(raw) * weight
            weighted = component if weighted is None else weighted.add(component)
            total += weight
        if weighted is None or total <= 0:
            return {}
        return ranked_monthly_signals(weighted / total, params, start_date, end_date)
