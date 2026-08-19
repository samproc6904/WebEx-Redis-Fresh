"""Remote tests against Railway backend."""
import json, requests, time

BASE = "https://webex-backend.up.railway.app"
DEV_KEY = "0YLvS1prjs8PYPv5xuV240b7SHJydlju"

def api(path, opts=None):
    opts = opts or {}
    headers = opts.pop("headers", {})
    r = requests.request("GET" if not opts.get("method") else opts["method"],
                         f"{BASE}{path}", headers=headers, json=opts.get("json"), timeout=15)
    print(f"  {opts.get('method','GET')} {path} -> {r.status_code}")
    return r

def get_token():
    r = api("/api/dev-login", {"method": "POST", "json": {"dev_key": DEV_KEY}})
    assert r.status_code == 200, f"dev-login failed: {r.text}"
    return r.json()["token"]

def auth(token):
    return {"Authorization": f"Bearer {token}"}

print("=== 1. Ping ===")
r = api("/api/ping")
assert r.status_code == 200 and r.json()["ok"] == True
print("  PASS: ping ok")

print("\n=== 2. Dev Login ===")
token = get_token()
print(f"  PASS: token={token[:20]}...")

print("\n=== 3. /api/me ===")
r = api("/api/me", {"headers": auth(token)})
assert r.status_code == 200
print(f"  PASS: user={r.json().get('user')}")

print("\n=== 4. /api/me no auth ===")
r = api("/api/me")
assert r.status_code == 401
print("  PASS: 401 without token")

print("\n=== 5. /api/sites ===")
r = api("/api/sites", {"headers": auth(token)})
assert r.status_code == 200
sites = r.json().get("sites", [])
print(f"  PASS: {len(sites)} sites")

print("\n=== 6. /api/sites/stats ===")
r = api("/api/sites/stats", {"headers": auth(token)})
assert r.status_code == 200
print(f"  PASS: stats={r.json()}")

print("\n=== 7. /api/proxies ===")
r = api("/api/proxies", {"headers": auth(token)})
assert r.status_code == 200
print(f"  PASS: {len(r.json().get('proxies', []))} proxies")

print("\n=== 8. /api/sessions ===")
r = api("/api/sessions", {"headers": auth(token)})
assert r.status_code == 200
print(f"  PASS: {len(r.json().get('sessions', []))} sessions")

print("\n=== 9. Check info ===")
r = api("/api/check/info", {"method": "POST", "headers": auth(token),
    "json": {"min_amount": 0.5, "max_amount": 10, "cards_count": 2}})
assert r.status_code == 200
info = r.json()
session_id = info["session_id"]
print(f"  PASS: session={session_id}, sites={info.get('sites_count')}, threads={info.get('threads')}")

print("\n=== 10. Check start (1 card) ===")
r = api("/api/check/start", {"method": "POST", "headers": auth(token),
    "json": {"cards": ["4403932159644474|10|2026|386"], "min_amount": 0.5, "max_amount": 10,
             "session_id": session_id}})
assert r.status_code == 200
print(f"  PASS: started, threads={r.json().get('threads')}")

print("\n=== 11. Poll results (60s) ===")
for i in range(12):
    time.sleep(5)
    r = api(f"/api/sessions/{session_id}", {"headers": auth(token)})
    s = r.json()
    checked = s.get("cards_checked", 0)
    print(f"  poll {i+1}: checked={checked}, status={s.get('status')}")
    if checked >= 1:
        break

print(f"\n  Results: {s.get('results', [])[:3]}")
print(f"  Status: {s.get('status')}")

print("\n=== 12. Stop session ===")
r = api("/api/sessions/stop", {"method": "POST", "headers": auth(token),
    "json": {"session_id": session_id}})
print(f"  PASS: {r.json()}")

print("\n=== ALL TESTS PASSED ===")
