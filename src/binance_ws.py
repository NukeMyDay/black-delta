"""
Binance Futures WebSocket — Real-time BTC price feed.

Connects to Binance BTCUSDT Perpetual Futures bookTicker stream
for the fastest available BTC price signal (~10ms updates).

Futures lead spot by 100-500ms because leveraged traders react faster.
This gives us an edge over Polymarket market makers who may use spot.

Usage:
    feed = BinancePriceFeed()
    feed.start()

    # Get current price (non-blocking, returns latest cached value):
    price = feed.btc_price          # mid-price (bid+ask)/2
    bid, ask = feed.btc_bid, feed.btc_ask

    # Track movement from a reference point:
    feed.set_window_reference()     # snapshot current price
    move = feed.btc_move            # current - reference
"""

import asyncio
import json
import logging
import threading
import time

log = logging.getLogger("binance_ws")

# Binance Futures WebSocket endpoints
FUTURES_WS = "wss://fstream.binance.com/ws/btcusdt@bookTicker"
SPOT_WS = "wss://stream.binance.com/ws/btcusdt@bookTicker"

# Reconnect settings
RECONNECT_DELAY = 2  # seconds
MAX_RECONNECT_DELAY = 30
WATCHDOG_TIMEOUT = 10  # force reconnect if no message for this long


