# live_engine/instrument_runner.py
# One InstrumentRunner per symbol — LIVE mode only.
# Receives ticks from UpstoxV3Client (streaming, not polling).
# Never mutates global CONFIG.

from broker.paper_broker import PaperBroker
from engine.trade_engine import TradeEngine
from engine.htf_structure import HTFStructure
from engine.ltf_entry import LTFEntry
from engine.scheduler import DailyScheduler
from live_engine.live_candle_builder import LiveCandleBuilder
from config import CONFIG

# SocketIO emitter — set by app.py so on_tick can push ticks directly to browser
_socketio_emit = None   # fn(event, data)

def set_socketio_emitter(fn):
    global _socketio_emit
    _socketio_emit = fn


class InstrumentRunner:

    def __init__(self, symbol: str, instrument_token: str, preset: dict):
        self.symbol           = symbol
        self.instrument_token = instrument_token

        risk     = float(preset.get("risk_per_trade",     CONFIG["risk_per_trade"]))
        cooldown = int(preset.get("cooldown",             CONFIG["cooldown"]))
        min_dist = float(preset.get("min_price_distance", CONFIG["min_price_distance"]))
        rr       = float(preset.get("rr_target",          CONFIG["rr_target"]))

        self.broker = PaperBroker(
            balance=CONFIG["starting_balance"],
            risk_per_trade=risk,
            symbol=symbol,
            live_mode=True,
        )

        self.engine = TradeEngine(
            self.broker,
            cooldown_candles=cooldown,
            min_price_distance=min_dist,
            symbol=symbol,
            # backtest_rr=None → live mode, state loaded from DB
        )
        self.engine.attach(HTFStructure(), LTFEntry(rr_target=rr))

        self.builder_5m  = LiveCandleBuilder(5)
        self.builder_15m = LiveCandleBuilder(15)
        self.scheduler   = DailyScheduler(self.broker, symbol)

        # Order book execution (set by MSR after WS connects)
        self._order_book = None   # OrderBook instance for this symbol
        self._liq_filter = None   # LiquidityFilter instance
        self._lom        = None   # LimitOrderManager instance
        self._ob_enabled = CONFIG.get("order_book_enabled", False)
        self._pending_ob_signal = None  # signal waiting for OB fill

        # Tick state
        self.last_price  = None
        self.last_time   = None
        self.prev_close  = None
        self.last_volume = None
        self.last_atp    = None

        # Seed prev_close from heatmap cache immediately so colour is correct
        # even before first fullFeed tick arrives from Upstox
        try:
            import heatmap_feed as _hf
            cached = _hf.get_ltp(self.symbol)
            if cached and cached.get("prev_close"):
                self.prev_close = cached["prev_close"]
        except Exception:
            pass

    def attach_order_book(self, order_book, liq_filter, lom):
        """Called by MSR after WS connects. Enables order book execution."""
        self._order_book = order_book
        self._liq_filter = liq_filter
        self._lom        = lom
        self._ob_enabled = True
        print(f"[{self.symbol}] Order book execution enabled")

    # ── Tick entry point ──────────────────────────────────────────────────────

    def on_tick(self, ltp: float, prev_close=None, timestamp=None,
                volume=None, atp=None):
        """
        Called by MultiStrategyRunner for every WS tick.
        Signature matches UpstoxV3Client per-token callback.
        """
        self.last_price  = ltp
        self.last_time   = timestamp
        self.last_volume = volume
        self.last_atp    = atp

        # Capture prev_close once at start of day
        if prev_close and not self.prev_close:
            self.prev_close = prev_close

        # Emit tick directly to browser — true real-time, no 1s push_loop delay
        if _socketio_emit:
            try:
                from datetime import datetime as _dt
                pc   = self.prev_close or ltp
                chg  = round(ltp - pc, 2)
                pct  = round((chg / pc) * 100, 2) if pc else 0.0
                ts   = timestamp.strftime("%H:%M:%S") if timestamp else _dt.now().strftime("%H:%M:%S")
                _socketio_emit("tick_update", {
                    "symbol":     self.symbol,
                    "ltp":        round(ltp, 2),
                    "prev_close": round(pc, 2),
                    "change":     chg,
                    "change_pct": pct,
                    "ts":         ts,
                }, broadcast=True)
            except Exception as _e:
                import logging as _log
                _log.getLogger(__name__).debug(f"tick_update emit error [{self.symbol}]: {_e}")

        # Feed tick into candle builders
        candle_5m  = self.builder_5m.update(ltp, timestamp)
        candle_15m = self.builder_15m.update(ltp, timestamp)

        # Closed HTF candle → update structure bias
        if candle_15m is not None:
            self.engine.on_htf_candle(candle_15m)

        # Closed LTF candle → check for entry signal + daily schedule
        if candle_5m is not None:
            if self._ob_enabled and self._order_book and not self.broker.position:
                # Order book mode: intercept signal, run liquidity check, place limit
                self._on_ltf_candle_ob(candle_5m)
            else:
                # Standard mode: direct broker open
                self.engine.on_ltf_candle(candle_5m)
            self.scheduler.check(
                candle_time=candle_5m.name,
                current_price=candle_5m["close"],
            )

    def _on_ltf_candle_ob(self, candle):
        """
        Order book execution path:
        1. Ask trade engine to generate signal (dry run — no broker.open yet)
        2. Run liquidity filter on current order book snapshot
        3a. Pass  → place limit order, wait for fill, then open position
        3b. Fail  → consume cooldown, skip entry, log reason
        """
        # Temporarily monkey-patch broker.open to intercept the signal
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
            return  # no signal generated

        signal = intercepted['signal']
        snap   = self._order_book.snapshot()

        # ── Liquidity check ────────────────────────────────────────────────
        result = self._liq_filter.check(signal['side'], snap)

        if not result.passed:
            # Consume cooldown so we don't re-enter on next candle
            self.engine.last_entry_index = self.engine.index
            print(f"[{self.symbol}] 🚫 OB filter rejected: {result.reason}")
            # Emit as missed signal to dashboard
            try:
                from live_engine.instrument_runner import _socketio_emit
                if _socketio_emit:
                    import datetime as _dt
                    _socketio_emit("signal_rejected", {
                        "symbol": self.symbol,
                        "side":   signal['side'],
                        "reason": result.reason,
                        "time":   _dt.datetime.now().strftime("%H:%M:%S"),
                    })
            except Exception:
                pass
            return

        # ── Liquidity passed — build limit order ───────────────────────────
        entry_price = result.entry_price  # best ask (BUY) or best bid (SELL)
        rr          = self.engine.ltf._rr_target()
        risk        = abs(entry_price - signal['stop'])
        if risk <= 0:
            risk = entry_price * 0.003
        qty = max(1, int((self.broker.risk_per_trade * self.broker.balance) / risk))

        from live_engine.limit_order_manager import LimitOrder
        order = LimitOrder(
            symbol      = self.symbol,
            side        = signal['side'],
            limit_price = entry_price,
            stop        = signal['stop'],
            rr          = rr,
            qty         = qty,
            signal_time = str(signal.get('time', '')),
        )

        print(f"[{self.symbol}] 📋 OB limit order: {signal['side']} @ ₹{entry_price:.2f} "
              f"(spread={result.spread_pct*100:.3f}% imbal={result.imbalance:.3f})")

        # Emit signal to dashboard immediately (pending fill)
        try:
            from live_engine.instrument_runner import _socketio_emit
            if _socketio_emit:
                import datetime as _dt
                _socketio_emit("signal_fired", {
                    "symbol":  self.symbol,
                    "side":    signal['side'],
                    "entry":   entry_price,
                    "stop":    signal['stop'],
                    "target":  signal['target'],
                    "qty":     qty,
                    "bias":    self.engine.current_bias,
                    "rr":      round(rr, 1),
                    "time":    _dt.datetime.now().strftime("%H:%M:%S"),
                    "ts":      _dt.datetime.now().isoformat(),
                    "ob_mode": True,
                })
        except Exception:
            pass

        # ── Launch limit order monitoring ──────────────────────────────────
        def on_fill(fill_result):
            if self.broker.position:
                return  # already in position (race condition guard)
            self.broker.open(
                side   = signal['side'],
                price  = fill_result.fill_price,
                stop   = fill_result.stop,
                target = fill_result.target,
                time   = candle.name,
            )
            self.engine.last_entry_index = self.engine.index
            self.engine.last_entry_price = fill_result.fill_price

        def on_miss(reason):
            # Cooldown already consumed above at liquidity check pass
            # but we need to consume it here (fill timeout path)
            self.engine.last_entry_index = self.engine.index
            print(f"[{self.symbol}] ⏰ Limit order missed: {reason}")

        # Consume cooldown now — prevents new signal while order is pending
        self.engine.last_entry_index = self.engine.index
        self._lom.execute(order, self._order_book, on_fill, on_miss)

    # ── Status for live dashboard ──────────────────────────────────────────────

    def get_status(self) -> dict:
        b   = self.broker
        ltp = self.last_price
        # Fallback: read from heatmap cache if runner hasn't received a direct tick yet
        if not ltp:
            try:
                import heatmap_feed as _hf
                cached = _hf.get_ltp(self.symbol)
                if cached:
                    ltp = cached.get("ltp")
                    if not self.prev_close:
                        self.prev_close = cached.get("prev_close")
            except Exception:
                pass
        pc  = self.prev_close
        chg     = round(ltp - pc, 2)         if ltp and pc else None
        chg_pct = round((chg / pc) * 100, 2) if chg and pc else None
        # Build daily trades list for UI (today's closed trades)
        from datetime import date as _date
        today_str = str(_date.today())
        daily_trades = [
            {
                "direction":   t.get("side", t.get("direction", "")),
                "entry_time":  str(t.get("entry_time", "")),
                "exit_time":   str(t.get("exit_time", "")),
                "entry_price": t.get("entry_price", t.get("entry", 0)),
                "exit_price":  t.get("exit_price",  t.get("exit",  0)),
                "qty":         t.get("qty", 0),
                "net_pnl":     round(t.get("net_pnl", 0), 2),
                "gross_pnl":   round(t.get("gross_pnl", 0), 2),
                "charges":     round(t.get("charges", 0), 2),
                "reason":      t.get("reason", ""),
            }
            for t in b.trade_log
            if str(t.get("entry_time", "")).startswith(today_str)
        ]
        realised   = sum(t["net_pnl"] for t in daily_trades)
        unrealised = round(b.get_equity(ltp or b.balance) - b.balance, 2) if b.position else 0.0

        return {
            "symbol":        self.symbol,
            "ltp":           ltp,
            "prev_close":    pc,
            "change":        chg,
            "change_pct":    chg_pct,
            "volume":        self.last_volume,
            "atp":           self.last_atp,
            "bias":          self.engine.current_bias,
            "position":      b.position,
            "balance":       round(b.balance, 2),
            "equity":        round(b.get_equity(ltp or b.balance), 2),
            "realised_pnl":  round(realised, 2),
            "unrealised_pnl":round(unrealised, 2),
            "daily_net":     round(b.daily_net_pnl, 2),
            "total_net":     round(b.total_net_pnl, 2),
            "daily_trades":  daily_trades,
            "kill_switch": {
                "triggered": self.engine.kill_switch_triggered,
                "peak":      round(self.engine.equity_peak, 2) if self.engine.equity_peak is not None else None,
                "current":   round(b.get_equity(ltp or b.balance), 2),
                "percent":   CONFIG.get("kill_switch_percent", 0.90),
            },
        }

    def get_trades(self) -> list:
        return self.broker.trade_log

    def get_equity_log(self) -> list:
        return self.broker.equity_log