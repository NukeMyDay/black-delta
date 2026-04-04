"""
BLACK DELTA / PULSE Dashboard

Web dashboard for real-time monitoring of the PULSE bot.
Runs the bot in a background thread and serves a live dashboard.

Usage:
    python dashboard.py              # Start dashboard + bot in simulation
    python dashboard.py --port 8080  # Custom port
"""

import argparse
import csv
import io
import json
import os
import threading
import time
from datetime import datetime, timezone

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
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
from src.signal import SignalAggregator
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
_signal: SignalAggregator | None = None

# Runtime-mutable follow config (dashboard can toggle)
_follow_config = {
    "auto_mode": FOLLOW_AUTO_MODE,
    "auto_fraction": FOLLOW_AUTO_FRACTION,
    "multiplier": FOLLOW_MULTIPLIER,
    "max_stake": FOLLOW_MAX_STAKE,
    "simulation": True,  # True = simulation (1:1 copy), False = real (sized by auto/manual)
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
    """Sweep pending follow trades, grouped by slug to minimize API calls."""
    now = time.time()
    pending = [t for t in state.follow_trades
               if t.get("outcome") == "pending"]

    # Group by slug — one API call per unique slug
    slugs_seen: dict[str, bool] = {}  # slug -> resolved?

    for trade in pending:
        slug = trade.get("event_slug")
        if not slug or slug in slugs_seen:
            continue

        try:
            parts = slug.split("-")
            window_start_ts = int(parts[-1])
            if "15m" in slug:
                window_dur = 900
            elif "5m" in slug:
                window_dur = 300
            elif "1h" in slug:
                window_dur = 3600
            else:
                window_dur = 300
            window_end_ts = window_start_ts + window_dur

            if now < window_end_ts + 15:
                slugs_seen[slug] = False
                continue

            # Primary: Polymarket outcome (one call per slug)
            market_winner = fetch_market_outcome(slug)
            if market_winner:
                # Pass the winner direction — resolve_follow_trade handles per-trade win/lose
                state.resolve_follow_trade(slug, market_winner, 0)
                slugs_seen[slug] = True
                continue

            # Fallback: Binance (only for BTC markets, after 3 min buffer)
            if not market_winner and now > window_end_ts + 180 and "btc" in slug:
                open_price = btc_feed.fetch_price_at_time(window_start_ts * 1000)
                close_price = btc_feed.fetch_price_at_time(window_end_ts * 1000)
                if open_price and close_price:
                    winner = "down" if close_price < open_price else "up"
                    state.resolve_follow_trade(slug, winner, close_price)
                    slugs_seen[slug] = True
                    continue

            slugs_seen[slug] = False
        except (ValueError, IndexError):
            slugs_seen[slug] = False


def handle_follow_trade(trade_data: dict):
    """Callback when a followed user trades. Handles BUY (open) and SELL (close)."""
    side = trade_data.get("side", "").upper()
    outcome_raw = trade_data.get("outcome", "").lower()
    direction = "up" if outcome_raw == "up" else "down" if outcome_raw == "down" else outcome_raw
    price = trade_data.get("price", 0)
    size = trade_data.get("size", 0)
    event_slug = trade_data.get("event_slug", "")

    if price <= 0 or price >= 1:
        return

    if side == "SELL":
        # Early exit: close matching pending BUY trades for this slug + direction
        _handle_follow_sell(trade_data, direction, price, size, event_slug)
        return

    if side != "BUY":
        return

    # --- BUY: open a new position ---
    source_cost = round(price * size, 2)

    if _follow_config["simulation"]:
        stake = source_cost
    elif _follow_config["auto_mode"]:
        fraction = _follow_config["auto_fraction"]
        stake = round(state.follow_capital * fraction, 2)
        stake = min(stake, _follow_config["max_stake"])
    else:
        stake = round(source_cost * _follow_config["multiplier"], 2)
        stake = min(stake, _follow_config["max_stake"])

    stake = max(stake, 0.01)
    payout_multiplier = round(1.0 / price, 2) if price > 0 else 0

    copy_trade = {
        "event_slug": event_slug,
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
    print(f"[FOLLOW] BUY copied: {direction.upper()} @ ${price:.2f} "
          f"(source ${source_cost:.2f} -> our ${stake:.2f}) "
          f"from {copy_trade['source_name'] or 'unknown'}")


def _handle_follow_sell(trade_data: dict, direction: str, sell_price: float,
                        sell_size: float, event_slug: str):
    """Close pending follow positions when the source user sells (early exit).

    Passes sell_size so state can do proportional (partial) closes.
    """
    state.record_follow_sell_event()
    closed = state.close_follow_trades(event_slug, direction, sell_price, sell_size)
    if closed > 0:
        name = trade_data.get("name", "") or "unknown"
        print(f"[FOLLOW] SELL: closed {closed} {direction.upper()} position(s) "
              f"@ ${sell_price:.2f} x{sell_size:.1f} from {name}")


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
                        _annotate_signal_agreement(trade)
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
                            _annotate_signal_agreement(trade)
                            state.record_trade(trade)
                            log_trade(trade)
                            bet_placed_slug = slug
                    last_analyze_time = now

            # Sweep all pending trades every 10s (PULSE + Follow + Signal)
            if now - last_sweep_time >= 10:
                resolve_all_pending(btc_feed)
                resolve_follow_pending(btc_feed)
                resolve_signal_pending(btc_feed)
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
    if "simulation" in body:
        _follow_config["simulation"] = bool(body["simulation"])
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


@app.get("/api/follow/export")
async def api_follow_export(format: str = "csv"):
    """Export all copied trades as CSV or JSON."""
    trades = list(state.follow_trades)
    if format == "json":
        content = json.dumps(trades, indent=2)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=copied_trades.json"},
        )
    # CSV
    fields = ["time", "event_slug", "title", "direction", "contract_price",
              "payout_multiplier", "source_cost", "stake_usd", "outcome",
              "pnl_usd", "sell_price", "source_wallet", "source_name",
              "source_size", "tx_hash"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(trades)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=copied_trades.csv"},
    )


@app.get("/api/follow/feed-status")
async def api_follow_feed_status():
    """Diagnostic endpoint for follow feed health."""
    if _follow_feed:
        return JSONResponse(_follow_feed.get_status())
    return JSONResponse({"error": "follow feed not initialized"})


# --- Signal Endpoints ---

@app.get("/api/signal")
async def api_signal():
    """Signal module state: active windows, emitted signals, stats."""
    if _signal:
        data = _signal.get_snapshot()
        if _follow_feed:
            data["feed"] = _follow_feed.get_status()
        else:
            data["feed"] = {"connected": False, "wallets_count": 0, "wallets": []}
        return JSONResponse(data)
    return JSONResponse({"error": "signal module not initialized"})


@app.patch("/api/signal/config")
async def api_signal_config(request: Request):
    """Update signal config at runtime."""
    if not _signal:
        return JSONResponse({"error": "signal module not initialized"}, status_code=500)
    body = await request.json()
    _signal.update_config(**body)
    snap = _signal.get_snapshot()
    return JSONResponse({"ok": True, "config": snap["config"]})


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

def _annotate_signal_agreement(trade: dict):
    """Check if the Signal module agrees with this PULSE trade direction.

    Annotates trade with:
      signal_agrees: True/False/None (None = no signal available)
      signal_direction: the signal's direction (if available)
      signal_confidence: the signal's confidence (if available)
    """
    if not _signal:
        trade["signal_agrees"] = None
        return

    slug = trade.get("event_slug", "")
    sig = _signal.get_active_signal(slug)

    if not sig:
        trade["signal_agrees"] = None
        trade["signal_direction"] = None
        trade["signal_confidence"] = None
        return

    pulse_dir = trade.get("direction", "")
    signal_dir = sig.get("direction", "")
    agrees = pulse_dir == signal_dir

    trade["signal_agrees"] = agrees
    trade["signal_direction"] = signal_dir
    trade["signal_confidence"] = sig.get("confidence", 0)

    if agrees:
        state.combined_bets += 1
        tag = "COMBINED"
    else:
        tag = "DIVERGENT"

    print(f"[{tag}] PULSE={pulse_dir.upper()} Signal={signal_dir.upper()} "
          f"conf={sig.get('confidence', 0):.2f} on {slug}")


def resolve_signal_pending(btc_feed: BTCFeed):
    """Sweep pending signals and resolve against actual market outcomes."""
    if not _signal:
        return

    now = time.time()
    pending_slugs = _signal.get_pending_slugs()

    for slug in pending_slugs:
        try:
            parts = slug.split("-")
            window_start_ts = int(parts[-1])
            window_dur = 900 if "15m" in slug else 300
            window_end_ts = window_start_ts + window_dur

            # Wait at least 15s after window end
            if now < window_end_ts + 15:
                continue

            # Primary: Polymarket outcome
            market_winner = fetch_market_outcome(slug)
            if market_winner:
                _signal.resolve_signal(slug, market_winner)
                continue

            # Fallback: Binance price comparison (after 3 min buffer, BTC only)
            if now > window_end_ts + 180 and "btc" in slug:
                open_price = btc_feed.fetch_price_at_time(window_start_ts * 1000)
                close_price = btc_feed.fetch_price_at_time(window_end_ts * 1000)
                if open_price and close_price:
                    winner = "down" if close_price < open_price else "up"
                    _signal.resolve_signal(slug, winner)
        except (ValueError, IndexError):
            continue


def _handle_signal(signal: dict):
    """Callback when Signal module emits a bet signal.

    Called once per window (after scout + flip check pass).
    """
    direction = signal.get("direction", "?").upper()
    confidence = signal.get("confidence", 0)
    bias = signal.get("bias", 0)
    slug = signal.get("slug", "")
    bet_usd = signal.get("suggested_bet_usd", 0)
    entry_tier = signal.get("entry_tier", "?")
    entry_price = signal.get("avg_entry_price", 0)
    trades = signal.get("trade_count", 0)
    elapsed = signal.get("window_elapsed_s", 0)
    remaining = signal.get("window_remaining_s", 0)

    print(f"[SIGNAL] BET {direction} on {slug} "
          f"(entry=${entry_price:.2f} [{entry_tier}], "
          f"bet=${bet_usd:.2f}, bias={bias:.0%}, "
          f"{trades} trades, {elapsed:.0f}s in, {remaining:.0f}s left)")

    # Post-hoc annotation: PULSE may have placed its bet before this signal fired.
    if slug:
        sig_dir = signal.get("direction", "")
        for trade in state.trades:
            if (trade.get("event_slug") == slug
                    and trade.get("bet_placed")
                    and trade.get("signal_agrees") is None):
                agrees = trade.get("direction", "") == sig_dir
                trade["signal_agrees"] = agrees
                trade["signal_direction"] = sig_dir
                trade["signal_confidence"] = confidence
                if agrees:
                    state.combined_bets += 1
                    print(f"[SIGNAL] Retroactively annotated PULSE bet on {slug} "
                          f"as COMBINED")


def _state_saver_loop(interval: int = 60):
    """Background thread: save follow state to disk every `interval` seconds."""
    while True:
        time.sleep(interval)
        try:
            state.save_follow_state()
        except Exception as e:
            print(f"[STATE] Save failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="PULSE Dashboard")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    global _btc_feed, _follow_feed, _signal
    formula = PulseFormula(df=STUDENT_T_DF)
    btc_feed = BTCFeed(window_seconds=VOL_WINDOW)
    _btc_feed = btc_feed

    # Restore follow state from previous run before starting anything
    state.load_follow_state()

    # --- Signal Module (init before bot thread to avoid race condition) ---
    signal_agg = SignalAggregator()
    _signal = signal_agg
    signal_agg.on_signal = _handle_signal

    bot_thread = threading.Thread(target=bot_loop, args=(formula, btc_feed),
                                  daemon=True)
    bot_thread.start()

    # Periodic state persistence (every 60s)
    saver_thread = threading.Thread(target=_state_saver_loop, args=(60,), daemon=True)
    saver_thread.start()

    # --- Follow Mode ---
    follow_feed = FollowFeed()
    _follow_feed = follow_feed

    # Dual callback: follow mode + signal aggregator
    def _on_rtds_trade(trade):
        handle_follow_trade(trade)
        signal_agg.ingest(trade)

    follow_feed.on_trade = _on_rtds_trade

    # Load persisted wallets from disk first
    follow_feed.load_wallets()

    # Ensure default wallet (Bonereaper) is always present with label
    BONEREAPER = "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30"
    follow_feed.add_wallet(BONEREAPER, "Bonereaper (default)")

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
        print(f"  Signal module: active (bias threshold {signal_agg.bias_threshold})")
    else:
        print("  Follow mode: no wallets configured (add via dashboard)")

    print(f"\n  BLACK DELTA / PULSE Dashboard")
    print(f"  http://localhost:{args.port}\n")

    try:
        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
    finally:
        # Save on clean shutdown (Ctrl+C, SIGTERM, etc.)
        print("[STATE] Saving follow state on shutdown...")
        state.save_follow_state()


if __name__ == "__main__":
    main()
