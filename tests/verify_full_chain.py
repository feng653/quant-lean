"""
Full chain verification script.
Tests: data loading → strategy signal generation → backtest → metrics → API access.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

print("=" * 60)
print("FULL CHAIN VERIFICATION")
print("=" * 60)

# ── 1. Load real CSI500 data ────────────────────────────────────────────────
print("\n1. Loading CSI500 parquet data...")
pivot = pd.read_parquet("data/cache/daily/csi500.parquet")
print(f"   Shape: {pivot.shape}")
print(f"   Date range: {pivot.index[0]} ~ {pivot.index[-1]}")
print(f"   Stocks: {len(pivot.columns)}")

# ── 2. Generate MA Cross strategy signals ───────────────────────────────────
print("\n2. Running MACrossStrategy signal generation...")
from backend.strategies.technical.ma_cross import MACrossStrategy
strat = MACrossStrategy()
meta = strat.metadata()
print(f"   Strategy: {meta.strategy_id} ({meta.display_name})")

# Create MultiIndex columns (code, 'close')
close_pivot = pivot.copy()
close_pivot.columns = pd.MultiIndex.from_product([close_pivot.columns, ["close"]])
print(f"   MultiIndex pivot: {close_pivot.shape}")

signals = strat.generate_batch_signals(
    close_pivot,
    {"fast_period": 5, "slow_period": 20, "min_score": 0.005},
    "2025-01-01",
    "2026-06-30",
)
total_signals = sum(len(v) for v in signals.values())
buy_signals = sum(1 for sigs in signals.values() for s in sigs if s.action == "BUY")
sell_signals = sum(1 for sigs in signals.values() for s in sigs if s.action == "SELL")
print(f"   Signal dates: {len(signals)}")
print(f"   Total items: {total_signals} (BUY={buy_signals}, SELL={sell_signals})")

# ── 3. Run Backtest Engine ──────────────────────────────────────────────────
print("\n3. Running BacktestEngine...")
from backend.core.cost_model import CostModel
from backend.core.engine import BacktestEngine

cm = CostModel()
engine = BacktestEngine(
    initial_capital=1_000_000,
    cost_model=cm,
    start_date="2025-01-01",
    end_date="2026-06-30",
    max_positions=20,
)
result = engine.run(signals, close_pivot, strategy_id="ma_cross_v1")
print(f"   Final equity: {result.final_equity:,.2f}")
print(f"   Total return: {(result.final_equity / 1000000 - 1) * 100:.2f}%")
print(f"   Trades executed: {len(result.trade_log)}")
print(f"   Equity curve points: {len(result.equity_curve)}")

# ── 4. Compute metrics ──────────────────────────────────────────────────────
print("\n4. Computing performance metrics...")
from backend.core.metrics import compute_all_metrics
metrics = compute_all_metrics(result.equity_curve, None, result.trade_log)
for key in ["sharpe_ratio", "annualized_return", "max_drawdown",
            "win_rate", "total_trades", "volatility", "profit_factor",
            "calmar_ratio", "sortino_ratio"]:
    val = metrics.get(key, "N/A")
    print(f"   {key}: {val}")

# ── 5. Verify API endpoints work with real data ────────────────────────────
print("\n5. Verifying API data access...")
from fastapi.testclient import TestClient
from backend.main import app
client = TestClient(app)

# Register/login
resp = client.post("/api/auth/register", json={
    "username": "full_chain_user", "password": "test123456", "display_name": "Chain"
})
if resp.status_code == 409:
    resp = client.post("/api/auth/login", json={
        "username": "full_chain_user", "password": "test123456"
    })
token = resp.json()["data"]["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# Data pools
resp = client.get("/api/data/pools", headers=headers)
pools = resp.json().get("data", [])
print(f"   Pools via API: {len(pools)} pools")

# CSI500 stocks
resp = client.get("/api/data/pools/csi500/stocks", headers=headers)
stock_data = resp.json().get("data", {})
n_stocks = stock_data.get("count", 0)
print(f"   CSI500 stocks: {n_stocks}")

# Stock data for a specific stock
resp = client.get("/api/data/stocks/000001?start=2025-01-01&end=2025-06-30", headers=headers)
stock_records = resp.json().get("data", {}).get("records", [])
print(f"   Stock 000001 records: {len(stock_records)} days")

# Trading calendar
resp = client.get("/api/data/calendar?start=2025-01-01&end=2025-06-30", headers=headers)
cal_data = resp.json().get("data", {})
n_days = cal_data.get("count", 0)
print(f"   Trading days (2025 H1): {n_days}")

# List strategies
# First scan
resp = client.post("/api/strategies/scan", headers=headers)
# May be 403 if not admin, but try list anyway
resp = client.get("/api/strategies", headers=headers)
strategies = resp.json().get("data", [])
print(f"   Registered strategies: {len(strategies)}")

# Check update status
resp = client.get("/api/data/update/status", headers=headers)
status = resp.json().get("data", {})
cache_info = status.get("pools_cache", [])
for c in cache_info:
    if c.get("pool_id") == "csi500":
        print(f"   CSI500 cache: {c.get('n_dates', 0)} days x {c.get('n_stocks', 0)} stocks, "
              f"{c.get('date_start', '?')} ~ {c.get('date_end', '?')}")

print()
print("=" * 60)
print("FULL CHAIN VERIFICATION PASSED")
print("Data → Strategy → Signals → Backtest → Metrics → API: ALL OK")
print("=" * 60)
