# app.py

import sys, os
# Ensure the project root is always in sys.path — fixes ModuleNotFoundError on Windows
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import threading
import json
import requests
from flask import Flask, render_template, request, jsonify, redirect
from flask_socketio import SocketIO, emit

import db
from config import CONFIG, INSTRUMENTS
from strategy_presets import (
    get_all_slots, save_to_slot, load_slot,
    lock_config, load_locked_config,
    SLOT_NAMES, DEFAULT_CONFIG,
)
from backtest_runner import run_backtest_all
from live_status import live_status
from reports import reports_bp

app = Flask(__name__)
app.config["SECRET_KEY"] = "trading-engine-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
app.register_blueprint(reports_bp)

live_runner = None
ob_runner   = None       # OB experiment runner (TRENT + M&M)
_signal_log = []   # in-memory signal history (last 200)
_runner_lock = threading.Lock()
_ob_lock     = threading.Lock()

# ── Data fetch state (Railway startup) ────────────────────────────────────────
_fetch_status = {"done": False, "running": False, "results": {}, "error": None}

# ── Classification state (runs after fetch, before backtest) ──────────────────
_classify_status = {
    "done":    False,
    "running": False,
    "results": {},    # symbol → {tier, gc_ratio, label}
    "error":   None,
}

# ── Backtest progress state ────────────────────────────────────────────────────
# Updated by run_backtest_all callback, polled by UI via /bt_progress
_bt_progress = {
    "running":   False,
    "config":    "",       # preset name being run
    "total":     0,
    "done":      0,
    "symbols":   {},       # symbol → {"status": "pending"|"ok"|"error", "result": {...}}
}


def _fetch_all_data_background(symbols_to_fetch=None):
    """
    Check CSVs for all (or specified) INSTRUMENTS.
    If CSV already exists → mark as "cached" (no Upstox call).
    If missing           → fetch from Upstox API.
    Updates _fetch_status so the UI can poll progress.
    """
    global _fetch_status
    _fetch_status["running"] = True
    _fetch_status["done"]    = False

    from fetch_upstox_history import fetch_symbol, csv_exists

    # Ensure token is loaded — restore from DB if CONFIG is empty (e.g. after restart)
    if not CONFIG.get("upstox_access_token", ""):
        try:
            from upstox_auth import load_token_from_db
            t = load_token_from_db()
            if t:
                CONFIG["upstox_access_token"] = t
                print("[Fetch] Token restored from DB for fetch")
            else:
                print("[Fetch] ⚠ No token in DB — fetches will fail")
        except Exception as te:
            print(f"[Fetch] Token load error: {te}")

    results = {}

    targets = [i for i in INSTRUMENTS
               if symbols_to_fetch is None or i["symbol"] in symbols_to_fetch]

    for inst in targets:
        sym = inst["symbol"]
        if csv_exists(sym):
            results[sym] = "cached"
            _fetch_status["results"] = dict(results)
            continue
        try:
            ok = fetch_symbol(sym, inst["token"], force=False)
            results[sym] = "ok" if ok else "failed"
        except Exception as e:
            print(f"[Fetch] {sym} error: {e}")
            results[sym] = f"error: {e}"
        _fetch_status["results"] = dict(results)

    _fetch_status["results"] = results
    _fetch_status["running"] = False
    _fetch_status["done"]    = True
    print("✅ Data check/fetch complete.")

    # ── Auto-classify after fetch ─────────────────────────────────────────────
    # Classify runs ONCE per symbol. Uses disk cache if already classified.
    # Only runs the classification backtest for symbols not yet in cache.
    # Emits progress to UI after each symbol so the step indicator updates.
    global _classify_status
    _classify_status.update({"done": False, "running": True, "results": {}, "error": None})
    socketio.emit("classify_status", _classify_status)

    try:
        from tier_classifier import classify_symbol
        syms_to_classify = [i["symbol"] for i in INSTRUMENTS
                            if symbols_to_fetch is None or i["symbol"] in symbols_to_fetch]

        for sym in syms_to_classify:
            # Skip symbols already in results (idempotent)
            if sym in _classify_status["results"]:
                continue
            try:
                result = classify_symbol(sym, force=False)   # cache-first, one backtest max
                _classify_status["results"][sym] = {
                    "tier":     result["tier"],
                    "gc_ratio": result["gc_ratio"],
                    "label":    result["label"],
                }
            except Exception as e:
                _classify_status["results"][sym] = {"tier": 3, "error": str(e)}
            # Emit once per symbol (not per poll request)
            socketio.emit("classify_status", dict(_classify_status))

    except Exception as e:
        _classify_status["error"] = str(e)

    _classify_status["running"] = False
    _classify_status["done"]    = True
    socketio.emit("classify_status", dict(_classify_status))
    print("✅ Classification complete.")


# ── JSON serialization helper ─────────────────────────────────────────────────

def _sanitize_for_json(obj):
    """
    Recursively convert non-JSON-serializable types so SocketIO emit never crashes.
    Handles: pandas Timestamp, numpy int/float, NaN, nested dicts/lists.
    """
    import pandas as pd
    import numpy as np
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(i) for i in obj]
    elif isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    elif isinstance(obj, float) and obj != obj:  # NaN check
        return None
    return obj


# ── Live runner ───────────────────────────────────────────────────────────────

def _start_live_runner():
    global live_runner
    with _runner_lock:
        if live_runner is not None:
            replaying = getattr(live_runner, '_replaying', False)
            if replaying:
                # Mid-replay — don't kill it, just return
                print("[App] Engine still replaying history — ignoring duplicate start")
                return
            if not live_runner.is_connected():
                print("[App] Stale runner detected (WS disconnected) — restarting")
                try:
                    live_runner.stop()
                except Exception:
                    pass
                live_runner = None
            else:
                return  # genuinely already running
        # Note: heatmap standalone WS is stopped inside MSR.start() just before
        # WS connect — after replay finishes — giving Upstox time to release.
        # Wire per-tick socketio emitter into instrument_runner
        try:
            import live_engine.instrument_runner as _ir
            def _emit_with_signal_log(event, data, **kwargs):
                if event == "signal_fired":
                    _signal_log.append(data)
                    if len(_signal_log) > 200:
                        _signal_log.pop(0)
                socketio.emit(event, data, **kwargs)
            _ir.set_socketio_emitter(_emit_with_signal_log)
            print("[App] Per-tick SocketIO emitter wired into instrument_runner")
        except Exception as _e:
            print(f"[App] instrument_runner emitter wire failed: {_e}")
        from live_engine.multi_strategy_runner import MultiStrategyRunner
        live_runner = MultiStrategyRunner()
        live_runner._replaying = True   # guard: don't kill during replay
        _engine_start_sid = getattr(threading.current_thread(), '_sid', None)

        def _start_and_signal():
            try:
                live_runner.start()   # blocks: replay + WS connect
            finally:
                live_runner._replaying = False

        threading.Thread(target=_start_and_signal, daemon=True).start()

        def push_loop():
            import time
            _signalled = False
            _last_connected = False
            while True:
                time.sleep(0.5)
                try:
                    runner = live_runner   # always use current global
                    if runner is None:
                        _signalled = False  # reset so engine_started fires again after restart
                        socketio.emit("live_update", {"connected": False, "instruments": [], "portfolio_equity": {}})
                        continue
                    instruments_status = _sanitize_for_json(runner.get_all_status())
                    portfolio_equity   = runner.get_portfolio_equity()
                    connected          = runner.is_connected()
                    first = instruments_status[0] if instruments_status else {}
                    live_status.update(
                        connected=connected,
                        instrument=first.get("symbol", ""),
                        ltp=first.get("ltp"),
                        instruments=instruments_status,
                        portfolio_equity=portfolio_equity,
                    )
                    # Emit engine_started once WS connects (after replay finishes)
                    if not _signalled and connected:
                        _signalled = True
                        socketio.emit("engine_started", {
                            "status": "ok",
                            "instruments": instruments_status,
                        })

                    socketio.emit("live_update", {
                        "connected":        connected,
                        "portfolio_equity": portfolio_equity,
                        "instruments":      instruments_status,
                    })
                    # Also push heatmap_tick for each instrument so heatmap updates
                    for inst in instruments_status:
                        sym = inst.get("symbol")
                        ltp = inst.get("ltp")
                        if sym and ltp:
                            socketio.emit("heatmap_tick", {
                                "data": {sym: {
                                    "symbol":     sym,
                                    "ltp":        ltp,
                                    "prev_close": inst.get("prev_close"),
                                    "change":     inst.get("change"),
                                    "change_pct": inst.get("change_pct"),
                                    "ts":         "",
                                }},
                                "connected": connected,
                                "ts": "",
                            })
                except Exception:
                    pass

        if not getattr(_start_live_runner, '_push_loop_running', False):
            _start_live_runner._push_loop_running = True
            threading.Thread(target=push_loop, daemon=True, name="push_loop").start()
        # heatmap_tick is now emitted directly from heatmap_feed.on_global_tick
        # via the socketio emitter wired at startup — no extra thread needed here


