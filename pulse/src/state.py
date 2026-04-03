"""
Shared state between the bot and the dashboard.

Thread-safe data store that the bot writes to and the API reads from.
"""

import threading
from collections import deque
from datetime import datetime, timezone


class PulseState:
    """Thread-safe state container for the PULSE bot."""

    def __init__(self, start_capital: float = 1000.0):
        self._lock = threading.Lock()
        self.start_capital = start_capital
        self.capital = start_capital
        self.mode = "simulation"

        # Current window
        self.current_slug: str | None = None
        self.current_analysis: dict | None = None
        self.current_btc_price: float = 0
        self.current_target_price: float = 0

        # Trade history (most recent first)
        self.trades: deque[dict] = deque(maxlen=500)

        # Stats
        self.total_bets = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_skips = 0
        self.total_pnl = 0.0

        # P&L curve over time (timestamp, capital)
        self.pnl_curve: deque[dict] = deque(maxlen=2000)
        self.pnl_curve.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "capital": start_capital,
            "pnl": 0,
        })

        # Bot status
        self.bot_running = False
        self.last_update: str | None = None
        self.volatility: float = 0
        self.prices_loaded: int = 0

        # --- Follow Mode ---
        self.follow_trades: deque[dict] = deque(maxlen=500)
        self.follow_capital = start_capital
        self.follow_start_capital = start_capital
        self.follow_total_bets = 0
        self.follow_wins = 0
        self.follow_losses = 0
        self.follow_pnl = 0.0
        self.follow_pnl_curve: deque[dict] = deque(maxlen=2000)
        self.follow_pnl_curve.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "capital": start_capital,
            "pnl": 0,
        })

    def update_current(self, slug: str, analysis: dict, btc_price: float,
                       target_price: float, volatility: float):
        with self._lock:
            self.current_slug = slug
            self.current_analysis = analysis
            self.current_btc_price = btc_price
            self.current_target_price = target_price
            self.volatility = volatility
            self.last_update = datetime.now(timezone.utc).isoformat()

    def record_trade(self, trade: dict):
        with self._lock:
            # Deduplicate: only one bet per event slug
            slug = trade.get("event_slug")
            if trade.get("bet_placed") and slug:
                for existing in self.trades:
                    if existing.get("event_slug") == slug and existing.get("bet_placed"):
                        return

            self.trades.appendleft(trade)

            if trade.get("bet_placed"):
                self.total_bets += 1
            else:
                self.total_skips += 1

    def resolve_trade(self, slug: str, outcome: str, btc_close: float):
        """Resolve a pending trade after the 5-min window closes."""
        with self._lock:
            for trade in self.trades:
                if trade.get("event_slug") == slug and trade.get("outcome") == "pending":
                    trade["outcome"] = outcome
                    if trade.get("bet_placed"):
                        stake = trade.get("stake_usd", 0)
                        multiplier = trade.get("payout_multiplier", 0)
                        if outcome == "win":
                            pnl = stake * (multiplier - 1)
                            self.total_wins += 1
                        else:
                            pnl = -stake
                            self.total_losses += 1
                        trade["pnl_usd"] = round(pnl, 2)
                        self.total_pnl += pnl
                        self.capital += pnl

                        self.pnl_curve.append({
                            "time": datetime.now(timezone.utc).isoformat(),
                            "capital": round(self.capital, 2),
                            "pnl": round(self.total_pnl, 2),
                        })
                    break

    # ------------------------------------------------------------------
    #  Follow Mode
    # ------------------------------------------------------------------
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
                            sell_price: float) -> int:
        """Close pending follow trades via early exit (SELL).

        P&L = stake * (sell_price / buy_price - 1).
        Returns the number of trades closed.
        """
        with self._lock:
            closed = 0
            for trade in self.follow_trades:
                if (trade.get("event_slug") == event_slug
                        and trade.get("direction") == direction
                        and trade.get("outcome") == "pending"):
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

            if closed > 0:
                self.follow_pnl_curve.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "capital": round(self.follow_capital, 2),
                    "pnl": round(self.follow_pnl, 2),
                })
            return closed

    def get_follow_snapshot(self) -> dict:
        """Return follow-mode state for the API."""
        with self._lock:
            win_rate = (self.follow_wins / self.follow_total_bets * 100
                        if self.follow_total_bets > 0 else 0)
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
                    "wins": self.follow_wins,
                    "losses": self.follow_losses,
                    "win_rate": round(win_rate, 1),
                    "pending": sum(1 for t in self.follow_trades
                                   if t.get("outcome") == "pending"),
                },
                "trades": list(self.follow_trades),
                "pnl_curve": list(self.follow_pnl_curve),
            }

    def get_snapshot(self) -> dict:
        with self._lock:
            recent_trades = list(self.trades)
            win_rate = (self.total_wins / self.total_bets * 100
                        if self.total_bets > 0 else 0)

            return {
                "mode": self.mode,
                "bot_running": self.bot_running,
                "last_update": self.last_update,
                "capital": {
                    "start": self.start_capital,
                    "current": round(self.capital, 2),
                    "pnl": round(self.total_pnl, 2),
                    "pnl_pct": round(self.total_pnl / self.start_capital * 100, 2),
                },
                "stats": {
                    "total_bets": self.total_bets,
                    "wins": self.total_wins,
                    "losses": self.total_losses,
                    "skips": self.total_skips,
                    "win_rate": round(win_rate, 1),
                    "pending": sum(1 for t in self.trades
                                   if t.get("outcome") == "pending"
                                   and t.get("bet_placed")),
                },
                "current": {
                    "slug": self.current_slug,
                    "btc_price": self.current_btc_price,
                    "target_price": self.current_target_price,
                    "volatility": self.volatility,
                    "analysis": self.current_analysis,
                },
                "recent_trades": recent_trades,
                "pnl_curve": list(self.pnl_curve),
            }


# Global singleton
state = PulseState()
