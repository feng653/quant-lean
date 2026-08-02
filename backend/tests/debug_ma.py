"""Debug MA cross signals."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import pandas as pd
import numpy as np

dates = pd.date_range('2024-01-01', periods=120, freq='B')
np.random.seed(42)
prices = np.concatenate([
    100 + np.random.randn(60).cumsum() * 0.5,
    100 + np.arange(60) * 0.5 + np.random.randn(60) * 0.5,
])

dfs = {}
dfs[('000001.SZ', 'close')] = pd.Series(prices, index=dates)
pivot = pd.DataFrame(dfs, index=dates)
pivot.columns = pd.MultiIndex.from_tuples(pivot.columns)

from backend.strategies.technical.ma_cross import MACrossStrategy
strat = MACrossStrategy()

# Manually trace the signal generation
codes = strat._extract_codes(pivot)
print(f"Codes: {codes}")

for code in codes:
    close = strat._get_close_series(pivot, code)
    close = close.loc['2024-01-15':'2024-06-30'].dropna()
    print(f"Close length: {len(close)}")
    
    fast_ma = close.rolling(window=5, min_periods=5).mean()
    slow_ma = close.rolling(window=25, min_periods=25).mean()
    
    prev_fast = fast_ma.shift(1)
    prev_slow = slow_ma.shift(1)
    
    golden_cross = (prev_fast < prev_slow) & (fast_ma >= slow_ma)
    print(f"Golden cross True count: {golden_cross.sum()}")
    
    for i, d in enumerate(close.index):
        if i > 300:
            break
        gc_val = golden_cross.get(d, False)
        if gc_val is True or gc_val:
            print(f"  d={d.strftime('%Y-%m-%d')} golden={gc_val} type={type(gc_val)}")
            try:
                s1 = float(fast_ma.shift(1).loc[d] / slow_ma.shift(1).loc[d] - 1)
                print(f"  strength={s1:.4f}")
            except Exception as e:
                print(f"  ERROR computing strength: {e}")
