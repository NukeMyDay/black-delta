"""
Phase Strategy v2 -- Bonereaper-replication via 3-state machine.

State Machine: DISCOVERY -> REINFORCEMENT -> HARVEST
Two decision heads:
  - delta_bias:     direction signal from mid_imbalance (-1..+1)
  - flow_magnitude: sizing signal from market certainty (0..1)

Budget Ladder (cumulative caps):
  DISCOVERY:     30% (normal) / 20% (low-conf)
  REINFORCEMENT: 65% (normal) / 45% (low-conf)
  HARVEST:      100% (normal) / blocked (low-conf)

Empirical basis: 39K Bonereaper trades, 294 windows (2026-04-06/09).
Spec frozen 2026-04-09 by Claude Code + Codex consensus.
"""

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone

from src.binance_ws import BinancePriceFeed
from src.chainlink import (
    get_btc_price_at_or_after_timestamp,
    get_btc_price_at_or_before_timestamp,
)
from src.executor import Executor
from src.polymarket import (
    fetch_market, fetch_midpoint, parse_market_data,
)

log = logging.getLogger("phase_strategy")


def _log(msg: str):
    """Print to stdout so dashboard log viewer and docker logs capture it."""
    print(f"[PHASE] {msg}")

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

# Order cadence: ~3.5s between orders (Bonereaper averages ~2.2s, we target
# a slightly conservative cadence to avoid rate limits while still reaching
# ~80 orders per 300s window)
ORDER_INTERVAL = 3.5

# Budget Ladder -- cumulative caps as fraction of max_budget
BUDGET_LADDER = {
    "DISCOVERY":     {"normal": 0.30, "low_conf": 0.20},
    "REINFORCEMENT": {"normal": 0.65, "low_conf": 0.45},
    "HARVEST":       {"normal": 1.00, "low_conf": 0.45},
}

# State transition thresholds (all time-based per freeze agreement)
DISCOVERY_MIN_ELAPSED = 80       # seconds before REINFORCEMENT eligible
REINFORCEMENT_MIN_ELAPSED = 180  # seconds before HARVEST eligible
HARVEST_DOMINANT_MIN = 0.90      # dominant_mid threshold for HARVEST entry
HARVEST_DOMINANT_DROP = 0.85     # drop below -> back to REINFORCEMENT
DEEP_HARVEST_THRESHOLD = 0.95   # dominant_mid for aggressive deployment

# Low confidence triggers
LOW_CONF_LATE_UNSTABLE_S = 180   # elapsed threshold for "late + unstable"
LOW_CONF_EARLY_FLIP_S = 120      # elapsed threshold for "early flip storm"
LOW_CONF_EARLY_FLIP_COUNT = 2    # flip count threshold
LOW_CONF_MID_UNCLEAR_S = 120     # elapsed threshold for "mid unclear"

# Bias stability
BIAS_STABILITY_MIN_S = 30.0      # seconds of stable bias for transitions
BIAS_DRIFT_THRESHOLD = 0.10      # deviation from anchor triggers reset

# Structural flip confirmation
STRUCTURAL_FLIP_CONFIRM_S = 30.0  # seconds on new side to confirm

# Direction signal thresholds
DIRECTION_CLEAR_IMBALANCE = 0.20  # |mid_imbalance| threshold
DIRECTION_CLEAR_UP_HIGH = 0.60    # up_mid above this = clear up
DIRECTION_CLEAR_UP_LOW = 0.40     # up_mid below this = clear down

# Execution
DISCOVERY_BIAS_CAP = 0.75         # max inventory_bias in DISCOVERY (0.25..0.75)
PENNY_THRESHOLD = 0.95            # mid >= this allows penny collecting

# Soft rebalancing after REINFORCEMENT -> DISCOVERY
REBALANCE_ORDERS = 5
REBALANCE_COUNTER_PCT = 0.60

# Midpoint cache
_midpoint_cache: dict[str, tuple[float, float]] = {}
MIDPOINT_CACHE_TTL = 1.5
MIDPOINT_CACHE_MAX = 50

PHASE_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "phase_state.json")
PHASE_TRADE_LOG = os.path.join(os.path.dirname(__file__), "..", "data", "phase_trades.jsonl")


# ---------------------------------------------------------------------------
#  Midpoint Cache
# ---------------------------------------------------------------------------

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
            if len(_midpoint_cache) > MIDPOINT_CACHE_MAX:
                stale = [k for k, (_, ts) in _midpoint_cache.items()
                         if now - ts > 60]
                for k in stale:
                    del _midpoint_cache[k]
            return mid
    except Exception:
        pass
    if cached and now - cached[1] < 10:
        return cached[0]
    return None


# ---------------------------------------------------------------------------
#  WindowState
# ---------------------------------------------------------------------------

