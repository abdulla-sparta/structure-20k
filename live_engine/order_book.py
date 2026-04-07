# live_engine/order_book.py
#
# Stores and updates 20-level order book depth per symbol.
# Updated on every full-feed tick from UpstoxV3Client.
# Thread-safe — multiple threads read while WS thread writes.

import threading
from dataclasses import dataclass, field
from typing import List, Tuple, Optional


@dataclass
class OrderBookSnapshot:
    """Immutable snapshot of order book at a point in time."""
    symbol:    str
    bids:      List[Tuple[float, int]]   # [(price, qty), ...] best first
    asks:      List[Tuple[float, int]]   # [(price, qty), ...] best first
    timestamp: float = 0.0               # unix time of snapshot

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        mid = self.mid_price
        sp  = self.spread
        if mid and sp and mid > 0:
            return sp / mid
        return None

    def bid_depth(self, levels: int = 5) -> int:
        """Total bid quantity across top N levels."""
        return sum(qty for _, qty in self.bids[:levels])

    def ask_depth(self, levels: int = 5) -> int:
        """Total ask quantity across top N levels."""
        return sum(qty for _, qty in self.asks[:levels])

    def imbalance(self, levels: int = 5) -> Optional[float]:
        """
        Bid imbalance ratio = bid_depth / (bid_depth + ask_depth).
        > 0.55 = buy pressure, < 0.45 = sell pressure.
        Returns None if no depth available.
        """
        bd = self.bid_depth(levels)
        ad = self.ask_depth(levels)
        total = bd + ad
        if total == 0:
            return None
        return bd / total

    def is_valid(self) -> bool:
        return bool(self.bids and self.asks and self.best_bid and self.best_ask)


class OrderBook:
    """
    Per-symbol order book. Updated from WS ticks.
    Provides thread-safe snapshot access.
    """

    def __init__(self, symbol: str):
        self.symbol   = symbol
        self._lock    = threading.Lock()
        self._bids:   List[Tuple[float, int]] = []
        self._asks:   List[Tuple[float, int]] = []
        self._ts:     float = 0.0

    def update(self, bids: List[Tuple[float, int]],
                     asks: List[Tuple[float, int]],
                     timestamp: float = 0.0):
        """Update book from WS tick. Called from WS thread."""
        with self._lock:
            # Sort: bids descending (best bid first), asks ascending (best ask first)
            self._bids = sorted(bids, key=lambda x: x[0], reverse=True)[:20]
            self._asks = sorted(asks, key=lambda x: x[0])[:20]
            self._ts   = timestamp

    def snapshot(self) -> OrderBookSnapshot:
        """Return immutable snapshot. Safe to call from any thread."""
        with self._lock:
            return OrderBookSnapshot(
                symbol    = self.symbol,
                bids      = list(self._bids),
                asks      = list(self._asks),
                timestamp = self._ts,
            )

    def is_ready(self) -> bool:
        """True if we have at least 1 level of depth."""
        with self._lock:
            return bool(self._bids and self._asks)


class OrderBookRegistry:
    """
    Global registry — one OrderBook per symbol.
    MSR registers symbols, WS tick handler calls update().
    """

    def __init__(self):
        self._books: dict[str, OrderBook] = {}
        self._lock  = threading.Lock()

    def register(self, symbol: str) -> OrderBook:
        with self._lock:
            if symbol not in self._books:
                self._books[symbol] = OrderBook(symbol)
            return self._books[symbol]

    def get(self, symbol: str) -> Optional[OrderBook]:
        return self._books.get(symbol)

    def update_from_tick(self, symbol: str,
                         bids: List[Tuple[float, int]],
                         asks: List[Tuple[float, int]],
                         timestamp: float = 0.0):
        """Called from WS tick — updates book if registered."""
        book = self._books.get(symbol)
        if book:
            book.update(bids, asks, timestamp)

    def all_snapshots(self) -> dict:
        return {sym: book.snapshot() for sym, book in self._books.items()}
