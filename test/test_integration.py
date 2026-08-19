"""Integration Tests — full end-to-end flow tests.

Usage:
    cd /root/webex && python -m pytest test/test_integration.py -v

Tests the complete flow: login → start check → poll results → stop → resume after restart
"""
import asyncio
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import engine
import mongo
import shopify

BASE = "http://localhost:8000"


def get_dev_token():
    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))
    r = requests.post(f"{BASE}/api/dev-login", json={"dev_key": cfg["dev_key"]})
    return r.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestFullFlow:
    """Test the complete check flow from start to finish."""

    def test_start_poll_stop(self):
        """Start check → poll for results → stop."""
        token = get_dev_token()
        uid = get_dev_token()

        # Get check info
        r1 = requests.post(f"{BASE}/api/check/info", headers=auth(token),
                           json={"min_amount": 0.5, "max_amount": 10, "cards_count": 1})
        info = r1.json()
        session_id = info["session_id"]

        # Start check
        r2 = requests.post(f"{BASE}/api/check/start", headers=auth(token),
                           json={"cards": ["5290970001390899|06|27|230"],
                                 "min_amount": 0.5, "max_amount": 10,
                                 "session_id": session_id})
        assert r2.status_code == 200

        # Poll for results (up to 60 seconds)
        for i in range(12):
            time.sleep(5)
            r3 = requests.get(f"{BASE}/api/sessions/{session_id}", headers=auth(token))
            s = r3.json()
            if s.get("cards_checked", 0) >= 1:
                break

        # Verify results
        assert s["cards_checked"] >= 1, f"Expected at least 1 checked, got {s['cards_checked']}"
        assert len(s.get("results", [])) >= 1
        assert s["cards"][0] == "5290970001390899|06|27|230"

        # Stop
        r4 = requests.post(f"{BASE}/api/sessions/stop", headers=auth(token),
                           json={"session_id": session_id})
        assert r4.status_code == 200


class TestResumeAfterRestart:
    """Simulate server restart and verify session resumes."""

    def test_resume_flow(self):
        token = get_dev_token()

        # Start a check with 2 cards
        r1 = requests.post(f"{BASE}/api/check/info", headers=auth(token),
                           json={"min_amount": 0.5, "max_amount": 10, "cards_count": 2})
        info = r1.json()
        session_id = info["session_id"]

        r2 = requests.post(f"{BASE}/api/check/start", headers=auth(token),
                           json={"cards": ["4403932159644474|10|2026|386",
                                           "5290970001390899|06|27|230"],
                                 "min_amount": 0.5, "max_amount": 10,
                                 "session_id": session_id})

        # Poll until at least 1 card is checked (up to 90s)
        for _ in range(18):
            time.sleep(5)
            r3 = requests.get(f"{BASE}/api/sessions/{session_id}", headers=auth(token))
            s = r3.json()
            if s.get("cards_checked", 0) >= 1:
                break

        assert s["cards_checked"] >= 1, "Should have at least 1 card checked"

        # Simulate restart: clear engine state
        engine.ACTIVE.clear()

        # If session is still running, resume it
        if s.get("status") == "running":
            loop = asyncio.new_event_loop()
            loop.run_until_complete(engine.resume_running_sessions())

        # Wait for all cards to complete (poll up to 90s)
        for _ in range(18):
            time.sleep(5)
            r4 = requests.get(f"{BASE}/api/sessions/{session_id}", headers=auth(token))
            s2 = r4.json()
            if s2.get("cards_checked", 0) >= 2:
                break

        # Verify all cards checked (either done or stopped after full check)
        assert s2["cards_checked"] >= 2, f"Expected 2+ checked, got {s2['cards_checked']}"


class TestCardsPersistence:
    """Test that cards are stored in session and restored on view."""

    def test_cards_stored_in_session(self):
        token = get_dev_token()

        # Start check with specific cards
        cards = ["4403932159644474|10|2026|386", "5290970001390899|06|27|230"]
        r1 = requests.post(f"{BASE}/api/check/info", headers=auth(token),
                           json={"min_amount": 0.5, "max_amount": 10, "cards_count": 2})
        info = r1.json()

        r2 = requests.post(f"{BASE}/api/check/start", headers=auth(token),
                           json={"cards": cards, "min_amount": 0.5, "max_amount": 10,
                                 "session_id": info["session_id"]})

        # Get session detail
        r3 = requests.get(f"{BASE}/api/sessions/{info['session_id']}", headers=auth(token))
        s = r3.json()

        # Verify cards are stored
        assert "cards" in s
        assert len(s["cards"]) == 2
        assert s["cards"] == cards

    def test_session_list_excludes_cards(self):
        """Session list should NOT include full cards array (performance)."""
        token = get_dev_token()
        r = requests.get(f"{BASE}/api/sessions", headers=auth(token))
        sessions = r.json().get("sessions", [])
        if sessions:
            assert "cards" not in sessions[0], "Session list should not include cards field"


class TestErrorClassification:
    """Test that API errors are classified correctly."""

    def test_timeout_is_error(self):
        """Timeout errors should be classified as ERROR, not DEAD."""
        assert shopify.classify("CONNECTION TIMEOUT") == "ERROR"
        assert shopify.classify("TIMEOUT") == "ERROR"

    def test_disconnect_is_error(self):
        """Disconnect errors should be classified as ERROR, not DEAD."""
        assert shopify.classify("SERVER DISCONNECTED") == "ERROR"

    def test_captcha_is_dead(self):
        """CAPTCHA should be DEAD (not ERROR)."""
        assert shopify.classify("CAPTCHA_REQUIRED") == "DEAD"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "--tb=short"])
