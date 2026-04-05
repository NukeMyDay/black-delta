"""
Shared state between the bot and the dashboard.

Thread-safe data store that the bot writes to and the API reads from.
"""

import json
import os
import threading
from collections import deque
from datetime import datetime, timezone

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "follow_state.json")


class AppState:
    """Thread-safe state container for the app."""

    def __init__(self, start_capital: float = 1000.0):
        self._lock = threading.Lock()

        # Capital config (persisted)
        self.base_capital = float(start_capital)  # fallback if Polymarket balance unavailable
        self.polymarket_balance: float | None = None  # live balance from Polymarket API
        self.reinvest_rate = 0.80   # 0-1
        self.risk_level = 5         # 1-10, maps to Kelly fraction
        self.signal_pct = 100       # 0-100
        self.daily_loss_limit_pct = 10  # % of betting capital
        self.kill_switch = True     # Paused by default — must be activated manually

        # Investors (friends only — owner is not listed)
        self.investors: list[dict] = []

        # Daily stats tracking
        self._daily_date = ""
        self._daily_pnl = 0.0
        self._daily_bets = 0
        self._daily_wins = 0
        self._peak_capital = self.base_capital  # for drawdown

        # --- Follow Mode ---
        self.follow_trades: deque[dict] = deque(maxlen=500)
        self.follow_capital = start_capital
        self.follow_start_capital = start_capital
        self.follow_total_bets = 0
        self.follow_sell_events = 0   # SELL events received from source (1 per RTDS SELL)
        self.follow_total_sells = 0   # close operations (can be > sell_events due to partial)
        self.follow_wins = 0
        self.follow_losses = 0
        self.follow_pnl = 0.0
        self.follow_pnl_curve: deque[dict] = deque(maxlen=2000)
        self.follow_pnl_curve.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "capital": start_capital,
            "pnl": 0,
        })

    # ------------------------------------------------------------------
    #  Capital properties
    # ------------------------------------------------------------------
    @property
    def betting_capital(self):
        # Use live Polymarket balance if available, else fall back to base + P&L
        if self.polymarket_balance is not None:
            return self.polymarket_balance
        profit = self.follow_pnl
        if profit > 0:
            return self.base_capital + profit * self.reinvest_rate
        return self.base_capital + profit

    @property
    def reserved(self):
        profit = self.follow_pnl
        return max(0, profit) * (1 - self.reinvest_rate)

    @property
    def kelly_fraction(self):
        # 1=1/16, 2=3/32, 3=1/8, 4=3/16, 5=1/4(optimal), 6=5/16, 7=3/8, 8=7/16, 9=1/2, 10=1/2
        mapping = {
            1: 1/16, 2: 3/32, 3: 1/8, 4: 3/16, 5: 1/4,
            6: 5/16, 7: 3/8, 8: 7/16, 9: 1/2, 10: 1/2,
        }
        return mapping.get(self.risk_level, 0.25)

    @property
    def total_shares(self) -> float:
        return sum(inv["shares"] for inv in self.investors)

    @property
    def nav_per_share(self) -> float:
        ts = self.total_shares
        if ts <= 0:
            return 1.0
        return self.betting_capital / ts

    @property
    def total_accrued_fees(self) -> float:
        nav = self.nav_per_share
        total = 0.0
        for inv in self.investors:
            current_value = inv["shares"] * nav
            net_deposited = inv["invested"] - inv["withdrawn"]
            profit = current_value - net_deposited
            total += max(0, profit) * inv["fee_pct"]
        return round(total, 2)

    @property
    def max_drawdown(self):
        current = self.betting_capital
        if self._peak_capital <= 0:
            return 0
        return round((1 - current / self._peak_capital) * 100, 2) if current < self._peak_capital else 0

    # ------------------------------------------------------------------
    #  Daily stats
    # ------------------------------------------------------------------
    def record_daily_bet(self, pnl: float, won: bool):
        """Update daily stats and peak/drawdown tracking."""
        with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._daily_date != today:
                self._daily_date = today
                self._daily_pnl = 0.0
                self._daily_bets = 0
                self._daily_wins = 0

            self._daily_bets += 1
            self._daily_pnl += pnl
            if won:
                self._daily_wins += 1

            # Update peak capital for drawdown tracking
            current = self.betting_capital
            if current > self._peak_capital:
                self._peak_capital = current

    # ------------------------------------------------------------------
    #  Investors (share-based pooling)
    # ------------------------------------------------------------------
    def add_investor(self, name: str, fee_pct: float = 0.05) -> dict:
        """Add a new investor with 0 shares."""
        with self._lock:
            inv = {
                "name": name,
                "shares": 0.0,
                "invested": 0.0,
                "withdrawn": 0.0,
                "fee_pct": max(0.0, min(1.0, fee_pct)),
                "fee_shares": 0.0,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.investors.append(inv)
            return inv

    def remove_investor(self, index: int) -> bool:
        """Remove an investor. Must have 0 shares first."""
        with self._lock:
            if index < 0 or index >= len(self.investors):
                return False
            if self.investors[index]["shares"] > 0.001:
                return False
            self.investors.pop(index)
            return True

    def update_investor(self, index: int, name: str = None, fee_pct: float = None) -> bool:
        """Update investor name or fee percentage."""
        with self._lock:
            if index < 0 or index >= len(self.investors):
                return False
            if name is not None:
                self.investors[index]["name"] = name
            if fee_pct is not None:
                self.investors[index]["fee_pct"] = max(0.0, min(1.0, fee_pct))
            return True

    def deposit(self, index: int, amount: float) -> bool:
        """Deposit cash — creates new shares at current NAV."""
        with self._lock:
            if index < 0 or index >= len(self.investors) or amount <= 0:
                return False
            nav = self.nav_per_share
            new_shares = amount / nav
            self.investors[index]["shares"] += new_shares
            self.investors[index]["invested"] += amount
            self.base_capital += amount
            # Update peak for drawdown tracking
            current = self.betting_capital
            if current > self._peak_capital:
                self._peak_capital = current
            return True

    def withdraw(self, index: int, amount: float = None) -> dict | None:
        """Withdraw capital. Fee on positive profit goes to owner as shares."""
        with self._lock:
            if index < 0 or index >= len(self.investors):
                return None
            inv = self.investors[index]
            nav = self.nav_per_share
            current_value = inv["shares"] * nav

            if amount is None or amount >= current_value:
                amount = current_value

            if amount <= 0 or amount > current_value + 0.01:
                return None

            shares_to_redeem = amount / nav

            # Cost basis (proportional)
            net_deposited = inv["invested"] - inv["withdrawn"]
            withdraw_fraction = shares_to_redeem / inv["shares"] if inv["shares"] > 0 else 1.0
            cost_of_withdrawn = net_deposited * withdraw_fraction
            profit = amount - cost_of_withdrawn

            # Fee on positive profit only
            fee_pct = inv["fee_pct"]
            fee_amount = max(0, profit) * fee_pct
            fee_shares = fee_amount / nav if nav > 0 else 0

            net_payout = amount - fee_amount

            # Update investor
            inv["shares"] -= shares_to_redeem
            inv["withdrawn"] += net_payout
            inv["fee_shares"] += fee_shares
            # Fee stays in pool (increases owner's proportional share automatically)

            # Cash leaves the fund — only net payout actually exits
            self.base_capital -= net_payout

            return {
                "gross": round(amount, 2),
                "profit": round(profit, 2),
                "fee_pct": fee_pct,
                "fee_amount": round(fee_amount, 2),
                "net_payout": round(net_payout, 2),
                "shares_redeemed": round(shares_to_redeem, 4),
                "fee_shares_to_owner": round(fee_shares, 4),
            }

    def get_investor_snapshot(self) -> list[dict]:
        """Return investor data for the API."""
        with self._lock:
            nav = self.nav_per_share
            result = []
            for i, inv in enumerate(self.investors):
                current_value = inv["shares"] * nav
                net_deposited = inv["invested"] - inv["withdrawn"]
                profit = current_value - net_deposited
                fee_pct = inv["fee_pct"]
                accrued_fee = max(0, profit) * fee_pct
                netto = current_value - accrued_fee
                result.append({
                    "index": i,
                    "name": inv["name"],
                    "is_owner": i == 0,
                    "shares": round(inv["shares"], 4),
                    "invested": round(inv["invested"], 2),
                    "withdrawn": round(inv["withdrawn"], 2),
                    "current_value": round(current_value, 2),
                    "profit": round(profit, 2),
                    "fee_pct": round(fee_pct * 100, 1),
                    "accrued_fee": round(accrued_fee, 2),
                    "netto": round(netto, 2),
                })
            return result

    # ------------------------------------------------------------------
    #  Follow Mode
    # ------------------------------------------------------------------
    def record_follow_sell_event(self):
        """Count one SELL event received from the source user."""
        with self._lock:
            self.follow_sell_events += 1

    def record_follow_trade(self, trade: dict):
        """Record a copy-trade from a followed wallet."""
        with self._lock:
            # Dedup by tx_hash
            tx = trade.get("tx_hash")
            if tx:
                for existing in self.follow_trades:
                    if existing.get("tx_hash") == tx:
                        return
            self.follow_trades.appendleft(trade)
            self.follow_total_bets += 1

    def resolve_follow_trade(self, event_slug: str, outcome: str, _btc_close: float):
        """Resolve ALL pending follow trades for a given slug."""
        with self._lock:
            resolved_any = False
            for trade in self.follow_trades:
                if (trade.get("event_slug") == event_slug
                        and trade.get("outcome") == "pending"):
                    direction = trade.get("direction")
                    # Determine win/lose per trade based on its direction
                    if outcome in ("win", "lose"):
                        trade_outcome = outcome
                    else:
                        # outcome is the market winner ("up"/"down")
                        trade_outcome = "win" if direction == outcome else "lose"

                    trade["outcome"] = trade_outcome
                    stake = trade.get("stake_usd", 0)
                    multiplier = trade.get("payout_multiplier", 0)
                    if trade_outcome == "win":
                        pnl = stake * (multiplier - 1)
                        self.follow_wins += 1
                    else:
                        pnl = -stake
                        self.follow_losses += 1
                    trade["pnl_usd"] = round(pnl, 2)
                    self.follow_pnl += pnl
                    self.follow_capital += pnl
                    resolved_any = True

            if resolved_any:
                self.follow_pnl_curve.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "capital": round(self.follow_capital, 2),
                    "pnl": round(self.follow_pnl, 2),
                })

    def close_follow_trades(self, event_slug: str, direction: str,
                            sell_price: float, sell_size: float = 0) -> int:
        """Close pending follow trades via early exit (SELL).

        Proportional closing: compares sell_size to total source position.
        If sell_size covers >=99% of the position, all trades close.
        Otherwise, trades close FIFO (oldest first); the last trade may
        be split into a closed portion and a smaller remaining position.

        P&L per closed portion = stake * (sell_price / buy_price - 1).
        Returns the number of close operations performed.
        """
        with self._lock:
            # Gather all pending trades matching slug + direction
            pending = [
                t for t in self.follow_trades
                if (t.get("event_slug") == event_slug
                    and t.get("direction") == direction
                    and t.get("outcome") == "pending")
            ]
            if not pending:
                return 0

            total_source = sum(t.get("source_size", 0) for t in pending)
            # Fall back to full close when sell_size unknown or covers all
            fraction = (sell_size / total_source) if total_source > 0 and sell_size > 0 else 1.0

            closed = 0
            splits_to_add: list[dict] = []

            if fraction >= 0.99:
                # --- Full close: liquidate every pending trade ---
                for trade in pending:
                    buy_price = trade.get("contract_price", 0)
                    if buy_price <= 0:
                        continue
                    stake = trade.get("stake_usd", 0)
                    pnl = round(stake * (sell_price / buy_price - 1), 2)
                    trade["outcome"] = "closed"
                    trade["pnl_usd"] = pnl
                    trade["sell_price"] = sell_price
                    self.follow_pnl += pnl
                    self.follow_capital += pnl
                    if pnl >= 0:
                        self.follow_wins += 1
                    else:
                        self.follow_losses += 1
                    closed += 1
            else:
                # --- Partial close: FIFO (oldest first) ---
                remaining = sell_size
                for trade in reversed(pending):  # reversed = oldest first
                    if remaining <= 0:
                        break
                    buy_price = trade.get("contract_price", 0)
                    if buy_price <= 0:
                        continue

                    trade_size = trade.get("source_size", 0)
                    trade_stake = trade.get("stake_usd", 0)

                    if trade_size <= remaining:
                        # Close this trade fully
                        pnl = round(trade_stake * (sell_price / buy_price - 1), 2)
                        trade["outcome"] = "closed"
                        trade["pnl_usd"] = pnl
                        trade["sell_price"] = sell_price
                        self.follow_pnl += pnl
                        self.follow_capital += pnl
                        if pnl >= 0:
                            self.follow_wins += 1
                        else:
                            self.follow_losses += 1
                        remaining -= trade_size
                        closed += 1
                    else:
                        # Split: close a fraction, keep the rest pending
                        close_frac = remaining / trade_size
                        closed_stake = round(trade_stake * close_frac, 2)
                        pnl = round(closed_stake * (sell_price / buy_price - 1), 2)

                        # Build the closed split BEFORE modifying original
                        split = dict(trade)
                        split["outcome"] = "closed"
                        split["pnl_usd"] = pnl
                        split["sell_price"] = sell_price
                        split["stake_usd"] = closed_stake
                        split["source_size"] = round(remaining, 4)
                        split["partial_close"] = True
                        splits_to_add.append(split)

                        # Shrink the original trade (stays pending)
                        trade["stake_usd"] = round(trade_stake - closed_stake, 2)
                        trade["source_size"] = round(trade_size - remaining, 4)

                        self.follow_pnl += pnl
                        self.follow_capital += pnl
                        if pnl >= 0:
                            self.follow_wins += 1
                        else:
                            self.follow_losses += 1
                        remaining = 0
                        closed += 1

            # Insert split entries at the front of the deque
            for split in splits_to_add:
                self.follow_trades.appendleft(split)

            if closed > 0:
                self.follow_total_sells += closed
                self.follow_pnl_curve.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "capital": round(self.follow_capital, 2),
                    "pnl": round(self.follow_pnl, 2),
                })
            return closed

    def save_follow_state(self, path: str = STATE_FILE):
        """Persist follow state to disk (atomic write via temp file)."""
        with self._lock:
            data = {
                "follow_capital": self.follow_capital,
                "follow_start_capital": self.follow_start_capital,
                "follow_total_bets": self.follow_total_bets,
                "follow_sell_events": self.follow_sell_events,
                "follow_total_sells": self.follow_total_sells,
                "follow_wins": self.follow_wins,
                "follow_losses": self.follow_losses,
                "follow_pnl": self.follow_pnl,
                "follow_trades": list(self.follow_trades),
                "follow_pnl_curve": list(self.follow_pnl_curve),
                # Capital config
                "base_capital": self.base_capital,
                "reinvest_rate": self.reinvest_rate,
                "risk_level": self.risk_level,
                "signal_pct": self.signal_pct,
                "daily_loss_limit_pct": self.daily_loss_limit_pct,
                "peak_capital": self._peak_capital,
                "kill_switch": self.kill_switch,
                "investors": self.investors,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def load_follow_state(self, path: str = STATE_FILE):
        """Restore follow state from disk. Silently skips if file missing."""
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        with self._lock:
            self.follow_capital = data.get("follow_capital", self.follow_capital)
            self.follow_start_capital = data.get("follow_start_capital", self.follow_start_capital)
            self.follow_total_bets = data.get("follow_total_bets", 0)
            self.follow_sell_events = data.get("follow_sell_events", 0)
            self.follow_total_sells = data.get("follow_total_sells", 0)
            self.follow_wins = data.get("follow_wins", 0)
            self.follow_losses = data.get("follow_losses", 0)
            self.follow_pnl = data.get("follow_pnl", 0.0)

            trades = data.get("follow_trades", [])
            self.follow_trades = deque(trades, maxlen=500)

            curve = data.get("follow_pnl_curve", [])
            self.follow_pnl_curve = deque(curve, maxlen=2000)

            # Capital config (backward compatible -- use defaults if missing)
            self.base_capital = data.get("base_capital", self.base_capital)
            self.reinvest_rate = data.get("reinvest_rate", self.reinvest_rate)
            self.risk_level = data.get("risk_level", self.risk_level)
            self.signal_pct = data.get("signal_pct", self.signal_pct)
            self.daily_loss_limit_pct = data.get("daily_loss_limit_pct", self.daily_loss_limit_pct)
            self._peak_capital = data.get("peak_capital", self._peak_capital)
            if "kill_switch" in data:
                self.kill_switch = data["kill_switch"]

            # Investors — friends only, strip legacy "Owner" entry if present
            investors = data.get("investors", [])
            self.investors = [inv for inv in investors if inv.get("name") != "Owner"]

        saved_at = data.get("saved_at", "unknown")
        print(f"[STATE] Follow state restored from disk (saved {saved_at})")

    def get_follow_snapshot(self) -> dict:
        """Return follow-mode state for the API."""
        with self._lock:
            resolved = self.follow_wins + self.follow_losses
            win_rate = (self.follow_wins / resolved * 100 if resolved > 0 else 0)
            return {
                "capital": {
                    "start": self.follow_start_capital,
                    "current": round(self.follow_capital, 2),
                    "pnl": round(self.follow_pnl, 2),
                    "pnl_pct": round(self.follow_pnl / self.follow_start_capital * 100, 2)
                            if self.follow_start_capital else 0,
                },
                "stats": {
                    "total_bets": self.follow_total_bets,
                    "total_sells": self.follow_sell_events,
                    "wins": self.follow_wins,
                    "losses": self.follow_losses,
                    "win_rate": round(win_rate, 1),
                    "pending": sum(1 for t in self.follow_trades
                                   if t.get("outcome") == "pending"),
                },
                "trades": list(self.follow_trades),
                "pnl_curve": list(self.follow_pnl_curve),
            }


# Global singleton
state = AppState()
