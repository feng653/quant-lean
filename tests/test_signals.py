"""Test signal generation with lowered thresholds."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
from backend.config import settings
from backend.strategies.registry import get_registry

registry = get_registry()
registry.scan_directory(str(settings.PROJECT_ROOT / "backend" / "strategies"))

pivot = pd.read_parquet(str(settings.abs_path("data/cache/daily/csi500.parquet")))
print(f"Data: {pivot.shape}")

test_cases = [
    ("ma_cross_v1", {"fast_period": 5, "slow_period": 20, "min_score": 0.0}),
    ("macd_signal_v1", {"fast": 12, "slow": 26, "signal": 9, "min_score": 0.0}),
    ("bollinger_breakout_v1", {"period": 20, "std_multiplier": 2.0, "min_score": 0.0}),
    ("rsi_reversal_v1", {"period": 14, "oversold": 30, "overbought": 70, "min_score": 0.0}),
    ("risk_parity_v1", {"lookback": 63, "rebalance_frequency": "monthly", "min_score": 0.0}),
]

for sid, params in test_cases:
    try:
        s = registry.get_strategy(sid)
        signals = s.generate_batch_signals(pivot, params, "2024-01-01", "2024-06-30")
        total = sum(len(v) for v in signals.values())
        print(f"  ✅ {sid}: {len(signals)} dates, {total} signals")
    except Exception as e:
        print(f"  ❌ {sid}: {e}")
