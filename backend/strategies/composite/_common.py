"""Shared implementation for concrete composite strategies."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from backend.core.types import SignalDict
from backend.strategies.base import (
    CompositeStrategy,
    ParamField,
    StrategyCategory,
    SubStrategyRef,
)
from backend.strategies.registry import get_registry

DEFAULT_SUB_STRATEGIES = (
    "ma_cross_v1",
    "rsi_reversal_v1",
    "macd_signal_v1",
    "donchian_breakout_v1",
    "short_reversal_v1",
    "low_volatility_v1",
)

SUB_STRATEGY_PARAM = ParamField(
    "sub_strategy_ids",
    "str",
    ",".join(DEFAULT_SUB_STRATEGIES),
    "逗号分隔的原子子策略 ID",
)


def default_refs() -> list[SubStrategyRef]:
    return [SubStrategyRef(strategy_id=sid, role="规则型信号源") for sid in DEFAULT_SUB_STRATEGIES]


class RuleCompositeStrategy(CompositeStrategy):
    """Common validation and execution plumbing; skipped by registry by filename."""

    point_in_time_context_capability = "dated_composite"

    @classmethod
    def _strategy_id(cls) -> str:
        return cls.metadata().strategy_id

    @staticmethod
    def _parse_ids(params: dict) -> list[str]:
        raw = params.get("sub_strategy_ids", ",".join(DEFAULT_SUB_STRATEGIES))
        if isinstance(raw, str):
            return [part.strip() for part in raw.split(",") if part.strip()]
        if isinstance(raw, (list, tuple)):
            return [str(part).strip() for part in raw if str(part).strip()]
        return []

    def validate_params(self, params: dict) -> tuple[bool, str]:
        ids = self._parse_ids(params)
        if not ids:
            return False, "sub_strategy_ids 至少包含一个策略"
        if len(ids) != len(set(ids)):
            return False, "sub_strategy_ids 不能重复"
        own_id = self._strategy_id()
        registry = get_registry()
        if not registry.list_all():
            registry.scan_directory(Path(__file__).resolve().parents[1])
        for strategy_id in ids:
            if strategy_id == own_id:
                return False, "组合策略不能递归引用自身"
            try:
                metadata = registry.get_metadata(strategy_id)
            except KeyError:
                return False, f"未知子策略: {strategy_id}"
            if metadata.category == StrategyCategory.COMPOSITE:
                return False, f"禁止嵌套组合策略: {strategy_id}"
            if metadata.requires_training:
                return False, f"组合策略暂不支持需要训练的子策略: {strategy_id}"
        return True, ""

    def _run_children(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
        warmup_days: int = 0,
    ) -> tuple[list[str], list[SignalDict]]:
        valid, message = self.validate_params(params)
        if not valid:
            raise ValueError(message)
        ids = self._parse_ids(params)
        child_start = start_date
        if warmup_days > 0 and len(pivot.index):
            dates = pd.DatetimeIndex(pd.to_datetime(pivot.index)).sort_values().unique()
            start_position = int(dates.searchsorted(pd.Timestamp(start_date), side="left"))
            warmup_position = max(0, start_position - warmup_days)
            child_start = dates[warmup_position].strftime("%Y-%m-%d")
        signals = [
            self._get_sub_strategy(strategy_id).generate_batch_signals(
                pivot, {}, child_start, end_date
            )
            for strategy_id in ids
        ]
        return ids, signals
