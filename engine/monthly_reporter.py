# engine/monthly_reporter.py
# Aggregates daily CSV reports into a monthly summary.
# Generates monthly equity curve PNG and summary dict.
# Called by DailyScheduler on the last trading day of each month.

import os
import csv
import calendar
from datetime import date, datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def is_last_trading_day(today: date) -> bool:
    """
    Returns True if today is the last weekday of the current month.
    (Simple heuristic — doesn't account for NSE holidays.)
    """
    last_day = calendar.monthrange(today.year, today.month)[1]
    for d in range(last_day, last_day - 5, -1):
        candidate = date(today.year, today.month, d)
        if candidate.weekday() < 5:   # Mon–Fri
            return today == candidate
    return False


def generate_monthly_report(symbol: str) -> dict | None:
    """
    Read all daily CSV files for this month, aggregate into monthly stats.
    Returns summary dict or None if no data found.
    """
    month_str  = datetime.now().strftime("%Y-%m")
    folder     = os.path.join("reports", "daily")

    if not os.path.exists(folder):
        return None

    daily_files = sorted([
        f for f in os.listdir(folder)
        if f.startswith(symbol) and month_str in f and f.endswith(".csv")
    ])

    if not daily_files:
        return None

    total_gross  = 0.0
    total_charges= 0.0
    total_net    = 0.0
    total_trades = 0
    total_wins   = 0

    for fname in daily_files:
        path = os.path.join(folder, fname)
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
            for r in rows:
                gross   = float(r.get("gross_pnl", 0) or 0)
                charges = float(r.get("charges",   0) or 0)
                net     = float(r.get("net_pnl",   0) or 0)
                total_gross   += gross
                total_charges += charges
                total_net     += net
                total_trades  += 1
                if net > 0:
                    total_wins += 1

    win_rate = round(total_wins / total_trades * 100, 1) if total_trades else 0.0

    return {
        "month":    month_str,
        "symbol":   symbol,
        "gross":    round(total_gross, 2),
        "charges":  round(total_charges, 2),
        "net":      round(total_net, 2),
        "trades":   total_trades,
        "wins":     total_wins,
        "win_rate": win_rate,
    }


def generate_monthly_equity_chart(symbol: str,
                                   starting_balance: float = 200000) -> str | None:
    """
    Build cumulative equity curve from daily CSVs.
    Saves PNG to reports/ and returns path.
    """
    month_str  = datetime.now().strftime("%Y-%m")
    folder     = os.path.join("reports", "daily")

    if not os.path.exists(folder):
        return None

    daily_files = sorted([
        f for f in os.listdir(folder)
        if f.startswith(symbol) and month_str in f and f.endswith(".csv")
    ])

    if not daily_files:
        return None

    dates   = []
    equity  = []
    balance = starting_balance

    for fname in daily_files:
        day_net = 0.0
        path    = os.path.join(folder, fname)
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                day_net += float(row.get("net_pnl", 0) or 0)
        balance += day_net
        dates.append(fname.replace(f"{symbol}_", "").replace(".csv", ""))
        equity.append(round(balance, 2))

    # Plot
    fig, ax = plt.subplots(figsize=(10, 4))
    color   = "#16a34a" if equity[-1] >= starting_balance else "#dc2626"
    ax.plot(dates, equity, color=color, linewidth=2)
    ax.fill_between(range(len(equity)), equity, starting_balance,
                    alpha=0.1, color=color)
    ax.axhline(starting_balance, color="#9ca3af", linewidth=0.8, linestyle="--")
    ax.set_title(f"Monthly Equity — {symbol} ({month_str})", fontsize=12, pad=10)
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity (₹)")
    ax.tick_params(axis="x", rotation=45, labelsize=8)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))
    plt.tight_layout()

    os.makedirs("reports", exist_ok=True)
    img_path = os.path.join("reports", f"monthly_equity_{symbol}_{month_str}.png")
    plt.savefig(img_path, dpi=120)
    plt.close()

    return img_path
