# live_engine/limit_order_manager.py
#
# Manages limit order lifecycle for paper trading:
#   1. Signal fires with limit price (from liquidity filter)
#   2. Monitor order book every second for 5s — if price stays at/better
#      than limit → simulate fill at limit price
#   3. If price moves away within 5s → miss, cooldown already consumed
#   4. On fill → recalculate SL/target from actual fill price
#      (stop stays at prev candle high/low, target shifts with entry)
#
# PAPER MODE only for now. Live mode: swap _attempt_fill() with real API call.

import threading
import time
import logging
from dataclasses import dataclass
from typing import Optional, Callable
from live_engine.order_book import OrderBook, OrderBookSnapshot

log = logging.getLogger(__name__)


@dataclass
class LimitOrder:
    symbol:       str
    side:         str           # 'BUY' or 'SELL'
    limit_price:  float         # bid or ask at signal time
    stop:         float         # structural stop (prev candle high/low)
    rr:           float         # RR target multiplier
    qty:          int           # calculated from risk
    signal_time:  str           # candle timestamp string

    # Set after fill
    filled_price: Optional[float] = None
    filled_qty:   Optional[int]   = None
    status:       str = 'pending'   # pending | filled | missed | cancelled


@dataclass
class FillResult:
    filled:      bool
    fill_price:  Optional[float]
    fill_qty:    Optional[int]
    stop:        Optional[float]   # recalculated from fill price
    target:      Optional[float]   # recalculated from fill price
    reason:      str


class LimitOrderManager:
    """
    Paper-mode limit order simulation.

    Fill logic:
        - Poll order book every 1s for up to fill_timeout_secs (default 5s)
        - BUY  fills if ask[0] <= limit_price (price came to us or better)
        - SELL fills if bid[0] >= limit_price (price came to us or better)
        - If 5s pass without fill → missed
    """

    def __init__(self,
                 fill_timeout_secs:   int   = 5,
                 poll_interval_secs:  float = 1.0):
        self.fill_timeout_secs  = fill_timeout_secs
        self.poll_interval_secs = poll_interval_secs

    def execute(self,
                order:      LimitOrder,
                order_book: OrderBook,
                on_fill:    Callable[[FillResult], None],
                on_miss:    Callable[[str], None]):
        """
        Launch fill monitoring in a background thread.

        on_fill(FillResult) — called if filled within timeout
        on_miss(reason)     — called if timeout or price moved away
        """
        t = threading.Thread(
            target=self._monitor,
            args=(order, order_book, on_fill, on_miss),
            daemon=True,
            name=f"LimitOrder-{order.symbol}"
        )
        t.start()

    def _monitor(self,
                 order:      LimitOrder,
                 order_book: OrderBook,
                 on_fill:    Callable,
                 on_miss:    Callable):
        """
        Poll order book every poll_interval_secs.
        Fill if price stays at/better than limit for 5s.
        """
        deadline = time.time() + self.fill_timeout_secs
        print(f"[LOM] {order.symbol} {order.side} limit@{order.limit_price} — monitoring {self.fill_timeout_secs}s")

        while time.time() < deadline:
            snap = order_book.snapshot()

            if not snap.is_valid():
                time.sleep(self.poll_interval_secs)
                continue

            filled, fill_price = self._attempt_fill(order, snap)

            if filled:
                result = self._build_fill_result(order, fill_price)
                order.filled_price = fill_price
                order.filled_qty   = order.qty
                order.status       = 'filled'
                print(f"[LOM] ✅ {order.symbol} {order.side} FILLED @ ₹{fill_price:.2f} "
                      f"(SL={result.stop} TGT={result.target})")
                try:
                    on_fill(result)
                except Exception as e:
                    log.error(f"[LOM] on_fill error: {e}")
                return

            time.sleep(self.poll_interval_secs)

        # Timeout — missed
        order.status = 'missed'
        reason = (f"{order.symbol} {order.side} limit@{order.limit_price} "
                  f"missed after {self.fill_timeout_secs}s")
        print(f"[LOM] ⏰ {reason}")
        try:
            on_miss(reason)
        except Exception as e:
            log.error(f"[LOM] on_miss error: {e}")

    def _attempt_fill(self, order: LimitOrder,
                      snap: OrderBookSnapshot) -> tuple[bool, float]:
        """
        Paper fill check:
            BUY  → fill if best ask <= limit_price
            SELL → fill if best bid >= limit_price
        Returns (filled, fill_price)
        """
        if order.side == 'BUY':
            best_ask = snap.best_ask
            if best_ask and best_ask <= order.limit_price:
                # Fill at best ask (could be better than our limit)
                return True, best_ask
        else:  # SELL
            best_bid = snap.best_bid
            if best_bid and best_bid >= order.limit_price:
                # Fill at best bid
                return True, best_bid
        return False, 0.0

    def _build_fill_result(self, order: LimitOrder,
                            fill_price: float) -> FillResult:
        """
        Recalculate SL and target from actual fill price.
        Stop stays at structural level (prev candle high/low).
        Target shifts based on actual risk from fill price.
        """
        risk = abs(fill_price - order.stop)

        if risk <= 0:
            # Degenerate case — use tiny risk to avoid zero target
            risk = fill_price * 0.003

        if order.side == 'BUY':
            stop   = round(order.stop, 2)
            target = round(fill_price + risk * order.rr, 2)
        else:
            stop   = round(order.stop, 2)
            target = round(fill_price - risk * order.rr, 2)

        return FillResult(
            filled     = True,
            fill_price = round(fill_price, 2),
            fill_qty   = order.qty,
            stop       = stop,
            target     = target,
            reason     = f"limit_fill@{fill_price:.2f}",
        )
