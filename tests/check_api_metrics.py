"""Check what the API returns for metrics."""
import httpx, json

BASE = "http://localhost:8000"
r = httpx.post(f"{BASE}/api/auth/login", json={
    "username": "admin", "password": "admin123",
}, follow_redirects=True, timeout=10)
token = r.json()["data"]["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# Get experiment list
r = httpx.get(f"{BASE}/api/experiments?limit=10", headers=headers, follow_redirects=True, timeout=10)
items = r.json()["data"]["items"]

for e in items:
    eid = e["id"]
    sid = e["strategy_id"]

    # Check experiment detail - does it have metrics fields directly?
    print(f"\n[{eid}] {sid}")
    print(f"  Experiment fields with metrics:")
    for k in ["sharpe_ratio", "annual_return", "max_drawdown", "win_rate", "total_trades"]:
        val = e.get(k)
        print(f"    {k}: {val} (type={type(val).__name__})")

    # Check metrics endpoint
    mr = httpx.get(f"{BASE}/api/experiments/{eid}/metrics", headers=headers, follow_redirects=True, timeout=10)
    if mr.status_code == 200:
        m = mr.json().get("data")
        print(f"  /metrics endpoint data:")
        if isinstance(m, dict):
            for k in ["sharpe_ratio", "annual_return", "max_drawdown", "win_rate", "total_trades",
                       "cumulative_return", "yearly_return"]:
                val = m.get(k)
                print(f"    {k}: {val} (type={type(val).__name__})")
        else:
            print(f"    response: {m} (type={type(m).__name__})")
