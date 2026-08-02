"""Debug backtest execution step by step."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np
import json

from backend.config import settings
from backend.core.engine import BacktestEngine
from backend.core.cost_model import CostModel
from backend.strategies.registry import get_registry
from backend.core.metrics import compute_all_metrics

# Load data
pivot = pd.read_parquet(str(settings.abs_path("data/cache/daily/csi500.parquet")))
print(f"Data: {pivot.shape}")
print(f"  Index: {pivot.index[0]} ~ {pivot.index[-1]}")
print(f"  Columns (sample): {list(pivot.columns[:3])}")

# Test ma_cross strategy
registry = get_registry()
registry.scan_directory(str(settings.PROJECT_ROOT / "backend" / "strategies"))

strategy = registry.get_strategy("ma_cross_v1")
params = {"fast_period": 5, "slow_period": 20, "min_score": 0.1}
signals = strategy.generate_batch_signals(pivot, params, "2023-07-01", "2026-06-30")
total_sig = sum(len(v) for v in signals.values())
print(f"\nSignals: {len(signals)} dates, {total_sig} items")

if total_sig == 0:
    # Try lower threshold
    params2 = {"fast_period": 5, "slow_period": 20, "min_score": 0.0}
    signals2 = strategy.generate_batch_signals(pivot, params2, "2023-07-01", "2026-06-30")
    total2 = sum(len(v) for v in signals2.values())
    print(f"With min_score=0: {len(signals2)} dates, {total2} items")

# Run backtest engine
cm = CostModel(commission_rate=0.0003, slippage_rate=0.001, stamp_duty_rate=0.001)
engine = BacktestEngine(initial_capital=1_000_000, cost_model=cm,
                        start_date="2023-07-01", end_date="2026-06-30", max_positions=20)
result = engine.run(signals, pivot, strategy_id="ma_cross_v1")
print(f"\nBacktest result:")
print(f"  Final equity: {result.final_equity:,.0f}")
print(f"  Total return: {(result.final_equity/1_000_000 - 1)*100:.2f}%")
print(f"  Trades: {len(result.trade_log)}")
print(f"  Equity curve: {len(result.equity_curve)} days")

# Compute metrics
metrics = compute_all_metrics(result.equity_curve, None, result.trade_log)
print(f"\nMetrics:")
for k in ["sharpe_ratio", "annualized_return", "max_drawdown", "win_rate", "total_trades", "profit_factor"]:
    print(f"  {k}: {metrics.get(k, 'N/A')}")
