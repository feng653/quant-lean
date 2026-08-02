"""Try known passwords against registered users."""
import httpx
import json

BASE = "http://localhost:8000"

# Known passwords from test scripts
credentials = [
    # (username, password, description)
    ("admin", "admin123", "default admin guess"),
    ("admin", "123456", "simple guess"),
    ("admin", "password", "common password"),
    ("test_api_user", "test123456", "test user"),
    ("reg_test_user", "test123456", "regular test user"),
    ("adm_test_user", "admin123456", "admin test user"),
]

print("Trying known passwords...")
print("=" * 60)

for username, password, desc in credentials:
    try:
        r = httpx.post(f"{BASE}/api/auth/login", json={
            "username": username,
            "password": password,
        }, timeout=5)
        if r.status_code == 200:
            data = r.json()["data"]
            print(f"✅  SUCCESS: {username} / {password}")
            print(f"   Display: {data.get('display_name')}")
            print(f"   Admin: {data.get('is_admin')}")
            print(f"   Token: {data.get('access_token', '')[:50]}...")
        elif r.status_code == 401:
            print(f"❌  FAIL: {username} / {password} - wrong password")
        else:
            print(f"⚠️   {username} / {password} -> status={r.status_code}")
    except Exception as e:
        print(f"⚠️   Error: {e}")

print("=" * 60)
print("\nTIP: 你可以直接注册新账号（首个注册的会自动成为admin）")
