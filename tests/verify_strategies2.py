"""Verify all strategies can process csi500 data format - v2."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import traceback
from backend.config import settings

# Load cached data
cache_path = settings.abs_path("data/cache/daily/csi500.parquet")
print(f"Loading: {cache_path}")
pivot = pd.read_parquet(cache_path)
print(f"Data loaded: {pivot.shape}")
print(f"  Columns: {len(pivot.columns)} stocks")

# Ensure we have enough data
print(f"  Date range: {pivot.index[0]} ~ {pivot.index[-1]}")
print(f"  Trading days: {len(pivot)}")

# Test each strategy
from backend.strategies.registry import get_registry, StrategyRegistry

registry = StrategyRegistry()
registry.scan_directory(str(settings.PROJECT_ROOT / "backend" / "strategies"))

results = {}
for meta in registry.list_all():
    sid = meta.strategy_id
    print(f"\n{'─'*50}")
    print(f"Testing: {sid}")
    try:
        strategy = registry.get_strategy(sid)

        if meta.requires_training:
            print(f"  ⏳ Requires training, skip signal test")
            results[sid] = "skip_training"
            continue

        params = {}
        for p in meta.params:
            if p.default is not None:
                params[p.name] = p.default

        print(f"  Params: {params}")
        signals = strategy.generate_batch_signals(
            pivot, params,
            "2024-01-01", "2024-06-30"
        )
        total = sum(len(v) for v in signals.values())
        print(f"  ✅ {total} signals on {len(signals)} dates")
        results[sid] = f"ok:{total}"
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        traceback.print_exc()
        results[sid] = f"fail:{e}"

print(f"\n{'='*60}")
print("SUMMARY:")
for sid, r in results.items():
    tag = "✅" if r.startswith("ok") else ("⏳" if r == "skip_training" else "❌")
    print(f"  {tag} {sid}: {r}")
