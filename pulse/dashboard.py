"""
BLACK DELTA / PULSE Dashboard

Web dashboard for real-time monitoring of the PULSE bot.
Runs the bot in a background thread and serves a live dashboard.

Usage:
    python dashboard.py              # Start dashboard + bot in simulation
    python dashboard.py --port 8080  # Custom port
"""

import argparse
import os
import threading
import time
from datetime import datetime, timezone

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.polymarket import (
    fetch_event,
    fetch_market,
    fetch_market_outcome,
    fetch_midpoint,
    fetch_orderbook,
    get_current_event_slug,
    get_next_event_slug,
    parse_market_data,
)
from src.formula import PulseFormula
from src.btc_feed import BTCFeed
from src.follow_feed import FollowFeed
from src.state import state
from src.logger import log_trade

load_dotenv()

STAKE_USD = float(os.getenv("PULSE_STAKE_USD", "5"))
MIN_EDGE = float(os.getenv("PULSE_MIN_EDGE", "0.08"))
VOL_WINDOW = int(os.getenv("PULSE_VOLATILITY_WINDOW", "300"))
STUDENT_T_DF = float(os.getenv("PULSE_STUDENT_T_DF", "4"))
TAKER_SWITCH_SECONDS = 30
MAKER_PRICE_OFFSET = 0.01

# Follow mode config
FOLLOW_WALLETS = os.getenv("FOLLOW_WALLETS", "")  # comma-separated addresses
FOLLOW_MULTIPLIER = float(os.getenv("FOLLOW_MULTIPLIER", "1.0"))  # 1.0 = same size as source
FOLLOW_MAX_STAKE = float(os.getenv("FOLLOW_MAX_STAKE", "50"))     # safety cap per trade
FOLLOW_AUTO_MODE = os.getenv("FOLLOW_AUTO_MODE", "true").lower() in ("1", "true", "yes")
FOLLOW_AUTO_FRACTION = float(os.getenv("FOLLOW_AUTO_FRACTION", "0.001"))  # 0.1% of capital per bet

app = FastAPI(title="BLACK DELTA / PULSE")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Module-level references for API access
_btc_feed: BTCFeed | None = None
_follow_feed: FollowFeed | None = None

# Runtime-mutable follow config (dashboard can toggle)
_follow_config = {
    "auto_mode": FOLLOW_AUTO_MODE,
    "auto_fraction": FOLLOW_AUTO_FRACTION,
    "multiplier": FOLLOW_MULTIPLIER,
    "max_stake": FOLLOW_MAX_STAKE,
}


# --- Bot Logic (runs in background thread) ---

def get_best_bid(token_id: str) -> float | None:
    book = fetch_orderbook(token_id)
    if not book:
        return None
    bids = book.get("bids", [])
    if not bids:
        return None
    best = max(bids, key=lambda b: float(b["price"]))
    return float(best["price"])


