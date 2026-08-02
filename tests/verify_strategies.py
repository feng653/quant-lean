"""Verify all strategies can process the csi500 data format."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np
from backend.config import settings

# Load cached data
cache_path = settings.abs_path("data/cache/daily/csi500.parquet")
pivot = pd.read_parquet(cache_path)
print(f"Data loaded: {pivot.shape}")
print(f"  Index type: {type(pivot.index)}")
print(f"  Columns (first 5): {list(pivot.columns[:5])}")
print(f"  Is MultiIndex: {isinstance(pivot.columns, pd.MultiIndex)}")

# Test each strategy
from backend.strategies.registry import get_registry
registry = get_registry()

results = {}
for meta in registry.list_all():
    sid = meta.strategy_id
    print(f"\n{'='*50}")
    print(f"Testing: {sid} ({meta.display_name})")
    try:
        strategy = registry.get_strategy(sid)

        if meta.requires_training:
            print(f"  ⏳ Requires training, skipping signal test")
            results[sid] = "skip_training"
            continue

        # Create default params
        params = {}
        for p in meta.params:
            if p.default is not None:
                params[p.name] = p.default

        signals = strategy.generate_batch_signals(
            pivot, params,
            "2024-01-01", "2024-06-30"
        )
        total_signals = sum(len(v) for v in signals.values())
        print(f"  ✅ Signals generated: {len(signals)} dates, {total_signals} signals")
        results[sid] = f"ok:{total_signals}"
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        results[sid] = f"fail:{e}"

print(f"\n{'='*60}")
print("RESULTS:")
for sid, result in results.items():
    status = "✅" if result.startswith("ok") else ("⏳" if result == "skip_training" else "❌")
    print(f"  {status} {sid}: {result}")
