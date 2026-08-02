"""RSI 均值回归策略 —— 超卖买入、超买卖出."""

from __future__ import annotations

import numpy as np
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
        name="period",
        type="int",
        default=7,
        description="RSI 计算周期（日）",
        min=5,
        max=30,
        step=1,
    ),
    ParamField(
        name="oversold",
        type="int",
        default=25,
        description="超卖阈值（RSI 低于此值视为超卖）",
        min=20,
        max=40,
        step=1,
    ),
    ParamField(
        name="overbought",
        type="int",
        default=75,
        description="超买阈值（RSI 高于此值视为超买）",
        min=60,
        max=80,
        step=1,
    ),
    ParamField(
        name="min_score",
        type="float",
        default=0.0,
        description="最低信号分数阈值",
        min=0.0,
        max=1.0,
        step=0.05,
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# 策略实现
# ═══════════════════════════════════════════════════════════════════════════


class RSIReversalStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_signal_state"

    """RSI 均值回归策略。

    策略原理:
        RSI (Relative Strength Index) 衡量价格变动的速度和幅度，范围 0-100。
        当 RSI 低于超卖阈值（默认30）时，说明近期跌幅过大、市场恐慌，
        预期价格将均值回归反弹，策略发出买入信号。
        当 RSI 高于超买阈值（默认70）时，说明涨幅过大、市场过度乐观，
        预期价格将回调，策略发出卖出信号。
        本质上是一种"逆向投资"策略——在恐慌时买入、在贪婪时卖出。

    适用范围:
        震荡市或波动较大的个股，不适合强单边趋势市场。
    """

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="rsi_reversal_v1",
            display_name="RSI 均值回归策略",
            version="1.0.0",
            category=StrategyCategory.TECHNICAL,
            description=(
                "基于 RSI 指标的均值回归策略。计算 RSI(默认14日)，"
                "当 RSI 跌破超卖阈值（默认30）时产生买入信号、预期超卖反弹；"
                "当 RSI 突破超买阈值（默认70）时产生卖出信号、预期超买回落。"
                "逆势交易逻辑，适合震荡行情，趋势市中需配合过滤条件使用。"
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.EVENT_ORDERS,
            params=PARAM_SCHEMA,
            max_position_pct=0.10,
            supported_position_modes=["equal_weight"],
            tags=["均值回归", "RSI", "逆向投资", "技术指标", "超买超卖"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        period = params.get("period", 14)
        oversold = params.get("oversold", 30)
        overbought = params.get("overbought", 70)
        if not isinstance(period, int) or period < 2:
            return False, "period 必须为 >=2 的整数"
        if not isinstance(oversold, int) or not isinstance(overbought, int):
            return False, "oversold 和 overbought 必须为整数"
        if oversold >= overbought:
            return False, "oversold 必须小于 overbought"
        if oversold < 0 or overbought > 100:
            return False, "阈值必须在 0-100 范围内"
        return True, ""

    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
    ) -> SignalDict:
        period: int = params.get("period", 14)
        oversold: int = params.get("oversold", 30)
        overbought: int = params.get("overbought", 70)
        min_score: float = params.get("min_score", 0.3)

        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        codes = self._extract_codes(pivot)

        signals: SignalDict = {}

        for code in codes:
            close = self._get_close_series(pivot, code)
            if close is None or len(close) < period + 1:
                continue

            # Keep public observations before the test/member-entry date for
            # time-series warmup; eligibility is enforced at the decision row.
            close = close.loc[:end].dropna()
            if len(close) < period + 1:
                continue

            # ── 纯 pandas 实现 RSI ──
            delta = close.diff()
            gain = delta.clip(lower=0)
            loss = (-delta).clip(lower=0)

            # 使用 Wilder's smoothing (EMA 等效)
            avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

            rs = avg_gain / (avg_loss.replace(0, np.nan))
            rsi = 100.0 - (100.0 / (1.0 + rs))

            # ── 信号生成: 检测区间穿越 ──
            # oversold 区间: RSI 从下方回升突破 oversold → BUY
            prev_rsi = rsi.shift(1)
            buy_condition = (prev_rsi < oversold) & (rsi >= oversold)
            # overbought 区间: RSI 从上方回落跌破 overbought → SELL
            sell_condition = (prev_rsi > overbought) & (rsi <= overbought)

            for d in rsi.index:
                if d < start or not code_is_eligible(code, d):
                    continue
                date_str = d.strftime("%Y-%m-%d")
                signals.setdefault(date_str, [])

                if buy_condition.get(d, False):
                    # 信号强度: RSI 越低反弹力度预期越大
                    strength = float((oversold - prev_rsi.get(d, oversold)) / oversold)
                    score = max(0.0, min(1.0, strength))
                    if score >= min_score:
                        signals[date_str].append(
                            SignalItem(code=code, action="BUY", score=score, weight=1.0)
                        )

                if sell_condition.get(d, False):
                    strength = float((prev_rsi.get(d, overbought) - overbought) / (100 - overbought))
                    score = max(0.0, min(1.0, strength))
                    if score >= min_score:
                        signals[date_str].append(
                            SignalItem(code=code, action="SELL", score=score, weight=1.0)
                        )

        return signals

    # ── 内部辅助 ─────────────────────────────────────────────────────

    @staticmethod
    def _extract_codes(pivot: pd.DataFrame) -> list[str]:
        """从 pivot 中提取股票代码列表."""
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
        """获取某只股票的收盘价序列."""
        if isinstance(pivot.columns, pd.MultiIndex):
            for field in ["close", "Close", "CLOSE", "收盘"]:
                if (code, field) in pivot.columns:
                    return pivot[(code, field)].copy()
        if "close" in pivot.columns:
            return pivot["close"].copy()
        if code in pivot.columns:
            return pivot[code].copy()
        return None
