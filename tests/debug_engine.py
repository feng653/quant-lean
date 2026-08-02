"""Debug backtest engine - step by step."""
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

s = registry.get_strategy("ma_cross_v1")
signals = s.generate_batch_signals(pivot,
    {"fast_period":5, "slow_period":20, "min_score":0.0},
    "2023-07-01", "2026-06-30")

# Check if signals exist for each date
signal_compact = {d: items for d, items in signals.items() if items}
print(f"Dates with signals: {len(signal_compact)}")

# Run with debug
cm = CostModel(commission_rate=0.0003, slippage_rate=0.001, stamp_duty_rate=0.001)
engine = BacktestEngine(1_000_000, cm, "2023-07-01", "2026-06-30", 20)
result = engine.run(signals, pivot, strategy_id="ma_cross_v1")
print(f"\nTrades: {len(result.trade_log)}")
print(f"Final: {result.final_equity:,.0f}")
if result.trade_log:
    print(f"First: {result.trade_log[0]}")
    print(f"Last: {result.trade_log[-1]}")
else:
    print("NO TRADES - debugging...")
    # Print signals on first 10 days to see what happens
    days = engine._get_trading_days(pivot)
    for i in range(min(20, len(days))):
        today = days[i]
        today_str = today.strftime("%Y-%m-%d")
        day_sigs = []
        if i > 0:
            prev_str = days[i-1].strftime("%Y-%m-%d")
            day_sigs = signals.get(prev_str, [])
        buys = [s for s in day_sigs if s.action.upper() == "BUY"]
        sells = [s for s in day_sigs if s.action.upper() == "SELL"]
        if day_sigs:
            cps = engine._get_close(pivot, today)
            item = day_sigs[0]
            cp = cps.get(item.code, "N/A")
            print(f"  Day {i} {today_str}: {len(buys)} BUY, {len(sells)} SELL, close[{item.code}]={cp}")