class WindowState:
    """Tracks all state for a single 5-minute trading window."""

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

        # --- v2 State Machine ---
        self.state: str = "DISCOVERY"
        self.state_entered_at: float = time.time()
        self.low_confidence: bool = False

        # --- Budget tracking (cumulative, never reset) ---
        self.inventory_total_usdc: float = 0.0
        self.up_usdc: float = 0.0
        self.down_usdc: float = 0.0
        self.up_shares: float = 0.0
        self.down_shares: float = 0.0

        # --- Order tracking ---
        self.orders: list[dict] = []
        self.order_count: int = 0

        # --- BTC reference ---
        self.btc_at_start: float = 0.0
        self.target_price: float = 0.0

        # --- Bias tracking ---
        self._bias_anchor: float = 0.5
        self._bias_anchor_ts: float = time.time()
        self._bias_last_side: str = "neutral"
        self.bias_stable_since_s: float = 0.0

        # --- Flip tracking ---
        self.flip_count: int = 0
        self.structural_flip_count: int = 0
        self.last_flip_elapsed_s: float = float("inf")
        self._last_bias_side: str = "neutral"
        self._flip_candidate_ts: float = 0.0
        self._flip_candidate_side: str = ""

        # --- Rebalancing (after REINFORCEMENT -> DISCOVERY) ---
        self._rebalance_remaining: int = 0

        # --- Lifecycle ---
        self.active: bool = True
        self.last_order_time: float = 0.0
        self.resolved: bool = False
        self.outcome: str | None = None
        self.pnl: float | None = None

        # --- Logging ---
        self._state_history: list[tuple[float, str]] = [(0.0, "DISCOVERY")]

    # --- Properties ---

    @property
    def elapsed(self) -> float:
        return time.time() - self.window_start

    @property
    def remaining(self) -> float:
        return max(0, (self.window_start + 300) - time.time())

    @property
    def budget_remaining(self) -> float:
        return max(0, self.max_budget - self.inventory_total_usdc)

    @property
    def inventory_bias(self) -> float:
        total = self.up_usdc + self.down_usdc
        return self.up_usdc / total if total > 0 else 0.5

    @property
    def net_direction(self) -> str:
        if self.up_usdc > self.down_usdc:
            return "up"
        elif self.down_usdc > self.up_usdc:
            return "down"
        return "neutral"

    # --- Bias stability tracking ---

    def update_bias_stability(self):
        """Update bias stability metrics after each order."""
        now = time.time()
        bias = self.inventory_bias
        current_side = "up" if bias >= 0.5 else "down"

        # Detect 0.5-crossing (flip)
        if (self._last_bias_side in ("up", "down") and
                current_side != self._last_bias_side):
            self.flip_count += 1
            self.last_flip_elapsed_s = self.elapsed

            # Start structural flip candidate
            self._flip_candidate_ts = now
            self._flip_candidate_side = current_side

        self._last_bias_side = current_side

        # Confirm structural flip if on new side for STRUCTURAL_FLIP_CONFIRM_S
        if (self._flip_candidate_side and
                current_side == self._flip_candidate_side and
                now - self._flip_candidate_ts >= STRUCTURAL_FLIP_CONFIRM_S):
            self.structural_flip_count += 1
            self._flip_candidate_side = ""  # consumed

        # Reset stability anchor on side change or drift
        if (current_side != self._bias_last_side or
                abs(bias - self._bias_anchor) > BIAS_DRIFT_THRESHOLD):
            self._bias_anchor = bias
            self._bias_anchor_ts = now
            self._bias_last_side = current_side

        self.bias_stable_since_s = now - self._bias_anchor_ts

    # --- Order recording ---

    def record_order(self, direction: str, usdc: float, shares: float,
                     price: float, state: str):
        self.orders.append({
            "direction": direction,
            "usdc": round(usdc, 2),
            "shares": round(shares, 2),
            "price": round(price, 4),
            "state": state,
            "elapsed": round(self.elapsed, 1),
            "time": datetime.now(timezone.utc).isoformat(),
        })
        self.order_count += 1
        self.inventory_total_usdc += usdc
        if direction == "up":
            self.up_usdc += usdc
            self.up_shares += shares
        else:
            self.down_usdc += usdc
            self.down_shares += shares

        self.update_bias_stability()

    # --- State transitions ---

    def transition_to(self, new_state: str):
        if new_state == self.state:
            return
        old_state = self.state
        self.state = new_state
        self.state_entered_at = time.time()
        self._state_history.append((self.elapsed, new_state))

        # Soft rebalancing on backward transition to DISCOVERY
        if old_state == "REINFORCEMENT" and new_state == "DISCOVERY":
            self._rebalance_remaining = REBALANCE_ORDERS

        _log(
            f"{self.slug} | {old_state} -> {new_state} @ {self.elapsed:.0f}s | "
            f"bias={self.inventory_bias:.3f} flips={self.flip_count} "
            f"struct_flips={self.structural_flip_count}"
        )

    # --- Budget ---

    def get_budget_cap(self) -> float:
        mode = "low_conf" if self.low_confidence else "normal"
        return self.max_budget * BUDGET_LADDER[self.state][mode]

    def can_spend(self, amount: float) -> bool:
        return (self.inventory_total_usdc + amount) <= self.get_budget_cap()

    # --- Summary ---

    def summary(self, include_orders: bool = False) -> dict:
        d = {
            "slug": self.slug,
            "window_start": self.window_start,
            "max_budget": self.max_budget,
            "budget_spent": round(self.inventory_total_usdc, 2),
            "budget_remaining": round(self.budget_remaining, 2),
            "budget_cap": round(self.get_budget_cap(), 2),
            "order_count": self.order_count,
            "up_usdc": round(self.up_usdc, 2),
            "down_usdc": round(self.down_usdc, 2),
            "up_shares": round(self.up_shares, 2),
            "down_shares": round(self.down_shares, 2),
            "net_direction": self.net_direction,
            "buy_bias": round(self.inventory_bias, 4),
            "btc_at_start": self.btc_at_start,
            "target_price": self.target_price,
            "elapsed": round(self.elapsed, 1),
            "remaining": round(self.remaining, 1),
            "active": self.active,
            "state": self.state,
            "low_confidence": self.low_confidence,
            "flip_count": self.flip_count,
            "structural_flip_count": self.structural_flip_count,
            "bias_stable_since_s": round(self.bias_stable_since_s, 1),
            "state_history": self._state_history,
            "outcome": self.outcome,
            "pnl": self.pnl,
        }
        if include_orders:
            d["orders"] = list(self.orders)
        return d


