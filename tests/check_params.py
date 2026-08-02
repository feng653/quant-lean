"""Check experiment params."""
import sqlite3, json

conn = sqlite3.connect("data/experiment.db")
r = conn.execute("SELECT id, name, strategy_id, params, pool_preset, status FROM experiments WHERE id=71").fetchone()
if r:
    params = json.loads(r[3]) if r[3] else {}
    print(f"ID={r[0]} Name={r[1]} Strategy={r[2]}")
    print(f"Params: {json.dumps(params, indent=2, ensure_ascii=False)}")
    print(f"Pool: {r[4]} Status: {r[5]}")
conn.close()
