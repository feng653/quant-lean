"""
Fine-tune MACD and RSI strategies by grid-searching parameter variations.
Saves best results to tests/tuned_macd_rsi.json.
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
from backend.config import settings
from backend.core.engine import BacktestEngine
from backend.core.cost_model import CostModel
from backend.core.metrics import compute_all_metrics
from backend.strategies.registry import get_registry

# ── Setup ────────────────────────────────────────────────────────────────────

pivot = pd.read_parquet(str(settings.abs_path("data/cache/daily/csi500.parquet")))
print(f"Data: {pivot.shape}, dates: {len(pivot)}, "
      f"range: {pivot.index[0].strftime('%Y-%m-%d')} ~ {pivot.index[-1].strftime('%Y-%m-%d')}")

registry = get_registry()
registry.scan_directory(str(settings.PROJECT_ROOT / "backend" / "strategies"))
print(f"Strategies registered: {len(registry.list_all())}")

TEST_START = "2023-07-01"
TEST_END = "2026-06-30"

cm = CostModel(commission_rate=0.0003, slippage_rate=0.001, stamp_duty_rate=0.001)


# ── Helper ───────────────────────────────────────────────────────────────────

def run_trial(strategy_id, params):
    """Run a single backtest trial and return metrics dict."""
    strategy = registry.get_strategy(strategy_id)
    signals = strategy.generate_batch_signals(pivot, params, TEST_START, TEST_END)

    total_sigs = sum(len(v) for v in signals.values())
    active_dates = sum(1 for v in signals.values() if v)

    engine = BacktestEngine(
        initial_capital=1_000_000, cost_model=cm,
        start_date=TEST_START, end_date=TEST_END, max_positions=20,
    )
    result = engine.run(signals, pivot, strategy_id=strategy_id)
    metrics = compute_all_metrics(result.equity_curve, None, result.trade_log)

    return {
        "sharpe": metrics.get("sharpe_ratio", 0) or 0,
        "return": metrics.get("annualized_return", 0) or 0,
        "mdd": metrics.get("max_drawdown", 0) or 0,
        "trades": len(result.trade_log),
        "signals": total_sigs,
        "active_dates": active_dates,
        "calmar": metrics.get("calmar_ratio", 0) or 0,
        "sortino": metrics.get("sortino_ratio", 0) or 0,
        "win_rate": metrics.get("win_rate", 0) or 0,
        "profit_factor": metrics.get("profit_factor", 0) or 0,
        "final_equity": result.final_equity,
        "all_metrics": {k: v for k, v in metrics.items()
                        if k not in ("error",)},
    }


# ── Strategy 1: MACD ─────────────────────────────────────────────────────────

print("\n" + "=" * 80)
print("Strategy 1: macd_signal_v1 — MACD Parameter Grid Search")
print("=" * 80)

macd_variations = [
    {"fast": 12, "slow": 26, "signal": 9, "min_score": 0.0},
    {"fast": 8, "slow": 21, "signal": 5, "min_score": 0.0},
    {"fast": 12, "slow": 26, "signal": 12, "min_score": 0.0},
    {"fast": 5, "slow": 35, "signal": 5, "min_score": 0.0},
]

macd_results = []
for params in macd_variations:
    label = f"fast={params['fast']},slow={params['slow']},signal={params['signal']}"
    t0 = time.time()
    result = run_trial("macd_signal_v1", params)
    elapsed = time.time() - t0
    print(f"  {label:40s} → Sharpe={result['sharpe']:.3f}, "
          f"AnnRet={result['return']*100:.1f}%, MDD={result['mdd']*100:.1f}%, "
          f"Trades={result['trades']}, Signals={result['signals']}, "
          f"({elapsed:.1f}s)")
    macd_results.append({"params": params, **result})

best_macd = max(macd_results, key=lambda r: r["sharpe"])
print(f"\nBest MACD: {best_macd['params']} → Sharpe={best_macd['sharpe']:.3f}")


# ── Strategy 2: RSI ──────────────────────────────────────────────────────────

print("\n" + "=" * 80)
print("Strategy 2: rsi_reversal_v1 — RSI Parameter Grid Search")
print("=" * 80)

rsi_thresholds = [
    {"oversold": 30, "overbought": 70},
    {"oversold": 25, "overbought": 75},
    {"oversold": 20, "overbought": 80},
    {"oversold": 35, "overbought": 65},
]
rsi_periods = [14, 21]

rsi_results = []
for thr in rsi_thresholds:
    for period in rsi_periods:
        params = {"period": period, **thr, "min_score": 0.0}
        label = f"period={period},oversold={thr['oversold']},overbought={thr['overbought']}"
        t0 = time.time()
        result = run_trial("rsi_reversal_v1", params)
        elapsed = time.time() - t0
        print(f"  {label:55s} → Sharpe={result['sharpe']:.3f}, "
              f"AnnRet={result['return']*100:.1f}%, MDD={result['mdd']*100:.1f}%, "
              f"Trades={result['trades']}, Signals={result['signals']}, "
              f"({elapsed:.1f}s)")
        rsi_results.append({"params": params, **result})

best_rsi = max(rsi_results, key=lambda r: r["sharpe"])
print(f"\nBest RSI: {best_rsi['params']} → Sharpe={best_rsi['sharpe']:.3f}")


# ── Save ─────────────────────────────────────────────────────────────────────

output = {
    "macd_signal_v1": {
        "best_params": best_macd["params"],
        "sharpe": best_macd["sharpe"],
        "return": best_macd["return"],
        "mdd": best_macd["mdd"],
        "trades": best_macd["trades"],
        "signals": best_macd["signals"],
        "calmar": best_macd["calmar"],
        "sortino": best_macd["sortino"],
        "win_rate": best_macd["win_rate"],
        "profit_factor": best_macd["profit_factor"],
        "final_equity": best_macd["final_equity"],
        "all_trials": macd_results,
    },
    "rsi_reversal_v1": {
        "best_params": best_rsi["params"],
        "sharpe": best_rsi["sharpe"],
        "return": best_rsi["return"],
        "mdd": best_rsi["mdd"],
        "trades": best_rsi["trades"],
        "signals": best_rsi["signals"],
        "calmar": best_rsi["calmar"],
        "sortino": best_rsi["sortino"],
        "win_rate": best_rsi["win_rate"],
        "profit_factor": best_rsi["profit_factor"],
        "final_equity": best_rsi["final_equity"],
        "all_trials": rsi_results,
    },
}

out_path = settings.PROJECT_ROOT / "tests" / "tuned_macd_rsi.json"
with open(str(out_path), "w") as f:
    json.dump(output, f, indent=2, ensure_ascii=False, default=str)
print(f"\nResults saved to: {out_path}")

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)
print(f"MACD: params={best_macd['params']}, Sharpe={best_macd['sharpe']:.3f}, "
      f"Return={best_macd['return']*100:.1f}%, MDD={best_macd['mdd']*100:.1f}%")
print(f"RSI:  params={best_rsi['params']}, Sharpe={best_rsi['sharpe']:.3f}, "
      f"Return={best_rsi['return']*100:.1f}%, MDD={best_rsi['mdd']*100:.1f}%")
