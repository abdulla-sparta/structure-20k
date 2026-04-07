# heatmap_feed.py
#
# Heatmap LTP cache — fed entirely by the V3 WebSocket tick stream.
# NO REST polling. Ticks arrive from UpstoxV3Client global listeners.
#
# Two modes:
#   Standalone (backtest/idle):  starts its own UpstoxV3Client
#   Live engine running:         registers on live engine's existing client
#                                (register_with_client called by MultiStrategyRunner)
#
# Cache: _ltp_cache[symbol] = {ltp, prev_close, change, change_pct, volume, atp, ts}
# /quotes route reads from this cache — zero REST calls to Upstox.

import threading
import logging
from datetime import datetime

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CONFIG, INSTRUMENTS

log = logging.getLogger(__name__)

# ── Shared state ──────────────────────────────────────────────────────────────
_ltp_cache:    dict = {}   # symbol → tick dict
_token_to_sym: dict = {}   # instrument_key → symbol
_lock          = threading.Lock()
_ws_client     = None
_started       = False

# Build token → symbol lookup once at import time
for _inst in INSTRUMENTS:
    _token_to_sym[_inst["token"]] = _inst["symbol"]


# ── Public API ────────────────────────────────────────────────────────────────

def get_all_ltps() -> dict:
    """Return a snapshot of the full LTP cache (thread-safe copy)."""
    with _lock:
        return dict(_ltp_cache)


def get_ltp(symbol: str) -> dict | None:
    """Return latest tick dict for one symbol, or None."""
    with _lock:
        return _ltp_cache.get(symbol)


def is_connected() -> bool:
    if _ws_client:
        return _ws_client.is_connected()
    return False


def cache_size() -> int:
    with _lock:
        return len(_ltp_cache)


# ── Tick handler (called by UpstoxV3Client global listener) ──────────────────

# SocketIO emitter — set by app.py after socketio is created
# Called directly from on_global_tick so EVERY tick reaches the browser
_socketio_emit = None   # fn(event, data) or None

def set_socketio_emitter(fn):
    """Called once by app.py to wire SocketIO into the heatmap feed."""
    global _socketio_emit
    _socketio_emit = fn
    log.info("HeatmapFeed: SocketIO emitter registered")


def on_global_tick(key: str, ltp: float, prev_close=None,
                   timestamp=None, volume=None, atp=None):
    """
    Receives every tick from the WS stream.
    Updates cache AND immediately emits to browser via SocketIO.
    No intermediate thread or polling loop needed.
    """
    sym = _token_to_sym.get(key)
    if not sym or not ltp or ltp <= 0:
        return

    with _lock:
        existing = _ltp_cache.get(sym, {})
        # Priority: WS-supplied prev_close (yesterday's close) > cached > ltp (fallback only)
        # NEVER lock onto first bad value — always accept fresh prev_close from WS
        pc = (prev_close if prev_close and prev_close > 0
              else existing.get("prev_close") if existing.get("prev_close", 0) > 0
              else ltp)
        chg     = round(ltp - pc, 2)
        chg_pct = round((chg / pc) * 100, 2) if pc and pc != ltp else 0.0
        ts_str  = (timestamp.strftime("%H:%M:%S")
                   if timestamp else datetime.now().strftime("%H:%M:%S"))

        entry = {
            "symbol":     sym,
            "ltp":        round(ltp, 2),
            "prev_close": round(pc, 2),
            "change":     chg,
            "change_pct": chg_pct,
            "volume":     int(volume) if volume else existing.get("volume", 0),
            "atp":        round(atp, 2) if atp else existing.get("atp"),
            "ts":         ts_str,
        }
        _ltp_cache[sym] = entry

    # Emit immediately to browser — no polling loop, true real-time
    # IMPORTANT: must pass namespace="/" when emitting from a background thread
    # (Flask-SocketIO threading mode requires explicit namespace outside request context)
    if _socketio_emit:
        try:
            _socketio_emit("heatmap_tick", {
                "data":      {sym: entry},
                "connected": True,
                "ts":        ts_str,
            })
        except Exception as _e:
            pass


# ── Seed cache from REST/CSV when market is closed ───────────────────────────

