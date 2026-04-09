# tier_classifier.py
#
# Automatically classifies any stock into Tier 1 / 2 / 3
# based on its backtest performance using the Gross-to-Charge ratio.
#
# ── TIER DEFINITIONS ──────────────────────────────────────────────────────────
#
#   Tier 1 (G/C > 2.0x) — Strong trending stock
#     → risk=1.5%   cooldown=40 candles  min_stop=0.3%  max_stop=1.2%
#     → "Let it run" — higher allocation, tighter cooldown
#
#   Tier 2 (G/C 1.0–2.0x) — Moderate, tradeable with discipline
#     → risk=1.0%   cooldown=50 candles  min_stop=0.3%  max_stop=1.2%
#     → Standard settings
#
#   Tier 3 (G/C < 1.0x) — Choppy / strategy doesn't suit
#     → SKIPPED in live trading entirely
#     → Still shown in backtest UI with tier labels
#
# ── G/C RATIO ─────────────────────────────────────────────────────────────────
#   G/C = avg_gross_per_trade / avg_charge_per_trade
#   Computed from a fixed "classification backtest" using neutral params
#   so the ratio reflects the STOCK's characteristics, not the config.
#
# ── CLASSIFICATION BACKTEST PARAMS ────────────────────────────────────────────
#   These are fixed — never change — so all stocks are compared on equal footing.
#   risk=0.01  rr=4  cooldown=50  stop_filter=0.3%-1.2%
#
# ── PERSISTENCE ───────────────────────────────────────────────────────────────
#   Tiers saved to data/tier_cache.json
#   Re-classified:
#     • When a new stock is added (auto, before first live trade)
#     • Every Sunday at midnight (scheduled reclassification)
#     • Manually via /reclassify endpoint
#
# ── USAGE ─────────────────────────────────────────────────────────────────────
#   from tier_classifier import get_tier_config, classify_symbol, reclassify_all
#
#   config = get_tier_config("TRENT")
#   # → {"tier": 1, "risk_per_trade": 0.15, "cooldown": 40, ...}
#
#   classify_symbol("NEWSTOCK")   # classify a single new stock
#   reclassify_all(symbols)       # reclassify all — run Sunday night

import json
import os
import pandas as pd
from datetime import datetime

# ── Constants ─────────────────────────────────────────────────────────────────

CACHE_PATH = "data/tier_cache.json"

# Fixed neutral params used for classification backtest
# Do NOT change these — they define the benchmark
_CLASS_PRESET = {
    "risk_per_trade":     0.01,
    "rr_target":          4,
    "cooldown":           50,
    "min_price_distance": 5,
}
_CLASS_MIN_STOP_PCT = 0.003   # 0.3%
_CLASS_MAX_STOP_PCT = 0.012   # 1.2%

# G/C thresholds
# ── Tuned 2026-03-07 ──────────────────────────────────────────────────────────
# T1 > 2.5  : TRENT(7.14), CUMMINSIND(3.52), M&M(2.93)          → 3 stocks
# T2 1.15–2.5: PERSISTENT(1.80), OFSS(1.73), RELIANCE(1.59),
#              BOSCHLTD(1.56), MPHASIS(1.19)                      → 5 stocks
# T3 < 1.15 : HCLTECH(2.44 demoted), MARUTI(1.07), VEDL(1.02)… skipped
# Result: 8 live stocks, best backtest config rr=4 T1cd=30 T2cd=50 → ₹10.91L
TIER1_THRESHOLD = 2.5   # G/C > 2.5 → Tier 1
TIER2_THRESHOLD = 1.15  # G/C 1.15–2.5 → Tier 2
                         # G/C < 1.15 → Tier 3

