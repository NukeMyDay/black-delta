"""
PULSE Strategy — BTC 5-minute Up/Down momentum betting.

Uses Binance Futures price lead over Polymarket to bet on BTC direction.
If BTC has already moved away from the target price, bet on continuation.

Core logic:
  1. At window start, read BTC price (Binance) and target price (Polymarket)
  2. If BTC > target → bet UP, if BTC < target → bet DOWN
  3. Apply filters: min distance ($3), entry price range ($0.50–$0.70)
  4. Dynamic sizing via 1/8-Kelly with adaptive win rate:
     - Base WR 62% (conservative)
     - Entry boost: cheaper entry = stronger signal → up to +5% WR
     - Distance boost: bigger BTC move = more momentum → up to +3% WR
     - Effective WR range: 62%–70% depending on signal quality
  5. FAK order with $0.02 slippage assumption

Paper mode by default — logs decisions without placing orders.
"""

import logging
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone

from src.binance_ws import BinancePriceFeed
from src.chainlink import get_btc_price as chainlink_btc_price
from src.polymarket import (
    fetch_market, fetch_midpoint, parse_market_data,
    get_current_event_slug,
)

log = logging.getLogger("pulse_strategy")

# Strategy parameters
SLIPPAGE = 0.02           # $0.02 assumed FAK slippage
ENTRY_MIN = 0.50          # Minimum entry price (filter noise)
ENTRY_MAX = 0.70          # Maximum entry price (filter low payout)
MIN_DISTANCE = 3.0        # Minimum |BTC - target| in USD
KELLY_FRACTION = 1 / 8    # Conservative Kelly fraction
MIN_STAKE = 1.0           # Minimum bet size
DEFAULT_CAPITAL = 1000.0  # Paper trading capital
DEFAULT_MAX_BET = 50.0    # Default max bet cap in USD

# Dynamic win rate parameters
BASE_WIN_RATE = 0.62      # Conservative baseline WR
ENTRY_BOOST_MAX = 0.05    # Max WR boost from cheap entry (+5% at $0.50)
DIST_BOOST_MAX = 0.03     # Max WR boost from large distance (+3% at $50+)
DIST_BOOST_FULL = 50.0    # Distance in USD for full distance boost


class PulseBet:
    """A single PULSE bet record."""

    def __init__(self, slug: str, direction: str, btc_price: float,
                 target_price: float, entry_price: float, stake: float,
                 edge_pct: float, distance: float, effective_wr: float = 0.0):
        self.time = datetime.now(timezone.utc).isoformat()
        self.slug = slug
        self.direction = direction
        self.btc_price = round(btc_price, 2)
        self.target_price = round(target_price, 2)
        self.entry_price = round(entry_price, 4)
        self.stake = round(stake, 2)
        self.edge_pct = round(edge_pct, 1)
        self.distance = round(distance, 2)
        self.effective_wr = round(effective_wr, 3)
        self.outcome: str = "pending"  # pending | win | lose | skip
        self.pnl: float = 0.0
        self.skip_reason: str | None = None
        self.window_start: int = 0

    def to_dict(self) -> dict:
        return {
            "time": self.time,
            "slug": self.slug,
            "direction": self.direction,
            "btc_price": self.btc_price,
            "target_price": self.target_price,
            "entry_price": self.entry_price,
            "stake": self.stake,
            "edge_pct": self.edge_pct,
            "distance": self.distance,
            "effective_wr": self.effective_wr,
            "outcome": self.outcome,
            "pnl": self.pnl,
            "skip_reason": self.skip_reason,
            "window_start": self.window_start,
        }


