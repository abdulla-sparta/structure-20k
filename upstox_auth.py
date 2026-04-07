# upstox_auth.py
#
# Upstox OAuth 2.0 — auto daily token flow.
#
# How Upstox OAuth works:
#   1. Your app redirects user to Upstox login URL
#   2. User logs in with their Upstox credentials
#   3. Upstox redirects back to YOUR redirect_uri with ?code=AUTH_CODE
#   4. Your app exchanges that code for access_token via POST
#   5. access_token is valid until 3:30 AM next day
#
# This module:
#   - Provides /auth/login  → redirects to Upstox login page
#   - Provides /auth/callback → receives code, exchanges for token,
#                               stores in DB + CONFIG, restarts WS
#   - On app startup: loads last saved token from DB if still valid
#   - Token auto-refreshes: a background thread checks at 3:31 AM daily
#     and opens the login URL (sends Telegram notification with login link)
#
# Setup (one time):
#   1. In Upstox developer console, set redirect URI to:
#      http://YOUR_SERVER_IP:5000/auth/callback
#   2. Add to .env:
#      UPSTOX_API_KEY=your_api_key
#      UPSTOX_API_SECRET=your_api_secret
#      UPSTOX_REDIRECT_URI=http://YOUR_SERVER_IP:5000/auth/callback

import os
import json
import requests
import threading
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode
from config import CONFIG
import db

log = logging.getLogger(__name__)

UPSTOX_AUTH_URL  = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _api_key() -> str:
    return os.getenv("UPSTOX_API_KEY", CONFIG.get("upstox_api_key", "")).strip()

def _api_secret() -> str:
    return os.getenv("UPSTOX_API_SECRET", "").strip()

def _redirect_uri() -> str:
    return os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:5000/auth/callback").strip()


# ── Token storage in DB ───────────────────────────────────────────────────────

def save_token(access_token: str):
    """Save token to DB, .env file, and update live CONFIG so WS picks it up."""
    # 1. Save to DB
    db.set("upstox_access_token", {
        "token":    access_token,
        "saved_at": datetime.now().isoformat(),
    })
    # 2. Update live CONFIG immediately
    CONFIG["upstox_access_token"] = access_token

    # 3. Write back to .env so token survives app restarts
    try:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            lines = open(env_path).readlines()
            updated = []
            token_written = False
            for line in lines:
                if line.startswith("UPSTOX_ACCESS_TOKEN"):
                    updated.append(f"UPSTOX_ACCESS_TOKEN={access_token}\n")
                    token_written = True
                else:
                    updated.append(line)
            if not token_written:
                updated.append(f"UPSTOX_ACCESS_TOKEN={access_token}\n")
            open(env_path, "w").writelines(updated)
            print(f"✅ Token written to .env")
    except Exception as e:
        print(f"⚠ Could not write token to .env: {e}")

    print(f"✅ Token saved to DB and CONFIG updated")


def load_token_from_db() -> str | None:
    """
    Load last saved token from DB.
    Returns token string if found, None otherwise.
    Upstox tokens expire at 3:30 AM IST = 22:00 UTC previous day.
    We treat a token as valid if saved within the last 24 hours
    and the current IST time is before 3:30 AM IST.
    """
    try:
        data = db.get("upstox_access_token")
        if not data or not data.get("token"):
            return None
        saved_at = datetime.fromisoformat(data["saved_at"])
        now_utc  = datetime.utcnow()

        # If saved_at is naive, treat as UTC
        if saved_at.tzinfo is not None:
            saved_at = saved_at.replace(tzinfo=None)  # strip tz for comparison

        age_hours = (now_utc - saved_at).total_seconds() / 3600

        # Upstox tokens last ~24h (expire at 3:30 AM IST = 22:00 UTC)
        # Accept token if less than 30 hours old (generous buffer)
        if age_hours > 30:
            print(f"[Auth] Saved token is expired ({age_hours:.1f}h old)")
            return None

        # Additional check: if current UTC time is past 22:00 (= 3:30 AM IST)
        # and token was saved before today's 22:00 UTC, it's expired
        cutoff_utc = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
        if now_utc >= cutoff_utc and saved_at < cutoff_utc:
            print("[Auth] Saved token expired (past 3:30 AM IST cutoff)")
            return None

        return data["token"]
    except Exception as e:
        log.warning(f"[Auth] Could not load token from DB: {e}")
        return None