# ── Startup: kick off background data fetch ───────────────────────────────────

def _on_startup():
    os.makedirs("data", exist_ok=True)
    os.makedirs("state", exist_ok=True)
    db.init_db()   # create Postgres/SQLite tables if not exist

    # OAuth: restore saved token from DB so WS starts without manual login
    try:
        from upstox_auth import restore_token_on_startup, start_daily_token_scheduler
        restore_token_on_startup()
        start_daily_token_scheduler()   # sends Telegram alert at 3:31 AM daily
    except Exception as e:
        print(f"⚠ Auth module error: {e}")

    # EOD CSV updater — appends today's candles to CSVs at 3:31 PM daily
    try:
        from eod_updater import start_eod_scheduler
        start_eod_scheduler()
    except Exception as e:
        print(f"⚠ EOD updater error: {e}")

    # Startup gap-fill — fetch any missing days since last CSV date (runs in background)
    def _startup_gap_fill():
        import time as _t
        _t.sleep(5)   # let token restore finish first
        try:
            import subprocess, sys
            print("[GapFill] Checking CSV freshness on startup...")
            result = subprocess.run(
                [sys.executable, "fetch_upstox_history.py"],
                capture_output=True, text=True,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                timeout=120
            )
            lines = (result.stdout or "").strip().split("\n")
            updated = [l for l in lines if "✅" in l or "appending" in l]
            already = [l for l in lines if "up to date" in l]
            print(f"[GapFill] Done — {len(updated)} symbols updated, {len(already)} already current")
            if updated:
                for l in updated[:5]:
                    print(f"  {l.strip()}")
        except Exception as e:
            print(f"[GapFill] Startup gap-fill error: {e}")
    threading.Thread(target=_startup_gap_fill, daemon=True, name="StartupGapFill").start()

    # Fetch real MTF margins from Upstox (used for qty sizing)
    def _fetch_margins_bg():
        try:
            from margin_fetcher import fetch_margins
            fetch_margins()
            print("✅ Margins loaded")
        except Exception as e:
            print(f"⚠ Margin fetch failed: {e} — using fallback 20%")
    threading.Thread(target=_fetch_margins_bg, daemon=True).start()
    # Wire SocketIO emitter into heatmap_feed BEFORE starting WS.
    # on_global_tick calls socketio.emit() directly — no polling loop needed.
    try:
        import heatmap_feed as _hf
        _hf.set_socketio_emitter(socketio.emit)
        print("[App] SocketIO emitter wired into heatmap_feed")
    except Exception as e:
        print(f"⚠ SocketIO emitter wire failed: {e}")

    # Start heatmap WS feed (standalone when live engine is OFF)
    try:
        from heatmap_feed import start_heatmap_feed
        start_heatmap_feed()
    except Exception as e:
        print(f"⚠ HeatmapFeed not started: {e}")

    # Auto-start/stop is handled by Railway cron jobs:
    #   /cron/start  → fires at 09:00 IST (03:30 UTC) Mon–Fri
    #   /cron/stop   → fires at 15:31 IST (10:01 UTC) Mon–Fri
    # See railway.toml for cron schedule configuration.
    print("[App] Auto-start/stop via Railway cron — see railway.toml")

    # Restore last backtest results from DB so page refresh shows them
    try:
        bt_meta = db.get("bt_last_run_meta") or {}
        if bt_meta:
            summary = bt_meta.get("summary", {})
            _bt_progress.update({
                "running": False,
                "config":  bt_meta.get("preset", "Custom"),
                "total":   len(summary),
                "done":    len(summary),
                "symbols": {sym: {
                    "status":     "ok",
                    "return_pct": v.get("return_pct"),
                    "net_pnl":    v.get("net_pnl"),
                    "trades":     v.get("trades"),
                    "win_rate":   v.get("win_rate"),
                    "tier":       v.get("tier"),
                    "gc_ratio":   v.get("gc_ratio"),
                    "balance":    v.get("balance"),
                } for sym, v in summary.items()},
            })
            print(f"[BT] Restored last backtest from DB — {len(summary)} symbols ({bt_meta.get('run_time', '?')})")
    except Exception as e:
        print(f"[BT] Could not restore last backtest: {e}")


# ── Live trade log routes (DB-backed) ─────────────────────────────────────────
# RULE: Only LIVE paper trades are saved to DB (via paper_broker live_mode=True).
#       Backtest trades are in-memory only — returned in /run_backtest JSON
#       and shown in the backtest results UI. They are never written to DB.

@app.route("/refresh_margins", methods=["POST","GET"])
def refresh_margins():
    """Manually re-fetch MTF margins from Upstox and update DB."""
    try:
        from margin_fetcher import fetch_margins
        margins = fetch_margins()
        return jsonify({"status": "ok", "count": len(margins), "margins": margins})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/margins")
def get_margins_api():
    """Get stored margins for all symbols."""
    try:
        from margin_fetcher import get_margins, calc_max_qty
        margins = get_margins()
        balance = CONFIG.get("capital_per_symbol", 200000)
        # Enrich with example max_qty at current price (from CSV last close)
        result = {}
        for sym, m in margins.items():
            result[sym] = {
                "margin_pct": m["margin_pct"],
                "leverage":   m["leverage"],
                "margin_pct_display": f"{m['margin_pct']*100:.1f}%",
                "leverage_display":   f"{m['leverage']:.1f}x",
                "source": m.get("source","fallback"),
            }
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/live_trades/<symbol>")
def get_live_trades(symbol: str):
    """
    Live trade log for one symbol from DB.
    ?date=YYYY-MM-DD  → specific date  (default: today)
    ?date=all         → full history
    """
    from datetime import datetime
    symbol      = symbol.upper()
    date_filter = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    trades      = db.get_trades(symbol) if date_filter == "all"                   else db.get_trades(symbol, date_str=date_filter)
    wins  = sum(1 for t in trades if t.get("net_pnl", 0) > 0)
    total = len(trades)
    return jsonify({
        "symbol":  symbol,
        "date":    date_filter,
        "trades":  trades,
        "summary": {
            "total":     total,
            "wins":      wins,
            "losses":    total - wins,
            "win_rate":  round(wins / total * 100, 1) if total else 0,
            "gross_pnl": round(sum(t.get("gross_pnl", 0) for t in trades), 2),
            "charges":   round(sum(t.get("charges",   0) for t in trades), 2),
            "net_pnl":   round(sum(t.get("net_pnl",   0) for t in trades), 2),
        },
    })


