"""Check experiment status and error logs."""
import sqlite3

conn = sqlite3.connect("data/experiment.db")
conn.row_factory = sqlite3.Row

# 1. Check experiments
rows = conn.execute("""
    SELECT id, user_id, name, strategy_id, status, error_log, progress_message,
           created_at, completed_at
    FROM experiments ORDER BY id
""").fetchall()

print(f"Total experiments: {len(rows)}")
print("=" * 120)
for r in rows:
    status = r["status"]
    err = r["error_log"] or ""
    print(f"ID={r['id']:3d} | user={r['user_id']} | strategy={r['strategy_id']:20s} | status={status:10s} | err={err[:80]}")
print("=" * 120)

# 2. Check experiment_metrics
metrics_count = conn.execute("SELECT COUNT(*) FROM experiment_metrics").fetchone()[0]
print(f"Completed metrics records: {metrics_count}")

# 3. Show full error details for failed experiments
failed = conn.execute("""
    SELECT id, name, strategy_id, error_log, params, pool_preset
    FROM experiments WHERE status = 'failed'
""").fetchall()
if failed:
    print(f"\n===== FAILED EXPERIMENTS DETAILS =====")
    for f in failed:
        print(f"\nExperiment #{f['id']} ({f['name'] or 'unnamed'}):")
        print(f"  Strategy: {f['strategy_id']}")
        print(f"  Pool: {f['pool_preset']}")
        print(f"  Error: {f['error_log']}")
        print(f"  Params: {f['params'][:200]}")
conn.close()
