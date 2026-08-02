"""Debug why backtest generates 0 trades despite signals."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
from backend.config import settings
from backend.core.engine import BacktestEngine
from backend.core.cost_model import CostModel
from backend.strategies.registry import get_registry

pivot = pd.read_parquet(str(settings.abs_path("data/cache/daily/csi500.parquet")))

registry = get_registry()
registry.scan_directory(str(settings.PROJECT_ROOT / "backend" / "strategies"))

strategy = registry.get_strategy("ma_cross_v1")
params = {"fast_period": 5, "slow_period": 20, "min_score": 0.0}
signals = strategy.generate_batch_signals(pivot, params, "2023-07-01", "2026-06-30")

# Examine signals
dates = sorted(signals.keys())
print(f"Signal dates: {len(dates)}")
print(f"First 3 dates: {dates[:3]}")
print(f"Last 3 dates: {dates[-3:]}")

# Show some signal items
for d in dates[:3]:
    items = signals[d]
    print(f"\n{d}: {len(items)} signals")
    for item in items[:3]:
        print(f"  {item.code} {item.action} score={item.score:.4f} weight={item.weight}")

# Check pivot trading days
pivot_dates = [d.strftime("%Y-%m-%d") for d in pivot.index
               if "2023-07-01" <= d.strftime("%Y-%m-%d") <= "2026-06-30"]
print(f"\nPivot trading days in range: {len(pivot_dates)}")
print(f"First: {pivot_dates[:3]}")
print(f"Last: {pivot_dates[-3:]}")

# Check if signals overlap with trading days
signal_dates_set = set(signals.keys())
overlap = signal_dates_set & set(pivot_dates)
print(f"\nSignal dates overlapping with pivot: {len(overlap)}")
