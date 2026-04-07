# backtest_runner.py
#
# Runs backtest for ONE symbol + ONE preset config.
# run_backtest_all() runs symbols sequentially — one at a time, easy on CPU.
#
# Returns per-symbol summary only (no equity curve) for the results table:
#   symbol, trades, win_rate, gross_pnl, charges, net_pnl, return_pct, balance
# Results sorted by return_pct descending.

import os
from config import CONFIG, INSTRUMENTS
from broker.paper_broker import PaperBroker
from engine.trade_engine import TradeEngine
from engine.htf_structure import HTFStructure
from engine.ltf_entry import LTFEntry
from data.loader import load_csv
from data.resampler import resample


# ── Stop-distance filter (applied inside LTFEntry) ────────────────────────────
# Skips signals where stop is too tight (noise) or too wide (bad structure).
# min_stop_pct / max_stop_pct come from tier config or preset override.

class _FilteredLTFEntry(LTFEntry):
    """LTFEntry with stop-distance % filter baked in."""
    def __init__(self, rr_target=None, min_stop_pct=0.003, max_stop_pct=0.012):
        super().__init__(rr_target)
        self._min_stop = min_stop_pct
        self._max_stop = max_stop_pct

    def update(self, candle, bias):
        sig = super().update(candle, bias)
        if sig is None:
            return None
        stop_pct = abs(sig["entry"] - sig["stop"]) / sig["entry"]
        if stop_pct < self._min_stop or stop_pct > self._max_stop:
            return None
        return sig