def analyze_and_update(formula: PulseFormula, btc_feed: BTCFeed, slug: str):
    """Analyze a market and update shared state."""

    market_raw = fetch_market(slug)
    if not market_raw:
        return None

    market = parse_market_data(market_raw)
    if not market["down_token_id"]:
        return None

    btc_price = btc_feed.fetch_current_price()
    if not btc_price:
        return None

    down_mid = fetch_midpoint(market["down_token_id"])
    up_mid = fetch_midpoint(market["up_token_id"])
    down_price = down_mid if down_mid and down_mid > 0 else market["down_price"]
    up_price = up_mid if up_mid and up_mid > 0 else market["up_price"]

    if not down_price or not up_price:
        return None

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
        return None

    event_start_ts = int(slug.split("-")[-1])
    target_price = btc_feed.fetch_price_at_time(event_start_ts * 1000)
    if not target_price:
        target_price = btc_price

    vol = btc_feed.get_volatility_per_second()

    down_calc = formula.calculate(
        btc_price=btc_price, target_price=target_price,
        time_left_seconds=time_left, down_price=down_price,
        volatility_per_second=vol,
    )
    up_calc = formula.calculate_up(
        btc_price=btc_price, target_price=target_price,
        time_left_seconds=time_left, up_price=up_price,
        volatility_per_second=vol,
    )

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

    if best["edge"] < MIN_EDGE:
        best["should_bet"] = False

    # Filter: skip DOWN bets at sigma ≈ 0 (BTC has upward drift at target)
    if (direction == "down" and abs(best.get("sigma_move", 0)) < 0.05
            and best["should_bet"]):
        best["should_bet"] = False
        best["reason"] = "DOWN at sigma~0: upward drift bias"

    # Determine order type
    if best["should_bet"]:
        if time_left <= TAKER_SWITCH_SECONDS:
            if best.get("ev_per_dollar_taker", 0) > 0:
                order_type = "taker"
            else:
                best["should_bet"] = False
                order_type = "skip"
        else:
            order_type = "maker"
    else:
        order_type = "skip"

    # Calculate limit price for maker
    limit_price = None
    if order_type == "maker":
        bid = get_best_bid(token_id)
        limit_price = round((bid + MAKER_PRICE_OFFSET) if bid else contract_price, 2)
        limit_price = max(0.01, limit_price)

    analysis = {
        **best,
        "direction": direction,
        "order_type": order_type,
        "limit_price": limit_price,
        "contract_price": contract_price,
        "up_price": up_price,
        "down_price": down_price,
    }

    state.update_current(slug, analysis, btc_price, target_price, vol)

    # Build trade data but DON'T record it here.
    # The bot_loop handles recording with deduplication (one bet per window).
    trade = None
    if best["should_bet"]:
        trade = {
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
            "should_bet": True,
            "bet_placed": True,
            "stake_usd": STAKE_USD,
            "mode": "simulation",
            "outcome": "pending",
            "pnl_usd": 0,
            "order_type": order_type,
            "limit_price": limit_price,
            "time": datetime.now(timezone.utc).isoformat(),
        }

    return analysis, trade


def resolve_previous_window(btc_feed: BTCFeed, slug: str):
    """Check outcome of previous window. Tries Polymarket first, Binance fallback."""
    prev_ts = int(slug.split("-")[-1]) - 300
    prev_slug = f"btc-updown-5m-{prev_ts}"

    # Check if we have pending trades for this slug
    has_pending = any(
        t.get("event_slug") == prev_slug
        and t.get("outcome") == "pending"
        and t.get("bet_placed")
        for t in state.trades
    )
    if not has_pending:
        return

    # Primary: Polymarket actual outcome (Chainlink truth)
    market_winner = fetch_market_outcome(prev_slug)
    if market_winner:
        for trade in state.trades:
            if (trade.get("event_slug") == prev_slug
                    and trade.get("outcome") == "pending"
                    and trade.get("bet_placed")):
                won = (trade.get("direction") == market_winner)
                outcome = "win" if won else "lose"
                state.resolve_trade(prev_slug, outcome, 0)
        return

    # Fallback: Binance price comparison
    current_start_ts = int(slug.split("-")[-1])
    close_price = btc_feed.fetch_price_at_time(current_start_ts * 1000)
    open_price = btc_feed.fetch_price_at_time(prev_ts * 1000)

    if close_price and open_price:
        for trade in state.trades:
            if (trade.get("event_slug") == prev_slug
                    and trade.get("outcome") == "pending"
                    and trade.get("bet_placed")):
                direction = trade.get("direction")
                if direction == "down":
                    won = close_price < open_price
                else:
                    won = close_price >= open_price
                outcome = "win" if won else "lose"
                state.resolve_trade(prev_slug, outcome, close_price)


