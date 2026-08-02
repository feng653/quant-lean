"""核心数据类型定义 —— 量化平台所有模块共享的数据结构."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════
# 交易信号
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class SignalItem:
    """单只股票的交易信号。"""

    code: str
    action: str  # "BUY" | "SELL" | "HOLD"
    score: float
    weight: float = 0.0  # 在组合中的权重（策略层设定）


# {日期字符串 → 当日信号列表}
SignalDict = dict[str, list[SignalItem]]


@dataclass(slots=True)
class RealtimeSignal:
    """实时/盘中产生的交易信号。"""

    code: str
    action: str
    score: float
    confidence: float
    reasoning: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# 交易记录
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class TradeRecord:
    """一笔成交记录（回测用）。"""

    date: str  # YYYY-MM-DD
    code: str
    action: str  # "BUY" | "SELL"
    price: float
    shares: int
    amount: float  # 成交金额 = price * shares
    cost: float  # 交易成本
    signal_date: str = ""  # 产生信号的 T 日；date 为 T+1 成交日
    signal_strategy: str = ""  # 产生信号的策略 ID
    signal_score: float = 0.0


@dataclass(slots=True)
class OrderRecord:
    """一笔订单记录（实盘/模拟盘用，包含状态字段）。"""

    date: str
    code: str
    action: str
    price: float
    shares: int
    amount: float
    cost: float
    signal_strategy: str = ""
    signal_score: float = 0.0
    order_type: str = "market"  # "market" | "limit"
    status: str = "pending"  # "pending" | "filled" | "rejected" | "cancelled"
    reject_reason: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# 持仓
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class PositionSnapshot:
    """某日收盘后单只股票的持仓快照。"""

    date: str
    code: str
    shares: int
    avg_cost: float  # 持仓均价
    close_price: float  # 当日收盘价
    market_value: float  # 当日市值
    unrealized_pnl: float  # 未实现盈亏


# ═══════════════════════════════════════════════════════════════════════════
# 回测结果
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class BacktestResult:
    """回测引擎的标准输出。"""

    equity_curve: Any  # pd.DataFrame: index=date, columns=['equity','benchmark']
    trade_log: list[TradeRecord]
    position_snapshots: list[PositionSnapshot]
    final_equity: float
    signals_generated: int
    trades_executed: int
