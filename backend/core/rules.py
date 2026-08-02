"""A股交易规则工具 —— 整手、交易日、资金检查."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import pandas as pd

from .cost_model import CostModel


def round_lot(shares: int) -> int:
    """将股数向下取整到100的倍数（A股整手规则）。

    >>> round_lot(250)
    200
    >>> round_lot(99)
    0
    """
    return max(0, (shares // 100) * 100)


def is_trading_day(d: date, calendar: Optional[pd.DatetimeIndex] = None) -> bool:
    """判断给定日期是否为交易日。

    Args:
        d: 待判断日期。
        calendar: 交易日历（pd.DatetimeIndex），若为 None 则退化为"仅排除周末"。

    Returns:
        True 如果是交易日。
    """
    if calendar is not None:
        # 标准化为日期比较
        return pd.Timestamp(d) in calendar
    # 降级方案：排除周六、周日
    return d.weekday() < 5  # 0=Mon ... 4=Fri → 5=Sat, 6=Sun


def next_trading_day(d: date, calendar: Optional[pd.DatetimeIndex] = None) -> date:
    """返回 d 之后最近的一个交易日（不含 d 本身）。

    Args:
        d: 参考日期。
        calendar: 交易日历。

    Returns:
        下一个交易日。
    """
    nxt = d + timedelta(days=1)
    max_attempts = 30  # 防止无限循环（如长假期）
    attempts = 0
    while attempts < max_attempts:
        if is_trading_day(nxt, calendar):
            return nxt
        nxt += timedelta(days=1)
        attempts += 1
    # fallback: 返回原日期 + 1 天
    return d + timedelta(days=1)


def can_buy(cash: float, price: float, shares: int, cost_model: CostModel) -> bool:
    """判断是否有足够资金买入指定股数。

    Args:
        cash: 可用资金。
        price: 买入单价。
        shares: 想要买入的股数（应为整手）。
        cost_model: 成本模型。

    Returns:
        True 如果可以买入。
    """
    if price <= 0 or shares <= 0 or cash <= 0:
        return False
    total_cost = cost_model.calc_buy_cost(price, shares)
    return total_cost <= cash
