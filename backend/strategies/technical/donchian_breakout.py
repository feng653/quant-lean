"""Stateful Donchian channel breakout strategy."""

from __future__ import annotations

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
)
from backend.strategies.factor._common import extract_codes, field_frame
from backend.strategies.research_context import code_is_eligible

PARAM_SCHEMA = [
    ParamField("entry_period", "int", 55, "入场通道周期", min=5, max=252),
    ParamField("exit_period", "int", 20, "离场通道周期", min=2, max=126),
]


class DonchianBreakoutStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_signal_state"

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="donchian_breakout_v1",
            display_name="唐奇安通道突破",
            version="1.0.0",
            category=StrategyCategory.TECHNICAL,
            description="以前一交易日确认的价格突破 55 日高点入场、跌破 20 日低点离场。",
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.EVENT_ORDERS,
            params=PARAM_SCHEMA,
            max_position_pct=0.10,
            tags=["趋势跟踪", "海龟交易", "通道突破", "无前视"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        entry = params.get("entry_period", 55)
        exit_ = params.get("exit_period", 20)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (entry, exit_)):
            return False, "entry_period 和 exit_period 必须为整数"
        if exit_ < 2 or entry <= exit_:
            return False, "entry_period 必须大于 exit_period，且 exit_period >= 2"
        return True, ""

    def generate_batch_signals(
        self, pivot: pd.DataFrame, params: dict, start_date: str, end_date: str
    ) -> SignalDict:
        entry = params.get("entry_period", 55)
        exit_ = params.get("exit_period", 20)
        close_frame = field_frame(pivot, "close")
        high_frame = field_frame(pivot, "high")
        low_frame = field_frame(pivot, "low")
        if high_frame.empty:
            high_frame = close_frame
        if low_frame.empty:
            low_frame = close_frame
        dates = pd.DatetimeIndex(pd.to_datetime(pivot.index))
        dates = dates.sort_values()
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        evaluation_dates = dates[dates <= end]
        signals: SignalDict = {}
        for code in extract_codes(pivot):
            if code not in close_frame:
                continue
            close = close_frame[code]
            high = high_frame[code] if code in high_frame else close
            low = low_frame[code] if code in low_frame else close
            # A signal on T observes close/high/low only through T-1.
            observed_close = close.shift(1)
            prior_upper = high.rolling(entry, min_periods=entry).max().shift(2)
            prior_lower = low.rolling(exit_, min_periods=exit_).min().shift(2)
            holding = False
            for date in evaluation_dates:
                price = observed_close.get(date)
                if pd.isna(price):
                    continue
                date_str = date.strftime("%Y-%m-%d")
                active = date >= start
                if not code_is_eligible(code, date):
                    holding = False
                    continue
                if not holding:
                    upper = prior_upper.get(date)
                    if pd.notna(upper) and price > upper:
                        strength = min(1.0, max(0.0, float(price / upper - 1)))
                        if active:
                            signals.setdefault(date_str, []).append(
                                SignalItem(code, "BUY", strength, 1.0)
                            )
                        holding = True
                else:
                    lower = prior_lower.get(date)
                    if pd.notna(lower) and price < lower:
                        strength = min(1.0, max(0.0, float(lower / price - 1)))
                        if active:
                            signals.setdefault(date_str, []).append(
                                SignalItem(code, "SELL", strength, 0.0)
                            )
                        holding = False
        return signals
