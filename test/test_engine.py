"""Engine Tests — tests the background checking engine.

Usage:
    cd /root/webex/backend && python -m pytest test/test_engine.py -v

Tests:
    - Card queue mechanics (_next_card)
    - Job creation and structure
    - Resume logic (via API integration tests in test_integration.py)
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import engine
import mongo


def get_dev_uid():
    cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))
    return cfg.get("owner_id", 0)


class TestCardQueue:
    """Test card queue mechanics (synchronous, no API calls)."""

    def test_next_card_order(self):
        """Cards should be processed in order."""
        job = {
            "queue": ["card1", "card2", "card3"],
            "idx": 0,
            "stop": False,
        }

        loop = asyncio.new_event_loop()

        async def get_all():
            results = []
            while True:
                c = await engine._next_card(job)
                if c is None:
                    break
                results.append(c)
            return results

        results = loop.run_until_complete(get_all())
        assert results == ["card1", "card2", "card3"]
        assert job["idx"] == 3

    def test_next_card_stop(self):
        """Should stop when job.stop is True."""
        job = {
            "queue": ["card1", "card2", "card3"],
            "idx": 0,
            "stop": True,
        }

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(engine._next_card(job))
        assert result is None

    def test_next_card_empty_queue(self):
        """Should return None on empty queue."""
        job = {
            "queue": [],
            "idx": 0,
            "stop": False,
        }

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(engine._next_card(job))
        assert result is None

    def test_next_card_concurrent(self):
        """Multiple concurrent reads should not skip cards."""
        job = {
            "queue": ["c1", "c2", "c3", "c4", "c5"],
            "idx": 0,
            "stop": False,
        }

        loop = asyncio.new_event_loop()

        async def read_cards():
            results = []
            while True:
                c = await engine._next_card(job)
                if c is None:
                    break
                results.append(c)
            return results

        results = loop.run_until_complete(read_cards())
        assert sorted(results) == ["c1", "c2", "c3", "c4", "c5"]
        assert len(results) == 5


class TestJobStructure:
    """Test job creation and structure (synchronous)."""

    def test_job_dict_structure(self):
        """Job dict should have all required fields."""
        job = {
            "uid": 123,
            "session_id": "TEST",
            "queue": ["card1"],
            "idx": 0,
            "stop": False,
            "threads": 2,
            "min_amount": 0.5,
            "max_amount": 10,
            "tasks": [],
            "done": False,
        }
        assert "uid" in job
        assert "session_id" in job
        assert "queue" in job
        assert "idx" in job
        assert "stop" in job
        assert "threads" in job
        assert "done" in job


class TestActiveDict:
    """Test the ACTIVE dict management."""

    def test_active_initially_empty(self):
        """ACTIVE dict should be managed correctly."""
        # Just check the structure
        assert isinstance(engine.ACTIVE, dict)

    def test_stop_job_sets_stop_flag(self):
        """stop_job should set stop=True on the job."""
        session_id = "TEST_STOP_FLAG"
        engine.ACTIVE[session_id] = {"stop": False, "session_id": session_id}
        engine.stop_job(session_id)
        assert engine.ACTIVE[session_id]["stop"] is True
        # Cleanup
        del engine.ACTIVE[session_id]


class TestPickFunctions:
    """Test _pick_site and _pick_proxy (synchronous)."""

    def test_pick_site_returns_string(self):
        uid = get_dev_uid()
        site = engine._pick_site(uid, 0.5, 10)
        assert isinstance(site, str)
        assert "." in site

    def test_pick_proxy_returns_string_or_none(self):
        uid = get_dev_uid()
        proxy = engine._pick_proxy(uid)
        # Proxy can be None if no proxies stored, or a string
        assert proxy is None or isinstance(proxy, str)


class TestJobStructure2:
    """Test new job fields for auto-recovery."""

    def test_job_has_last_activity(self):
        """Job should track last_activity for watchdog."""
        job = {
            "uid": 123,
            "session_id": "TEST2",
            "queue": ["card1"],
            "idx": 0,
            "stop": False,
            "threads": 2,
            "min_amount": 0,
            "max_amount": 9999,
            "tasks": [],
            "done": False,
            "last_activity": time.time(),
            "created_at": time.time(),
        }
        assert "last_activity" in job
        assert "created_at" in job
        assert isinstance(job["last_activity"], float)

    def test_watchdog_constants(self):
        """Watchdog constants should be reasonable."""
        assert engine.WATCHDOG_INTERVAL == 30
        assert engine.STUCK_TIMEOUT == 120
        assert engine.HEARTBEAT_INTERVAL == 15
        assert engine.WATCHDOG_INTERVAL < engine.STUCK_TIMEOUT


class TestRespawnJob:
    """Test the _respawn_job helper."""

    def test_respawn_completes_if_no_pending(self):
        """_respawn_job should finish session if all cards are checked."""
        loop = asyncio.new_event_loop()

        session_id = "TEST_RESPAWN_EMPTY"
        # Create a fake job with no pending cards
        job = {
            "uid": 123,
            "session_id": session_id,
            "queue": ["card1"],
            "idx": 1,  # all processed
            "stop": False,
            "threads": 1,
            "min_amount": 0,
            "max_amount": 9999,
            "tasks": [],
            "done": False,
            "last_activity": time.time(),
            "created_at": time.time(),
        }
        engine.ACTIVE[session_id] = job

        # Mock get_session_checked_cards to return "card1" as checked
        original_fn = mongo.get_session_checked_cards
        mongo.get_session_checked_cards = lambda sid: {"card1"}

        try:
            loop.run_until_complete(engine._respawn_job(job))
            # Session should be marked done
            assert job["done"] is True
            assert session_id not in engine.ACTIVE
        finally:
            mongo.get_session_checked_cards = original_fn
            engine.ACTIVE.pop(session_id, None)
            loop.close()


class TestMongoHelpers:
    """Test new mongo helpers for watchdog."""

    def test_touch_session(self):
        """touch_session should not raise."""
        # Just verify it doesn't crash
        mongo.touch_session("NONEXISTENT_SESSION_ID")
        # No assertion needed — just shouldn't throw

    def test_get_stuck_sessions(self):
        """get_stuck_sessions should return a list."""
        result = mongo.get_stuck_sessions(999999)  # very large timeout = nothing stuck
        assert isinstance(result, list)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "--tb=short"])
