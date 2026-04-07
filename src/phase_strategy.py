"""
Phase Strategy — Bonereaper-replication via 4-phase progressive buying.

Replicates Bonereaper's exact trading pattern:
  Phase 1 (5-30s):    22% budget, exploratory, ~50/50 both sides
  Phase 2 (30-120s):  21% budget, direction building, 55-80% dominant
  Phase 3 (120-240s): 20% budget, escalation, 65-90% dominant
  Phase 4 (240-300s): 37% budget, closing, max conviction on winner

BTC price from Binance Futures WebSocket determines direction + conviction.
Both sides bought in every window (minority = hedge + profit source).
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from src.binance_ws import BinancePriceFeed
from src.chainlink import get_btc_price as chainlink_btc_price, get_btc_price_with_timestamp
from src.executor import Executor
from src.polymarket import (
    fetch_market, fetch_midpoint, parse_market_data,
)

log = logging.getLogger("phase_strategy")

# Phase timing boundaries (seconds into window)
PHASE_BOUNDARIES = [
    (5, 30, "exploration"),    # Phase 1
    (30, 120, "building"),     # Phase 2
    (120, 240, "escalation"),  # Phase 3
    (240, 300, "closing"),     # Phase 4
]

DEFAULT_PHASE_PCTS = [0.22, 0.21, 0.20, 0.37]  # sum = 1.00
DEFAULT_CONVICTION_THRESHOLD = 50.0
MIN_BTC_MOVE = 3.0
ORDER_INTERVAL = 12
PHASE_EXPECTED_ORDERS = [2, 6, 8, 5]

# Midpoint cache — avoid blocking REST calls on every order
_midpoint_cache: dict[str, tuple[float, float]] = {}
MIDPOINT_CACHE_TTL = 1.5  # seconds (reduced from 3s for fresher prices)
MIDPOINT_CACHE_MAX = 50   # max entries before pruning
PHASE_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "phase_state.json")


def _get_midpoint_cached(token_id: str) -> float | None:
    """Fetch midpoint with short cache to avoid hammering the API."""
    now = time.time()
    cached = _midpoint_cache.get(token_id)
    if cached and now - cached[1] < MIDPOINT_CACHE_TTL:
        return cached[0]
    try:
        mid = fetch_midpoint(token_id)
        if mid is not None and mid > 0:
            mid = float(mid)
            _midpoint_cache[token_id] = (mid, now)
            # Prune stale entries periodically
            if len(_midpoint_cache) > MIDPOINT_CACHE_MAX:
                stale = [k for k, (_, ts) in _midpoint_cache.items()
                         if now - ts > 60]
                for k in stale:
                    del _midpoint_cache[k]
            return mid
    except Exception:
        pass
    # Return stale cache only if < 10s old (not indefinitely stale)
    if cached and now - cached[1] < 10:
        return cached[0]
    return None


class WindowState:
    """Tracks state for a single 5-minute trading window."""

    def __init__(self, slug: str, window_start: int, max_budget: float):
        self.slug = slug
        self.window_start = window_start
        self.max_budget = max_budget
        self.created_at = time.time()

        # Market data
        self.up_token_id: str | None = None
        self.down_token_id: str | None = None
        self.condition_id: str | None = None
        self.neg_risk: bool = False
        self.tick_size: str = "0.01"

        # Budget tracking
        self.budget_spent = 0.0
        self.budget_per_phase = [0.0, 0.0, 0.0, 0.0]
        self.phase_targets = [max_budget * p for p in DEFAULT_PHASE_PCTS]

        # Order tracking
        self.orders: list[dict] = []
        self.order_count = 0
        self.phase_order_counts = [0, 0, 0, 0]
        self.up_usdc = 0.0
        self.down_usdc = 0.0
        self.up_shares = 0.0
        self.down_shares = 0.0

        # BTC reference (per-window, NOT global)
        self.btc_at_start: float = 0.0
        self.target_price: float = 0.0  # Chainlink price at window start (resolution oracle)
        self.last_btc_move: float = 0.0
        self.current_direction: str = ""

        # State
        self.active = True
        self.current_phase = -1
        self.last_order_time = 0.0
        self.resolved = False
        self.outcome: str | None = None
        self.pnl: float | None = None

    @property
    def elapsed(self) -> float:
        return time.time() - self.window_start

    @property
    def remaining(self) -> float:
        return max(0, (self.window_start + 300) - time.time())

    @property
    def budget_remaining(self) -> float:
        return max(0, self.max_budget - self.budget_spent)

    @property
    def net_direction(self) -> str:
        if self.up_usdc > self.down_usdc:
            return "up"
        elif self.down_usdc > self.up_usdc:
            return "down"
        return "neutral"

    @property
    def buy_bias(self) -> float:
        total = self.up_usdc + self.down_usdc
        return self.up_usdc / total if total > 0 else 0.5

    def record_order(self, direction: str, usdc: float, shares: float,
                     price: float, phase: int):
        self.orders.append({
            "direction": direction,
            "usdc": round(usdc, 2),
            "shares": round(shares, 2),
            "price": round(price, 4),
            "phase": phase,
            "elapsed": round(self.elapsed, 1),
            "time": datetime.now(timezone.utc).isoformat(),
        })
        self.order_count += 1
        self.budget_spent += usdc
        if 0 <= phase < 4:
            self.budget_per_phase[phase] += usdc
            self.phase_order_counts[phase] += 1
        if direction == "up":
            self.up_usdc += usdc
            self.up_shares += shares
        else:
            self.down_usdc += usdc
            self.down_shares += shares

    def summary(self) -> dict:
        return {
            "slug": self.slug,
            "window_start": self.window_start,
            "max_budget": self.max_budget,
            "budget_spent": round(self.budget_spent, 2),
            "budget_remaining": round(self.budget_remaining, 2),
            "phase_spent": [round(p, 2) for p in self.budget_per_phase],
            "order_count": self.order_count,
            "up_usdc": round(self.up_usdc, 2),
            "down_usdc": round(self.down_usdc, 2),
            "up_shares": round(self.up_shares, 2),
            "down_shares": round(self.down_shares, 2),
            "net_direction": self.net_direction,
            "buy_bias": round(self.buy_bias, 4),
            "btc_at_start": self.btc_at_start,
            "target_price": self.target_price,
            "last_btc_move": round(self.last_btc_move, 2),
            "elapsed": round(self.elapsed, 1),
            "remaining": round(self.remaining, 1),
            "active": self.active,
            "current_phase": self.current_phase,
            "outcome": self.outcome,
            "pnl": self.pnl,
        }


class PhaseStrategy:
    """4-phase progressive buying strategy replicating Bonereaper."""

    def __init__(self, binance_feed: BinancePriceFeed, executor: Executor, state):
        self.btc = binance_feed
        self.executor = executor
        self.state = state
        self._lock = threading.Lock()

        # Config
        self.max_budget = float(os.getenv("PHASE_MAX_BUDGET", "100"))
        self.min_volatility = float(os.getenv("PHASE_MIN_VOLATILITY", "5"))
        self.conviction_threshold = float(
            os.getenv("PHASE_CONVICTION_THRESHOLD", "50")
        )
        self.daily_loss_limit = float(os.getenv("PHASE_DAILY_LOSS_LIMIT", "200"))
        self.consecutive_loss_pause = int(
            os.getenv("PHASE_CONSECUTIVE_LOSS_PAUSE", "3")
        )

        # State
        self._thread: threading.Thread | None = None
        self._running = False
        self.active_windows: dict[str, WindowState] = {}
        self.completed_windows: list[dict] = []
        self._resolving_slugs: set[str] = set()  # prevent duplicate resolution threads
        self._consecutive_losses = 0
        self._daily_pnl = 0.0
        self._daily_date = ""
        self._paused_until = 0.0

        self.paper_mode = True
        self.total_windows = 0
        self.total_pnl = 0.0
        self.wins = 0
        self.losses = 0

        # Latency tracking (processing + CLOB)
        self._proc_latencies: list[float] = []   # ms, last N processing times
        self._clob_latencies: list[float] = []   # ms, last N CLOB round-trips
        self._latency_max_samples = 200

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._main_loop, name="phase-strategy", daemon=True
        )
        self._thread.start()
        mode = "PAPER" if self.paper_mode else "LIVE"
        log.info(f"PhaseStrategy started ({mode}, max ${self.max_budget}/window)")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    # ------------------------------------------------------------------
    #  Main Loop
    # ------------------------------------------------------------------
    def _main_loop(self):
        while self._running:
            try:
                now = time.time()

                if self.state.kill_switch:
                    time.sleep(2)
                    continue
                if now < self._paused_until:
                    time.sleep(1)
                    continue

                self._check_daily_reset()

                if self._daily_pnl <= -self.daily_loss_limit:
                    time.sleep(60)
                    continue

                if not self.btc.is_connected or self.btc.btc_price == 0:
                    time.sleep(1)
                    continue

                # Snapshot active windows under lock, then process WITHOUT lock.
                # Network calls (fetch_midpoint, fetch_market) must NOT hold
                # the lock — otherwise get_status() blocks the dashboard API.
                with self._lock:
                    expired = []
                    to_process = []
                    for slug, ws in list(self.active_windows.items()):
                        if ws.remaining <= 0:
                            expired.append(slug)
                        else:
                            to_process.append(ws)

                # Process windows WITHOUT lock (network I/O happens here)
                for ws in to_process:
                    self._process_window(ws)

                # Finalize expired windows (brief lock for state mutation only)
                for slug in expired:
                    with self._lock:
                        ws = self.active_windows.pop(slug, None)
                    if ws:
                        ws.active = False
                        self._finalize_window(ws)

                # Check for new windows (network I/O) WITHOUT lock
                self._check_new_windows()
                time.sleep(1)

            except Exception as e:
                log.error(f"PhaseStrategy error: {e}", exc_info=True)
                time.sleep(5)

    # ------------------------------------------------------------------
    #  Window Management
    # ------------------------------------------------------------------
    def _check_new_windows(self):
        now = int(time.time())
        window_start = now - (now % 300)
        elapsed = now - window_start

        if elapsed > 30:
            return

        slug = f"btc-updown-5m-{window_start}"
        with self._lock:
            if slug in self.active_windows:
                return
            if len(self.active_windows) >= 3:
                return

        # Budget: respect dashboard max_bet_per_window setting
        max_bet = getattr(self.state, "max_bet_per_window", None)
        budget = min(float(max_bet), self.max_budget) if max_bet else self.max_budget

        ws = WindowState(slug, window_start, budget)
        ws.btc_at_start = self.btc.btc_price

        # Snapshot Chainlink price as target (same oracle Polymarket uses)
        try:
            ws.target_price = chainlink_btc_price()
        except Exception:
            ws.target_price = 0.0
        if ws.target_price <= 0:
            ws.target_price = ws.btc_at_start  # fallback to Binance

        # Fetch market data
        market = fetch_market(slug)
        if not market:
            log.debug(f"Market not yet available: {slug}")
            return

        md = parse_market_data(market)
        if not md:
            log.warning(f"Could not parse market: {slug}")
            return

        ws.up_token_id = md.get("up_token_id")
        ws.down_token_id = md.get("down_token_id")
        ws.condition_id = md.get("condition_id")

        if ws.up_token_id and self.executor.enabled:
            info = self.executor.get_market_info(ws.up_token_id)
            ws.neg_risk = info.get("neg_risk", False)
            ws.tick_size = info.get("tick_size", "0.01")

        with self._lock:
            self.active_windows[slug] = ws
            self.total_windows += 1
        log.info(
            f"New window: {slug} | BTC=${ws.btc_at_start:.0f} | "
            f"Budget=${budget:.0f}"
        )

    def _process_window(self, ws: WindowState):
        elapsed = ws.elapsed

        # Determine current phase
        phase = -1
        for i, (start, end, _) in enumerate(PHASE_BOUNDARIES):
            if start <= elapsed < end:
                phase = i
                break
        if phase < 0:
            return

        ws.current_phase = phase

        # Rate limit orders
        now = time.time()
        if now - ws.last_order_time < ORDER_INTERVAL:
            return

        # Phase budget check
        phase_budget = ws.phase_targets[phase]
        phase_spent = ws.budget_per_phase[phase]
        if phase_spent >= phase_budget:
            return
        if ws.budget_remaining < 1.0:
            return

        # BTC movement — skip orders if feed is stale (>5s old)
        if self.btc.price_age_ms > 5000:
            log.debug(f"{ws.slug}: BTC feed stale ({self.btc.price_age_ms:.0f}ms), skipping order")
            return

        proc_start = time.time()
        btc_move = self.btc.btc_price - ws.btc_at_start
        ws.last_btc_move = btc_move
        abs_move = abs(btc_move)
        conviction = min(abs_move / self.conviction_threshold, 1.0)

        # Direction
        if abs_move >= MIN_BTC_MOVE:
            direction = "up" if btc_move > 0 else "down"
        elif ws.current_direction:
            direction = ws.current_direction
        else:
            direction = "up"  # near 50/50 in phase 1 anyway
        ws.current_direction = direction

        # Per-tick budget: spread remaining phase budget across remaining orders
        phase_remaining = phase_budget - phase_spent
        orders_remaining = max(1, PHASE_EXPECTED_ORDERS[phase] - ws.phase_order_counts[phase])
        order_budget = min(phase_remaining / orders_remaining, ws.budget_remaining, phase_remaining)

        # Reduce spending in low-conviction later phases
        if phase >= 2 and conviction < 0.3:
            order_budget *= 0.5

        if order_budget < 1.0:
            return

        # Dominant/minority split
        dominant_pct = self._get_dominant_pct(phase, conviction)
        dominant_usd = order_budget * dominant_pct
        minority_usd = order_budget * (1.0 - dominant_pct)

        # Map direction to tokens
        if direction == "up":
            dom_token, dom_dir = ws.up_token_id, "up"
            min_token, min_dir = ws.down_token_id, "down"
        else:
            dom_token, dom_dir = ws.down_token_id, "down"
            min_token, min_dir = ws.up_token_id, "up"

        # Track processing latency (decision-making time)
        proc_ms = (time.time() - proc_start) * 1000
        self._proc_latencies.append(proc_ms)
        if len(self._proc_latencies) > self._latency_max_samples:
            self._proc_latencies = self._proc_latencies[-self._latency_max_samples:]

        # Place orders (dominant first, then minority)
        placed = False
        if dominant_usd >= 0.50 and dom_token:
            if self._place_order(ws, dom_token, dom_dir, dominant_usd, phase):
                placed = True
        if minority_usd >= 0.50 and min_token:
            if self._place_order(ws, min_token, min_dir, minority_usd, phase):
                placed = True

        if placed:
            ws.last_order_time = now

    def _get_dominant_pct(self, phase: int, conviction: float) -> float:
        if phase == 0:
            return 0.50 + conviction * 0.15   # 50-65%
        elif phase == 1:
            return 0.55 + conviction * 0.25   # 55-80%
        elif phase == 2:
            return 0.65 + conviction * 0.25   # 65-90%
        else:
            return 0.75 + conviction * 0.20   # 75-95%

    def _place_order(self, ws: WindowState, token_id: str, direction: str,
                     amount_usd: float, phase: int) -> bool:
        """Place a single order. Returns True if recorded successfully."""
        # Get cached midpoint (avoids blocking REST call on every order)
        mid = _get_midpoint_cached(token_id)
        if mid is None or mid < 0.01 or mid >= 0.99:
            return False

        # Cap amount BEFORE calculating shares
        amount_usd = min(amount_usd, self.executor.max_bet_usd)
        if amount_usd < 0.50:
            return False

        max_price = min(mid + 0.03, 0.99)

        if self.paper_mode:
            # Simulate realistic fill: mid + 2 cent slippage (conservative)
            sim_price = min(mid + 0.02, 0.99)
            sim_shares = amount_usd / sim_price
            log.info(
                f"[PAPER] {ws.slug} P{phase+1} | {direction.upper()} "
                f"${amount_usd:.2f} @ ${sim_price:.3f} ({sim_shares:.1f} shares) | "
                f"BTC: ${ws.last_btc_move:+.1f}"
            )
            ws.record_order(direction, amount_usd, sim_shares, sim_price, phase)
            return True

        # Live order — measure CLOB round-trip latency
        clob_start = time.time()
        resp = self.executor.place_market_buy(
            token_id=token_id,
            amount_usd=amount_usd,
            max_price=max_price,
            neg_risk=ws.neg_risk,
            tick_size=ws.tick_size,
        )
        clob_ms = (time.time() - clob_start) * 1000
        self._clob_latencies.append(clob_ms)
        if len(self._clob_latencies) > self._latency_max_samples:
            self._clob_latencies = self._clob_latencies[-self._latency_max_samples:]

        if resp and resp.get("orderID"):
            fill_price = max_price  # conservative: assume worst-case fill
            live_shares = amount_usd / fill_price
            ws.record_order(direction, amount_usd, live_shares, fill_price, phase)
            log.info(
                f"[LIVE] {ws.slug} P{phase+1} | {direction.upper()} "
                f"${amount_usd:.2f} @ ${fill_price:.3f} ({live_shares:.1f} shares) | "
                f"order={resp['orderID'][:12]}... CLOB={clob_ms:.0f}ms"
            )
            return True

        log.warning(f"Order failed: {ws.slug} {direction} ${amount_usd:.2f} CLOB={clob_ms:.0f}ms")
        return False

    # ------------------------------------------------------------------
    #  Window Finalization & Resolution
    # ------------------------------------------------------------------
    def _finalize_window(self, ws: WindowState):
        summary = ws.summary()
        with self._lock:
            self.completed_windows.append(summary)
            if len(self.completed_windows) > 200:
                self.completed_windows = self.completed_windows[-200:]

        log.info(
            f"Window closed: {ws.slug} | Spent: ${ws.budget_spent:.2f} | "
            f"Dir: {ws.net_direction} (bias={ws.buy_bias:.2f}) | "
            f"Orders: {ws.order_count} | "
            f"UP: {ws.up_shares:.0f} shares / DOWN: {ws.down_shares:.0f} shares"
        )

        # Schedule resolution (retry up to 3 times, guarded against duplicates)
        with self._lock:
            self._resolving_slugs.add(ws.slug)
        threading.Timer(45, self._resolve_window, args=[ws.slug, 0]).start()

    def _resolve_window(self, slug: str, attempt: int):
        # Guard: skip if another thread already resolved this slug
        with self._lock:
            if slug not in self._resolving_slugs:
                return  # already resolved or removed

        outcome = None

        # Method 1: Chainlink oracle with timestamp verification
        # Find the target price from the completed window summary
        target_price = 0.0
        with self._lock:
            for summary in self.completed_windows:
                if summary["slug"] == slug:
                    target_price = summary.get("target_price", 0) or summary.get("btc_at_start", 0)
                    break

        if target_price > 0:
            try:
                window_start = int(slug.split("-")[-1])
                window_end = window_start + 300
                end_price, updated_at = get_btc_price_with_timestamp()
                if end_price > 0 and updated_at >= window_end:
                    outcome = "up" if end_price > target_price else "down"
            except Exception as e:
                log.debug(f"Chainlink resolution error for {slug}: {e}")

        # Method 2: Polymarket API fallback
        if not outcome:
            try:
                from src.polymarket import fetch_market_outcome
                outcome = fetch_market_outcome(slug)
            except Exception as e:
                log.debug(f"Polymarket resolution error for {slug}: {e}")

        if not outcome:
            if attempt < 5:
                threading.Timer(30, self._resolve_window,
                                args=[slug, attempt + 1]).start()
            else:
                log.warning(f"Could not resolve {slug} after {attempt+1} attempts — voiding")
                with self._lock:
                    self._resolving_slugs.discard(slug)
            return

        with self._lock:
            for summary in self.completed_windows:
                if summary["slug"] != slug or summary.get("outcome"):
                    continue

                summary["outcome"] = outcome

                # P&L: winning shares pay $1.00 each, losers pay $0
                up_shares = summary.get("up_shares", 0)
                down_shares = summary.get("down_shares", 0)
                total_spent = summary["budget_spent"]

                payout = up_shares if outcome == "up" else down_shares
                pnl = round(payout - total_spent, 2)
                summary["pnl"] = pnl

                # Stats
                self.total_pnl += pnl
                self._daily_pnl += pnl

                if pnl >= 0:
                    self.wins += 1
                    self._consecutive_losses = 0
                else:
                    self.losses += 1
                    self._consecutive_losses += 1
                    if self._consecutive_losses >= self.consecutive_loss_pause:
                        self._paused_until = time.time() + 1800
                        log.warning(
                            f"{self._consecutive_losses} consecutive losses "
                            f"— pausing 30 min"
                        )

                correct = summary["net_direction"] == outcome
                self._resolving_slugs.discard(slug)
                log.info(
                    f"RESOLVED: {slug} -> {outcome} | "
                    f"{'CORRECT' if correct else 'WRONG'} | "
                    f"Payout: ${payout:.2f} - Cost: ${total_spent:.2f} = "
                    f"P&L: ${pnl:+.2f} | Total: ${self.total_pnl:+.2f}"
                )
                break

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------
    def _check_daily_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_date:
            if self._daily_date:
                log.info(f"Daily reset | Yesterday P&L: ${self._daily_pnl:+.2f}")
            self._daily_date = today
            self._daily_pnl = 0.0

    # ------------------------------------------------------------------
    #  State Persistence
    # ------------------------------------------------------------------

    def save_state(self):
        """Persist completed windows and stats to disk."""
        with self._lock:
            data = {
                "completed_windows": self.completed_windows[-200:],
                "total_pnl": self.total_pnl,
                "wins": self.wins,
                "losses": self.losses,
                "total_windows": self.total_windows,
                "daily_pnl": self._daily_pnl,
                "daily_date": self._daily_date,
                "consecutive_losses": self._consecutive_losses,
            }
        try:
            os.makedirs(os.path.dirname(PHASE_STATE_FILE), exist_ok=True)
            tmp = PHASE_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, PHASE_STATE_FILE)
        except Exception as e:
            print(f"[PHASE] Save failed: {e}")

    def load_state(self):
        """Restore completed windows and stats from disk."""
        try:
            with open(PHASE_STATE_FILE, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        with self._lock:
            self.completed_windows = data.get("completed_windows", [])
            self.total_windows = data.get("total_windows", 0)

            # Recompute stats from actual window outcomes
            self.wins = 0
            self.losses = 0
            self.total_pnl = 0.0
            for w in self.completed_windows:
                if not w.get("outcome"):
                    continue
                pnl = w.get("pnl", 0) or 0
                self.total_pnl += pnl
                if pnl >= 0:
                    self.wins += 1
                else:
                    self.losses += 1

            self._daily_pnl = data.get("daily_pnl", 0.0)
            self._daily_date = data.get("daily_date", "")
            self._consecutive_losses = data.get("consecutive_losses", 0)

        total = self.wins + self.losses
        print(f"[PHASE] State restored: {total} windows, {self.wins}W/{self.losses}L, "
              f"P&L ${self.total_pnl:+.2f}")

    # ------------------------------------------------------------------
    #  Dashboard API
    # ------------------------------------------------------------------
    def _latency_stats(self, samples: list[float]) -> dict:
        if not samples:
            return {"avg": 0, "min": 0, "max": 0}
        return {
            "avg": round(sum(samples) / len(samples), 1),
            "min": round(min(samples), 1),
            "max": round(max(samples), 1),
        }

    def get_status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "paper_mode": self.paper_mode,
                "max_budget": self.max_budget,
                "total_windows": self.total_windows,
                "active_windows": {
                    s: w.summary() for s, w in self.active_windows.items()
                },
                "total_pnl": round(self.total_pnl, 2),
                "daily_pnl": round(self._daily_pnl, 2),
                "wins": self.wins,
                "losses": self.losses,
                "win_rate": round(
                    self.wins / max(self.wins + self.losses, 1) * 100, 1
                ),
                "consecutive_losses": self._consecutive_losses,
                "paused_until": (
                    self._paused_until
                    if self._paused_until > time.time()
                    else None
                ),
                "recent_windows": self.completed_windows[-20:],
                "latency_processing": self._latency_stats(self._proc_latencies),
                "latency_clob": self._latency_stats(self._clob_latencies),
            }
