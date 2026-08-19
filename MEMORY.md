# WebEx — Memory & Preferences

> Ye file project ki memory hai. Har session start par ise padho, project ko samjho, aur iske conventions follow karo. Credentials yahan hardcode nahi — `config.json` se aate hain.

## Project Overview

WebEx ek web console hai jisme do parts hain:
- **Frontend** — login + dashboard (HTML/CSS/Vanilla JS)
- **Backend** — Python FastAPI (auth + API)

Login **Telegram OAuth** se hota hai. Owner/whitelisted user hi access kar sakta hai.

## Folder Structure

```
webex/
├── config.json          # Credentials + settings (real secrets yahan hain)
├── MEMORY.md            # Ye file (memory/preferences)
├── test/                # Test files yahan store hote hain (scripts, logs, fixtures)
├── backend/
│   ├── main.py          # FastAPI app — pura backend
│   ├── requirements.txt
│   └── .venv/           # Test environment (gitignore-worthy)
└── frontend/
    ├── index.html       # Login page (tabs: Telegram / Owner)
    ├── styles.css       # Dark OLED + Glassmorphism design system
    ├── app.js           # Login flow + widget + tabs
    ├── dashboard.html   # Post-login dashboard
    └── dashboard.js     # Session check + logout
```

## Credentials (config.json)

| Key | Value |
|-----|-------|
| `bot_token` | `8817688817:...` |
| `bot_username` | `OmKimi_bot` |
| `bot_name` | `OmKimi` |
| `owner_id` | `6426931258` |
| `dev_key` | owner quick-login secret (testing) |
| `allowed_ids` | extra whitelisted user IDs |

**Rule:** Token/keys kabhi code mein hardcode mat karo — sirf `config.json` se load karo. Backend startup pe `config.json` read hota hai.

## Tech Stack

- **Backend:** Python 3.12 + FastAPI + Uvicorn (`backend/requirements.txt`)
- **Frontend:** Pure HTML5 + CSS3 + Vanilla JS (no build step)
- **Auth:** Telegram OAuth (Login Widget) — HMAC-SHA256 signature verify + owner/whitelist check
- **Session:** In-memory bearer token (secrets.token_urlsafe), TTL 24h

## Design System (ui-ux-pro-max se generate)

- **Theme:** Dark OLED (`#020617` bg) + Glassmorphism (backdrop blur 16px)
- **Accent:** `#16A34A` (green), Card `#0E1223`, Border `#334155`, Muted `#94A3B8`
- **Typography:** Fira Sans (body) + Fira Code (mono)
- **Motion:** 180-300ms transitions, glow effects, `prefers-reduced-motion` respected
- **A11y:** contrast 4.5:1, visible focus, keyboard nav, cursor-pointer on clickables

## Login Flow

1. Frontend `app.js` fetches `/api/config` → bot username
2. Two tabs: **Telegram** (OAuth widget) aur **Owner** (dev_key quick login)
3. Telegram tab: widget `onauth` → `POST /api/login` → backend HMAC verify
4. Owner tab: `POST /api/dev-login` → dev_key match → session as owner
5. Success → token localStorage → redirect `/dashboard`

## Run / Test

```bash
cd /root/webex/backend
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Test endpoints:
- `GET /health` → `{"status":"ok"}`
- `GET /api/config` → bot username (public)
- `POST /api/login` → Telegram OAuth verify
- `POST /api/dev-login` → `{"dev_key":"..."}` owner quick login
- `GET /api/me` → session check (Bearer token)
- `POST /api/logout`

Test files `test/` folder mein rakhe jate hain. Har test script ko wahan store karo, production code se alag.

## Deployment Notes

Backend kahin aur deploy hoga (Railway likely). Frontend backend hi serve karta hai, isliye ek service = dono. Telegram OAuth widget ke liye **BotFather mein `/setdomain`** set karna zaruri hai.

## Preference — Stable Public URL

- **Public URL kabhi change nahi hona chahiye.** Testing ke liye trycloudflare quick tunnel temporary hai (har restart pe naya URL) — production/stable testing ke liye use nahi karna.
- **Stable URL ke liye:** Railway deploy (`*.up.railway.app` fixed domain) ya named Cloudflare tunnel with custom domain.
- Jab bhi public URL dena ho, pehle stable solution (Railway) prefer karo, quick tunnel sirf emergency/one-off ke liye.

## Rules

- **Variables config.json mein save honge, Railway env vars mein nahi.** Saare secrets, tokens, keys, URLs — sab config.json mein store karo. Railway pe variables set nahi karna.
- **Browser tool use nahi karna.** (gstack browse binary / headless browser / screenshot tooling) — layout/viewing ke liye browser automation allowed nahi. Code + static verification se kaam karna hai.
- **Tunnel URL kabhi change nahi karna.** Quick tunnel restart mat karo. Backend/frontend restart karo sirf (`kill $(lsof -t -i:8000)` + uvicorn start). Tunnel same rahega har baar. Agar tunnel dead hai to user ko batao, khud mat restart karo.
- **Backend/Frontend restart karo, URL same rahega.** Naya tunnel image mat banao. Sirf restart karo.
