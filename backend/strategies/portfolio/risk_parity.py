"""风险平价策略 —— 基于倒波动率加权的组合配置."""

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
from backend.strategies.research_context import eligible_codes_on

# ═══════════════════════════════════════════════════════════════════════════
# 参数 Schema
# ═══════════════════════════════════════════════════════════════════════════

PARAM_SCHEMA: list[ParamField] = [
    ParamField(
        name="lookback",
        type="int",
        default=63,
        description="波动率回看窗口（日）",
        min=30,
        max=252,
        step=1,
    ),
    ParamField(
        name="rebalance_frequency",
        type="choice",
        default="monthly",
        description="再平衡频率",
        choices=["monthly", "weekly"],
    ),
    ParamField(
        name="min_score",
        type="float",
        default=0.0,
        description="最低信号分数阈值（低于此值的权重忽略）",
        min=0.0,
        max=1.0,
        step=0.05,
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# 策略实现
# ═══════════════════════════════════════════════════════════════════════════


class RiskParityStrategy(StrategyProtocol):
    point_in_time_context_capability = "dated_portfolio_allocation"

    """风险平价策略（倒波动率加权）。

    策略原理:
        风险平价的核心思想是让每只股票对组合的风险贡献相等，而非仓位相等。
        首先计算每只股票过去 N 个交易日的日收益率波动率；
        然后按"倒波动率"分配权重：波动率越小的股票获得越大的权重，
        因为需要更多仓位才能贡献同等风险；波动率越大的股票权重越小。
        该策略自动偏向低波动股票，在震荡市中降低组合回撤。
        每月（或每周）重新计算权重并调仓。

    注意:
        这是一个资产配置框架，而非选股策略。它不做 alpha 选股，
        而是对已有的股票池进行风险预算分配。

    适用范围:
        多资产组合配置，震荡市防御性组合管理。
    """

    @classmethod
    def metadata(cls) -> StrategyMetadata:
        return StrategyMetadata(
            strategy_id="risk_parity_v1",
            display_name="风险平价策略",
            version="1.0.0",
            category=StrategyCategory.PORTFOLIO,
            description=(
                "基于倒波动率加权的风险平价组合策略。计算每只股票过去 N 日的"
                "日收益率波动率，按 1/波动率 进行权重分配，使得每只股票对组合的"
                "风险贡献相等。波动率越低的股票权重越大（需更多仓位才能贡献同等风险），"
                "月度再平衡。本质是风险预算分配框架，自动偏向低波动股票，降低组合回撤。"
            ),
            supported_modes=[StrategyMode.BATCH],
            requires_training=False,
            retrain_frequency=RetrainFrequency.NEVER,
            estimated_training_seconds=0,
            portfolio_signal_mode=PortfolioSignalMode.TARGET_WEIGHTS,
            params=PARAM_SCHEMA,
            max_position_pct=0.20,
            supported_position_modes=["risk_parity"],
            tags=["风险平价", "组合优化", "低波动", "资产配置", "再平衡", "风险管理"],
        )

    def validate_params(self, params: dict) -> tuple[bool, str]:
        lookback = params.get("lookback", 63)
        if not isinstance(lookback, int) or lookback < 5:
            return False, "lookback 必须为 >=5 的整数"
        freq = params.get("rebalance_frequency", "monthly")
        if freq not in ("monthly", "weekly"):
            return False, "rebalance_frequency 必须为 monthly 或 weekly"
        return True, ""

    def generate_batch_signals(
        self,
        pivot: pd.DataFrame,
        params: dict,
        start_date: str,
        end_date: str,
    ) -> SignalDict:
        lookback: int = params.get("lookback", 63)
        freq: str = params.get("rebalance_frequency", "monthly")
        min_score: float = params.get("min_score", 0.1)

        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        codes = self._extract_codes(pivot)

        if not codes:
            return {}

        # 确保索引为 DatetimeIndex
        if not isinstance(pivot.index, pd.DatetimeIndex):
            pivot = pivot.copy()
            pivot.index = pd.to_datetime(pivot.index)

        # 确定调仓日期
        all_dates = pivot.index[pivot.index >= start]
        all_dates = all_dates[all_dates <= end]
        if len(all_dates) == 0:
            return {}

        rebalance_dates = self._get_rebalance_dates(all_dates, freq)

        signals: SignalDict = {}

        for rd in rebalance_dates:
            # 回看窗口必须包含测试开始日前的历史，不能只在测试区间内截取。
            hist_dates = pivot.index[pivot.index <= rd][-lookback:]

            # 计算每只股票的波动率
            volatilities: dict[str, float] = {}
            eligible = eligible_codes_on(rd)
            allocation_codes = (
                codes
                if eligible is None
                else [code for code in codes if code in eligible]
            )
            for code in allocation_codes:
                close = self._get_close_series(pivot, code)
                if close is None:
                    continue
                # 截取回看窗口内的数据
                hist_close = close.reindex(hist_dates).dropna()
                if len(hist_close) < max(10, lookback // 3):
                    continue

                returns = hist_close.pct_change().dropna()
                if len(returns) < 5:
                    continue

                vol = float(returns.std())
                if vol > 1e-10:
                    volatilities[code] = vol

            if not volatilities:
                continue

            # ── 倒波动率加权 ──
            inv_vol = {code: 1.0 / vol for code, vol in volatilities.items()}
            total_inv_vol = sum(inv_vol.values())
            if total_inv_vol <= 0:
                continue

            date_str = rd.strftime("%Y-%m-%d")
            signals.setdefault(date_str, [])

            for code, iv in inv_vol.items():
                weight = iv / total_inv_vol  # 权重
                score = weight  # 信号分数即权重

                if score >= min_score:
                    signals[date_str].append(
                        SignalItem(
                            code=code,
                            action="BUY",  # 风险平价产生持仓权重信号
                            score=score,
                            weight=weight,
                        )
                    )

        return signals

    # ── 内部辅助 ─────────────────────────────────────────────────────

    @staticmethod
    def _get_rebalance_dates(
        dates: pd.DatetimeIndex, freq: str
    ) -> pd.DatetimeIndex:
        """根据频率获取调仓日期."""
        freq_map = {
            "monthly": "M",
            "weekly": "W",
        }
        offset_alias = freq_map.get(freq, "M")
        # 取每个周期的最后一个交易日
        group_key = dates.to_period(offset_alias)
        last_per_group = dates.to_series().groupby(group_key).last()
        return pd.DatetimeIndex(last_per_group.values)

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
