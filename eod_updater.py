# eod_updater.py
#
# End-of-Day CSV updater.
#
# Runs as a background daemon thread.
# At 3:31 PM every trading day, fetches today's completed 1min
# candles from Upstox intraday API and appends them to each
# symbol's CSV file.
#
# Notifications:
#   - Prints detailed log to terminal/Railway logs
#   - Sends Telegram summary if bot token configured
#
# Flow:
#   3:31 PM → fetch intraday candles for all symbols
#           → deduplicate against existing CSV
#           → append new rows
#           → log result + send Telegram summary

import os
import time
import threading
import requests
import pandas as pd
from datetime import datetime, timedelta

from config import CONFIG, INSTRUMENTS

DATA_DIR  = os.environ.get("CSV_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "csvdata"))
NSE_OPEN  = "09:15"
NSE_CLOSE = "15:30"


def _fetch_intraday(token: str, access_token: str) -> pd.DataFrame | None:
    """
    Fetch today's completed 1min candles from Upstox intraday API.
    Returns DataFrame [open,high,low,close,volume] or None.
    """
    try:
        url = (
            f"https://api.upstox.com/v2/historical-candle/intraday/"
            f"{token.replace('|', '%7C')}/1minute"
        )
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=15,
        )
        if not resp.ok:
            return None
        candles = resp.json().get("data", {}).get("candles", [])
        if not candles:
            return None
        df = pd.DataFrame(
            candles,
            columns=["datetime", "open", "high", "low", "close", "volume", "oi"],
        )
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime").sort_index()
        df = df.between_time(NSE_OPEN, NSE_CLOSE)
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        print(f"[EOD] Fetch error for {token}: {e}")
        return None


def run_eod_update() -> dict:
    """
    Fetch today's candles for all symbols and append to CSVs.
    Returns summary dict {ok: [...], failed: [...], skipped: [...]}.
    """
    access_token = CONFIG.get("upstox_access_token", "")
    if not access_token:
        print("[EOD] ⚠ No access token — skipping EOD update")
        return {"ok": [], "failed": [], "skipped": [], "error": "No token"}

    today     = datetime.now().date()
    os.makedirs(DATA_DIR, exist_ok=True)

    ok, failed, skipped = [], [], []
    total_rows_added = 0
    csv_end_dates = {}

    print(f"\n{'='*60}")
    print(f"[EOD] 📥 Starting EOD CSV update — {today}")
    print(f"{'='*60}")

    for inst in INSTRUMENTS:
        sym   = inst["symbol"]
        token = inst["token"]
        csv   = os.path.join(DATA_DIR, f"{sym}_1YEAR_1MIN.csv")

        df_new = _fetch_intraday(token, access_token)
        if df_new is None or df_new.empty:
            print(f"  [{sym:12}] ❌ No intraday data returned")
            failed.append(sym)
            time.sleep(0.3)
            continue

        df_new = df_new[df_new.index.date == today]

        if df_new.empty:
            print(f"  [{sym:12}] ⏭ No rows for today in response")
            skipped.append(sym)
            time.sleep(0.3)
            continue

        try:
            if os.path.exists(csv):
                existing = pd.read_csv(csv, index_col=0, parse_dates=True)
                prev_end = existing.index[-1] if len(existing) else "—"
                combined = pd.concat([existing, df_new])
            else:
                prev_end = "—"
                combined = df_new

            combined = combined[~combined.index.duplicated(keep="last")]
            combined.sort_index(inplace=True)
            combined.to_csv(csv)

            new_rows = len(df_new)
            total_rows_added += new_rows
            last_ts  = combined.index[-1]
            csv_end_dates[sym] = str(last_ts)[:16]
            print(f"  [{sym:12}] ✅ +{new_rows:3d} rows  |  {str(prev_end)[:16]} → {str(last_ts)[:16]}")
            ok.append(sym)

        except Exception as e:
            print(f"  [{sym:12}] ❌ CSV write error: {e}")
            failed.append(sym)

        time.sleep(0.3)

    print(f"\n[EOD] {'='*56}")
    print(f"[EOD] ✅ Updated : {len(ok):2d} symbols  (+{total_rows_added} rows total)")
    if failed:
        print(f"[EOD] ❌ Failed  : {len(failed):2d} symbols  → {', '.join(failed)}")
    if skipped:
        print(f"[EOD] ⏭ Skipped : {len(skipped):2d} symbols  → {', '.join(skipped)}")
    print(f"[EOD] CSVs are now current through {today} market close")
    print(f"[EOD] {'='*56}\n")

    _send_telegram_summary(today, ok, failed, skipped, total_rows_added, csv_end_dates)

    return {"ok": ok, "failed": failed, "skipped": skipped, "rows_added": total_rows_added}


def _send_telegram_summary(today, ok, failed, skipped, rows, csv_end_dates):
    """Telegram notification after EOD update."""
    try:
        bot  = CONFIG.get("telegram_bot_token", "")
        chat = CONFIG.get("telegram_chat_id", "")
        if not bot or not chat:
            return

        status = "✅" if not failed else "⚠️"
        lines = [
            f"{status} <b>EOD CSV Update — {today}</b>",
            f"",
            f"📥 Updated: <b>{len(ok)} symbols</b> (+{rows} rows)",
        ]

        if failed:
            lines.append(f"❌ Failed: {', '.join(failed)}")
        if skipped:
            lines.append(f"⏭ Skipped: {', '.join(skipped)}")

        # Show a few key symbols' end dates
        key_syms = ["TRENT", "CUMMINSIND", "OFSS", "PERSISTENT", "HCLTECH"]
        date_lines = []
        for s in key_syms:
            if s in csv_end_dates:
                date_lines.append(f"  {s}: {csv_end_dates[s]}")
        if date_lines:
            lines.append(f"\n📅 CSV ends at:")
            lines.extend(date_lines)

        lines.append(f"\n⏰ Next update tomorrow at 15:31")

        msg = "\n".join(lines)
        requests.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
        print(f"[EOD] Telegram notification sent ✅")
    except Exception as e:
        print(f"[EOD] Telegram send failed: {e}")


def start_eod_scheduler():
    """
    Background daemon thread.
    Waits until 3:31 PM, runs EOD update, then waits until next 3:31 PM.
    """
    def _loop():
        print("[EOD] Scheduler started — will update CSVs daily at 15:31 IST")
        while True:
            now      = datetime.now()
            next_run = now.replace(hour=11, minute=31, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)

            wait = (next_run - datetime.now()).total_seconds()
            print(f"[EOD] Next CSV update at {next_run.strftime('%Y-%m-%d %H:%M')} "
                  f"({wait/3600:.1f}h from now)")
            time.sleep(max(wait, 0))

            if datetime.now().weekday() >= 5:
                print("[EOD] Weekend — skipping EOD update")
                time.sleep(60)
                continue

            run_eod_update()
            time.sleep(90)  # prevent double-fire within same minute

    t = threading.Thread(target=_loop, daemon=True, name="EODUpdater")
    t.start()
    return t
