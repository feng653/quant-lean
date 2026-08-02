"""
Phase C: Run all 10 strategies backtest with tuned params.
"""
import sys, os, json, hashlib, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np
from backend.config import settings
from backend.core.engine import BacktestEngine
from backend.core.cost_model import CostModel
from backend.core.metrics import compute_all_metrics
from backend.strategies.registry import get_registry
import aiosqlite

# ── Setup ────────────────────────────────────────────────────────────────────

pivot = pd.read_parquet(str(settings.abs_path("data/cache/daily/csi500.parquet")))
print(f"Data: {pivot.shape}, dates: {len(pivot)}")

registry = get_registry()
registry.scan_directory(str(settings.PROJECT_ROOT / "backend" / "strategies"))
print(f"Strategies: {len(registry.list_all())}")

TEST_START = "2023-07-01"
TEST_END = "2026-06-30"

# ── 10 strategies with tuned params ─────────────────────────────────────────

STRATEGIES = [
    # ── Technical (no training) ──
    {"id": "ma_cross_v1", "name": "MA双均线交叉",
     "params": {"fast_period": 5, "slow_period": 20, "min_score": 0.0}},
    {"id": "macd_signal_v1", "name": "MACD金叉",
     "params": {"fast": 12, "slow": 26, "signal": 9, "min_score": 0.0}},
    {"id": "bollinger_breakout_v1", "name": "布林带突破",
     "params": {"period": 20, "std_multiplier": 2.0, "min_score": 0.0}},
    {"id": "rsi_reversal_v1", "name": "RSI均值回归",
     "params": {"period": 14, "oversold": 30, "overbought": 70, "min_score": 0.0}},
    {"id": "risk_parity_v1", "name": "风险平价",
     "params": {"lookback": 63, "rebalance_frequency": "monthly", "min_score": 0.0}},
    # ── Factor (close-only, no volume → some factors will be NaN) ──
    {"id": "alphamaster_gbr_v1", "name": "AlphaMaster GBR",
     "params": {"n_estimators": 100, "max_depth": 3, "learning_rate": 0.05, "top_k": 30},
     "requires_training": True},
    # ── ML (close-only, simplified) ──
    {"id": "alpha158_lgb_v1", "name": "Alpha158+LightGBM",
     "params": {"n_estimators": 100, "max_depth": 4, "learning_rate": 0.05, "top_k_pct": 0.1},
     "requires_training": True},
    {"id": "alpha158_xgb_v1", "name": "Alpha158+XGBoost",
     "params": {"n_estimators": 100, "max_depth": 4, "learning_rate": 0.05, "top_k_pct": 0.1},
     "requires_training": True},
    {"id": "lstm_rank_v1", "name": "LSTM深度学习排序",
     "params": {"seq_len": 30, "hidden_size": 32, "num_layers": 1, "epochs": 5, "top_k_pct": 0.1},
     "requires_training": True},
    {"id": "transformer_rank_v1", "name": "Transformer排序",
     "params": {"seq_len": 30, "hidden_size": 32, "num_layers": 1, "nhead": 4, "epochs": 3, "top_k_pct": 0.1},
     "requires_training": True},
]


async def save_result(exp_name, strategy_id, params, result, metrics):
    """Save backtest result to DB."""
    async with aiosqlite.connect(str(settings.abs_path(settings.EXPERIMENT_DB))) as db:
        params_hash = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()
        params_str = json.dumps(params, ensure_ascii=False)

        cursor = await db.execute("""
            INSERT INTO experiments (user_id, name, strategy_id, strategy_category,
                pool_preset, test_start, test_end, params, params_hash, mode,
                status, progress_pct, progress_message, created_at, completed_at)
            VALUES (2, ?, ?, 'technical', 'csi500', ?, ?, ?, ?, 'batch',
                'completed', 100, '回测完成', datetime('now'), datetime('now'))
        """, (exp_name, strategy_id, TEST_START, TEST_END, params_str, params_hash))
        exp_id = cursor.lastrowid

        # Equity curve
        if not result.equity_curve.empty and "equity" in result.equity_curve.columns:
            ec = result.equity_curve.reset_index().dropna(subset=["equity"])
            for _, row in ec.iterrows():
                await db.execute(
                    "INSERT INTO equity_curve (experiment_id, date, equity) VALUES (?, ?, ?)",
                    (exp_id, str(row["date"])[:10], float(row["equity"]))
                )

        # Trade log
        for t in result.trade_log:
            await db.execute("""
                INSERT INTO trade_log (experiment_id, date, code, action, price, shares, amount, cost, signal_strategy, signal_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (exp_id, t.date, t.code, t.action, t.price, t.shares, t.amount, t.cost, t.signal_strategy, t.signal_score))

        # Metrics
        def _v(v, d=0.0):
            try: return float(v) if v is not None and not pd.isna(v) else d
            except: return d

        _M = metrics
        await db.execute("""
            INSERT INTO experiment_metrics (experiment_id, sharpe_ratio, annual_return, max_drawdown,
                volatility, calmar_ratio, sortino_ratio, win_rate, profit_loss_ratio,
                avg_trade_return, total_trades, avg_holding_days, turnover_rate,
                information_ratio, alpha, beta, tracking_error, var_95, cvar_95,
                skewness, kurtosis, profit_factor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            exp_id,
            _v(_M.get("sharpe_ratio")), _v(_M.get("annualized_return")),
            _v(_M.get("max_drawdown")), _v(_M.get("annualized_volatility")),
            _v(_M.get("calmar_ratio")), _v(_M.get("sortino_ratio")),
            _v(_M.get("win_rate")), _v(_M.get("win_loss_ratio")),
            _v(_M.get("avg_trade_return")), _v(_M.get("total_trades"), 0),
            _v(_M.get("avg_holding_days")), _v(_M.get("turnover_rate")),
            _v(_M.get("information_ratio")), _v(_M.get("alpha")),
            _v(_M.get("beta")), _v(_M.get("tracking_error")),
            _v(_M.get("var_95")), _v(_M.get("cvar_95")),
            _v(_M.get("return_skewness")), _v(_M.get("return_kurtosis")),
            _v(_M.get("profit_factor")),
        ))
        await db.commit()
    return exp_id