# ---------------------------------------------------------------------------
#  PhaseStrategy v2
# ---------------------------------------------------------------------------

class PhaseStrategy:
    """3-state progressive buying strategy replicating Bonereaper."""

    def __init__(self, binance_feed: BinancePriceFeed, executor: Executor, state):
        self.btc = binance_feed
        self.executor = executor
        self.state = state
        self._lock = threading.Lock()

        # Config
        self.max_budget = float(os.getenv("PHASE_MAX_BUDGET", "100"))
        self.daily_loss_limit = float(os.getenv("PHASE_DAILY_LOSS_LIMIT", "200"))
        self.consecutive_loss_pause = int(
            os.getenv("PHASE_CONSECUTIVE_LOSS_PAUSE", "3")
        )

        # State
        self._thread: threading.Thread | None = None
        self._running = False
        self.active_windows: dict[str, WindowState] = {}
        self.completed_windows: list[dict] = []
        self._resolving_slugs: set[str] = set()
        self._consecutive_losses = 0
        self._daily_pnl = 0.0
        self._daily_date = ""
        self._paused_until = 0.0

        self.paper_mode = True
        self.total_windows = 0
        self.total_pnl = 0.0
        self.wins = 0
        self.losses = 0

        # Latency tracking
        self._proc_latencies: list[float] = []
        self._clob_latencies: list[float] = []
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
        _log(f"PhaseStrategy v2 started ({mode}, max ${self.max_budget}/window)")

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

                # Snapshot active windows under lock, process without lock
                with self._lock:
                    expired = []
                    to_process = []
                    for slug, ws in list(self.active_windows.items()):
                        if ws.remaining <= 0:
                            expired.append(slug)
                        else:
                            to_process.append(ws)

                for ws in to_process:
                    self._process_window(ws)

                for slug in expired:
                    with self._lock:
                        ws = self.active_windows.pop(slug, None)
                    if ws:
                        ws.active = False
                        self._finalize_window(ws)

                self._check_new_windows()
                time.sleep(1)

            except Exception as e:
                _log(f"PhaseStrategy error: {e}")
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

        max_bet = getattr(self.state, "max_bet_per_window", None)
        budget = min(float(max_bet), self.max_budget) if max_bet else self.max_budget

        ws = WindowState(slug, window_start, budget)
        ws.btc_at_start = self.btc.btc_price

        try:
            ws.target_price, _ = get_btc_price_at_or_before_timestamp(window_start)
        except Exception:
            ws.target_price = 0.0
        if ws.target_price <= 0:
            ws.target_price = ws.btc_at_start

        market = fetch_market(slug)
        if not market:
            _log(f"Market not yet available: {slug}")
            return

        md = parse_market_data(market)
        if not md:
            _log(f"Could not parse market: {slug}")
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
        _log(
            f"New window: {slug} | BTC=${ws.btc_at_start:.0f} | "
            f"Budget=${budget:.0f}"
        )

    # ------------------------------------------------------------------
    #  Core: Process Window Tick
    # ------------------------------------------------------------------

    def _process_window(self, ws: WindowState):
        elapsed = ws.elapsed

        # Wait for initial market data (first 5 seconds)
        if elapsed < 5:
            return

        # Rate limit orders
        now = time.time()
        if now - ws.last_order_time < ORDER_INTERVAL:
            return

        # Debug: log every tick that passes rate limiting
        if ws.order_count == 0 and int(elapsed) % 10 < 2:
            _log(f"{ws.slug} TICK @ {elapsed:.0f}s | tokens={ws.up_token_id is not None}/{ws.down_token_id is not None} | btc_age={self.btc.price_age_ms:.0f}ms")

        # Skip if feed stale
        if self.btc.price_age_ms > 5000:
            _log(f"{ws.slug} SKIP: BTC feed stale ({self.btc.price_age_ms:.0f}ms)")
            return

        proc_start = time.time()

        # --- 1. Observe market state ---
        up_mid = _get_midpoint_cached(ws.up_token_id) if ws.up_token_id else None
        down_mid = _get_midpoint_cached(ws.down_token_id) if ws.down_token_id else None

        if up_mid is None or down_mid is None:
            _log(f"{ws.slug} SKIP: midpoints unavailable (up_token={ws.up_token_id is not None}, down_token={ws.down_token_id is not None}, up_mid={up_mid}, down_mid={down_mid})")
            return
        if up_mid < 0.01 or down_mid < 0.01:
            _log(f"{ws.slug} SKIP: midpoints too low (up={up_mid}, down={down_mid})")
            return

        implied_sum = up_mid + down_mid
        mid_imbalance = up_mid - down_mid
        dominant_mid = max(up_mid, down_mid)

        # --- 2. Compute signals ---
        direction_clear = (
            mid_imbalance >= DIRECTION_CLEAR_IMBALANCE or
            mid_imbalance <= -DIRECTION_CLEAR_IMBALANCE or
            up_mid >= DIRECTION_CLEAR_UP_HIGH or
            up_mid <= DIRECTION_CLEAR_UP_LOW
        )
        bias_stable = ws.bias_stable_since_s >= BIAS_STABILITY_MIN_S
        no_recent_flip = (elapsed - ws.last_flip_elapsed_s) >= BIAS_STABILITY_MIN_S

        # Low confidence flag
        ws.low_confidence = (
            (elapsed >= LOW_CONF_LATE_UNSTABLE_S and not bias_stable) or
            (ws.flip_count >= LOW_CONF_EARLY_FLIP_COUNT and elapsed <= LOW_CONF_EARLY_FLIP_S) or
            (elapsed >= LOW_CONF_MID_UNCLEAR_S and not direction_clear and not bias_stable)
        )

        # Harvest eligibility
        harvest_eligible = (
            dominant_mid >= HARVEST_DOMINANT_MIN and
            elapsed >= REINFORCEMENT_MIN_ELAPSED and
            no_recent_flip and
            not ws.low_confidence
        )

        # --- 3. State transitions ---
        self._update_state(ws, elapsed, bias_stable, no_recent_flip,
                           direction_clear, harvest_eligible, dominant_mid)

        # --- 4. Budget check ---
        if ws.budget_remaining < 0.50:
            return
        if not ws.can_spend(0.50):
            return

        # --- 5. Compute delta_bias (direction head) ---
        delta_bias = self._compute_delta_bias(
            ws, mid_imbalance, up_mid, down_mid, elapsed
        )

        # --- 6. Compute flow_magnitude (sizing head) ---
        magnitude = self._compute_magnitude(
            ws, dominant_mid, implied_sum, direction_clear, bias_stable, elapsed
        )

        # Dampen magnitude under low confidence
        if ws.low_confidence:
            magnitude *= 0.5

        # --- 7. Execute based on state ---
        self._execute_orders(
            ws, delta_bias, magnitude, up_mid, down_mid,
            implied_sum, dominant_mid
        )

        # Track processing latency
        proc_ms = (time.time() - proc_start) * 1000
        self._proc_latencies.append(proc_ms)
        if len(self._proc_latencies) > self._latency_max_samples:
            self._proc_latencies = self._proc_latencies[-self._latency_max_samples:]

    # ------------------------------------------------------------------
    #  State Machine
    # ------------------------------------------------------------------

    def _update_state(self, ws: WindowState, elapsed: float,
                      bias_stable: bool, no_recent_flip: bool,
                      direction_clear: bool, harvest_eligible: bool,
                      dominant_mid: float):
        """Evaluate and apply state transitions."""

        # Track structural flips since state entry for backward transitions
        state_entry_elapsed = elapsed - (time.time() - ws.state_entered_at)
        structural_flip_after_entry = any(
            ts >= state_entry_elapsed
            for ts, _ in [(ws.last_flip_elapsed_s, None)]
            if ws.structural_flip_count > 0 and ts >= state_entry_elapsed
        )
        # More precise: check if a structural flip was confirmed after state entry
        # We approximate: if last_flip_elapsed_s > state_entry time and it was structural
        flip_after_state_entry = (
            ws.last_flip_elapsed_s != float("inf") and
            ws.last_flip_elapsed_s >= state_entry_elapsed and
            ws.structural_flip_count > 0
        )

        if ws.state == "DISCOVERY":
            if (elapsed >= DISCOVERY_MIN_ELAPSED and
                    bias_stable and no_recent_flip and direction_clear):
                ws.transition_to("REINFORCEMENT")

        elif ws.state == "REINFORCEMENT":
            if harvest_eligible:
                ws.transition_to("HARVEST")
            elif flip_after_state_entry:
                ws.transition_to("DISCOVERY")

        elif ws.state == "HARVEST":
            if dominant_mid < HARVEST_DOMINANT_DROP:
                ws.transition_to("REINFORCEMENT")
            elif flip_after_state_entry:
                ws.transition_to("REINFORCEMENT")

    # ------------------------------------------------------------------
    #  Delta Bias Head (Direction)
    # ------------------------------------------------------------------

    def _compute_delta_bias(self, ws: WindowState, mid_imbalance: float,
                            up_mid: float, down_mid: float,
                            elapsed: float) -> float:
        """
        Compute direction signal from market mid-prices.
        Returns float in [-1, +1] where +1 = strong Up, -1 = strong Down.

        Primary signal: mid_imbalance (Pearson 0.51 to final_bias at trade #10).
        """
        # Raw signal from mid_imbalance, scaled to [-1, +1]
        # mid_imbalance range is roughly [-0.5, +0.5], but typically [-0.3, +0.3]
        raw = mid_imbalance / 0.30  # normalize so +/-0.30 maps to +/-1.0
        raw = max(-1.0, min(1.0, raw))

        # Dampen early (< 30s) -- signal not yet readable
        if elapsed < 30:
            dampening = elapsed / 30.0  # linear ramp 0..1
            raw *= dampening

        return raw

    # ------------------------------------------------------------------
    #  Flow Magnitude Head (Sizing)
    # ------------------------------------------------------------------

    def _compute_magnitude(self, ws: WindowState, dominant_mid: float,
                           implied_sum: float, direction_clear: bool,
                           bias_stable: bool, elapsed: float) -> float:
        """
        Compute sizing signal from market certainty.
        Returns float in [0, 1] where 1 = maximum deployment rate.
        """
        # Base magnitude from dominant_mid certainty
        # dominant_mid 0.50 -> 0.0, dominant_mid 0.65 -> 0.5, dominant_mid 0.90 -> 1.0
        if dominant_mid <= 0.50:
            base = 0.0
        elif dominant_mid >= 0.90:
            base = 1.0
        else:
            base = (dominant_mid - 0.50) / 0.40

        # Arb bonus: if implied_sum < 1.0, there's free value
        if implied_sum < 0.98:
            arb_boost = min(0.2, (1.0 - implied_sum) * 2.0)
            base = min(1.0, base + arb_boost)

        # Direction clarity boost
        if direction_clear and bias_stable:
            base = min(1.0, base * 1.2)

        # Time pressure: slight boost in final 60s to deploy remaining budget
        if elapsed >= 240 and ws.state in ("REINFORCEMENT", "HARVEST"):
            time_pressure = (elapsed - 240) / 60.0  # 0..1 over last 60s
            base = min(1.0, base + time_pressure * 0.15)

        return base

    # ------------------------------------------------------------------
    #  Execution Engine
    # ------------------------------------------------------------------

    def _execute_orders(self, ws: WindowState, delta_bias: float,
                        magnitude: float, up_mid: float, down_mid: float,
                        implied_sum: float, dominant_mid: float):
        """
        Translate delta_bias + magnitude into concrete orders.
        Behavior depends on current state (execution mode).
        """
        if magnitude < 0.05:
            return  # nothing to do

        # Per-tick budget based on remaining cap and magnitude
        budget_cap = ws.get_budget_cap()
        budget_available = budget_cap - ws.inventory_total_usdc
        if budget_available < 0.50:
            return

        # Base order size: spread available budget over remaining time
        remaining_s = ws.remaining
        if remaining_s < ORDER_INTERVAL:
            return
        remaining_ticks = remaining_s / ORDER_INTERVAL
        per_tick_base = budget_available / max(remaining_ticks, 1.0)

        # Scale by magnitude
        order_budget = per_tick_base * (0.3 + 0.7 * magnitude)
        order_budget = min(order_budget, budget_available, ws.budget_remaining)

        if order_budget < 0.50:
            return

        # --- State-specific execution mode ---

        if ws.state == "DISCOVERY":
            self._execute_discovery(ws, delta_bias, order_budget,
                                    up_mid, down_mid, implied_sum)
        elif ws.state == "REINFORCEMENT":
            self._execute_reinforcement(ws, delta_bias, order_budget,
                                        up_mid, down_mid, implied_sum, dominant_mid)
        elif ws.state == "HARVEST":
            self._execute_harvest(ws, delta_bias, order_budget,
                                  up_mid, down_mid, dominant_mid)

    def _execute_discovery(self, ws: WindowState, delta_bias: float,
                           order_budget: float, up_mid: float,
                           down_mid: float, implied_sum: float):
        """
        DISCOVERY: Bilateral exploration.
        Buy both sides, slight lean toward delta_bias direction.
        Bias capped at DISCOVERY_BIAS_CAP.
        """
        # Determine split
        if ws._rebalance_remaining > 0:
            # Soft rebalancing after backward transition
            dominant_pct = get_rebalance_split(
                ws._rebalance_remaining, ws.inventory_bias
            )
            ws._rebalance_remaining -= 1
        else:
            # Normal DISCOVERY: 50% base + delta_bias lean (max +/-25%)
            dominant_pct = 0.50 + delta_bias * 0.15
            dominant_pct = max(0.30, min(0.70, dominant_pct))

        # Enforce bias cap
        if ws.inventory_bias > DISCOVERY_BIAS_CAP:
            dominant_pct = min(dominant_pct, 0.35)  # force more down
        elif ws.inventory_bias < (1.0 - DISCOVERY_BIAS_CAP):
            dominant_pct = max(dominant_pct, 0.65)  # force more up

        # Map delta_bias to direction
        if delta_bias >= 0:
            up_pct = dominant_pct
        else:
            up_pct = 1.0 - dominant_pct

        up_usd = order_budget * up_pct
        down_usd = order_budget * (1.0 - up_pct)

        placed = False
        if up_usd >= 0.50 and ws.up_token_id:
            if self._place_order(ws, ws.up_token_id, "up", up_usd, up_mid):
                placed = True
        if down_usd >= 0.50 and ws.down_token_id:
            if self._place_order(ws, ws.down_token_id, "down", down_usd, down_mid):
                placed = True

        if placed:
            ws.last_order_time = time.time()

    def _execute_reinforcement(self, ws: WindowState, delta_bias: float,
                                order_budget: float, up_mid: float,
                                down_mid: float, implied_sum: float,
                                dominant_mid: float):
        """
        REINFORCEMENT: Direction-biased deployment.
        Dominant side gets lion's share, scaled with conviction.
        Minority side still gets orders (hedge + arb capture).
        """
        # Conviction from delta_bias strength
        conviction = abs(delta_bias)

        # Dominant percentage: 60-85% based on conviction
        dominant_pct = 0.60 + conviction * 0.25
        dominant_pct = max(0.55, min(0.85, dominant_pct))

        # Arb opportunity: boost minority when implied_sum < 0.98
        if implied_sum < 0.98:
            arb_factor = min(0.15, (1.0 - implied_sum) * 1.5)
            dominant_pct -= arb_factor  # give more to minority

        # Direction from existing inventory bias (REINFORCEMENT continues direction)
        if ws.inventory_bias >= 0.5:
            up_pct = dominant_pct
        else:
            up_pct = 1.0 - dominant_pct

        # Override with delta_bias if it disagrees strongly with inventory
        if delta_bias > 0.5 and ws.inventory_bias < 0.5:
            up_pct = dominant_pct  # market says up, go with market
        elif delta_bias < -0.5 and ws.inventory_bias >= 0.5:
            up_pct = 1.0 - dominant_pct  # market says down, go with market

        up_usd = order_budget * up_pct
        down_usd = order_budget * (1.0 - up_pct)

        placed = False
        if up_usd >= 0.50 and ws.up_token_id:
            if self._place_order(ws, ws.up_token_id, "up", up_usd, up_mid):
                placed = True
        if down_usd >= 0.50 and ws.down_token_id:
            if self._place_order(ws, ws.down_token_id, "down", down_usd, down_mid):
                placed = True

        if placed:
            ws.last_order_time = time.time()

    def _execute_harvest(self, ws: WindowState, delta_bias: float,
                         order_budget: float, up_mid: float,
                         down_mid: float, dominant_mid: float):
        """
        HARVEST: Near-unilateral deployment on dominant side.
        Deep harvest (dominant_mid >= 0.95) allows penny collecting.
        Minority only on clear arb.
        """
        deep_harvest = dominant_mid >= DEEP_HARVEST_THRESHOLD

        # Determine dominant direction
        is_up_dominant = up_mid > down_mid

        if deep_harvest:
            # Aggressive: 90-95% dominant, penny collecting allowed
            dominant_pct = 0.92
        else:
            # Standard harvest: 85-90%
            dominant_pct = 0.87

        if is_up_dominant:
            up_usd = order_budget * dominant_pct
            down_usd = order_budget * (1.0 - dominant_pct)
        else:
            down_usd = order_budget * dominant_pct
            up_usd = order_budget * (1.0 - dominant_pct)

        placed = False
        if up_usd >= 0.50 and ws.up_token_id:
            if self._place_order(ws, ws.up_token_id, "up", up_usd, up_mid):
                placed = True
        if down_usd >= 0.50 and ws.down_token_id:
            if self._place_order(ws, ws.down_token_id, "down", down_usd, down_mid):
                placed = True

        if placed:
            ws.last_order_time = time.time()

    # ------------------------------------------------------------------
    #  Order Placement
    # ------------------------------------------------------------------

    def _place_order(self, ws: WindowState, token_id: str, direction: str,
                     amount_usd: float, mid: float) -> bool:
        """Place a single order. Returns True if recorded successfully."""
        # No upper mid rejection -- penny collecting is allowed (spec v2)
        if mid < 0.01:
            return False

        # Budget guard
        if not ws.can_spend(amount_usd):
            amount_usd = ws.get_budget_cap() - ws.inventory_total_usdc
            if amount_usd < 0.50:
                return False

        # Cap to executor limit
        amount_usd = min(amount_usd, self.executor.max_bet_usd)
        if amount_usd < 0.50:
            return False

        # Price cap: allow up to mid + 0.03, or 0.99 for penny collecting
        if mid >= PENNY_THRESHOLD:
            max_price = 0.99
        else:
            max_price = min(mid + 0.03, 0.99)

        if self.paper_mode:
            sim_price = min(mid + 0.02, 0.99)
            sim_shares = amount_usd / sim_price
            _log(
                f"[PAPER] {ws.slug} {ws.state} | {direction.upper()} "
                f"${amount_usd:.2f} @ ${sim_price:.3f} ({sim_shares:.1f}sh) | "
                f"mid={mid:.3f} bias={ws.inventory_bias:.3f} "
                f"{'LOW_CONF' if ws.low_confidence else ''}"
            )
            ws.record_order(direction, amount_usd, sim_shares, sim_price, ws.state)
            return True

        # Live order
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
            fill_price = max_price
            live_shares = amount_usd / fill_price
            ws.record_order(direction, amount_usd, live_shares, fill_price, ws.state)
            _log(
                f"[LIVE] {ws.slug} {ws.state} | {direction.upper()} "
                f"${amount_usd:.2f} @ ${fill_price:.3f} ({live_shares:.1f}sh) | "
                f"order={resp['orderID'][:12]}... CLOB={clob_ms:.0f}ms"
            )
            return True

        _log(
            f"Order failed: {ws.slug} {direction} ${amount_usd:.2f} "
            f"CLOB={clob_ms:.0f}ms"
        )
        return False

    # ------------------------------------------------------------------
    #  Window Finalization & Resolution
    # ------------------------------------------------------------------

    def _finalize_window(self, ws: WindowState):
        # Summary without orders for in-memory dashboard (lightweight)
        summary = ws.summary(include_orders=False)
        # Full record with orders for persistent trade log
        full_record = ws.summary(include_orders=True)

        with self._lock:
            self.completed_windows.append(summary)
            if len(self.completed_windows) > 200:
                self.completed_windows = self.completed_windows[-200:]

        # Append full record (with orders) to JSONL trade log
        self._append_trade_log(full_record)

        states_visited = [s for _, s in ws._state_history]
        _log(
            f"Window closed: {ws.slug} | Spent: ${ws.inventory_total_usdc:.2f}/{ws.max_budget:.0f} | "
            f"Dir: {ws.net_direction} (bias={ws.inventory_bias:.3f}) | "
            f"Orders: {ws.order_count} | States: {' -> '.join(states_visited)} | "
            f"Flips: {ws.flip_count} (struct: {ws.structural_flip_count}) | "
            f"UP: {ws.up_shares:.0f}sh / DOWN: {ws.down_shares:.0f}sh"
        )

        with self._lock:
            self._resolving_slugs.add(ws.slug)
        threading.Timer(120, self._resolve_window, args=[ws.slug, 0]).start()

    def _resolve_window(self, slug: str, attempt: int):
        with self._lock:
            if slug not in self._resolving_slugs:
                return

        outcome = None

        # Method 1: Polymarket settled outcome
        try:
            from src.polymarket import fetch_market_outcome
            outcome = fetch_market_outcome(slug)
        except Exception as e:
            _log(f"Polymarket resolution error for {slug}: {e}")

        # Cross-check Chainlink
        if outcome:
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
                    end_price, updated_at = get_btc_price_at_or_after_timestamp(window_end)
                    if end_price > 0 and updated_at >= window_end:
                        cl_winner = "up" if end_price > target_price else "down"
                        if cl_winner != outcome.lower():
                            _log(
                                f"Resolution mismatch {slug}: Polymarket={outcome} "
                                f"Chainlink={cl_winner} (end={end_price:.2f} vs target={target_price:.2f})"
                            )
                except Exception:
                    pass

        # Method 2: Chainlink fallback
        if not outcome and attempt >= 4:
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
                    end_price, updated_at = get_btc_price_at_or_after_timestamp(window_end)
                    if end_price > 0 and updated_at >= window_end:
                        outcome = "up" if end_price > target_price else "down"
                        _log(
                            f"Chainlink fallback for {slug}: {outcome} "
                            f"(end={end_price:.2f} vs target={target_price:.2f})"
                        )
                except Exception as e:
                    _log(f"Chainlink resolution error for {slug}: {e}")

        if not outcome:
            if attempt < 10:
                threading.Timer(30, self._resolve_window,
                                args=[slug, attempt + 1]).start()
            else:
                _log(f"Could not resolve {slug} after {attempt+1} attempts -- voiding")
                with self._lock:
                    for summary in self.completed_windows:
                        if summary["slug"] == slug and not summary.get("outcome"):
                            summary["outcome"] = "void"
                            summary["pnl"] = 0.0
                            break
                    self._resolving_slugs.discard(slug)
            return

        with self._lock:
            for summary in self.completed_windows:
                if summary["slug"] != slug or summary.get("outcome"):
                    continue

                summary["outcome"] = outcome

                up_shares = summary.get("up_shares", 0)
                down_shares = summary.get("down_shares", 0)
                total_spent = summary["budget_spent"]

                payout = up_shares if outcome == "up" else down_shares
                pnl = round(payout - total_spent, 2)
                summary["pnl"] = pnl

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
                        _log(
                            f"{self._consecutive_losses} consecutive losses "
                            f"-- pausing 30 min"
                        )

                correct = summary["net_direction"] == outcome
                self._resolving_slugs.discard(slug)
                _log(
                    f"RESOLVED: {slug} -> {outcome} | "
                    f"{'CORRECT' if correct else 'WRONG'} | "
                    f"Payout: ${payout:.2f} - Cost: ${total_spent:.2f} = "
                    f"P&L: ${pnl:+.2f} | Total: ${self.total_pnl:+.2f}"
                )
                # Update outcome/pnl in trade log
                self._update_trade_log_resolution(slug, outcome, pnl)
                break

    # ------------------------------------------------------------------
    #  Trade Log (append-only JSONL, all windows + orders)
    # ------------------------------------------------------------------

    def _append_trade_log(self, record: dict):
        """Append a full window record (with orders) to the JSONL trade log."""
        try:
            os.makedirs(os.path.dirname(PHASE_TRADE_LOG), exist_ok=True)
            with open(PHASE_TRADE_LOG, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            _log(f"Trade log append failed: {e}")

    def _update_trade_log_resolution(self, slug: str, outcome: str, pnl: float):
        """Update outcome/pnl for a window in the JSONL trade log.

        Reads the file, patches the matching line, rewrites.
        This runs once per resolution (~every 5 min), so the I/O is acceptable.
        """
        try:
            if not os.path.exists(PHASE_TRADE_LOG):
                return
            lines = []
            updated = False
            with open(PHASE_TRADE_LOG, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if not updated:
                        try:
                            record = json.loads(line)
                            if record.get("slug") == slug and not record.get("outcome"):
                                record["outcome"] = outcome
                                record["pnl"] = pnl
                                line = json.dumps(record)
                                updated = True
                        except json.JSONDecodeError:
                            pass
                    lines.append(line)
            if updated:
                tmp = PHASE_TRADE_LOG + ".tmp"
                with open(tmp, "w") as f:
                    f.write("\n".join(lines) + "\n")
                os.replace(tmp, PHASE_TRADE_LOG)
        except Exception as e:
            _log(f"Trade log resolution update failed: {e}")

    def load_trade_log(self) -> list[dict]:
        """Load all windows from the JSONL trade log. Returns list of records."""
        records = []
        try:
            if not os.path.exists(PHASE_TRADE_LOG):
                return records
            with open(PHASE_TRADE_LOG, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            _log(f"Trade log load failed: {e}")
        return records

    # ------------------------------------------------------------------
    #  Reset (purge all history)
    # ------------------------------------------------------------------

    def reset_stats(self):
        """Purge all phase history — completed windows, stats, trade log."""
        with self._lock:
            self.completed_windows.clear()
            self.total_pnl = 0.0
            self.wins = 0
            self.losses = 0
            self.total_windows = 0
            self._daily_pnl = 0.0
            self._daily_date = ""
            self._consecutive_losses = 0
            self._paused_until = 0.0

        # Delete persistent files
        for path in (PHASE_STATE_FILE, PHASE_TRADE_LOG):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                _log(f"Reset: could not delete {path}: {e}")

        _log("Phase stats and trade log purged")

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------

    def _check_daily_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_date:
            if self._daily_date:
                _log(f"Daily reset | Yesterday P&L: ${self._daily_pnl:+.2f}")
            self._daily_date = today
            self._daily_pnl = 0.0

    # ------------------------------------------------------------------
    #  State Persistence
    # ------------------------------------------------------------------

    def save_state(self):
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
        try:
            with open(PHASE_STATE_FILE, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        with self._lock:
            self.completed_windows = data.get("completed_windows", [])
            self.total_windows = data.get("total_windows", 0)

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
        print(
            f"[PHASE] State restored: {total} windows, {self.wins}W/{self.losses}L, "
            f"P&L ${self.total_pnl:+.2f}"
        )

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


# ---------------------------------------------------------------------------
#  Helpers (module-level)
# ---------------------------------------------------------------------------

def get_rebalance_split(rebalance_orders_remaining: int,
                        current_bias: float) -> float:
    """Returns up_pct for the next order after a backward transition."""
    if rebalance_orders_remaining <= 0:
        return 0.50
    if current_bias > 0.5:
        # Over-invested in Up -> buy more Down
        return 1.0 - REBALANCE_COUNTER_PCT  # 0.40 for Up
    else:
        return REBALANCE_COUNTER_PCT  # 0.60 for Up
