"""Signal-to-paper-return helpers for composite weighting (not discoverable)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.core.types import SignalDict, SignalItem
from backend.strategies.research_context import eligible_codes_on


def close_frame(pivot: pd.DataFrame) -> pd.DataFrame:
    if isinstance(pivot.columns, pd.MultiIndex):
        values = {
            str(code): pivot[(code, "close")]
            for code in pivot.columns.get_level_values(0).unique()
            if (code, "close") in pivot.columns
        }
        return pd.DataFrame(values, index=pivot.index).sort_index()
    return pivot.drop(columns=["date"], errors="ignore").copy().sort_index()


def signal_daily_returns(signals: SignalDict, pivot: pd.DataFrame) -> pd.Series:
    """Estimate a signal stream's close-to-close paper return.

    Signals stamped T alter the paper holdings for T+1's close-to-close return,
    matching the platform's next-session execution convention closely enough for
    strategy-level weighting without leaking future returns into a rebalance.
    """
    close = close_frame(pivot)
    asset_returns = close.pct_change()
    positions: dict[str, float] = {}
    result = pd.Series(0.0, index=close.index, dtype=float)
    events = {pd.Timestamp(date): items for date, items in signals.items()}
    for date in close.index:
        eligible = eligible_codes_on(date)
        if eligible is not None:
            positions = {
                code: weight
                for code, weight in positions.items()
                if code in eligible
            }
        available = {
            code: weight
            for code, weight in positions.items()
            if code in asset_returns and np.isfinite(asset_returns.at[date, code])
        }
        total = sum(available.values())
        if total > 0:
            result.at[date] = sum(
                weight / total * float(asset_returns.at[date, code])
                for code, weight in available.items()
            )
        # A T signal changes exposure only after T's close. The first paper
        # return it can affect is therefore indexed T+1, matching T+1 execution
        # without placing T+1's close into the history available on T.
        for item in events.get(pd.Timestamp(date), []):
            if item.action.upper() == "BUY":
                positions[item.code] = max(float(item.weight), float(item.score), 0.0)
            elif item.action.upper() == "SELL":
                positions.pop(item.code, None)
    return result


def merge_on_date(
    date_str: str,
    signals_list: list[SignalDict],
    weights: list[float],
) -> list[SignalItem]:
    net_scores: dict[str, float] = {}
    for signals, strategy_weight in zip(signals_list, weights):
        if strategy_weight <= 0:
            continue
        for item in signals.get(date_str, []):
            action = item.action.upper()
            if action not in {"BUY", "SELL"}:
                continue
            score = max(0.0, float(item.score)) * strategy_weight
            direction = 1.0 if action == "BUY" else -1.0
            net_scores[item.code] = net_scores.get(item.code, 0.0) + direction * score
    items: list[SignalItem] = []
    for code, net_score in sorted(net_scores.items()):
        eligible = eligible_codes_on(date_str)
        if eligible is not None and code not in eligible:
            continue
        if abs(net_score) <= 1e-12:
            continue
        action = "BUY" if net_score > 0 else "SELL"
        score = abs(net_score)
        items.append(SignalItem(code, action, score, score if action == "BUY" else 0.0))
    return items
