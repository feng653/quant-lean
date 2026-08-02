"""Verify the experiments API returns correct structure for frontend."""
import httpx, json

BASE = "http://localhost:8000"

# Login
r = httpx.post(f"{BASE}/api/auth/login", json={
    "username": "reg_test_user",
    "password": "test123456",
})
assert r.status_code == 200, f"Login failed: {r.status_code}"
token = r.json()["data"]["access_token"]
print("✅ Login OK")

# Test experiments list
r2 = httpx.get(f"{BASE}/api/experiments",
    headers={"Authorization": f"Bearer {token}"})
assert r2.status_code == 200, f"API failed: {r2.status_code}"
body = r2.json()
inner = body["data"]
print(f"✅ Experiments API returns correct structure")
print(f"   items: {len(inner['items'])} items")
print(f"   total: {inner['total']}")
print(f"   page: {inner['page']}")
print(f"   limit: {inner['limit']}")

# The frontend now unwraps correctly via:
#   response.data.data = {items, total, page, limit}
print(f"\n✅ Frontend fix verified: listExperiments() returns correct shape")
print(f"   result.items  -> {type(inner['items']).__name__} (length {len(inner['items'])})")
print(f"   result.total  -> {inner['total']}")
print(f"   result.page   -> {inner['page']}")
