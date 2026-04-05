"""
Signal Module -- Real-time directional signal aggregation from RTDS trades.

Instead of copying individual trades (Follow Mode), this module aggregates
trades from monitored wallets into a per-window directional signal.

Flow:
  RTDS trade -> SignalAggregator.ingest(trade) -> per-window accumulator
  Scout (30s / 8 trades) -> Direction Flip Check (90s / 15+ trades) -> Bet

Designed for wallets like Bonereaper that trade BOTH sides simultaneously
and build directional positions over time within each 5-min window.

Strategy (Scout + Flip Detection + Entry-Price Sizing):
  1. Scout at 30s / 8 trades: record dominant direction (internal, no bet)
  2. At 90s / 15+ trades: check if direction flipped since scout
     - If flipped -> suppress signal (bias was unstable)
     - If stable -> compute entry price, size bet accordingly
  3. Entry-price-based Kelly sizing:
     - avg_entry < 0.45: full ⅛-Kelly (best EV, contrarian)
     - 0.45 <= avg_entry < 0.55: half ⅛-Kelly (moderate EV)
     - avg_entry >= 0.55: skip (edge evaporates after slippage)

  Result: 1 signal per window max. Direction flips = no bet.
"""

import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

# --- Bet Sizing (all overridable via .env) ---
# Separate win rates per tier (empirical from 47 signals):
#   Full tier (entry < 0.45): 57% accuracy — contrarian bets, best EV
#   Half tier (0.45-0.55):    75% accuracy — moderate conviction
#   Skip (>= 0.55):          No bet — edge evaporates after slippage
WIN_RATE_FULL = float(os.getenv("SIGNAL_WIN_RATE_FULL", "0.57"))
WIN_RATE_HALF = float(os.getenv("SIGNAL_WIN_RATE_HALF", "0.75"))
KELLY_SAFETY = float(os.getenv("SIGNAL_KELLY_FRACTION", "0.125"))  # ⅛-Kelly (conservative for €1k)
ENTRY_FULL = float(os.getenv("SIGNAL_ENTRY_FULL", "0.60"))
ENTRY_HALF = float(os.getenv("SIGNAL_ENTRY_HALF", "0.70"))
SIM_START_CAPITAL = float(os.getenv("SIGNAL_START_CAPITAL", "1000.0"))

# --- Scout + Bet Thresholds ---
SCOUT_MIN_ELAPSED = int(os.getenv("SIGNAL_SCOUT_MIN_ELAPSED", "30"))
SCOUT_MIN_TRADES = int(os.getenv("SIGNAL_SCOUT_MIN_TRADES", "8"))
BET_MIN_ELAPSED = int(os.getenv("SIGNAL_BET_MIN_ELAPSED", "90"))
BET_MIN_TRADES = int(os.getenv("SIGNAL_BET_MIN_TRADES", "15"))


class WindowAccumulator:
    """Accumulates trades for a single market window (slug).

    Two internal stages:
      1. Scout (30s/8 trades): records direction, internal only
      2. Bet (90s/15+ trades): emits signal if direction stable + entry OK
    """

    __slots__ = (
        "slug", "window_start", "window_duration",
        "up_usdc", "down_usdc", "up_shares", "down_shares",
        "trade_count", "first_trade_ts", "last_trade_ts",
        "scouted", "scout_direction",
        "bet_emitted", "signal_direction",
        "direction_flipped", "skip_reason",
        "latency_samples",
        "up_asset", "down_asset",
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
        self.scouted: bool = False
        self.scout_direction: str | None = None
        self.bet_emitted: bool = False
        self.signal_direction: str | None = None
        self.direction_flipped: bool = False
        self.skip_reason: str | None = None  # "flip", "entry_high", "bias_low"
        self.latency_samples: list[float] = []
        self.up_asset: str = ""    # token_id for UP outcome
        self.down_asset: str = ""  # token_id for DOWN outcome

    @property
    def signal_emitted(self) -> bool:
        """Backward compat: True if a bet signal was emitted."""
        return self.bet_emitted

    @property
    def status(self) -> str:
        """Human-readable window status."""
        if self.direction_flipped:
            return "flipped"
        if self.bet_emitted:
            return "confirmed"
        if self.skip_reason:
            return "skipped"
        if self.scouted:
            return "scouting"
        return "accumulating"

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
            if not self.up_asset:
                self.up_asset = trade.get("asset", "")
        elif outcome == "down":
            self.down_usdc += usdc
            self.down_shares += shares
            if not self.down_asset:
                self.down_asset = trade.get("asset", "")
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

    def get_avg_entry_price(self, direction: str) -> float:
        """Average entry price for the dominant direction."""
        if direction == "up":
            price = (self.up_usdc / self.up_shares
                     if self.up_shares > 0 else 0.5)
        else:
            price = (self.down_usdc / self.down_shares
                     if self.down_shares > 0 else 0.5)
        return max(0.01, min(0.99, price))

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
            "signal_emitted": self.bet_emitted,
            "signal_direction": self.signal_direction,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            # Scout + Flip Detection
            "status": self.status,
            "scouted": self.scouted,
            "scout_direction": self.scout_direction,
            "direction_flipped": self.direction_flipped,
            "skip_reason": self.skip_reason,
        }