# Per-tier live trading config
TIER_CONFIGS = {
    1: {
        "tier":               1,
        "label":              "Tier 1 — Strong trend",
        "risk_per_trade":     0.015,  # 1.5% of balance risked per trade
        "cooldown":           30,     # 30 × 5min = 150min between entries
        "min_stop_pct":       0.003,
        "max_stop_pct":       0.012,
        "rr_target":          4,
    },
    2: {
        "tier":               2,
        "label":              "Tier 2 — Moderate",
        "risk_per_trade":     0.010,  # 1.0% of balance risked per trade
        "cooldown":           50,     # 50 × 5min = 250min between entries
        "min_stop_pct":       0.003,
        "max_stop_pct":       0.012,
        "rr_target":          4,
    },
    3: {
        "tier":               3,
        "label":              "Tier 3 — Skipped (choppy)",
        "risk_per_trade":     0.0,    # zero — no live trades
        "cooldown":           999,
        "min_stop_pct":       0.003,
        "max_stop_pct":       0.012,
        "rr_target":          4,
    },
}

# ── In-process lock + memory cache ───────────────────────────────────────────
# Prevents multiple threads classifying the same symbol simultaneously.
# _mem_cache holds results for the lifetime of the process — no repeated I/O.

import threading
_classify_lock  = threading.Lock()
_mem_cache: dict = {}   # symbol → classification result (in-memory fast path)

# ── Cache I/O ─────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            return json.load(open(CACHE_PATH))
        except Exception:
            pass
    return {}


def _save_cache(cache: dict):
    os.makedirs("data", exist_ok=True)
    json.dump(cache, open(CACHE_PATH, "w"), indent=2)


# ── Core classification ───────────────────────────────────────────────────────

def _compute_class_metrics(symbol: str) -> tuple[float | None, float | None]:
    """
    Run classification backtest on symbol.
    Returns:
      (gc_ratio, return_pct)
      gc_ratio   = avg_gross / avg_charge per trade
      return_pct = final backtest return percentage
    Returns None if CSV missing or no trades generated.
    """
    # Lazy import to avoid circular deps
    from backtest_runner import run_backtest
    import engine.ltf_entry as le

    # Patch LTFEntry with stop% filter for classification
    original_LTF = le.LTFEntry

    class _FilteredLTF(le.LTFEntry):
        def update(self, candle, bias):
            sig = super().update(candle, bias)
            if sig is None:
                return None
            stop_pct = abs(sig["entry"] - sig["stop"]) / sig["entry"]
            if stop_pct < _CLASS_MIN_STOP_PCT or stop_pct > _CLASS_MAX_STOP_PCT:
                return None
            return sig

    le.LTFEntry = _FilteredLTF
    import backtest_runner as br
    br.LTFEntry = le.LTFEntry

    try:
        result = run_backtest(symbol, _CLASS_PRESET)
    finally:
        le.LTFEntry = original_LTF
        br.LTFEntry = original_LTF

    if result.get("error") or not result.get("trades"):
        return None, None

    tl = pd.DataFrame(result["trade_log"])
    if len(tl) == 0:
        return None, None

    avg_gross  = tl["gross_pnl"].mean()
    avg_charge = tl["charges"].mean()

    if avg_charge <= 0:
        return None, None

    return round(avg_gross / avg_charge, 3), float(result.get("return_pct", 0.0))


def _gc_to_tier(gc: float | None, return_pct: float | None) -> int:
    # Hard safety filter:
    # if the neutral classification backtest is not profitable,
    # force Tier 3 (skip) regardless of G/C.
    if gc is None or return_pct is None:
        return 3
    if return_pct <= 0:
        return 3
    if gc >= TIER1_THRESHOLD:
        return 1
    if gc >= TIER2_THRESHOLD:
        return 2
    return 3


# ── Public API ────────────────────────────────────────────────────────────────

