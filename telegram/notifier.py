# telegram/notifier.py

import requests


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> bool:
    if not bot_token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.ok
    except Exception as e:
        print(f"Telegram send error: {e}")
        return False


def send_telegram_summary(bot_token: str, chat_id: str, report: dict) -> bool:
    lines = [
        f"📊 <b>Daily Report — {report.get('symbol', '')}</b>",
        f"Date: {report.get('date', '')}",
        f"Trades: {report.get('trades', 0)} | Win Rate: {report.get('win_rate', 0)}%",
        f"Gross PnL: ₹{report.get('gross', 0):,.2f}",
        f"Charges:   ₹{report.get('charges', 0):,.2f}",
        f"Net PnL:   ₹{report.get('net', 0):,.2f}",
        f"Balance:   ₹{report.get('balance', 0):,.2f}",
    ]
    return send_telegram_message(bot_token, chat_id, "\n".join(lines))


def send_telegram_image(bot_token: str, chat_id: str,
                        image_path: str, caption: str = "") -> bool:
    if not bot_token or not chat_id:
        return False
    try:
        with open(image_path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=20,
            )
        return r.ok
    except Exception as e:
        print(f"Telegram image error: {e}")
        return False
