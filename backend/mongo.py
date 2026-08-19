"""REDIS — database layer replacing MongoDB."""
from __future__ import annotations

import json
import logging
import os
import time
import threading
from pathlib import Path

# Load REDIS_URL from config.json (never from env vars)
_config_path = Path(__file__).resolve().parent.parent / "config.json"
try:
    with open(_config_path, "r", encoding="utf-8") as _f:
        _cfg = json.load(_f)
    REDIS_URL = str(_cfg.get("redis_url", "")).strip()
except Exception:
    REDIS_URL = ""

_redis_client = None
_redis_lock = threading.Lock()

def get_redis():
    """Lazy Redis client."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if not REDIS_URL:
        return None
    try:
        import redis as redis_lib
        _redis_client = redis_lib.from_url(
            REDIS_URL, decode_responses=True, socket_timeout=3, socket_connect_timeout=3
        )
        _redis_client.ping()
        logging.info("[Redis] connected to %s", REDIS_URL.split("@")[-1] if "@" in REDIS_URL else REDIS_URL)
        return _redis_client
    except Exception as e:
        _redis_client = None
        logging.warning("[Redis] unreachable: %s", e)
        return None


def redis_is_online() -> bool:
    return get_redis() is not None


# ─── Cache ─────────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 2.0

def _cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
            return entry[1]
        return None

def _cache_set(key: str, value):
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)

def _cache_invalidate_prefix(prefix: str):
    with _cache_lock:
        to_del = [k for k in _cache if k.startswith(prefix)]
        for k in to_del:
            del _cache[k]


# ─── Helpers ───────────────────────────────────────────────────────────
def _key(*parts) -> str:
    return ":".join(str(p) for p in parts)


# ─── Proxies ──────────────────────────────────────────────────────────
def push_proxy_to_mongo(user_id: int, proxy_str: str):
    r = get_redis()
    if r is None: return
    try:
        r.sadd(_key("proxies", user_id), proxy_str)
        _cache_invalidate_prefix(f"proxies:{user_id}")
    except Exception:
        pass

def delete_proxy_from_mongo(user_id: int, proxy_str: str | None = None):
    r = get_redis()
    if r is None: return
    try:
        if proxy_str:
            r.srem(_key("proxies", user_id), proxy_str)
        else:
            r.delete(_key("proxies", user_id))
        _cache_invalidate_prefix(f"proxies:{user_id}")
    except Exception:
        pass

def get_user_proxies(user_id: int) -> list[str]:
    cache_key = f"proxies:{user_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    r = get_redis()
    if r is None: return []
    try:
        proxies = list(r.smembers(_key("proxies", user_id)))
        _cache_set(cache_key, proxies)
        return proxies
    except Exception as e:
        logging.warning("[Redis] get_user_proxies error: %s", e)
        return []


# ─── Sessions ──────────────────────────────────────────────────────────
def create_session(user_id: int, session_id: str, sites_count: int,
                   proxies_count: int, threads: int, cards_count: int,
                   min_amount: float = 0, max_amount: float = 9999,
                   cards: list = None) -> dict | None:
    r = get_redis()
    if r is None: return None
    doc = {
        "session_id": session_id, "user_id": user_id, "gateway": "Shopify",
        "sites_count": sites_count, "proxies_count": proxies_count,
        "threads": threads, "cards_count": cards_count, "cards_checked": 0,
        "live": 0, "charged": 0, "dead": 0, "errors": 0,
        "min_amount": min_amount, "max_amount": max_amount,
        "status": "running", "cards": cards or [],
        "started_at": time.time(), "finished_at": None,
        "last_activity": time.time(),
    }
    try:
        r.set(_key("session", session_id), json.dumps(doc))
        r.sadd(_key("user_sessions", user_id), session_id)
        _cache_invalidate_prefix(f"s:{session_id}")
        _cache_invalidate_prefix(f"sessions:{user_id}")
        return doc
    except Exception as e:
        logging.warning("[Redis] create_session error: %s", e)
        return None


def _get_session_raw(session_id: str) -> dict | None:
    r = get_redis()
    if r is None: return None
    try:
        raw = r.get(_key("session", session_id))
        if raw:
            return json.loads(raw)
        return None
    except Exception:
        return None


def _save_session(doc: dict):
    r = get_redis()
    if r is None: return
    try:
        r.set(_key("session", doc["session_id"]), json.dumps(doc))
    except Exception:
        pass


def update_session_result(session_id: str, card: str, status: str,
                          response: str = "", site: str = "",
                          gateway: str = "", price: str = None):
    r = get_redis()
    if r is None: return
    s = (status or "").upper()
    try:
        doc = _get_session_raw(session_id)
        if not doc:
            return
        doc["cards_checked"] = doc.get("cards_checked", 0) + 1
        doc["last_activity"] = time.time()
        if s == "APPROVED": doc["live"] = doc.get("live", 0) + 1
        elif s == "CHARGED": doc["charged"] = doc.get("charged", 0) + 1
        elif s == "DEAD": doc["dead"] = doc.get("dead", 0) + 1
        elif s == "ERROR": doc["errors"] = doc.get("errors", 0) + 1

        result_doc = {"card": card, "status": s, "response": response,
                      "site": site, "gateway": gateway, "price": price}
        # Keep last 500 in session results
        results = doc.get("results", [])
        results.append(result_doc)
        if len(results) > 500:
            results = results[-500:]
        doc["results"] = results

        _save_session(doc)
        _cache_invalidate_prefix(f"s:{session_id}")

        # Store charged/approved permanently (never lost)
        if s in ("APPROVED", "CHARGED"):
            result_doc["session_id"] = session_id
            result_doc["ts"] = time.time()
            r.set(_key("charged", session_id, card), json.dumps(result_doc))
            r.sadd(_key("charged_sessions", session_id), card)
    except Exception as e:
        logging.warning("[Redis] update_session_result error: %s", e)


def touch_session(session_id: str):
    r = get_redis()
    if r is None: return
    try:
        doc = _get_session_raw(session_id)
        if doc:
            doc["last_activity"] = time.time()
            _save_session(doc)
    except Exception:
        pass


def get_stuck_sessions(timeout_seconds: int = 120) -> list[dict]:
    r = get_redis()
    if r is None: return []
    cutoff = time.time() - timeout_seconds
    try:
        stuck = []
        # Scan all session keys
        for key in r.scan_iter("session:*"):
            raw = r.get(key)
            if not raw: continue
            doc = json.loads(raw)
            if doc.get("status") == "running" and (doc.get("last_activity") or 0) < cutoff:
                stuck.append({
                    "session_id": doc["session_id"],
                    "user_id": doc["user_id"],
                    "cards": doc.get("cards", []),
                    "threads": doc.get("threads", 25),
                    "min_amount": doc.get("min_amount", 0),
                    "max_amount": doc.get("max_amount", 9999),
                    "cards_checked": doc.get("cards_checked", 0),
                    "cards_count": doc.get("cards_count", 0),
                })
        return stuck[:10]
    except Exception as e:
        logging.warning("[Redis] get_stuck_sessions error: %s", e)
        return []


def finish_session(session_id: str):
    r = get_redis()
    if r is None: return
    try:
        doc = _get_session_raw(session_id)
        if doc:
            doc["status"] = "done"
            doc["finished_at"] = time.time()
            _save_session(doc)
            _cache_invalidate_prefix(f"s:{session_id}")
    except Exception:
        pass


def stop_session(session_id: str):
    r = get_redis()
    if r is None: return
    try:
        doc = _get_session_raw(session_id)
        if doc:
            doc["status"] = "stopped"
            doc["finished_at"] = time.time()
            _save_session(doc)
            _cache_invalidate_prefix(f"s:{session_id}")
    except Exception:
        pass


def update_session_cards(session_id: str, cards_count: int,
                         min_amount: float = 0, max_amount: float = 9999,
                         cards: list = None):
    r = get_redis()
    if r is None: return
    try:
        doc = _get_session_raw(session_id)
        if doc:
            doc["cards_count"] = cards_count
            doc["min_amount"] = min_amount
            doc["max_amount"] = max_amount
            doc["status"] = "running"
            doc["finished_at"] = None
            if cards is not None:
                doc["cards"] = cards
            _save_session(doc)
            _cache_invalidate_prefix(f"s:{session_id}")
    except Exception:
        pass


def mark_all_running_stopped():
    r = get_redis()
    if r is None: return
    try:
        for key in r.scan_iter("session:*"):
            raw = r.get(key)
            if not raw: continue
            doc = json.loads(raw)
            if doc.get("status") == "running":
                doc["status"] = "stopped"
                doc["finished_at"] = time.time()
                r.set(key, json.dumps(doc))
    except Exception:
        pass


def get_user_sessions(user_id: int, limit: int = 50) -> list[dict]:
    cache_key = f"sessions:{user_id}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    r = get_redis()
    if r is None: return []
    try:
        session_ids = list(r.smembers(_key("user_sessions", user_id)))
        sessions = []
        for sid in session_ids:
            doc = _get_session_raw(sid)
            if doc and doc.get("user_id") == user_id:
                # Remove large fields for listing
                slim = {k: v for k, v in doc.items() if k not in ("results", "cards")}
                sessions.append(slim)
        sessions.sort(key=lambda x: x.get("started_at", 0), reverse=True)
        result = sessions[:limit]
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        logging.warning("[Redis] get_user_sessions error: %s", e)
        return []


def get_session(session_id: str, user_id: int) -> dict | None:
    cache_key = f"s:{session_id}:{user_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    r = get_redis()
    if r is None: return None
    try:
        doc = _get_session_raw(session_id)
        if doc and doc.get("user_id") == user_id:
            _cache_set(cache_key, doc)
            return doc
        return None
    except Exception as e:
        logging.warning("[Redis] get_session error: %s", e)
        return None


def get_running_sessions() -> list[dict]:
    r = get_redis()
    if r is None: return []
    try:
        running = []
        for key in r.scan_iter("session:*"):
            raw = r.get(key)
            if not raw: continue
            doc = json.loads(raw)
            if doc.get("status") == "running":
                running.append(doc)
        return running
    except Exception as e:
        logging.warning("[Redis] get_running_sessions error: %s", e)
        return []


def get_session_checked_cards(session_id: str) -> set[str]:
    r = get_redis()
    if r is None: return set()
    try:
        doc = _get_session_raw(session_id)
        if not doc: return set()
        return {r["card"] for r in doc.get("results", []) if "card" in r}
    except Exception as e:
        logging.warning("[Redis] get_session_checked_cards error: %s", e)
        return set()


# ─── Auth Sessions ────────────────────────────────────────────────────
def save_auth_session(token: str, user_data: dict, expires_at: float):
    r = get_redis()
    if r is None: return
    try:
        doc = {
            "token": token,
            "user_id": user_data.get("uid"),
            "first_name": user_data.get("first_name", ""),
            "username": user_data.get("username", ""),
            "exp": expires_at,
            "created_at": time.time(),
        }
        r.set(_key("auth", token), json.dumps(doc), ex=int(expires_at - time.time()) + 60)
    except Exception as e:
        logging.warning("[Redis] save_auth_session error: %s", e)


def get_auth_session(token: str) -> dict | None:
    r = get_redis()
    if r is None: return None
    try:
        raw = r.get(_key("auth", token))
        if not raw: return None
        doc = json.loads(raw)
        if doc.get("exp", 0) < time.time():
            r.delete(_key("auth", token))
            return None
        return {
            "uid": doc["user_id"],
            "first_name": doc.get("first_name", ""),
            "username": doc.get("username", ""),
            "exp": doc["exp"],
        }
    except Exception as e:
        logging.warning("[Redis] get_auth_session error: %s", e)
        return None


def delete_auth_session(token: str):
    r = get_redis()
    if r is None: return
    try:
        r.delete(_key("auth", token))
    except Exception:
        pass


# ─── Sites ────────────────────────────────────────────────────────────
def get_sites(gateway: str = "Shopify") -> list[dict]:
    r = get_redis()
    if r is None: return []
    try:
        raw = r.get(_key("sites", gateway))
        if raw:
            return json.loads(raw)
        return []
    except Exception:
        return []


def save_sites(gateway: str, sites: list[dict]):
    r = get_redis()
    if r is None: return
    try:
        r.set(_key("sites", gateway), json.dumps(sites))
    except Exception:
        pass


# ─── Indexes (no-op for Redis) ───────────────────────────────────────
def ensure_indexes():
    logging.info("[Redis] no indexes needed (key-value store)")
