# data/loader.py

import pandas as pd
import os


def load_csv(path: str) -> pd.DataFrame:
    """
    Load a 1-minute OHLC CSV.
    Expects columns: time, open, high, low, close (case-insensitive).
    Sets DatetimeIndex.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Data file not found: {path}")

    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]

    time_col = next((c for c in df.columns if "time" in c or "date" in c), None)
    if time_col is None:
        raise ValueError("No time/date column found in CSV.")

    df[time_col] = pd.to_datetime(df[time_col])
    df.set_index(time_col, inplace=True)
    df.sort_index(inplace=True)

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            raise ValueError(f"Missing column: {col}")

    return df[["open", "high", "low", "close"]]