def resolve_all_pending(btc_feed: BTCFeed):
    """Sweep ALL pending trades and resolve using Polymarket's actual outcome.

    Priority: Polymarket Gamma API (Chainlink truth) > Binance fallback.
    """
    now = time.time()
    pending = [t for t in state.trades
               if t.get("outcome") == "pending" and t.get("bet_placed")]

    for trade in pending:
        slug = trade.get("event_slug")
        if not slug:
            continue
        try:
            window_start_ts = int(slug.split("-")[-1])
            window_end_ts = window_start_ts + 300

            # Only resolve if window has fully closed (small buffer for settlement)
            if now < window_end_ts + 15:
                continue

            direction = trade.get("direction")
            resolved = False

            # Primary: query Polymarket for the actual Chainlink-based outcome
            market_winner = fetch_market_outcome(slug)
            if market_winner:
                won = (direction == market_winner)
                outcome = "win" if won else "lose"
                state.resolve_trade(slug, outcome, 0)
                resolved = True

            # Fallback: Binance price comparison (if Polymarket not yet settled)
            if not resolved and now > window_end_ts + 180:
                open_price = btc_feed.fetch_price_at_time(window_start_ts * 1000)
                close_price = btc_feed.fetch_price_at_time(window_end_ts * 1000)
                if open_price and close_price:
                    if direction == "down":
                        won = close_price < open_price
                    else:
                        won = close_price >= open_price
                    outcome = "win" if won else "lose"
                    state.resolve_trade(slug, outcome, close_price)
        except (ValueError, IndexError):
            continue


def resolve_follow_pending(btc_feed: BTCFeed):
    """Sweep pending follow trades and resolve them."""
    now = time.time()
    pending = [t for t in state.follow_trades
               if t.get("outcome") == "pending"]

    for trade in pending:
        slug = trade.get("event_slug")
        if not slug:
            continue
        try:
            # Extract window timing from slug
            parts = slug.split("-")
            window_start_ts = int(parts[-1])
            # Determine window duration from slug pattern
            if "15m" in slug:
                window_dur = 900
            elif "5m" in slug:
                window_dur = 300
            else:
                window_dur = 300  # default
            window_end_ts = window_start_ts + window_dur

            if now < window_end_ts + 15:
                continue

            direction = trade.get("direction")
            resolved = False

            # Primary: Polymarket outcome
            market_winner = fetch_market_outcome(slug)
            if market_winner:
                won = (direction == market_winner)
                outcome = "win" if won else "lose"
                state.resolve_follow_trade(slug, outcome, 0)
                resolved = True

            # Fallback: Binance
            if not resolved and now > window_end_ts + 180:
                open_price = btc_feed.fetch_price_at_time(window_start_ts * 1000)
                close_price = btc_feed.fetch_price_at_time(window_end_ts * 1000)
                if open_price and close_price:
                    if direction == "down":
                        won = close_price < open_price
                    else:
                        won = close_price >= open_price
                    outcome = "win" if won else "lose"
                    state.resolve_follow_trade(slug, outcome, close_price)
        except (ValueError, IndexError):
            continue


