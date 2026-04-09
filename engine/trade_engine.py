# engine/trade_engine.py
#
# Core engine — identical logic for backtest and live.
#
# CRITICAL: backtest_rr param marks a run as backtest.
# When backtest_rr is passed, _load_state() is NEVER called.
# This prevents old live engine_state_*.json files from corrupting
# backtest results.
#
# ─────────────────────────────────────────────────────────────────
# TRAILING KILL SWITCH  (live mode only — disabled in backtest)
# ─────────────────────────────────────────────────────────────────
#  equity_peak = highest equity seen this session. ONLY moves up.
#  threshold   = equity_peak × kill_switch_percent  (also moves up)
# ─────────────────────────────────────────────────────────────────

import pandas as pd
from config import CONFIG
from engine.session import is_entry_allowed, is_force_exit_time
from engine.scheduler import DailyScheduler
from engine.state_manager import StateManager


class TradeEngine:

    def __init__(self, broker,
                 cooldown_candles:    int   = None,
                 min_price_distance:  float = None,
                 symbol:              str   = "",
                 backtest_rr:         float = None):

        self.broker       = broker
        self.symbol       = symbol
        self.backtest_rr  = backtest_rr

        # ── CRITICAL: backtest detection ──────────────────────────────
        # backtest_rr being set = this is a backtest run.
        # Never load live state in a backtest run.
        self._is_backtest = (backtest_rr is not None)

        self.htf = None
        self.ltf = None

        self.current_bias = None
        self.last_bias    = None

        self.last_entry_index   = -1000
        self.cooldown_candles   = cooldown_candles   if cooldown_candles   is not None else CONFIG.get("cooldown", 300)
        self.min_price_distance = min_price_distance if min_price_distance is not None else CONFIG.get("min_price_distance", 5)
        self.last_entry_price   = None

        self.index               = 0
        self.last_processed_time = None

        self.replay_mode = False

        self.equity_peak           = broker.balance
        self.kill_switch_triggered = False

        self.scheduler     = DailyScheduler(broker, symbol=symbol)
        self.state_manager = StateManager(symbol=symbol)

        # ONLY load state in live mode AND NOT a backtest
        if not self._is_backtest and CONFIG.get("mode") == "live":
            self._load_state()

    def attach_engines(self, htf_engine, ltf_engine):
        self.htf = htf_engine
        self.ltf = ltf_engine
        if self.backtest_rr is not None and hasattr(self.ltf, "set_rr"):
            self.ltf.set_rr(self.backtest_rr)

    def attach(self, htf_engine, ltf_engine):
        """Alias for live instrument_runner."""
        self.attach_engines(htf_engine, ltf_engine)

    def start_replay(self): self.replay_mode = True
    def stop_replay(self):  self.replay_mode = False

    def on_tick(self, tick: dict):
        if not tick:
            return
        ltp       = tick.get("ltp")
        timestamp = tick.get("timestamp")
        if ltp is None or timestamp is None:
            return
        candle = pd.Series(
            {"open": ltp, "high": ltp, "low": ltp, "close": ltp},
            name=pd.to_datetime(timestamp),
        )
        self.on_ltf_candle(candle)

    def on_htf_candle(self, candle):
        if not self.htf:
            return
        bias = self.htf.update(candle)
        if bias != self.last_bias:
            self.current_bias = bias
            self.last_bias    = bias
            # Persist bias immediately so it survives restart
            self._save_state()

    def on_ltf_candle(self, candle):
        self.index           += 1
        current_time          = candle.name.time()
        self.last_processed_time = candle.name
        price                 = candle["close"]

        # 1. Broker update
        self.broker.update(price, candle.name)
        self.broker.record_equity(time=candle.name, current_price=price)
        current_equity = self.broker.get_equity(price)

        # 2. Update trailing equity peak
        if current_equity > self.equity_peak:
            self.equity_peak = current_equity

        # 3. Trailing kill switch — LIVE only
        if not self._is_backtest and CONFIG.get("kill_switch_enabled", False):
            percent   = CONFIG.get("kill_switch_percent", 0.90)
            threshold = self.equity_peak * percent

            if current_equity <= threshold:
                if not self.kill_switch_triggered:
                    self.kill_switch_triggered = True
                    self._send_kill_switch_alert(threshold)
                if CONFIG.get("mode") == "live":
                    self._save_state()
                # Only block new entries — still allow existing position to exit
                if not self.broker.position:
                    return

        # 4. Force exit at session end
        if is_force_exit_time(current_time):
            if self.broker.position:
                self.broker.force_close(price=price, time=candle.name)
            if not self._is_backtest and CONFIG.get("mode") == "live":
                self._save_state()
            return

        # 5. Session filter
        # Apply in BOTH live and backtest so entries are realistic and
        # don't fire near force-exit where charges dominate tiny moves.
        if not is_entry_allowed(current_time):
            if not self._is_backtest and CONFIG.get("mode") == "live":
                self._save_state()
            return

        # 6. Guards
        if not self.current_bias:
            return
        if self.broker.position:
            return
        if (self.index - self.last_entry_index) < self.cooldown_candles:
            return
        if self.replay_mode:
            return

        # 7. LTF signal
        if not self.ltf:
            return
        signal = self.ltf.update(candle, self.current_bias)
        if not signal:
            return

        # 8. Price distance filter
        if (self.last_entry_price is not None and
                abs(signal["entry"] - self.last_entry_price) < self.min_price_distance):
            return

        # 9. Execute trade
        self.broker.open(
            side=signal["side"],
            price=signal["entry"],
            stop=signal["stop"],
            target=signal["target"],
            time=candle.name,
        )
        self.last_entry_index = self.index
        self.last_entry_price = signal["entry"]

        # 9b. Emit signal alert to dashboard
        try:
            from instrument_runner import _socketio_emit
            if _socketio_emit:
                import datetime as _dt
                _socketio_emit("signal_fired", {
                    "symbol":    self.symbol,
                    "side":      signal["side"],
                    "entry":     signal["entry"],
                    "stop":      signal["stop"],
                    "target":    signal["target"],
                    "qty":       self.broker.position.get("qty", 0) if self.broker.position else 0,
                    "bias":      self.current_bias,
                    "rr":        round(abs(signal["target"] - signal["entry"]) / abs(signal["entry"] - signal["stop"]), 1) if signal["entry"] != signal["stop"] else 0,
                    "time":      _dt.datetime.now().strftime("%H:%M:%S"),
                    "ts":        _dt.datetime.now().isoformat(),
                })
        except Exception:
            pass

        # 10. Scheduler + state (live only)
        if not self._is_backtest and CONFIG.get("mode") == "live":
            self.scheduler.check(candle_time=candle.name, current_price=price)
            self._save_state()

    def get_summary(self) -> dict:
        b     = self.broker
        start = b.starting_balance
        net   = b.total_net_pnl
        wins  = sum(1 for t in b.trade_log if t["net_pnl"] > 0)
        total = len(b.trade_log)
        return {
            "symbol":           self.symbol,
            "starting_balance": start,
            "final_balance":    round(b.balance, 2),
            "pct_return":       round(net / start * 100, 2) if start else 0,
            "gross_pnl":        round(b.total_gross_pnl, 2),
            "charges":          round(b.total_charges, 2),
            "net_pnl":          round(net, 2),
            "total_trades":     total,
            "wins":             wins,
            "losses":           total - wins,
            "win_rate":         round(wins / total * 100, 1) if total else 0,
            "equity_peak":      round(self.equity_peak, 2),
            "kill_triggered":   self.kill_switch_triggered,
        }

    def _send_kill_switch_alert(self, threshold: float):
        try:
            from telegram.notifier import send_telegram_message
            pct = int(CONFIG.get("kill_switch_percent", 0.9) * 100)
            send_telegram_message(
                CONFIG["telegram_bot_token"],
                CONFIG["telegram_chat_id"],
                f"🚨 KILL SWITCH [{self.symbol or 'ENGINE'}]\n\n"
                f"Peak Equity:  ₹{self.equity_peak:,.0f}\n"
                f"Threshold:    ₹{threshold:,.0f}  ({pct}% of peak)\n"
                f"Current:      ₹{self.broker.balance:,.0f}\n\n"
                f"New entries halted for the session."
            )
        except Exception:
            pass

    def _save_state(self):
        if self._is_backtest or CONFIG.get("mode") != "live":
            return
        self.state_manager.save({
            "current_bias":          self.current_bias,
            "last_entry_index":      self.last_entry_index,
            "last_entry_price":      self.last_entry_price,
            "equity_peak":           self.equity_peak,
            "kill_switch_triggered": self.kill_switch_triggered,
            "open_position":         self.broker.position,
            "last_processed_time":   str(self.last_processed_time) if self.last_processed_time else None,
            "broker":                self.broker.get_state(),
        })

    def _load_state(self):
        if self._is_backtest or CONFIG.get("mode") != "live":
            return
        state = self.state_manager.load()
        if not state:
            return

        self.current_bias          = state.get("current_bias")
        self.last_entry_index      = state.get("last_entry_index", -1000)
        self.last_entry_price      = state.get("last_entry_price")
        self.equity_peak           = state.get("equity_peak") or self.broker.balance
        self.kill_switch_triggered = state.get("kill_switch_triggered", False)

        # Only restore open position if it's from TODAY — never replay a stale
        # position from a previous trading day (causes duplicate DB writes).
        from datetime import date as _date
        saved_pos = state.get("open_position")
        if saved_pos:
            entry_time_str = str(saved_pos.get("entry_time", ""))
            today_str = str(_date.today())
            if entry_time_str.startswith(today_str):
                self.broker.position = saved_pos
            else:
                print(f"⚠️  [{self.symbol}] Stale position from {entry_time_str[:10]} discarded — not restoring.")
                self.broker.position = None
        else:
            self.broker.position = None

        broker_state = state.get("broker")
        if broker_state:
            self.broker.restore(
                balance=broker_state.get("balance"),
                trade_log=broker_state.get("trade_log"),
                totals=broker_state.get("totals", {}),
            )

        t = state.get("last_processed_time")
        if t:
            self.last_processed_time = pd.to_datetime(t)

        print(f"✅ [{self.symbol}] Engine state restored.")
