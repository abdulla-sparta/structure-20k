# StructureEngine

Fully autonomous intraday paper trading engine for Indian equities (NSE).
Runs 28 Nifty50 symbols on a 15m bias + 5m entry structure strategy.
Deployed on Railway — starts at 9:00 AM, stops at 3:31 PM, zero manual intervention.

---

## Features

- **Autonomous** — cron auto-starts at 09:00 IST, auto-stops at 15:31 IST every weekday
- **Live dashboard** — heatmap, position cards, signal alerts, trade log
- **Telegram alerts** — entry, exit, EOD CSV update, kill switch, daily login reminder
- **Persistent history** — CSVs on Railway volume, EOD appends daily at 15:31
- **Backtest dashboard** — run any symbol/preset, compare results
- **Paper trading** — risk-managed qty sizing, 5x leverage cap, per-symbol kill switch

---

## Local Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # add Upstox API key/secret + Telegram tokens
python fetch_upstox_history.py  # fetch 12 months of 1min CSVs (~10 mins)
python app.py                  # open http://localhost:5000
```

---

## Railway Deployment

1. Push repo to GitHub
2. Railway → New Project → Deploy from GitHub
3. Add **PostgreSQL** plugin
4. Add **Volume** → mount at `/app/data`
5. Set env vars: `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `UPSTOX_REDIRECT_URI`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
6. Deploy → visit `/auth/login` once for OAuth
7. Engine runs autonomously from next weekday 09:00 IST

---

## Cron Schedule (railway.toml)

| IST | UTC | Action |
|---|---|---|
| 09:00 Mon–Fri | 03:30 | `/cron/start` → engine starts 🚀 |
| 15:31 Mon–Fri | 10:01 | `/cron/stop` → engine stops 🔴 |
| 15:32 Mon–Fri | 10:02 | `/eod_update` → CSVs appended ✅ |

---

## Telegram Alerts

🚀 Engine started · 🟢 Entry · ✅ Exit profit · ❌ Exit loss · ⏰ EOD exit · 🚨 Kill switch · ✅ EOD CSV · 🔴 Engine stopped · 🔐 Daily login reminder

---

## Best Config (from backtest)

T1 (risk=15%, cd=30): TRENT, CUMMINSIND, M&M  
T2 (risk=10%, cd=50): OFSS, HCLTECH, PERSISTENT, MPHASIS, BOSCHLTD, RELIANCE  
RR=5 · Net PNL: ₹10.82L · 8/9 profitable
