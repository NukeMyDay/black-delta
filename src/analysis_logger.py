"""
Bonereaper Trade Analysis Logger — Comprehensive trade capture for strategy analysis.

Logs EVERY trade from followed wallets (BUY + SELL, all markets) with:
  - Full trade details (side, outcome, price, size, asset, slug)
  - BTC/ETH spot price at time of trade (via Binance REST API, cached)
  - BTC price at window start (reference price for up/down market)
  - Both token mid-prices (Up + Down) at time of each trade
  - Time delta between consecutive trades within same window
  - Window context: elapsed time, cumulative volumes per direction, trade sequence
  - Per-window summaries with market resolution, previous window outcome,
    BTC pre-trade movement, and theoretical P&L

Output:
  - logs/analysis_trades_{DATE}.csv      — Every individual trade
  - logs/analysis_windows_{DATE}.json    — Per-window aggregated summaries

Usage:
    logger = AnalysisLogger()
    logger.start()                # starts BTC/ETH price cache thread
    logger.log_trade(trade)       # called from FollowFeed callback
"""

import csv
import json
import os
import threading
import time
from datetime import datetime, timezone

import requests

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")

# ── Binance price cache ──────────────────────────────────────────
BINANCE_API = "https://api.binance.com/api/v3"

_price_cache: dict[str, float] = {"BTCUSDT": 0.0, "ETHUSDT": 0.0}
_price_cache_lock = threading.Lock()
_price_cache_ts: float = 0.0
PRICE_CACHE_TTL = 3  # seconds — refresh every 3s


