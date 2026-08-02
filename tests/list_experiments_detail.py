"""List all experiments with user info to distinguish real vs test."""
import sqlite3, json

exp_conn = sqlite3.connect("data/experiment.db")
exp_conn.row_factory = sqlite3.Row

user_conn = sqlite3.connect("data/users.db")
user_conn.row_factory = sqlite3.Row

users = {r["id"]: r["username"] for r in user_conn.execute("SELECT id, username FROM users").fetchall()}
user_conn.close()

rows = exp_conn.execute("""
    SELECT id, user_id, name, strategy_id, status, error_log, created_at, completed_at
    FROM experiments ORDER BY id
""").fetchall()
exp_conn.close()

print(f"{'ID':>3} | {'User':<18} | {'Name':<18} | {'Strategy':<20} | {'Status':<10} | {'Created':<20} | {'Error'}")
print("=" * 120)
for r in rows:
    username = users.get(r["user_id"], f"user#{r['user_id']}")
    err = (r["error_log"] or "")[:50]
    print(f"{r['id']:3d} | {username:<18} | {str(r['name'] or ''):<18} | {r['strategy_id']:<20} | {r['status']:<10} | {str(r['created_at'] or ''):<20} | {err}")

print()
print("说明:")
print("  - user_id=1 -> 最早的用户, user_id=2=admin")
print("  - test_api_user, reg_test_user, adm_test_user = 测试自动创建")
print("  - 带 e2e_ / login_ / full_chain 前缀 = 自动测试")
print("  - 其他 = 你手动创建的实验")