def handle_follow_trade(trade_data: dict):
    """Callback when a followed user trades. Records a copy-trade."""
    # Only copy BUY trades (opening positions)
    if trade_data.get("side", "").upper() != "BUY":
        return

    outcome_raw = trade_data.get("outcome", "").lower()
    if outcome_raw == "up":
        direction = "up"
    elif outcome_raw == "down":
        direction = "down"
    else:
        direction = outcome_raw

    price = trade_data.get("price", 0)
    size = trade_data.get("size", 0)
    if price <= 0 or price >= 1:
        return

    source_cost = round(price * size, 2)

    if _follow_config["auto_mode"]:
        # Auto: fraction of current capital per bet
        fraction = _follow_config["auto_fraction"]
        stake = round(state.follow_capital * fraction, 2)
    else:
        # Manual: proportional to source with multiplier
        stake = round(source_cost * _follow_config["multiplier"], 2)

    stake = min(stake, _follow_config["max_stake"])
    stake = max(stake, 0.01)

    payout_multiplier = round(1.0 / price, 2) if price > 0 else 0

    copy_trade = {
        "event_slug": trade_data.get("event_slug", ""),
        "direction": direction,
        "contract_price": price,
        "payout_multiplier": payout_multiplier,
        "stake_usd": stake,
        "source_wallet": trade_data.get("wallet", ""),
        "source_name": trade_data.get("name", ""),
        "source_size": size,
        "source_cost": source_cost,
        "source_price": price,
        "tx_hash": trade_data.get("tx_hash", ""),
        "slug": trade_data.get("slug", ""),
        "title": trade_data.get("title", ""),
        "outcome": "pending",
        "pnl_usd": 0,
        "bet_placed": True,
        "mode": "simulation",
        "time": datetime.now(timezone.utc).isoformat(),
        "detected_at": trade_data.get("detected_at", time.time()),
    }

    state.record_follow_trade(copy_trade)
    print(f"[FOLLOW] Copied: {direction.upper()} @ ${price:.2f} "
          f"(source ${source_cost:.2f} -> our ${stake:.2f}) "
          f"from {copy_trade['source_name'] or 'unknown'}")


ANALYZE_INTERVAL = 1  # re-analyze every N seconds


def bot_loop(formula: PulseFormula, btc_feed: BTCFeed):
    """Main bot loop running in background thread.

    Price updates come from WebSocket callback + ticker thread (independent).
    This loop focuses on analysis and trade management.
    """
    state.bot_running = True
    state.mode = "simulation"

    # Wire real-time price callback: WS/ticker -> state (independent of this loop)
    def _on_price(price: float):
        state.current_btc_price = price
    btc_feed.on_price = _on_price

    print("[BOT] Bootstrapping BTC price feed...")
    btc_feed.bootstrap()
    state.prices_loaded = len(btc_feed.prices)
    print(f"[BOT] Loaded {state.prices_loaded} price points")
    print(f"[BOT] Volatility: {btc_feed.get_volatility_per_second():.8f}")
    print("[BOT] Running (1s tick, analyze every {0}s)...".format(ANALYZE_INTERVAL))

    last_slug = None
    last_analyze_time = 0
    last_sweep_time = 0
    bet_placed_slug = None  # track which window already has a bet

    while state.bot_running:
        try:
            slug = get_current_event_slug()
            now = time.time()

            if slug != last_slug:
                # New 5-minute window
                if last_slug:
                    resolve_previous_window(btc_feed, slug)
                last_slug = slug
                bet_placed_slug = None  # reset for new window

                result = analyze_and_update(formula, btc_feed, slug)
                if result is not None:
                    _analysis, trade = result
                    if trade and bet_placed_slug != slug:
                        state.record_trade(trade)
                        log_trade(trade)
                        bet_placed_slug = slug
                last_analyze_time = now

            elif now - last_analyze_time >= ANALYZE_INTERVAL:
                # Re-analyze periodically for fresh edge/display
                window_start_ts = int(slug.split("-")[-1])
                time_left = (window_start_ts + 300) - now
                if time_left > 5:
                    result = analyze_and_update(formula, btc_feed, slug)
                    if result is not None:
                        _analysis, trade = result
                        if trade and bet_placed_slug != slug:
                            state.record_trade(trade)
                            log_trade(trade)
                            bet_placed_slug = slug
                    last_analyze_time = now

            # Sweep all pending trades every 10s (PULSE + Follow)
            if now - last_sweep_time >= 10:
                resolve_all_pending(btc_feed)
                resolve_follow_pending(btc_feed)
                last_sweep_time = now

            time.sleep(1)
        except Exception as e:
            print(f"[BOT ERROR] {e}")
            time.sleep(2)


# --- API Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("templates/dashboard.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/state")
async def api_state():
    return JSONResponse(state.get_snapshot())


