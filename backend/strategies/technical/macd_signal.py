"""MACD 金叉策略 —— DIF 上穿 DEA 买入、下穿卖出."""

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
        name="fast",
        type="int",
        default=8,
        description="快线 EMA 周期",
        min=8,
        max=20,
        step=1,
    ),
    ParamField(
        name="slow",
        type="int",
        default=21,
        description="慢线 EMA 周期",
        min=20,
        max=40,
        step=1,
    ),
    ParamField(
        name="signal",
        type="int",
        default=5,
        description="信号线 EMA 周期",
        min=5,
        max=15,
        step=1,
    ),
    ParamField(
        name="min_score",
        type="float",
        default=0.2,
        description="最低信号分数阈值",
        min=0.0,
        max=1.0,
        step=0.05,
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# 策略实现
# ═══════════════════════════════════════════════════════════════════════════


class MACDSignalStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_signal_state"

    """MACD 金叉死叉策略。

    策略原理:
        MACD 由三个组件构成：DIF（快线 EMA - 慢线 EMA）、DEA（DIF 的 EMA 平滑）、
        MACD 柱状图（DIF - DEA，代表趋势动能）。
        当 DIF 从下方上穿 DEA 时（金叉），说明短期动量开始强于长期趋势，
        上升动能增强，策略发出买入信号。
        当 DIF 从上方下穿 DEA 时（死叉），说明动量转弱、趋势可能反转，
        策略发出卖出信号。
        MACD 兼具趋势方向和动量双重特性，比单纯均线交叉能更早捕捉趋势变化。

    适用范围:
        趋势市场；在震荡市中可能产生频繁假信号。
    """

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="macd_signal_v1",
            display_name="MACD 金叉策略",
            version="1.0.0",
            category=StrategyCategory.TECHNICAL,
            description=(
                "经典 MACD 金叉死叉策略。计算 DIF（12日EMA - 26日EMA）和 "
                "DEA（DIF的9日EMA），DIF上穿DEA形成金叉产生买入信号，"
                "DIF下穿DEA形成死叉产生卖出信号。"
                "信号强度基于柱状图（DIF-DEA）的相对大小。"
                "MACD 兼具趋势和动量双重特性，是所有技术指标中表现最稳健的策略之一。"
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.EVENT_ORDERS,
            params=PARAM_SCHEMA,
            max_position_pct=0.10,
            supported_position_modes=["equal_weight"],
            tags=["MACD", "金叉死叉", "趋势跟踪", "动量", "经典策略", "技术指标"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        fast = params.get("fast", 12)
        slow = params.get("slow", 26)
        signal_p = params.get("signal", 9)
        if not all(isinstance(p, int) for p in [fast, slow, signal_p]):
            return False, "所有周期参数必须为整数"
        if fast >= slow:
            return False, "fast 必须小于 slow"
        if signal_p <= 0:
            return False, "signal 必须为正整数"
        return True, ""

    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
    ) -> SignalDict:
        fast: int = params.get("fast", 12)
        slow: int = params.get("slow", 26)
        signal_p: int = params.get("signal", 9)
        min_score: float = params.get("min_score", 0.2)

        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        codes = self._extract_codes(pivot)

        signals: SignalDict = {}

        for code in codes:
            close = self._get_close_series(pivot, code)
            if close is None or len(close) < slow + signal_p:
                continue

            close = close.loc[:end].dropna()
            if len(close) < slow + signal_p:
                continue

            # ── 纯 pandas 实现 MACD ──
            ema_fast = close.ewm(span=fast, adjust=False).mean()
            ema_slow = close.ewm(span=slow, adjust=False).mean()

            dif = ema_fast - ema_slow
            dea = dif.ewm(span=signal_p, adjust=False).mean()
            histogram = 2 * (dif - dea)  # MACD 柱状图

            # ── 交叉检测 ──
            prev_dif = dif.shift(1)
            prev_dea = dea.shift(1)

            # 金叉: 前一日 DIF <= DEA 且当日 DIF > DEA
            golden_cross = (prev_dif <= prev_dea) & (dif > dea)
            # 死叉: 前一日 DIF >= DEA 且当日 DIF < DEA
            death_cross = (prev_dif >= prev_dea) & (dif < dea)

            # Normalize each signal only with values observable on that date.
            # A full-window maximum makes an old score (and min_score gate)
            # change when later bars arrive.
            historical_scale = (
                histogram.abs()
                .expanding(min_periods=1)
                .max()
                .replace(0.0, 1.0)
                .fillna(1.0)
            )

            for d in dif.index:
                if d < start or not code_is_eligible(code, d):
                    continue
                date_str = d.strftime("%Y-%m-%d")
                signals.setdefault(date_str, [])

                if golden_cross.get(d, False):
                    strength = float(
                        abs(histogram.get(d, 0.0)) / historical_scale.at[d]
                    )
                    score = max(0.0, min(1.0, strength))
                    if score >= min_score:
                        signals[date_str].append(
                            SignalItem(code=code, action="BUY", score=score, weight=1.0)
                        )

                if death_cross.get(d, False):
                    strength = float(
                        abs(histogram.get(d, 0.0)) / historical_scale.at[d]
                    )
                    score = max(0.0, min(1.0, strength))
                    if score >= min_score:
                        signals[date_str].append(
                            SignalItem(code=code, action="SELL", score=score, weight=1.0)
                        )

        return signals

    # ── 内部辅助 ─────────────────────────────────────────────────────

    @staticmethod
    def _extract_codes(pivot: pd.DataFrame) -> list[str]:
        if isinstance(pivot.columns, pd.MultiIndex):
            codes = list({c[0] for c in pivot.columns if isinstance(c, tuple)})
            if codes:
                return codes
        if "code" in pivot.columns:
            return list(pivot["code"].unique())
        codes = [str(c) for c in pivot.columns if c != "date"]
        if codes:
            return codes
        return []

    @staticmethod
    def _get_close_series(pivot: pd.DataFrame, code: str) -> pd.Series | None:
        if isinstance(pivot.columns, pd.MultiIndex):
            for field in ["close", "Close", "CLOSE", "收盘"]:
                if (code, field) in pivot.columns:
                    return pivot[(code, field)].copy()
        if "close" in pivot.columns:
            return pivot["close"].copy()
        if code in pivot.columns:
            return pivot[code].copy()
        return None