def is_token_valid() -> bool:
    """Check if current CONFIG token is non-empty."""
    return bool(CONFIG.get("upstox_access_token", ""))


# ── OAuth URL builder ─────────────────────────────────────────────────────────

def get_login_url() -> str:
    """Build Upstox OAuth login URL to redirect user to."""
    params = {
        "response_type": "code",
        "client_id":     _api_key(),
        "redirect_uri":  _redirect_uri(),
    }
    return f"{UPSTOX_AUTH_URL}?{urlencode(params)}"


# ── Token exchange ────────────────────────────────────────────────────────────

def exchange_code_for_token(auth_code: str) -> str | None:
    """
    Exchange OAuth authorization code for access_token.
    Called by /auth/callback route when Upstox redirects back.
    Returns access_token string or None on failure.
    """
    try:
        resp = requests.post(
            UPSTOX_TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept":       "application/json",
            },
            data={
                "code":          auth_code,
                "client_id":     _api_key(),
                "client_secret": _api_secret(),
                "redirect_uri":  _redirect_uri(),
                "grant_type":    "authorization_code",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"❌ Token exchange failed: {resp.status_code} — {resp.text[:300]}")
            return None
        token = resp.json().get("access_token")
        if token:
            print(f"✅ Token received from Upstox")
            return token
        print(f"❌ No access_token in response: {resp.json()}")
        return None
    except Exception as e:
        print(f"❌ Token exchange error: {e}")
        return None


# ── Startup: restore token from DB ───────────────────────────────────────────

def restore_token_on_startup():
    """
    Called at app startup. If a valid token is saved in DB, load it
    into CONFIG so WS can start immediately without manual login.
    """
    token = load_token_from_db()
    if token:
        CONFIG["upstox_access_token"] = token
        print(f"✅ Token restored from DB — WS will start automatically")
        return True
    else:
        print(f"⚠ No valid token in DB — visit /auth/login to authenticate")
        return False


# ── Daily auto-refresh scheduler ──────────────────────────────────────────────

def start_daily_token_scheduler():
    """
    Background thread that waits until 3:31 AM daily, then sends a
    Telegram notification with the login URL so you can re-authenticate
    from your phone. Runs forever as daemon.
    """
    def _scheduler():
        import time
        while True:
            now  = datetime.now()
            # Next 3:31 AM
            next_refresh = now.replace(hour=3, minute=31, second=0, microsecond=0)
            if next_refresh <= now:
                next_refresh += timedelta(days=1)

            wait_secs = (next_refresh - datetime.now()).total_seconds()
            print(f"[Auth] Next token refresh check at {next_refresh.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"({wait_secs/3600:.1f}h)")
            time.sleep(max(wait_secs, 0))

            # Token expired — send Telegram alert with login link
            login_url = get_login_url()
            msg = (
                f"🔐 *StructureEngine — Token Expired*\n\n"
                f"Upstox token has expired (3:30 AM cutoff).\n\n"
                f"👉 [Click here to re-login]({login_url})\n\n"
                f"Or open this URL in your browser:\n`{login_url}`"
            )
            _send_telegram(msg)
            print(f"[Auth] Token expired — sent Telegram alert with login URL")
            time.sleep(60)   # prevent double-fire

    t = threading.Thread(target=_scheduler, daemon=True, name="TokenScheduler")
    t.start()
    return t


def _send_telegram(msg: str):
    """Send message via Telegram (non-blocking, best effort)."""
    bot_token = CONFIG.get("telegram_bot_token", "")
    chat_id   = CONFIG.get("telegram_chat_id", "")
    if not bot_token or not chat_id:
        print(f"[Auth] Telegram not configured — login URL: {get_login_url()}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       msg,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
    except Exception as e:
        log.warning(f"[Auth] Telegram send failed: {e}")