class PulseStrategy:
    """PULSE momentum strategy for BTC 5-min Up/Down markets."""

    def __init__(self, btc_feed: BinancePriceFeed, executor=None):
        self.btc_feed = btc_feed
        self.executor = executor
        self.paper_mode = True  # Always paper for now

        # Configurable limits (settable via dashboard)
        self.max_bet_cap: float = DEFAULT_MAX_BET

        # Capital tracking (paper)
        self.paper_capital = DEFAULT_CAPITAL
        self.starting_capital = DEFAULT_CAPITAL

        # Stats
        self.total_bets = 0
        self.wins = 0
        self.losses = 0
        self.skips = 0
        self.total_pnl = 0.0
        self.daily_pnl = 0.0
        self._daily_date: str = ""

        # Bet history
        self.bets: deque[PulseBet] = deque(maxlen=500)
        self.pending_bets: dict[str, PulseBet] = {}  # slug -> bet

        # Current window state
        self.current_slug: str | None = None
        self.current_window_start: int = 0

        # Window start price cache (target reference per window)
        self._window_start_prices: dict[int, float] = {}

        # Thread control
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_processed_window: int = 0

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start the PULSE strategy loop."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="pulse-strategy",
            daemon=True,
        )
        self._thread.start()
        log.info("PULSE strategy started (paper=%s)", self.paper_mode)

    def stop(self):
        """Stop the strategy loop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("PULSE strategy stopped")

    def get_status(self) -> dict:
        """Return current status for dashboard."""
        with self._lock:
            total = self.wins + self.losses
            wr = round(self.wins / max(total, 1) * 100, 1)
            avg_pnl = round(self.total_pnl / max(total, 1), 2) if total > 0 else 0
            total_staked = sum(
                b.stake for b in self.bets
                if b.outcome in ("win", "lose")
            )
            roi = round(self.total_pnl / max(total_staked, 1) * 100, 1) if total_staked > 0 else 0

            # Current window info
            current = None
            if self.current_slug and self.current_slug in self.pending_bets:
                bet = self.pending_bets[self.current_slug]
                now = time.time()
                remaining = max(0, (bet.window_start + 300) - now)
                current = {
                    **bet.to_dict(),
                    "remaining": round(remaining),
                }

            # Chainlink price (for dashboard display)
            cl_price = chainlink_btc_price()

            return {
                "running": self._running,
                "paper_mode": self.paper_mode,
                "capital": round(self.paper_capital, 2),
                "starting_capital": round(self.starting_capital, 2),
                "chainlink_btc": round(cl_price, 2) if cl_price > 0 else 0,
                "total_pnl": round(self.total_pnl, 2),
                "daily_pnl": round(self.daily_pnl, 2),
                "total_bets": total,
                "total_skips": self.skips,
                "wins": self.wins,
                "losses": self.losses,
                "win_rate": wr,
                "roi": roi,
                "avg_pnl": avg_pnl,
                "current_window": current,
                "recent_bets": [b.to_dict() for b in list(self.bets)[-30:]],
                "max_bet_cap": self.max_bet_cap,
                "filters": {
                    "entry_min": ENTRY_MIN,
                    "entry_max": ENTRY_MAX,
                    "min_distance": MIN_DISTANCE,
                    "slippage": SLIPPAGE,
                    "base_wr": BASE_WIN_RATE,
                    "kelly_fraction": KELLY_FRACTION,
                },
            }

    # ------------------------------------------------------------------
    #  Main Loop
    # ------------------------------------------------------------------

    def _run_loop(self):
        """Main strategy loop — check every second, act at window boundaries."""
        print("[PULSE] Strategy loop started")
        while self._running:
            try:
                now = int(time.time())
                window_start = now - (now % 300)

                # New window? Process it
                if window_start > self._last_processed_window:
                    # First resolve previous window
                    self._resolve_completed()

                    # Snapshot Chainlink BTC price at window boundary.
                    # This is the EXACT oracle Polymarket uses for resolution.
                    # Binance may differ on close calls — Chainlink is ground truth.
                    cl_price = chainlink_btc_price()
                    if cl_price > 0:
                        self._window_start_prices[window_start] = cl_price
                    else:
                        # Fallback to Binance if Chainlink is unreachable
                        snap_price = self.btc_feed.btc_price
                        if snap_price > 0:
                            self._window_start_prices[window_start] = snap_price

                    # Wait 3 seconds for BTC to move from target
                    time.sleep(3)

                    # Process new window
                    self._process_window(window_start)
                    self._last_processed_window = window_start

                time.sleep(1)
            except Exception as e:
                log.error("PULSE loop error: %s", e)
                time.sleep(5)

    def _process_window(self, window_start: int):
        """Evaluate and potentially bet on a new 5-min window."""
        slug = f"btc-updown-5m-{window_start}"
        self.current_slug = slug
        self.current_window_start = window_start

        # Reset daily stats if new day
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self.daily_pnl = 0.0

        # 1. Target price = Chainlink BTC/USD at window boundary.
        #    This is the EXACT oracle Polymarket uses for resolution.
        #    Snapshotted in the main loop right at the window boundary.
        target_price = self._window_start_prices.get(window_start, 0.0)
        if target_price <= 0:
            # Missed the snapshot — try Chainlink now as fallback
            target_price = chainlink_btc_price()
            if target_price > 0:
                self._window_start_prices[window_start] = target_price

        # Prune old entries
        stale = [k for k in self._window_start_prices if k < window_start - 600]
        for k in stale:
            del self._window_start_prices[k]

        # Current BTC price from Binance (fast, real-time — for momentum detection)
        btc_price = self.btc_feed.btc_price
        if btc_price <= 0 or target_price <= 0:
            self._record_skip(slug, window_start, 0, 0, "no_btc_price")
            return

        # 2. Get market data from Polymarket
        try:
            market = fetch_market(slug)
            if not market:
                self._record_skip(slug, window_start, btc_price, target_price, "no_market")
                return
            md = parse_market_data(market)
        except Exception as e:
            self._record_skip(slug, window_start, btc_price, target_price, f"market_error:{e}")
            return

        # 4. Calculate distance
        distance = btc_price - target_price
        abs_distance = abs(distance)

        # FILTER 1: Minimum BTC movement
        if abs_distance < MIN_DISTANCE:
            self._record_skip(slug, window_start, btc_price, target_price,
                              f"dist_{abs_distance:.1f}<{MIN_DISTANCE}")
            return

        # 5. Determine direction
        direction = "UP" if distance > 0 else "DOWN"

        # 6. Get entry price (market midpoint + slippage)
        if direction == "UP":
            token_id = md.get("up_token_id")
            market_price = md.get("up_price")
        else:
            token_id = md.get("down_token_id")
            market_price = md.get("down_price")

        if not token_id or not market_price:
            self._record_skip(slug, window_start, btc_price, target_price, "no_token")
            return

        # Try live midpoint, fallback to market price
        live_mid = None
        try:
            live_mid = fetch_midpoint(token_id)
        except Exception:
            pass
        base_price = float(live_mid) if live_mid and float(live_mid) > 0 else float(market_price)
        entry_price = base_price + SLIPPAGE

        # FILTER 2: Entry price range
        if entry_price < ENTRY_MIN:
            self._record_skip(slug, window_start, btc_price, target_price,
                              f"entry_{entry_price:.2f}<{ENTRY_MIN}")
            return
        if entry_price > ENTRY_MAX:
            self._record_skip(slug, window_start, btc_price, target_price,
                              f"entry_{entry_price:.2f}>{ENTRY_MAX}")
            return

        # 7. Dynamic win rate estimation
        #    - Entry boost: cheaper entry = stronger repricing signal
        #      $0.50 → +5%, $0.60 → +2.5%, $0.70 → 0%
        entry_boost = max(0, (ENTRY_MAX - entry_price) / (ENTRY_MAX - ENTRY_MIN)) * ENTRY_BOOST_MAX
        #    - Distance boost: bigger BTC move = more momentum confirmation
        #      $50+ → +3%, $25 → +1.5%, $3 → ~0.2%
        dist_boost = min(abs_distance / DIST_BOOST_FULL, 1.0) * DIST_BOOST_MAX
        effective_wr = BASE_WIN_RATE + entry_boost + dist_boost

        # 8. Kelly sizing with dynamic WR
        b = (1.0 / entry_price) - 1.0  # payout odds
        kelly = max(0, (effective_wr * b - (1 - effective_wr)) / b)
        stake = self.paper_capital * kelly * KELLY_FRACTION

        # FILTER 3: Minimum stake (Kelly says no edge)
        if stake < MIN_STAKE:
            self._record_skip(slug, window_start, btc_price, target_price,
                              f"stake_{stake:.2f}<{MIN_STAKE}")
            return

        # Apply caps: max bet cap and 10% of capital hard limit
        stake = round(min(stake, self.max_bet_cap, self.paper_capital * 0.10), 2)

        # Calculate edge vs implied
        implied_prob = entry_price
        edge_pct = (effective_wr - implied_prob) * 100

        # 9. Place bet (paper mode)
        bet = PulseBet(
            slug=slug,
            direction=direction,
            btc_price=btc_price,
            target_price=target_price,
            entry_price=entry_price,
            stake=stake,
            edge_pct=edge_pct,
            distance=abs_distance,
            effective_wr=effective_wr,
        )
        bet.window_start = window_start

        with self._lock:
            self.pending_bets[slug] = bet
            self.bets.append(bet)
            self.total_bets += 1

        action = "PAPER" if self.paper_mode else "LIVE"
        print(f"[PULSE] {action} {direction} ${stake:.2f} @ {entry_price:.2f} | "
              f"BTC ${btc_price:.0f} target ${target_price:.0f} "
              f"dist ${abs_distance:.1f} edge {edge_pct:.1f}% wr {effective_wr:.1%}")

    def _resolve_completed(self):
        """Resolve bets from completed windows."""
        now = time.time()
        resolved_slugs = []

        for slug, bet in list(self.pending_bets.items()):
            window_end = bet.window_start + 300
            if now < window_end + 15:
                continue  # Window not done yet

            # Fetch market outcome
            from src.polymarket import fetch_market_outcome
            winner = fetch_market_outcome(slug)
            if not winner:
                if now > window_end + 120:
                    # Give up after 2 minutes
                    bet.outcome = "lose"
                    bet.pnl = -bet.stake
                    resolved_slugs.append(slug)
                continue

            # Determine win/lose
            won = (
                (winner.lower() == "up" and bet.direction == "UP") or
                (winner.lower() == "down" and bet.direction == "DOWN")
            )

            with self._lock:
                if won:
                    payout = bet.stake * (1.0 / bet.entry_price)
                    bet.pnl = round(payout - bet.stake, 2)
                    bet.outcome = "win"
                    self.wins += 1
                    self.paper_capital += bet.pnl
                else:
                    bet.pnl = -bet.stake
                    bet.outcome = "lose"
                    self.losses += 1
                    self.paper_capital -= bet.stake

                self.total_pnl += bet.pnl
                self.daily_pnl += bet.pnl

            result = "WIN" if won else "LOSE"
            print(f"[PULSE] {result} {bet.direction} {slug[-10:]} | "
                  f"P&L ${bet.pnl:+.2f} | Total ${self.total_pnl:+.2f}")
            resolved_slugs.append(slug)

        for slug in resolved_slugs:
            self.pending_bets.pop(slug, None)

    def _record_skip(self, slug: str, window_start: int, btc_price: float,
                     target_price: float, reason: str):
        """Record a skipped window."""
        bet = PulseBet(
            slug=slug, direction="--", btc_price=btc_price,
            target_price=target_price, entry_price=0, stake=0,
            edge_pct=0, distance=abs(btc_price - target_price) if target_price > 0 else 0,
        )
        bet.outcome = "skip"
        bet.skip_reason = reason
        bet.window_start = window_start

        with self._lock:
            self.bets.append(bet)
            self.skips += 1

