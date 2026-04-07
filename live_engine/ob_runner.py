# live_engine/ob_runner.py
#
# Standalone Order Book engine for TRENT and M&M experiment.
# Runs in same process as main engine but completely independent:
#   - Own PaperBroker per symbol (₹50k capital each)
#   - Own candle builders (5m + 15m)
#   - Reuses HTF bias from main engine (shared state, no recompute)
#   - All entries via limit orders at best bid/ask (order book)
#   - All exits via same SL/target/force-exit logic as main engine
#   - Shares the existing Upstox WS connection (no second auth)
#
# Started manually from /ob page — does NOT auto-start with main engine.

import threading
import time
import pandas as pd
from datetime import datetime, timezone, timedelta

from config import CONFIG
from broker.paper_broker import PaperBroker
from engine.trade_engine import TradeEngine
from engine.htf_structure import HTFStructure
from engine.ltf_entry import LTFEntry
from live_engine.live_candle_builder import LiveCandleBuilder
from live_engine.order_book import OrderBook, OrderBookRegistry
from live_engine.liquidity_filter import LiquidityFilter
from live_engine.limit_order_manager import LimitOrderManager, LimitOrder

_IST = timezone(timedelta(hours=5, minutes=30))

OB_SYMBOLS = ["TRENT", "M&M"]
OB_BALANCE = 50_000   # ₹50k per symbol

# Token map — same as config.py
_TOKEN_MAP = {i["symbol"]: i["token"] for i in CONFIG.get("instruments", []) or [
    {"symbol": "TRENT",  "token": "NSE_EQ|INE849A01020"},
    {"symbol": "M&M",    "token": "NSE_EQ|INE101A01026"},
]}


