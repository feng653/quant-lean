"""Clean up failed experiments."""
import sqlite3

conn = sqlite3.connect("data/experiment.db")

# Delete failed experiments
deleted = conn.execute("DELETE FROM experiments WHERE status='failed'").rowcount
print(f"Deleted {deleted} failed experiments")

# Also delete experiment 71 (empty params)
conn.execute("DELETE FROM experiments WHERE id=71")
print("Deleted experiment 71 (empty params)")

conn.commit()

remain = conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
print(f"Remaining experiments: {remain}")

for r in conn.execute("SELECT id, name, strategy_id, status FROM experiments ORDER BY id"):
    print(f"  #{r[0]} {r[2]:22s} {r[3]:10s} {r[1]}")
conn.close()
