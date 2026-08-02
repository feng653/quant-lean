"""Final verification: metrics API returns correct data for frontend."""
import httpx

BASE = "http://localhost:8000"
r = httpx.post(f"{BASE}/api/auth/login", json={
    "username": "admin", "password": "admin123",
}, follow_redirects=True, timeout=10)
token = r.json()["data"]["access_token"]
headers = {"Authorization": f"Bearer {token}"}

r = httpx.get(f"{BASE}/api/experiments?limit=10", headers=headers, follow_redirects=True, timeout=10)
items = r.json()["data"]["items"]

for e in items:
    eid = e["id"]
    mr = httpx.get(f"{BASE}/api/experiments/{eid}/metrics", headers=headers, follow_redirects=True, timeout=10)
    m = mr.json().get("data") or {}

    # Fields that frontend needs
    check = {
        "annualized_return": m.get("annualized_return"),
        "annual_return": m.get("annual_return"),
        "cumulative_return": m.get("cumulative_return"),
        "sharpe_ratio": m.get("sharpe_ratio"),
        "max_drawdown": m.get("max_drawdown"),
        "win_rate": m.get("win_rate"),
        "total_trades": m.get("total_trades"),
    }

    # Simulate frontend generateExtendedMetrics
    raw = m
    annualizedReturn = raw.get("annualized_return") if raw.get("annualized_return") is not None else (raw.get("annual_return") or 0)
    cumulativeReturn = raw.get("cumulative_return") if raw.get("cumulative_return") is not None else annualizedReturn

    print(f"[{e['strategy_id']}]")
    print(f"  annual_return(raw)={m.get('annual_return')}")
    print(f"  → annualized_return(computed)={annualizedReturn}")
    print(f"  → cumulative_return(computed)={cumulativeReturn}")
    print(f"  累计收益显示: {(cumulativeReturn * 100):.2f}%")
    print(f"  年化收益显示: {(annualizedReturn * 100):.2f}%")
    print(f"  Sharpe: {m.get('sharpe_ratio'):.2f}")
    print(f"  最大回撤: {(m.get('max_drawdown', 0) * 100):.2f}%")
    print(f"  胜率: {(m.get('win_rate', 0) * 100):.1f}%")
    print(f"  总交易: {m.get('total_trades', 0)}")
    print(f"  ✅ 无NaN" if all(v is not None for v in [annualizedReturn, cumulativeReturn, m.get('sharpe_ratio'), m.get('max_drawdown'), m.get('win_rate')]) else "  ⚠️ 有None")
    print()