class OBInstrumentRunner:
    """
    Single-symbol OB runner. Mirrors InstrumentRunner but
    always uses order book execution path.
    """

    def __init__(self, symbol: str, token: str, risk_per_trade: float):
        self.symbol = symbol
        self.token  = token

        # Independent broker — ₹50k capital, separate from main engine
        # Prefix symbol with OB_ so DB state doesn't collide with main engine
        self.broker = PaperBroker(
            balance        = OB_BALANCE,
            risk_per_trade = risk_per_trade,
            symbol         = f"OB_{symbol}",
            live_mode      = True,
        )

        # Trade engine — same strategy logic as main engine
        rr = CONFIG.get("rr_target", 3)
        cd = CONFIG.get("cooldown", 20)
        md = CONFIG.get("min_price_distance", 5)
        self.engine = TradeEngine(
            self.broker,
            cooldown_candles    = cd,
            min_price_distance  = md,
            symbol              = symbol,
        )
        self.engine.attach(HTFStructure(), LTFEntry(rr_target=rr))

        # Candle builders
        self.builder_5m  = LiveCandleBuilder(5)
        self.builder_15m = LiveCandleBuilder(15)

        # State
        self.last_price  = None
        self.last_time   = None
        self.prev_close  = None

        # Order book components
        self.order_book  = OrderBook(symbol)
        self._liq_filter = LiquidityFilter(
            spread_max_pct   = CONFIG.get("ob_spread_max_pct",   0.0005),
            imbalance_levels = CONFIG.get("ob_imbalance_levels", 5),
            imbalance_min    = CONFIG.get("ob_imbalance_min",    0.55),
            imbalance_max    = CONFIG.get("ob_imbalance_max",    0.45),
        )
        self._lom = LimitOrderManager(
            fill_timeout_secs  = CONFIG.get("ob_fill_timeout_secs",  5),
            poll_interval_secs = CONFIG.get("ob_poll_interval_secs", 1.0),
        )
        self._pending_order = False   # True while limit order is being monitored

        # SocketIO emitter (set by OBRunner after init)
        self._emit = None

    def set_bias_from_main(self, bias: str):
        """Inject HTF bias from main engine runner — avoids recomputing."""
        if bias and bias != self.engine.current_bias:
            self.engine.current_bias = bias
            print(f"[OB:{self.symbol}] Bias synced from main engine: {bias}")

    def on_tick(self, ltp: float, prev_close=None, timestamp=None,
                bids=None, asks=None, **kwargs):
        """Called from OBRunner tick handler for every WS tick."""
        self.last_price = ltp
        self.last_time  = timestamp
        if prev_close and not self.prev_close:
            self.prev_close = prev_close

        # Update order book depth
        if bids or asks:
            self.order_book.update(bids or [], asks or [], time.time())

        # Build candles
        candle_15m = self.builder_15m.update(ltp, timestamp)
        candle_5m  = self.builder_5m.update(ltp, timestamp)

        # HTF candle → update structure
        if candle_15m is not None:
            self.engine.on_htf_candle(candle_15m)

        # LTF candle → check for signal → OB execution
        if candle_5m is not None:
            self._on_ltf_candle(candle_5m)

    def _on_ltf_candle(self, candle):
        """Intercept signal and route through order book execution."""
        if self._pending_order:
            return  # already waiting for a fill

        # Check SL/target/force-exit for existing position first
        if self.broker.position:
            self._check_exit(candle)
            return

        # Intercept signal from engine
        original_open = self.broker.open
        intercepted   = {}

        def _intercept(side, price, stop, target, time=None, **kwargs):
            intercepted['signal'] = {
                'side': side, 'entry': price,
                'stop': stop, 'target': target, 'time': time
            }

        self.broker.open = _intercept
        try:
            self.engine.on_ltf_candle(candle)
        finally:
            self.broker.open = original_open

        if not intercepted:
            return

        signal = intercepted['signal']
        self._execute_ob_signal(signal, candle)

    def _check_exit(self, candle):
        """Pass candle to engine for SL/target/force-exit checks on open position."""
        # Engine checks SL/target internally via broker.close_if_hit()
        # Just call on_ltf_candle — broker already has position, engine won't open new
        self.engine.on_ltf_candle(candle)

    def _execute_ob_signal(self, signal, candle):
        """Run liquidity filter then place limit order."""
        snap   = self.order_book.snapshot()
        result = self._liq_filter.check(signal['side'], snap)

        if not result.passed:
            # Consume cooldown — don't re-enter on same candle
            self.engine.last_entry_index = self.engine.index
            print(f"[OB:{self.symbol}] 🚫 Rejected: {result.reason}")
            self._emit_event("ob_signal_rejected", {
                "symbol": self.symbol,
                "side":   signal['side'],
                "reason": result.reason,
                "spread": round((result.spread_pct or 0) * 100, 4),
                "time":   datetime.now(_IST).strftime("%H:%M:%S"),
            })
            return

        # Build limit order
        entry_price = result.entry_price
        rr          = self.engine.ltf._rr_target()
        risk        = abs(entry_price - signal['stop'])
        if risk <= 0:
            risk = entry_price * 0.003
        qty = max(1, int((self.broker.risk_per_trade * self.broker.balance) / risk))

        order = LimitOrder(
            symbol      = self.symbol,
            side        = signal['side'],
            limit_price = entry_price,
            stop        = signal['stop'],
            rr          = rr,
            qty         = qty,
            signal_time = str(signal.get('time', '')),
        )

        self._pending_order = True
        self.engine.last_entry_index = self.engine.index  # consume cooldown

        print(f"[OB:{self.symbol}] 📋 Limit {signal['side']} @ ₹{entry_price:.2f} "
              f"spread={result.spread_pct*100:.3f}% imbal={result.imbalance:.3f}")

        self._emit_event("ob_signal", {
            "symbol":    self.symbol,
            "side":      signal['side'],
            "entry":     entry_price,
            "stop":      signal['stop'],
            "target":    signal['target'],
            "qty":       qty,
            "spread":    round((result.spread_pct or 0) * 100, 4),
            "imbalance": round(result.imbalance or 0, 3),
            "bias":      self.engine.current_bias,
            "rr":        round(rr, 1),
            "time":      datetime.now(_IST).strftime("%H:%M:%S"),
            "status":    "pending",
        })

        def on_fill(fill_result):
            self._pending_order = False
            if self.broker.position:
                return
            self.broker.open(
                side   = signal['side'],
                price  = fill_result.fill_price,
                stop   = fill_result.stop,
                target = fill_result.target,
                time   = candle.name,
            )
            self.engine.last_entry_price = fill_result.fill_price
            print(f"[OB:{self.symbol}] ✅ Filled @ ₹{fill_result.fill_price:.2f}")
            self._emit_event("ob_fill", {
                "symbol":     self.symbol,
                "side":       signal['side'],
                "fill_price": fill_result.fill_price,
                "stop":       fill_result.stop,
                "target":     fill_result.target,
                "qty":        qty,
                "time":       datetime.now(_IST).strftime("%H:%M:%S"),
            })

        def on_miss(reason):
            self._pending_order = False
            print(f"[OB:{self.symbol}] ⏰ Miss: {reason}")
            self._emit_event("ob_miss", {
                "symbol": self.symbol,
                "reason": reason,
                "time":   datetime.now(_IST).strftime("%H:%M:%S"),
            })

        self._lom.execute(order, self.order_book, on_fill, on_miss)

    def _emit_event(self, event, data):
        if self._emit:
            try:
                self._emit(event, data)
            except Exception as e:
                print(f"[OB:{self.symbol}] emit error: {e}")

    def get_status(self) -> dict:
        b   = self.broker
        ltp = self.last_price or 0
        pos = b.position
        pc  = self.prev_close or ltp
        chg = round(ltp - pc, 2) if pc else 0
        pct = round((chg / pc) * 100, 2) if pc else 0

        snap = self.order_book.snapshot()
        return {
            "symbol":        self.symbol,
            "ltp":           round(ltp, 2),
            "change":        chg,
            "change_pct":    pct,
            "bias":          self.engine.current_bias or "NO BIAS",
            "balance":       round(b.balance, 2),
            "realised_pnl":  round(b.daily_net_pnl, 2),
            "unrealised_pnl":round(b.get_unrealised_pnl(ltp), 2) if ltp else 0,
            "position":      pos,
            "pending_order": self._pending_order,
            "daily_trades":  b.daily_trades,
            "ob_ready":      snap.is_valid(),
            "best_bid":      round(snap.best_bid or 0, 2),
            "best_ask":      round(snap.best_ask or 0, 2),
            "spread_pct":    round((snap.spread_pct or 0) * 100, 4),
            "imbalance":     round(snap.imbalance(5) or 0.5, 3),
            "bid_levels":    [[round(p, 2), q] for p, q in snap.bids[:5]],
            "ask_levels":    [[round(p, 2), q] for p, q in snap.asks[:5]],
            "kill_switch":   {
                "triggered": self.engine.kill_switch_triggered,
                "peak":      round(b.balance, 2),
                "current":   round(b.balance, 2),
                "percent":   CONFIG.get("kill_switch_percent", 0.9),
            },
        }


