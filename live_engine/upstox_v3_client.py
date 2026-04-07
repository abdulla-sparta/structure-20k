# live_engine/upstox_v3_client.py
#
# Upstox V3 protobuf WebSocket client — TICK STREAMING.
# Modelled exactly after the old working project (structure-engine-v2).
#
# Key points that match old working client:
#   - WS runs in its OWN daemon thread (not blocking main thread)
#   - Token read fresh from CONFIG at each connect attempt
#   - Verbose print statements (same as old project) for easy debugging
#   - Reconnect loop runs in a separate thread
#   - Subscribe msg sent as BINARY opcode (required by Upstox)
#   - Checks feed.type == live_feed before processing
#
# Callback signatures:
#   per-token:  fn(ltp, prev_close, timestamp, volume, atp)
#   global:     fn(key, ltp, prev_close, timestamp, volume, atp)

import requests
import websocket
import threading
import ssl
import json
import time
import logging
from datetime import datetime, timezone

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live_engine import MarketDataFeedV3_pb2 as pb
from config import CONFIG

# Order book registry — populated from full-feed depth on every tick
# Imported lazily to avoid circular import issues at module load
_ob_registry = None
def _get_ob_registry():
    global _ob_registry
    if _ob_registry is None:
        try:
            from live_engine.order_book import OrderBookRegistry
            _ob_registry = OrderBookRegistry()
        except Exception:
            pass
    return _ob_registry

log = logging.getLogger(__name__)


