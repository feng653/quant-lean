"""Market-regime weighted strategy composite."""

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
)
from backend.strategies.composite._common import (
    SUB_STRATEGY_PARAM,
    RuleCompositeStrategy,
    default_refs,
)
from backend.strategies.composite._signal_perf import close_frame, merge_on_date
from backend.strategies.research_context import mask_cross_section

TREND_IDS = {"ma_cross_v1", "macd_signal_v1", "donchian_breakout_v1", "momentum_cross_v1"}
DEFENSIVE_IDS = {"rsi_reversal_v1", "short_reversal_v1", "low_volatility_v1"}


class CompositeRegimeStrategy(RuleCompositeStrategy):
    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="composite_regime_v1",
            display_name="市场状态策略组合",
            version="1.0.0",
            category=StrategyCategory.COMPOSITE,
            description="以股票池等权指数相对 MA200 判断状态，在趋势族与防御族间切换权重。",
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.EVENT_ORDERS,
            params=[
                SUB_STRATEGY_PARAM,
                ParamField("regime_ma_days", "int", 200, "市场状态均线周期", min=60, max=252),
                ParamField("dominant_weight", "float", 0.70, "主导策略族权重", min=0.5, max=1),
            ],
            sub_strategies=default_refs(),
            integration_method="market_regime_switch",
            tags=["组合策略", "市场状态", "趋势防御切换"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        valid, message = super().validate_params(params)
        if not valid:
            return valid, message
        ma_days = params.get("regime_ma_days", 200)
        dominant = params.get("dominant_weight", 0.70)
        if isinstance(ma_days, bool) or not isinstance(ma_days, int) or ma_days < 20:
            return False, "regime_ma_days 必须为 >=20 的整数"
        if (
            isinstance(dominant, bool)
            or not isinstance(dominant, (int, float))
            or not 0.5 <= dominant <= 1
        ):
            return False, "dominant_weight 必须在 [0.5, 1] 范围内"
        return True, ""

    def generate_batch_signals(
        self, pivot: pd.DataFrame, params: dict, start_date: str, end_date: str
    ) -> SignalDict:
        ids, signals = self._run_children(pivot, params, start_date, end_date)
        # Calculate each security's trailing return on the complete public tape,
        # then form the market cross-section from members effective that day.
        # Masking prices first would incorrectly discard an entrant's valid
        # pre-entry close needed for its first eligible return.
        market_returns = mask_cross_section(close_frame(pivot).pct_change())
        market = market_returns.mean(axis=1).fillna(0).add(1).cumprod()
        # State used on T is based entirely on T-1 and earlier closes.
        observed = market.shift(1)
        regime_ma = market.rolling(params.get("regime_ma_days", 200)).mean().shift(1)
        dominant = float(params.get("dominant_weight", 0.70))
        all_dates = sorted(set().union(*(item.keys() for item in signals)))
        result: SignalDict = {}
        month_weights: dict[str, list[float]] = {}
        for date_str in all_dates:
            date = pd.Timestamp(date_str)
            month = date.strftime("%Y-%m")
            if month not in month_weights:
                bull = (
                    date in observed.index
                    and pd.notna(regime_ma.get(date))
                    and observed.get(date) >= regime_ma.get(date)
                )
                trend_total = dominant if bull else 1.0 - dominant
                defensive_total = 1.0 - trend_total
                trend_count = sum(strategy_id in TREND_IDS for strategy_id in ids)
                defensive_count = sum(strategy_id in DEFENSIVE_IDS for strategy_id in ids)
                other_count = len(ids) - trend_count - defensive_count
                weights = []
                for strategy_id in ids:
                    if strategy_id in TREND_IDS and trend_count:
                        weights.append(trend_total / trend_count)
                    elif strategy_id in DEFENSIVE_IDS and defensive_count:
                        weights.append(defensive_total / defensive_count)
                    else:
                        weights.append(1.0 / len(ids) if other_count else 0.0)
                total = sum(weights)
                month_weights[month] = [weight / total for weight in weights]
            items = merge_on_date(date_str, signals, month_weights[month])
            if items:
                result[date_str] = items
        return result