class OBRunner:
    """
    Manages 2 OBInstrumentRunners (TRENT + M&M).
    Shares WS client with main engine via add_global_listener.
    Reuses bias from main engine runners.
    Started/stopped manually from /ob page.
    """

    def __init__(self, main_runner=None, socketio_emit=None):
        """
        main_runner : live_runner from app.py (for bias sharing + WS sharing)
        socketio_emit : socketio.emit function
        """
        self._main_runner  = main_runner
        self._emit         = socketio_emit
        self._running      = False
        self._lock         = threading.Lock()

        risk = CONFIG.get("risk_per_trade", 0.01)
        self.runners: dict[str, OBInstrumentRunner] = {}

        for sym in OB_SYMBOLS:
            token = _TOKEN_MAP.get(sym, f"NSE_EQ|{sym}")
            r = OBInstrumentRunner(sym, token, risk)
            r._emit = socketio_emit
            self.runners[sym] = r
            print(f"[OBRunner] Initialized {sym} @ ₹{OB_BALANCE:,} risk={risk*100:.2f}%")

    def start(self):
        """
        Wire into main engine WS via global listener.
        If main engine isn't running, we can't get ticks — return error.
        """
        with self._lock:
            if self._running:
                return {"status": "already_running"}

            if not self._main_runner or not self._main_runner.is_connected():
                return {"status": "error",
                        "message": "Main engine must be running first — start main engine then OB engine"}

            # Sync bias from main engine immediately
            self._sync_bias()

            # Register global tick listener on the shared WS client
            self._main_runner.client.add_global_listener(self._on_global_tick)
            print("[OBRunner] ✅ Wired into main engine WS — listening for TRENT + M&M ticks")

            # Start bias sync thread — updates bias every 30s from main engine
            threading.Thread(target=self._bias_sync_loop, daemon=True,
                             name="OBBiasSync").start()

            # Start force-exit watchdog
            threading.Thread(target=self._force_exit_watchdog, daemon=True,
                             name="OBForceExit").start()

            self._running = True
            self._emit_status()
            return {"status": "ok"}

    def stop(self):
        with self._lock:
            if not self._running:
                return
            if self._main_runner:
                try:
                    listeners = self._main_runner.client._global_listeners
                    if self._on_global_tick in listeners:
                        listeners.remove(self._on_global_tick)
                except Exception:
                    pass
            self._running = False
            print("[OBRunner] Stopped")

    def is_running(self) -> bool:
        return self._running

    def _on_global_tick(self, key: str, ltp: float, prev_close=None,
                        timestamp=None, volume=None, atp=None, **kwargs):
        """Global WS listener — receives ticks for ALL symbols, filters TRENT + M&M."""
        if not self._running:
            return

        # Resolve symbol from token key
        sym = None
        for s, tok in _TOKEN_MAP.items():
            if key == tok or key == tok.replace("|", ":"):
                sym = s
                break
        if sym not in self.runners:
            return

        runner = self.runners[sym]

        # Extract depth from main engine's WS client order book registry
        bids, asks = [], []
        try:
            reg = self._main_runner.client.get_order_book_registry()
            if reg:
                ob = reg.get(key) or reg.get(key.replace("|", ":"))
                if ob:
                    snap = ob.snapshot()
                    bids = snap.bids
                    asks = snap.asks
                    # Also update our own order book
                    runner.order_book.update(bids, asks, time.time())
        except Exception:
            pass

        runner.on_tick(ltp=ltp, prev_close=prev_close,
                       timestamp=timestamp, bids=bids, asks=asks)

        # Emit live update to /ob page
        if self._emit:
            try:
                self._emit("ob_live_update", self._get_payload())
            except Exception:
                pass

    def _sync_bias(self):
        """Pull current bias from main engine runners into OB runners."""
        if not self._main_runner:
            return
        for token, main_r in self._main_runner.runners.items():
            sym = main_r.symbol
            if sym in self.runners:
                bias = main_r.engine.current_bias
                if bias:
                    self.runners[sym].set_bias_from_main(bias)

    def _bias_sync_loop(self):
        """Background thread — syncs bias from main engine every 30s."""
        while self._running:
            time.sleep(30)
            try:
                self._sync_bias()
            except Exception as e:
                print(f"[OBRunner] Bias sync error: {e}")

    def _force_exit_watchdog(self):
        """Force-close all OB positions at 15:25 IST."""
        print("[OBRunner] Force-exit watchdog started")
        while self._running:
            time.sleep(10)
            now = datetime.now(_IST).time()
            from datetime import time as _dtime
            if _dtime(15, 25) <= now <= _dtime(15, 26):
                print("[OBRunner] ⏰ 15:25 — force-closing OB positions")
                for sym, runner in self.runners.items():
                    try:
                        if runner.broker.position:
                            ltp = runner.last_price or runner.broker.position.get("entry_price", 0)
                            runner.broker.force_close(ltp, pd.Timestamp.now(tz="Asia/Kolkata"))
                            print(f"[OBRunner] Force-closed {sym} @ ₹{ltp}")
                    except Exception as e:
                        print(f"[OBRunner] Force-close error {sym}: {e}")
                if self._emit:
                    self._emit("ob_live_update", self._get_payload())
                break

    def get_all_status(self) -> list:
        return [r.get_status() for r in self.runners.values()]

    def _get_payload(self) -> dict:
        instruments = self.get_all_status()
        total_real  = sum(i["realised_pnl"]   for i in instruments)
        total_unr   = sum(i["unrealised_pnl"]  for i in instruments)
        return {
            "instruments":    instruments,
            "total_realised": round(total_real, 2),
            "total_unreal":   round(total_unr, 2),
            "total_net":      round(total_real + total_unr, 2),
            "running":        self._running,
        }

    def _emit_status(self):
        if self._emit:
            self._emit("ob_live_update", self._get_payload())