"""Shopify Checker Tests — tests classify, check_card, and API calls.

Usage:
    cd /root/webex && python -m pytest test/test_shopify.py -v

Tests:
    - classify() with various response strings
    - check_card() with test cards
    - call_autossh() error handling
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from shopify.checker import classify, check_card, parse_card, random_site


class TestClassify:
    """Test the classify function with various response strings."""

    def test_charged_thank_you(self):
        assert classify("Thank You") == "CHARGED"
        assert classify("Thank You $0.50 USD") == "CHARGED"

    def test_approved_3d(self):
        assert classify("3D CC") == "APPROVED"
        assert classify("3d cc") == "APPROVED"
        assert classify("ACTION_REQUIRED") == "APPROVED"

    def test_approved_cvv(self):
        assert classify("incorrect_cvc") == "APPROVED"
        assert classify("CVV_MISMATCH") == "APPROVED"
        assert classify("AVS_MISMATCH") == "APPROVED"

    def test_dead_declined(self):
        assert classify("CARD_DECLINED") == "DEAD"
        assert classify("do_not_honor") == "DEAD"
        # Note: insufficient_funds is APPROVED (card valid but low balance)

    def test_error_timeout(self):
        assert classify("CONNECTION TIMEOUT") == "ERROR"
        assert classify("TIMEOUT") == "ERROR"
        assert classify("API Error: ReadTimeout:") == "ERROR"

    def test_error_disconnect(self):
        assert classify("SERVER DISCONNECTED") == "ERROR"
        assert classify("RemoteProtocolError: Server disconnected") == "ERROR"

    def test_error_connection(self):
        assert classify("CONNECTION REFUSED") == "ERROR"
        assert classify("SSL ERROR") == "ERROR"
        assert classify("NETWORK_ERROR") == "ERROR"

    def test_error_empty(self):
        assert classify("") == "ERROR"
        assert classify(None) == "ERROR"

    def test_dead_unknown(self):
        assert classify("SOME_UNKNOWN_RESPONSE") == "DEAD"

    def test_captcha(self):
        assert classify("CAPTCHA_REQUIRED") == "DEAD"
        assert classify("HCAPTCHA DETECTED") == "ERROR"


class TestParseCard:
    """Test card string parsing."""

    def test_valid_card(self):
        result = parse_card("4403932159644474|10|2026|386")
        assert result is not None
        assert result["number"] == "4403932159644474"
        assert result["month"] == "10"
        assert result["year"] == "2026"
        assert result["cvv"] == "386"

    def test_two_digit_year(self):
        result = parse_card("4403932159644474|10|26|386")
        assert result["year"] == "2026"

    def test_invalid_card(self):
        assert parse_card("4403932159644474") is None
        assert parse_card("") is None


class TestRandomSite:
    """Test random site selection."""

    def test_random_site_returns_string(self):
        site = random_site()
        assert isinstance(site, str)
        assert "." in site


class TestCheckCard:
    """Test the check_card function (makes real API call)."""

    def test_check_card_valid(self):
        result = asyncio.get_event_loop().run_until_complete(
            check_card("4403932159644474|10|2026|386", "checkout.7zero.com")
        )
        assert result is not None
        assert "status" in result
        assert result["status"] in ("CHARGED", "APPROVED", "DEAD", "ERROR")
        assert result["card"] == "4403932159644474|10|2026|386"
        assert result["site"] == "checkout.7zero.com"

    def test_check_card_invalid_format(self):
        result = asyncio.get_event_loop().run_until_complete(
            check_card("invalid", "checkout.7zero.com")
        )
        assert result["status"] == "ERROR"
        assert "Invalid card format" in result["response"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "--tb=short"])
