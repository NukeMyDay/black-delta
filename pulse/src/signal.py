"""
Signal Module -- Real-time directional signal aggregation from RTDS trades.

Instead of copying individual trades (Follow Mode), this module aggregates
trades from monitored wallets into a per-window directional signal.

Flow:
  RTDS trade -> SignalAggregator.ingest(trade) -> per-window accumulator
  When bias crosses threshold -> signal emitted -> on_signal callback

Designed for wallets like Bonereaper that trade BOTH sides simultaneously
and build directional positions over time within each 5-min window.
"""

import threading
import time
from collections import deque
from datetime import datetime, timezone


class WindowAccumulator:
    """Accumulates trades for a single market window (slug)."""

    __slots__ = (
        "slug", "window_start", "window_duration",
        "up_usdc", "down_usdc", "up_shares", "down_shares",
        "trade_count", "first_trade_ts", "last_trade_ts",
        "signal_emitted", "signal_direction",
        "latency_samples",
    )

    def __init__(self, slug: str):
        self.slug = slug
        self.window_start = self._parse_window_start(slug)
        self.window_duration = 900 if "15m" in slug else 300
        self.up_usdc = 0.0
        self.down_usdc = 0.0
        self.up_shares = 0.0
        self.down_shares = 0.0
        self.trade_count = 0
        self.first_trade_ts = 0.0
        self.last_trade_ts = 0.0
        self.signal_emitted = False
        self.signal_direction: str | None = None
        self.latency_samples: list[float] = []

    @staticmethod
    def _parse_window_start(slug: str) -> int:
        try:
            return int(slug.split("-")[-1])
        except (ValueError, IndexError):
            return 0

    def add(self, trade: dict):
        outcome = trade.get("outcome", "").lower()
        usdc = trade.get("price", 0) * trade.get("size", 0)
        shares = trade.get("size", 0)

        if outcome == "up":
            self.up_usdc += usdc
            self.up_shares += shares
        elif outcome == "down":
            self.down_usdc += usdc
            self.down_shares += shares
        else:
            return

        self.trade_count += 1
        now = trade.get("detected_at", time.time())
        if self.first_trade_ts == 0:
            self.first_trade_ts = now
        self.last_trade_ts = now

        # Track RTDS latency
        rtds_ts = trade.get("timestamp", 0)
        if rtds_ts > 0 and now > 0:
            latency = (now - rtds_ts) * 1000  # ms
            if 0 < latency < 30_000:  # sanity: 0-30s
                self.latency_samples.append(latency)

    @property
    def total_usdc(self) -> float:
        return self.up_usdc + self.down_usdc

    @property
    def bias(self) -> float:
        """Directional bias: 0.0 = all Down, 0.5 = balanced, 1.0 = all Up."""
        total = self.total_usdc
        if total <= 0:
            return 0.5
        return self.up_usdc / total

    @property
    def dominant_direction(self) -> str:
        return "up" if self.bias >= 0.5 else "down"

    @property
    def dominant_pct(self) -> float:
        """How strong the dominant side is (50-100%)."""
        return max(self.bias, 1.0 - self.bias) * 100

    @property
    def window_elapsed(self) -> float:
        """Seconds elapsed since window start."""
        if self.window_start <= 0:
            return 0
        return time.time() - self.window_start

    @property
    def window_remaining(self) -> float:
        return max(0, self.window_duration - self.window_elapsed)

    @property
    def avg_latency_ms(self) -> float:
        if not self.latency_samples:
            return 0
        return sum(self.latency_samples) / len(self.latency_samples)

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "window_start": self.window_start,
            "window_duration": self.window_duration,
            "window_elapsed": round(self.window_elapsed, 1),
            "window_remaining": round(self.window_remaining, 1),
            "up_usdc": round(self.up_usdc, 2),
            "down_usdc": round(self.down_usdc, 2),
            "total_usdc": round(self.total_usdc, 2),
            "up_shares": round(self.up_shares, 1),
            "down_shares": round(self.down_shares, 1),
            "bias": round(self.bias, 4),
            "dominant": self.dominant_direction,
            "dominant_pct": round(self.dominant_pct, 1),
            "trade_count": self.trade_count,
            "signal_emitted": self.signal_emitted,
            "signal_direction": self.signal_direction,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
        }


