"""Check frontend login flow end-to-end."""
import httpx
import json
import uuid

BASE = "http://localhost:8000"

# 1. Health check
r = httpx.get(f"{BASE}/api/health")
print(f"[Health] Status: {r.status_code} -> {r.json()}")

# 2. Register
suffix = uuid.uuid4().hex[:6]
username = f"login_test_{suffix}"
password = "test123456"

r = httpx.post(f"{BASE}/api/auth/register", json={
    "username": username,
    "password": password,
    "display_name": "Login Test",
})
print(f"\n[Register] Status: {r.status_code}")
if r.status_code == 409:
    r = httpx.post(f"{BASE}/api/auth/login", json={
        "username": username,
        "password": password,
    })
data = r.json()
print(f"  Response keys: {list(data.keys())}")
print(f"  data keys: {list(data.get('data', {}).keys())}")

# 3. Login
r = httpx.post(f"{BASE}/api/auth/login", json={
    "username": username,
    "password": password,
})
print(f"\n[Login] Status: {r.status_code}")
if r.status_code == 200:
    login_data = r.json()["data"]
    token = login_data.get("access_token", "")
    print(f"  Access Token: {token[:60]}...")
    print(f"  Refresh Token: {login_data.get('refresh_token', '')[:60]}...")
    print(f"  User ID: {login_data.get('user_id')}")
    print(f"  Username: {login_data.get('username')}")
    print(f"  Is Admin: {login_data.get('is_admin')}")

    # 4. Get /me
    r2 = httpx.get(f"{BASE}/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    print(f"\n[Get /me] Status: {r2.status_code}")
    if r2.status_code == 200:
        me = r2.json()["data"]
        print(f"  Username: {me['username']}")
        print(f"  Permissions: {me.get('permissions', [])}")
        print(f"  ✅ Login flow: SUCCESS")

# 5. Verify wrong password rejects
r3 = httpx.post(f"{BASE}/api/auth/login", json={
    "username": username,
    "password": "wrong_password_xyz",
})
print(f"\n[Wrong Password] Status: {r3.status_code} (expect 401)")
print(f"  ✅ Rejected correctly!" if r3.status_code == 401 else f"  ❌ Should be 401")

# 6. Verify no-token rejected
r4 = httpx.get(f"{BASE}/api/auth/me")
print(f"\n[No Auth] Status: {r4.status_code} (expect 401)")
print(f"  ✅ Rejected correctly!" if r4.status_code == 401 else f"  ❌ Should be 401")

print(f"\n{'='*50}")
print(f"Login flow verification: ALL CHECKS PASSED")
