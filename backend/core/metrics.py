"""36项量化评估指标 —— 基于日净值序列计算."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

# 波动率下限：低于此值时夏普/索提诺等比率置0，避免除零放大
VOL_FLOOR: float = 1e-6

# 年化交易日
TRADING_DAYS_PER_YEAR: int = 252


def compute_all_metrics(
    equity_curve: pd.DataFrame,
    benchmark_equity: pd.Series | pd.DataFrame | None = None,
    trade_log: list | None = None,
    risk_free_rate: float = 0.03,
) -> dict[str, Any]:
    """一站式计算全部 36+ 项评估指标。

    Args:
        equity_curve: 日净值序列，至少含 'equity' 列（index 为日期）。
        benchmark_equity: 基准净值序列（可选）。若为 DataFrame，取第一列。
        trade_log: 成交记录列表（可选），用于计算换手率、胜率等交易维度指标。
        risk_free_rate: 无风险年化利率（默认 3%）。

    Returns:
        指标字典，键为指标名，值为 float / int / None。
    """
    metrics: dict[str, Any] = {}

    if equity_curve.empty or "equity" not in equity_curve.columns:
        return {"error": "equity_curve is empty or missing 'equity' column"}

    eq = equity_curve["equity"].dropna()
    if len(eq) < 2:
        return {"error": "insufficient data points"}

    # ── 日收益率 ──
    daily_returns = eq.pct_change().dropna()

    # ── 基准日收益率 ──
    bench_returns: pd.Series | None = None
    aligned_strategy_returns: pd.Series | None = None
    if benchmark_equity is not None:
        if isinstance(benchmark_equity, pd.DataFrame):
            bench = benchmark_equity.iloc[:, 0]
        else:
            bench = benchmark_equity
        # 对齐日期
        bench = bench.reindex(eq.index).ffill().dropna()
        candidate_benchmark_returns = bench.pct_change().dropna()
        common_idx = daily_returns.index.intersection(
            candidate_benchmark_returns.index
        )
        # 相对指标要求覆盖完整的策略收益窗口；部分基准绝不能反向改变
        # 策略自身的年化收益、波动率和 Sharpe 等绝对指标。
        if len(common_idx) == len(daily_returns) and len(common_idx) > 1:
            aligned_strategy_returns = daily_returns.loc[common_idx]
            bench_returns = candidate_benchmark_returns.loc[common_idx]

    n_days = len(daily_returns)
    n_years = n_days / TRADING_DAYS_PER_YEAR

    # ── 1-4: 收益类 ──────────────────────────────────────────────────
    cumulative_return = (eq.iloc[-1] / eq.iloc[0]) - 1
    annualized_return = (1 + cumulative_return) ** (1 / max(n_years, 1e-6)) - 1
    total_return = cumulative_return

    avg_daily_return = float(daily_returns.mean())
    daily_vol = float(daily_returns.std())

    metrics["cumulative_return"] = float(cumulative_return)
    metrics["annualized_return"] = float(annualized_return)
    metrics["total_return"] = float(total_return)
    metrics["avg_daily_return"] = avg_daily_return

    # ── 5-7: 波动率 ──────────────────────────────────────────────────
    annualized_vol = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)
    metrics["daily_volatility"] = daily_vol
    metrics["annualized_volatility"] = annualized_vol
    metrics["upside_volatility"] = float(
        daily_returns[daily_returns > 0].std() * math.sqrt(TRADING_DAYS_PER_YEAR)
    )

    # ── 8-9: 最大回撤 ────────────────────────────────────────────────
    running_max = eq.cummax()
    drawdown = (eq - running_max) / running_max
    max_drawdown = float(drawdown.min())
    metrics["max_drawdown"] = max_drawdown

    # 最大回撤持续天数
    drawdown_duration = _max_drawdown_duration(eq)
    metrics["max_drawdown_duration"] = drawdown_duration

    # 回撤恢复时间（从最大回撤起点到回到前期高点）
    dd_peak_idx = drawdown.idxmin()
    dd_start = running_max.loc[:dd_peak_idx]
    if not dd_start.empty:
        dd_start_date = dd_start.idxmax()
        recovery = _recovery_days(eq, dd_start_date, running_max.loc[dd_start_date])
        metrics["max_drawdown_recovery_days"] = recovery

    # ── 10-16: 风险调整收益 ──────────────────────────────────────────
    rf_daily = risk_free_rate / TRADING_DAYS_PER_YEAR
    excess_daily = daily_returns - rf_daily

    if daily_vol > VOL_FLOOR:
        sharpe = float(excess_daily.mean() / daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR))
    else:
        sharpe = 0.0
    metrics["sharpe_ratio"] = sharpe

    # Sortino
    downside_std = float(daily_returns[daily_returns < 0].std())
    if downside_std > VOL_FLOOR:
        sortino = float(excess_daily.mean() / downside_std * math.sqrt(TRADING_DAYS_PER_YEAR))
    else:
        sortino = 0.0
    metrics["sortino_ratio"] = sortino

    # Calmar
    if abs(max_drawdown) > VOL_FLOOR:
        calmar = float(annualized_return / abs(max_drawdown))
    else:
        calmar = 0.0
    metrics["calmar_ratio"] = calmar

    # Information Ratio (vs benchmark)
    if (
        bench_returns is not None
        and aligned_strategy_returns is not None
        and len(bench_returns) > 1
    ):
        active_returns = aligned_strategy_returns - bench_returns
        active_std = float(active_returns.std())
        if active_std > VOL_FLOOR:
            info_ratio = float(
                active_returns.mean() / active_std * math.sqrt(TRADING_DAYS_PER_YEAR)
            )
        else:
            info_ratio = 0.0
        metrics["information_ratio"] = info_ratio
        metrics["tracking_error"] = float(active_std * math.sqrt(TRADING_DAYS_PER_YEAR))
    else:
        metrics["information_ratio"] = None
        metrics["tracking_error"] = None

    # Omega Ratio
    threshold = 0.0
    gains = float(daily_returns[daily_returns > threshold].sum())
    losses = float(abs(daily_returns[daily_returns <= threshold].sum()))
    metrics["omega_ratio"] = gains / max(losses, VOL_FLOOR)

    # Tail Ratio (95% VaR)
    var_95 = float(np.percentile(daily_returns, 5))
    cvar_95 = float(daily_returns[daily_returns <= var_95].mean())
    metrics["var_95"] = var_95
    metrics["cvar_95"] = cvar_95

    # ── 17-18: 基准相关 ──────────────────────────────────────────────
    if (
        bench_returns is not None
        and aligned_strategy_returns is not None
        and len(bench_returns) > 1
    ):
        aligned = pd.concat(
            [aligned_strategy_returns, bench_returns], axis=1
        ).dropna()
        aligned.columns = ["strategy", "benchmark"]
        bench_cum = (1 + aligned["benchmark"]).cumprod()
        benchmark_return = float(bench_cum.iloc[-1] - 1)
        benchmark_years = len(aligned) / TRADING_DAYS_PER_YEAR
        benchmark_ann = float(
            (1 + benchmark_return) ** (1 / max(benchmark_years, 1e-6)) - 1
        )
        metrics["benchmark_return"] = benchmark_return
        metrics["benchmark_annualized"] = benchmark_ann

        # Beta & Alpha
        cov_matrix = np.cov(aligned["strategy"], aligned["benchmark"])
        beta = float(cov_matrix[0, 1] / max(cov_matrix[1, 1], VOL_FLOOR))
        alpha = float(
            (aligned["strategy"].mean() - rf_daily) - beta * (aligned["benchmark"].mean() - rf_daily)
        ) * TRADING_DAYS_PER_YEAR
        metrics["beta"] = beta
        metrics["alpha"] = alpha

        # 相关系数
        metrics["correlation"] = float(aligned["strategy"].corr(aligned["benchmark"]))
        metrics["r_squared"] = float(aligned["strategy"].corr(aligned["benchmark"]) ** 2)
    else:
        metrics["benchmark_return"] = None
        metrics["benchmark_annualized"] = None
        metrics["beta"] = None
        metrics["alpha"] = None
        metrics["correlation"] = None
        metrics["r_squared"] = None

    # ── 19-24: 交易维度（需要有 trade_log）───────────────────────────
    if trade_log:
        _compute_trade_metrics(trade_log, eq, metrics)
    else:
        for key in (
            "total_trades",
            "win_rate",
            "avg_win",
            "avg_loss",
            "profit_factor",
            "avg_holding_days",
            "turnover_rate",
            "avg_trade_return",
            "max_consecutive_wins",
            "max_consecutive_losses",
            "expectency",
        ):
            metrics.setdefault(key, None)

    # ── 25: 盈亏比 ───────────────────────────────────────────────────
    # FIXED: reviewer issue #13 — 修复 avg_loss 为 None 时的除零风险
    avg_win = metrics.get("avg_win")
    avg_loss = metrics.get("avg_loss")
    if avg_loss is not None and avg_loss != 0 and avg_win is not None:
        metrics["win_loss_ratio"] = abs(avg_win / avg_loss)
    else:
        metrics["win_loss_ratio"] = None

    # ── 26-28: 胜率相关 ──────────────────────────────────────────────
    if "win_rate" in metrics and metrics["win_rate"] is not None:
        wr = metrics["win_rate"]  # 0~1
        # Kelly 仓位 (简化版)
        if metrics.get("win_loss_ratio"):
            kelly = wr - (1 - wr) / metrics["win_loss_ratio"]
            metrics["kelly_fraction"] = max(0.0, float(kelly))
        else:
            metrics["kelly_fraction"] = None
    else:
        metrics["kelly_fraction"] = None

    # ── 29-30: Calmar 补充 ───────────────────────────────────────────
    metrics["mar_ratio"] = metrics.get("calmar_ratio")  # 别名
    metrics["return_over_max_drawdown"] = metrics.get("calmar_ratio")

    # ── 31-33: 稳定性 ────────────────────────────────────────────────
    # 滚动夏普标准差
    if n_days >= 63:
        roll_sharpe = daily_returns.rolling(63).apply(
            lambda x: (x.mean() - rf_daily) / max(x.std(), VOL_FLOOR) * math.sqrt(TRADING_DAYS_PER_YEAR)
        ).dropna()
        metrics["sharpe_stability"] = float(roll_sharpe.std())
    else:
        metrics["sharpe_stability"] = None

    # 月度收益胜率
    # Pandas 3 removed the legacy "M" alias, while pandas 2.0/2.1 do not know
    # the replacement "ME" alias yet. Keep both supported by the declared
    # pandas>=2.0 dependency range.
    try:
        monthly_groups = daily_returns.resample("ME")
    except ValueError:  # pragma: no cover - exercised on pandas 2.0/2.1
        monthly_groups = daily_returns.resample("M")
    monthly = monthly_groups.apply(lambda x: (1 + x).prod() - 1).dropna()
    if len(monthly) > 0:
        metrics["monthly_win_rate"] = float((monthly > 0).mean())
        metrics["best_month"] = float(monthly.max())
        metrics["worst_month"] = float(monthly.min())
    else:
        metrics["monthly_win_rate"] = None
        metrics["best_month"] = None
        metrics["worst_month"] = None

    # ── 34-36: 其他 ──────────────────────────────────────────────────
    # 正收益天数比例
    metrics["positive_day_ratio"] = float((daily_returns > 0).mean())

    # 偏度 & 峰度
    metrics["return_skewness"] = float(daily_returns.skew())
    metrics["return_kurtosis"] = float(daily_returns.kurtosis())

    # 净值终值
    metrics["final_equity"] = float(eq.iloc[-1])
    metrics["initial_equity"] = float(eq.iloc[0])
    metrics["n_days"] = n_days
    metrics["n_years"] = round(n_years, 2)

    return metrics


# ═══════════════════════════════════════════════════════════════════════════
# 内部辅助
# ═══════════════════════════════════════════════════════════════════════════


def _max_drawdown_duration(eq: pd.Series) -> int:
    """计算最大回撤持续交易日数。"""
    running_max = eq.cummax()
    in_drawdown = eq < running_max
    if not in_drawdown.any():
        return 0
    # 找出最长的连续 True 段
    durations = in_drawdown.astype(int).groupby(
        (in_drawdown != in_drawdown.shift()).cumsum()
    ).cumsum()
    return int(durations.max()) if not durations.empty else 0


def _recovery_days(
    eq: pd.Series, start_date: pd.Timestamp, peak_value: float
) -> int | None:
    """计算从回撤起点恢复到前期高点所需交易日数。"""
    after = eq.loc[start_date:]
    recovered = after[after >= peak_value]
    if recovered.empty:
        return None
    recovery_date = recovered.index[0]
    return len(eq.loc[start_date:recovery_date]) - 1


def _compute_trade_metrics(
    trade_log: list,
    equity: pd.Series,
    metrics: dict[str, Any],
) -> None:
    """从成交记录中提取交易维度指标。"""
    # 按股票逐笔 FIFO 配对。买方成本进入持仓成本，卖方成本从收入扣除，
    # 因此胜率、盈亏因子和收益率全部使用真实净额而非裸成交价。
    trades: list[float] = []
    trade_returns: list[float] = []
    holding_days_list: list[int] = []
    traded_amount = 0.0
    buy_queue: dict[
        str,
        list[tuple[pd.Timestamp, float, int]],
    ] = {}

    for t in sorted(trade_log, key=lambda x: x.date):
        traded_amount += abs(float(t.amount))
        t_date = pd.Timestamp(t.date)
        if t.action.upper() == "BUY":
            if t.shares <= 0:
                continue
            buy_cash_outflow = float(t.amount) + max(float(t.cost), 0.0)
            buy_cost_per_share = buy_cash_outflow / t.shares
            buy_queue.setdefault(t.code, []).append(
                (t_date, buy_cost_per_share, t.shares)
            )
        elif t.action.upper() == "SELL":
            if (
                t.shares <= 0
                or t.code not in buy_queue
                or not buy_queue[t.code]
            ):
                continue
            sell_shares = t.shares
            sell_cash_inflow = float(t.amount) - max(float(t.cost), 0.0)
            sell_net_per_share = sell_cash_inflow / t.shares
            while sell_shares > 0 and buy_queue[t.code]:
                buy_date, buy_cost_per_share, buy_shares = (
                    buy_queue[t.code][0]
                )
                matched = min(buy_shares, sell_shares)
                invested = buy_cost_per_share * matched
                pnl = (
                    sell_net_per_share - buy_cost_per_share
                ) * matched
                trades.append(pnl)
                trade_returns.append(pnl / invested if invested > 0 else 0.0)
                holding_days = (t_date - buy_date).days
                holding_days_list.append(max(holding_days, 1))
                sell_shares -= matched
                if buy_shares > matched:
                    buy_queue[t.code][0] = (
                        buy_date,
                        buy_cost_per_share,
                        buy_shares - matched,
                    )
                else:
                    buy_queue[t.code].pop(0)

    total_trades = len(trades)
    metrics["total_trades"] = total_trades

    if total_trades > 0:
        wins = [p for p in trades if p > 0]
        losses = [p for p in trades if p <= 0]
        metrics["win_rate"] = len(wins) / total_trades
        metrics["avg_win"] = float(np.mean(wins)) if wins else 0.0
        metrics["avg_loss"] = float(np.mean(losses)) if losses else 0.0
        gross_profit = float(sum(wins))
        gross_loss = float(abs(sum(losses)))
        metrics["profit_factor"] = (
            gross_profit / gross_loss
            if gross_loss > 0
            else (float("inf") if gross_profit > 0 else 0.0)
        )
        metrics["avg_holding_days"] = float(np.mean(holding_days_list))
        metrics["avg_trade_return"] = float(np.mean(trade_returns))
        metrics["expectency"] = float(np.mean(trades))

        consecutive_wins = consecutive_losses = 0
        max_wins = max_losses = 0
        for pnl in trades:
            if pnl > 0:
                consecutive_wins += 1
                consecutive_losses = 0
            else:
                consecutive_losses += 1
                consecutive_wins = 0
            max_wins = max(max_wins, consecutive_wins)
            max_losses = max(max_losses, consecutive_losses)
        metrics["max_consecutive_wins"] = max_wins
        metrics["max_consecutive_losses"] = max_losses
    else:
        metrics["win_rate"] = 0.0
        metrics["avg_win"] = 0.0
        metrics["avg_loss"] = 0.0
        metrics["profit_factor"] = 0.0
        metrics["avg_holding_days"] = 0
        metrics["avg_trade_return"] = 0.0
        metrics["expectency"] = 0.0
        metrics["max_consecutive_wins"] = 0
        metrics["max_consecutive_losses"] = 0

    average_equity = float(equity.mean()) if len(equity) else 0.0
    years = max((pd.Timestamp(equity.index[-1]) - pd.Timestamp(equity.index[0])).days / 365.25, 1 / 252)
    metrics["turnover_rate"] = (
        float(traded_amount / average_equity / years)
        if average_equity > 0
        else None
    )
