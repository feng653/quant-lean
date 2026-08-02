"""
Run 3 factor/ML strategies with tuned params, backtest, and save results.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

print("=" * 60)
print("FACTOR/ML STRATEGY TUNING & BACKTEST")
print("=" * 60)

# ── 1. Load data ─────────────────────────────────────────────────────────
print("\n[1] Loading CSI500 parquet data...")
pivot_raw = pd.read_parquet("data/cache/daily/csi500.parquet")
print(f"    Shape: {pivot_raw.shape}, Stocks: {len(pivot_raw.columns)}")

# Transform to MultiIndex (code, "close") for strategy compatibility
pivot = pivot_raw.copy()
pivot.columns = pd.MultiIndex.from_product([pivot.columns, ["close"]])
print(f"    MultiIndex shape: {pivot.shape}")

TEST_START = "2023-07-01"
TEST_END = "2026-06-30"

# ── 2. Imports ───────────────────────────────────────────────────────────
from backend.core.engine import BacktestEngine
from backend.core.cost_model import CostModel
from backend.core.metrics import compute_all_metrics

# ── 3. Run each strategy ─────────────────────────────────────────────────
results = {}

CONFIGS = [
    {
        "id": "alphamaster_gbr_v1",
        "name": "AlphaMaster GBR",
        "params": {"n_estimators": 50, "max_depth": 3, "learning_rate": 0.05, "top_k": 30},
        "module": "backend.strategies.factor.alphamaster_gbr",
        "class": "AlphaMasterGBRStrategy",
    },
    {
        "id": "alpha158_lgb_v1",
        "name": "Alpha158 + LightGBM",
        "params": {"n_estimators": 50, "max_depth": 3, "learning_rate": 0.05, "top_k_pct": 0.1},
        "module": "backend.strategies.ml.alpha158_lgb",
        "class": "Alpha158LGBStrategy",
    },
    {
        "id": "alpha158_xgb_v1",
        "name": "Alpha158 + XGBoost",
        "params": {"n_estimators": 50, "max_depth": 3, "learning_rate": 0.05, "top_k_pct": 0.1},
        "module": "backend.strategies.ml.alpha158_xgb",
        "class": "Alpha158XGBStrategy",
    },
]

cm = CostModel(commission_rate=0.0003, slippage_rate=0.001, stamp_duty_rate=0.001)

for cfg in CONFIGS:
    sid = cfg["id"]
    print(f"\n{'=' * 60}")
    print(f"[{sid}] {cfg['name']}")
    print(f"{'=' * 60}")

    try:
        t0 = time.time()

        # Import and instantiate
        mod = __import__(cfg["module"], fromlist=[cfg["class"]])
        cls = getattr(mod, cfg["class"])
        strategy = cls()
        meta = strategy.metadata()
        print(f"    Strategy ID: {meta.strategy_id} | Category: {meta.category.value}")
        print(f"    Params: {cfg['params']}")

        # Generate signals
        print("    Generating signals (Walk-Forward)...")
        signals = strategy.generate_batch_signals(pivot, cfg["params"], TEST_START, TEST_END)

        num_signal_dates = len(signals)
        total_items = sum(len(v) for v in signals.values())
        buys = sum(1 for sigs in signals.values() for s in sigs if s.action.upper() == "BUY")
        print(f"    Signal dates: {num_signal_dates} | Items: {total_items} (BUY={buys})")

        t_gen = time.time() - t0
        print(f"    Signal generation took {t_gen:.1f}s")

        if num_signal_dates == 0:
            print("    WARNING: No signals generated, skipping backtest")
            results[sid] = {"sharpe": None, "return": None, "error": "No signals generated"}
            continue

        # Run backtest
        print("    Running backtest...")
        engine = BacktestEngine(
            initial_capital=1_000_000,
            cost_model=cm,
            start_date=TEST_START,
            end_date=TEST_END,
            max_positions=20,
        )
        result = engine.run(signals, pivot, strategy_id=sid)
        print(f"    Final equity: {result.final_equity:,.2f}")
        print(f"    Trades executed: {result.trades_executed}")

        # Compute metrics
        if not result.equity_curve.empty and "equity" in result.equity_curve.columns:
            metrics = compute_all_metrics(
                result.equity_curve, None, result.trade_log
            )
            sharpe = metrics.get("sharpe_ratio", 0.0)
            cum_return = metrics.get("cumulative_return", 0.0)
            max_dd = metrics.get("max_drawdown", 0.0)
            print(f"    Sharpe: {sharpe:.4f} | Cum Return: {cum_return:.4f} ({cum_return*100:.2f}%) | Max DD: {max_dd:.4f} ({max_dd*100:.2f}%)")
        else:
            sharpe, cum_return = 0.0, 0.0
            print("    WARNING: Empty equity curve")

        total_time = time.time() - t0
        print(f"    Total time: {total_time:.1f}s")

        results[sid] = {
            "sharpe": round(sharpe, 4) if sharpe is not None else None,
            "return": round(cum_return, 6) if cum_return is not None else None,
            "max_drawdown": round(max_dd, 6) if max_dd is not None else None,
            "signal_dates": num_signal_dates,
            "trades": result.trades_executed,
            "error": None,
        }

    except Exception as e:
        import traceback
        print(f"    ERROR: {e}")
        traceback.print_exc()
        results[sid] = {"sharpe": None, "return": None, "error": str(e)}

# ── 4. Save results ──────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print("SAVING RESULTS")
print(f"{'=' * 60}")

# Ensure tests directory exists
os.makedirs("tests", exist_ok=True)

with open("tests/tuned_factor_ml.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

print("\nResults saved to tests/tuned_factor_ml.json")
print(json.dumps(results, indent=2, ensure_ascii=False))

# ── 5. Summary ───────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print("SUMMARY")
print(f"{'=' * 60}")
for sid, r in results.items():
    err = r.get("error")
    if err:
        print(f"  {sid}: FAILED - {err}")
    else:
        print(f"  {sid}: Sharpe={r['sharpe']}, Return={r['return']} ({r.get('return',0)*100:.2f}%), Trades={r.get('trades',0)}")
