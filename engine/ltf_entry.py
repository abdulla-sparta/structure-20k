# engine/ltf_entry.py
# rr_target passed at instantiation — never reads global CONFIG.
# This makes parallel backtest runs thread-safe.

MIN_STOP_PCT = 0.003   # 0.3% of price — stops tighter than this are rejected

class LTFEntry:
    def __init__(self, rr_target: float = None, min_stop_pct: float = MIN_STOP_PCT):
        self._rr          = rr_target   # set by TradeEngine; None = read from CONFIG at signal time
        self._min_stop_pct = min_stop_pct
        self.prev_candle  = None

    def set_rr(self, rr: float):
        self._rr = rr

    def _rr_target(self) -> float:
        if self._rr is not None:
            return self._rr
        from config import CONFIG
        return CONFIG["rr_target"]

    def update(self, candle, bias):
        if bias is None or self.prev_candle is None:
            self.prev_candle = candle
            return None

        close  = candle["close"]
        rr     = self._rr_target()
        signal = None

        if bias == "BEARISH" and close < self.prev_candle["low"]:
            entry    = close
            stop_raw = self.prev_candle["high"]
            risk_raw = stop_raw - entry

            # Enforce minimum stop distance
            min_risk = entry * self._min_stop_pct
            if risk_raw < min_risk:
                # Widen stop to minimum — keeps entry/target valid
                risk_raw = min_risk
                stop_raw = round(entry + risk_raw, 2)

            if risk_raw > 0:
                signal = {
                    "side":   "SELL",
                    "entry":  entry,
                    "stop":   round(stop_raw, 2),
                    "target": round(entry - risk_raw * rr, 2),
                }

        elif bias == "BULLISH" and close > self.prev_candle["high"]:
            entry    = close
            stop_raw = self.prev_candle["low"]
            risk_raw = entry - stop_raw

            # Enforce minimum stop distance
            min_risk = entry * self._min_stop_pct
            if risk_raw < min_risk:
                risk_raw = min_risk
                stop_raw = round(entry - risk_raw, 2)

            if risk_raw > 0:
                signal = {
                    "side":   "BUY",
                    "entry":  entry,
                    "stop":   round(stop_raw, 2),
                    "target": round(entry + risk_raw * rr, 2),
                }

        self.prev_candle = candle
        return signal

    def reset(self):
        self.prev_candle = None