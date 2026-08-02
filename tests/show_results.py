"""Display final backtest results in a nice table."""
import httpx

BASE = "http://localhost:8000"
r = httpx.post(f"{BASE}/api/auth/login", json={
    "username": "admin", "password": "admin123",
}, follow_redirects=True, timeout=10)
token = r.json()["data"]["access_token"]
headers = {"Authorization": f"Bearer {token}"}

r = httpx.get(f"{BASE}/api/experiments?limit=20", headers=headers, follow_redirects=True, timeout=10)
items = r.json()["data"]["items"]

print("=" * 95)
print("📊  十大策略近3年回测表现 (2023-07-01 ~ 2026-06-30)")
print("=" * 95)
print(f"{'#':>3} {'策略名称':<22} {'Sharpe':<9} {'年化收益':<10} {'最大回撤':<10} {'胜率':<8} {'交易数':<7} {'状态':<9}")
print("-" * 95)

results = []
for e in items:
    eid = e["id"]
    mr = httpx.get(f"{BASE}/api/experiments/{eid}/metrics", headers=headers, follow_redirects=True, timeout=10)
    m = mr.json().get("data") or {} if mr.status_code == 200 else {}

    def v(key, scale=1, fmt=".2f", default="-"):
        val = m.get(key)
        if val is None or val == 0:
            return default
        try:
            return f"{float(val)*scale:{fmt}}"
        except:
            return default

    sharpe = m.get("sharpe_ratio", 0) or 0
    annual_return = m.get("annual_return", 0) or 0
    max_drawdown = m.get("max_drawdown", 0) or 0
    win_rate = m.get("win_rate", 0) or 0
    total_trades = m.get("total_trades", 0) or 0

    results.append({
        "id": eid,
        "sid": e["strategy_id"],
        "name": e["name"],
        "status": e["status"],
        "sharpe": sharpe,
        "return": annual_return * 100,
        "mdd": max_drawdown * 100,
        "wr": win_rate * 100,
        "trades": total_trades,
        "error": (e.get("error_log") or "")[:60],
    })

# Sort by Sharpe descending
results.sort(key=lambda x: x["sharpe"], reverse=True)

for i, r in enumerate(results, 1):
    sharpe_s = f"{r['sharpe']:.2f}" if r['sharpe'] != 0 else "-"
    ret_s = f"{r['return']:.1f}%" if r['return'] != 0 else "-"
    mdd_s = f"{r['mdd']:.1f}%" if r['mdd'] != 0 else "-"
    wr_s = f"{r['wr']:.0f}%" if r['wr'] != 0 else "-"
    trades_s = str(int(r['trades'])) if r['trades'] > 0 else "-"
    status = r['status']

    print(f"{i:3d} {r['sid']:<22} {sharpe_s:<9} {ret_s:<10} {mdd_s:<10} {wr_s:<8} {trades_s:<7} {status:<9}")
    if r['error']:
        print(f"     └─ {r['error']}")

print("=" * 95)
print()

# Summary
print("📈 综合排名 (按 Sharpe 比):")
print("-" * 40)
ranked = [r for r in results if r["sharpe"] != 0]
ranked.sort(key=lambda x: x["sharpe"], reverse=True)
for i, r in enumerate(ranked, 1):
    medal = {1:"🥇", 2:"🥈", 3:"🥉"}.get(i, f"  {i}.")
    print(f" {medal} {r['sid']:22s} Sharpe={r['sharpe']:.2f}  收益={r['return']:.1f}%")
