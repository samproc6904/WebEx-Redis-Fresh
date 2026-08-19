"""BACKGROUND CHECKING ENGINE — runs card checks on the server.

Browser submits a batch via POST /api/check/start; the engine spawns async
workers that keep checking even if the browser/page is closed. Results go
straight into the session document in MongoDB; the browser polls
GET /api/sessions/{id} for live updates.

Auto-recovery: a watchdog runs every 30s and:
  1. Detects sessions stuck in MongoDB (running but no activity for 120s)
  2. Detects sessions whose workers all died (in ACTIVE but no progress)
  3. Respawns workers for pending cards in both cases
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import mongo as mongo_mod
import shopify
import sites as sites_mod

log = logging.getLogger("webex")

ACTIVE: dict[str, dict] = {}
_lock = asyncio.Lock()

MAX_ERROR_RETRIES = 1        # reduced from 2 — checker.py already retries PoolTimeout
WATCHDOG_INTERVAL = 30       # seconds between watchdog sweeps
STUCK_TIMEOUT = 120          # seconds of inactivity before considered stuck
HEARTBEAT_INTERVAL = 15      # seconds between heartbeats
API_COOLDOWN = 0.25          # seconds between requests per thread (80 concurrent × 10s each = 8 req/s throughput)


async def start_job(uid: int, session_id: str, cards: list[str],
                    threads: int, min_amount: float = 0,
                    max_amount: float = 9999) -> None:
    """Create an in-memory job and spawn worker tasks."""
    clean = []
    for c in cards:
        c = str(c).strip()
        if c:
            clean.append(c)
    if not clean:
        return
    # Kill any existing workers for this session to prevent duplicate checking
    existing = ACTIVE.get(session_id)
    if existing:
        log.warning("[ENGINE] session=%s already running (idx=%d) — killing old workers",
                    session_id, existing.get("idx", 0))
        existing["stop"] = True
        existing["generation"] = existing.get("generation", 0) + 1  # Invalidate old workers
        for t in existing.get("tasks", []):
            if not t.done():
                t.cancel()
    job = {
        "generation": existing.get("generation", 1) if existing else 1,
        "uid": uid,
        "session_id": session_id,
        "queue": clean,
        "idx": 0,
        "stop": False,
        "threads": max(1, threads),
        "min_amount": min_amount,
        "max_amount": max_amount,
        "tasks": [],
        "done": False,
        "last_activity": time.time(),
        "created_at": time.time(),
    }
    async with _lock:
        ACTIVE[session_id] = job
    job["tasks"] = [
        asyncio.create_task(_worker(job, i))
        for i in range(job["threads"])
    ]
    # Watcher marks the job done once all workers finish
    asyncio.create_task(_watch(job))
    # Touch activity in MongoDB so watchdog doesn't immediately flag it
    try:
        mongo_mod.touch_session(session_id)
    except Exception:
        pass
    log.info("[ENGINE] started session=%s cards=%d threads=%d",
             session_id, len(clean), job["threads"])


def stop_job(session_id: str) -> None:
    """Signal workers to stop (called from /api/sessions/stop)."""
    job = ACTIVE.get(session_id)
    if job:
        job["stop"] = True
        log.info("[ENGINE] stop requested session=%s", session_id)


async def resume_running_sessions() -> None:
    """Resume any sessions that were 'running' when the server restarted.

    Called once at startup. Reads session data from MongoDB, computes
    pending cards (all cards minus already-checked), and spawns new workers.
    """
    sessions = mongo_mod.get_running_sessions()
    if not sessions:
        return
    log.info("[ENGINE] resuming %d running session(s)", len(sessions))
    for s in sessions:
        sid = s.get("session_id", "")
        uid = s.get("user_id", 0)
        cards = s.get("cards", [])
        checked = mongo_mod.get_session_checked_cards(sid)
        pending = [c for c in cards if c not in checked]
        if not pending:
            log.info("[ENGINE] session=%s already complete (no pending cards), marking done", sid)
            mongo_mod.finish_session(sid)
            continue
        log.info("[ENGINE] resuming session=%s pending=%d/%d", sid, len(pending), len(cards))
        await start_job(
            uid=uid, session_id=sid, cards=pending,
            threads=s.get("threads", 10),
            min_amount=s.get("min_amount", 0),
            max_amount=s.get("max_amount", 9999),
        )


def active_session_ids() -> list[str]:
    return list(ACTIVE.keys())


async def _next_card(job: dict) -> Optional[str]:
    """Thread-safe queue pop."""
    async with _lock:
        if job["stop"] or job["idx"] >= len(job["queue"]):
            return None
        card = job["queue"][job["idx"]]
        job["idx"] += 1
        return card


async def _worker(job: dict, worker_idx: int = 0) -> None:
    uid = job["uid"]
    sid = job["session_id"]
    min_amt = job["min_amount"]
    max_amt = job["max_amount"]
    my_generation = job.get("generation", 1)
    # Stagger worker startup: spread initial requests over 3 seconds
    await asyncio.sleep((worker_idx % 75) * 0.04)
    while True:
        # Check if this worker's generation is still current
        if job.get("generation", 1) != my_generation:
            log.debug("[ENGINE] worker %d exiting - generation changed (%d -> %d)",
                      worker_idx, my_generation, job.get("generation", 1))
            break
        card = await _next_card(job)
        if card is None:
            break
        site = _pick_site(uid, min_amt, max_amt)
        proxy = _pick_proxy(uid)
        result = None
        for attempt in range(1, MAX_ERROR_RETRIES + 2):
            try:
                result = await shopify.check_card(card, site, proxy)
            except Exception as e:
                log.warning("[ENGINE] check exception card=%s attempt=%d err=%s",
                            card[:16] + "...", attempt, str(e)[:120])
            if result and result.get("status") != "ERROR":
                break
            # On error, retry with backup API explicitly
            if attempt <= MAX_ERROR_RETRIES:
                site = _pick_site(uid, min_amt, max_amt)
                # Force backup API if primary failed
                if attempt == 1 and result and "Application not found" in str(result.get("response", "")):
                    log.warning("[ENGINE] Primary API down, forcing backup API")
                await asyncio.sleep(0.5)
        # Check generation again before saving
        if job.get("generation", 1) != my_generation:
            log.debug("[ENGINE] skipping result save - generation changed")
            break
        if result is None:
            result = {
                "card": card, "site": site, "status": "ERROR",
                "response": "Engine check failed", "gateway": "NONE",
                "price": None,
            }
        # Skip if job was stopped (prevents in-flight result from duplicate job)
        if job.get("stop"):
            break
        try:
            mongo_mod.update_session_result(
                sid, card, result.get("status", "ERROR"),
                response=result.get("response", ""),
                site=result.get("site", ""),
                gateway=result.get("gateway", ""),
                price=result.get("price"),
            )
        except Exception as e:
            log.warning("[ENGINE] save result error: %s", e)
        # Broadcast result via WebSocket (instant push to frontend)
        try:
            import main as main_mod
            await main_mod.ws_manager.broadcast(sid, {
                "type": "result",
                "card": card,
                "status": result.get("status", "ERROR"),
                "response": result.get("response", ""),
                "site": result.get("site", ""),
                "gateway": result.get("gateway", ""),
                "price": result.get("price"),
            })
        except Exception:
            pass  # WebSocket not critical — result already in MongoDB
        # Update in-memory heartbeat
        job["last_activity"] = time.time()
        # Cooldown between requests to prevent API flood
        await asyncio.sleep(API_COOLDOWN)


async def _watch(job: dict) -> None:
    """Remove job from ACTIVE once all workers finish."""
    try:
        await asyncio.gather(*job["tasks"])
    except Exception:
        pass
    async with _lock:
        ACTIVE.pop(job["session_id"], None)
    job["done"] = True
    sid = job["session_id"]
    # Mark session as finished in MongoDB (unless it was manually stopped)
    if not job.get("stop"):
        try:
            mongo_mod.finish_session(sid)
        except Exception as e:
            log.warning("[ENGINE] finish_session error: %s", e)
    # Broadcast final status via WebSocket
    try:
        import main as main_mod
        await main_mod.ws_manager.broadcast(sid, {
            "type": "status",
            "status": "stopped" if job.get("stop") else "done",
        })
    except Exception:
        pass
    log.info("[ENGINE] finished session=%s cards_done=%d",
             sid, job["idx"])


def _pick_site(uid: int, min_amount: float, max_amount: float) -> str:
    try:
        filtered = sites_mod.get_random_sites_by_amount(
            uid, min_amount, max_amount, limit=50)
        if filtered:
            return random.choice(filtered)
    except Exception:
        pass
    try:
        return shopify.random_site()
    except Exception:
        return "vukoo.com"


def _pick_proxy(uid: int) -> Optional[str]:
    try:
        stored = mongo_mod.get_user_proxies(uid)
        if stored:
            return random.choice(stored)
    except Exception:
        pass
    return None


# ─── Watchdog / Auto-recovery ─────────────────────────────────────────

async def _respawn_job(job: dict) -> None:
    """Kill old workers (if any) and spawn fresh ones for remaining cards."""
    sid = job["session_id"]
    # Increment generation to invalidate old workers
    job["generation"] = job.get("generation", 1) + 1
    # Cancel existing dead tasks
    for t in job.get("tasks", []):
        if not t.done():
            t.cancel()
    # Recalculate pending cards
    checked = mongo_mod.get_session_checked_cards(sid)
    pending = [c for c in job["queue"] if c not in checked]
    if not pending:
        log.info("[WATCHDOG] session=%s no pending cards left, finishing", sid)
        async with _lock:
            ACTIVE.pop(sid, None)
        job["done"] = True
        try:
            mongo_mod.finish_session(sid)
        except Exception:
            pass
        return
    # Reset job state
    job["queue"] = pending
    job["idx"] = 0
    job["stop"] = False
    job["done"] = False
    job["last_activity"] = time.time()
    job["tasks"] = [
        asyncio.create_task(_worker(job, i))
        for i in range(job["threads"])
    ]
    asyncio.create_task(_watch(job))
    try:
        mongo_mod.touch_session(sid)
    except Exception:
        pass
    log.info("[WATCHDOG] respawned session=%s pending=%d threads=%d gen=%d",
             sid, len(pending), job["threads"])


async def watchdog():
    """Background loop: detect stuck/crashed sessions and recover them.

    Runs every WATCHDOG_INTERVAL seconds. Two recovery paths:
    1. In ACTIVE but workers dead/stuck → respawn workers
    2. In MongoDB 'running' but NOT in ACTIVE (crash) → re-create job
    """
    await asyncio.sleep(10)  # initial grace period for server startup
    log.info("[WATCHDOG] started (interval=%ds, stuck_timeout=%ds)",
             WATCHDOG_INTERVAL, STUCK_TIMEOUT)
    while True:
        try:
            now = time.time()
            # ── Path 1: Check ACTIVE sessions for stuck workers ──
            async with _lock:
                active_ids = list(ACTIVE.keys())
            for sid in active_ids:
                job = ACTIVE.get(sid)
                if not job or job.get("done") or job.get("stop"):
                    continue
                elapsed = now - job.get("last_activity", job.get("created_at", now))
                alive_tasks = [t for t in job.get("tasks", []) if not t.done()]
                if elapsed > STUCK_TIMEOUT and not alive_tasks:
                    # All workers died and no progress → respawn
                    log.warning("[WATCHDOG] session=%s stuck (%.0fs, 0 alive workers) → respawning",
                                sid, elapsed)
                    await _respawn_job(job)
                elif elapsed > STUCK_TIMEOUT and alive_tasks:
                    # Workers alive but no progress → could be slow API, just log
                    if int(elapsed) % 60 == 0:  # log every ~60s to avoid spam
                        log.info("[WATCHDOG] session=%s slow but alive (%.0fs, %d workers)",
                                 sid, elapsed, len(alive_tasks))

            # ── Path 2: Check MongoDB for crashed sessions (not in ACTIVE) ──
            stuck_sessions = mongo_mod.get_stuck_sessions(STUCK_TIMEOUT)
            for s in stuck_sessions:
                sid = s.get("session_id", "")
                if sid in ACTIVE:
                    continue  # already being handled by Path 1
                if not s.get("cards"):
                    continue
                checked = mongo_mod.get_session_checked_cards(sid)
                pending = [c for c in s["cards"] if c not in checked]
                if not pending:
                    log.info("[WATCHDOG] session=%s in DB but no pending cards, marking done", sid)
                    mongo_mod.finish_session(sid)
                    continue
                log.warning("[WATCHDOG] session=%s crashed (in DB, not in ACTIVE) → recovering %d pending cards",
                            sid, len(pending))
                # Re-create the job in memory
                job = {
                    "uid": s.get("user_id", 0),
                    "session_id": sid,
                    "queue": pending,
                    "idx": 0,
                    "stop": False,
                    "threads": max(1, s.get("threads", 10)),
                    "min_amount": s.get("min_amount", 0),
                    "max_amount": s.get("max_amount", 9999),
                    "tasks": [],
                    "done": False,
                    "last_activity": time.time(),
                    "created_at": time.time(),
                }
                async with _lock:
                    ACTIVE[sid] = job
                job["tasks"] = [
                    asyncio.create_task(_worker(job))
                    for _ in range(job["threads"])
                ]
                asyncio.create_task(_watch(job))
                try:
                    mongo_mod.touch_session(sid)
                except Exception:
                    pass
                log.info("[WATCHDOG] recovered session=%s pending=%d threads=%d",
                         sid, len(pending), job["threads"])

        except Exception as e:
            log.error("[WATCHDOG] error: %s", e)

        await asyncio.sleep(WATCHDOG_INTERVAL)