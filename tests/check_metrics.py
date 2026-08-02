"""Check stored metrics in database and via API."""
import sqlite3, json

conn = sqlite3.connect("data/experiment.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT e.id, e.name, e.strategy_id, m.*
    FROM experiments e
    LEFT JOIN experiment_metrics m ON e.id = m.experiment_id
    WHERE e.status = 'completed'
    ORDER BY e.id
""").fetchall()
conn.close()

print(f"{'ID':>3} {'Strategy':<22} {'Sharpe':<8} {'AnnReturn':<10} {'MaxDD':<10} {'WinRate':<8} {'Trades':<7}")
print("=" * 70)

for r in rows:
    sid = r["strategy_id"]
    s = r["sharpe_ratio"]
    ar = r["annual_return"]
    md = r["max_drawdown"]
    wr = r["win_rate"]
    tt = r["total_trades"]

    print(f"{r['id']:3d} {sid:<22} {str(s):<8} {str(ar):<10} {str(md):<10} {str(wr):<8} {str(tt):<7}")
    print(f"     annual_return type={type(ar).__name__} value={ar}")
    print(f"     sharpe_ratio  type={type(s).__name__} value={s}")