def run_backtest(symbol: str, preset: dict) -> dict:
    """
    Run backtest for one symbol with one preset config.
    Thread-safe: uses only local variables, never touches global CONFIG.

    Returns summary dict. Returns error dict if CSV missing.
    """
    # Accept either 1YEAR or 6MONTH CSV — prefer 1YEAR for more data
    data_dir = os.environ.get("CSV_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "csvdata"))
    csv_path = None
    for suffix in ["1YEAR_1MIN", "6MONTH_1MIN"]:
        candidate = os.path.join(data_dir, f"{symbol}_{suffix}.csv")
        if os.path.exists(candidate):
            csv_path = candidate
            break
    if csv_path is None:
        return {
            "symbol":     symbol,
            "error":      "CSV not found — run fetch_upstox_history.py first",
            "return_pct": -9999,
        }

    # ── Resolve config: tier config → preset override → global CONFIG ────────
    # If preset explicitly passes risk/cooldown, those win (backtest UI).
    # Otherwise use the symbol's tier config (auto-classified).
    tier_cfg = {}
    tier_num = None
    gc_ratio = None
    try:
        from tier_classifier import _mem_cache, TIER_CONFIGS
        import copy
        if symbol in _mem_cache:
            cached   = _mem_cache[symbol]
            tier_cfg = copy.copy(TIER_CONFIGS[cached["tier"]])
            tier_num = cached["tier"]
            gc_ratio = cached["gc_ratio"]
    except Exception:
        pass

    def _resolve(key, default):
        # tier config wins for risk + cooldown + stop filter
        # UI preset wins for rr_target + min_price_distance
        tier_priority_keys = {"risk_per_trade", "cooldown", "min_stop_pct", "max_stop_pct"}
        if key in tier_priority_keys:
            if key in tier_cfg:
                return tier_cfg[key]
            if key in preset and preset[key] is not None:
                return preset[key]
        else:
            if key in preset and preset[key] is not None:
                return preset[key]
            if key in tier_cfg:
                return tier_cfg[key]
        return default

    risk         = float(_resolve("risk_per_trade",     CONFIG["risk_per_trade"]))
    rr           = float(_resolve("rr_target",          CONFIG["rr_target"]))
    cooldown     = int(_resolve("cooldown",              CONFIG["cooldown"]))
    min_dist     = float(_resolve("min_price_distance",  CONFIG["min_price_distance"]))
    min_stop_pct = float(_resolve("min_stop_pct",        0.003))
    max_stop_pct = float(_resolve("max_stop_pct",        0.012))

    try:
        df_1m  = load_csv(csv_path)
        df_5m  = resample(df_1m, 5)
        df_15m = resample(df_1m, 15)
    except Exception as e:
        return {"symbol": symbol, "error": str(e), "return_pct": -9999}

    broker = PaperBroker(
        balance=CONFIG["starting_balance"],
        risk_per_trade=risk,
        symbol=symbol,
    )
    engine = TradeEngine(
        broker,
        cooldown_candles=cooldown,
        min_price_distance=min_dist,
        symbol=symbol,
        backtest_rr=rr,
    )
    engine.attach_engines(
        HTFStructure(),
        _FilteredLTFEntry(
            min_stop_pct=min_stop_pct,
            max_stop_pct=max_stop_pct,
        ),
    )

    # Merged HTF/LTF pass — bias updates in real-time alongside LTF candles
    htf_list = list(df_15m.iterrows())
    htf_i    = 0
    for ltf_ts, ltf_candle in df_5m.iterrows():
        while htf_i < len(htf_list) and htf_list[htf_i][0] <= ltf_ts:
            engine.on_htf_candle(htf_list[htf_i][1])
            htf_i += 1
        engine.on_ltf_candle(ltf_candle)

    total = len(broker.trade_log)
    wins  = sum(1 for t in broker.trade_log if t["net_pnl"] > 0)
    start = CONFIG["starting_balance"]
    net   = broker.total_net_pnl

    # ── Persist final bias to DB so live engine inherits it ──────────────────
    # This is the KEY bridge: backtest ends knowing HTF bias, live engine
    # must start from the same bias — not from scratch.
    try:
        import db as _db
        _db.init_db()
        # Only save bias — NOT broker balance/PNL (those are backtest paper numbers,
        # live engine must start with fresh CONFIG["starting_balance"])
        _db.set_engine_state(symbol, {
            "current_bias":          engine.current_bias,
            "last_entry_index":      -1000,   # reset cooldown for live
            "last_entry_price":      None,
            "equity_peak":           None,    # live broker will set its own peak
            "kill_switch_triggered": False,
            "open_position":         None,
            "last_processed_time":   str(engine.last_processed_time) if engine.last_processed_time else None,
            "broker":                None,    # live broker starts fresh
        })
    except Exception as _e:
        print(f"⚠ [{symbol}] Could not save backtest state to DB: {_e}")

    return {
        "symbol":     symbol,
        "final_bias": engine.current_bias,
        "balance":    round(broker.balance, 2),
        "return_pct": round(net / start * 100, 2) if start else 0,
        "gross_pnl":  round(broker.total_gross_pnl, 2),
        "charges":    round(broker.total_charges, 2),
        "net_pnl":    round(net, 2),
        "trades":     total,
        "wins":       wins,
        "losses":     total - wins,
        "win_rate":   round(wins / total * 100, 1) if total else 0,
        "error":      None,
        "tier":       tier_num,
        "gc_ratio":   gc_ratio,
        "applied_config": {
            "risk":     round(risk, 3),
            "rr":       rr,
            "cooldown": cooldown,
            "stop_min": round(min_stop_pct * 100, 1),
            "stop_max": round(max_stop_pct * 100, 1),
        },
        "trade_log":  broker.trade_log,
    }


def run_backtest_all(preset: dict, symbols: list[str] = None,
                     on_progress=None) -> list[dict]:
    """
    Run backtest for all (or specified) symbols sequentially — one at a time.
    Returns list of summary dicts sorted by return_pct descending.

    on_progress(symbol, status, done, total, result) called after each symbol.
    """
    targets = [i["symbol"] for i in INSTRUMENTS] if symbols is None else symbols
    results = []
    total   = len(targets)
    done    = 0

    for sym in targets:
        try:
            r = run_backtest(sym, preset)
            results.append(r)
            done += 1
            if on_progress:
                on_progress(sym, "ok" if not r.get("error") else "error",
                            done, total, r)
        except Exception as e:
            r = {"symbol": sym, "error": str(e), "return_pct": -9999}
            results.append(r)
            done += 1
            if on_progress:
                on_progress(sym, "error", done, total, r)

    results.sort(key=lambda x: x.get("return_pct", -9999), reverse=True)

    try:
        import db as _db
        bt_store = {
            r["symbol"]: r.get("trade_log", [])
            for r in results if not r.get("error")
        }
        _db.set("bt_last_run", bt_store)
        _db.set("bt_last_run_meta", {
            "preset":   preset.get("name", "custom"),
            "symbols":  [r["symbol"] for r in results if not r.get("error")],
            "run_time": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as _e:
        print(f"[DB] bt_last_run save warning: {_e}")

    return results