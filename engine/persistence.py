# engine/persistence.py
#
# Trade log stored in Postgres (Railway) or SQLite (local) via db.py.
# Replaces reports/daily/{symbol}_{date}.csv files.
# CSV export still available for download if needed.

import sys
import os
import csv
import io
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import db


def save_daily_trades(trade_log: list, symbol: str, date_str: str = None):
    """Save closed trades to DB. Called by scheduler at end of session."""
    if not trade_log:
        return
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    for trade in trade_log:
        db.save_trade(symbol, trade, date_str)


def load_daily_trades(symbol: str, date_str: str) -> list:
    """Load trades for a symbol on a given date."""
    return db.get_trades(symbol, date_str)


def load_all_trades(symbol: str) -> list:
    """Load all historical trades for a symbol."""
    return db.get_trades(symbol)


def export_csv(symbol: str, date_str: str = None) -> str:
    """Export trades as CSV string (for download endpoint)."""
    trades = db.get_trades(symbol, date_str)
    if not trades:
        return ""
    fields = ["direction", "entry_time", "exit_time", "entry_price",
              "exit_price", "qty", "gross_pnl", "charges", "net_pnl", "reason"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(trades)
    return buf.getvalue()
