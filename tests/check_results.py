"""Check experiment results and error logs."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import httpx

BASE = "http://localhost:8000"
r = httpx.post(f"{BASE}/api/auth/login", json={
    "username": "admin", "password": "admin123",
}, follow_redirects=True, timeout=10)
token = r.json()["data"]["access_token"]
headers = {"Authorization": f"Bearer {token}"}

r = httpx.get(f"{BASE}/api/experiments?limit=50", headers=headers, follow_redirects=True, timeout=10)
items = r.json()["data"]["items"]

print("Experiment Results:")
print("=" * 80)
for e in items:
    sid = e["strategy_id"]
    status = e["status"]
    err = (e.get("error_log") or "")[:100]
    metrics = {}
    # Check if metrics exist by calling metrics endpoint
    mr = httpx.get(f"{BASE}/api/experiments/{e['id']}/metrics", headers=headers, follow_redirects=True, timeout=10)
    if mr.status_code == 200:
        m = mr.json().get("data")
        if m:
            metrics = m

    s = metrics.get("sharpe_ratio", "N/A")
    ar = metrics.get("annual_return", "N/A")
    mdd = metrics.get("max_drawdown", "N/A")

    print(f"[{status:10s}] ID={e['id']:3d} {sid:25s} Sharpe={str(s):10s} Return={str(ar):10s} MDD={str(mdd):10s}")
    if err:
        print(f"  ├─ Error: {err}")
    if status == "completed" and (s == "N/A" or s == 0):
        print(f"  └─ ⚠️  Completed with no/zero metrics")

print("\nFailed experiments detail:")
r2 = httpx.get(f"{BASE}/api/experiments?status=failed", headers=headers, follow_redirects=True, timeout=10)
failed = r2.json()["data"]["items"]
for f in failed:
    print(f"  #{f['id']} {f['strategy_id']}: {f.get('error_log','')[:200]}")
