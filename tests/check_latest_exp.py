"""Check latest experiment metrics."""
import httpx, json

BASE = "http://localhost:8000"
r = httpx.post(f"{BASE}/api/auth/login", json={
    "username": "admin", "password": "admin123",
}, follow_redirects=True)
t = r.json()["data"]["access_token"]
h = {"Authorization": f"Bearer {t}"}

# Experiment 71
mr = httpx.get(f"{BASE}/api/experiments/71/metrics", headers=h, follow_redirects=True)
m = mr.json().get("data") or {}
print("Experiment #71 (risk_parity_v1, from frontend):")
for k in ["sharpe_ratio","annual_return","max_drawdown","win_rate","total_trades"]:
    print(f"  {k}: {m.get(k)}")
print()

# Check the latest 3 experiments
r2 = httpx.get(f"{BASE}/api/experiments?limit=5", headers=h, follow_redirects=True)
for e in r2.json()["data"]["items"]:
    eid = e["id"]
    if e["status"] == "completed":
        mr2 = httpx.get(f"{BASE}/api/experiments/{eid}/metrics", headers=h, follow_redirects=True)
        m2 = mr2.json().get("data") or {}
        sr = m2.get("sharpe_ratio", 0) or 0
        ar = m2.get("annual_return", 0) or 0
        print(f"  #{eid:3d} {e['strategy_id']:22s} Sharpe={sr:.2f}  Return={ar*100:.2f}%")
    elif e["status"] == "failed":
        print(f"  #{eid:3d} {e['strategy_id']:22s} ❌ {e.get('error_log','')[:60]}")

print(f"\n✅ Backend running on port 8000")
