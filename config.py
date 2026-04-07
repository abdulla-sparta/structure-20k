# config.py — all secrets from .env, never hardcoded

import os
from dotenv import load_dotenv

load_dotenv()

# ── INSTRUMENTS ───────────────────────────────────────────────────────────────
# 28 confirmed symbols from watchlist. Max 50 total (add via live dashboard UI).
# margin_pct = Upstox intraday margin % — update when you share the full table.
# For BACKTEST qty: qty = int(balance × risk_per_trade / risk_points)
# For LIVE qty:     qty = int(balance / (price × margin_pct))
INSTRUMENTS = [
    {"symbol": "TRENT",      "token": "NSE_EQ|INE849A01020", "margin_pct": 0.20},
    {"symbol": "TITAN",      "token": "NSE_EQ|INE280A01028", "margin_pct": 0.20},
    {"symbol": "OFSS",       "token": "NSE_EQ|INE881D01027", "margin_pct": 0.20},
    {"symbol": "PERSISTENT", "token": "NSE_EQ|INE262H01021", "margin_pct": 0.20},
    {"symbol": "MPHASIS",    "token": "NSE_EQ|INE356A01018", "margin_pct": 0.20},
    {"symbol": "PIDILITIND", "token": "NSE_EQ|INE318A01026", "margin_pct": 0.20},
    {"symbol": "CUMMINSIND", "token": "NSE_EQ|INE298A01020", "margin_pct": 0.20},
    {"symbol": "BAJAJ-AUTO", "token": "NSE_EQ|INE917I01010", "margin_pct": 0.20},
    {"symbol": "APOLLOHOSP", "token": "NSE_EQ|INE437A01024", "margin_pct": 0.20},
    {"symbol": "CIPLA",      "token": "NSE_EQ|INE059A01026", "margin_pct": 0.20},
    {"symbol": "ASIANPAINT", "token": "NSE_EQ|INE021A01026", "margin_pct": 0.20},
    {"symbol": "LT",         "token": "NSE_EQ|INE018A01030", "margin_pct": 0.20},
    {"symbol": "VEDL",       "token": "NSE_EQ|INE205A01025", "margin_pct": 0.20},
    {"symbol": "TVSMOTOR",   "token": "NSE_EQ|INE494B01023", "margin_pct": 0.20},
    {"symbol": "DIVISLAB",   "token": "NSE_EQ|INE361B01024", "margin_pct": 0.20},
    {"symbol": "AUROPHARMA", "token": "NSE_EQ|INE406A01037", "margin_pct": 0.20},
    {"symbol": "SUNPHARMA",  "token": "NSE_EQ|INE044A01036", "margin_pct": 0.20},
    {"symbol": "MAXHEALTH",  "token": "NSE_EQ|INE027H01010", "margin_pct": 0.20},
    {"symbol": "ALKEM",      "token": "NSE_EQ|INE540L01014", "margin_pct": 0.20},
    {"symbol": "WIPRO",      "token": "NSE_EQ|INE075A01022", "margin_pct": 0.20},
    {"symbol": "INFY",       "token": "NSE_EQ|INE009A01021", "margin_pct": 0.20},
    {"symbol": "M&M",        "token": "NSE_EQ|INE101A01026", "margin_pct": 0.20},
    {"symbol": "MARUTI",     "token": "NSE_EQ|INE585B01010", "margin_pct": 0.20},
    {"symbol": "BOSCHLTD",   "token": "NSE_EQ|INE323A01026", "margin_pct": 0.20},
    {"symbol": "RELIANCE",   "token": "NSE_EQ|INE002A01018", "margin_pct": 0.20},
    {"symbol": "HCLTECH",    "token": "NSE_EQ|INE860A01027", "margin_pct": 0.20},
    {"symbol": "TCS",        "token": "NSE_EQ|INE467B01029", "margin_pct": 0.20},
    {"symbol": "BHARTIARTL", "token": "NSE_EQ|INE397D01024", "margin_pct": 0.20},
]

MAX_SYMBOLS = 50   # hard cap — UI enforces this

# Quick symbol → margin_pct lookup
MARGIN_TABLE = {i["symbol"]: i["margin_pct"] for i in INSTRUMENTS}

CONFIG = {

    # ── MODE ──────────────────────────────────────────────────────────────────
    # IMPORTANT: Keep as "backtest" — app.py switches to "live" only when
    # the live engine is explicitly started. Never leave as "live" at rest
    # or backtest runs will restore live engine state and corrupt results.
    "mode":          "backtest",
    "backtest_mode": True,

    # ── ACCOUNT ───────────────────────────────────────────────────────────────
    "starting_balance": 200000,   # ₹2L per symbol, independent

    # ── QTY SIZING (risk-based) ────────────────────────────────────────────────
    # qty = int(balance × risk_per_trade / risk_points)
    # risk_points = abs(entry_price - stop_price)
    "risk_per_trade": 0.01,       # 1% default — user sets in backtest UI

    # ── STRATEGY ──────────────────────────────────────────────────────────────
    "rr_target":          3,
    "cooldown":           20,     # 5m candles
    "min_price_distance": 5,

    # ── TIMEFRAMES ────────────────────────────────────────────────────────────
    "htf_minutes":    15,
    "ltf_minutes":     5,
    "swing_lookback": 20,

    # ── SESSION (IST) ─────────────────────────────────────────────────────────
    "entry_start_time": "09:20",
    "entry_end_time":   "14:30",   # Block entries after 14:30 — RR 1:5 needs time to play out
    "force_exit_time":  "15:25",

    # ── TRAILING KILL SWITCH (live only) ──────────────────────────────────────
    "kill_switch_enabled": True,
    "kill_switch_percent": 0.90,

    # ── PORTFOLIO DAILY LOSS LIMIT ────────────────────────────────────────────
    "portfolio_daily_loss_limit": 50000,

    # ── ORDER BOOK EXECUTION ──────────────────────────────────────────────────
    # When enabled: signals go through liquidity filter + limit order fill
    # instead of instant market fill at candle close price.
    # Set to False to revert to original behaviour instantly.
    "order_book_enabled":    False,   # ← flip to True when ready to test
    "ob_spread_max_pct":     0.0005,  # 0.05% — reject if spread wider than this
    "ob_imbalance_levels":   5,       # depth levels for imbalance calculation
    "ob_imbalance_min":      0.55,    # BUY needs bid ratio >= this
    "ob_imbalance_max":      0.45,    # SELL needs bid ratio <= this
    "ob_fill_timeout_secs":  5,       # simulate fill check for 5s
    "ob_poll_interval_secs": 1.0,     # check fill every 1s

    # ── CREDENTIALS (from .env) ───────────────────────────────────────────────
    "upstox_access_token": os.getenv("UPSTOX_ACCESS_TOKEN", ""),
    "upstox_api_key":      os.getenv("UPSTOX_API_KEY", ""),
    "upstox_ws_url":       os.getenv("UPSTOX_WS_URL",
                               "wss://api.upstox.com/v3/feed/market-data-feed"),

    # ── TELEGRAM (from .env) ──────────────────────────────────────────────────
    "telegram_enabled":   True,
    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id":   os.getenv("TELEGRAM_CHAT_ID", ""),
}