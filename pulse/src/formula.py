"""
PULSE Edge Formula — calculates whether a BTC 5-min contract has positive EV.

Stufe 1+2: Student-t distribution (fat tails) + EWMA volatility.
"""

import numpy as np
from scipy.stats import t as student_t

# Taker fee for crypto markets (from API: rate=0.072)
# Maker fee is 0% (takerOnly=true) with 20% rebate on taker fees
TAKER_FEE_RATE = 0.072
MAKER_FEE_RATE = 0.0


class PulseFormula:
    """Calculates real probability and edge for BTC 5-min contracts."""

    def __init__(self, df: float = 4.0, ewma_lambda: float = 0.94):
        """
        Args:
            df: Student-t degrees of freedom. Lower = fatter tails.
                4 is a good starting point for BTC.
            ewma_lambda: EWMA decay factor. 0.94 is standard (RiskMetrics).
                Lower = more reactive to recent volatility.
        """
        self.df = df
        self.ewma_lambda = ewma_lambda
        self._ewma_variance: float | None = None

    def update_volatility(self, returns: list[float]) -> float:
        """
        Calculate EWMA volatility from a series of returns.

        Args:
            returns: List of log returns (e.g., from 1-second BTC price ticks).

        Returns:
            Annualized volatility estimate.
        """
        if len(returns) < 2:
            # Fallback: BTC average daily vol ~2.5%
            return 0.025

        variance = np.var(returns)
        for r in returns:
            variance = self.ewma_lambda * variance + (1 - self.ewma_lambda) * r ** 2
        self._ewma_variance = variance

        # Convert to annualized (returns are per-second, 86400 seconds/day)
        # But we work in daily vol for the formula, so just scale from
        # per-tick to per-day based on tick interval
        return float(np.sqrt(variance))

    def calculate(
        self,
        btc_price: float,
        target_price: float,
        time_left_seconds: float,
        down_price: float,
        volatility_per_second: float,
    ) -> dict:
        """
        Calculate edge for a DOWN bet.

        We focus on DOWN because that's where the fat-tail edge lives:
        the market underprices the probability of BTC dropping below target.

        Args:
            btc_price: Current BTC price.
            target_price: The "Price to Beat".
            time_left_seconds: Seconds until contract expires.
            down_price: Current price of the DOWN contract ($0.01-$0.99).
            volatility_per_second: EWMA volatility of per-second returns.

        Returns:
            Dict with real_prob, implied_prob, edge, ev, should_bet, details.
        """
        if time_left_seconds <= 0 or down_price <= 0 or down_price >= 1:
            return self._no_bet("Invalid inputs")

        distance = btc_price - target_price  # positive = BTC above target

        # Expected move in the remaining time
        # Scale per-second vol to the remaining window
        expected_move = btc_price * volatility_per_second * np.sqrt(time_left_seconds)

        if expected_move <= 0:
            return self._no_bet("Zero expected move")

        # How many sigma away is the target?
        sigma_move = distance / expected_move

        # Student-t CDF: probability of BTC ending below target
        # (i.e., dropping by at least 'distance' in 'time_left_seconds')
        if distance > 0:
            # BTC is ABOVE target — need a drop
            real_prob = float(student_t.cdf(-sigma_move, df=self.df))
        else:
            # BTC is BELOW target — already "down", need to stay there
            real_prob = float(student_t.cdf(-sigma_move, df=self.df))

        # Implied probability from market price
        implied_prob = down_price

        # Edge
        edge = real_prob - implied_prob

        # Expected value per $1 bet, accounting for fees
        # Maker (limit order): 0% fee
        # Taker (market order): 7.2% fee
        payout = 1.0

        # Maker EV (our preferred strategy — limit orders)
        maker_cost = down_price
        maker_profit = payout - maker_cost
        maker_loss = maker_cost
        maker_ev = (real_prob * maker_profit) - ((1 - real_prob) * maker_loss)

        # Taker EV (for comparison / urgency)
        taker_cost = down_price * (1 + TAKER_FEE_RATE)
        taker_profit = payout - taker_cost
        taker_loss = taker_cost
        taker_ev = (real_prob * taker_profit) - ((1 - real_prob) * taker_loss)

        maker_multiplier = payout / maker_cost if maker_cost > 0 else 0

        return {
            "real_prob": round(real_prob, 6),
            "implied_prob": round(implied_prob, 4),
            "edge": round(edge, 6),
            "ev_per_dollar_maker": round(maker_ev, 4),
            "ev_per_dollar_taker": round(taker_ev, 4),
            "ev_per_dollar": round(maker_ev, 4),  # default to maker
            "payout_multiplier": round(maker_multiplier, 2),
            "sigma_move": round(sigma_move, 3),
            "volatility": round(volatility_per_second, 8),
            "distance": round(distance, 2),
            "time_left": round(time_left_seconds, 1),
            "effective_cost_maker": round(maker_cost, 4),
            "effective_cost_taker": round(taker_cost, 4),
            "effective_cost": round(maker_cost, 4),
            "should_bet": edge > 0 and maker_ev > 0,
            "reason": self._reason(edge, maker_ev, sigma_move),
        }

    def calculate_up(
        self,
        btc_price: float,
        target_price: float,
        time_left_seconds: float,
        up_price: float,
        volatility_per_second: float,
    ) -> dict:
        """Same as calculate() but for an UP bet (when BTC is below target)."""
        if time_left_seconds <= 0 or up_price <= 0 or up_price >= 1:
            return self._no_bet("Invalid inputs")

        distance = target_price - btc_price  # positive = BTC below target

        expected_move = btc_price * volatility_per_second * np.sqrt(time_left_seconds)
        if expected_move <= 0:
            return self._no_bet("Zero expected move")

        sigma_move = distance / expected_move
        real_prob = float(student_t.cdf(-sigma_move, df=self.df))

        implied_prob = up_price
        edge = real_prob - implied_prob

        payout = 1.0

        maker_cost = up_price
        maker_ev = (real_prob * (payout - maker_cost)) - ((1 - real_prob) * maker_cost)

        taker_cost = up_price * (1 + TAKER_FEE_RATE)
        taker_ev = (real_prob * (payout - taker_cost)) - ((1 - real_prob) * taker_cost)

        maker_multiplier = payout / maker_cost if maker_cost > 0 else 0

        return {
            "real_prob": round(real_prob, 6),
            "implied_prob": round(implied_prob, 4),
            "edge": round(edge, 6),
            "ev_per_dollar_maker": round(maker_ev, 4),
            "ev_per_dollar_taker": round(taker_ev, 4),
            "ev_per_dollar": round(maker_ev, 4),
            "payout_multiplier": round(maker_multiplier, 2),
            "sigma_move": round(sigma_move, 3),
            "volatility": round(volatility_per_second, 8),
            "distance": round(distance, 2),
            "time_left": round(time_left_seconds, 1),
            "effective_cost_maker": round(maker_cost, 4),
            "effective_cost_taker": round(taker_cost, 4),
            "effective_cost": round(maker_cost, 4),
            "should_bet": edge > 0 and maker_ev > 0,
            "reason": self._reason(edge, maker_ev, sigma_move),
        }

    def _reason(self, edge: float, ev: float, sigma: float) -> str:
        if ev <= 0:
            return f"Negative EV ({ev:.4f})"
        if edge <= 0:
            return f"No edge (edge={edge:.4f})"
        if sigma < 1:
            return f"BET - low sigma ({sigma:.1f}), high probability of crossing"
        if sigma < 2:
            return f"BET - moderate sigma ({sigma:.1f}), edge={edge:.3f}"
        return f"BET - tail event ({sigma:.1f} sigma), edge={edge:.4f}"

    def _no_bet(self, reason: str) -> dict:
        return {
            "real_prob": 0, "implied_prob": 0, "edge": 0, "ev_per_dollar": 0,
            "payout_multiplier": 0, "sigma_move": 0, "volatility": 0,
            "distance": 0, "time_left": 0, "effective_cost": 0,
            "should_bet": False, "reason": reason,
        }
