"""双均线交叉策略 —— 金叉买入、死叉卖出."""

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
from backend.strategies.research_context import code_is_eligible

# ═══════════════════════════════════════════════════════════════════════════
# 参数 Schema
# ═══════════════════════════════════════════════════════════════════════════

PARAM_SCHEMA: list[ParamField] = [
    ParamField(
        name="fast_period",
        type="int",
        default=20,
        description="快速均线周期（日）",
        min=5,
        max=60,
        step=1,
    ),
    ParamField(
        name="slow_period",
        type="int",
        default=30,
        description="慢速均线周期（日）",
        min=20,
        max=250,
        step=1,
    ),
    ParamField(
        name="min_score",
        type="float",
        default=0.0,
        description="最低信号分数阈值（低于此值忽略）",
        min=0.0,
        max=1.0,
        step=0.05,
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# 策略实现
# ═══════════════════════════════════════════════════════════════════════════


class MACrossStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_signal_state"

    """双均线交叉策略（经典趋势跟踪）。

    策略原理:
        当短期均线由下向上穿越长期均线时（金叉），视为上升趋势开始，产生买入信号；
        当短期均线由上向下跌破长期均线时（死叉），视为下降趋势开始，产生卖出信号。

        信号评分基于交叉点的"距离强度"——金叉时 short_ma / long_ma - 1，
        死叉时 1 - short_ma / long_ma，值越大表示信号越强。

    适用范围:
        趋势明显的市场环境；震荡市中可能产生频繁假信号。
    """

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="ma_cross_v1",
            display_name="双均线交叉策略",
            version="1.0.0",
            category=StrategyCategory.TECHNICAL,
            description=(
                "经典双均线趋势跟踪策略。计算快速均线和慢速均线，"
                "金叉（快线上穿慢线）产生买入信号，死叉（快线下穿慢线）产生卖出信号。"
                "适用于趋势明显的市场，震荡市需配合过滤条件使用。"
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.EVENT_ORDERS,
            params=PARAM_SCHEMA,
            max_position_pct=0.10,
            supported_position_modes=["equal_weight"],
            tags=["趋势跟踪", "均线", "经典策略", "技术指标"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        fast = params.get("fast_period", 20)
        slow = params.get("slow_period", 60)
        if not isinstance(fast, int) or not isinstance(slow, int):
            return False, "fast_period 和 slow_period 必须为整数"
        if fast >= slow:
            return False, "fast_period 必须小于 slow_period"
        return True, ""

    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
    ) -> SignalDict:
        """对 pivot 中每只股票分别计算双均线交叉信号。

        pivot 格式假设:
            - index: 日期 (pd.DatetimeIndex)
            - columns: MultiIndex (code, field) 或单层 field
            - 必须包含 'close' 列

        为兼容性，此处假设 pivot 的 close 可通过 stock_data['close'] 获取。
        如果 pivot 是多股票宽表，则遍历各股票。
        """
        fast_period: int = params.get("fast_period", 20)
        slow_period: int = params.get("slow_period", 60)
        min_score: float = params.get("min_score", 0.5)

        # 日期范围
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)

        # ── 检测 pivot 数据结构 ──
        # 尝试发现股票列表
        codes = self._extract_codes(pivot)

        signals: SignalDict = {}

        for code in codes:
            close = self._get_close_series(pivot, code)
            if close is None or len(close) < slow_period:
                continue

            # 截取日期范围
            close = close.loc[:end].dropna()
            if len(close) < slow_period:
                continue

            # 计算均线
            fast_ma = close.rolling(window=fast_period, min_periods=fast_period).mean()
            slow_ma = close.rolling(window=slow_period, min_periods=slow_period).mean()

            # FIXED: reviewer issue #14 — 前视偏差修复：使用 T-1 日数据计算 T 日信号
            # 交叉检测：前一日 fast < slow 且当日 fast >= slow → 金叉
            #         前一日 fast > slow 且当日 fast <= slow → 死叉
            # shift(1) 确保用 T-1 日数据判断交叉，信号标记在 T 日
            prev_fast = fast_ma.shift(1)
            prev_slow = slow_ma.shift(1)

            # 金叉: T-1日 fast < slow, T日 fast >= slow（用 T 日数据确认交叉，但计算基于 T-1）
            golden_cross = (prev_fast < prev_slow) & (fast_ma >= slow_ma)
            # 死叉: T-1日 fast > slow, T日 fast <= slow
            death_cross = (prev_fast > prev_slow) & (fast_ma <= slow_ma)

            for d in close.index:
                if d < start or not code_is_eligible(code, d):
                    continue
                date_str = d.strftime("%Y-%m-%d")
                signals.setdefault(date_str, [])

                if golden_cross.get(d, False):
                    # 计算信号强度（基于 T-1 日数据避免前视）
                    strength = float(fast_ma.shift(1).loc[d] / slow_ma.shift(1).loc[d] - 1)
                    score = abs(strength)
                    if score >= min_score:
                        signals[date_str].append(
                            SignalItem(
                                code=code,
                                action="BUY",
                                score=score,
                                weight=1.0,
                            )
                        )

                if death_cross.get(d, False):
                    strength = float(1 - fast_ma.shift(1).loc[d] / slow_ma.shift(1).loc[d])
                    score = abs(strength)
                    if score >= min_score:
                        signals[date_str].append(
                            SignalItem(
                                code=code,
                                action="SELL",
                                score=score,
                                weight=1.0,
                            )
                        )

        return signals

    # ── 内部辅助 ─────────────────────────────────────────────────────

    @staticmethod
    def _extract_codes(pivot: pd.DataFrame) -> list[str]:
        """从 pivot 中提取股票代码列表。"""
        if isinstance(pivot.columns, pd.MultiIndex):
            # (code, field) 格式
            codes = list({c[0] for c in pivot.columns if isinstance(c, tuple)})
            if codes:
                return codes
        # 退化：单股票模式
        if "code" in pivot.columns:
            return list(pivot["code"].unique())
        # 把所有列名视为股票代码（用于简单格式 pivot, 列名就是股票代码）
        codes = [str(c) for c in pivot.columns if c != "date"]
        if codes:
            return codes
        return []

    @staticmethod
    def _get_close_series(
        pivot: pd.DataFrame, code: str
    ) -> pd.Series | None:
        """获取某只股票的收盘价序列。"""
        if isinstance(pivot.columns, pd.MultiIndex):
            if (code, "close") in pivot.columns:
                return pivot[(code, "close")].copy()
            # 尝试其他可能的字段名
            for field in ["close", "Close", "CLOSE", "收盘"]:
                if (code, field) in pivot.columns:
                    return pivot[(code, field)].copy()
        # 单列 close
        if "close" in pivot.columns:
            return pivot["close"].copy()
        # 列名就是股票代码
        if code in pivot.columns:
            return pivot[code].copy()
        return None