def classify_symbol(symbol: str, force: bool = False) -> dict:
    """
    Classify a single symbol. Uses in-memory cache first, then disk cache.
    Thread-safe: only one classification per symbol runs at a time.
    force=True ignores both caches and re-runs the backtest.

    Returns:
        {
            "symbol":    "TRENT",
            "tier":      1,
            "gc_ratio":  7.1,
            "label":     "Tier 1 — Strong trend",
            "classified_at": "2026-03-06 09:00:00",
            "config":    { risk_per_trade, cooldown, ... }
        }
    """
    # ── Fast path: in-memory cache (no I/O, no backtest) ─────────────────────
    if not force and symbol in _mem_cache:
        entry = _mem_cache[symbol]
        return {**entry, "config": TIER_CONFIGS[entry["tier"]]}

    # ── Acquire lock: only ONE thread classifies a symbol at a time ──────────
    with _classify_lock:

        # Re-check mem cache inside lock — another thread may have finished
        if not force and symbol in _mem_cache:
            entry = _mem_cache[symbol]
            return {**entry, "config": TIER_CONFIGS[entry["tier"]]}

        # ── Disk cache check ─────────────────────────────────────────────────
        if not force:
            disk = _load_cache()
            if symbol in disk:
                entry = disk[symbol]
                _mem_cache[symbol] = entry   # warm the mem cache
                return {**entry, "config": TIER_CONFIGS[entry["tier"]]}

        # ── Actually classify (runs backtest once) ───────────────────────────
        print(f"[Classifier] Classifying {symbol}...")
        gc, return_pct = _compute_class_metrics(symbol)
        tier = _gc_to_tier(gc, return_pct)

        entry = {
            "symbol":        symbol,
            "tier":          tier,
            "gc_ratio":      gc,
            "return_pct":    round(return_pct, 2) if return_pct is not None else None,
            "label":         TIER_CONFIGS[tier]["label"],
            "classified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # Write to both caches
        _mem_cache[symbol] = entry
        cache = _load_cache()
        cache[symbol] = entry
        _save_cache(cache)

        rp = entry["return_pct"]
        print(f"[Classifier] {symbol} → Tier {tier}  G/C={gc}x  Ret={rp}%  ({TIER_CONFIGS[tier]['label']})")
        return {**entry, "config": TIER_CONFIGS[tier]}


def reclassify_all(symbols: list[str],
                   on_progress=None) -> dict[str, dict]:
    """
    Reclassify all symbols. Called on Sunday night or manually.
    on_progress(symbol, result) called after each symbol completes.

    Returns dict of symbol → classification result.
    """
    print(f"[Classifier] Reclassifying {len(symbols)} symbols...")
    results = {}
    for i, sym in enumerate(symbols, 1):
        result = classify_symbol(sym, force=True)
        results[sym] = result
        if on_progress:
            on_progress(sym, result, i, len(symbols))
    print(f"[Classifier] Done. Tier breakdown: "
          f"T1={sum(1 for r in results.values() if r['tier']==1)} "
          f"T2={sum(1 for r in results.values() if r['tier']==2)} "
          f"T3={sum(1 for r in results.values() if r['tier']==3)}")
    return results


def get_tier_config(symbol: str) -> dict:
    """
    Get the tier config for a symbol.
    If not yet classified, classifies it first (auto-classify on first use).

    Returns TIER_CONFIGS[tier] merged with tier/gc metadata.
    Safe to call from live instrument_runner.
    """
    result = classify_symbol(symbol, force=False)
    return result["config"]


def get_all_tiers() -> dict[str, dict]:
    """Return full cache — used by UI to display tier table."""
    cache = _load_cache()
    return {sym: {**v, "config": TIER_CONFIGS[v["tier"]]}
            for sym, v in cache.items()}


def get_tier_summary() -> dict:
    """Summary counts for UI dashboard."""
    cache = _load_cache()
    t1 = [s for s, v in cache.items() if v["tier"] == 1]
    t2 = [s for s, v in cache.items() if v["tier"] == 2]
    t3 = [s for s, v in cache.items() if v["tier"] == 3]
    return {
        "tier1": t1, "tier2": t2, "tier3": t3,
        "total": len(cache),
        "classified": len(cache),
    }


def warm_mem_cache():
    """
    Load disk cache into memory at startup.
    Call once from app.py after import — eliminates all disk reads during trading.
    """
    global _mem_cache
    _mem_cache = _load_cache()
    print(f"[Classifier] Warmed mem cache: {len(_mem_cache)} symbols loaded")


# Auto-warm on import
try:
    warm_mem_cache()
except Exception:
    pass
