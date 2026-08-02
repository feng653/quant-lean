"""Check latest experiments and their errors."""
import sqlite3, json

conn = sqlite3.connect("data/experiment.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT id, user_id, name, strategy_id, status, error_log, params,
           pool_preset, test_start, test_end, created_at
    FROM experiments ORDER BY id DESC LIMIT 10
""").fetchall()
conn.close()

print("Latest experiments:")
print("=" * 100)
for r in rows:
    status = r["status"]
    err = (r.get("error_log") or "")[:120]
    params = json.loads(r["params"]) if r["params"] else {}
    pool = r["pool_preset"] or "?"
    period = f"{r['test_start']}~{r['test_end']}" if r["test_start"] else "?"
    print(f"#{r['id']} [{status:10s}] {r['strategy_id']:22s} pool={pool:8s} period={period}")
    print(f"     user={r['user_id']} name={r['name']} params={json.dumps(params)}")
    if err:
        print(f"     ❌ {err}")
    print()

# Also check experiment_metrics for the latest ones
conn2 = sqlite3.connect("data/experiment.db")
conn2.row_factory = sqlite3.Row
metrics_rows = conn2.execute("""
    SELECT experiment_id, sharpe_ratio, annual_return, max_drawdown, win_rate, total_trades
    FROM experiment_metrics ORDER BY experiment_id DESC LIMIT 10
""").fetchall()
conn2.close()

print("\nMetrics for latest:")
for m in metrics_rows:
    print(f"  #{m['experiment_id']}: sharpe={m['sharpe_ratio']} return={m['annual_return']} trades={m['total_trades']}")
