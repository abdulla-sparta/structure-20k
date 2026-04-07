# engine/htf_structure.py
#
# Higher-Timeframe Structure Bias — Break of Structure (BOS) approach.
#
# ── WHY THE OLD LOGIC WAS BROKEN ─────────────────────────────────────────────
# Old code tracked ALL-TIME high/low as reference points.
# A stock in a year-long downtrend (e.g. TRENT 4400→3600) had last_high = 4400.
# A Jan-Feb recovery to 4300 never exceeded 4400 → BULLISH NEVER fired.
# Result: engine only ever took SELL trades regardless of trend direction.
#
# ── FIX: Break of Structure (BOS) with confirmed swing pivots ────────────────
# 1. Scan for confirmed swing highs/lows (local extremes with N candles each side)
# 2. BULLISH = close breaks ABOVE a confirmed swing high
# 3. BEARISH = close breaks BELOW a confirmed swing low
# 4. Reference consumed after each BOS — waits for next confirmed pivot
#
# ── PARAMETERS ────────────────────────────────────────────────────────────────
# pivot_left / pivot_right : candles on each side to confirm a swing pivot.
# Default 2/2 works well for 15m HTF (catches swings every ~1–3 hours).
# Increase to 3/3 for slower, less noisy bias changes.


class HTFStructure:

    def __init__(self, pivot_left: int = 3, pivot_right: int = 3):
        self.left  = pivot_left
        self.right = pivot_right

        self.bias            = None
        self.last_swing_high = None   # confirmed pivot high  → BOS trigger for BULLISH
        self.last_swing_low  = None   # confirmed pivot low   → BOS trigger for BEARISH

        self._buf            = []
        # Start before 0 so the scan range begins at idx=0 on first call,
        # preventing the first `left` candles from being permanently skipped.
        self._checked_up_to  = -(self.right + 1)

    # ── Public API ────────────────────────────────────────────────────────────

    def update(self, candle) -> str | None:
        """
        Call on each closed HTF candle.
        Returns current bias: "BULLISH" | "BEARISH" | None
        """
        c = {
            "high":  float(candle["high"]),
            "low":   float(candle["low"]),
            "close": float(candle.get("close", candle["high"])),
        }
        self._buf.append(c)
        n = len(self._buf)

        # ── Step 1: scan newly valid pivot positions ──────────────────────────
        # idx is valid when: idx >= left  (has enough left neighbours)
        #                    idx <  n - right  (has enough right neighbours)
        for idx in range(max(self.left, self._checked_up_to + 1), n - self.right):
            pivot = self._buf[idx]

            # Pivot HIGH — strictly higher than all neighbours
            if (all(pivot["high"] > self._buf[idx - i]["high"] for i in range(1, self.left + 1)) and
                    all(pivot["high"] > self._buf[idx + i]["high"] for i in range(1, self.right + 1))):
                self.last_swing_high = pivot["high"]

            # Pivot LOW — strictly lower than all neighbours
            if (all(pivot["low"] < self._buf[idx - i]["low"] for i in range(1, self.left + 1)) and
                    all(pivot["low"] < self._buf[idx + i]["low"] for i in range(1, self.right + 1))):
                self.last_swing_low = pivot["low"]

        self._checked_up_to = n - self.right - 1

        # ── Step 2: BOS check on current close ───────────────────────────────
        close = c["close"]

        if self.last_swing_high is not None and close > self.last_swing_high:
            self.bias            = "BULLISH"
            self.last_swing_high = None   # consumed — await next pivot

        elif self.last_swing_low is not None and close < self.last_swing_low:
            self.bias           = "BEARISH"
            self.last_swing_low = None    # consumed

        # Trim buffer (no need for full history)
        max_buf = (self.left + self.right + 1) * 50
        if len(self._buf) > max_buf:
            trim                 = max_buf // 2
            self._buf            = self._buf[-trim:]
            self._checked_up_to  = max(self.left, self._checked_up_to - trim)

        return self.bias

    def reset(self):
        self.bias            = None
        self.last_swing_high = None
        self.last_swing_low  = None
        self._buf            = []
        self._checked_up_to  = -(self.right + 1)