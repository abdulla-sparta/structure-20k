# live_engine/live_candle_builder.py
# Builds OHLC candles from a tick stream using time-bucketing.
# This is the correct approach for irregular tick intervals —
# NOT accumulating N ticks (which was the bug in the old live_runner.py).

import pandas as pd
from datetime import datetime


class LiveCandleBuilder:
    """
    Call update(price, timestamp) on every tick.
    Returns a closed candle Series when a new time bucket starts,
    otherwise returns None.

    Example:
        builder_5m  = LiveCandleBuilder(5)
        builder_15m = LiveCandleBuilder(15)

        def on_tick(tick):
            candle_5m  = builder_5m.update(tick["ltp"], tick["timestamp"])
            candle_15m = builder_15m.update(tick["ltp"], tick["timestamp"])
            if candle_15m: engine.on_htf_candle(candle_15m)
            if candle_5m:  engine.on_ltf_candle(candle_5m)
    """

    def __init__(self, timeframe_minutes: int):
        self.tf              = timeframe_minutes
        self.current_candle  = None
        self.current_bucket  = None

    def update(self, price: float, timestamp) -> pd.Series | None:
        """
        Returns closed candle as pd.Series (indexed by bucket time), or None.
        """
        dt     = pd.to_datetime(timestamp)
        bucket = dt.replace(
            minute=(dt.minute // self.tf) * self.tf,
            second=0,
            microsecond=0,
        )

        if self.current_bucket != bucket:
            closed = self._to_series(self.current_candle, self.current_bucket)

            self.current_bucket = bucket
            self.current_candle = {
                "open":  price,
                "high":  price,
                "low":   price,
                "close": price,
            }

            return closed  # None on very first tick

        # Update running candle
        c = self.current_candle
        c["high"]  = max(c["high"], price)
        c["low"]   = min(c["low"],  price)
        c["close"] = price

        return None

    def _to_series(self, candle: dict | None, bucket) -> pd.Series | None:
        if candle is None or bucket is None:
            return None
        return pd.Series(candle, name=bucket)

    def reset(self):
        self.current_candle = None
        self.current_bucket = None
