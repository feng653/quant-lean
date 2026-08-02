"""
Direct backtest runner - bypasses job worker.
Loads data, runs strategy, saves results directly to DB.
"""
import sys, os, json, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
from backend.config import settings
from backend.core.engine import BacktestEngine
from backend.core.cost_model import CostModel
from backend.core.metrics import compute_all_metrics
from backend.strategies.registry import get_registry
import aiosqlite

async def run_experiment_direct(name, strategy_id, params, pool_id, test_start, test_end):
    """Run backtest and save results directly."""
    from backend.main import _init_databases
    await _init_databases()

    # Load data
    cache_path = settings.abs_path(f"data/cache/daily/{pool_id}.parquet")
    if not os.path.exists(cache_path):
        print(f"  ❌ Data not found: {cache_path}")
        return None

    pivot = pd.read_parquet(cache_path)
    print(f"  Data: {pivot.shape}, idx={type(pivot.index).__name__}, cols={type(pivot.columns).__name__}")

    # Get strategy (scan if needed)
    registry = get_registry()
    if not registry.list_all():
        strategies_dir = settings.PROJECT_ROOT / "backend" / "strategies"
        registry.scan_directory(strategies_dir)
        print(f"  Scanned: {len(registry.list_all())} strategies")
    try:
        strategy = registry.get_strategy(strategy_id)
    except KeyError:
        print(f"  ❌ Strategy not registered: {strategy_id}")
        return None

    # Generate signals
    try:
        signals = strategy.generate_batch_signals(pivot, params, test_start, test_end)
        total_sig = sum(len(v) for v in signals.values())
        print(f"  Signals: {len(signals)} dates, {total_sig} items")
    except Exception as e:
        print(f"  ❌ Signal generation failed: {e}")
        return None

    if total_sig == 0:
        print(f"  ⚠️  No signals generated - check params")
        # Verify _extract_codes works
        try:
            codes = strategy._extract_codes(pivot)
            print(f"  Debug: _extract_codes returned {len(codes)} codes: {codes[:3]}")
        except Exception as e2:
            print(f"  Debug: _extract_codes failed: {e2}")

    # Run backtest
    cm = CostModel(commission_rate=0.0003, slippage_rate=0.001, stamp_duty_rate=0.001)
    engine = BacktestEngine(initial_capital=1_000_000, cost_model=cm,
                            start_date=test_start, end_date=test_end, max_positions=20)
    result = engine.run(signals, pivot, strategy_id=strategy_id)
    print(f"  Trades: {len(result.trade_log)}, Final: {result.final_equity:,.0f}")

    # Compute metrics
    metrics = compute_all_metrics(result.equity_curve, None, result.trade_log)
    sharpe = metrics.get("sharpe_ratio", 0)
    ret = metrics.get("annualized_return", 0)
    print(f"  Sharpe: {sharpe:.4f}, Return: {ret*100:.2f}%")

    # Save to DB
    async with aiosqlite.connect(str(settings.abs_path(settings.EXPERIMENT_DB))) as db:
        params_hash = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()
        params_str = json.dumps(params, ensure_ascii=False)

        cursor = await db.execute("""
            INSERT INTO experiments (user_id, name, strategy_id, strategy_category,
                pool_preset, test_start, test_end, params, params_hash, mode,
                status, progress_pct, progress_message, created_at, completed_at)
            VALUES (2, ?, ?, 'technical', ?, ?, ?, ?, ?, 'batch',
                'completed', 100, '回测完成', datetime('now'), datetime('now'))
        """, (name, strategy_id, pool_id, test_start, test_end, params_str, params_hash))
        exp_id = cursor.lastrowid

        # Save equity curve
        if not result.equity_curve.empty and "equity" in result.equity_curve.columns:
            ec = result.equity_curve.reset_index().dropna(subset=["equity"])
            for _, row in ec.iterrows():
                await db.execute(
                    "INSERT INTO equity_curve (experiment_id, date, equity) VALUES (?, ?, ?)",
                    (exp_id, str(row["date"])[:10], float(row["equity"]))
                )

        # Save trade log
        for t in result.trade_log:
            await db.execute("""
                INSERT INTO trade_log (experiment_id, date, code, action, price, shares, amount, cost, signal_strategy, signal_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (exp_id, t.date, t.code, t.action, t.price, t.shares, t.amount, t.cost, t.signal_strategy, t.signal_score))

        # Save metrics (handle None/NaN)
        def _v(v, default=0.0):
            if v is None:
                return default
            try:
                return float(v) if not pd.isna(v) else default
            except (ValueError, TypeError):
                return default

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
            _v(_M.get("sharpe_ratio")),
            _v(_M.get("annualized_return")),
            _v(_M.get("max_drawdown")),
            _v(_M.get("annualized_volatility")),
            _v(_M.get("calmar_ratio")),
            _v(_M.get("sortino_ratio")),
            _v(_M.get("win_rate")),
            _v(_M.get("win_loss_ratio")),
            _v(_M.get("avg_trade_return")),
            _v(_M.get("total_trades"), 0),
            _v(_M.get("avg_holding_days")),
            _v(_M.get("turnover_rate")),
            _v(_M.get("information_ratio")),
            _v(_M.get("alpha")),
            _v(_M.get("beta")),
            _v(_M.get("tracking_error")),
            _v(_M.get("var_95")),
            _v(_M.get("cvar_95")),
            _v(_M.get("return_skewness")),
            _v(_M.get("return_kurtosis")),
            _v(_M.get("profit_factor")),
        ))
        await db.commit()

    print(f"  ✅ Saved as experiment #{exp_id}")
    return exp_id

STRATEGIES = [
    ("MA双均线交叉", "ma_cross_v1", {"fast_period": 5, "slow_period": 20, "min_score": 0.0}),
    ("MACD金叉", "macd_signal_v1", {"fast": 12, "slow": 26, "signal": 9, "min_score": 0.0}),
    ("布林带突破", "bollinger_breakout_v1", {"period": 20, "std_multiplier": 2.0, "min_score": 0.0}),
    ("RSI均值回归", "rsi_reversal_v1", {"period": 14, "oversold": 30, "overbought": 70, "min_score": 0.0}),
    ("风险平价", "risk_parity_v1", {"lookback": 63, "rebalance_frequency": "monthly", "min_score": 0.0}),
]

import asyncio

async def main():
    print("Direct backtest runner (bypasses job worker)")
    print("=" * 60)

    for name, sid, params in STRATEGIES:
        print(f"\n[{sid}] {name}")
        eid = await run_experiment_direct(name, sid, params, "csi500", "2023-07-01", "2026-06-30")

    print(f"\n{'='*60}")
    print("Done! Check results via API.")

if __name__ == "__main__":
    asyncio.run(main())