@app.route("/live_trades_all")
def get_all_live_trades():
    """Today's live trades for ALL symbols. ?date=YYYY-MM-DD or ?date=all"""
    from datetime import datetime
    date_filter = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    result = {}
    for inst in INSTRUMENTS:
        sym    = inst["symbol"]
        trades = db.get_trades(sym) if date_filter == "all"                  else db.get_trades(sym, date_str=date_filter)
        if trades:
            result[sym] = trades
    return jsonify(result)


# ── Backtest dashboard ────────────────────────────────────────────────────────

@app.route("/")
def index():
    locked = load_locked_config()
    return render_template(
        "index.html",
        config=CONFIG,
        locked=locked,
        slots=get_all_slots(),
        slot_names=SLOT_NAMES,
        instruments=INSTRUMENTS,
    )


@app.route("/run_backtest", methods=["POST"])
def api_run_backtest():
    """
    Run backtest for selected symbols with user config + 3 preset comparisons.
    Pushes per-symbol progress via SocketIO as each symbol completes.
    Also polls-able via GET /bt_progress.
    """
    global _bt_progress
    data = request.json or {}
    symbols = data.get("symbols", None)

    custom_preset = {
        "name":               data.get("name", "Custom"),
        "risk_per_trade":     float(data.get("risk_per_trade", CONFIG["risk_per_trade"])),
        "rr_target":          float(data.get("rr_target",      CONFIG["rr_target"])),
        "cooldown":           int(data.get("cooldown",          CONFIG["cooldown"])),
        "min_price_distance": float(data.get("min_price_distance", CONFIG["min_price_distance"])),
        "starting_balance":   float(data.get("starting_balance", CONFIG.get("starting_balance", 20000))),
        "apply_tier_overrides": bool(data.get("apply_tier_overrides", False)),
    }

    targets = symbols or [i["symbol"] for i in INSTRUMENTS]
    # Optional: trade only good structure symbols (Tier 1/2), skip choppy Tier 3.
    if bool(data.get("filter_choppy", False)):
        try:
            from tier_classifier import classify_symbol, get_all_tiers
            tier_map = get_all_tiers()
            filtered = []
            for sym in targets:
                t = tier_map.get(sym)
                if not t:
                    t = classify_symbol(sym, force=False)
                if int(t.get("tier", 3)) in (1, 2):
                    filtered.append(sym)
            targets = filtered
        except Exception as e:
            print(f"[BT] Tier filter warning: {e}")

    # CUSTOM MODE ONLY — no preset comparisons
    _bt_progress.update({
        "running": True,
        "config":  "Custom",
        "total":   len(targets),
        "done":    0,
        "symbols": {s: {"status": "pending"} for s in targets},
    })
    socketio.emit("bt_progress", _bt_progress)

    def on_progress(symbol, status, done, total, result):
        global _bt_progress
        _bt_progress["done"] = done
        _bt_progress["symbols"][symbol] = {
            "status":     status,
            "return_pct": result.get("return_pct"),
            "net_pnl":    result.get("net_pnl"),
            "trades":     result.get("trades"),
            "win_rate":   result.get("win_rate"),
            "error":      result.get("error"),
            "tier":       result.get("tier"),
            "gc_ratio":   result.get("gc_ratio"),
        }
        socketio.emit("bt_progress", {**_bt_progress})

    rows = run_backtest_all(preset=custom_preset, symbols=targets,
                            on_progress=on_progress)
    all_results = {"Custom": rows}

    _bt_progress["running"] = False
    socketio.emit("bt_progress", _bt_progress)

    # ── Persist last backtest results to DB so page refresh restores them ──
    from datetime import datetime as _dt
    try:
        bt_store = {}
        for row in rows:
            sym = row.get("symbol")
            if sym:
                bt_store[sym] = row.get("trades", [])
        db.set("bt_last_run", bt_store)
        db.set("bt_last_run_meta", {
            "preset":    custom_preset.get("name", "Custom"),
            "run_time":  _dt.now().strftime("%Y-%m-%d %H:%M"),
            "config":    custom_preset,
            "summary":   {r["symbol"]: {
                "return_pct": r.get("return_pct"),
                "net_pnl":    r.get("net_pnl"),
                "trades":     r.get("trades_count", len(r.get("trades", []))),
                "win_rate":   r.get("win_rate"),
                "tier":       r.get("tier"),
                "gc_ratio":   r.get("gc_ratio"),
                "balance":    r.get("balance"),
            } for r in rows if r.get("symbol")},
        })
        print(f"[BT] Results saved to DB — {len(bt_store)} symbols")
    except Exception as e:
        print(f"[BT] DB save error: {e}")

    return jsonify(all_results)


@app.route("/bt_progress")
def bt_progress_route():
    """Polling fallback for backtest progress."""
    return jsonify(_bt_progress)


@app.route("/classify_status")
def classify_status_route():
    """Poll classification progress — mirrors /fetch_status pattern."""
    return jsonify(_classify_status)


@app.route("/reclassify_all", methods=["POST"])
def reclassify_all_route():
    """
    Force re-classify all symbols (ignores cache).
    Called by Sunday scheduler or manually from UI.
    """
    global _classify_status
    data     = request.json or {}
    selected = data.get("symbols", None)

    syms = selected or [i["symbol"] for i in INSTRUMENTS]
    _classify_status.update({"done": False, "running": True, "results": {}, "error": None})
    socketio.emit("classify_status", _classify_status)

    # Apply custom tier config from UI before reclassifying
    tier_config = data.get("tier_config", {})
    if tier_config:
        import tier_classifier as tc
        # Update thresholds
        if "gc_threshold_t1" in tier_config:
            tc.TIER1_THRESHOLD = float(tier_config["gc_threshold_t1"])
        if "gc_threshold_t2" in tier_config:
            tc.TIER2_THRESHOLD = float(tier_config["gc_threshold_t2"])
        # Update tier configs
        for tier_num, key in [(1, "tier1"), (2, "tier2")]:
            if key in tier_config:
                tc.TIER_CONFIGS[tier_num].update(tier_config[key])
        # Update classification preset
        if "classify_preset" in tier_config:
            tc._CLASS_PRESET.update(tier_config["classify_preset"])
            cp = tier_config["classify_preset"]
            if "min_stop_pct" in cp:
                tc._CLASS_MIN_STOP_PCT = float(cp["min_stop_pct"])
            if "max_stop_pct" in cp:
                tc._CLASS_MAX_STOP_PCT = float(cp["max_stop_pct"])

    def _do_reclassify():
        global _classify_status
        from tier_classifier import classify_symbol
        for sym in syms:
            try:
                result = classify_symbol(sym, force=True)   # force = ignore cache
                _classify_status["results"][sym] = {
                    "tier":     result["tier"],
                    "gc_ratio": result["gc_ratio"],
                    "label":    result["label"],
                }
            except Exception as e:
                _classify_status["results"][sym] = {"tier": 3, "error": str(e)}
            socketio.emit("classify_status", _classify_status)
        _classify_status["running"] = False
        _classify_status["done"]    = True
        socketio.emit("classify_status", _classify_status)
        print(f"✅ Reclassification done for {len(syms)} symbols")

    threading.Thread(target=_do_reclassify, daemon=True).start()
    return jsonify({"status": "ok", "symbols": syms})