async def main():
    # Clean DB
    conn = aiosqlite.connect(str(settings.abs_path(settings.EXPERIMENT_DB)))
    c = await conn
    for t in ["experiment_metrics", "equity_curve", "trade_log", "model_artifacts",
              "sweep_experiments", "param_sweeps", "experiments"]:
        await c.execute(f"DELETE FROM {t}")
    await c.commit()
    await c.close()
    print("DB cleaned.\n")

    cm = CostModel(commission_rate=0.0003, slippage_rate=0.001, stamp_duty_rate=0.001)
    results = []

    for cfg in STRATEGIES:
        sid = cfg["id"]
        name = cfg["name"]
        params = cfg["params"]
        req_train = cfg.get("requires_training", False)

        print(f"[{sid}] {name}", end=" ")

        try:
            strategy = registry.get_strategy(sid)
        except KeyError:
            print("❌ not registered")
            continue

        # Generate signals
        try:
            if req_train:
                # Train first, then generate signals
                try:
                    trained = strategy.train(pivot, params, "2022-01-01", "2023-06-30")
                    # Store model and generate signals with it
                    signals = strategy.generate_batch_signals(pivot, params, TEST_START, TEST_END)
                except NotImplementedError:
                    # Try directly without training
                    signals = strategy.generate_batch_signals(pivot, params, TEST_START, TEST_END)
            else:
                signals = strategy.generate_batch_signals(pivot, params, TEST_START, TEST_END)

            total_sig = sum(len(v) for v in signals.values())
            active_dates = sum(1 for v in signals.values() if v)
            print(f"→ {active_dates} dates, {total_sig} signals", end=" ")
        except Exception as e:
            print(f"❌ signal error: {type(e).__name__}: {e}")
            continue

        # Run backtest
        engine = BacktestEngine(initial_capital=1_000_000, cost_model=cm,
                                start_date=TEST_START, end_date=TEST_END, max_positions=20)
        result = engine.run(signals, pivot, strategy_id=sid)
        print(f"→ {len(result.trade_log)} trades, final={result.final_equity:,.0f}", end=" ")

        # Metrics
        metrics = compute_all_metrics(result.equity_curve, None, result.trade_log)
        s = metrics.get("sharpe_ratio", 0) or 0
        r = metrics.get("annualized_return", 0) or 0
        print(f"→ Sharpe={s:.2f}, AnnRet={r*100:.1f}%")

        # Save
        eid = await save_result(name, sid, params, result, metrics)
        results.append({
            "id": eid, "name": name, "sid": sid,
            "sharpe": s, "return": r,
            "mdd": metrics.get("max_drawdown", 0) or 0,
            "win_rate": metrics.get("win_rate", 0) or 0,
            "trades": len(result.trade_log),
            "signals": total_sig,
        })

    # ── Final Report ──
    time.sleep(0.5)
    print(f"\n{'='*85}")
    print("📊  十大策略近3年回测排名 (2023-07-01 ~ 2026-06-30)")
    print(f"{'='*85}")
    print(f"{'Rank':>4} {'Strategy':<22} {'Sharpe':<8} {'年化':<9} {'最大回撤':<9} {'胜率':<7} {'交易':<6} {'信号':<6}")
    print("-" * 85)

    results.sort(key=lambda x: x["sharpe"], reverse=True)
    for i, r in enumerate(results, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f" {i}.")
        print(f"{medal:>4} {r['sid']:<22} {r['sharpe']:>7.2f}  {r['return']*100:>7.1f}%"
              f"  {r['mdd']*100:>7.1f}%  {r['win_rate']*100:>5.0f}%"
              f"  {r['trades']:>5}  {r['signals']:>5}")
    print(f"{'='*85}")

if __name__ == "__main__":
    asyncio.run(main())
