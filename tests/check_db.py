import sqlite3
c = sqlite3.connect("data/experiment.db")
rows = c.execute("SELECT e.id, e.name, e.strategy_id, m.sharpe_ratio, m.annual_return, m.max_drawdown, m.win_rate, m.total_trades FROM experiments e LEFT JOIN experiment_metrics m ON e.id=m.experiment_id ORDER BY e.id DESC LIMIT 5").fetchall()
for x in rows:
    print(f"#{x[0]} {x[2]:25s} sharpe={x[3]} ann={x[4]} mdd={x[5]} wr={x[6]} tr={x[7]}")
c.close()