@app.route("/get_tiers")
def get_tiers_route():
    """Return full tier table — used by backtest UI to show tier badges."""
    try:
        from tier_classifier import get_all_tiers, get_tier_summary
        return jsonify({
            "tiers":   get_all_tiers(),
            "summary": get_tier_summary(),
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/fetch_symbol", methods=["POST"])
def fetch_symbol_route():
    """Fetch/refresh CSV data for a single symbol in background."""
    symbol = (request.json or {}).get("symbol", "").upper()
    inst = next((i for i in INSTRUMENTS if i["symbol"] == symbol), None)
    if not inst:
        return jsonify({"status": "error", "message": "Symbol not in INSTRUMENTS"})

    def _do_fetch():
        from fetch_upstox_history import fetch_symbol as _fetch
        ok = _fetch(symbol, inst["token"])
        _fetch_status["results"][symbol] = "ok" if ok else "failed"

    threading.Thread(target=_do_fetch, daemon=True).start()
    _fetch_status["results"][symbol] = "fetching"
    return jsonify({"status": "ok", "symbol": symbol})


@app.route("/refetch_all", methods=["POST"])
def refetch_all():
    """Check CSVs for selected symbols; fetch from Upstox only if missing."""
    global _fetch_status
    data     = request.json or {}
    selected = data.get("symbols", None)   # None = all INSTRUMENTS

    _fetch_status = {"done": False, "running": False, "results": {}, "error": None}
    threading.Thread(
        target=_fetch_all_data_background,
        kwargs={"symbols_to_fetch": selected},
        daemon=True
    ).start()
    return jsonify({"status": "ok"})


@app.route("/fetch_status")
def fetch_status():
    return jsonify(_fetch_status)


# ── Config & presets ──────────────────────────────────────────────────────────

@app.route("/update_config", methods=["POST"])
def update_config():
    data = request.json or {}
    CONFIG["risk_per_trade"]     = float(data.get("risk_per_trade",     CONFIG["risk_per_trade"]))
    CONFIG["rr_target"]          = float(data.get("rr_target",          CONFIG["rr_target"]))
    CONFIG["cooldown"]           = int(data.get("cooldown",              CONFIG["cooldown"]))
    CONFIG["min_price_distance"] = float(data.get("min_price_distance",  CONFIG["min_price_distance"]))
    return jsonify({"status": "ok"})


@app.route("/save_slot", methods=["POST"])
def api_save_slot():
    """Save current config to a named slot (Config A/B/C)."""
    data = request.json or {}
    slot = data.get("slot")
    cfg  = {
        "risk_per_trade":     float(data.get("risk_per_trade",     0.01)),
        "rr_target":          float(data.get("rr_target",          3)),
        "cooldown":           int(data.get("cooldown",              300)),
        "min_price_distance": float(data.get("min_price_distance",  5)),
    }
    try:
        saved = save_to_slot(slot, cfg)
        return jsonify({"status": "ok", "saved": saved})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/load_slot/<slot_name>")
def api_load_slot(slot_name: str):
    """Load a saved config slot into the UI."""
    try:
        cfg = load_slot(slot_name)
        return jsonify(cfg)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 404


@app.route("/get_slots")
def api_get_slots():
    return jsonify(get_all_slots())


@app.route("/lock_config", methods=["POST"])
def api_lock_config():
    """Lock any config (preset or slot) for live engine."""
    data = request.json or {}
    cfg  = {
        "name":               data.get("name", "Custom"),
        "risk_per_trade":     float(data.get("risk_per_trade",     0.01)),
        "rr_target":          float(data.get("rr_target",          3)),
        "cooldown":           int(data.get("cooldown",              300)),
        "min_price_distance": float(data.get("min_price_distance",  5)),
    }
    locked = lock_config(cfg)
    # Apply to live engine immediately if running
    if live_runner:
        live_runner.apply_config(locked)
    return jsonify({"status": "ok", "locked": locked})


@app.route("/locked_config")
def api_locked_config():
    return jsonify(load_locked_config())


# ── Upstox symbol search ──────────────────────────────────────────────────────

@app.route("/search_instruments")
def search_instruments():
    """
    Proxy to Upstox instrument search API.
    Supports NSE EQ and F&O (futures + options).
    Query param: q = search string (e.g. "TCS", "NIFTY")
    """
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])

    token = CONFIG.get("upstox_access_token", "")
    if not token:
        return jsonify({"error": "No access token configured"}), 500

    try:
        resp = requests.get(
            "https://api.upstox.com/v2/market-quote/search",
            params={"query": q, "instrument_type": "NSE_EQ,NSE_FO"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=5,
        )
        if not resp.ok:
            return jsonify([])
        items = resp.json().get("data", [])
        results = [
            {
                "symbol":  i.get("symbol") or i.get("trading_symbol", ""),
                "name":    i.get("name", ""),
                "token":   i.get("instrument_key", ""),
                "segment": i.get("segment", ""),
            }
            for i in items[:20]
        ]
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Market quotes — served from WS tick cache (NO REST polling) ──────────────

@app.route("/quotes")
def market_quotes():
    """
    Returns LTP + day change snapshot for all instruments.

    Priority:
      1. WS tick cache (heatmap_feed) — ONLY used during market hours
         (avoids serving stale cache from previous session after market close)
      2. Upstox market-quote REST API — accurate snapshot with real prev_close
         (always used outside market hours, and on first load before WS ticks)
      3. CSV last close — last resort when API unavailable (no token / offline)
    """
    from datetime import datetime, time as dtime, timezone, timedelta
    # IST = UTC+5:30 — no pytz dependency needed
    _ist_offset = timezone(timedelta(hours=5, minutes=30))
    _now = datetime.now(_ist_offset).time()
    _market_open  = dtime(9, 15)
    _market_close = dtime(15, 35)
    _is_market_hours = _market_open <= _now <= _market_close

    from heatmap_feed import get_all_ltps
    cached = get_all_ltps()
    # Only serve WS cache during live market hours — outside hours REST gives fresher data
    if cached and _is_market_hours:
        return jsonify(cached)

    # Try Upstox market-quote API for accurate LTP + prev_close
    token = CONFIG.get("upstox_access_token", "")
    if token:
        try:
            import requests as _req
            # Batch all tokens in one call (Upstox accepts comma-separated)
            keys = ",".join(inst["token"] for inst in INSTRUMENTS)
            resp = _req.get(
                "https://api.upstox.com/v2/market-quote/quotes",
                params={"instrument_key": keys},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=8,
            )
            if resp.ok:
                data   = resp.json().get("data", {})
                result = {}
                for inst in INSTRUMENTS:
                    sym = inst["symbol"]
                    # Upstox returns key as "NSE_EQ:INE..." (colon not pipe)
                    api_key = inst["token"].replace("|", ":")
                    q = data.get(api_key) or data.get(inst["token"], {})
                    if not q:
                        # fuzzy match
                        for k, v in data.items():
                            if sym in k:
                                q = v; break
                    if q:
                        ltp = q.get("last_price") or q.get("ltp") or 0
                        pc  = q.get("ohlc", {}).get("close") or q.get("prev_close") or ltp
                        chg = round(ltp - pc, 2) if pc else 0
                        pct = round((chg / pc) * 100, 2) if pc else 0
                        result[sym] = {
                            "symbol": sym, "ltp": ltp, "prev_close": pc,
                            "change": chg, "change_pct": pct,
                            "volume": q.get("volume", 0), "atp": None,
                            "ts": "snapshot",
                        }
                        # Also populate heatmap cache so next call is instant
                        import heatmap_feed as _hf
                        with _hf._lock:
                            _hf._ltp_cache[sym] = result[sym]
                    else:
                        result[sym] = {
                            "symbol": sym, "ltp": None, "prev_close": None,
                            "change": None, "change_pct": None,
                            "volume": 0, "atp": None, "ts": None,
                        }
                if any(v.get("ltp") for v in result.values()):
                    return jsonify(result)
        except Exception as e:
            print(f"[Quotes] API error: {e}")

    # Last resort: CSV — last 1min close vs previous day's last close
    print("[Quotes] Using CSV fallback (no token or REST failed)")
    import pandas as pd
    result = {}
    for inst in INSTRUMENTS:
        sym  = inst["symbol"]
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"{sym}_1YEAR_1MIN.csv")
        try:
            df = pd.read_csv(path, parse_dates=["datetime"])
            df = df.sort_values("datetime")
            ltp      = float(df["close"].iloc[-1])
            last_day = df["datetime"].iloc[-1].date()
            prev_day = df[df["datetime"].dt.date < last_day]
            pc       = float(prev_day["close"].iloc[-1]) if len(prev_day) else ltp
            chg      = round(ltp - pc, 2)
            pct      = round((chg / pc) * 100, 2) if pc else 0
            result[sym] = {
                "symbol": sym, "ltp": ltp, "prev_close": pc,
                "change": chg, "change_pct": pct, "volume": 0, "atp": None,
                "ts": "market closed",
            }
        except Exception:
            result[sym] = {
                "symbol": sym, "ltp": None, "prev_close": None,
                "change": None, "change_pct": None, "volume": 0, "atp": None,
                "ts": None,
            }
    return jsonify(result)



