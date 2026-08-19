"""PROXY — cloned from KYu7 modules/proxy.py (same parse + check logic).

Storage: MongoDB (user_id + proxy natural key), same as KYu7 write-through.
"""
from __future__ import annotations

import asyncio
import re
from typing import Callable, Optional
from urllib.parse import quote

import aiohttp

from mongo import (
    delete_proxy_from_mongo,
    get_user_proxies,
    push_proxy_to_mongo,
)

MAX_BULK = 50
CHECK_URL = "https://www.gstatic.com/generate_204"


def parse_proxy(s) -> Optional[dict]:
    """Same 7-format parser as KYu7."""
    if not s or not s.strip():
        return None
    s = s.strip()
    proto = "http"
    m = re.match(r"^(https?|socks[45])://", s, re.IGNORECASE)
    if m:
        proto = m.group(1).lower()
        s = s[len(m.group(1)) + 3:]

    patterns = [
        (r"^([^:@]+):([^:@]+)@([\w\.-]+):(\d+)$", 1, 2, 3, 4),   # user:pass@host:port
        (r"^([\w\.-]+):(\d+):([^:@]+):([^:@]+)$", 3, 4, 1, 2),   # host:port:user:pass
        (r"^([^:@]+):([^:@]+):([\w\.-]+):(\d+)$", 1, 2, 3, 4),   # user:pass:host:port
        (r"^([^:@]+):([^:@]+)\s+([\w\.-]+):(\d+)$", 1, 2, 3, 4),  # user:pass host:port
        (r"^([^:@]+)\s+([^:@]+)\s+([\w\.-]+)\s+(\d+)$", 1, 2, 3, 4),  # user pass host port
        (r"^([\w\.-]+):(\d+)$", 0, 0, 1, 2),   # host:port (no auth)
        (r"^([\w\.-]+):(\d+)::$", 0, 0, 1, 2),  # host:port:: (no-auth db format)
    ]
    for pat, ui, pi, ii, poi in patterns:
        m = re.match(pat, s)
        if m:
            g = m.groups()
            if not pi:
                ip, port = g[ii - 1], g[poi - 1]
                return {
                    "db": f"{ip}:{port}::",
                    "url": f"{proto}://{ip}:{port}",
                    "http": f"http://{ip}:{port}",
                }
            user, pwd, ip, port = g[ui - 1], g[pi - 1], g[ii - 1], g[poi - 1]
            if port.isdigit():
                eu, ep = quote(user, safe=""), quote(pwd, safe="")
                return {
                    "db": f"{ip}:{port}:{user}:{pwd}",
                    "url": f"{proto}://{eu}:{ep}@{ip}:{port}",
                    "http": f"http://{user}:{pwd}@{ip}:{port}",
                }
    return None


def to_db_format(pstr: str) -> Optional[str]:
    p = parse_proxy(pstr)
    return p["db"] if p else None


async def check_proxy(url: str, timeout: int = 10) -> bool:
    """Same gstatic generate_204 check as KYu7."""
    try:
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as s:
            async with s.get(CHECK_URL, proxy=url, ssl=False) as resp:
                return resp.status in (200, 204)
    except Exception:
        return False


async def check_bulk(
    proxies: list[dict],
    timeout: int = 10,
    progress_cb: Optional[Callable[[int, int, int, int], None]] = None,
) -> list[dict]:
    """Same Semaphore(50) bulk check as KYu7. Returns live proxy dicts."""
    sem = asyncio.Semaphore(MAX_BULK)
    live, dead = 0, 0
    total = len(proxies)

    async def check(p):
        nonlocal live, dead
        async with sem:
            ok = await check_proxy(p["url"], timeout=timeout)
            if ok:
                live += 1
            else:
                dead += 1
            if progress_cb:
                try:
                    progress_cb(live + dead, total, live, dead)
                except Exception:
                    pass
            return (p, ok)

    results = await asyncio.gather(*[check(p) for p in proxies])
    return [p for p, ok in results if ok]


def save_proxies_bulk(user_id: int, pstrs: list[str]) -> int:
    """Save live proxies in db format (ip:port:user:pass). Dedup natural key."""
    added = 0
    for pstr in pstrs:
        db_fmt = to_db_format(pstr)
        if not db_fmt:
            continue
        push_proxy_to_mongo(user_id, db_fmt)
        added += 1
    return added


def delete_proxy(user_id: int, proxy_str: Optional[str] = None) -> None:
    delete_proxy_from_mongo(user_id, proxy_str)
