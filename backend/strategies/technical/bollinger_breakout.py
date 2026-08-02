"""布林带突破策略 —— 突破上轨买入、跌破中轨止损."""

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
        name="period",
        type="int",
        default=60,
        description="布林带计算周期（日）",
        min=10,
        max=60,
        step=1,
    ),
    ParamField(
        name="std_multiplier",
        type="float",
        default=2.0,
        description="标准差倍数（上/下轨宽度）",
        min=1.5,
        max=3.0,
        step=0.1,
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


class BollingerBreakoutStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_signal_state"

    """布林带突破策略（带中轨止损）。

    策略原理:
        布林带由三条线组成：中轨（N日均线）、上轨（中轨 + K倍标准差）、
        下轨（中轨 - K倍标准差）。价格突破上轨意味着波动率扩张且方向向上，
        可能开启一轮趋势，策略发出买入信号；价格回落跌破中轨说明上涨动能
        衰竭，策略发出卖出信号作为止损，保护已获利润。
        该策略在趋势市场中表现优异，但震荡市中可能产生较多假突破信号。

    适用范围:
        趋势明显的市场环境；波动率较大的个股。
    """

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="bollinger_breakout_v1",
            display_name="布林带突破策略",
            version="1.0.0",
            category=StrategyCategory.TECHNICAL,
            description=(
                "基于布林带的波动率突破策略。计算 N 日均线和上下 K 倍标准差轨道，"
                "收盘价突破上轨产生买入信号（波动扩张+方向确认），"
                "收盘价回落到中轨下方产生卖出信号（趋势衰竭止损）。"
                "中轨止损机制有效控制了回撤风险，适合趋势行情。"
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.EVENT_ORDERS,
            params=PARAM_SCHEMA,
            max_position_pct=0.10,
            supported_position_modes=["equal_weight"],
            tags=["布林带", "波动率突破", "趋势跟踪", "止损", "技术指标"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        period = params.get("period", 20)
        if not isinstance(period, int) or period < 5:
            return False, "period 必须为 >=5 的整数"
        std_mult = params.get("std_multiplier", 2.0)
        if not isinstance(std_mult, (int, float)) or std_mult <= 0:
            return False, "std_multiplier 必须为正数"
        return True, ""

    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
    ) -> SignalDict:
        period: int = params.get("period", 20)
        std_mult: float = params.get("std_multiplier", 2.0)
        min_score: float = params.get("min_score", 0.3)

        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        codes = self._extract_codes(pivot)

        signals: SignalDict = {}

        for code in codes:
            close = self._get_close_series(pivot, code)
            if close is None or len(close) < period + 1:
                continue

            close = close.loc[:end].dropna()
            if len(close) < period + 1:
                continue

            # ── 纯 pandas 计算布林带 ──
            mid_band = close.rolling(window=period, min_periods=period).mean()
            rolling_std = close.rolling(window=period, min_periods=period).std()
            upper_band = mid_band + std_mult * rolling_std
            lower_band = mid_band - std_mult * rolling_std

            # 剔除 NaN 区间
            valid = mid_band.notna()
            mid_band = mid_band[valid]
            upper_band = upper_band[valid]
            close_valid = close[valid]

            # ── 信号生成 ──

            # 持有状态跟踪（简单版：信号日重置状态）
            in_position = False

            for d in close_valid.index:
                if d < start:
                    continue
                if not code_is_eligible(code, d):
                    # Do not let an entry/exit outside the contemporaneous
                    # universe mutate state carried into a later eligible day.
                    in_position = False
                    continue
                date_str = d.strftime("%Y-%m-%d")
                signals.setdefault(date_str, [])

                c = close_valid[d]
                ub = upper_band[d]
                mb = mid_band[d]
                lb = lower_band[d]

                if not in_position:
                    # 突破上轨 → BUY
                    if c > ub:
                        # 信号强度: 突破幅度 / 轨道宽度
                        band_width = ub - lb
                        if band_width > 0:
                            strength = float((c - ub) / band_width)
                        else:
                            strength = 0.5
                        score = max(0.0, min(1.0, strength * 3))
                        if score >= min_score:
                            signals[date_str].append(
                                SignalItem(code=code, action="BUY", score=score, weight=1.0)
                            )
                        in_position = True
                else:
                    # 跌破中轨 → SELL (止损)
                    if c < mb:
                        strength = float((mb - c) / mb) if mb > 0 else 0.5
                        score = max(0.0, min(1.0, strength * 5))
                        if score >= min_score:
                            signals[date_str].append(
                                SignalItem(code=code, action="SELL", score=score, weight=1.0)
                            )
                        in_position = False

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
