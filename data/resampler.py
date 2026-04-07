# data/resampler.py
#
# NSE market hours: Monday–Friday, 09:15 IST – 15:30 IST
# 1-minute candles in this window = 375 candles/day
# After resampling to 5m  → 75 candles/day  (9:15–15:25)
# After resampling to 15m → 25 candles/day  (9:15–15:15)
#
# The between_time() filter is a safety guard so the resampler is
# correct even if the input CSV contains pre-market / post-market rows
# (e.g. 9:00, 15:31 etc.). For CSVs that already contain only market
# hours data this filter is a no-op.

import pandas as pd

NSE_OPEN  = "09:15"
NSE_CLOSE = "15:30"


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """
    Resample 1-minute NSE OHLC data to N-minute candles.

    Steps:
      1. Strip any rows outside 09:15–15:30 IST (pre/post market)
      2. Resample with origin="start_day" so candles anchor at 09:15
         (not midnight), giving correct 09:15, 09:20, 09:25… grid
      3. Drop NaN rows (overnight/weekend gaps)
      4. Cap at 15:25 so no partial candle starting at 15:30

    Parameters
    ----------
    df      : DataFrame with DatetimeIndex, columns open/high/low/close
    minutes : target candle size (5, 15, 30, etc.)

    Returns
    -------
    DataFrame resampled to N-minute candles, market hours only
    """
    # 1. Market hours only
    df_mkt = df.between_time(NSE_OPEN, NSE_CLOSE)

    # 2. Resample anchored to start of each day (gives 09:15, 09:20 … grid)
    resampled = df_mkt.resample(f"{minutes}min", origin="start_day").agg({
        "open":  "first",
        "high":  "max",
        "low":   "min",
        "close": "last",
    }).dropna()

    # 3. Final trim: cap at 15:25 (drop 15:30 partial candle)
    resampled = resampled[resampled.index.time <= pd.Timestamp("15:25").time()]

    return resampled
