"""Check database schema for experiment_metrics table."""
import sqlite3

conn = sqlite3.connect("data/experiment.db")
# Get table schema
schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='experiment_metrics'").fetchone()
print("experiment_metrics schema:")
print(schema[0] if schema else "NOT FOUND")

# Check columns
cols = conn.execute("PRAGMA table_info(experiment_metrics)").fetchall()
print(f"\nColumns ({len(cols)}):")
for c in cols:
    print(f"  {c[1]:30s} {c[2]:15s} nullable={not c[3]} default={c[4]}")

# Check what the _run_experiment function inserts
# From main.py line 434-480, these are the columns inserted:
insert_cols = [
    "experiment_id", "sharpe_ratio", "annual_return", "max_drawdown", "volatility",
    "calmar_ratio", "sortino_ratio", "win_rate", "profit_loss_ratio",
    "avg_trade_return", "max_consecutive_wins", "max_consecutive_losses",
    "total_trades", "avg_holding_days", "turnover_rate", "information_ratio",
    "treynor_ratio", "alpha", "beta", "tracking_error", "upside_capture",
    "downside_capture", "var_95", "cvar_95", "skewness", "kurtosis",
    "daily_sharpe", "monthly_sharpe", "yearly_return", "recovery_days",
    "max_drawdown_duration", "avg_drawdown", "avg_drawdown_days",
    "best_month", "worst_month", "positive_months", "profit_factor", "expectency"
]
print(f"\nColumns used in INSERT ({len(insert_cols)}):")
existing_cols = {c[1] for c in cols}
for c in insert_cols:
    found = "✅" if c in existing_cols else "❌ MISSING"
    print(f"  {found} {c}")

conn.close()
