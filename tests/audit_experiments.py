"""Audit current experiments and their params."""
import sqlite3, json

conn = sqlite3.connect("data/experiment.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT id, name, strategy_id, status, params, pool_preset, error_log, progress_message
    FROM experiments ORDER BY id
""").fetchall()
conn.close()

print(f"Total: {len(rows)} experiments\n")
for r in rows:
    params = json.loads(r["params"]) if r["params"] else {}
    min_score = params.get("min_score", "N/A")
    err = (r["error_log"] or "")[:60]
    print(f"ID={r['id']:3d} | {r['strategy_id']:25s} | status={r['status']:10s} | min_score={str(min_score):8s} | pool={r['pool_preset']}")
    if err:
        print(f"  Error: {err}")