class UpstoxV3Client:

    AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"

    def __init__(self, tokens: list):
        self.tokens            = tokens
        self._callbacks        = {}    # token → fn(ltp, prev_close, ts, vol, atp)
        self._global_listeners = []   # [fn(key, ltp, prev_close, ts, vol, atp)]
        self._market_closed_cb = None  # fn() called when market is confirmed closed
        self._ws               = None
        self._connected        = False
        self._stop             = False

    # ── Register ──────────────────────────────────────────────────────────────

    def on_tick(self, instrument_key: str, callback):
        """Per-symbol callback: fn(ltp, prev_close, timestamp, volume, atp)"""
        self._callbacks[instrument_key] = callback

    def add_global_listener(self, callback):
        """Global listener for ALL ticks: fn(key, ltp, prev_close, ts, vol, atp)"""
        if callback not in self._global_listeners:
            self._global_listeners.append(callback)

    def set_market_closed_callback(self, fn):
        """Register a function to call when market_info shows all segments closed."""
        self._market_closed_cb = fn

    def is_connected(self) -> bool:
        return self._connected

    def get_order_book_registry(self):
        """Return the shared OrderBookRegistry populated from WS ticks."""
        return _get_ob_registry()

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def start(self):
        """Blocking reconnect loop. Run in a thread via start_in_thread()."""
        while not self._stop:
            try:
                self._connect_once()
            except Exception as e:
                print(f"[WS] Connect error: {e}")
            if not self._stop:
                print("[WS] Disconnected — reconnecting in 5s…")
                time.sleep(5)

    def start_in_thread(self):
        """Start WS in a background daemon thread (non-blocking)."""
        t = threading.Thread(target=self.start, daemon=True, name="UpstoxV3WS")
        t.start()
        return t

    def stop(self):
        self._stop = True
        if self._ws:
            self._ws.close()

    # ── Internal connect ─────────────────────────────────────────────────────

    def _connect_once(self):
        # Always read token fresh — user may update it via UI/env
        token = CONFIG.get("upstox_access_token", "")
        if not token:
            print("[WS] No access token in CONFIG — retrying in 30s")
            print("[WS] Set UPSTOX_ACCESS_TOKEN in .env file and restart")
            time.sleep(30)
            return

        # Step 1: Authorize — get WS URI
        print("🔐 Authorizing feed...")
        try:
            resp = requests.get(
                self.AUTHORIZE_URL,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"❌ Authorization failed: {resp.status_code} — {resp.text[:200]}")
                time.sleep(10)
                return
            ws_url = resp.json()["data"]["authorizedRedirectUri"]
            print(f"✅ Authorized")
            print(f"Connecting to: {ws_url[:80]}…")
        except Exception as e:
            print(f"❌ Authorize request failed: {e}")
            time.sleep(10)
            return

        # Step 2: open WS — runs in ITS OWN thread (same as old project)
        self._ws = websocket.WebSocketApp(
            ws_url,
            header=[
                f"Authorization: Bearer {token}",
                "Accept: */*",
                "Sec-WebSocket-Protocol: binary",
            ],
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
        )

        # run_forever in a daemon thread — same pattern as old project
        ws_thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={
                "sslopt":         {"cert_reqs": ssl.CERT_NONE},
                "ping_interval":  20,
                "ping_timeout":   10,
            },
            daemon=True,
        )
        ws_thread.start()
        ws_thread.join()   # block until WS closes, then reconnect loop fires

    # ── WS event handlers ─────────────────────────────────────────────────────

    def _on_open(self, ws):
        self._connected = True
        print(f"✅ WebSocket Connected")

        # After 4s, if cache is still empty (market closed), seed from REST/CSV
        if self._market_closed_cb:
            def _delayed_seed():
                time.sleep(4)
                if self._market_closed_cb:
                    self._market_closed_cb()
            threading.Thread(target=_delayed_seed, daemon=True).start()

        subscribe_msg = {
            "guid":   "live-feed",
            "method": "sub",
            "data": {
                "mode":           "full",
                "instrumentKeys": self.tokens,
            },
        }
        # MUST send as BINARY — text mode doesn't work with Upstox V3
        ws.send(
            json.dumps(subscribe_msg).encode("utf-8"),
            opcode=websocket.ABNF.OPCODE_BINARY,
        )
        print(f"📡 Subscribed to: {self.tokens}")

    def _on_message(self, ws, message: bytes):
        try:
            feed = pb.FeedResponse()
            feed.ParseFromString(message)

            # Only process live feed ticks (skip initial_feed and market_info)
            if feed.type != pb.Type.live_feed:
                if feed.type == pb.Type.market_info:
                    closed_count = 0
                    for seg, status in feed.marketInfo.segmentStatus.items():
                        sname = pb.MarketStatus.Name(status)
                        print(f"[Market] {seg}: {sname}")
                        if sname in ("NORMAL_CLOSE", "CLOSING_END"):
                            closed_count += 1
                    # If NSE_EQ is closed, seed heatmap from REST/CSV
                    nse_status = feed.marketInfo.segmentStatus.get("NSE_EQ", -1)
                    if nse_status in (3, 5):  # NORMAL_CLOSE=3, CLOSING_END=5
                        if self._market_closed_cb:
                            threading.Thread(
                                target=self._market_closed_cb, daemon=True
                            ).start()
                return

            # Resolve base timestamp from feed header — always convert to IST
            from datetime import timedelta
            IST = timezone(timedelta(hours=5, minutes=30))
            if feed.currentTs:
                base_ts = datetime.fromtimestamp(
                    feed.currentTs / 1000, tz=timezone.utc
                ).astimezone(IST).replace(tzinfo=None)
            else:
                base_ts = datetime.utcnow() + timedelta(hours=5, minutes=30)

            for key, f in feed.feeds.items():
                tick = self._extract(f)
                if tick is None:
                    continue
                ltp, prev_close, volume, atp, ltt, bids, asks = tick

                # Use per-tick ltt (last trade time) when available — more precise
                ts = base_ts
                if ltt and ltt > 0:
                    try:
                        ts = datetime.fromtimestamp(
                            ltt / 1000, tz=timezone.utc
                        ).astimezone(IST).replace(tzinfo=None)
                    except Exception:
                        pass

                # ── Push depth to order book registry ─────────────────────
                if bids or asks:
                    try:
                        reg = _get_ob_registry()
                        if reg:
                            import time as _time
                            reg.update_from_tick(key, bids, asks, _time.time())
                            # Also try normalized key (| vs :)
                            alt = key.replace("|", ":") if "|" in key else key.replace(":", "|")
                            reg.update_from_tick(alt, bids, asks, _time.time())
                    except Exception:
                        pass

                # One-time log: show key format vs registered callbacks
                if not getattr(self, '_key_format_logged', False):
                    self._key_format_logged = True
                    registered = list(self._callbacks.keys())[:3]
                    n_global = len(self._global_listeners)
                    print(f"[WS] Proto key format: '{key}' | Registered: {registered} | GlobalListeners: {n_global} | ObjId: {id(self)}")
                print(f"📈 {key} | LTP: {ltp}")

                # Per-token callback → InstrumentRunner
                # Upstox proto may use | or : as separator — normalize
                cb = self._callbacks.get(key)
                if cb is None:
                    alt_key = key.replace("|", ":") if "|" in key else key.replace(":", "|")
                    cb = self._callbacks.get(alt_key)
                if cb:
                    try:
                        cb(ltp=ltp, prev_close=prev_close,
                           timestamp=ts, volume=volume, atp=atp)
                    except Exception as e:
                        log.debug(f"Per-token cb error [{key}]: {e}")

                # Global listeners → heatmap + SocketIO emitter
                for gl in self._global_listeners:
                    try:
                        gl(key=key, ltp=ltp, prev_close=prev_close,
                           timestamp=ts, volume=volume, atp=atp)
                    except Exception as e:
                        log.debug(f"Global listener error: {e}")

        except Exception as e:
            log.debug(f"Parse error: {e}")

    def _extract(self, f):
        """Extract (ltp, prev_close, volume, atp, ltt, bids, asks) from a Feed proto.
        Returns None if no ltp. bids/asks are lists of (price, qty) tuples."""
        ltp = prev_close = volume = atp = ltt = None
        bids, asks = [], []

        if f.HasField("fullFeed"):
            ff = f.fullFeed
            if ff.HasField("marketFF"):
                mff        = ff.marketFF
                ltp        = mff.ltpc.ltp  or None
                prev_close = mff.ltpc.cp   or None
                ltt        = mff.ltpc.ltt  or None
                volume     = mff.vtt       or None
                atp        = mff.atp       or None

                # ── Extract order book depth (full mode) ──────────────────
                # Upstox V3 proto: marketFF.marketLevel.bidAskQuote repeated
                try:
                    for level in mff.marketLevel.bidAskQuote:
                        if level.bidP > 0 and level.bidQ > 0:
                            bids.append((float(level.bidP), int(level.bidQ)))
                        if level.askP > 0 and level.askQ > 0:
                            asks.append((float(level.askP), int(level.askQ)))
                except Exception:
                    pass  # depth not available on this tick — use empty lists

            elif ff.HasField("indexFF"):
                iff        = ff.indexFF
                ltp        = iff.ltpc.ltp  or None
                prev_close = iff.ltpc.cp   or None
                ltt        = iff.ltpc.ltt  or None

        elif f.HasField("ltpc"):
            ltp        = f.ltpc.ltp  or None
            prev_close = f.ltpc.cp   or None
            ltt        = f.ltpc.ltt  or None

        if not ltp or ltp <= 0:
            return None
        return ltp, prev_close, volume, atp, ltt, bids, asks

    def _on_error(self, ws, error):
        self._connected = False
        print(f"❌ WebSocket error: {error}")

    def _on_close(self, ws, code, msg):
        self._connected = False
        print(f"🔴 WebSocket closed: {code} {msg}")