# ── Upstox OAuth 2.0 ──────────────────────────────────────────────────────────

@app.route("/auth/login")
def auth_login():
    """
    Redirect user to Upstox login page.
    After login, Upstox redirects to /auth/callback with ?code=AUTH_CODE.
    Visit this URL once manually, or click the link in Telegram at 3:31 AM.
    """
    from upstox_auth import get_login_url, _api_key
    if not _api_key():
        return (
            "<h2>UPSTOX_API_KEY not set</h2>"
            "<p>Add UPSTOX_API_KEY and UPSTOX_API_SECRET to your .env file</p>"
        ), 400
    login_url = get_login_url()
    return redirect(login_url)


@app.route("/auth/callback")
def auth_callback():
    """
    Upstox redirects here after user logs in.
    Exchanges auth code for access_token, saves to DB, restarts WS feed.
    """
    from upstox_auth import exchange_code_for_token, save_token

    auth_code = request.args.get("code")
    error     = request.args.get("error")

    if error:
        return (
            f"<h2>❌ Upstox OAuth Error</h2><p>{error}</p>"
            f"<a href='/auth/login'>Try again</a>"
        ), 400

    if not auth_code:
        return "<h2>❌ No auth code received</h2>", 400

    token = exchange_code_for_token(auth_code)
    if not token:
        return (
            "<h2>❌ Token exchange failed</h2>"
            "<p>Check server logs for details.</p>"
            "<p><b>Common causes:</b><ul>"
            "<li>UPSTOX_REDIRECT_URI in .env doesn't match Upstox dev console</li>"
            "<li>UPSTOX_API_SECRET is wrong</li>"
            "<li>Auth code already used (codes are single-use)</li>"
            "</ul></p>"
            "<a href='/auth/login'>Try again</a>"
        ), 500

    # Save token to DB + update live CONFIG
    save_token(token)

    # Restart heatmap WS feed with new token (if not already connected)
    try:
        import heatmap_feed as _hf
        if _hf._ws_client and not _hf._ws_client.is_connected():
            _hf._ws_client.start_in_thread()
        elif not _hf._ws_client:
            from heatmap_feed import start_heatmap_feed
            _hf._started = False   # allow re-start
            start_heatmap_feed()
    except Exception as e:
        print(f"[Auth] WS restart error: {e}")

    return """
    <html><body style="font-family:sans-serif;max-width:500px;margin:60px auto;text-align:center">
      <h2>✅ Authenticated Successfully</h2>
      <p>Token saved. Live feed will start within 10 seconds.</p>
      <p><a href="/">Go to Dashboard</a> | <a href="/heatmap">Heatmap</a></p>
    </body></html>
    """


@app.route("/auth/status")
def auth_status():
    """Quick check — is a valid token loaded? Shows token source and state."""
    from upstox_auth import load_token_from_db, _redirect_uri, _api_key
    token_in_config = CONFIG.get("upstox_access_token", "")
    token_in_db     = load_token_from_db()
    token_in_env    = os.getenv("UPSTOX_ACCESS_TOKEN", "")

    def _mask(t):
        return t[:12] + "…" if t else None

    return jsonify({
        "token_in_config":  bool(token_in_config),
        "token_in_db":      bool(token_in_db),
        "token_in_env":     bool(token_in_env),
        "config_preview":   _mask(token_in_config),
        "env_preview":      _mask(token_in_env),
        "ws_connected":     bool(__import__("heatmap_feed").is_connected()),
        "redirect_uri":     _redirect_uri(),
        "api_key_set":      bool(_api_key()),
        "login_url":        "/auth/login",
    })


@app.route("/heatmap")
def heatmap_page():
    return render_template("heatmap.html", instruments=INSTRUMENTS)


# ── Live dashboard ────────────────────────────────────────────────────────────

@app.route("/live")
def live_dashboard():
    return render_template("live.html", instruments=INSTRUMENTS)


@app.route("/live_view")
def live_view():
    """Read-only live view — shareable, no controls."""
    return render_template("live_view.html")


@app.route("/live_status")
def live_status_route():
    return jsonify(live_status.get_status())


@app.route("/add_symbol", methods=["POST"])
def add_symbol():
    """Add a symbol to the live engine instrument list."""
    data   = request.json or {}
    symbol = data.get("symbol", "").upper()
    token  = data.get("token", "")
    margin = float(data.get("margin_pct", 0.20))

    if not symbol or not token:
        return jsonify({"status": "error", "message": "symbol and token required"}), 400

    # Add to INSTRUMENTS if not already present
    existing = [i["symbol"] for i in INSTRUMENTS]
    if symbol not in existing:
        INSTRUMENTS.append({"symbol": symbol, "token": token, "margin_pct": margin})

    return jsonify({"status": "ok", "symbol": symbol})