class SignalAggregator:
    """Aggregates RTDS trades into per-window directional signals.

    Strategy: Scout → Flip Check → Entry-Price-Based Bet (max 1 per window).

    Config:
        min_usdc:        Minimum total USDC volume before any action
        bias_threshold:  Bias must exceed this to emit (0.5-1.0, default 0.65)
        only_5m:         Only process 5-minute markets (ignore 15m)

    Callback:
        on_signal(signal_dict) is called once per window when conditions are met.
    """

    def __init__(self, **config):
        self._lock = threading.Lock()

        # Config with defaults
        self.min_usdc = config.get("min_usdc", 50.0)
        self.bias_threshold = config.get("bias_threshold", 0.65)
        self.only_5m = config.get("only_5m", True)

        # Active windows: slug -> WindowAccumulator
        self._windows: dict[str, WindowAccumulator] = {}

        # Signal history (most recent first) — only actual bet signals
        self.signals: deque[dict] = deque(maxlen=500)

        # Stats
        self.total_trades_ingested = 0
        self.total_signals_emitted = 0
        self.total_scouts = 0
        self.total_flips = 0
        self.total_skips = 0

        # Outcome tracking
        self.total_resolved = 0
        self.total_correct = 0
        self.total_wrong = 0
        self.sim_pnl = 0.0
        self.sim_capital = SIM_START_CAPITAL
        self.sim_total_invested = 0.0
        self.pnl_curve: deque[dict] = deque(maxlen=2000)
        self.pnl_curve.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "capital": SIM_START_CAPITAL, "pnl": 0.0,
        })

        # Callback
        self.on_signal: callable | None = None

    # ------------------------------------------------------------------
    #  Config
    # ------------------------------------------------------------------
    def update_config(self, **kwargs):
        if "min_usdc" in kwargs:
            self.min_usdc = max(0, float(kwargs["min_usdc"]))
        if "bias_threshold" in kwargs:
            self.bias_threshold = max(0.51, min(0.99, float(kwargs["bias_threshold"])))
        if "only_5m" in kwargs:
            self.only_5m = bool(kwargs["only_5m"])
        # Legacy compat: ignore min_trades / min_elapsed_s (now fixed constants)

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

            # Check for signal progression
            signal = self._check_signal(win)
            if signal:
                self.signals.appendleft(signal)
                self.total_signals_emitted += 1

                callback = self.on_signal
                if callback:
                    try:
                        callback(signal)
                    except Exception as e:
                        print(f"[SIGNAL] Callback error: {e}")

            # Cleanup expired windows (older than 10 minutes)
            self._cleanup()

    def _check_signal(self, win: WindowAccumulator) -> dict | None:
        """Two-stage signal check: Scout → Flip Check → Bet.

        Returns a signal dict if a bet should be placed, None otherwise.
        Scout is internal only (no signal emitted to history).
        """
        # Already emitted or terminated?
        if win.bet_emitted or win.direction_flipped or win.skip_reason:
            return None

        # Window expired?
        if win.window_remaining <= 0:
            return None

        # Minimum volume
        if win.total_usdc < self.min_usdc:
            return None

        direction = win.dominant_direction
        bias = win.bias if direction == "up" else (1.0 - win.bias)

        # --- Stage 1: Scout ---
        if not win.scouted:
            if (win.trade_count >= SCOUT_MIN_TRADES
                    and win.window_elapsed >= SCOUT_MIN_ELAPSED
                    and bias >= self.bias_threshold):
                win.scouted = True
                win.scout_direction = direction
                self.total_scouts += 1
                print(f"[SIGNAL] SCOUT: {direction.upper()} {bias:.0%} "
                      f"on {win.slug} ({win.trade_count} trades, "
                      f"{win.window_elapsed:.0f}s)")
            return None  # Scout never emits a signal

        # --- Stage 2: Bet check ---
        if win.trade_count < BET_MIN_TRADES:
            return None
        if win.window_elapsed < BET_MIN_ELAPSED:
            return None

        # Direction flip check
        if direction != win.scout_direction:
            win.direction_flipped = True
            win.skip_reason = "flip"
            self.total_flips += 1
            print(f"[SIGNAL] DIRECTION FLIP on {win.slug}: "
                  f"scout={win.scout_direction.upper()} → "
                  f"now={direction.upper()} — NO BET")
            return None

        # Bias threshold check (may have weakened since scout)
        if bias < self.bias_threshold:
            win.skip_reason = "bias_low"
            self.total_skips += 1
            print(f"[SIGNAL] BIAS TOO LOW on {win.slug}: "
                  f"{bias:.0%} < {self.bias_threshold:.0%} — NO BET")
            return None

        # Compute entry price and bet sizing
        avg_entry_price = win.get_avg_entry_price(direction)
        entry_tier, suggested_bet_pct, suggested_bet_usd = \
            self._compute_bet_sizing(avg_entry_price)

        if entry_tier == "skip":
            win.skip_reason = "entry_high"
            self.total_skips += 1
            print(f"[SIGNAL] ENTRY TOO HIGH on {win.slug}: "
                  f"${avg_entry_price:.2f} >= ${ENTRY_HALF:.2f} — NO BET")
            return None

        if suggested_bet_usd < 1.0:
            win.skip_reason = "no_edge"
            self.total_skips += 1
            print(f"[SIGNAL] NO KELLY EDGE on {win.slug}: "
                  f"${avg_entry_price:.2f} @ {entry_tier} → $0 bet — SKIP")
            return None

        # --- Emit signal ---
        win.bet_emitted = True
        win.signal_direction = direction

        # Confidence: composite score from bias strength, trade count, and volume
        bias_score = (bias - 0.5) / 0.5  # 0.0 at 50%, 1.0 at 100%
        count_score = min(1.0, win.trade_count / 30)  # saturates at 30 trades
        volume_score = min(1.0, win.total_usdc / 300)  # saturates at $300
        confidence = round(bias_score * 0.5 + count_score * 0.25
                          + volume_score * 0.25, 3)

        print(f"[SIGNAL] BET: {direction.upper()} on {win.slug} "
              f"(entry=${avg_entry_price:.2f}, tier={entry_tier}, "
              f"bet=${suggested_bet_usd:.2f}, bias={bias:.0%}, "
              f"{win.trade_count} trades, {win.window_elapsed:.0f}s)")

        token_id = win.up_asset if direction == "up" else win.down_asset

        return {
            "slug": win.slug,
            "event_slug": win.slug,  # compatible with PULSE
            "direction": direction,
            "token_id": token_id,
            "confidence": confidence,
            "bias": round(bias, 4),
            "avg_entry_price": round(avg_entry_price, 4),
            "entry_tier": entry_tier,
            "suggested_bet_pct": suggested_bet_pct,
            "suggested_bet_usd": suggested_bet_usd,
            "scout_direction": win.scout_direction,
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

    def _compute_bet_sizing(self, avg_entry_price: float):
        """Compute bet size using Kelly with tier-specific win rates.

        Returns (entry_tier, bet_pct, bet_usd).
        Each tier uses its own empirical win rate for Kelly calculation:
          "full"  (entry < 0.45): WIN_RATE_FULL (57%) — contrarian, best EV
          "half"  (0.45-0.55):    WIN_RATE_HALF (75%) — moderate conviction
          "skip"  (>= 0.55):     no bet — edge evaporates after slippage
        """
        if avg_entry_price >= ENTRY_HALF:
            return "skip", 0.0, 0.0

        # Net payout odds: win $1/share at cost avg_entry_price
        b = (1.0 / avg_entry_price - 1) if avg_entry_price > 0.01 else 0
        if b <= 0:
            return "skip", 0.0, 0.0

        # Tier-specific win rate — Kelly sizes correctly per population
        if avg_entry_price < ENTRY_FULL:
            tier = "full"
            p = WIN_RATE_FULL
        else:
            tier = "half"
            p = WIN_RATE_HALF

        q = 1 - p
        kelly = max(0, (p * b - q) / b)
        fraction = kelly * KELLY_SAFETY

        bet_usd = round(self.sim_capital * fraction, 2)
        return tier, round(fraction * 100, 2), bet_usd

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

            # Entry tier breakdown (only resolved signals)
            tier_full = {"total": 0, "correct": 0}
            tier_half = {"total": 0, "correct": 0}
            for s in self.signals:
                if s.get("outcome") not in ("correct", "wrong"):
                    continue
                tier = s.get("entry_tier", "")
                if tier == "full":
                    tier_full["total"] += 1
                    if s["outcome"] == "correct":
                        tier_full["correct"] += 1
                elif tier == "half":
                    tier_half["total"] += 1
                    if s["outcome"] == "correct":
                        tier_half["correct"] += 1
            tier_full["accuracy"] = (round(tier_full["correct"] / tier_full["total"] * 100, 1)
                                     if tier_full["total"] > 0 else 0)
            tier_half["accuracy"] = (round(tier_half["correct"] / tier_half["total"] * 100, 1)
                                     if tier_half["total"] > 0 else 0)

            # Count direction flips in active windows
            direction_flips = sum(
                1 for w in self._windows.values() if w.direction_flipped)

            roi_pct = ((self.sim_capital - SIM_START_CAPITAL) /
                       SIM_START_CAPITAL * 100)

            return {
                "config": {
                    "min_usdc": self.min_usdc,
                    "bias_threshold": self.bias_threshold,
                    "only_5m": self.only_5m,
                    "scout_min_trades": SCOUT_MIN_TRADES,
                    "scout_min_elapsed": SCOUT_MIN_ELAPSED,
                    "bet_min_trades": BET_MIN_TRADES,
                    "bet_min_elapsed": BET_MIN_ELAPSED,
                    "entry_full": ENTRY_FULL,
                    "entry_half": ENTRY_HALF,
                    "win_rate_full": WIN_RATE_FULL,
                    "win_rate_half": WIN_RATE_HALF,
                    "kelly_fraction": KELLY_SAFETY,
                },
                "stats": {
                    "total_trades_ingested": self.total_trades_ingested,
                    "total_signals_emitted": self.total_signals_emitted,
                    "total_scouts": self.total_scouts,
                    "total_flips": self.total_flips,
                    "total_skips": self.total_skips,
                    "active_windows": len(active_dicts),
                    "avg_latency_ms": round(avg_latency, 1),
                    "total_resolved": self.total_resolved,
                    "correct": self.total_correct,
                    "wrong": self.total_wrong,
                    "accuracy": round(accuracy, 1),
                    "sim_pnl": round(self.sim_pnl, 2),
                    "sim_capital": round(self.sim_capital, 2),
                    "roi_pct": round(roi_pct, 2),
                    "direction_flips": direction_flips,
                    "entry_tier_full": tier_full,
                    "entry_tier_half": tier_half,
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

                    entry_price = sig.get("avg_entry_price", 0.5)
                    stake = sig.get("suggested_bet_usd", 0)

                    if correct:
                        self.total_correct += 1
                    else:
                        self.total_wrong += 1
                    self.total_resolved += 1

                    if stake > 0:
                        payout_mult = 1.0 / entry_price if entry_price > 0 else 1.0
                        pnl = stake * (payout_mult - 1) if correct else -stake
                        sig["sim_pnl"] = round(pnl, 2)
                        self.sim_pnl += pnl
                        self.sim_capital += pnl
                        self.sim_total_invested += stake
                    else:
                        sig["sim_pnl"] = 0

                    sig["sim_entry_price"] = round(entry_price, 4)

                    self.pnl_curve.append({
                        "time": datetime.now(timezone.utc).isoformat(),
                        "capital": round(self.sim_capital, 2),
                        "pnl": round(self.sim_pnl, 2),
                    })
                    break  # 1 signal per window

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

            roi_pct = ((self.sim_capital - SIM_START_CAPITAL) /
                       SIM_START_CAPITAL * 100)
            return {
                "total_resolved": self.total_resolved,
                "correct": self.total_correct,
                "wrong": self.total_wrong,
                "pending": sum(1 for s in self.signals if s.get("outcome") == "pending"),
                "accuracy": round(accuracy, 1),
                "sim_pnl": round(self.sim_pnl, 2),
                "sim_capital": round(self.sim_capital, 2),
                "roi_pct": round(roi_pct, 2),
                "confidence_breakdown": conf_breakdown,
                "pnl_curve": list(self.pnl_curve),
            }

    def get_active_signal(self, slug: str) -> dict | None:
        """Get the active signal for a specific market slug, if any."""
        with self._lock:
            win = self._windows.get(slug)
            if not win or not win.bet_emitted:
                return None
            for sig in self.signals:
                if sig["slug"] == slug:
                    return sig
            return None
