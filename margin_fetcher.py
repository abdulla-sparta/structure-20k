# margin_fetcher.py
#
# Fetches REAL intraday (MTF) margin % for each instrument from Upstox API.
# Stores in DB under key "margins" as {symbol: margin_pct, ...}
# Also computes max_qty = floor(balance × margin_pct / price) for each symbol.
#
# Called:
#   1. At app startup (once)
#   2. Daily at 09:10 IST before market open via scheduler
#   3. Manually via GET /refresh_margins
#
# Upstox API: POST /v2/charges/margin
#   margin_pct = total_margin / 100  (e.g. 20% margin → 0.20)
#   leverage   = 1 / margin_pct      (e.g. 0.20 → 5x)
#   max_qty    = floor(balance * leverage / price)
#             = floor(balance / (price * margin_pct))
#
# Example: TRENT ₹3740, margin 20%, balance ₹2L
#   max_qty = 200000 / (3740 × 0.20) = 200000 / 748 = 267 shares
#   margin needed = 3740 × 267 × 0.20 = ₹1,99,476 ✅ fits in ₹2L

import requests
import db
import logging
from config import CONFIG, INSTRUMENTS

log = logging.getLogger(__name__)

# Fallback margin if API fails (conservative 20% = 5x MTF)
FALLBACK_MARGIN_PCT = 0.20


def fetch_margins(balance: float = None) -> dict:
    """
    Fetch MTF intraday margin% for all INSTRUMENTS from Upstox.
    Returns dict: {symbol: {"margin_pct": 0.20, "leverage": 5.0, "token": "..."}}
    Also stores result in DB under key "margins".
    """
    if balance is None:
        balance = CONFIG.get("capital_per_symbol", 200000)

    token = CONFIG.get("upstox_access_token", "")
    if not token:
        log.warning("No access token — using fallback margins")
        return _fallback_margins()

    url     = "https://api.upstox.com/v2/charges/margin"
    headers = {
        "accept":        "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }

    results = {}

    # Upstox margin API takes one instrument at a time
    for inst in INSTRUMENTS:
        sym  = inst["symbol"]
        ikey = inst["token"]

        payload = {
            "instruments": [{
                "instrument_key":  ikey,
                "quantity":        1,
                "transaction_type": "BUY",
                "product":         "I",   # I = Intraday (MTF)
            }]
        }

        try:
            r = requests.post(url, headers=headers, json=payload, timeout=8)
            if not r.ok:
                log.warning(f"  {sym}: API error {r.status_code} — using fallback")
                results[sym] = _sym_fallback(inst)
                continue

            data = r.json()
            margin_data = data.get("data", {}).get("margins", [])
            if not margin_data:
                log.warning(f"  {sym}: empty margin response — using fallback")
                results[sym] = _sym_fallback(inst)
                continue

            raw_margin = margin_data[0].get("total_margin", 20.0)  # % e.g. 20.0

            # Upstox returns margin as percentage (e.g. 20.0 = 20%)
            # BUT for qty formula we need it as fraction (0.20)
            # If raw_margin > 1 it's a percentage; if <= 1 it's already fraction
            if raw_margin > 1:
                margin_pct = raw_margin / 100.0
            else:
                margin_pct = raw_margin   # already fraction

            # Safety: clamp between 5% (20x max) and 100% (1x, no leverage)
            margin_pct = max(0.05, min(1.0, margin_pct))
            leverage   = round(1.0 / margin_pct, 2)

            results[sym] = {
                "margin_pct": margin_pct,
                "leverage":   leverage,
                "token":      ikey,
                "source":     "api",
            }
            log.info(f"  {sym}: margin={margin_pct*100:.1f}% ({leverage:.1f}x)")

        except Exception as e:
            log.warning(f"  {sym}: exception {e} — using fallback")
            results[sym] = _sym_fallback(inst)

    # Store in DB
    db.set("margins", results)
    log.info(f"✅ Margins fetched and stored for {len(results)} symbols")
    return results


def _sym_fallback(inst: dict) -> dict:
    """Conservative 20% margin fallback for one symbol."""
    return {
        "margin_pct": inst.get("margin_pct", FALLBACK_MARGIN_PCT),
        "leverage":   round(1.0 / inst.get("margin_pct", FALLBACK_MARGIN_PCT), 2),
        "token":      inst["token"],
        "source":     "fallback",
    }


def _fallback_margins() -> dict:
    """Conservative fallback for ALL symbols (no API call)."""
    return {
        inst["symbol"]: _sym_fallback(inst)
        for inst in INSTRUMENTS
    }


def get_margins() -> dict:
    """
    Get margins from DB (fast, no API call).
    Falls back to config values if DB empty.
    """
    stored = db.get("margins")
    if stored:
        return stored
    return _fallback_margins()


def get_margin_pct(symbol: str) -> float:
    """Get margin fraction for one symbol. e.g. 0.20 for 20% MTF margin."""
    margins = get_margins()
    return margins.get(symbol, {}).get("margin_pct", FALLBACK_MARGIN_PCT)


def calc_max_qty(symbol: str, price: float, balance: float) -> int:
    """
    Max qty = floor(balance / (price × margin_pct))
    This is the HARD UPPER LIMIT — you physically can't buy more with your capital.

    Example: TRENT ₹3740, margin 20%, balance ₹2L
      max_qty = 200000 / (3740 × 0.20) = 267
    """
    if price <= 0:
        return 1
    margin_pct = get_margin_pct(symbol)
    max_qty = int(balance / (price * margin_pct))
    return max(1, max_qty)


if __name__ == "__main__":
    # Run directly to test: python margin_fetcher.py
    import logging
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    margins = fetch_margins()
    print("\n📊 Margin Table:")
    print(f"{'Symbol':<14} {'Margin%':>8} {'Leverage':>9} {'Source':>10}")
    print("-" * 46)
    for sym, m in sorted(margins.items()):
        print(f"{sym:<14} {m['margin_pct']*100:>7.1f}% {m['leverage']:>8.1f}x {m['source']:>10}")