class SignalAggregator:
    """Aggregates RTDS trades into per-window directional signals.

    Config:
        min_trades:      Minimum trades before a signal can fire
        min_usdc:        Minimum total USDC volume before signal fires
        bias_threshold:  Bias must exceed this to emit (0.5-1.0, default 0.65)
        min_elapsed_s:   Minimum seconds into window before signal fires
        only_5m:         Only process 5-minute markets (ignore 15m)
        sim_stake:       Simulated stake per signal for P&L tracking

    Callback:
        on_signal(signal_dict) is called once per window when conditions are met.
    """

    def __init__(self, **config):
        self._lock = threading.Lock()

        # Config with defaults
        self.min_trades = config.get("min_trades", 8)
        self.min_usdc = config.get("min_usdc", 50.0)
        self.bias_threshold = config.get("bias_threshold", 0.65)
        self.min_elapsed_s = config.get("min_elapsed_s", 30)
        self.only_5m = config.get("only_5m", True)

        # Active windows: slug -> WindowAccumulator
        self._windows: dict[str, WindowAccumulator] = {}

        # Signal history (most recent first)
        self.signals: deque[dict] = deque(maxlen=500)

        # Stats
        self.total_trades_ingested = 0
        self.total_signals_emitted = 0

        # Outcome tracking
        self.total_resolved = 0
        self.total_correct = 0
        self.total_wrong = 0
        self.sim_pnl = 0.0
        self.sim_total_invested = 0.0  # cumulative USDC deployed (1:1 with Bonereaper)
        self.pnl_curve: deque[dict] = deque(maxlen=2000)
        self.pnl_curve.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "roi_pct": 0.0, "pnl": 0.0, "invested": 0.0,
        })

        # Callback
        self.on_signal: callable | None = None

    # ------------------------------------------------------------------
    #  Config
    # ------------------------------------------------------------------
    def update_config(self, **kwargs):
        if "min_trades" in kwargs:
            self.min_trades = max(1, int(kwargs["min_trades"]))
        if "min_usdc" in kwargs:
            self.min_usdc = max(0, float(kwargs["min_usdc"]))
        if "bias_threshold" in kwargs:
            self.bias_threshold = max(0.51, min(0.99, float(kwargs["bias_threshold"])))
        if "min_elapsed_s" in kwargs:
            self.min_elapsed_s = max(0, float(kwargs["min_elapsed_s"]))
        if "only_5m" in kwargs:
            self.only_5m = bool(kwargs["only_5m"])
        # sim_stake removed — stake is now 1:1 with actual source volume

    # ------------------------------------------------------------------
    #  Trade ingestion (called from FollowFeed callback)
    # ------------------------------------------------------------------
    def ingest(self, trade: dict):
        """Process a single RTDS trade into the accumulator.

        This is the main entry point -- wire it as a second callback
        alongside the existing follow mode handler.
        """
        slug = trade.get("slug", "")
        if not slug:
            return

        # Filter: only BUY trades (SELL doesn't contribute to direction signal)
        if trade.get("side", "").upper() != "BUY":
            return

        # Filter: only 5m if configured (check for -5m- to exclude 15m)
        if self.only_5m and "-5m-" not in slug:
            return

        # Filter: only BTC/ETH up/down markets (covers both "updown" and "up-down" slug variants)
        if "updown" not in slug and "up-down" not in slug:
            return

        with self._lock:
            self.total_trades_ingested += 1

            # Get or create window accumulator
            if slug not in self._windows:
                self._windows[slug] = WindowAccumulator(slug)
            win = self._windows[slug]
            win.add(trade)

            # Check if signal should fire
            if not win.signal_emitted:
                signal = self._check_signal(win)
                if signal:
                    win.signal_emitted = True
                    win.signal_direction = signal["direction"]
                    self.signals.appendleft(signal)
                    self.total_signals_emitted += 1

                    # Fire callback outside lock to avoid deadlocks
                    callback = self.on_signal
                    if callback:
                        try:
                            callback(signal)
                        except Exception as e:
                            print(f"[SIGNAL] Callback error: {e}")

            # Cleanup expired windows (older than 10 minutes)
            self._cleanup()

    def _check_signal(self, win: WindowAccumulator) -> dict | None:
        """Check if a window has accumulated enough to emit a signal."""
        if win.trade_count < self.min_trades:
            return None
        if win.total_usdc < self.min_usdc:
            return None
        if win.window_elapsed < self.min_elapsed_s:
            return None
        if win.dominant_pct / 100 < self.bias_threshold:
            return None

        # Window still has time left? (don't signal expired markets)
        if win.window_remaining <= 0:
            return None

        direction = win.dominant_direction
        bias = win.bias if direction == "up" else (1.0 - win.bias)

        # Confidence: composite score from bias strength, trade count, and volume
        bias_score = (bias - 0.5) / 0.5  # 0.0 at 50%, 1.0 at 100%
        count_score = min(1.0, win.trade_count / 30)  # saturates at 30 trades
        volume_score = min(1.0, win.total_usdc / 300)  # saturates at $300
        confidence = round(bias_score * 0.5 + count_score * 0.25 + volume_score * 0.25, 3)

        return {
            "slug": win.slug,
            "event_slug": win.slug,  # compatible with PULSE
            "direction": direction,
            "confidence": confidence,
            "bias": round(bias, 4),
            "up_usdc": round(win.up_usdc, 2),
            "down_usdc": round(win.down_usdc, 2),
            "total_usdc": round(win.total_usdc, 2),
            "trade_count": win.trade_count,
            "window_elapsed_s": round(win.window_elapsed, 1),
            "window_remaining_s": round(win.window_remaining, 1),
            "avg_latency_ms": round(win.avg_latency_ms, 1),
            "outcome": "pending",
            "market_winner": None,
            "sim_pnl": 0,
            "sim_entry_price": 0,
            "emitted_at": time.time(),
            "time": datetime.now(timezone.utc).isoformat(),
        }

    def _cleanup(self):
        """Remove windows older than 10 minutes past their end."""
        now = time.time()
        expired = [
            slug for slug, win in self._windows.items()
            if win.window_start > 0
            and now > win.window_start + win.window_duration + 600
        ]
        for slug in expired:
            del self._windows[slug]

    # ------------------------------------------------------------------
    #  API / Dashboard
    # ------------------------------------------------------------------
    def get_snapshot(self) -> dict:
        """Return current state for the dashboard API."""
        with self._lock:
            # Active windows sorted by recency
            active = sorted(
                self._windows.values(),
                key=lambda w: w.last_trade_ts,
                reverse=True,
            )
            # Only show windows that still have time or recent
            now = time.time()
            active_dicts = [
                w.to_dict() for w in active
                if w.window_start > 0
                and now < w.window_start + w.window_duration + 120
            ]

            # Latest latency across all active windows
            all_latencies = []
            for w in active:
                all_latencies.extend(w.latency_samples)
            avg_latency = (sum(all_latencies) / len(all_latencies)
                          if all_latencies else 0)

            accuracy = (self.total_correct / self.total_resolved * 100
                       if self.total_resolved > 0 else 0)

            return {
                "config": {
                    "min_trades": self.min_trades,
                    "min_usdc": self.min_usdc,
                    "bias_threshold": self.bias_threshold,
                    "min_elapsed_s": self.min_elapsed_s,
                    "only_5m": self.only_5m,
                },
                "stats": {
                    "total_trades_ingested": self.total_trades_ingested,
                    "total_signals_emitted": self.total_signals_emitted,
                    "active_windows": len(active_dicts),
                    "avg_latency_ms": round(avg_latency, 1),
                    "total_resolved": self.total_resolved,
                    "correct": self.total_correct,
                    "wrong": self.total_wrong,
                    "accuracy": round(accuracy, 1),
                    "sim_pnl": round(self.sim_pnl, 2),
                    "sim_total_invested": round(self.sim_total_invested, 2),
                    "roi_pct": round(self.sim_pnl / self.sim_total_invested * 100, 2)
                              if self.sim_total_invested > 0 else 0,
                },
                "windows": active_dicts,
                "signals": list(self.signals),
                "pnl_curve": list(self.pnl_curve),
            }

    # ------------------------------------------------------------------
    #  Outcome Resolution
    # ------------------------------------------------------------------
    def resolve_signal(self, slug: str, market_winner: str):
        """Resolve a signal's outcome after the market settles.

        Args:
            slug: Market slug
            market_winner: "up" or "down" (actual market result)
        """
        with self._lock:
            for sig in self.signals:
                if sig.get("slug") == slug and sig.get("outcome") == "pending":
                    correct = sig["direction"] == market_winner
                    sig["outcome"] = "correct" if correct else "wrong"
                    sig["market_winner"] = market_winner

                    # Simulated P&L: 1:1 with Bonereaper's actual window volume
                    stake = sig["total_usdc"]  # what he actually deployed
                    entry_price = sig.get("bias", 0.65)
                    if correct:
                        payout_mult = 1.0 / entry_price if entry_price > 0 else 1.0
                        pnl = stake * (payout_mult - 1)
                        self.total_correct += 1
                    else:
                        pnl = -stake
                        self.total_wrong += 1

                    sig["sim_pnl"] = round(pnl, 2)
                    sig["sim_entry_price"] = round(entry_price, 4)
                    self.sim_pnl += pnl
                    self.sim_total_invested += stake
                    self.total_resolved += 1

                    roi_pct = (self.sim_pnl / self.sim_total_invested * 100
                               if self.sim_total_invested > 0 else 0)
                    self.pnl_curve.append({
                        "time": datetime.now(timezone.utc).isoformat(),
                        "roi_pct": round(roi_pct, 2),
                        "pnl": round(self.sim_pnl, 2),
                        "invested": round(self.sim_total_invested, 2),
                    })
                    break

    def get_pending_slugs(self) -> list[str]:
        """Return slugs of signals awaiting resolution."""
        with self._lock:
            return list(set(
                sig["slug"] for sig in self.signals
                if sig.get("outcome") == "pending"
            ))

    def get_accuracy_stats(self) -> dict:
        """Return accuracy and simulated P&L statistics."""
        with self._lock:
            accuracy = (self.total_correct / self.total_resolved * 100
                        if self.total_resolved > 0 else 0)

            # Breakdown by confidence bucket
            buckets = {"low": [0, 0], "mid": [0, 0], "high": [0, 0]}
            for sig in self.signals:
                if sig.get("outcome") not in ("correct", "wrong"):
                    continue
                conf = sig.get("confidence", 0)
                if conf < 0.4:
                    key = "low"
                elif conf < 0.7:
                    key = "mid"
                else:
                    key = "high"
                buckets[key][0] += 1  # total
                if sig["outcome"] == "correct":
                    buckets[key][1] += 1  # correct

            conf_breakdown = {}
            for key, (total, correct) in buckets.items():
                conf_breakdown[key] = {
                    "total": total,
                    "correct": correct,
                    "accuracy": round(correct / total * 100, 1) if total > 0 else 0,
                }

            roi_pct = (self.sim_pnl / self.sim_total_invested * 100
                       if self.sim_total_invested > 0 else 0)
            return {
                "total_resolved": self.total_resolved,
                "correct": self.total_correct,
                "wrong": self.total_wrong,
                "pending": sum(1 for s in self.signals if s.get("outcome") == "pending"),
                "accuracy": round(accuracy, 1),
                "sim_pnl": round(self.sim_pnl, 2),
                "sim_total_invested": round(self.sim_total_invested, 2),
                "roi_pct": round(roi_pct, 2),
                "confidence_breakdown": conf_breakdown,
                "pnl_curve": list(self.pnl_curve),
            }

    def get_active_signal(self, slug: str) -> dict | None:
        """Get the current signal for a specific market slug, if any."""
        with self._lock:
            win = self._windows.get(slug)
            if not win or not win.signal_emitted:
                return None
            # Find the matching signal
            for sig in self.signals:
                if sig["slug"] == slug:
                    return sig
            return None