def _fetch_binance_prices():
    """Fetch BTC and ETH spot prices from Binance."""
    global _price_cache_ts
    try:
        resp = requests.get(
            f"{BINANCE_API}/ticker/price",
            params={"symbols": '["BTCUSDT","ETHUSDT"]'},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            with _price_cache_lock:
                for item in data:
                    symbol = item.get("symbol", "")
                    price = float(item.get("price", 0))
                    if symbol in _price_cache:
                        _price_cache[symbol] = price
                _price_cache_ts = time.time()
    except Exception:
        pass  # stale cache is fine, don't block trade logging


def _get_cached_prices() -> dict[str, float]:
    """Return latest cached BTC/ETH prices, refreshing if stale."""
    global _price_cache_ts
    now = time.time()
    if now - _price_cache_ts > PRICE_CACHE_TTL:
        _fetch_binance_prices()
    with _price_cache_lock:
        return dict(_price_cache)


# ── BTC price at window start (cached per window) ────────────────
# We snapshot BTC price at the start of each 5m window via Binance
# kline endpoint. Cached to avoid repeated calls for same window.

_btc_window_start_cache: dict[int, float] = {}
_btc_window_cache_lock = threading.Lock()


def _fetch_btc_at_timestamp(ts: int) -> float:
    """Fetch BTC close price for the 1-minute candle containing `ts`."""
    with _btc_window_cache_lock:
        if ts in _btc_window_start_cache:
            return _btc_window_start_cache[ts]
    try:
        # Binance kline: 1m candle at the window start time
        resp = requests.get(
            f"{BINANCE_API}/klines",
            params={
                "symbol": "BTCUSDT",
                "interval": "1m",
                "startTime": ts * 1000,
                "limit": 1,
            },
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0:
                # kline format: [openTime, open, high, low, close, ...]
                close_price = float(data[0][4])
                with _btc_window_cache_lock:
                    _btc_window_start_cache[ts] = close_price
                    # Keep cache bounded
                    if len(_btc_window_start_cache) > 200:
                        oldest = sorted(_btc_window_start_cache.keys())[:100]
                        for k in oldest:
                            _btc_window_start_cache.pop(k, None)
                return close_price
    except Exception:
        pass
    return 0.0


# ── Token mid-price fetcher (non-blocking, cached briefly) ───────

_mid_cache: dict[str, tuple[float, float]] = {}  # token_id → (price, timestamp)
_mid_cache_lock = threading.Lock()
MID_CACHE_TTL = 5  # seconds


def _fetch_mid_price(token_id: str) -> float:
    """Fetch mid-market price for a token, with brief caching."""
    if not token_id:
        return 0.0

    now = time.time()
    with _mid_cache_lock:
        cached = _mid_cache.get(token_id)
        if cached and now - cached[1] < MID_CACHE_TTL:
            return cached[0]

    try:
        from src.polymarket import fetch_midpoint
        mid = fetch_midpoint(token_id)
        if mid and mid > 0:
            with _mid_cache_lock:
                _mid_cache[token_id] = (mid, now)
                # Bound cache
                if len(_mid_cache) > 100:
                    _mid_cache.clear()
            return mid
    except Exception:
        pass
    return 0.0


# ── Window Tracker ────────────────────────────────────────────────

class WindowTracker:
    """Tracks per-window aggregated stats for a single market slug."""

    def __init__(self, slug: str):
        self.slug = slug
        self.window_start = self._parse_window_start(slug)
        self.window_duration = 900 if "15m" in slug else 300

        # Trade accumulation
        self.trade_timestamps: list[float] = []  # for inter-trade delta
        self.buy_up_usdc = 0.0
        self.buy_down_usdc = 0.0
        self.buy_up_shares = 0.0
        self.buy_down_shares = 0.0
        self.sell_up_usdc = 0.0
        self.sell_down_usdc = 0.0
        self.sell_up_shares = 0.0
        self.sell_down_shares = 0.0
        self.buy_count = 0
        self.sell_count = 0
        self.first_trade_ts = 0.0
        self.last_trade_ts = 0.0

        # Token IDs (for mid-price lookups)
        self.up_token_id: str = ""
        self.down_token_id: str = ""

        # Price context at first/last trade
        self.btc_at_first_trade = 0.0
        self.btc_at_last_trade = 0.0
        self.eth_at_first_trade = 0.0
        self.eth_at_last_trade = 0.0

        # BTC at window start (fetched once)
        self.btc_at_window_start = 0.0
        self._btc_start_fetched = False

        # Market resolution (filled after window closes)
        self.market_outcome: str | None = None  # "up" or "down"

    @staticmethod
    def _parse_window_start(slug: str) -> int:
        try:
            return int(slug.split("-")[-1])
        except (ValueError, IndexError):
            return 0

    def ensure_btc_start(self):
        """Lazily fetch BTC price at window start (called once per window)."""
        if self._btc_start_fetched or self.window_start <= 0:
            return
        self._btc_start_fetched = True
        self.btc_at_window_start = _fetch_btc_at_timestamp(self.window_start)

    def add_trade(self, trade: dict, btc_price: float, eth_price: float) -> float:
        """Add a trade to this window's accumulation.

        Returns the time delta (seconds) since the previous trade in this window.
        """
        side = trade.get("side", "").upper()
        outcome = trade.get("outcome", "").lower()
        price = trade.get("price", 0)
        size = trade.get("size", 0)
        usdc = price * size

        if side == "BUY":
            self.buy_count += 1
            if outcome == "up":
                self.buy_up_usdc += usdc
                self.buy_up_shares += size
                if not self.up_token_id:
                    self.up_token_id = trade.get("asset", "")
            elif outcome == "down":
                self.buy_down_usdc += usdc
                self.buy_down_shares += size
                if not self.down_token_id:
                    self.down_token_id = trade.get("asset", "")
        elif side == "SELL":
            self.sell_count += 1
            if outcome == "up":
                self.sell_up_usdc += usdc
                self.sell_up_shares += size
                if not self.up_token_id:
                    self.up_token_id = trade.get("asset", "")
            elif outcome == "down":
                self.sell_down_usdc += usdc
                self.sell_down_shares += size
                if not self.down_token_id:
                    self.down_token_id = trade.get("asset", "")

        now = trade.get("detected_at", time.time())

        # Inter-trade time delta
        delta_s = 0.0
        if self.trade_timestamps:
            delta_s = now - self.trade_timestamps[-1]
        self.trade_timestamps.append(now)

        if self.first_trade_ts == 0:
            self.first_trade_ts = now
            self.btc_at_first_trade = btc_price
            self.eth_at_first_trade = eth_price
        self.last_trade_ts = now
        self.btc_at_last_trade = btc_price
        self.eth_at_last_trade = eth_price

        # Fetch BTC at window start (once)
        self.ensure_btc_start()

        return delta_s

    @property
    def total_buy_usdc(self) -> float:
        return self.buy_up_usdc + self.buy_down_usdc

    @property
    def total_sell_usdc(self) -> float:
        return self.sell_up_usdc + self.sell_down_usdc

    @property
    def net_direction(self) -> str:
        """Net BUY direction (up or down). Ignores SELLs for direction signal."""
        if self.buy_up_usdc > self.buy_down_usdc:
            return "up"
        elif self.buy_down_usdc > self.buy_up_usdc:
            return "down"
        return "neutral"

    @property
    def buy_bias(self) -> float:
        """Buy-side bias: fraction towards UP (0.0=all down, 1.0=all up)."""
        total = self.total_buy_usdc
        if total <= 0:
            return 0.5
        return self.buy_up_usdc / total

    @property
    def elapsed_at_first_trade(self) -> float:
        """Seconds into the window when first trade was detected."""
        if self.window_start <= 0 or self.first_trade_ts <= 0:
            return 0
        return self.first_trade_ts - self.window_start

    @property
    def elapsed_at_last_trade(self) -> float:
        """Seconds into the window when last trade was detected."""
        if self.window_start <= 0 or self.last_trade_ts <= 0:
            return 0
        return self.last_trade_ts - self.window_start

    @property
    def btc_move_during_window(self) -> float:
        """BTC price change from first to last trade (absolute $)."""
        if self.btc_at_first_trade <= 0:
            return 0
        return self.btc_at_last_trade - self.btc_at_first_trade

    @property
    def btc_move_pct(self) -> float:
        """BTC price change from first to last trade (%)."""
        if self.btc_at_first_trade <= 0:
            return 0
        return (self.btc_at_last_trade - self.btc_at_first_trade) / self.btc_at_first_trade * 100

    @property
    def btc_pre_move(self) -> float:
        """BTC movement from window start to first trade ($).

        Positive = BTC went up before he started trading.
        Key question: does he react to price moves or predict them?
        """
        if self.btc_at_window_start <= 0 or self.btc_at_first_trade <= 0:
            return 0
        return self.btc_at_first_trade - self.btc_at_window_start

    @property
    def avg_inter_trade_delta(self) -> float:
        """Average seconds between consecutive trades."""
        if len(self.trade_timestamps) < 2:
            return 0
        deltas = [
            self.trade_timestamps[i] - self.trade_timestamps[i - 1]
            for i in range(1, len(self.trade_timestamps))
        ]
        return sum(deltas) / len(deltas) if deltas else 0

    @property
    def min_inter_trade_delta(self) -> float:
        """Minimum seconds between consecutive trades (burst detection)."""
        if len(self.trade_timestamps) < 2:
            return 0
        deltas = [
            self.trade_timestamps[i] - self.trade_timestamps[i - 1]
            for i in range(1, len(self.trade_timestamps))
        ]
        return min(deltas) if deltas else 0

    @property
    def theoretical_pnl(self) -> float | None:
        """Theoretical P&L if market resolved in his net direction.

        Only available after market_outcome is set.
        """
        if not self.market_outcome:
            return None
        won = self.net_direction == self.market_outcome
        if self.net_direction == "neutral":
            return 0.0

        # Shares bought on winning side
        if self.market_outcome == "up":
            win_shares = self.buy_up_shares - self.sell_up_shares
            win_cost = self.buy_up_usdc - self.sell_up_usdc
            lose_cost = self.buy_down_usdc - self.sell_down_usdc
        else:
            win_shares = self.buy_down_shares - self.sell_down_shares
            win_cost = self.buy_down_usdc - self.sell_down_usdc
            lose_cost = self.buy_up_usdc - self.sell_up_usdc

        # Winning shares pay $1 each, losing shares pay $0
        pnl = win_shares - win_cost - max(0, lose_cost)
        return round(pnl, 2)

    def to_summary(self) -> dict:
        """Export window summary for JSON."""
        return {
            "slug": self.slug,
            "window_start": self.window_start,
            "window_duration": self.window_duration,
            # Timing
            "first_trade_elapsed_s": round(self.elapsed_at_first_trade, 1),
            "last_trade_elapsed_s": round(self.elapsed_at_last_trade, 1),
            "trading_span_s": round(self.elapsed_at_last_trade - self.elapsed_at_first_trade, 1),
            # Counts
            "total_trades": self.buy_count + self.sell_count,
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            # BUY volumes
            "buy_up_usdc": round(self.buy_up_usdc, 2),
            "buy_down_usdc": round(self.buy_down_usdc, 2),
            "buy_up_shares": round(self.buy_up_shares, 2),
            "buy_down_shares": round(self.buy_down_shares, 2),
            "total_buy_usdc": round(self.total_buy_usdc, 2),
            # SELL volumes
            "sell_up_usdc": round(self.sell_up_usdc, 2),
            "sell_down_usdc": round(self.sell_down_usdc, 2),
            "sell_up_shares": round(self.sell_up_shares, 2),
            "sell_down_shares": round(self.sell_down_shares, 2),
            "total_sell_usdc": round(self.total_sell_usdc, 2),
            # Direction
            "net_direction": self.net_direction,
            "buy_bias": round(self.buy_bias, 4),
            "buy_bias_pct": round(abs(self.buy_bias - 0.5) * 200, 1),  # 0-100%
            # BTC context
            "btc_at_window_start": round(self.btc_at_window_start, 2),
            "btc_at_first_trade": round(self.btc_at_first_trade, 2),
            "btc_at_last_trade": round(self.btc_at_last_trade, 2),
            "btc_pre_move_usd": round(self.btc_pre_move, 2),
            "btc_move_usd": round(self.btc_move_during_window, 2),
            "btc_move_pct": round(self.btc_move_pct, 4),
            # ETH context
            "eth_at_first_trade": round(self.eth_at_first_trade, 2),
            "eth_at_last_trade": round(self.eth_at_last_trade, 2),
            # Avg entry prices
            "avg_buy_up_price": round(self.buy_up_usdc / self.buy_up_shares, 4) if self.buy_up_shares > 0 else None,
            "avg_buy_down_price": round(self.buy_down_usdc / self.buy_down_shares, 4) if self.buy_down_shares > 0 else None,
            # Inter-trade timing
            "avg_trade_delta_s": round(self.avg_inter_trade_delta, 2),
            "min_trade_delta_s": round(self.min_inter_trade_delta, 2),
            # Market resolution
            "market_outcome": self.market_outcome,
            "direction_correct": (self.net_direction == self.market_outcome) if self.market_outcome else None,
            "theoretical_pnl": self.theoretical_pnl,
            # Timestamps
            "first_trade_ts": self.first_trade_ts,
            "last_trade_ts": self.last_trade_ts,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }


# ── CSV Trade Fields ──────────────────────────────────────────────

ANALYSIS_FIELDS = [
    "timestamp",           # ISO8601 detection time
    "slug",                # Market identifier
    "side",                # BUY or SELL
    "outcome",             # Up or Down
    "price",               # Contract price (0-1)
    "size",                # Shares
    "usdc_value",          # price * size
    "asset",               # Token ID
    "condition_id",        # Market condition
    "tx_hash",             # Transaction hash
    # Window context
    "window_start",        # Unix ts of window start
    "window_elapsed_s",    # Seconds into window at time of trade
    "window_remaining_s",  # Seconds left in window
    "trade_seq",           # Sequence number within this window (1, 2, 3...)
    "trade_delta_s",       # Seconds since previous trade in this window
    # Cumulative window state AT TIME of this trade
    "cum_buy_up_usdc",     # Cumulative BUY UP volume
    "cum_buy_down_usdc",   # Cumulative BUY DOWN volume
    "cum_buy_bias",        # Current buy-side bias (0-1)
    "cum_trades",          # Total trades so far in window
    # BTC/ETH context
    "btc_price",           # BTC spot at time of trade
    "eth_price",           # ETH spot at time of trade
    "btc_at_window_start", # BTC price at the 5m window open
    "btc_pre_move",        # BTC move from window start to now ($)
    # Token mid-prices (both sides)
    "up_mid_price",        # Current Up token mid-market price
    "down_mid_price",      # Current Down token mid-market price
    # Source wallet
    "wallet",              # Matched wallet address
    "name",                # Market name / title
]


# ── Main Logger Class ─────────────────────────────────────────────

class AnalysisLogger:
    """Comprehensive trade logger for Bonereaper strategy analysis."""

    def __init__(self):
        self._lock = threading.Lock()
        self._windows: dict[str, WindowTracker] = {}
        self._price_thread: threading.Thread | None = None
        self._running = False

        # Previous window outcomes: {slug_prefix: "up"/"down"}
        # Used to detect streak-following or mean-reversion patterns
        self._prev_outcomes: dict[str, str] = {}
        self._prev_outcomes_lock = threading.Lock()

        # Stats
        self.total_trades_logged = 0
        self.total_windows_tracked = 0
        self.total_windows_resolved = 0

    def start(self):
        """Start the background price cache thread."""
        if self._running:
            return
        self._running = True
        self._price_thread = threading.Thread(
            target=self._price_loop, daemon=True)
        self._price_thread.start()
        # Initial price fetch
        _fetch_binance_prices()
        print("[ANALYSIS] Trade analysis logger started — "
              "logging all trades + BTC/ETH prices + token mids + resolution")

    def stop(self):
        self._running = False

    def _price_loop(self):
        """Background thread: keep BTC/ETH price cache fresh."""
        while self._running:
            _fetch_binance_prices()
            time.sleep(PRICE_CACHE_TTL)

    def log_trade(self, trade: dict):
        """Log a single trade from the RTDS feed.

        Called from the FollowFeed callback for EVERY trade (BUY + SELL).
        """
        slug = trade.get("slug", "")
        if not slug:
            return

        prices = _get_cached_prices()
        btc_price = prices.get("BTCUSDT", 0)
        eth_price = prices.get("ETHUSDT", 0)

        with self._lock:
            # Get or create window tracker
            if slug not in self._windows:
                self._windows[slug] = WindowTracker(slug)
                self.total_windows_tracked += 1
            win = self._windows[slug]

            # Add trade to window accumulator (returns inter-trade delta)
            trade_delta = win.add_trade(trade, btc_price, eth_price)

            # Build CSV row with cumulative window state
            trade_seq = win.buy_count + win.sell_count
            window_elapsed = 0
            window_remaining = 0
            if win.window_start > 0:
                now = trade.get("detected_at", time.time())
                window_elapsed = now - win.window_start
                window_remaining = max(0, win.window_duration - window_elapsed)

            # BTC pre-move: from window start to current
            btc_pre = 0
            if win.btc_at_window_start > 0 and btc_price > 0:
                btc_pre = btc_price - win.btc_at_window_start

            # Fetch mid-prices for both tokens (non-blocking, cached)
            up_mid = _fetch_mid_price(win.up_token_id) if win.up_token_id else 0
            down_mid = _fetch_mid_price(win.down_token_id) if win.down_token_id else 0

            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "slug": slug,
                "side": trade.get("side", ""),
                "outcome": trade.get("outcome", ""),
                "price": round(trade.get("price", 0), 4),
                "size": round(trade.get("size", 0), 2),
                "usdc_value": round(
                    trade.get("price", 0) * trade.get("size", 0), 2),
                "asset": trade.get("asset", ""),
                "condition_id": trade.get("condition_id", ""),
                "tx_hash": trade.get("tx_hash", ""),
                # Window context
                "window_start": win.window_start,
                "window_elapsed_s": round(window_elapsed, 1),
                "window_remaining_s": round(window_remaining, 1),
                "trade_seq": trade_seq,
                "trade_delta_s": round(trade_delta, 2),
                # Cumulative state
                "cum_buy_up_usdc": round(win.buy_up_usdc, 2),
                "cum_buy_down_usdc": round(win.buy_down_usdc, 2),
                "cum_buy_bias": round(win.buy_bias, 4),
                "cum_trades": trade_seq,
                # BTC/ETH
                "btc_price": round(btc_price, 2),
                "eth_price": round(eth_price, 2),
                "btc_at_window_start": round(win.btc_at_window_start, 2),
                "btc_pre_move": round(btc_pre, 2),
                # Token mid-prices
                "up_mid_price": round(up_mid, 4) if up_mid else "",
                "down_mid_price": round(down_mid, 4) if down_mid else "",
                # Source
                "wallet": trade.get("wallet", ""),
                "name": trade.get("name", "") or trade.get("title", ""),
            }

            self._write_csv(row)
            self.total_trades_logged += 1

            # Cleanup expired windows and save summaries
            self._cleanup()

    def _write_csv(self, row: dict):
        """Append a trade row to today's analysis CSV."""
        os.makedirs(LOG_DIR, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(LOG_DIR, f"analysis_trades_{today}.csv")
        file_exists = os.path.exists(path)

        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=ANALYSIS_FIELDS)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(
                    {k: row.get(k, "") for k in ANALYSIS_FIELDS})
        except Exception as e:
            print(f"[ANALYSIS] CSV write error: {e}")

    def _cleanup(self):
        """Remove expired windows, resolve outcomes, save summaries to JSON."""
        now = time.time()
        expired_slugs = []
        for slug, win in self._windows.items():
            if win.window_start <= 0:
                continue
            # Window ended + 2 min grace period (for resolution + final trades)
            if now > win.window_start + win.window_duration + 120:
                expired_slugs.append(slug)

        if not expired_slugs:
            return

        summaries = []
        for slug in expired_slugs:
            win = self._windows.pop(slug, None)
            if not win or (win.buy_count + win.sell_count == 0):
                continue

            # Resolve market outcome
            self._resolve_window(win)

            summary = win.to_summary()

            # Add previous window outcome (for streak analysis)
            prev_slug = self._get_previous_slug(slug)
            with self._prev_outcomes_lock:
                summary["prev_window_outcome"] = self._prev_outcomes.get(prev_slug)
                # Store this window's outcome for the next one
                if win.market_outcome:
                    self._prev_outcomes[slug] = win.market_outcome
                    # Keep bounded
                    if len(self._prev_outcomes) > 500:
                        keys = sorted(self._prev_outcomes.keys())[:250]
                        for k in keys:
                            self._prev_outcomes.pop(k, None)

            summaries.append(summary)

        if summaries:
            self._append_window_summaries(summaries)

    def _resolve_window(self, win: WindowTracker):
        """Fetch market resolution for a completed window."""
        try:
            from src.polymarket import fetch_market_outcome
            outcome = fetch_market_outcome(win.slug)
            if outcome:
                win.market_outcome = outcome
                self.total_windows_resolved += 1
        except Exception:
            pass

    @staticmethod
    def _get_previous_slug(slug: str) -> str:
        """Get the slug for the previous 5m window."""
        try:
            parts = slug.split("-")
            ts = int(parts[-1])
            duration = 900 if "15m" in slug else 300
            prev_ts = ts - duration
            parts[-1] = str(prev_ts)
            return "-".join(parts)
        except (ValueError, IndexError):
            return ""

    def _append_window_summaries(self, summaries: list[dict]):
        """Append completed window summaries to today's JSON file."""
        os.makedirs(LOG_DIR, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(LOG_DIR, f"analysis_windows_{today}.json")

        # Load existing summaries
        existing = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, Exception):
                existing = []

        existing.extend(summaries)

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            print(f"[ANALYSIS] JSON write error: {e}")

        for s in summaries:
            direction = s["net_direction"].upper()
            bias_pct = s["buy_bias_pct"]
            btc_move = s["btc_move_usd"]
            btc_pre = s.get("btc_pre_move_usd", 0)
            trades = s["total_trades"]
            buy_vol = s["total_buy_usdc"]
            sell_vol = s["total_sell_usdc"]
            outcome = s.get("market_outcome", "?")
            correct = s.get("direction_correct")
            correct_str = "CORRECT" if correct else "WRONG" if correct is False else "?"
            pnl = s.get("theoretical_pnl")
            pnl_str = f"${pnl:+.2f}" if pnl is not None else "?"
            prev = s.get("prev_window_outcome", "?")
            avg_delta = s.get("avg_trade_delta_s", 0)
            print(f"[ANALYSIS] {s['slug']}: {direction} {correct_str} "
                  f"bias={bias_pct:.0f}% "
                  f"BTC pre=${btc_pre:+.0f} Δ${btc_move:+.0f} "
                  f"trades={trades} avg_delta={avg_delta:.1f}s "
                  f"buy=${buy_vol:.0f} sell=${sell_vol:.0f} "
                  f"pnl={pnl_str} "
                  f"mkt={outcome} prev={prev}")

    # ------------------------------------------------------------------
    #  API / Dashboard
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        """Return logger status for dashboard API."""
        with self._lock:
            active_windows = []
            for slug, win in sorted(
                self._windows.items(),
                key=lambda x: x[1].last_trade_ts,
                reverse=True,
            ):
                if win.buy_count + win.sell_count > 0:
                    active_windows.append(win.to_summary())

            return {
                "total_trades_logged": self.total_trades_logged,
                "total_windows_tracked": self.total_windows_tracked,
                "total_windows_resolved": self.total_windows_resolved,
                "active_windows": len(active_windows),
                "windows": active_windows[:20],
                "btc_price": round(_price_cache.get("BTCUSDT", 0), 2),
                "eth_price": round(_price_cache.get("ETHUSDT", 0), 2),
                "price_cache_age_s": round(
                    time.time() - _price_cache_ts, 1) if _price_cache_ts else -1,
            }

    def get_window_summaries(self, date: str | None = None) -> list[dict]:
        """Load saved window summaries from disk for a given date."""
        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(LOG_DIR, f"analysis_windows_{date}.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            return []

    def flush_active_windows(self):
        """Force-flush all active windows to JSON (for shutdown/export)."""
        with self._lock:
            summaries = []
            for win in self._windows.values():
                if win.buy_count + win.sell_count > 0:
                    self._resolve_window(win)
                    summaries.append(win.to_summary())
            if summaries:
                self._append_window_summaries(summaries)
