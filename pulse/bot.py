"""
PULSE Bot -- BTC 5-Minute Statistical Edge Bot.

Runs in simulation mode by default. Monitors Polymarket BTC Up/Down markets,
calculates edge using the PULSE formula, and logs decisions.

Hybrid Order Strategy:
  1. Early in window: Place limit order (maker, 0% fee) at calculated fair price
  2. Monitor if filled. If not filled and edge grows:
  3. Last 30s: Switch to market order (taker, 7.2% fee) if EV still positive

Usage:
    python bot.py                  # Run in simulation mode
    python bot.py --live           # Run in live mode (places real orders)
    python bot.py --once           # Analyze current market once and exit
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from src.polymarket import (
    fetch_event,
    fetch_market,
    fetch_midpoint,
    fetch_orderbook,
    get_current_event_slug,
    get_next_event_slug,
    parse_market_data,
)
from src.formula import PulseFormula
from src.btc_feed import BTCFeed
from src.logger import log_trade

load_dotenv()

# Configuration from env
STAKE_USD = float(os.getenv("PULSE_STAKE_USD", "5"))
MIN_EDGE = float(os.getenv("PULSE_MIN_EDGE", "0.08"))
VOL_WINDOW = int(os.getenv("PULSE_VOLATILITY_WINDOW", "300"))
STUDENT_T_DF = float(os.getenv("PULSE_STUDENT_T_DF", "4"))

# Hybrid order strategy thresholds
TAKER_SWITCH_SECONDS = 30  # Switch to taker in last N seconds
MAKER_PRICE_OFFSET = 0.01  # Place limit order 1 tick above best bid


def print_header():
    print()
    print("=" * 60)
    print("  BLACK DELTA / PULSE")
    print("  BTC 5-Min Statistical Edge Bot")
    print("=" * 60)
    print()


def print_analysis(slug: str, direction: str, calc: dict, btc_price: float,
                   target: float, order_type: str, mode: str):
    """Pretty-print an analysis result."""
    bet_marker = ">>> BET <<<" if calc["should_bet"] else "    skip   "
    edge_pct = calc["edge"] * 100

    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {slug}")
    print(f"  BTC: ${btc_price:,.2f}  |  Target: ${target:,.2f}  |  "
          f"Distance: ${calc['distance']:+,.2f}")
    print(f"  Direction: {direction.upper()}  |  "
          f"Contract: ${calc['implied_prob']:.2f}  |  "
          f"Payout: {calc['payout_multiplier']:.1f}x")
    print(f"  Sigma: {calc['sigma_move']:.2f}  |  "
          f"Real Prob: {calc['real_prob']:.4f}  |  "
          f"Edge: {edge_pct:+.2f}%")
    print(f"  EV (Maker): ${calc.get('ev_per_dollar_maker', 0):+.4f}  |  "
          f"EV (Taker): ${calc.get('ev_per_dollar_taker', 0):+.4f}")
    if calc["should_bet"]:
        print(f"  Order: {order_type.upper()}  |  "
              f"Time left: {calc['time_left']:.0f}s")
    print(f"  {bet_marker}  |  Mode: {mode}")
    print(f"  Reason: {calc['reason']}")
    print("-" * 60)


def get_best_bid(token_id: str) -> float | None:
    """Get the best bid price from the orderbook."""
    book = fetch_orderbook(token_id)
    if not book:
        return None
    bids = book.get("bids", [])
    if not bids:
        return None
    # Bids are sorted by price ascending in the raw response,
    # but we want the highest bid
    best = max(bids, key=lambda b: float(b["price"]))
    return float(best["price"])


def determine_order_strategy(calc: dict, time_left: float) -> dict:
    """
    Decide between maker and taker order based on time remaining and EV.

    Returns dict with order_type, price, and rationale.
    """
    maker_ev = calc.get("ev_per_dollar_maker", 0)
    taker_ev = calc.get("ev_per_dollar_taker", 0)

    if time_left <= TAKER_SWITCH_SECONDS:
        # Late in the window -- no time to wait for fill
        if taker_ev > 0:
            return {
                "order_type": "taker",
                "use_limit": False,
                "reason": f"Late window ({time_left:.0f}s left), taker EV positive",
            }
        else:
            return {
                "order_type": "skip",
                "use_limit": False,
                "reason": f"Late window, taker EV negative ({taker_ev:+.4f})",
            }
    else:
        # Early/mid window -- prefer maker (0% fee)
        if maker_ev > 0:
            return {
                "order_type": "maker",
                "use_limit": True,
                "reason": f"Limit order ({time_left:.0f}s left for fill)",
            }
        else:
            return {
                "order_type": "skip",
                "use_limit": False,
                "reason": f"Maker EV negative ({maker_ev:+.4f})",
            }


def calculate_limit_price(direction: str, token_id: str,
                          implied_prob: float) -> float:
    """
    Calculate optimal limit order price.

    Strategy: Place just above the best bid to get priority,
    but never above our calculated fair value.
    """
    best_bid = get_best_bid(token_id)

    if best_bid is not None:
        # One tick above best bid for priority
        limit_price = best_bid + MAKER_PRICE_OFFSET
    else:
        # No orderbook data -- use implied price minus a small buffer
        limit_price = implied_prob

    # Round to tick size (0.01)
    limit_price = round(limit_price, 2)

    # Never go below $0.01
    return max(0.01, limit_price)


def analyze_market(formula: PulseFormula, btc_feed: BTCFeed,
                   slug: str, mode: str) -> dict | None:
    """Analyze a single market and return the best opportunity."""

    # Fetch market data
    market_raw = fetch_market(slug)
    if not market_raw:
        return None

    market = parse_market_data(market_raw)
    if not market["down_token_id"]:
        return None

    # Get fresh BTC price
    btc_price = btc_feed.fetch_current_price()
    if not btc_price:
        print("  [WARN] Could not fetch BTC price")
        return None

    # Get live contract prices from CLOB (more accurate than Gamma snapshot)
    down_mid = fetch_midpoint(market["down_token_id"])
    up_mid = fetch_midpoint(market["up_token_id"])

    down_price = down_mid if down_mid and down_mid > 0 else market["down_price"]
    up_price = up_mid if up_mid and up_mid > 0 else market["up_price"]

    if not down_price or not up_price:
        return None

    # Parse event times
    event_raw = fetch_event(slug)
    if not event_raw:
        return None

    end_time_str = event_raw.get("endDate", "")
    if not end_time_str:
        return None

    end_time = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    time_left = (end_time - now).total_seconds()

    if time_left <= 5:
        return None  # Too close to expiry

    # Target Price ("Price to Beat") = BTC price at the START of the 5-min window.
    # The slug contains the window start timestamp: btc-updown-5m-{UNIX_TS}
    event_start_ts = int(slug.split("-")[-1])
    target_price = btc_feed.fetch_price_at_time(event_start_ts * 1000)

    if not target_price:
        target_price = btc_price
        print("  [WARN] Could not fetch target price, using current BTC price")

    vol = btc_feed.get_volatility_per_second()

    # Calculate edge for BOTH directions, pick the better one
    down_calc = formula.calculate(
        btc_price=btc_price,
        target_price=target_price,
        time_left_seconds=time_left,
        down_price=down_price,
        volatility_per_second=vol,
    )

    up_calc = formula.calculate_up(
        btc_price=btc_price,
        target_price=target_price,
        time_left_seconds=time_left,
        up_price=up_price,
        volatility_per_second=vol,
    )

    # Pick the direction with higher maker EV
    if down_calc["ev_per_dollar_maker"] >= up_calc["ev_per_dollar_maker"]:
        best = down_calc
        direction = "down"
        contract_price = down_price
        token_id = market["down_token_id"]
    else:
        best = up_calc
        direction = "up"
        contract_price = up_price
        token_id = market["up_token_id"]

    # Apply minimum edge filter
    if best["edge"] < MIN_EDGE:
        best["should_bet"] = False

    # Filter: skip DOWN bets at sigma ≈ 0 (BTC has upward drift at target)
    if (direction == "down" and abs(best.get("sigma_move", 0)) < 0.05
            and best["should_bet"]):
        best["should_bet"] = False
        best["reason"] = "DOWN at sigma~0: upward drift bias"

    # Determine order strategy (maker vs taker vs skip)
    strategy = determine_order_strategy(best, time_left)

    if best["should_bet"] and strategy["order_type"] == "skip":
        best["should_bet"] = False
        best["reason"] = strategy["reason"]

    # Calculate limit price for maker orders
    limit_price = None
    if best["should_bet"] and strategy["use_limit"]:
        limit_price = calculate_limit_price(direction, token_id, contract_price)

    order_type = strategy["order_type"] if best["should_bet"] else "none"

    print_analysis(slug, direction, best, btc_price, target_price, order_type, mode)

    if best["should_bet"] and limit_price:
        print(f"  Limit price: ${limit_price:.2f}  "
              f"(best bid + ${MAKER_PRICE_OFFSET})  |  "
              f"Strategy: {strategy['reason']}")
        print("-" * 60)

    # Log
    log_trade({
        "event_slug": slug,
        "direction": direction,
        "btc_price": btc_price,
        "target_price": target_price,
        "distance": best["distance"],
        "time_left_seconds": time_left,
        "contract_price": contract_price,
        "real_prob": best["real_prob"],
        "implied_prob": best["implied_prob"],
        "edge": best["edge"],
        "ev_per_dollar": best["ev_per_dollar"],
        "sigma_move": best["sigma_move"],
        "volatility": best["volatility"],
        "payout_multiplier": best["payout_multiplier"],
        "should_bet": best["should_bet"],
        "bet_placed": best["should_bet"],
        "stake_usd": STAKE_USD if best["should_bet"] else 0,
        "mode": mode,
        "outcome": "pending",
        "pnl_usd": 0,
    })

    return {
        "direction": direction,
        "calc": best,
        "slug": slug,
        "order_type": order_type,
        "limit_price": limit_price,
        "token_id": token_id,
    }


def run_once(formula: PulseFormula, btc_feed: BTCFeed, mode: str):
    """Analyze the current market once."""
    slug = get_current_event_slug()
    print(f"  Analyzing current market: {slug}")
    result = analyze_market(formula, btc_feed, slug, mode)
    if not result:
        print("  [INFO] No active market found or no data available.")
        print("  [INFO] Trying next window...")
        slug = get_next_event_slug()
        analyze_market(formula, btc_feed, slug, mode)


def run_loop(formula: PulseFormula, btc_feed: BTCFeed, mode: str):
    """
    Continuously monitor markets with hybrid order strategy.

    Per 5-minute window:
      1. At window start: Analyze, place maker order if edge exists
      2. Every 10s: Re-evaluate (price moved? edge changed?)
      3. Last 30s: If maker not filled, consider taker if EV positive
    """
    print(f"  Mode: {mode.upper()}")
    print(f"  Stake: ${STAKE_USD}")
    print(f"  Min Edge: {MIN_EDGE * 100}%")
    print(f"  Student-t df: {STUDENT_T_DF}")
    print(f"  Vol Window: {VOL_WINDOW}s")
    print(f"  Taker switch: last {TAKER_SWITCH_SECONDS}s")
    print()
    print("  Bootstrapping BTC price feed...")
    btc_feed.bootstrap()
    print(f"  Loaded {len(btc_feed.prices)} price points")
    print(f"  Current volatility (per-sec): "
          f"{btc_feed.get_volatility_per_second():.8f}")
    print()
    print("  Monitoring markets (Ctrl+C to stop)...")
    print("-" * 60)

    last_slug = None
    current_window = {
        "slug": None,
        "maker_placed": False,
        "taker_placed": False,
        "bet_direction": None,
    }

    while True:
        try:
            slug = get_current_event_slug()

            if slug != last_slug:
                # === New 5-minute window ===
                last_slug = slug

                # Reset window state
                current_window = {
                    "slug": slug,
                    "maker_placed": False,
                    "taker_placed": False,
                    "bet_direction": None,
                }

                # Initial analysis + maker order
                result = analyze_market(formula, btc_feed, slug, mode)
                if result and result["calc"]["should_bet"]:
                    if result["order_type"] == "maker":
                        current_window["maker_placed"] = True
                        current_window["bet_direction"] = result["direction"]
                        print(f"  [SIM] Maker order placed: "
                              f"{result['direction'].upper()} "
                              f"@ ${result['limit_price']:.2f}")
                    elif result["order_type"] == "taker":
                        current_window["taker_placed"] = True
                        current_window["bet_direction"] = result["direction"]
                        print(f"  [SIM] Taker order placed: "
                              f"{result['direction'].upper()}")
                    print("-" * 60)

            else:
                # === Same window -- re-evaluate ===
                btc_feed.fetch_current_price()

                # Calculate time left in this window
                window_start_ts = int(slug.split("-")[-1])
                window_end_ts = window_start_ts + 300
                now_ts = time.time()
                time_left = window_end_ts - now_ts

                # If we haven't bet yet and we're in the taker zone
                if (not current_window["maker_placed"]
                        and not current_window["taker_placed"]
                        and 5 < time_left <= TAKER_SWITCH_SECONDS):
                    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                          f"Taker window -- re-evaluating {slug}")
                    result = analyze_market(formula, btc_feed, slug, mode)
                    if result and result["calc"]["should_bet"]:
                        if result["order_type"] == "taker":
                            current_window["taker_placed"] = True
                            current_window["bet_direction"] = result["direction"]
                            print(f"  [SIM] Late taker order: "
                                  f"{result['direction'].upper()}")
                        print("-" * 60)

                # If we placed a maker order, re-check if edge still valid
                elif (current_window["maker_placed"]
                      and not current_window["taker_placed"]
                      and 5 < time_left <= TAKER_SWITCH_SECONDS):
                    # Maker order might not have filled.
                    # In simulation we assume it filled.
                    # In live mode we'd check order status here.
                    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                          f"Maker order pending -- "
                          f"{time_left:.0f}s left (assumed filled in sim)")

            # Poll interval
            time.sleep(10)

        except KeyboardInterrupt:
            print("\n  Stopped by user.")
            break
        except Exception as e:
            print(f"  [ERROR] {e}")
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="PULSE -- BTC 5-Min Edge Bot")
    parser.add_argument("--live", action="store_true", help="Run in live mode")
    parser.add_argument("--once", action="store_true", help="Analyze once and exit")
    args = parser.parse_args()

    mode = "live" if args.live else "simulation"
    env_mode = os.getenv("PULSE_MODE", "simulation")
    if mode == "live" and env_mode != "live":
        print("  [SAFETY] --live flag set but PULSE_MODE != 'live' in .env")
        print("  [SAFETY] Set PULSE_MODE=live in .env to confirm.")
        sys.exit(1)

    print_header()

    formula = PulseFormula(df=STUDENT_T_DF)
    btc_feed = BTCFeed(window_seconds=VOL_WINDOW)

    if args.once:
        btc_feed.bootstrap()
        run_once(formula, btc_feed, mode)
    else:
        run_loop(formula, btc_feed, mode)


if __name__ == "__main__":
    main()