@app.route("/remove_symbol", methods=["POST"])
def remove_symbol():
    """Remove a symbol from the live engine instrument list."""
    symbol = (request.json or {}).get("symbol", "").upper()
    global INSTRUMENTS
    INSTRUMENTS[:] = [i for i in INSTRUMENTS if i["symbol"] != symbol]
    return jsonify({"status": "ok"})


@app.route("/get_instruments")
def get_instruments():
    return jsonify(INSTRUMENTS)




# ── Trade log ──────────────────────────────────────────────────────────────────

@app.route("/trade_log")
def trade_log_page():
    """Standalone trade log viewer page."""
    return render_template("trade_log.html",
                           instruments=INSTRUMENTS)


@app.route("/api/trades/<symbol>")
def api_trades_symbol(symbol):
    """
    Return trades for one symbol.
    ?source=backtest  → last backtest run from bt_last_run DB key (REPLACE each run)
    ?source=live      → live trades from trades table, ?date=YYYY-MM-DD or 'all'
    """
    symbol = symbol.upper()
    source = request.args.get("source", "live")
    date   = request.args.get("date", "")

    if source == "backtest":
        # Read from bt_last_run (last backtest run only — replaced each run)
        bt_store = db.get("bt_last_run") or {}
        trades   = bt_store.get(symbol, [])
        meta     = db.get("bt_last_run_meta") or {}
        label    = f"Backtest · {meta.get('preset','?')} · {meta.get('run_time','?')}"
    else:
        # Live trades from trades table
        if date and date not in ("", "all"):
            trades = db.get_trades(symbol, date)
        else:
            trades = db.get_trades(symbol)
        label = f"Live · {date or 'all'}"

    wins  = sum(1 for t in trades if float(t.get("net_pnl", 0)) > 0)
    total = len(trades)
    net   = sum(float(t.get("net_pnl",   0)) for t in trades)
    gross = sum(float(t.get("gross_pnl", 0)) for t in trades)
    chg   = sum(float(t.get("charges",   0)) for t in trades)

    return jsonify({
        "symbol":  symbol,
        "source":  source,
        "label":   label,
        "trades":  trades,
        "summary": {
            "total":    total,
            "wins":     wins,
            "losses":   total - wins,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "gross":    round(gross, 2),
            "charges":  round(chg, 2),
            "net":      round(net, 2),
        },
    })


@app.route("/api/trades/dates/<symbol>")
def api_trade_dates(symbol):
    """Return distinct LIVE trade dates for a symbol (for date picker)."""
    symbol = symbol.upper()
    try:
        with db._conn() as cx:
            if db.USE_POSTGRES:
                with cx.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT date FROM trades WHERE symbol=%s ORDER BY date DESC",
                        (symbol,))
                    rows = cur.fetchall()
            else:
                cur = cx.execute(
                    "SELECT DISTINCT date FROM trades WHERE symbol=? ORDER BY date DESC",
                    (symbol,))
                rows = cur.fetchall()
        return jsonify([r[0] for r in rows])
    except Exception:
        return jsonify([])


@app.route("/api/bt_last_run")
def api_bt_last_run():
    """Return metadata + per-symbol summary of the last backtest run."""
    bt_store = db.get("bt_last_run") or {}
    meta     = db.get("bt_last_run_meta") or {}
    summary  = {}
    for sym, trades in bt_store.items():
        wins  = sum(1 for t in trades if float(t.get("net_pnl", 0)) > 0)
        total = len(trades)
        net   = sum(float(t.get("net_pnl",   0)) for t in trades)
        gross = sum(float(t.get("gross_pnl", 0)) for t in trades)
        chg   = sum(float(t.get("charges",   0)) for t in trades)
        summary[sym] = {
            "total":    total,
            "wins":     wins,
            "losses":   total - wins,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "gross":    round(gross, 2),
            "charges":  round(chg, 2),
            "net":      round(net, 2),
        }
    return jsonify({"meta": meta, "symbols": summary})


@app.route("/api/trades/summary")
def api_trades_summary():
    """Return per-symbol live trade summary for a given date."""
    date   = request.args.get("date", "")
    result = []
    for inst in INSTRUMENTS:
        sym    = inst["symbol"]
        trades = db.get_trades(sym, date) if date else db.get_trades(sym)
        if not trades:
            continue
        wins  = sum(1 for t in trades if float(t.get("net_pnl", 0)) > 0)
        total = len(trades)
        net   = sum(float(t.get("net_pnl", 0)) for t in trades)
        result.append({
            "symbol":   sym,
            "total":    total,
            "wins":     wins,
            "losses":   total - wins,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "net":      round(net, 2),
        })
    return jsonify(result)

# ── Debug endpoint ───────────────────────────────────────────────────────────
@app.route("/signals")
def signals_page():
    return render_template("signals.html")


@app.route("/api/signals")
def api_signals():
    return jsonify(_signal_log)


# ── ORDER BOOK EXPERIMENT — /ob ───────────────────────────────────────────────

@app.route("/ob")
def ob_page():
    """OB experiment page — TRENT + M&M order book entries."""
    locked = db.get("locked_config") or {}
    return render_template("ob.html", locked=locked)