def seed_cache_if_empty():
    """
    Populate heatmap cache from Upstox REST or CSV when WS has no ticks.
    Called after WS connects and when market_info shows market is closed.
    Only runs if cache is empty — avoids overwriting live tick data.
    """
    global _ltp_cache
    with _lock:
        already_seeded = bool(_ltp_cache)
    if already_seeded:
        return  # already have data from WS ticks

    log.info("[HeatmapFeed] Cache empty after WS connect — seeding from REST/CSV")
    print("[HeatmapFeed] Seeding cache from REST/CSV (market closed)...")

    try:
        import requests as _req
        from config import CONFIG, INSTRUMENTS
        from datetime import datetime

        token = CONFIG.get("upstox_access_token", "")
        seeded = 0

        # Try REST first
        if token:
            try:
                keys = ",".join(inst["token"] for inst in INSTRUMENTS)
                resp = _req.get(
                    "https://api.upstox.com/v2/market-quote/quotes",
                    params={"instrument_key": keys},
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=8,
                )
                if resp.ok:
                    data = resp.json().get("data", {})
                    ts_str = datetime.now().strftime("%H:%M:%S")
                    for inst in INSTRUMENTS:
                        sym     = inst["symbol"]
                        api_key = inst["token"].replace("|", ":")
                        q = data.get(api_key) or data.get(inst["token"], {})
                        if not q:
                            for k, v in data.items():
                                if sym in k:
                                    q = v; break
                        if q:
                            ltp = q.get("last_price") or q.get("ltp") or 0
                            # net_change = Upstox-calculated change from yesterday's close
                            # This is the most reliable source — server computes it correctly
                            net_chg = q.get("net_change")      # absolute ₹ change
                            # ohlc.close = TODAY's close (not yesterday's!) — don't use as prev_close
                            # Derive prev_close from ltp - net_change
                            if net_chg is not None and ltp and ltp > 0:
                                pc  = round(ltp - net_chg, 2)
                            else:
                                pc  = _ltp_cache.get(sym, {}).get("prev_close") or ltp
                            if ltp and ltp > 0:
                                chg = round(ltp - pc, 2) if pc else 0
                                pct = round((chg / pc) * 100, 2) if pc and pc != ltp else 0
                                entry = {
                                    "symbol": sym, "ltp": round(ltp, 2), "prev_close": round(pc, 2),
                                    "change": chg, "change_pct": pct, "volume": q.get("volume", 0),
                                    "atp": q.get("average_trade_price") or None, "ts": ts_str,
                                }
                                with _lock:
                                    _ltp_cache[sym] = entry
                                seeded += 1
                    if seeded > 0:
                        print(f"[HeatmapFeed] REST seed: {seeded} symbols")
                        _emit_full_snapshot()
                        return
            except Exception as e:
                print(f"[HeatmapFeed] REST seed failed: {e}")

        # CSV fallback
        import pandas as pd, os
        ts_str = datetime.now().strftime("%H:%M:%S")
        for inst in INSTRUMENTS:
            sym  = inst["symbol"]
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"{sym}_1YEAR_1MIN.csv")
            try:
                df = pd.read_csv(path, parse_dates=["datetime"])
                df = df.sort_values("datetime")
                ltp = float(df["close"].iloc[-1])
                from datetime import date as _date
                last_day = df["datetime"].iloc[-1].date()
                prev_day = df[df["datetime"].dt.date < last_day]
                pc  = float(prev_day["close"].iloc[-1]) if len(prev_day) else ltp
                chg = round(ltp - pc, 2)
                pct = round((chg / pc) * 100, 2) if pc else 0
                entry = {
                    "symbol": sym, "ltp": ltp, "prev_close": pc,
                    "change": chg, "change_pct": pct, "volume": 0, "atp": None,
                    "ts": ts_str,
                }
                with _lock:
                    _ltp_cache[sym] = entry
                seeded += 1
            except Exception:
                pass
        if seeded > 0:
            print(f"[HeatmapFeed] CSV seed: {seeded} symbols (no socket emit — REST poll will correct prev_close)")
    except Exception as e:
        print(f"[HeatmapFeed] Seed error: {e}")


def _emit_full_snapshot():
    """Emit entire cache to browser in one shot via SocketIO."""
    if not _socketio_emit:
        return
    with _lock:
        snapshot = dict(_ltp_cache)
    if not snapshot:
        return
    try:
        _socketio_emit("heatmap_tick", {
            "data":      snapshot,
            "connected": False,
            "ts":        "market closed — CSV snapshot",
        })
        print(f"[HeatmapFeed] Emitted snapshot: {len(snapshot)} symbols")
    except Exception as e:
        log.debug(f"[HeatmapFeed] Emit snapshot error: {e}")


# ── Start modes ──────────────────────────────────────────────────────────────

