"""
BTC price feed for volatility calculation.

Uses Binance WebSocket for real-time trade data (~50ms latency)
with REST API fallback for historical price lookups.
"""

import asyncio
import json
import threading
import time

import numpy as np
import requests
import websockets

BINANCE_API = "https://api.binance.com/api/v3"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"


class BTCFeed:
    """Maintains a rolling window of BTC prices via WebSocket + REST fallback."""

    def __init__(self, window_seconds: int = 300):
        self.window_seconds = window_seconds
        self.prices: list[tuple[float, float]] = []  # (timestamp, price)
        self._lock = threading.Lock()

        # WebSocket state
        self._ws_price: float = 0
        self._ws_ts: float = 0
        self._ws_thread: threading.Thread | None = None
        self._ws_running = False
        self._ws_connected = False

        # Callback for real-time price updates (called from WS thread)
        self.on_price: callable | None = None

        # Price ticker thread (independent REST fallback)
        self._ticker_thread: threading.Thread | None = None
        self._ticker_running = False

    # ------------------------------------------------------------------
    #  WebSocket (real-time, ~50ms latency)
    # ------------------------------------------------------------------
    def start_ws(self):
        """Start the WebSocket feed in a background thread."""
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._ws_running = True
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()
        print("[WS] Binance trade stream starting...")

        # Start independent price ticker thread (REST fallback every 2s)
        if not self._ticker_thread or not self._ticker_thread.is_alive():
            self._ticker_running = True
            self._ticker_thread = threading.Thread(target=self._price_ticker, daemon=True)
            self._ticker_thread.start()

    def _ws_loop(self):
        """Background thread running the async WebSocket."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._ws_connect())

    async def _ws_connect(self):
        """Connect to Binance WebSocket with auto-reconnect."""
        while self._ws_running:
            try:
                async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                    self._ws_connected = True
                    print("[WS] Connected to Binance trade stream")
                    async for msg in ws:
                        if not self._ws_running:
                            break
                        try:
                            data = json.loads(msg)
                            price = float(data["p"])  # trade price
                            self._ws_price = price
                            self._ws_ts = time.time()  # local clock for freshness check
                            self._add_price(price)
                            # Fire callback for instant frontend updates
                            if self.on_price:
                                try:
                                    self.on_price(price)
                                except Exception:
                                    pass
                        except (KeyError, ValueError):
                            pass
            except Exception as e:
                self._ws_connected = False
                print(f"[WS] Disconnected: {e}, reconnecting in 2s...")
                await asyncio.sleep(2)

    def stop_ws(self):
        """Stop the WebSocket feed and price ticker."""
        self._ws_running = False
        self._ticker_running = False

    def _price_ticker(self):
        """Independent thread that fetches BTC price via REST every 2s.

        Acts as fallback when WebSocket is not connected.
        Ensures state.current_btc_price stays fresh even when
        the main bot loop is blocked on Polymarket API calls.
        """
        print("[TICKER] Price ticker thread started")
        while self._ticker_running:
            try:
                # Skip if WebSocket is delivering fresh prices
                if self._ws_connected and self._ws_price > 0 and (time.time() - self._ws_ts) < 5:
                    time.sleep(1)
                    continue

                # REST fallback
                resp = requests.get(
                    f"{BINANCE_API}/ticker/price",
                    params={"symbol": "BTCUSDT"},
                    timeout=3,
                )
                if resp.status_code == 200:
                    price = float(resp.json()["price"])
                    self._add_price(price)
                    if self.on_price:
                        try:
                            self.on_price(price)
                        except Exception:
                            pass
            except Exception:
                pass
            time.sleep(2)

    # ------------------------------------------------------------------
    #  Price Access
    # ------------------------------------------------------------------
    def fetch_current_price(self) -> float | None:
        """
        Get current BTC/USDT price.
        Prefers WebSocket (real-time), falls back to REST API.
        """
        # Use WebSocket price if fresh (< 5 seconds old)
        if self._ws_price > 0 and (time.time() - self._ws_ts) < 5:
            return self._ws_price

        # Fallback: REST API (WebSocket stale or not connected)
        try:
            resp = requests.get(
                f"{BINANCE_API}/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=3,
            )
            if resp.status_code == 200:
                price = float(resp.json()["price"])
                self._add_price(price)
                return price
        except (requests.RequestException, KeyError, ValueError):
            pass
        return None

    def get_feed_status(self) -> dict:
        """Return diagnostic info about the price feed."""
        ws_age = time.time() - self._ws_ts if self._ws_ts > 0 else -1
        return {
            "ws_connected": self._ws_connected,
            "ws_price": self._ws_price,
            "ws_age_seconds": round(ws_age, 1),
            "ws_fresh": self._ws_price > 0 and ws_age < 5,
            "ticker_running": self._ticker_running,
            "price_count": len(self.prices),
        }

    def fetch_recent_klines(self, interval: str = "1m", limit: int = 60) -> list[float]:
        """Fetch recent kline close prices from Binance (for bootstrapping)."""
        try:
            resp = requests.get(
                f"{BINANCE_API}/klines",
                params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
                timeout=5,
            )
            if resp.status_code == 200:
                return [float(k[4]) for k in resp.json()]
        except (requests.RequestException, KeyError, ValueError, IndexError):
            pass
        return []

    def fetch_price_at_time(self, timestamp_ms: int) -> float | None:
        """Get BTC price at a specific point in time using Binance klines."""
        try:
            resp = requests.get(
                f"{BINANCE_API}/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "startTime": timestamp_ms,
                    "limit": 1,
                },
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    return float(data[0][1])  # Open price
        except (requests.RequestException, KeyError, ValueError, IndexError):
            pass
        return None

    # ------------------------------------------------------------------
    #  Internal
    # ------------------------------------------------------------------
    def _add_price(self, price: float) -> None:
        """Add a price observation and prune old entries."""
        now = time.time()
        with self._lock:
            self.prices.append((now, price))
            cutoff = now - self.window_seconds
            while self.prices and self.prices[0][0] < cutoff:
                self.prices.pop(0)

    def get_returns(self) -> list[float]:
        """Calculate log returns from price history."""
        with self._lock:
            if len(self.prices) < 2:
                return []
            prices_arr = [p for _, p in self.prices]
        returns = []
        for i in range(1, len(prices_arr)):
            if prices_arr[i - 1] > 0:
                returns.append(np.log(prices_arr[i] / prices_arr[i - 1]))
        return returns

    def get_volatility_per_second(self) -> float:
        """Estimate per-second volatility from price history."""
        returns = self.get_returns()
        if len(returns) < 10:
            return 0.000085  # Fallback: ~2.5% daily vol
        return float(np.std(returns))

    def bootstrap(self) -> None:
        """Load initial price data for volatility calculation, then start WebSocket."""
        closes = self.fetch_recent_klines(interval="1m", limit=60)
        now = time.time()
        for i, price in enumerate(closes):
            ts = now - (len(closes) - 1 - i) * 60
            self.prices.append((ts, price))

        # Start real-time WebSocket feed
        self.start_ws()
