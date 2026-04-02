"""
BTC price feed for volatility calculation.

Uses Binance public API (no auth needed) for 1-second kline data
as a real-time BTC price source independent of Polymarket.
"""

import time
import requests
from collections import deque

import numpy as np

BINANCE_API = "https://api.binance.com/api/v3"


class BTCFeed:
    """Maintains a rolling window of BTC prices and computes returns."""

    def __init__(self, window_seconds: int = 300):
        """
        Args:
            window_seconds: How many seconds of price history to keep.
        """
        self.window_seconds = window_seconds
        self.prices: deque[tuple[float, float]] = deque()  # (timestamp, price)

    def fetch_current_price(self) -> float | None:
        """Get current BTC/USDT price from Binance."""
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

    def fetch_recent_klines(self, interval: str = "1m", limit: int = 60) -> list[float]:
        """
        Fetch recent kline (candlestick) close prices from Binance.
        Useful for bootstrapping the volatility calculation.

        Args:
            interval: Kline interval (1m, 5m, etc.)
            limit: Number of candles.

        Returns:
            List of close prices.
        """
        try:
            resp = requests.get(
                f"{BINANCE_API}/klines",
                params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
                timeout=5,
            )
            if resp.status_code == 200:
                closes = [float(k[4]) for k in resp.json()]
                return closes
        except (requests.RequestException, KeyError, ValueError, IndexError):
            pass
        return []

    def _add_price(self, price: float) -> None:
        """Add a price observation and prune old entries."""
        now = time.time()
        self.prices.append((now, price))
        cutoff = now - self.window_seconds
        while self.prices and self.prices[0][0] < cutoff:
            self.prices.popleft()

    def get_returns(self) -> list[float]:
        """Calculate log returns from price history."""
        if len(self.prices) < 2:
            return []
        prices_arr = [p for _, p in self.prices]
        returns = []
        for i in range(1, len(prices_arr)):
            if prices_arr[i - 1] > 0:
                returns.append(np.log(prices_arr[i] / prices_arr[i - 1]))
        return returns

    def get_volatility_per_second(self) -> float:
        """
        Estimate per-second volatility from the price history.

        Returns:
            Standard deviation of per-observation returns.
            If not enough data, returns a conservative default.
        """
        returns = self.get_returns()
        if len(returns) < 10:
            # Fallback: BTC ~2.5% daily vol -> per-second
            # 0.025 / sqrt(86400) ~= 0.000085
            return 0.000085
        return float(np.std(returns))

    def fetch_price_at_time(self, timestamp_ms: int) -> float | None:
        """
        Get the BTC price at a specific point in time using Binance klines.

        Args:
            timestamp_ms: Unix timestamp in milliseconds.

        Returns:
            The open price of the 1-minute candle containing that timestamp.
        """
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

    def bootstrap(self) -> None:
        """Load initial price data for volatility calculation."""
        closes = self.fetch_recent_klines(interval="1m", limit=60)
        now = time.time()
        for i, price in enumerate(closes):
            # Space them 60 seconds apart, ending at now
            ts = now - (len(closes) - 1 - i) * 60
            self.prices.append((ts, price))
