import asyncio
import hashlib
import hmac
import json
import logging
import random
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import engine as engine_mod
import mongo as mongo_mod
import proxy as proxy_mod
import shopify
import sites as sites_mod

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
FRONTEND = ROOT / "frontend"

# ── Logging setup ──────────────────────────────────────────────────────
LOG_FILE = ROOT / "main_py.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("webex")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()
BOT_TOKEN = str(CFG.get("bot_token", "")).strip()
BOT_NAME = str(CFG.get("bot_name", "")).strip()
OWNER_ID = int(CFG.get("owner_id") or 0)
ALLOWED = {int(i) for i in (CFG.get("allowed_ids") or [])}
DEV_KEY = str(CFG.get("dev_key", "")).strip()
TTL_HOURS = int(CFG.get("session_ttl_hours") or 24)
AUTH_MAX_AGE = int(CFG.get("auth_date_max_age") or 86400)

sessions: dict[str, dict] = {}  # in-memory fallback


# ─── WebSocket Connection Manager ─────────────────────────────────────
class ConnectionManager:
    """Manages WebSocket connections per session for real-time result push."""

    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}  # session_id → [ws]
        self._lock = asyncio.Lock()

    async def connect(self, session_id: str, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            if session_id not in self._connections:
                self._connections[session_id] = []
            self._connections[session_id].append(ws)

    async def disconnect(self, session_id: str, ws: WebSocket):
        async with self._lock:
            conns = self._connections.get(session_id, [])
            if ws in conns:
                conns.remove(ws)
            if not conns and session_id in self._connections:
                del self._connections[session_id]

    async def broadcast(self, session_id: str, message: dict):
        """Send message to all connected clients for a session."""
        async with self._lock:
            conns = self._connections.get(session_id, [])
        dead = []
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    try:
                        conns.remove(ws)
                    except ValueError:
                        pass

    def has_connections(self, session_id: str) -> bool:
        return bool(self._connections.get(session_id))


ws_manager = ConnectionManager()

app = FastAPI(title="Webex", docs_url=None, redoc_url=None)

# ── Startup cleanup ─────────────────────────────────────────────────────
# In-memory engine jobs are lost on restart — mark leftover running
# On startup: resume any sessions that were 'running' when server last stopped
# instead of marking them as stopped — tasks continue from where they left off.
@app.on_event("startup")
async def _startup_resume():
    # Ensure MongoDB indexes for faster queries
    try:
        mongo_mod.ensure_indexes()
        log.info("[STARTUP] MongoDB indexes ensured")
    except Exception as e:
        log.warning("[STARTUP] index error: %s", e)
    try:
        await engine_mod.resume_running_sessions()
        log.info("[STARTUP] resumed running sessions (or none to resume)")
    except Exception as e:
        log.warning("[STARTUP] resume error: %s", e)
    # Start the watchdog for auto-recovery
    try:
        asyncio.create_task(engine_mod.watchdog())
        log.info("[STARTUP] watchdog started")
    except Exception as e:
        log.warning("[STARTUP] watchdog start error: %s", e)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/dashboard")
async def dashboard():
    return FileResponse(FRONTEND / "dashboard.html")


@app.get("/shopify")
async def shopify_page():
    return FileResponse(FRONTEND / "dashboard.html")


@app.get("/proxy")
async def proxy_page():
    return FileResponse(FRONTEND / "dashboard.html")


@app.get("/sites")
async def sites_page():
    return FileResponse(FRONTEND / "dashboard.html")


@app.get("/settings")
async def settings_page():
    return FileResponse(FRONTEND / "dashboard.html")


@app.get("/health")
async def health():
    return {"status": "ok", "time": int(time.time())}


@app.get("/api/ping")
async def ping():
    return {"ok": True, "ts": int(time.time())}


@app.post("/api/check")
async def check_card_endpoint(request: Request):
    """Check a card through the Shopify API (/autossh)."""
    sess = bearer(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    cc = str(data.get("cc") or data.get("card") or "").strip()
    site = str(data.get("site") or "").strip()
    proxy = data.get("proxy") or None
    min_amount = float(data.get("min_amount") or 0)
    max_amount = float(data.get("max_amount") or 9999)
    session_id = str(data.get("session_id") or "").strip()

    log.info("CHECK REQ | card=%s | site=%s | proxy=%s | range=$%.2f-$%.2f", cc[:16]+"...", site or "auto", proxy or "none", min_amount, max_amount)

    # If no site specified, pick from user's sites filtered by amount range
    if not site:
        filtered = sites_mod.get_random_sites_by_amount(sess["uid"], min_amount, max_amount, limit=50)
        if filtered:
            site = random.choice(filtered)
        else:
            site = shopify.random_site()
        log.info("CHECK SITE | picked=%s (from %d matches)", site[:60], len(filtered) if filtered else 0)

    if not proxy:
        stored = mongo_mod.get_user_proxies(sess["uid"])
        if stored:
            proxy = random.choice(stored)
    if not cc:
        raise HTTPException(400, "cc is required")
    try:
        t0 = time.time()
        result = await shopify.check_card(cc, site, proxy)
        elapsed = round(time.time() - t0, 2)
        status = result.get("status", "UNKNOWN")
        resp_text = (result.get("response") or "")[:80]
        log.info("CHECK DONE | card=%s | status=%s | resp=%s | time=%.2fs", cc[:16]+"...", status, resp_text, elapsed)
        # Save result to session if session_id provided
        if session_id:
            mongo_mod.update_session_result(
                session_id, cc, status,
                response=result.get("response", ""),
                site=result.get("site", ""),
                gateway=result.get("gateway", ""),
                price=result.get("price"),
            )
    except Exception as e:
        log.error("CHECK FAIL | card=%s | error=%s: %s", cc[:16]+"...", type(e).__name__, str(e)[:120])
        raise HTTPException(500, f"Check failed: {type(e).__name__}: {str(e)[:150]}")
    return result


# ─── Proxy endpoints ───────────────────────────────────────────────────────

@app.get("/api/proxies")
async def proxies_list(request: Request):
    sess = bearer(request)
    rows = mongo_mod.get_user_proxies(sess["uid"])
    return {"count": len(rows), "proxies": rows}


@app.post("/api/proxies/check")
async def proxies_check(request: Request):
    """Bulk-check proxies (same Semaphore(50) as KYu7) and save live ones."""
    sess = bearer(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    raw = str(data.get("proxies") or "")
    timeout = int(data.get("timeout") or 10)
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    valid = [p for p in (proxy_mod.parse_proxy(l) for l in lines) if p]
    if not valid:
        raise HTTPException(400, "No valid proxies. Use host:port:user:pass or user:pass@host:port")

    progress = {"done": 0, "total": len(valid), "live": 0, "dead": 0}

    async def _cb(done, total, live, dead):
        progress.update(done=done, total=total, live=live, dead=dead)

    live = await proxy_mod.check_bulk(valid, timeout=timeout, progress_cb=_cb)
    added = proxy_mod.save_proxies_bulk(sess["uid"], [p["db"] for p in live])
    total_saved = len(mongo_mod.get_user_proxies(sess["uid"]))
    return {
        "total": len(valid),
        "live": len(live),
        "dead": len(valid) - len(live),
        "added": added,
        "saved": total_saved,
    }


@app.post("/api/proxies/delete")
async def proxies_delete(request: Request):
    sess = bearer(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    proxy_str = data.get("proxy") or None
    proxy_mod.delete_proxy(sess["uid"], proxy_str)
    total_saved = len(mongo_mod.get_user_proxies(sess["uid"]))
    return {"saved": total_saved}


# ─── Sites endpoints ─────────────────────────────────────────────────────────

@app.get("/api/sites")
async def sites_list(request: Request):
    """List all stored sites for the user. Falls back to file-based sites."""
    sess = bearer(request)
    rows = sites_mod.get_user_sites(sess["uid"])
    # If no MongoDB sites, use fallback
    if not rows:
        fallback = sites_mod._load_fallback_sites()
        rows = [{"url": u, "amount": "0.50", "gateway": "Shopify", "response": "N/A"} for u in fallback]
    return {"count": len(rows), "sites": rows}


@app.post("/api/sites/add")
async def sites_add(request: Request):
    """Add one or more sites. Body: { "urls": "domain1.com\ndomain2.com" } or { "url": "single.com" }"""
    sess = bearer(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    raw = str(data.get("urls") or data.get("url") or "").strip()
    if not raw:
        raise HTTPException(400, "url(s) required")
    urls = [l.strip() for l in raw.split("\n") if l.strip()]
    if len(urls) == 1:
        urls = [urls[0]]
    result = sites_mod.add_sites_bulk(sess["uid"], urls)
    total = len(sites_mod.get_user_sites(sess["uid"]))
    result["total"] = total
    return result


@app.post("/api/sites/remove")
async def sites_remove(request: Request):
    """Remove a site. Body: { "url": "domain.com" } or { "all": true }"""
    sess = bearer(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if data.get("all"):
        count = sites_mod.remove_all_sites(sess["uid"])
        return {"removed": count, "total": 0}
    url = str(data.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    ok = sites_mod.remove_site(sess["uid"], url)
    total = len(sites_mod.get_user_sites(sess["uid"]))
    return {"removed": 1 if ok else 0, "total": total}


@app.post("/api/sites/check")
async def sites_check(request: Request):
    """Check a single site via /autossh API with a test card."""
    sess = bearer(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    url = str(data.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")

    proxy = None
    stored = mongo_mod.get_user_proxies(sess["uid"])
    if stored:
        proxy = random.choice(stored)

    from shopify.checker import call_autossh, classify, parse_domain, random_site as _rs
    domain = parse_domain(url) or _rs()
    test_card = "4031630227127791|02|2030|680"

    start = time.time()
    result_data = await call_autossh(test_card, domain, proxy)
    elapsed_ms = int((time.time() - start) * 1000)

    raw_response = str(result_data.get("Response") or "Unknown")
    status = classify(raw_response)
    gateway = str(result_data.get("Gateway") or "Shopify")
    price = str(result_data.get("Price") or "0.00")
    proxy_ip = str(result_data.get("ProxyIP") or "N/A")

    if status in ("CHARGED", "APPROVED"):
        check_status = "valid"
    elif status == "ERROR":
        check_status = "error"
    else:
        check_status = "invalid"

    sites_mod.update_site_check(
        sess["uid"], domain, raw_response, check_status,
        amount=price, gateway=gateway, proxy_ip=proxy_ip,
        time_str=f"{elapsed_ms}ms"
    )

    return {
        "url": domain,
        "status": check_status,
        "response": raw_response,
        "amount": price,
        "gateway": gateway,
        "proxy_ip": proxy_ip,
        "time": f"{elapsed_ms}ms",
    }


@app.post("/api/sites/check-all")
async def sites_check_all(request: Request):
    """Check all stored sites for the user."""
    sess = bearer(request)
    sites = sites_mod.get_user_sites(sess["uid"])
    if not sites:
        return {"total": 0, "valid": 0, "invalid": 0, "errors": 0, "results": []}

    proxy = None
    stored = mongo_mod.get_user_proxies(sess["uid"])
    if stored:
        proxy = random.choice(stored)

    from shopify.checker import call_autossh, classify, parse_domain

    test_card = "4031630227127791|02|2030|680"
    valid = invalid = errors = 0
    results = []

    for site_doc in sites:
        url = site_doc.get("url", "")
        domain = parse_domain(url)
        if not domain:
            invalid += 1
            continue
        try:
            start = time.time()
            data = await call_autossh(test_card, domain, proxy)
            elapsed_ms = int((time.time() - start) * 1000)
            raw = str(data.get("Response") or "Unknown")
            st = classify(raw)
            gw = str(data.get("Gateway") or "Shopify")
            price = str(data.get("Price") or "0.00")
            pip = str(data.get("ProxyIP") or "N/A")

            if st in ("CHARGED", "APPROVED"):
                cs = "valid"; valid += 1
            elif st == "ERROR":
                cs = "error"; errors += 1
            else:
                cs = "invalid"; invalid += 1

            sites_mod.update_site_check(
                sess["uid"], domain, raw, cs,
                amount=price, gateway=gw, proxy_ip=pip,
                time_str=f"{elapsed_ms}ms"
            )
            results.append({"url": domain, "status": cs, "response": raw, "amount": price})
        except Exception as e:
            errors += 1
            sites_mod.update_site_check(
                sess["uid"], domain, str(e)[:100], "error"
            )
            results.append({"url": domain, "status": "error", "response": str(e)[:100]})

    return {
        "total": len(sites),
        "valid": valid,
        "invalid": invalid,
        "errors": errors,
        "results": results[:50],
    }


@app.get("/api/sites/export")
async def sites_export(request: Request):
    """Export all site URLs as plain text."""
    sess = bearer(request)
    sites = sites_mod.get_user_sites(sess["uid"])
    lines = [s.get("url", "") for s in sites if s.get("url")]
    return {"count": len(lines), "text": "\n".join(lines)}


@app.get("/api/sites/stats")
async def sites_stats(request: Request):
    """Get min/max amount and count from user's stored sites."""
    sess = bearer(request)
    return sites_mod.get_site_amount_stats(sess["uid"])


@app.post("/api/check/info")
async def check_info(request: Request):
    """Return filtered sites count, proxy count, and calculated thread count. Also creates a session."""
    sess = bearer(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    min_amount = float(data.get("min_amount") or 0)
    max_amount = float(data.get("max_amount") or 9999)
    cards_count = int(data.get("cards_count") or 0)

    sites_count, proxies_count, threads = _check_threads(sess["uid"], min_amount, max_amount)

    session_id = secrets.token_hex(8).upper()

    # Create session in DB
    mongo_mod.create_session(
        user_id=sess["uid"], session_id=session_id,
        sites_count=sites_count, proxies_count=proxies_count,
        threads=threads, cards_count=cards_count,
        min_amount=min_amount, max_amount=max_amount,
    )

    log.info("CHECK INFO | user=%s | session=%s | sites=%d | proxies=%d | threads=%d",
             sess["uid"], session_id, sites_count, proxies_count, threads)

    return {
        "sites_count": sites_count, "proxies_count": proxies_count,
        "threads": threads, "session_id": session_id,
    }


def _check_threads(uid: int, min_amount: float, max_amount: float) -> tuple[int, int, int]:
    """Return (sites_count, proxies_count, threads).

    Default target: 250 threads. Threads share proxies (not 1:1),
    so proxy count does NOT cap thread count.
    """
    filtered_sites = sites_mod.get_random_sites_by_amount(uid, min_amount, max_amount, limit=99999)
    sites_count = len(filtered_sites)
    stored_proxies = mongo_mod.get_user_proxies(uid)
    proxies_count = len(stored_proxies)

    # Base: 75 threads default (reduced from 250 to prevent Railway overload)
    threads = 75

    # Only cap if we have very few sites (need at least 1 site per batch)
    if sites_count > 0:
        threads = min(threads, sites_count * 5)  # at least 5 threads per site
    threads = max(10, threads)

    return sites_count, proxies_count, threads


@app.post("/api/check/start")
async def check_start(request: Request):
    """Start a background checking job on the server.

    Body: { "cards": ["cc|mm|yy|cvv", ...], "min_amount": float,
            "max_amount": float, "session_id": optional reuse }
    Returns immediately with session_id; the engine keeps checking even if
    the browser/page is closed. Poll GET /api/sessions/{id} for live results.
    """
    sess = bearer(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    cards = data.get("cards") or []
    if not isinstance(cards, list) or not cards:
        raise HTTPException(400, "cards list is required")
    min_amount = float(data.get("min_amount") or 0)
    max_amount = float(data.get("max_amount") or 9999)
    session_id = str(data.get("session_id") or "").strip()

    sites_count, proxies_count, threads = _check_threads(sess["uid"], min_amount, max_amount)

    # Reuse session created by /api/check/info, or create a new one
    if session_id:
        existing = mongo_mod.get_session(session_id, sess["uid"])
        if existing:
            mongo_mod.update_session_cards(session_id, len(cards), min_amount, max_amount, cards=cards)
        else:
            session_id = ""
    if not session_id:
        session_id = secrets.token_hex(8).upper()
        mongo_mod.create_session(
            user_id=sess["uid"], session_id=session_id,
            sites_count=sites_count, proxies_count=proxies_count,
            threads=threads, cards_count=len(cards),
            min_amount=min_amount, max_amount=max_amount,
            cards=cards,
        )

    # Skip start_job if session already has active workers (prevents duplicate checking)
    from engine import ACTIVE as _ACTIVE
    if session_id not in _ACTIVE:
        await engine_mod.start_job(
            uid=sess["uid"], session_id=session_id, cards=cards,
            threads=threads, min_amount=min_amount, max_amount=max_amount,
        )
    else:
        log.info("CHECK START (already active) | user=%s | session=%s", sess["uid"], session_id)

    log.info("CHECK START | user=%s | session=%s | cards=%d | sites=%d | proxies=%d | threads=%d",
             sess["uid"], session_id, len(cards), sites_count, proxies_count, threads)

    return {
        "session_id": session_id, "threads": threads,
        "sites_count": sites_count, "proxies_count": proxies_count,
    }


# ─── Session endpoints ────────────────────────────────────────────────

@app.get("/api/sessions")
async def sessions_list(request: Request):
    """List all sessions for the user."""
    sess = bearer(request)
    sessions = mongo_mod.get_user_sessions(sess["uid"])
    return {"sessions": sessions}


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str, request: Request):
    """Get a single session with results."""
    sess = bearer(request)
    session = mongo_mod.get_session(session_id, sess["uid"])
    if not session:
        raise HTTPException(404, "Session not found")
    return session


@app.post("/api/sessions/stop")
async def session_stop(request: Request):
    """Stop a running session."""
    sess = bearer(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    sid = data.get("session_id", "")
    if not sid:
        raise HTTPException(400, "session_id required")
    engine_mod.stop_job(sid)
    mongo_mod.stop_session(sid)
    # Notify WebSocket clients
    await ws_manager.broadcast(sid, {"type": "status", "status": "stopped"})
    return {"ok": True}


# ─── WebSocket for real-time results ──────────────────────────────────
@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for real-time card checking results.

    Connect with: ws://host/ws/{session_id}?token={bearer_token}
    Server pushes: {type: "result", card, status, response, site, gateway, price}
                  {type: "status", status: "running"|"done"|"stopped"}
                  {type: "stats", checked, live, charged, dead}
    """
    # Auth via query param
    token = websocket.query_params.get("token", "")
    sess = sessions.get(token)
    if not sess or sess.get("exp", 0) < time.time():
        # Try MongoDB
        sess = mongo_mod.get_auth_session(token)
    if not sess:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await ws_manager.connect(session_id, websocket)
    log.info("[WS] connected session=%s user=%s", session_id, sess["uid"])
    try:
        # Send current session state immediately
        session = mongo_mod.get_session(session_id, sess["uid"])
        if session:
            await websocket.send_json({
                "type": "init",
                "status": session.get("status", "unknown"),
                "checked": session.get("cards_checked", 0),
                "total": session.get("cards_count", 0),
                "live": session.get("live", 0),
                "charged": session.get("charged", 0),
                "dead": session.get("dead", 0),
                "results": (session.get("results") or [])[-50:],  # last 50
                "cards": session.get("cards", []),
            })
        # Keep connection alive — wait for disconnect
        while True:
            data = await websocket.receive_text()
            # Client can send "ping" or other commands
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("[WS] error session=%s: %s", session_id, e)
    finally:
        await ws_manager.disconnect(session_id, websocket)
        log.info("[WS] disconnected session=%s", session_id)


@app.get("/api/config")
async def public_config():
    return {
        "bot_username": str(CFG.get("bot_username", "")).strip(),
        "bot_name": BOT_NAME,
        "owner_tab": bool(DEV_KEY and OWNER_ID),
    }


def verify_telegram(data: dict) -> bool:
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
        raise HTTPException(500, "Server config missing: bot_token")
    auth = {k: v for k, v in data.items() if k != "hash"}
    check = "\n".join(f"{k}={auth[k]}" for k in sorted(auth))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, str(data.get("hash", "")))


def can_access(uid: int) -> bool:
    if OWNER_ID and uid == OWNER_ID:
        return True
    return uid in ALLOWED


def bearer(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing session token")
    token = auth[7:]
    # Check in-memory first (fast), then MongoDB (persistent)
    sess = sessions.get(token)
    if sess and sess["exp"] > time.time():
        return sess
    # Try MongoDB
    sess = mongo_mod.get_auth_session(token)
    if sess:
        # Cache in memory for faster access
        sessions[token] = sess
        return sess
    raise HTTPException(401, "Session expired")


def issue_session(uid: int, first_name: str, username: str) -> tuple[str, dict]:
    token = secrets.token_urlsafe(32)
    exp = time.time() + TTL_HOURS * 3600
    sess_data = {
        "uid": uid,
        "first_name": first_name,
        "username": username,
        "exp": exp,
    }
    # Store in both memory and MongoDB
    sessions[token] = sess_data
    mongo_mod.save_auth_session(token, sess_data, exp)
    return token, {
        "id": uid,
        "first_name": first_name,
        "username": username,
    }


@app.post("/api/login")
async def login(request: Request):
    data = await request.json()
    if not verify_telegram(data):
        raise HTTPException(401, "Invalid Telegram signature")
    auth_date = int(data.get("auth_date") or 0)
    if time.time() - auth_date > AUTH_MAX_AGE:
        raise HTTPException(401, "Login data too old")
    uid = int(data.get("id") or 0)
    if not uid or not can_access(uid):
        raise HTTPException(403, "Access denied")
    token, user = issue_session(uid, data.get("first_name", ""), data.get("username", ""))
    return {"token": token, "user": user}


@app.post("/api/dev-login")
async def dev_login(request: Request):
    if not DEV_KEY or not OWNER_ID:
        raise HTTPException(404, "Owner login disabled")
    data = await request.json()
    if not secrets.compare_digest(str(data.get("dev_key", "")), DEV_KEY):
        raise HTTPException(401, "Invalid dev key")
    token, user = issue_session(OWNER_ID, "Owner", "owner")
    return {"token": token, "user": user}


@app.get("/api/me")
async def me(request: Request):
    return {"user": bearer(request)}


@app.post("/api/logout")
async def logout(request: Request):
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        sessions.pop(token, None)
        mongo_mod.delete_auth_session(token)
    return {"ok": True}


# ── CLI Endpoints (no bearer auth, uses dev_key) ────────────────────────
@app.get("/api/cli/sites")
async def cli_sites(request: Request):
    """CLI: Get all sites. Auth: dev_key query param."""
    dk = request.query_params.get("dev_key", "")
    if not secrets.compare_digest(dk, DEV_KEY):
        raise HTTPException(401, "Invalid dev key")
    sites = sites_mod.get_user_sites(OWNER_ID)
    return {"count": len(sites), "sites": sites}


@app.get("/api/cli/proxy")
async def cli_proxy(request: Request):
    """CLI: Get all proxies. Auth: dev_key query param."""
    dk = request.query_params.get("dev_key", "")
    if not secrets.compare_digest(dk, DEV_KEY):
        raise HTTPException(401, "Invalid dev key")
    proxies = proxy_mod.get_user_proxies(OWNER_ID)
    return {"count": len(proxies), "proxies": proxies}


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
