"""Check available pools from API."""
import httpx

r = httpx.post("http://localhost:8000/api/auth/login", json={
    "username": "admin", "password": "admin123",
}, follow_redirects=True)
t = r.json()["data"]["access_token"]
h = {"Authorization": f"Bearer {t}"}

r2 = httpx.get("http://localhost:8000/api/data/pools", headers=h, follow_redirects=True)
print("Available pools from API:")
for p in r2.json()["data"]:
    print(f"  ID={p['id']:10s} Name={p['name']:10s} Stocks={p['count']}")
