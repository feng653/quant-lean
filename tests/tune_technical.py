"""Grid-search tune 3 underperforming technical strategies."""

import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np

from backend.config import settings
from backend.core.engine import BacktestEngine
from backend.core.cost_model import CostModel
from backend.core.metrics import compute_all_metrics
from backend.strategies.registry import get_registry

TEST_START = "2023-07-01"
TEST_END = "2026-06-30"

# ── Load data ────────────────────────────────────────────────────────────────

print("Loading data...")
pivot = pd.read_parquet(str(settings.abs_path("data/cache/daily/csi500.parquet")))
print(f"  Data: {pivot.shape}, dates: {pivot.index[0]} ~ {pivot.index[-1]}")

# ── Scan registry ────────────────────────────────────────────────────────────

print("Scanning strategies...")
registry = get_registry()
registry.scan_directory(str(settings.PROJECT_ROOT / "backend" / "strategies"))
all_ids = [m.strategy_id for m in registry.list_all()]
print(f"  Registered: {all_ids}")

# ── Cost model ───────────────────────────────────────────────────────────────

cm = CostModel(commission_rate=0.0003, slippage_rate=0.001, stamp_duty_rate=0.001)

# ── Helper ───────────────────────────────────────────────────────────────────


def evaluate(strategy_id, params):
    strategy = registry.get_strategy(strategy_id)
    signals = strategy.generate_batch_signals(pivot, params, TEST_START, TEST_END)
    total_sig = sum(len(v) for v in signals.values())

    engine = BacktestEngine(
        initial_capital=1_000_000,
        cost_model=cm,
        start_date=TEST_START,
        end_date=TEST_END,
        max_positions=20,
    )
    result = engine.run(signals, pivot, strategy_id=strategy_id)
    metrics = compute_all_metrics(result.equity_curve, None, result.trade_log)

    sharpe = metrics.get("sharpe_ratio") or 0.0
    ret = metrics.get("annualized_return") or 0.0
    mdd = metrics.get("max_drawdown") or 0.0

    if isinstance(sharpe, (int, float)) and np.isfinite(sharpe):
        pass
    else:
        sharpe = 0.0
    if isinstance(ret, (int, float)) and np.isfinite(ret):
        pass
    else:
        ret = 0.0
    if isinstance(mdd, (int, float)) and np.isfinite(mdd):
        pass
    else:
        mdd = 0.0

    return {
        "sharpe": float(sharpe),
        "return": float(ret),
        "mdd": float(mdd),
        "signals": total_sig,
        "trades": len(result.trade_log),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Strategy 1: ma_cross_v1
# ═══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("Strategy 1: ma_cross_v1 (MA period pairs grid search)")
print("=" * 70)

ma_pairs = [(5, 20), (10, 40), (20, 60), (10, 30), (5, 30), (20, 80)]
best_ma = None
best_ma_sharpe = -999.0

for fast, slow in ma_pairs:
    params = {"fast_period": fast, "slow_period": slow, "min_score": 0.0}
    try:
        res = evaluate("ma_cross_v1", params)
        print(
            f"  MA({fast:>2},{slow:>2}): Sharpe={res['sharpe']:+.4f}  "
            f"Return={res['return']*100:+7.2f}%  MDD={res['mdd']*100:+7.2f}%  "
            f"sig={res['signals']}  trades={res['trades']}"
        )
        if res["sharpe"] > best_ma_sharpe:
            best_ma_sharpe = res["sharpe"]
            best_ma = {"best_params": params, **res}
    except Exception as e:
        print(f"  MA({fast},{slow}): ERROR - {e}")

print(
    f"\n  BEST: MA({best_ma['best_params']['fast_period']},{best_ma['best_params']['slow_period']})  "
    f"Sharpe={best_ma['sharpe']:+.4f}  "
    f"Return={best_ma['return']*100:+.2f}%  "
    f"MDD={best_ma['mdd']*100:+.2f}%"
)

# ═══════════════════════════════════════════════════════════════════════════════
# Strategy 2: bollinger_breakout_v1
# ═══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("Strategy 2: bollinger_breakout_v1 (std_multiplier × period grid search)")
print("=" * 70)

stds = [1.5, 2.0, 2.5]
periods = [20, 30, 40]
best_bb = None
best_bb_sharpe = -999.0

for std in stds:
    for period in periods:
        params = {"period": period, "std_multiplier": std, "min_score": 0.0}
        try:
            res = evaluate("bollinger_breakout_v1", params)
            print(
                f"  BB(p={period}, std={std:.1f}): Sharpe={res['sharpe']:+.4f}  "
                f"Return={res['return']*100:+7.2f}%  MDD={res['mdd']*100:+7.2f}%  "
                f"sig={res['signals']}  trades={res['trades']}"
            )
            if res["sharpe"] > best_bb_sharpe:
                best_bb_sharpe = res["sharpe"]
                best_bb = {"best_params": params, **res}
        except Exception as e:
            print(f"  BB(p={period}, std={std:.1f}): ERROR - {e}")

print(
    f"\n  BEST: BB(period={best_bb['best_params']['period']}, "
    f"std={best_bb['best_params']['std_multiplier']:.1f})  "
    f"Sharpe={best_bb['sharpe']:+.4f}  "
    f"Return={best_bb['return']*100:+.2f}%  "
    f"MDD={best_bb['mdd']*100:+.2f}%"
)

# ═══════════════════════════════════════════════════════════════════════════════
# Strategy 3: risk_parity_v1
# ═══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("Strategy 3: risk_parity_v1 (lookback × rebalance_frequency grid search)")
print("=" * 70)

lookbacks = [30, 63, 126]
freqs = ["monthly", "weekly"]
best_rp = None
best_rp_sharpe = -999.0

for lb in lookbacks:
    for freq in freqs:
        params = {"lookback": lb, "rebalance_frequency": freq, "min_score": 0.0}
        try:
            res = evaluate("risk_parity_v1", params)
            print(
                f"  RP(lb={lb}, {freq:>7}): Sharpe={res['sharpe']:+.4f}  "
                f"Return={res['return']*100:+7.2f}%  MDD={res['mdd']*100:+7.2f}%  "
                f"sig={res['signals']}  trades={res['trades']}"
            )
            if res["sharpe"] > best_rp_sharpe:
                best_rp_sharpe = res["sharpe"]
                best_rp = {"best_params": params, **res}
        except Exception as e:
            print(f"  RP(lb={lb}, {freq}): ERROR - {e}")

print(
    f"\n  BEST: RP(lookback={best_rp['best_params']['lookback']}, "
    f"rebalance={best_rp['best_params']['rebalance_frequency']})  "
    f"Sharpe={best_rp['sharpe']:+.4f}  "
    f"Return={best_rp['return']*100:+.2f}%  "
    f"MDD={best_rp['mdd']*100:+.2f}%"
)

# ═══════════════════════════════════════════════════════════════════════════════
# Save results
# ═══════════════════════════════════════════════════════════════════════════════

output = {
    "ma_cross_v1": {
        "best_params": best_ma["best_params"],
        "sharpe": best_ma["sharpe"],
        "return": best_ma["return"],
        "mdd": best_ma["mdd"],
    },
    "bollinger_breakout_v1": {
        "best_params": best_bb["best_params"],
        "sharpe": best_bb["sharpe"],
        "return": best_bb["return"],
        "mdd": best_bb["mdd"],
    },
    "risk_parity_v1": {
        "best_params": best_rp["best_params"],
        "sharpe": best_rp["sharpe"],
        "return": best_rp["return"],
        "mdd": best_rp["mdd"],
    },
}

out_path = settings.abs_path("tests/tuned_technical.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

print(f"\n{'='*70}")
print("Results saved to tests/tuned_technical.json")
print(json.dumps(output, indent=2, ensure_ascii=False))
print(f"{'='*70}")
