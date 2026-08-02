"""Verify the experiments API returns correct structure."""
import httpx, json

BASE = "http://localhost:8000"

# Login
r = httpx.post(f"{BASE}/api/auth/login", json={
    "username": "reg_test_user",
    "password": "test123456",
}, follow_redirects=True)
print(f"Login: {r.status_code}")
if r.status_code == 200:
    token = r.json()["data"]["access_token"]
    print("  Token OK")

    # Test experiments list
    r2 = httpx.get(f"{BASE}/api/experiments",
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=True)
    print(f"Experiments: {r2.status_code}")
    if r2.status_code == 200:
        body = r2.json()
        inner = body["data"]
        print(f"  items: {len(inner['items'])}")
        print(f"  total: {inner['total']}")
        print(f"  page: {inner['page']}")
        print(f"  limit: {inner['limit']}")
        print("  ✅ Frontend fix verified")
    else:
        print(f"  Response: {r2.text[:200]}")
else:
    print(f"  Error: {r.text[:200]}")