@app.get("/api/feed-status")
async def api_feed_status():
    """Diagnostic endpoint for BTC price feed health."""
    if _btc_feed:
        return JSONResponse(_btc_feed.get_feed_status())
    return JSONResponse({"error": "feed not initialized"})


@app.get("/api/follow")
async def api_follow():
    """Follow mode state: trades, stats, P&L."""
    data = state.get_follow_snapshot()
    if _follow_feed:
        data["feed"] = _follow_feed.get_status()
    else:
        data["feed"] = {"connected": False, "wallets_count": 0}
    data["config"] = {
        **_follow_config,
        "current_auto_stake": round(state.follow_capital * _follow_config["auto_fraction"], 2),
    }
    return JSONResponse(data)


@app.post("/api/follow/wallets")
async def api_follow_add_wallet(request: Request):
    """Add a wallet to follow."""
    body = await request.json()
    address = body.get("address", "").strip()
    label = body.get("label", "").strip()
    if not address or not address.startswith("0x") or len(address) < 10:
        return JSONResponse({"error": "Invalid wallet address"}, status_code=400)
    if not _follow_feed:
        return JSONResponse({"error": "Follow feed not initialized"}, status_code=500)
    info = _follow_feed.add_wallet(address, label)
    return JSONResponse({"ok": True, "wallet": {
        "address": info["address"],
        "label": info.get("label", ""),
        "proxy_wallet": info.get("proxy_wallet"),
    }})


@app.patch("/api/follow/config")
async def api_follow_update_config(request: Request):
    """Update follow config at runtime (auto_mode, fraction, multiplier, max)."""
    body = await request.json()
    if "auto_mode" in body:
        _follow_config["auto_mode"] = bool(body["auto_mode"])
    if "auto_fraction" in body:
        val = float(body["auto_fraction"])
        _follow_config["auto_fraction"] = max(0.0001, min(0.01, val))  # 0.01% - 1%
    if "multiplier" in body:
        val = float(body["multiplier"])
        _follow_config["multiplier"] = max(0.01, min(10.0, val))
    if "max_stake" in body:
        val = float(body["max_stake"])
        _follow_config["max_stake"] = max(1.0, val)
    return JSONResponse({"ok": True, "config": _follow_config})


@app.delete("/api/follow/wallets")
async def api_follow_remove_wallet(request: Request):
    """Remove a followed wallet."""
    body = await request.json()
    address = body.get("address", "").strip()
    if not address:
        return JSONResponse({"error": "Address required"}, status_code=400)
    if not _follow_feed:
        return JSONResponse({"error": "Follow feed not initialized"}, status_code=500)
    _follow_feed.remove_wallet(address)
    return JSONResponse({"ok": True})


@app.get("/api/follow/feed-status")
async def api_follow_feed_status():
    """Diagnostic endpoint for follow feed health."""
    if _follow_feed:
        return JSONResponse(_follow_feed.get_status())
    return JSONResponse({"error": "follow feed not initialized"})


