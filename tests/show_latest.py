"""Show latest experiments and server status."""
import httpx, json

BASE = "http://localhost:8000"

# Check server
r = httpx.get(f"{BASE}/api/health", follow_redirects=True)
print(f"Server: {'✅ Running' if r.status_code == 200 else '❌ Down'}")
print()

# Login
r = httpx.post(f"{BASE}/api/auth/login", json={
    "username": "admin", "password": "admin123",
}, follow_redirects=True)
token = r.json()["data"]["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# List experiments
r = httpx.get(f"{BASE}/api/experiments?limit=20", headers=headers, follow_redirects=True)
items = r.json()["data"]["items"]

if not items:
    print("📭 没有实验记录")
else:
    print(f"📊 共 {len(items)} 个实验:")
    print(f"{'ID':>4} {'Status':<10} {'Strategy':<22} {'Pool':<10} {'Test Period':<24} {'Name'}")
    print("-" * 90)
    for e in items:
        status = e["status"]
        sid = e["strategy_id"]
        pool = e["pool_preset"] or "custom"
        period = f"{e.get('test_start','?')} ~ {e.get('test_end','?')}"
        name = (e["name"] or "")[:20]
        print(f"{e['id']:4d} {status:<10} {sid:<22} {pool:<10} {period:<24} {name}")
        if status == "failed" and e.get("error_log"):
            err = e["error_log"][:100]
            print(f"     ❌ {err}")

# Print pools
r2 = httpx.get(f"{BASE}/api/data/pools", headers=headers, follow_redirects=True)
pools = r2.json()["data"]
print(f"\n📂 可用股票池:")
for p in pools:
    print(f"   {p['id']:10s} {p['name']:<10s} ({p['count']} stocks)")

print(f"\n✅ 后端服务运行中")
