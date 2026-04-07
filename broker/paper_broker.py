# broker/paper_broker.py
#
# QTY SIZING — identical formula to old project (structure-engine-v2)
# ─────────────────────────────────────────────────────────────────────
#
#   qty_risk    = (balance × risk_per_trade) / stop_distance
#   qty_capital = (balance × 5) / price          ← hardcoded 5x MTF
#   qty         = floor(min(qty_risk, qty_capital))
#
# qty_capital is the hard cap — no external imports, cannot fail.
# 5x = 20% margin requirement (standard Upstox MTF intraday).
#
# FIX (2026-03-12): Added threading.Lock() around position open/close
# to prevent race condition where two simultaneous ticks both see
# position != None and both call _close(), writing duplicate DB rows.

import math
import threading


class PaperBroker:

    BROKERAGE_PER_ORDER = 20
    STT_RATE            = 0.00025
    EXCHANGE_RATE       = 0.0000345
    GST_RATE            = 0.18
    SEBI_RATE           = 0.000001
    LEVERAGE            = 5          # 5x MTF intraday (20% margin)

    def __init__(self, balance: float, risk_per_trade: float = 0.01,
                 symbol: str = "", live_mode: bool = False):
        self.starting_balance = balance
        self.balance          = balance
        self.risk_per_trade   = risk_per_trade
        self.symbol           = symbol
        self.live_mode        = live_mode

        self.position   = None
        self.trade_log  = []
        self.equity_log = []

        self.total_gross_pnl = 0.0
        self.total_charges   = 0.0
        self.total_net_pnl   = 0.0
        self.daily_gross_pnl = 0.0
        self.daily_net_pnl   = 0.0
        self.daily_trades    = 0

        # ── FIX: lock prevents simultaneous tick/candle threads both
        # seeing position != None and writing duplicate DB rows ──────
        self._lock = threading.Lock()

    # ── Open ──────────────────────────────────────────────────────────────────
    def open(self, side: str, price: float, stop: float,
             target: float, time) -> dict | None:

        with self._lock:
            if self.position or price <= 0:
                return None

            stop_distance = abs(price - stop)
            if stop_distance <= 0:
                return None

            qty_risk    = (self.balance * self.risk_per_trade) / stop_distance
            qty_capital = (self.balance * self.LEVERAGE) / price
            qty         = math.floor(min(qty_risk, qty_capital))

            if qty <= 0:
                return None

            self.position = {
                "side":        side,
                "entry_price": round(price, 2),
                "stop":        round(stop, 2),
                "target":      round(target, 2),
                "qty":         qty,
                "entry_time":  time,
            }
            pos_snapshot = dict(self.position)

        # Telegram entry alert outside lock (network call — don't hold lock)
        if self.live_mode and self.symbol:
            try:
                from config import CONFIG
                bot  = CONFIG.get("telegram_bot_token", "")
                chat = CONFIG.get("telegram_chat_id", "")
                if bot and chat:
                    rr   = round(abs(target - price) / abs(price - stop), 1) if price != stop else 0
                    side_emoji = "🟢" if side == "BUY" else "🔴"
                    msg = (
                        f"{side_emoji} <b>ENTRY — {self.symbol}</b>\n\n"
                        f"Side:    <b>{side}</b>\n"
                        f"Entry:   ₹{price:,.2f}\n"
                        f"Stop:    ₹{stop:,.2f}  ({round(abs(price-stop),2):+.2f})\n"
                        f"Target:  ₹{target:,.2f}  ({round(abs(target-price),2):+.2f})\n"
                        f"Qty:     {qty}\n"
                        f"RR:      1:{rr}\n"
                        f"Time:    {str(time)[11:16]}"
                    )
                    import requests as _req
                    _req.post(
                        f"https://api.telegram.org/bot{bot}/sendMessage",
                        json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
                        timeout=6,
                    )
            except Exception:
                pass

        return pos_snapshot

    # ── Update every candle ───────────────────────────────────────────────────
    def update(self, price: float, time) -> dict | None:
        if not self.position:
            return None

        side, stop, target = (self.position[k] for k in ("side", "stop", "target"))
        hit, exit_price = False, price

        if side == "BUY":
            if price <= stop:     exit_price, hit = stop,   True
            elif price >= target: exit_price, hit = target, True
        else:
            if price >= stop:     exit_price, hit = stop,   True
            elif price <= target: exit_price, hit = target, True

        return self._close(exit_price, time, "sl_or_target") if hit else None

    # ── Force close ───────────────────────────────────────────────────────────
    def force_close(self, price: float, time) -> dict | None:
        return self._close(price, time, "force_exit") if self.position else None

    # ── Internal close ────────────────────────────────────────────────────────
    def _close(self, exit_price: float, time, reason: str) -> dict | None:
        with self._lock:
            # FIX: re-check inside lock — another thread may have already closed
            if not self.position:
                return None

            p, qty = self.position, self.position["qty"]

            gross = (exit_price - p["entry_price"]) * qty if p["side"] == "BUY" \
                    else (p["entry_price"] - exit_price) * qty

            charges = self._calc_charges(p["entry_price"], exit_price, qty)
            net     = gross - charges

            self.balance         += net
            self.total_gross_pnl += gross
            self.total_charges   += charges
            self.total_net_pnl   += net
            self.daily_gross_pnl += gross
            self.daily_net_pnl   += net
            self.daily_trades    += 1

            trade = {
                "direction":   p["side"],
                "entry_time":  str(p["entry_time"]),
                "exit_time":   str(time),
                "entry_price": round(p["entry_price"], 2),
                "exit_price":  round(exit_price, 2),
                "qty":         qty,
                "gross_pnl":   round(gross, 2),
                "charges":     round(charges, 2),
                "net_pnl":     round(net, 2),
                "reason":      reason,
            }
            self.trade_log.append(trade)
            self.position = None   # cleared inside lock — prevents any race

        # DB write + Telegram outside lock (slow I/O — don't hold lock)
        if self.live_mode and self.symbol:
            try:
                import db
                from datetime import datetime
                db.save_trade(self.symbol, trade,
                              date_str=datetime.now().strftime("%Y-%m-%d"))
            except Exception:
                pass

            try:
                from config import CONFIG
                bot  = CONFIG.get("telegram_bot_token", "")
                chat = CONFIG.get("telegram_chat_id", "")
                if bot and chat:
                    pnl_emoji = "✅" if net >= 0 else "❌"
                    reason_map = {
                        "sl_or_target": "🎯 Target" if net >= 0 else "🛑 Stop Loss",
                        "force_exit":   "⏰ EOD Exit",
                    }
                    reason_label = reason_map.get(reason, reason)
                    msg = (
                        f"{pnl_emoji} <b>EXIT — {self.symbol}</b>  [{reason_label}]\n\n"
                        f"Side:    {p['side']}\n"
                        f"Entry:   ₹{p['entry_price']:,.2f}  ({str(p['entry_time'])[11:16]})\n"
                        f"Exit:    ₹{exit_price:,.2f}  ({str(time)[11:16]})\n"
                        f"Qty:     {qty}\n\n"
                        f"Gross:   ₹{gross:+,.2f}\n"
                        f"Charges: ₹{charges:,.2f}\n"
                        f"<b>Net:     ₹{net:+,.2f}</b>\n\n"
                        f"Balance: ₹{self.balance:,.2f}"
                    )
                    import requests as _req
                    _req.post(
                        f"https://api.telegram.org/bot{bot}/sendMessage",
                        json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
                        timeout=6,
                    )
            except Exception:
                pass

        return trade

    # ── Equity ────────────────────────────────────────────────────────────────
    def get_equity(self, price: float) -> float:
        if not self.position:
            return self.balance
        p = self.position
        unreal = (price - p["entry_price"]) * p["qty"] if p["side"] == "BUY" \
                 else (p["entry_price"] - price) * p["qty"]
        return self.balance + unreal

    def record_equity(self, time, current_price: float):
        self.equity_log.append({
            "time":   str(time),
            "equity": round(self.get_equity(current_price), 2),
        })

    # ── State ─────────────────────────────────────────────────────────────────
    def get_state(self) -> dict:
        return {
            "balance":    self.balance,
            "trade_log":  self.trade_log,
            "equity_log": self.equity_log,
            "totals": {
                "gross":   self.total_gross_pnl,
                "charges": self.total_charges,
                "net":     self.total_net_pnl,
            },
        }

    def restore(self, balance: float, trade_log: list, totals: dict):
        self.balance         = balance
        self.trade_log       = trade_log or []
        self.total_gross_pnl = totals.get("gross", 0)
        self.total_charges   = totals.get("charges", 0)
        self.total_net_pnl   = totals.get("net", 0)

    def reset_daily_stats(self):
        self.daily_gross_pnl = 0.0
        self.daily_net_pnl   = 0.0
        self.daily_trades    = 0

    # ── Charges ───────────────────────────────────────────────────────────────
    def _calc_charges(self, entry: float, exit_: float, qty: int) -> float:
        turnover  = (entry + exit_) * qty
        brokerage = self.BROKERAGE_PER_ORDER * 2
        stt       = exit_ * qty * self.STT_RATE
        exchange  = turnover * self.EXCHANGE_RATE
        gst       = (brokerage + exchange) * self.GST_RATE
        sebi      = turnover * self.SEBI_RATE
        return round(brokerage + stt + exchange + gst + sebi, 2)