@app.route("/ob/start", methods=["POST"])
def ob_start():
    global ob_runner
    with _ob_lock:
        if ob_runner and ob_runner.is_running():
            return jsonify({"status": "already_running"})
        if live_runner is None or not live_runner.is_connected():
            return jsonify({"status": "error",
                            "message": "Main engine must be running first"}), 400
        try:
            from live_engine.ob_runner import OBRunner
            ob_runner = OBRunner(
                main_runner   = live_runner,
                socketio_emit = socketio.emit,
            )
            result = ob_runner.start()
            if result.get("status") == "error":
                ob_runner = None
                return jsonify(result), 400
            return jsonify({"status": "ok", "message": "OB engine started"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/ob/stop", methods=["POST"])
def ob_stop():
    global ob_runner
    with _ob_lock:
        if ob_runner is None:
            return jsonify({"status": "not_running"})
        try:
            ob_runner.stop()
            ob_runner = None
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/ob/status")
def ob_status():
    if ob_runner is None or not ob_runner.is_running():
        return jsonify({"running": False, "instruments": [],
                        "total_realised": 0, "total_unreal": 0, "total_net": 0})
    payload = ob_runner._get_payload()
    return jsonify(_sanitize_for_json(payload))


@app.route("/close_position", methods=["POST"])
def api_close_position():
    """Force-close open position on a symbol (for testing)."""
    if not live_runner:
        return jsonify({"error": "Engine not running"}), 400
    data   = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "TRENT")
    for runner in live_runner.runners.values():
        if runner.symbol == symbol:
            if not runner.broker.position:
                return jsonify({"error": f"{symbol} has no open position"}), 400
            import pandas as pd
            ltp = runner.last_price or (runner.broker.position.get("entry_price", 0))
            result = runner.broker.force_close(ltp, pd.Timestamp.now(tz="Asia/Kolkata"))
            return jsonify({"status": "ok", "symbol": symbol, "closed": result})
    return jsonify({"error": f"{symbol} not found"}), 404


@app.route("/debug_status")
def api_debug_status():
    """Debug endpoint — shows raw get_status() for all runners."""
    if not live_runner:
        return jsonify({"error": "Engine not running"}), 400
    out = {}
    for token, runner in live_runner.runners.items():
        b = runner.broker
        out[runner.symbol] = {
            "last_price":   runner.last_price,
            "has_position": bool(b.position),
            "position":     b.position,
            "balance":      round(b.balance, 2),
            "status_ltp":   runner.get_status().get("ltp"),
            "status_pos":   runner.get_status().get("position"),
        }
    return jsonify(out)


@app.route("/test_trade", methods=["POST"])
def api_test_trade():
    """
    Inject a fake paper trade to verify the full pipeline:
      signal → broker.open() → position shows on UI → tick updates P&L
      → EOD closes at 15:25 OR target/SL hit

    POST /test_trade
    Body (JSON, all optional):
      { "symbol": "TRENT", "side": "BUY", "rr": 3 }

    Defaults: first T1 symbol, BUY, rr=3
    Uses current LTP as entry, 0.5% stop, rr×stop as target.
    """
    if not live_runner:
        return jsonify({"error": "Engine not running"}), 400

    data   = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "TRENT")
    side   = data.get("side", "BUY").upper()
    rr     = float(data.get("rr", 3))

    # Find the runner for this symbol
    runner = None
    for r in live_runner.runners.values():
        if r.symbol == symbol:
            runner = r
            break

    if not runner:
        syms = [r.symbol for r in live_runner.runners.values()]
        return jsonify({"error": f"Symbol {symbol} not found", "available": syms}), 404

    ltp = runner.last_price
    if not ltp or ltp <= 0:
        # Fallback: read from heatmap cache (always populated from WS)
        try:
            import heatmap_feed as _hf
            cached = _hf.get_ltp(symbol) or {}
            ltp = cached.get("ltp") or 0
        except Exception:
            ltp = 0
    if not ltp or ltp <= 0:
        return jsonify({"error": f"No LTP available for {symbol} yet — try again in a few seconds"}), 400

    if runner.broker.position:
        return jsonify({"error": f"{symbol} already has an open position"}), 400

    # Build a realistic signal: 0.5% stop, rr×stop target
    stop_dist  = round(ltp * 0.005, 2)
    if side == "BUY":
        stop   = round(ltp - stop_dist, 2)
        target = round(ltp + stop_dist * rr, 2)
    else:
        stop   = round(ltp + stop_dist, 2)
        target = round(ltp - stop_dist * rr, 2)

    import pandas as pd
    now = pd.Timestamp.now(tz="Asia/Kolkata")

    runner.broker.open(
        side=side, price=ltp,
        stop=stop, target=target, time=now,
    )
    runner.engine.last_entry_index = runner.engine.index
    runner.engine.last_entry_price = ltp

    pos = runner.broker.position
    print(f"[TEST TRADE] {symbol} {side} @ ₹{ltp} | SL ₹{stop} | TGT ₹{target} | Qty {pos['qty']}")

    return jsonify({
        "status":  "ok",
        "symbol":  symbol,
        "side":    side,
        "entry":   ltp,
        "stop":    stop,
        "target":  target,
        "qty":     pos["qty"],
        "message": f"Fake {side} trade opened on {symbol}. Watch the card update in real-time!"
    })


@app.route("/eod_update", methods=["POST"])
def api_eod_update():
    """
    Manually trigger EOD CSV update.
    POST /eod_update
    Useful if auto-scheduler missed a day or you want to force-update.
    """
    import threading
    def _run():
        from eod_updater import run_eod_update
        run_eod_update()
    threading.Thread(target=_run, daemon=True, name="ManualEOD").start()
    return jsonify({"status": "started", "message": "EOD update running in background — check server logs"})


@app.route("/gap_fill", methods=["POST"])
def api_gap_fill():
    """
    Manually trigger startup gap-fill — fetches any missing historical days.
    POST /gap_fill
    Use this when CSV data is stale and you want to catch up without restarting.
    """
    import threading, subprocess, sys as _sys
    def _run():
        try:
            print("[GapFill] Manual gap-fill triggered...")
            result = subprocess.run(
                [_sys.executable, "fetch_upstox_history.py"],
                capture_output=True, text=True,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                timeout=180
            )
            lines = (result.stdout or "").strip().split("\n")
            updated = [l for l in lines if "✅" in l or "appending" in l]
            print(f"[GapFill] Manual done — {len(updated)} symbols updated")
        except Exception as e:
            print(f"[GapFill] Manual error: {e}")
    threading.Thread(target=_run, daemon=True, name="ManualGapFill").start()
    return jsonify({"status": "started", "message": "Gap-fill running in background — CSVs will update with missing days"})


@app.route("/test_telegram", methods=["POST"])
def api_test_telegram():
    """
    Send test Telegram messages for every message type.
    POST /test_telegram
    Use this to verify bot token + chat_id before going live.
    """
    import requests as _req
    from datetime import datetime

    bot  = CONFIG.get("telegram_bot_token", "")
    chat = CONFIG.get("telegram_chat_id", "")

    if not bot or not chat:
        return jsonify({"status": "error", "message": "No telegram_bot_token or telegram_chat_id in config/DB"}), 400

    results = {}
    now = datetime.now().strftime("%H:%M:%S")

    def _send(name, text):
        try:
            r = _req.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
                timeout=8,
            )
            results[name] = "✅ sent" if r.ok else f"❌ {r.status_code}: {r.text[:80]}"
        except Exception as e:
            results[name] = f"❌ {e}"

    # 1. Entry alert
    _send("entry", (
        f"🟢 <b>ENTRY — TRENT</b>  [TEST]\n\n"
        f"Side:    <b>BUY</b>\n"
        f"Entry:   ₹3,720.00\n"
        f"Stop:    ₹3,703.50  (-16.50)\n"
        f"Target:  ₹3,802.50  (+82.50)\n"
        f"Qty:     540\n"
        f"RR:      1:5.0\n"
        f"Time:    {now}"
    ))

    # 2. Exit — profit
    _send("exit_profit", (
        f"✅ <b>EXIT — TRENT</b>  [🎯 Target]\n\n"
        f"Side:    BUY\n"
        f"Entry:   ₹3,720.00  (09:32)\n"
        f"Exit:    ₹3,802.50  (11:15)\n"
        f"Qty:     540\n\n"
        f"Gross:   ₹+44,550.00\n"
        f"Charges: ₹1,480.00\n"
        f"<b>Net:     ₹+43,070.00</b>\n\n"
        f"Balance: ₹2,43,070.00"
    ))

    # 3. Exit — loss
    _send("exit_loss", (
        f"❌ <b>EXIT — CUMMINSIND</b>  [🛑 Stop Loss]\n\n"
        f"Side:    SELL\n"
        f"Entry:   ₹4,810.00  (10:05)\n"
        f"Exit:    ₹4,826.50  (10:07)\n"
        f"Qty:     415\n\n"
        f"Gross:   ₹-6,847.50\n"
        f"Charges: ₹1,356.00\n"
        f"<b>Net:     ₹-8,203.50</b>\n\n"
        f"Balance: ₹1,91,796.50"
    ))

    # 4. EOD CSV update
    _send("eod_update", (
        f"✅ <b>EOD CSV Update — {datetime.now().date()}</b>  [TEST]\n\n"
        f"📥 Updated: <b>28 symbols</b> (+10,500 rows)\n\n"
        f"📅 CSV ends at:\n"
        f"  TRENT: {datetime.now().date()} 15:29\n"
        f"  CUMMINSIND: {datetime.now().date()} 15:29\n"
        f"  OFSS: {datetime.now().date()} 15:29\n\n"
        f"⏰ Next update tomorrow at 15:31"
    ))

    # 5. Kill switch
    _send("kill_switch", (
        f"🚨 <b>KILL SWITCH — TRENT</b>  [TEST]\n\n"
        f"Peak Equity:  ₹2,20,000\n"
        f"Threshold:    ₹1,98,000  (90% of peak)\n"
        f"Current:      ₹1,95,000\n\n"
        f"New entries halted for the session."
    ))

    all_ok = all("✅" in v for v in results.values())
    return jsonify({
        "status": "ok" if all_ok else "partial",
        "results": results,
        "message": "Check your Telegram — all 5 message types sent" if all_ok else "Some messages failed — check results"
    })


