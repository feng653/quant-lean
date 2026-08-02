"""Shared, non-discoverable helpers for cross-sectional factor strategies."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from backend.core.types import SignalDict, SignalItem
from backend.strategies.research_context import mask_cross_section


def extract_codes(pivot: pd.DataFrame) -> list[str]:
    if isinstance(pivot.columns, pd.MultiIndex):
        return sorted({str(column[0]) for column in pivot.columns})
    return sorted(str(column) for column in pivot.columns if column != "date")


def field_frame(pivot: pd.DataFrame, field: str) -> pd.DataFrame:
    """Return a date × code frame for one market field."""
    if isinstance(pivot.columns, pd.MultiIndex):
        values: dict[str, pd.Series] = {}
        aliases = (field, field.capitalize(), field.upper())
        for code in extract_codes(pivot):
            for alias in aliases:
                if (code, alias) in pivot.columns:
                    values[code] = pivot[(code, alias)]
                    break
        return pd.DataFrame(values, index=pivot.index).sort_index()
    if field == "close":
        return pivot.drop(columns=["date"], errors="ignore").copy().sort_index()
    return pd.DataFrame(index=pivot.index)


def monthly_signal_dates(
    index: pd.Index, start_date: str, end_date: str
) -> pd.DatetimeIndex:
    """Return month-end signal dates for next-month-first-session execution."""
    dates = pd.DatetimeIndex(pd.to_datetime(index)).sort_values()
    # The pivot may contain observations after the requested experiment end.
    # Determine month boundaries from the as-of slice so the same request has
    # identical output when it receives a full frame or a frame truncated at
    # end_date.
    dates = dates[dates <= pd.Timestamp(end_date)]
    if dates.empty:
        return dates
    last = dates.to_series(index=dates).groupby(dates.to_period("M")).last()
    signal_dates = pd.DatetimeIndex(last.to_numpy())
    signal_dates = signal_dates[
        (signal_dates >= pd.Timestamp(start_date))
        & (signal_dates <= pd.Timestamp(end_date))
    ]
    # The engine can consume the immediately preceding pivot session on the
    # first backtest day. Emit one initial decision there so a test beginning
    # mid-month or on month-first is not forced to stay in cash for a month.
    previous = dates[dates < pd.Timestamp(start_date)]
    if len(previous):
        signal_dates = signal_dates.insert(0, previous[-1]).unique().sort_values()
    return signal_dates


def cross_sectional_rank(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize each date's eligible finite factor values to [0, 1].

    The input retains every security's public pre-entry history for trailing
    features.  The platform context is applied immediately before ranking so a
    future constituent cannot alter today's ranks or Top-K denominator.
    """
    clean = mask_cross_section(raw).replace([np.inf, -np.inf], np.nan)
    return clean.rank(axis=1, pct=True, method="average")


def validate_top_k(params: dict, default_lookback: int, lookback_key: str) -> tuple[bool, str]:
    lookback = params.get(lookback_key, default_lookback)
    top_k = params.get("top_k_pct", 0.10)
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 2:
        return False, f"{lookback_key} 必须为 >=2 的整数"
    if isinstance(top_k, bool) or not isinstance(top_k, (int, float)):
        return False, "top_k_pct 必须为数字"
    if not 0 < float(top_k) <= 1:
        return False, "top_k_pct 必须在 (0, 1] 范围内"
    return True, ""


def ranked_monthly_signals(
    raw: pd.DataFrame,
    params: dict,
    start_date: str,
    end_date: str,
) -> SignalDict:
    ranked = cross_sectional_rank(raw)
    dates = monthly_signal_dates(ranked.index, start_date, end_date)
    top_k_pct = float(params.get("top_k_pct", 0.10))
    signals: SignalDict = {}
    for date in dates:
        row = ranked.loc[date].dropna().sort_values(ascending=False)
        if row.empty:
            continue
        count = max(1, math.ceil(len(row) * top_k_pct))
        signals[date.strftime("%Y-%m-%d")] = [
            SignalItem(
                code=str(code),
                action="BUY",
                score=float(score),
                weight=float(score),
            )
            for code, score in row.iloc[:count].items()
        ]
    return signals


def short_reversal_raw(pivot: pd.DataFrame, lookback: int) -> pd.DataFrame:
    close = field_frame(pivot, "close")
    return -close.pct_change(lookback).shift(1)


def low_volatility_raw(
    pivot: pd.DataFrame, lookback: int, method: str
) -> pd.DataFrame:
    returns = field_frame(pivot, "close").pct_change()
    if method == "downside":
        volatility = returns.clip(upper=0).rolling(lookback).std()
    else:
        volatility = returns.rolling(lookback).std()
    return -volatility.shift(1)


def liquidity_raw(pivot: pd.DataFrame, lookback: int, method: str) -> pd.DataFrame:
    close = field_frame(pivot, "close")
    amount = field_frame(pivot, "amount")
    if amount.empty:
        volume = field_frame(pivot, "volume")
        amount = volume.mul(close)
    amount = amount.replace(0, np.nan)
    if method == "amihud":
        illiquidity = close.pct_change().abs().div(amount)
        return -illiquidity.rolling(lookback).mean().shift(1)
    return np.log1p(amount.rolling(lookback).mean()).shift(1)


def momentum_raw(
    pivot: pd.DataFrame, lookback_months: int, skip_months: int
) -> pd.DataFrame:
    close = field_frame(pivot, "close")
    lookback_days = lookback_months * 21
    skip_days = skip_months * 21
    # Both endpoints are at least T-1, so a signal stamped T never sees T's close.
    return (
        close.shift(skip_days + 1).div(close.shift(lookback_days + 1)).sub(1)
    )