def _rest_poll_loop():
    """
    Background REST poller — runs every 5s when no WS client is active.
    Keeps heatmap data fresh at startup and after engine stops.
    Stops automatically once a WS client (MSR or standalone) connects.
    """
    import time as _t
    import requests as _req
    from config import CONFIG, INSTRUMENTS
    print("[HeatmapFeed] REST poll loop started")
    while True:
        _t.sleep(5)
        if _ws_client and _ws_client.is_connected():
            _t.sleep(25)  # WS is live — long sleep, check again in 30s total
            continue   # WS is live — no REST polling needed
        token = CONFIG.get("upstox_access_token", "")
        if not token:
            continue
        try:
            keys = "|".join(i["token"] for i in INSTRUMENTS)
            r = _req.get(
                "https://api.upstox.com/v2/market-quote/quotes",
                params={"instrument_key": keys},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=5,
            )
            if r.status_code != 200:
                continue
            data = r.json().get("data", {})
            with _lock:
                for sym_key, q in data.items():
                    ltp    = q.get("last_price") or q.get("ltp") or 0
                    if not ltp:
                        continue
                    net_ch = q.get("net_change")  # ₹ change from yesterday's close
                    # sym_key is like "NSE_EQ:TRENT" — map to symbol name
                    sym = sym_key.split(":")[-1] if ":" in sym_key else sym_key
                    for inst in INSTRUMENTS:
                        ikey = inst["token"].replace("|", ":").split(":")[-1]
                        if ikey == sym or inst["symbol"] == sym:
                            sym = inst["symbol"]
                            break
                    # Use existing prev_close if already set correctly
                    # Only compute new prev_close if net_change is available (reliable)
                    existing_pc = _ltp_cache.get(sym, {}).get("prev_close", 0)
                    if net_ch is not None and net_ch != 0:
                        pc = round(ltp - net_ch, 2)  # derive from net_change (most reliable)
                    elif existing_pc and existing_pc != ltp:
                        pc = existing_pc  # keep existing good value
                    else:
                        continue  # can't determine prev_close reliably — skip this update
                    chg = round(ltp - pc, 2)
                    pct = round((chg / pc) * 100, 2) if pc else 0
                    _ltp_cache[sym] = {
                        "ltp": ltp, "prev_close": pc,
                        "change": chg, "change_pct": pct,
                        "volume": q.get("volume", 0), "atp": q.get("average_price", 0),
                        "symbol": sym, "ts": "",
                    }
            # Emit updated cache so frontend gets correct prev_close
            if _socketio_emit:
                try:
                    _socketio_emit("heatmap_tick", {
                        "data": dict(_ltp_cache),
                        "connected": False,
                        "ts": "REST snapshot",
                    })
                except Exception:
                    pass
        except Exception as e:
            pass   # silent fail — REST poll is best-effort


def start_heatmap_feed():
    """
    Seed heatmap from REST/CSV and start background REST poller.
    Does NOT start a standalone WS — Upstox allows only one WS per token.
    The live engine's MSR client owns the WS when running.
    After engine stops, call start_standalone_ws() to restore live ticks.
    """
    global _ws_client, _started
    if _started:
        return
    _started = True
    import threading as _t
    _t.Thread(target=seed_cache_if_empty, daemon=True).start()
    _t.Thread(target=_rest_poll_loop, daemon=True, name="heatmap_rest_poll").start()
    print("[HeatmapFeed] REST poll started — no standalone WS (MSR owns WS when live)")


def start_standalone_ws():
    """
    Start a standalone WS ONLY after live engine is stopped.
    In normal operation, heatmap shares the live engine WS via register_with_client().
    """
    global _ws_client, _started
    stop_standalone()  # clean up first

    if not CONFIG.get("upstox_access_token", ""):
        print("[HeatmapFeed] No token — CSV/REST only after engine stop")
        return

    from live_engine.upstox_v3_client import UpstoxV3Client
    tokens     = [inst["token"] for inst in INSTRUMENTS]
    _ws_client = UpstoxV3Client(tokens=tokens)
    _ws_client.add_global_listener(on_global_tick)
    _ws_client.set_market_closed_callback(seed_cache_if_empty)
    _ws_client.start_in_thread()
    _started = True
    print(f"[HeatmapFeed] Standalone WS started (post-engine-stop) — {len(tokens)} instruments")


def stop_standalone():
    """Stop standalone WS if running (e.g. after engine was stopped)."""
    global _ws_client, _started
    if _ws_client:
        try:
            _ws_client.stop()
            print("[HeatmapFeed] Standalone WS stopped")
        except Exception as e:
            print(f"[HeatmapFeed] stop error: {e}")
    _ws_client = None
    _started   = False


def get_active_client():
    """
    Return the active WS client (standalone or shared).
    MSR should use this client instead of creating its own.
    """
    return _ws_client


def register_with_client(client):
    """
    Register heatmap listener on an EXISTING UpstoxV3Client
    (called by MultiStrategyRunner so both share one WS connection).
    Stops the standalone client first to avoid duplicate connections.
    """
    global _ws_client, _started
    # Stop standalone client first — Upstox kills both if two connections share a token
    stop_standalone()
    _started   = True
    _ws_client = client
    client.add_global_listener(on_global_tick)
    client.set_market_closed_callback(seed_cache_if_empty)
    print(f"[HeatmapFeed] Registered on live engine WS client (ObjId: {id(client)})")
    log.info("HeatmapFeed: registered on live engine WS client")