@app.get("/api/formula")
async def api_formula():
    return JSONResponse({
        "name": "PULSE Edge Formula",
        "version": "Stage 1+2",
        "parameters": {
            "student_t_df": STUDENT_T_DF,
            "ewma_lambda": 0.94,
            "min_edge": MIN_EDGE,
            "stake_usd": STAKE_USD,
            "taker_fee": 0.072,
            "maker_fee": 0.0,
            "taker_switch_seconds": TAKER_SWITCH_SECONDS,
        },
        "description": {
            "overview": (
                "PULSE calculates the real probability that BTC will cross "
                "the target price within the 5-minute window. When this "
                "probability is higher than what the market implies, "
                "an edge exists."
            ),
            "steps": [
                {
                    "step": 1,
                    "name": "EWMA Volatility",
                    "formula": "var(t) = lambda * var(t-1) + (1-lambda) * r(t)^2",
                    "explanation": (
                        "Exponentially Weighted Moving Average: Weights recent "
                        "price movements more heavily than older ones. "
                        "Lambda=0.94 means the last ~17 data points account "
                        "for 50% of the variance. Reacts quickly to volatility "
                        "clusters (after a large move, the next large move "
                        "is more likely)."
                    ),
                },
                {
                    "step": 2,
                    "name": "Sigma Distance",
                    "formula": "sigma = distance / (price * vol * sqrt(time_left))",
                    "explanation": (
                        "How many standard deviations is the target price "
                        "away from the current price? The lower the sigma "
                        "value, the more likely the target will be crossed."
                    ),
                },
                {
                    "step": 3,
                    "name": "Student-t Distribution (Fat Tails)",
                    "formula": "real_prob = student_t.cdf(-sigma, df=4)",
                    "explanation": (
                        "Instead of the normal distribution, we use the "
                        "Student-t distribution with 4 degrees of freedom. "
                        "It has 'fatter tails' — extreme moves are more "
                        "likely than the normal distribution predicts. BTC "
                        "actually behaves this way: 3-sigma events occur "
                        "~10x more often than the bell curve suggests."
                    ),
                },
                {
                    "step": 4,
                    "name": "Edge & Expected Value",
                    "formula": "edge = real_prob - implied_prob; EV = prob * profit - (1-prob) * loss",
                    "explanation": (
                        "Edge = difference between our estimate and the "
                        "market price. EV = expected profit per dollar bet. "
                        "We only bet when both are positive and the edge "
                        "exceeds the minimum threshold."
                    ),
                },
                {
                    "step": 5,
                    "name": "Hybrid Order Strategy",
                    "formula": "if time > 30s: maker (0% fee); else: taker (7.2%)",
                    "explanation": (
                        "Early in the window we place limit orders (maker) "
                        "with no fee. In the last 30 seconds we switch to "
                        "market orders (taker) if the EV remains positive "
                        "after the 7.2% fee."
                    ),
                },
            ],
            "edge_source": (
                "The market systematically underprices tail events. Humans "
                "judge unlikely events as even more unlikely than they are "
                "(probability neglect). This is especially visible in BTC "
                "5-minute markets: when BTC is $80+ above the target, the "
                "market offers e.g. 200x — implying 0.5%. Statistically, "
                "the real probability is ~2-3%. This discrepancy is our edge."
            ),
        },
    })


# --- Startup ---

def main():
    parser = argparse.ArgumentParser(description="PULSE Dashboard")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    global _btc_feed, _follow_feed
    formula = PulseFormula(df=STUDENT_T_DF)
    btc_feed = BTCFeed(window_seconds=VOL_WINDOW)
    _btc_feed = btc_feed

    bot_thread = threading.Thread(target=bot_loop, args=(formula, btc_feed),
                                  daemon=True)
    bot_thread.start()

    # --- Follow Mode ---
    follow_feed = FollowFeed()
    _follow_feed = follow_feed
    follow_feed.on_trade = handle_follow_trade

    # Load persisted wallets from disk first
    follow_feed.load_wallets()

    # Then add any from .env (merges, no duplicates)
    if FOLLOW_WALLETS:
        for wallet in FOLLOW_WALLETS.split(","):
            wallet = wallet.strip()
            if wallet:
                follow_feed.add_wallet(wallet)

    if follow_feed.wallets:
        follow_feed.start()
        print(f"  Follow mode: tracking {len(follow_feed.wallets)} wallet(s)")
        print(f"  Follow multiplier: {FOLLOW_MULTIPLIER}x (max ${FOLLOW_MAX_STAKE})")
    else:
        print("  Follow mode: no wallets configured (add via dashboard)")

    print(f"\n  BLACK DELTA / PULSE Dashboard")
    print(f"  http://localhost:{args.port}\n")

    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
