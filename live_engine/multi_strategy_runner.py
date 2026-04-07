# live_engine/multi_strategy_runner.py
# Owns one InstrumentRunner per symbol.
# Manages the single shared WebSocket client.
# Routes ticks to correct runner AND heatmap feed.

from config import CONFIG, INSTRUMENTS
from strategy_presets import load_locked_config
from live_engine.instrument_runner import InstrumentRunner
from live_engine.upstox_v3_client import UpstoxV3Client
import heatmap_feed
import os, pandas as pd
from datetime import date as _date, timedelta as _td


class MultiStrategyRunner:

    def __init__(self):
        # Switch to live mode so TradeEngine loads state from DB
        CONFIG["mode"] = "live"

        preset = load_locked_config()
        print(f"🔒 Live preset: {preset.get('name', 'Custom')}")

        # Load tier classifications from DB for per-symbol risk scaling
        try:
            from tier_classifier import get_all_tiers
            tier_map = get_all_tiers()  # {symbol: {"tier": 1|2|3, ...}}
        except Exception:
            tier_map = {}
        print(f"[MSR] Tier map loaded: {len(tier_map)} symbols")

        # ── Order book execution setup (must be before instruments loop) ──
        self._ob_enabled = CONFIG.get("order_book_enabled", False)
        if self._ob_enabled:
            from live_engine.order_book import OrderBookRegistry
            from live_engine.liquidity_filter import LiquidityFilter
            from live_engine.limit_order_manager import LimitOrderManager
            self._ob_registry = OrderBookRegistry()
            self._liq_filter  = LiquidityFilter(
                spread_max_pct   = CONFIG.get("ob_spread_max_pct",     0.0005),
                imbalance_levels = CONFIG.get("ob_imbalance_levels",   5),
                imbalance_min    = CONFIG.get("ob_imbalance_min",      0.55),
                imbalance_max    = CONFIG.get("ob_imbalance_max",      0.45),
            )
            self._lom = LimitOrderManager(
                fill_timeout_secs  = CONFIG.get("ob_fill_timeout_secs",  5),
                poll_interval_secs = CONFIG.get("ob_poll_interval_secs", 1.0),
            )
            print(f"[MSR] Order book execution ENABLED — "
                  f"spread<{CONFIG.get('ob_spread_max_pct',0.0005)*100:.3f}% "
                  f"imbal>{CONFIG.get('ob_imbalance_min',0.55)}")
        else:
            self._ob_registry = None
            self._liq_filter  = None
            self._lom         = None
            print("[MSR] Order book execution DISABLED — standard market fill")

        self.runners: dict[str, InstrumentRunner] = {}
        tokens: list[str] = []

        base_risk = float(preset.get("risk_per_trade", CONFIG["risk_per_trade"]))

        for inst in INSTRUMENTS:
            sym   = inst["symbol"]
            token = inst["token"]

            # Apply tier-based risk scaling
            tier_info = tier_map.get(sym, {})
            tier = tier_info.get("tier", 2)

            if tier == 3:
                print(f"  ✗ {sym} — T3 (skipped in live)")
                continue  # T3 symbols are skipped entirely
            elif tier == 1:
                sym_risk = base_risk * 1.5   # T1 gets 1.5x risk
            else:
                sym_risk = base_risk          # T2 gets base risk

            sym_preset = dict(preset)
            sym_preset["risk_per_trade"] = sym_risk

            runner = InstrumentRunner(
                symbol=sym,
                instrument_token=token,
                preset=sym_preset,
            )
            self.runners[token] = runner
            tokens.append(token)

            # Attach order book to runner if enabled
            if self._ob_enabled and self._ob_registry:
                ob = self._ob_registry.register(sym)
                runner.attach_order_book(ob, self._liq_filter, self._lom)

            print(f"  ▸ {sym} T{tier} risk={sym_risk:.1%} ({token})")

        self._daily_loss_limit = CONFIG.get("portfolio_daily_loss_limit", 50000)
        self._portfolio_halted = False

        # Single WebSocket for all tokens
        self.client = UpstoxV3Client(tokens=tokens)

        # Register per-token tick callbacks
        for token, runner in self.runners.items():
            self.client.on_tick(token, self._make_handler(runner))
        print(f"[MSR] Registered {len(self.runners)} per-token callbacks on client ObjId: {id(self.client)}")

        # Register heatmap as global listener — shares same WS, no second connection
        heatmap_feed.register_with_client(self.client)

        # Register OB registry with WS client so depth updates flow in
        if self._ob_enabled and self._ob_registry:
            # The WS client uses _get_ob_registry() internally — point it to ours
            # by pre-registering all tokens so update_from_tick() finds them
            for inst in INSTRUMENTS:
                sym   = inst["symbol"]
                token = inst["token"]
                # Register both key formats (| and :) to handle proto variations
                self._ob_registry.register(token)
                self._ob_registry.register(token.replace("|", ":"))
                # Also register by symbol name for convenience
                self._ob_registry.register(sym)
            # Share our registry with the global WS client registry
            import live_engine.upstox_v3_client as _vc
            _vc._ob_registry = self._ob_registry
            print(f"[MSR] OB registry shared with WS client — {len(list(self._ob_registry._books.keys()))} books registered")

    def _make_handler(self, runner: InstrumentRunner):
        def handler(ltp: float, prev_close=None, timestamp=None, volume=None, atp=None):
            if self._portfolio_halted:
                # Kill switch: block new entries but still process ticks for
                # open positions so SL/target/force-exit work correctly
                if not runner.broker.position:
                    return  # no position — skip entirely
            runner.on_tick(ltp=ltp, prev_close=prev_close, timestamp=timestamp,
                           volume=volume, atp=atp)
            if not self._portfolio_halted:
                self._check_portfolio_loss()
        return handler

    def _check_portfolio_loss(self):
        total = sum(r.broker.daily_net_pnl for r in self.runners.values())
        if total <= -abs(self._daily_loss_limit) and not self._portfolio_halted:
            self._portfolio_halted = True
            msg = (
                f"🚨 PORTFOLIO DAILY LOSS LIMIT HIT\n"
                f"Total loss: ₹{abs(total):,.0f}\n"
                f"Limit: ₹{self._daily_loss_limit:,.0f}\nAll instruments halted."
            )
            print(msg)
            try:
                from telegram.notifier import send_telegram_message
                send_telegram_message(CONFIG["telegram_bot_token"],
                                      CONFIG["telegram_chat_id"], msg)
            except Exception:
                pass

    def get_all_status(self) -> list[dict]:
        return [r.get_status() for r in self.runners.values()]

    def get_portfolio_equity(self) -> float:
        return sum(
            r.broker.get_equity(r.last_price or r.broker.balance)
            for r in self.runners.values()
        )

    def is_connected(self) -> bool:
        return self.client.is_connected()

    def apply_config(self, preset: dict):
        """Hot-swap config on all runners (called from /lock_config)."""
        for runner in self.runners.values():
            runner.broker.risk_per_trade = float(preset.get("risk_per_trade", runner.broker.risk_per_trade))
            runner.engine.cooldown_candles   = int(preset.get("cooldown", runner.engine.cooldown_candles))
            runner.engine.min_price_distance = float(preset.get("min_price_distance", runner.engine.min_price_distance))
            if runner.engine.ltf and hasattr(runner.engine.ltf, "set_rr"):
                runner.engine.ltf.set_rr(float(preset.get("rr_target", 3)))

    def _fetch_today_intraday(self, token: str) -> pd.DataFrame | None:
        """
        Fetch today's 1min intraday candles from Upstox API.
        Returns DataFrame with columns [open,high,low,close,volume] or None.
        Uses /v2/historical-candle/intraday/{token}/1minute
        """
        try:
            import requests as _req
            from config import CONFIG as _cfg
            access_token = _cfg.get("upstox_access_token", "")
            if not access_token:
                return None
            url = (
                f"https://api.upstox.com/v2/historical-candle/intraday/"
                f"{token.replace('|', '%7C')}/1minute"
            )
            resp = _req.get(
                url,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                timeout=15,
            )
            if not resp.ok:
                return None
            candles = resp.json().get("data", {}).get("candles", [])
            if not candles:
                return None
            df = pd.DataFrame(candles, columns=["datetime","open","high","low","close","volume","oi"])
            df["datetime"] = pd.to_datetime(df["datetime"])
            df = df.set_index("datetime").sort_index()
            return df[["open","high","low","close","volume"]]
        except Exception as e:
            return None

    def _replay_htf_history(self):
        """
        Two-phase startup replay:

        PHASE 1 — Historical CSV (up to yesterday):
            Feed last 200 x 15min candles from CSV into HTF engine.
            Establishes swing structure + bias from 6 months of data.

        PHASE 2 — Today's intraday gap-fill (9:15 AM → now):
            Fetch today's 1min candles from Upstox intraday API.
            Feed through BOTH HTF (15min) and LTF (5min) engines.
            Fills the gap between market open and server start time.
            Without this, any BOS that happened before you started
            the server would be completely missed.
        """
        today = _date.today()
        data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        replayed = 0

        for inst in INSTRUMENTS:
            sym   = inst["symbol"]
            token = inst["token"]
            runner = self.runners.get(token)
            if not runner:
                continue

            # ── PHASE 1: Historical CSV → HTF only ──────────────────────────
            csv_path = os.path.join(data_dir, f"{sym}_1YEAR_1MIN.csv")
            if os.path.exists(csv_path):
                try:
                    df = pd.read_csv(csv_path, parse_dates=["datetime"])
                    df = df.sort_values("datetime").set_index("datetime")
                    df15 = df["close"].resample("15min").ohlc().dropna()
                    df15.columns = ["open","high","low","close"]
                    df15["volume"] = df["volume"].resample("15min").sum()
                    cutoff = pd.Timestamp(today, tz=df15.index.tz)
                    hist = df15[df15.index < cutoff].tail(200)
                    for ts, row in hist.iterrows():
                        candle = pd.Series({
                            "open": row["open"], "high": row["high"],
                            "low":  row["low"],  "close": row["close"],
                            "volume": row["volume"],
                        }, name=ts)
                        runner.engine.on_htf_candle(candle)
                except Exception as e:
                    print(f"  ⚠ [{sym}] Phase 1 CSV replay failed: {e}")

            # ── PHASE 2: Today's intraday gap-fill → HTF + LTF ──────────────
            # Fetch 1min candles from 9:15 AM → now from Upstox API
            # replay_mode = True → engine processes candles but SKIPS order placement
            runner.engine.start_replay()
            df1m = self._fetch_today_intraday(token)
            if df1m is not None and len(df1m) > 0:
                # Build 15min candles for HTF
                df15 = df1m["close"].resample("15min").ohlc().dropna()
                df15.columns = ["open","high","low","close"]
                df15["volume"] = df1m["volume"].resample("15min").sum()

                # Build 5min candles for LTF
                df5 = df1m["close"].resample("5min").ohlc().dropna()
                df5.columns = ["open","high","low","close"]
                df5["volume"] = df1m["volume"].resample("5min").sum()

                # Merge and replay in chronological order (same as backtest logic)
                htf_list = list(df15.iterrows())
                htf_i    = 0
                for ltf_ts, ltf_row in df5.iterrows():
                    # Feed any HTF candles that closed before this LTF candle
                    while htf_i < len(htf_list) and htf_list[htf_i][0] <= ltf_ts:
                        ts, row = htf_list[htf_i]
                        runner.engine.on_htf_candle(pd.Series({
                            "open": row["open"], "high": row["high"],
                            "low":  row["low"],  "close": row["close"],
                            "volume": row["volume"],
                        }, name=ts))
                        htf_i += 1
                    # Feed LTF candle (replayed candles should NOT trigger real orders)
                    runner.engine.on_ltf_candle(pd.Series({
                        "open": ltf_row["open"], "high": ltf_row["high"],
                        "low":  ltf_row["low"],  "close": ltf_row["close"],
                        "volume": ltf_row["volume"],
                    }, name=ltf_ts))

                gaps_filled = len(df1m)
                bias = runner.engine.current_bias
                print(f"  📊 [{sym}] Gap-fill: {gaps_filled} ticks → bias: {bias or 'NO BIAS'}")
            else:
                bias = runner.engine.current_bias
                print(f"  📊 [{sym}] No intraday data (market closed?) — bias: {bias or 'NO BIAS'}")

            runner.engine.stop_replay()   # ← live trading resumes from here
            replayed += 1

        print(f"✅ Startup replay complete: {replayed} symbols — engine fully caught up")

    def _force_exit_watchdog(self):
        """
        Portfolio-level safety net — force-closes ALL open positions at 15:25 IST.
        Runs in its own thread, independent of kill switch or per-symbol engine state.
        This guarantees EOD exit even if kill switch blocked on_tick() delivery.
        """
        import time as _time
        from datetime import datetime, timezone, timedelta, time as _dtime
        _IST = timezone(timedelta(hours=5, minutes=30))
        print("[MSR] Force-exit watchdog started — will close all at 15:25 IST")
        while True:
            _time.sleep(10)
            now = datetime.now(_IST).time()
            if _dtime(15, 25) <= now <= _dtime(15, 26):
                print("[MSR] ⏰ 15:25 IST — portfolio force-exit watchdog firing")
                _closed = 0
                for token, runner in self.runners.items():
                    try:
                        if runner.broker.position:
                            ltp = runner.last_price or runner.broker.position.get("entry_price", 0)
                            import pandas as pd
                            runner.broker.force_close(ltp, pd.Timestamp.now(tz="Asia/Kolkata"))
                            _closed += 1
                            print(f"[MSR] Force-closed {runner.symbol} @ ₹{ltp}")
                    except Exception as e:
                        print(f"[MSR] Force-close error {runner.symbol}: {e}")
                print(f"[MSR] Force-exit watchdog done — {_closed} positions closed")
                break  # done for today

    def start(self):
        print("🚀 Starting MultiStrategyRunner...")
        print("📜 Phase 1: Replaying CSV history + Phase 2: Filling today's gap...")
        self._replay_htf_history()
        # Stop standalone heatmap WS before engine WS starts
        # prevents dual-feed LTP conflict (competing heatmap_tick emits)
        try:
            if heatmap_feed._ws_client and heatmap_feed._ws_client.is_connected():
                heatmap_feed._ws_client.stop()
                print("[MSR] Standalone heatmap WS stopped before engine WS connect")
            heatmap_feed._started = False  # allow restart after engine stops
        except Exception as e:
            print(f"[MSR] Heatmap stop warning: {e}")
        # Start force-exit watchdog in background — safety net independent of kill switch
        import threading as _threading
        _threading.Thread(target=self._force_exit_watchdog, daemon=True,
                          name="ForceExitWatchdog").start()
        self.client.start()   # blocks — run in thread

    def stop(self):
        """Stop the WS client and all instrument runners cleanly."""
        try:
            self.client.stop()
            print("[MultiStrategyRunner] WS client stopped")
        except Exception as e:
            print(f"[MultiStrategyRunner] stop error: {e}")