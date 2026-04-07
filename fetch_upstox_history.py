#!/usr/bin/env python3
# fetch_upstox_history.py
#
# Fetches 1-minute OHLC from Upstox v2 API for all INSTRUMENTS.
#
# IMPORTANT — Upstox API hard limit for 1-min candles is ~200 days.
# To build a full year of data you must run this script periodically
# (monthly). Each run fetches new data and APPENDS it to the existing
# CSV — history grows over time.
#
# CSV filename: data/SYMBOL_1YEAR_1MIN.csv
#
# Strategy:
#   - CSV exists → fetch only from (last_date + 1 day) to today → append
#   - CSV missing → fetch up to FETCH_MONTHS months back (API max ~6m)
#
# Usage:
#   python fetch_upstox_history.py               # all symbols (smart append)
#   python fetch_upstox_history.py --symbol TCS  # single symbol
#   python fetch_upstox_history.py --force       # re-fetch full history

import os
import time
import argparse
import requests
import pandas as pd
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

from config import CONFIG, INSTRUMENTS

NSE_OPEN     = "09:15"
NSE_CLOSE    = "15:30"
DATA_DIR     = os.environ.get("CSV_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "csvdata"))
INTERVAL     = "1minute"
FETCH_MONTHS = 12       # rolling 1-year window — fresh deploy rebuilds full year


def fetch_chunk(token: str, start: datetime, end: datetime,
                headers: dict) -> pd.DataFrame | None:
    """Fetch one chunk (<=30 days) of 1min candles from Upstox v2 API."""
    url = (
        f"https://api.upstox.com/v2/historical-candle/"
        f"{token.replace('|', '%7C')}/"
        f"{INTERVAL}/"
        f"{end.strftime('%Y-%m-%d')}/"
        f"{start.strftime('%Y-%m-%d')}"
    )
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if not resp.ok:
            print(f"    Warning HTTP {resp.status_code}: {resp.text[:120]}")
            return None
        candles = resp.json().get("data", {}).get("candles", [])
        if not candles:
            return None
        df = pd.DataFrame(
            candles,
            columns=["datetime", "open", "high", "low", "close", "volume", "oi"],
        )
        df["datetime"] = pd.to_datetime(df["datetime"])
        df.set_index("datetime", inplace=True)
        df.sort_index(inplace=True)
        df = df.between_time(NSE_OPEN, NSE_CLOSE)
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        print(f"    Exception: {e}")
        return None


def csv_path(symbol: str) -> str:
    return os.path.join(DATA_DIR, f"{symbol}_1YEAR_1MIN.csv")


def csv_exists(symbol: str) -> bool:
    p = csv_path(symbol)
    return os.path.exists(p) and os.path.getsize(p) > 1024

# keep MONTHS accessible for app.py import
MONTHS = FETCH_MONTHS


def _load_existing(symbol: str):
    try:
        df = pd.read_csv(csv_path(symbol), index_col=0, parse_dates=True)
        return df if not df.empty else None
    except Exception:
        return None


def fetch_symbol(symbol: str, token: str, force: bool = False, months: int = None) -> bool:
    """
    Smart fetch for one symbol:
      - CSV exists and not force -> fetch only NEW data since last row, append
      - CSV missing or force     -> fetch up to FETCH_MONTHS back from today
    Returns True on success.
    """
    headers = {
        "Authorization": f"Bearer {CONFIG['upstox_access_token']}",
        "Accept": "application/json",
    }
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    existing_df = _load_existing(symbol) if not force else None

    if existing_df is not None:
        last_ts    = pd.Timestamp(existing_df.index[-1])
        last_ts_naive = last_ts.tz_localize(None) if last_ts.tzinfo is None else last_ts.tz_convert(None)
        fetch_from = (last_ts_naive + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        if fetch_from.date() >= today.date():
            print(f"  [{symbol}] Already up to date ({last_ts.date()})")
            return True
        months_needed = max(1, int((today - fetch_from).days / 30) + 1)
        start_date = fetch_from
        print(f"  [{symbol}] Existing ends {last_ts.date()} — "
              f"appending {(today - fetch_from).days} new days...")
    else:
        months_needed = months if months else FETCH_MONTHS
        start_date    = today - relativedelta(months=months_needed)
        label = "Force re-fetch" if force else "Fresh fetch"
        print(f"  [{symbol}] {label} — {months_needed} months...")

    all_frames = []
    for m in range(months_needed):
        chunk_end_m   = today - relativedelta(months=m)
        chunk_start_m = chunk_end_m - relativedelta(months=1) + timedelta(days=1)
        if chunk_end_m.date() < start_date.date():
            break
        if chunk_start_m < start_date:
            chunk_start_m = start_date

        print(f"    Chunk {m+1:>2}: {chunk_start_m.date()} -> {chunk_end_m.date()}", end=" ")
        df = fetch_chunk(token, chunk_start_m, chunk_end_m, headers)
        if df is not None and not df.empty:
            all_frames.append(df)
            print(f"ok {len(df):,} rows")
        else:
            print("(no data)")
        time.sleep(0.4)

    if not all_frames:
        if existing_df is not None:
            print(f"  [{symbol}] No new data from API — keeping existing CSV")
            return True
        print(f"  [{symbol}] No data fetched.")
        return False

    new_df = pd.concat(all_frames)
    combined = pd.concat([existing_df, new_df]) if existing_df is not None else new_df
    combined = combined[~combined.index.duplicated(keep="last")]
    combined.sort_index(inplace=True)

    os.makedirs(DATA_DIR, exist_ok=True)
    combined.to_csv(csv_path(symbol))

    span = (combined.index[-1] - combined.index[0]).days
    print(f"  [{symbol}] {len(combined):,} candles total "
          f"({combined.index[0].date()} -> {combined.index[-1].date()}, "
          f"{span}d / {span/30:.1f}m) saved.")
    return True


def fetch_all(symbols: list[str] = None, force: bool = False):
    targets = [i for i in INSTRUMENTS
               if symbols is None or i["symbol"] in symbols]
    if not targets:
        print("No matching symbols found.")
        return

    mode = "FORCE RE-FETCH" if force else "SMART APPEND"
    print(f"\n[{mode}] 1-min history for {len(targets)} symbols\n")
    ok, fail = [], []
    for inst in targets:
        success = fetch_symbol(inst["symbol"], inst["token"], force=force)
        (ok if success else fail).append(inst["symbol"])
        time.sleep(1)

    print(f"\n{'='*55}")
    print(f"Success ({len(ok)}): {ok}")
    if fail:
        print(f"Failed  ({len(fail)}): {fail}")
    print("\nTip: Run monthly — history grows with each run.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch Upstox 1-min history (appends to existing CSV)")
    parser.add_argument("--symbol", nargs="+", default=None)
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch full history even if CSV exists")
    args = parser.parse_args()
    fetch_all(symbols=args.symbol, force=args.force)