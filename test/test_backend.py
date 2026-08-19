"""Backend API Tests — tests all FastAPI endpoints.

Usage:
    cd /root/webex && python -m pytest test/test_backend.py -v

Requires: server running on localhost:8000
"""
import json
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import requests

BASE = "http://localhost:8000"

# ─── Helpers ──────────────────────────────────────────────────────────

def get_dev_token():
    """Get a dev-login token using the config dev_key."""
    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))
    r = requests.post(f"{BASE}/api/dev-login", json={"dev_key": cfg["dev_key"]})
    assert r.status_code == 200, f"dev-login failed: {r.status_code} {r.text}"
    return r.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ─── Tests ────────────────────────────────────────────────────────────

class TestPing:
    def test_ping_ok(self):
        r = requests.get(f"{BASE}/api/ping")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert "ts" in d


class TestAuth:
    def test_dev_login(self):
        token = get_dev_token()
        assert token and len(token) > 10

    def test_me_endpoint(self):
        token = get_dev_token()
        r = requests.get(f"{BASE}/api/me", headers=auth(token))
        assert r.status_code == 200
        assert "user" in r.json()

    def test_me_no_auth(self):
        r = requests.get(f"{BASE}/api/me")
        assert r.status_code == 401


class TestSites:
    def test_sites_list(self):
        token = get_dev_token()
        r = requests.get(f"{BASE}/api/sites", headers=auth(token))
        assert r.status_code == 200
        d = r.json()
        assert "sites" in d
        assert isinstance(d["sites"], list)

    def test_sites_stats(self):
        token = get_dev_token()
        r = requests.get(f"{BASE}/api/sites/stats", headers=auth(token))
        assert r.status_code == 200
        d = r.json()
        assert "total" in d or "count" in d


class TestProxies:
    def test_proxies_list(self):
        token = get_dev_token()
        r = requests.get(f"{BASE}/api/proxies", headers=auth(token))
        assert r.status_code == 200
        d = r.json()
        assert "proxies" in d
        assert isinstance(d["proxies"], list)


class TestCheckFlow:
    """Test the full check flow: info → start → session → stop."""

    def test_check_info(self):
        token = get_dev_token()
        r = requests.post(f"{BASE}/api/check/info", headers=auth(token),
                          json={"min_amount": 0.5, "max_amount": 10, "cards_count": 2})
        assert r.status_code == 200
        d = r.json()
        assert "session_id" in d
        assert d["sites_count"] > 0
        assert d["threads"] > 0
        return d["session_id"]

    def test_check_start(self):
        token = get_dev_token()
        # First get info (creates session)
        r1 = requests.post(f"{BASE}/api/check/info", headers=auth(token),
                           json={"min_amount": 0.5, "max_amount": 10, "cards_count": 1})
        info = r1.json()
        session_id = info["session_id"]

        # Start the check
        r2 = requests.post(f"{BASE}/api/check/start", headers=auth(token),
                           json={"cards": ["4403932159644474|10|2026|386"],
                                 "min_amount": 0.5, "max_amount": 10,
                                 "session_id": session_id})
        assert r2.status_code == 200
        d = r2.json()
        assert d["session_id"] == session_id
        assert d["threads"] > 0
        return session_id

    def test_session_detail(self):
        token = get_dev_token()
        # Start a check
        r1 = requests.post(f"{BASE}/api/check/info", headers=auth(token),
                           json={"min_amount": 0.5, "max_amount": 10, "cards_count": 1})
        info = r1.json()
        session_id = info["session_id"]

        r2 = requests.post(f"{BASE}/api/check/start", headers=auth(token),
                           json={"cards": ["5290970001390899|06|27|230"],
                                 "min_amount": 0.5, "max_amount": 10,
                                 "session_id": session_id})

        # Poll for results (up to 60 seconds)
        for _ in range(12):
            time.sleep(5)
            r3 = requests.get(f"{BASE}/api/sessions/{session_id}", headers=auth(token))
            s = r3.json()
            if s.get("cards_checked", 0) >= 1:
                break

        # Get session detail
        assert s["session_id"] == session_id
        assert s["cards_count"] == 1
        assert len(s.get("cards", [])) == 1
        assert s["cards"][0] == "5290970001390899|06|27|230"
        # Should have at least 1 result
        assert len(s.get("results", [])) >= 1

    def test_session_list(self):
        token = get_dev_token()
        r = requests.get(f"{BASE}/api/sessions", headers=auth(token))
        assert r.status_code == 200
        d = r.json()
        assert "sessions" in d
        assert isinstance(d["sessions"], list)
        # Sessions list should NOT contain cards field (performance)
        if d["sessions"]:
            assert "cards" not in d["sessions"][0]

    def test_session_stop(self):
        token = get_dev_token()
        # Start a check
        r1 = requests.post(f"{BASE}/api/check/info", headers=auth(token),
                           json={"min_amount": 0.5, "max_amount": 10, "cards_count": 1})
        info = r1.json()
        session_id = info["session_id"]

        r2 = requests.post(f"{BASE}/api/check/start", headers=auth(token),
                           json={"cards": ["4403932159644474|10|2026|386"],
                                 "min_amount": 0.5, "max_amount": 10,
                                 "session_id": session_id})

        # Stop the session
        r3 = requests.post(f"{BASE}/api/sessions/stop", headers=auth(token),
                           json={"session_id": session_id})
        assert r3.status_code == 200
        assert r3.json()["ok"] is True


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "--tb=short"])
