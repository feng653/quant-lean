import httpx, json

r = httpx.options("http://localhost:8000/api/auth/login",
    headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    follow_redirects=True)
print(f"CORS preflight: {r.status_code}")
for k in ["access-control-allow-origin", "access-control-allow-methods", "access-control-allow-headers"]:
    print(f"  {k}: {r.headers.get(k, 'MISSING')}")

r2 = httpx.post("http://localhost:8000/api/auth/login",
    json={"username": "admin", "password": "admin123"},
    headers={"Origin": "http://localhost:5173"},
    follow_redirects=True)
print(f"\nLogin w/ Origin: {r2.status_code}")
print(f"  CORS origin: {r2.headers.get('access-control-allow-origin', 'MISSING')}")
if r2.status_code == 200:
    print("  ✅ Login successful from frontend origin")
else:
    print(f"  ❌ {r2.text[:200]}")
