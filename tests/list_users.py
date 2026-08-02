"""List all registered users and their passwords (if stored)."""
import sqlite3

conn = sqlite3.connect("data/users.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, username, display_name, email, is_admin, is_active, created_at, last_login
    FROM users ORDER BY id
""").fetchall()
conn.close()

print(f"Total users: {len(rows)}")
print("=" * 120)
print(f"{'ID':>4} | {'Username':<20} | {'Display':<16} | {'Email':<25} | {'Admin':<6} | {'Active':<7} | {'Created':<20} | {'Last Login':<20}")
print("-" * 120)
for r in rows:
    print(f"{r['id']:4d} | {r['username']:<20} | {str(r['display_name'] or ''):<16} | {str(r['email'] or ''):<25} | {r['is_admin']!s:<6} | {r['is_active']!s:<7} | {str(r['created_at'] or ''):<20} | {str(r['last_login'] or ''):<20}")
print("=" * 120)