@app.route("/debug")
def debug_status():
    """Quick health check — shows token state, CSV counts, cache state."""
    import os
    token = CONFIG.get("upstox_access_token", "")
    # Count CSVs in data/
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    csv_files = [f for f in os.listdir(data_dir) if f.endswith(".csv")] if os.path.exists(data_dir) else []
    import heatmap_feed as _hf
    cache = _hf.get_all_ltps()
    return jsonify({
        "token_present":    bool(token),
        "token_preview":    token[:15] + "..." if token else None,
        "ws_connected":     _hf.is_connected(),
        "heatmap_cache_size": len(cache),
        "heatmap_sample":   {k: v.get("ltp") for k, v in list(cache.items())[:5]},
        "csv_count":        len(csv_files),
        "csv_files_sample": csv_files[:5],
        "data_dir":         data_dir,
        "live_runner_active": live_runner is not None,
    })


# ── SocketIO ──────────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect(auth=None):
    if live_runner:
        try:
            instruments = _sanitize_for_json(live_runner.get_all_status())  # FIX: sanitize Timestamps
            connected   = live_runner.is_connected()
            portfolio   = live_runner.get_portfolio_equity()
        except Exception as _e:
            print(f"[on_connect] get_all_status error: {_e}")
            instruments, connected, portfolio = [], False, {}
        # Restore button + bias state on page refresh
        emit("engine_started", {"status": "ok", "instruments": instruments})
        emit("live_update", {
            "connected":        connected,
            "portfolio_equity": portfolio,
            "instruments":      instruments,
        })
        # Replay cached signals so browser sees signals fired before connect
        if _signal_log:
            emit("signal_history", _signal_log)
        # Replay cached signals so browser sees signals fired before connect
        if _signal_log:
            emit("signal_history", _signal_log)


@socketio.on("stop_engine")
def on_stop_engine():
    global live_runner
    with _runner_lock:
        if live_runner is None:
            emit("engine_stopped", {"status": "not_running"})
            return
        try:
            live_runner.stop()
        except Exception as e:
            print(f"[App] stop error: {e}")
        live_runner = None
    # Restart standalone heatmap WS so heatmap stays live after stop
    try:
        import heatmap_feed as _hf
        _hf.start_standalone_ws()
    except Exception as e:
        print(f"[App] heatmap restart error: {e}")
    emit("engine_stopped", {"status": "ok"})
    print("[App] Engine stopped by user")


@socketio.on("start_all")
def on_start_all():
    """Master Start button — starts live engine for all configured symbols."""
    import os as _os
    from dotenv import load_dotenv as _lde
    _lde(override=True)
    from config import CONFIG as _cfg

    # Try env var first, then fall back to DB token
    token = _os.getenv("UPSTOX_ACCESS_TOKEN", "") or CONFIG.get("upstox_access_token", "")
    if not token:
        # Last resort: try loading from DB directly
        try:
            from upstox_auth import load_token_from_db
            token = load_token_from_db() or ""
        except Exception:
            pass

    if not token:
        emit("engine_error", {"msg": "\u274c No Upstox token found. Go to /auth/login to authenticate first."})
        return

    _cfg["upstox_access_token"] = token
    CONFIG["upstox_access_token"] = token
    _start_live_runner()
    emit("engine_starting", {"status": "ok", "msg": "Replaying history + connecting WS..."})


@app.route("/cron/start", methods=["POST", "GET"])
def cron_start():
    """
    Called by Railway cron at 09:00 IST (03:30 UTC) Mon–Fri.
    Starts the live engine automatically — no manual click needed.
    """
    from datetime import datetime
    import os as _os
    from dotenv import load_dotenv as _lde
    _lde(override=True)

    today   = datetime.now().date()
    weekday = datetime.now().weekday()

    if weekday >= 5:
        print(f"[Cron] /cron/start called on weekend ({today}) — skipping")
        return jsonify({"status": "skipped", "reason": "weekend"})

    token = _os.getenv("UPSTOX_ACCESS_TOKEN", "") or CONFIG.get("upstox_access_token", "")
    if not token:
        print("[Cron] /cron/start — no access token, cannot start")
        return jsonify({"status": "error", "reason": "no_token"}), 400

    CONFIG["upstox_access_token"] = token

    global live_runner
    if live_runner is not None:
        print("[Cron] /cron/start — engine already running")
        return jsonify({"status": "already_running"})

    print(f"[Cron] /cron/start — auto-starting engine for {today}")
    _start_live_runner()

    # Telegram notification
    try:
        bot  = CONFIG.get("telegram_bot_token", "")
        chat = CONFIG.get("telegram_chat_id", "")
        if bot and chat:
            import requests as _req
            _req.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat,
                      "text": f"🚀 <b>StructureEngine Auto-Started</b>\n\n"
                              f"📅 {today}  ⏰ 09:00 IST\n"
                              f"Replaying history → WS connecting...\n"
                              f"Engine will run autonomously till 15:31.",
                      "parse_mode": "HTML"},
                timeout=6,
            )
    except Exception:
        pass

    return jsonify({"status": "started", "date": str(today)})


@app.route("/cron/stop", methods=["POST", "GET"])
def cron_stop():
    """
    Called by Railway cron at 15:31 IST (10:01 UTC) Mon–Fri.
    Stops the live engine — all open positions force-closed by engine at 15:25.
    """
    from datetime import datetime
    today = datetime.now().date()

    global live_runner
    if live_runner is None:
        print(f"[Cron] /cron/stop — engine not running ({today})")
        return jsonify({"status": "not_running"})

    print(f"[Cron] /cron/stop — auto-stopping engine for {today}")
    try:
        live_runner.stop()
    except Exception as e:
        print(f"[Cron] stop error: {e}")
    live_runner = None

    socketio.emit("live_update", {
        "connected": False, "instruments": [], "portfolio_equity": {}
    })

    # Restart standalone heatmap WS
    try:
        import heatmap_feed as _hf
        _hf.start_standalone_ws()
    except Exception:
        pass

    # Telegram notification
    try:
        bot  = CONFIG.get("telegram_bot_token", "")
        chat = CONFIG.get("telegram_chat_id", "")
        if bot and chat:
            import requests as _req
            _req.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat,
                      "text": f"🔴 <b>StructureEngine Auto-Stopped</b>\n\n"
                              f"📅 {today}  ⏰ 15:31 IST\n"
                              f"Market closed. Engine restarts tomorrow at 09:00.",
                      "parse_mode": "HTML"},
                timeout=6,
            )
    except Exception:
        pass

    print(f"[Cron] Engine stopped cleanly ✅")
    return jsonify({"status": "stopped", "date": str(today)})


# ── Entry point ───────────────────────────────────────────────────────────────

# Always run startup — works for both gunicorn (module import) and direct run
_on_startup()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
