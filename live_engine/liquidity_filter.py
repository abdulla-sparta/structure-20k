# live_engine/liquidity_filter.py
#
# Checks order book conditions before allowing a signal to proceed.
# Two checks:
#   1. Spread filter  — reject if spread > max_spread_pct
#   2. Imbalance filter — reject if book doesn't confirm signal direction
#
# If rejected → signal is SKIPPED but cooldown IS consumed (by caller)
# so the engine doesn't re-enter on the same candle.

from dataclasses import dataclass
from typing import Optional
from live_engine.order_book import OrderBookSnapshot


@dataclass
class LiquidityResult:
    passed:        bool
    reason:        str          # human-readable reason for pass/fail
    spread_pct:    Optional[float] = None
    imbalance:     Optional[float] = None
    best_bid:      Optional[float] = None
    best_ask:      Optional[float] = None
    entry_price:   Optional[float] = None   # bid or ask to use as limit price


class LiquidityFilter:
    """
    Validates order book conditions for a signal.

    Config params (all from CONFIG):
        ob_spread_max_pct   : float  = 0.0005  (0.05%)
        ob_imbalance_levels : int    = 5
        ob_imbalance_min    : float  = 0.55    (BUY needs >= this)
        ob_imbalance_max    : float  = 0.45    (SELL needs <= this)
    """

    def __init__(self,
                 spread_max_pct:    float = 0.0005,
                 imbalance_levels:  int   = 5,
                 imbalance_min:     float = 0.55,
                 imbalance_max:     float = 0.45):
        self.spread_max_pct   = spread_max_pct
        self.imbalance_levels = imbalance_levels
        self.imbalance_min    = imbalance_min
        self.imbalance_max    = imbalance_max

    def check(self, side: str, snapshot: OrderBookSnapshot) -> LiquidityResult:
        """
        Run liquidity checks for a signal.

        Args:
            side     : 'BUY' or 'SELL'
            snapshot : current order book snapshot

        Returns:
            LiquidityResult with passed=True/False and reason
        """

        # ── Guard: book must be valid ──────────────────────────────────────
        if not snapshot.is_valid():
            return LiquidityResult(
                passed=False,
                reason="No order book data available"
            )

        spread_pct = snapshot.spread_pct
        imbalance  = snapshot.imbalance(self.imbalance_levels)
        best_bid   = snapshot.best_bid
        best_ask   = snapshot.best_ask

        # ── Check 1: Spread ────────────────────────────────────────────────
        if spread_pct is not None and spread_pct > self.spread_max_pct:
            return LiquidityResult(
                passed     = False,
                reason     = f"Spread too wide: {spread_pct*100:.3f}% > {self.spread_max_pct*100:.3f}%",
                spread_pct = spread_pct,
                imbalance  = imbalance,
                best_bid   = best_bid,
                best_ask   = best_ask,
            )

        # ── Check 2: Imbalance ─────────────────────────────────────────────
        if imbalance is not None:
            if side == 'BUY' and imbalance < self.imbalance_min:
                return LiquidityResult(
                    passed     = False,
                    reason     = f"Insufficient bid pressure: imbalance={imbalance:.3f} < {self.imbalance_min}",
                    spread_pct = spread_pct,
                    imbalance  = imbalance,
                    best_bid   = best_bid,
                    best_ask   = best_ask,
                )
            if side == 'SELL' and imbalance > self.imbalance_max:
                return LiquidityResult(
                    passed     = False,
                    reason     = f"Insufficient ask pressure: imbalance={imbalance:.3f} > {self.imbalance_max}",
                    spread_pct = spread_pct,
                    imbalance  = imbalance,
                    best_bid   = best_bid,
                    best_ask   = best_ask,
                )

        # ── All checks passed — set limit entry price ──────────────────────
        # BUY  → limit at best ask (paying what sellers want → fast fill)
        # SELL → limit at best bid (selling what buyers want → fast fill)
        entry_price = best_ask if side == 'BUY' else best_bid

        return LiquidityResult(
            passed      = True,
            reason      = f"OK — spread={spread_pct*100:.3f}% imbalance={imbalance:.3f}" if spread_pct and imbalance else "OK",
            spread_pct  = spread_pct,
            imbalance   = imbalance,
            best_bid    = best_bid,
            best_ask    = best_ask,
            entry_price = entry_price,
        )
