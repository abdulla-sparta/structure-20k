# engine/scheduler.py

from datetime import date
from engine.session import is_force_exit_time
from engine.reporter import generate_daily_report
from engine.persistence import save_daily_trades
from engine.monthly_reporter import (
    generate_monthly_report,
    generate_monthly_equity_chart,
    is_last_trading_day,
)
from config import CONFIG, INSTRUMENTS


def _is_sunday(today) -> bool:
    return today.weekday() == 6   # 0=Mon … 6=Sun


class DailyScheduler:
    """
    Called every LTF candle in live mode.
    Handles end-of-day force-exit, reporting, and daily stat reset.
    """

    def __init__(self, broker, symbol: str = ""):
        self.broker                  = broker
        self.symbol                  = symbol
        self.last_report_date        = None
        self.last_reclassify_sunday  = None   # track Sunday reclassification

    def check(self, candle_time, current_price: float):

        current_time = candle_time.time()
        today        = candle_time.date()

        # Force exit any open position at session end
        if is_force_exit_time(current_time) and self.broker.position:
            self.broker.force_close(price=current_price, time=candle_time)

        # Daily report — run exactly once per day
        if is_force_exit_time(current_time) and self.last_report_date != today:

            report = generate_daily_report(self.broker, self.symbol)

            # Persist trades to CSV
            save_daily_trades(
                trade_log=self.broker.trade_log,
                symbol=self.symbol,
                date_str=str(today),
            )

            if CONFIG.get("telegram_enabled"):
                try:
                    from telegram.notifier import send_telegram_summary
                    send_telegram_summary(
                        bot_token=CONFIG["telegram_bot_token"],
                        chat_id=CONFIG["telegram_chat_id"],
                        report=report,
                    )
                except Exception as e:
                    print(f"Telegram error: {e}")

            # Monthly report on last trading day of month
            if is_last_trading_day(today):
                monthly = generate_monthly_report(self.symbol)
                img     = generate_monthly_equity_chart(self.symbol)

                if monthly and CONFIG.get("telegram_enabled"):
                    try:
                        from telegram.notifier import send_telegram_summary, send_telegram_image
                        send_telegram_summary(
                            bot_token=CONFIG["telegram_bot_token"],
                            chat_id=CONFIG["telegram_chat_id"],
                            report={
                                "date":     monthly["month"],
                                "symbol":   monthly["symbol"],
                                "gross":    monthly["gross"],
                                "charges":  monthly["charges"],
                                "net":      monthly["net"],
                                "trades":   monthly["trades"],
                                "win_rate": monthly["win_rate"],
                                "balance":  self.broker.balance,
                            },
                        )
                        if img:
                            send_telegram_image(
                                bot_token=CONFIG["telegram_bot_token"],
                                chat_id=CONFIG["telegram_chat_id"],
                                image_path=img,
                                caption=f"📊 Monthly Equity — {self.symbol}",
                            )
                    except Exception as e:
                        print(f"Monthly Telegram error: {e}")

            self.broker.reset_daily_stats()
            self.last_report_date = today

        # ── Sunday reclassification ───────────────────────────────────────────
        # Every Sunday at 15:30 (after market) — re-run classification backtest
        # for all instruments so tier assignments stay current.
        # Only runs once per Sunday regardless of how many symbols are live.
        if (_is_sunday(today)
                and current_time.hour == 15
                and current_time.minute >= 30
                and self.last_reclassify_sunday != today):
            self.last_reclassify_sunday = today
            import threading
            def _reclassify():
                try:
                    from tier_classifier import reclassify_all
                    syms = [i["symbol"] for i in INSTRUMENTS]
                    print(f"[Scheduler] Sunday reclassification started for {len(syms)} symbols")
                    reclassify_all(syms)
                    print("[Scheduler] Sunday reclassification complete")
                except Exception as e:
                    print(f"[Scheduler] Reclassification error: {e}")
            threading.Thread(target=_reclassify, daemon=True).start()