class BinancePriceFeed:
    """Real-time BTC price from Binance Futures WebSocket."""

    def __init__(self, use_futures: bool = True):
        """
        Args:
            use_futures: True for Perpetual Futures (fastest), False for Spot.
        """
        self._ws_url = FUTURES_WS if use_futures else SPOT_WS
        self._source = "futures" if use_futures else "spot"

        # Price state (thread-safe via GIL for simple reads)
        self.btc_bid: float = 0.0
        self.btc_ask: float = 0.0
        self.btc_price: float = 0.0  # mid = (bid+ask)/2
        self.last_update_ts: float = 0.0
        self.update_count: int = 0

        # Window reference tracking
        self._window_ref_price: float = 0.0
        self._window_ref_ts: float = 0.0

        # Latency tracking (zero overhead — computed from existing message parsing)
        self.latency_last_ms: float = 0.0
        self.latency_min_ms: float = float("inf")
        self.latency_max_ms: float = 0.0
        self._latency_sum: float = 0.0
        self._latency_count: int = 0

        # Connection state
        self._thread: threading.Thread | None = None
        self._running = False
        self._connected = False
        self._last_msg_ts: float = 0.0
        self._reconnect_count: int = 0

        # Callbacks (optional)
        self.on_price_update: callable | None = None  # (price, bid, ask, ts)

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------
    @property
    def btc_move(self) -> float:
        """BTC price change since set_window_reference() was called."""
        if self._window_ref_price == 0 or self.btc_price == 0:
            return 0.0
        return self.btc_price - self._window_ref_price

    @property
    def btc_move_pct(self) -> float:
        """BTC price change in percent since reference."""
        if self._window_ref_price == 0:
            return 0.0
        return (self.btc_price - self._window_ref_price) / self._window_ref_price * 100

    @property
    def window_ref_price(self) -> float:
        """The reference price set at window start."""
        return self._window_ref_price

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def price_age_ms(self) -> float:
        """How old is the current price in milliseconds."""
        if self.last_update_ts == 0:
            return float("inf")
        return (time.time() - self.last_update_ts) * 1000

    def set_window_reference(self, price: float | None = None):
        """
        Snapshot the current BTC price as the reference for a new window.
        All btc_move calculations will be relative to this.

        Args:
            price: Override reference price. If None, uses current btc_price.
        """
        self._window_ref_price = price if price is not None else self.btc_price
        self._window_ref_ts = time.time()

    def start(self):
        """Start the WebSocket connection in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"binance-ws-{self._source}",
            daemon=True,
        )
        self._thread.start()
        log.info(f"BinancePriceFeed started ({self._source}): {self._ws_url}")

    def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("BinancePriceFeed stopped")

    # ------------------------------------------------------------------
    #  WebSocket Loop
    # ------------------------------------------------------------------
    def _run_loop(self):
        """Main thread entry: runs asyncio event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._ws_loop())
        loop.close()

    async def _ws_loop(self):
        """Connect, subscribe, receive messages. Auto-reconnect on failure."""
        delay = RECONNECT_DELAY

        while self._running:
            try:
                await self._connect_and_listen()
                delay = RECONNECT_DELAY  # reset on clean disconnect
            except Exception as e:
                self._connected = False
                if not self._running:
                    break
                log.warning(f"Binance WS error: {e}. Reconnecting in {delay}s...")
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, MAX_RECONNECT_DELAY)
                self._reconnect_count += 1

    async def _connect_and_listen(self):
        """Single WebSocket session."""
        try:
            import websockets
        except ImportError:
            log.error("websockets package required: pip install websockets")
            return

        async with websockets.connect(
            self._ws_url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._connected = True
            self._last_msg_ts = time.time()
            log.info(f"Binance WS connected ({self._source})")

            # Watchdog task
            watchdog = asyncio.create_task(self._watchdog(ws))

            try:
                async for raw in ws:
                    if not self._running:
                        break
                    self._last_msg_ts = time.time()
                    self._handle_message(raw)
            finally:
                watchdog.cancel()
                self._connected = False

    async def _watchdog(self, ws):
        """Force reconnect if no messages received for too long."""
        while self._running:
            await asyncio.sleep(WATCHDOG_TIMEOUT / 2)  # Check more frequently
            if time.time() - self._last_msg_ts > WATCHDOG_TIMEOUT:
                log.warning("Binance WS watchdog: no messages, forcing reconnect")
                await ws.close()
                return

    def _handle_message(self, raw: str):
        """Parse bookTicker message and update price state.

        Format: {"s":"BTCUSDT","b":"69142.50","a":"69142.60","T":1775433900123}
          b = best bid price
          a = best ask price
          T = transaction time (ms)
        """
        try:
            data = json.loads(raw)

            bid = float(data.get("b", 0))
            ask = float(data.get("a", 0))

            if bid <= 0 or ask <= 0 or bid > ask:
                return  # Invalid or inverted quote

            mid = (bid + ask) / 2.0

            now = time.time()
            self.btc_bid = bid
            self.btc_ask = ask
            self.btc_price = mid
            self.last_update_ts = now
            self.update_count += 1

            # Latency: compare Binance server timestamp to local receive time
            server_ts_ms = data.get("T") or data.get("E")
            if server_ts_ms:
                lat = (now * 1000) - float(server_ts_ms)
                if 0 < lat < 5000:  # sanity: ignore clock skew > 5s
                    self.latency_last_ms = lat
                    if lat < self.latency_min_ms:
                        self.latency_min_ms = lat
                    if lat > self.latency_max_ms:
                        self.latency_max_ms = lat
                    self._latency_sum += lat
                    self._latency_count += 1

            # Fire callback if registered
            if self.on_price_update:
                try:
                    self.on_price_update(mid, bid, ask, self.last_update_ts)
                except Exception:
                    pass  # Don't let callback errors kill the feed

        except (json.JSONDecodeError, ValueError, KeyError):
            pass  # Skip malformed messages

    # ------------------------------------------------------------------
    #  Status / Debug
    # ------------------------------------------------------------------
    @property
    def latency_avg_ms(self) -> float:
        return self._latency_sum / self._latency_count if self._latency_count > 0 else 0

    def status(self) -> dict:
        """Return current status for dashboard/logging."""
        return {
            "source": self._source,
            "connected": self._connected,
            "btc_price": round(self.btc_price, 2),
            "btc_bid": round(self.btc_bid, 2),
            "btc_ask": round(self.btc_ask, 2),
            "btc_move": round(self.btc_move, 2),
            "window_ref": round(self._window_ref_price, 2),
            "price_age_ms": round(self.price_age_ms, 1),
            "update_count": self.update_count,
            "reconnects": self._reconnect_count,
            "latency_last_ms": round(self.latency_last_ms, 1),
            "latency_min_ms": round(self.latency_min_ms, 1) if self.latency_min_ms < float("inf") else 0,
            "latency_max_ms": round(self.latency_max_ms, 1),
            "latency_avg_ms": round(self.latency_avg_ms, 1),
        }
