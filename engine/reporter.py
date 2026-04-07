# engine/reporter.py

import os
import csv
from datetime import datetime


def generate_daily_report(broker, symbol: str) -> dict:
    """Generate daily report dict and save to reports/daily/"""

    today      = datetime.now().strftime("%Y-%m-%d")
    trades     = broker.trade_log
    wins       = [t for t in trades if t["net_pnl"] > 0]
    losses     = [t for t in trades if t["net_pnl"] <= 0]
    win_rate   = round(len(wins) / len(trades) * 100, 1) if trades else 0.0

    report = {
        "date":      today,
        "symbol":    symbol,
        "trades":    len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  win_rate,
        "gross":     round(broker.total_gross_pnl, 2),
        "charges":   round(broker.total_charges, 2),
        "net":       round(broker.total_net_pnl, 2),
        "balance":   round(broker.balance, 2),
    }

    # Save CSV
    folder = os.path.join("reports", "daily")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{symbol}_{today}.csv")

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        for k, v in report.items():
            w.writerow([k, v])

    return report
