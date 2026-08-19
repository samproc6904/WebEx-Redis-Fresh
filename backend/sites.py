"""Sites manager — Redis-backed. Add/remove/check/export Shopify sites."""
from __future__ import annotations

import json
import logging
import os
import random as _r
import re
import time
from urllib.parse import urlparse


def _get_redis():
    """Get Redis client from mongo module."""
    from mongo import get_redis
    return get_redis()


def _clean_url(raw: str) -> str:
    """Strip protocol/path, return clean domain."""
    s = raw.strip().lower()
    if not s:
        return ""
    if not re.match(r"^https?://", s):
        s = "https://" + s
    try:
        parsed = urlparse(s)
        host = (parsed.hostname or "").strip().lower()
    except Exception:
        host = re.sub(r"^(https?://)?", "", raw, flags=re.IGNORECASE).split("/")[0].strip().lower()
    host = host.rstrip(".")
    return host


def _is_valid_url(url: str) -> bool:
    """Basic domain validation."""
    if not url or len(url) < 4:
        return False
    if "." not in url:
        return False
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", url):
        return False
    if not re.match(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$", url):
        return False
    return True


def _user_sites_key(owner_id: int) -> str:
    return f"sites:{owner_id}"


def _site_key(owner_id: int, domain: str) -> str:
    return f"site:{owner_id}:{domain}"


def get_user_sites(owner_id: int) -> list[dict]:
    r = _get_redis()
    if r is None: return []
    try:
        raw = r.get(_user_sites_key(owner_id))
        if raw:
            return json.loads(raw)
        return []
    except Exception as e:
        logging.warning("[Sites] get_user_sites error: %s", e)
        return []


def _save_user_sites(owner_id: int, sites: list[dict]):
    r = _get_redis()
    if r is None: return
    try:
        r.set(_user_sites_key(owner_id), json.dumps(sites))
    except Exception:
        pass


def add_site(owner_id: int, url: str, amount: str = "0.00", gateway: str = "Shopify",
             response: str = "N/A", last_check: str = "N/A",
             proxy_ip: str = "N/A", time_str: str = "N/A") -> dict | None:
    domain = _clean_url(url)
    if not domain or not _is_valid_url(domain):
        return None
    now = int(time.time())
    doc = {
        "owner_id": owner_id,
        "url": domain,
        "amount": amount,
        "gateway": gateway,
        "response": response,
        "last_check": last_check,
        "checked_at": 0,
        "added_at": now,
        "proxy_ip": proxy_ip,
        "time": time_str,
    }
    try:
        sites = get_user_sites(owner_id)
        for s in sites:
            if s.get("url") == domain:
                return None  # duplicate
        sites.append(doc)
        _save_user_sites(owner_id, sites)
        return doc
    except Exception as e:
        logging.warning("[Sites] add_site error: %s", e)
        return None


def add_sites_bulk(owner_id: int, urls: list[str]) -> dict:
    try:
        existing = get_user_sites(owner_id)
        existing_urls = {s["url"] for s in existing}
    except Exception:
        existing = []
        existing_urls = set()

    added = 0
    duplicates = 0
    now = int(time.time())

    for raw in urls:
        domain = _clean_url(raw)
        if not domain or not _is_valid_url(domain):
            continue
        if domain in existing_urls:
            duplicates += 1
            continue
        existing.append({
            "owner_id": owner_id,
            "url": domain,
            "amount": "0.00",
            "gateway": "Shopify",
            "response": "N/A",
            "last_check": "N/A",
            "checked_at": 0,
            "added_at": now,
            "proxy_ip": "N/A",
            "time": "N/A",
        })
        existing_urls.add(domain)
        added += 1

    _save_user_sites(owner_id, existing)
    return {"added": added, "duplicates": duplicates}


def remove_site(owner_id: int, url: str) -> bool:
    domain = _clean_url(url)
    if not domain: return False
    try:
        sites = get_user_sites(owner_id)
        new_sites = [s for s in sites if s.get("url") != domain]
        if len(new_sites) == len(sites):
            return False
        _save_user_sites(owner_id, new_sites)
        return True
    except Exception:
        return False


def remove_all_sites(owner_id: int) -> int:
    try:
        sites = get_user_sites(owner_id)
        count = len(sites)
        _save_user_sites(owner_id, [])
        return count
    except Exception:
        return 0


def update_site_check(owner_id: int, url: str, response: str, status: str,
                      amount: str = "0.00", gateway: str = "Shopify",
                      proxy_ip: str = "N/A", time_str: str = "N/A"):
    domain = _clean_url(url)
    if not domain: return
    now = int(time.time())
    try:
        sites = get_user_sites(owner_id)
        for s in sites:
            if s.get("url") == domain:
                s["response"] = response
                s["last_check"] = status
                s["checked_at"] = now
                s["amount"] = amount
                s["gateway"] = gateway
                s["proxy_ip"] = proxy_ip
                s["time"] = time_str
                break
        _save_user_sites(owner_id, sites)
    except Exception as e:
        logging.warning("[Sites] update_site_check error: %s", e)


def get_site_amount_stats(owner_id: int) -> dict:
    sites = get_user_sites(owner_id)
    if not sites:
        fallback = _load_fallback_sites()
        if fallback:
            return {"min": 0.50, "max": 20.00, "count": len(fallback), "source": "auto"}
        return {"min": 0, "max": 0, "count": 0}

    amounts = []
    for s in sites:
        try:
            a = float(s.get("amount", "0") or "0")
            if a > 0:
                amounts.append(a)
        except (ValueError, TypeError):
            pass

    if not amounts:
        return {"min": 0, "max": 0, "count": len(sites)}
    return {"min": round(min(amounts), 2), "max": round(max(amounts), 2), "count": len(sites)}


_FALLBACK_SITES = None

def _load_fallback_sites() -> list[str]:
    global _FALLBACK_SITES
    if _FALLBACK_SITES is not None:
        return _FALLBACK_SITES

    candidates = [
        "/root/KYu7/mass_gates/sites.txt",
        "/root/webex/backend/shopify/sites.json",
    ]
    sites = []
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            if path.endswith('.json'):
                with open(path) as f:
                    data = json.load(f)
                for item in data:
                    url = item.get('url', '').strip() if isinstance(item, dict) else str(item).strip()
                    if url:
                        sites.append(url)
            else:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            sites.append(line)
            if sites:
                logging.info("[Sites] loaded %d fallback sites from %s", len(sites), path)
                break
        except Exception as e:
            logging.warning("[Sites] fallback load error from %s: %s", path, e)

    _FALLBACK_SITES = sites
    return sites


def get_random_sites_by_amount(owner_id: int, min_amount: float = 0,
                                max_amount: float = 9999, limit: int = 50) -> list[str]:
    sites = get_user_sites(owner_id)
    mongo_sites = []
    if sites:
        for s in sites:
            try:
                a = float(s.get("amount", "0") or "0")
            except (ValueError, TypeError):
                a = 0
            url = s.get("url", "")
            if url and min_amount <= a <= max_amount:
                mongo_sites.append(url)

    result = mongo_sites if mongo_sites else _load_fallback_sites()
    _r.shuffle(result)
    return result[:limit]
