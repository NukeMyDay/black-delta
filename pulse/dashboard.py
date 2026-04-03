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
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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
from src.state import state
from src.logger import log_trade

load_dotenv()

STAKE_USD = float(os.getenv("PULSE_STAKE_USD", "5"))
MIN_EDGE = float(os.getenv("PULSE_MIN_EDGE", "0.08"))
VOL_WINDOW = int(os.getenv("PULSE_VOLATILITY_WINDOW", "300"))
STUDENT_T_DF = float(os.getenv("PULSE_STUDENT_T_DF", "4"))
TAKER_SWITCH_SECONDS = 30
MAKER_PRICE_OFFSET = 0.01

app = FastAPI(title="BLACK DELTA / PULSE")
app.mount("/static", StaticFiles(directory="static"), name="static")


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

    # Filter: skip if sigma > 0 (price moving against our bet direction)
    if best.get("sigma_move", 0) > 0 and best["should_bet"]:
        best["should_bet"] = False
        best["reason"] = "Sigma > 0: price moving against direction"

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
    """Check the outcome of the previous window and resolve pending trades."""
    prev_ts = int(slug.split("-")[-1]) - 300
    prev_slug = f"btc-updown-5m-{prev_ts}"

    # Get the close price for the previous window (= open of current)
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
    """Sweep ALL pending trades and try to resolve them (retry for API failures)."""
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

            # Only resolve if window has fully closed (10s buffer)
            if now < window_end_ts + 10:
                continue

            open_price = btc_feed.fetch_price_at_time(window_start_ts * 1000)
            close_price = btc_feed.fetch_price_at_time(window_end_ts * 1000)

            if open_price and close_price:
                direction = trade.get("direction")
                if direction == "down":
                    won = close_price < open_price
                else:
                    won = close_price >= open_price
                outcome = "win" if won else "lose"
                state.resolve_trade(slug, outcome, close_price)
        except (ValueError, IndexError):
            continue


ANALYZE_INTERVAL = 1  # re-analyze every N seconds


def bot_loop(formula: PulseFormula, btc_feed: BTCFeed):
    """Main bot loop running in background thread.

    Fetches BTC price every second for smooth frontend updates.
    Re-analyzes every ANALYZE_INTERVAL seconds for fast reaction.
    """
    state.bot_running = True
    state.mode = "simulation"

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
            # Always fetch fresh BTC price (1s cadence)
            fresh_price = btc_feed.fetch_current_price()
            if fresh_price:
                state.current_btc_price = fresh_price

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

            # Sweep all pending trades every 60s
            if now - last_sweep_time >= 60:
                resolve_all_pending(btc_feed)
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


@app.get("/api/formula")
async def api_formula():
    return JSONResponse({
        "name": "PULSE Edge Formula",
        "version": "Stufe 1+2",
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
                "PULSE berechnet die reale Wahrscheinlichkeit, dass BTC den "
                "Target-Preis innerhalb des 5-Minuten-Fensters kreuzt. Wenn "
                "diese Wahrscheinlichkeit hoeher ist als der Markt einpreist, "
                "existiert ein Edge."
            ),
            "steps": [
                {
                    "step": 1,
                    "name": "EWMA Volatilitaet",
                    "formula": "var(t) = lambda * var(t-1) + (1-lambda) * r(t)^2",
                    "explanation": (
                        "Exponentially Weighted Moving Average: Gewichtet "
                        "aktuelle Preisbewegungen staerker als aeltere. "
                        "Lambda=0.94 bedeutet die letzten ~17 Datenpunkte "
                        "machen 50% der Varianz aus. Reagiert schnell auf "
                        "Volatilitaets-Cluster (nach einem grossen Move ist "
                        "der naechste grosse Move wahrscheinlicher)."
                    ),
                },
                {
                    "step": 2,
                    "name": "Sigma-Distanz",
                    "formula": "sigma = distance / (price * vol * sqrt(time_left))",
                    "explanation": (
                        "Wie viele Standardabweichungen liegt der Target-Preis "
                        "vom aktuellen Preis entfernt? Je niedriger der Sigma-"
                        "Wert, desto wahrscheinlicher wird der Target gekreuzt."
                    ),
                },
                {
                    "step": 3,
                    "name": "Student-t Verteilung (Fat Tails)",
                    "formula": "real_prob = student_t.cdf(-sigma, df=4)",
                    "explanation": (
                        "Statt der Normalverteilung nutzen wir die Student-t "
                        "Verteilung mit 4 Freiheitsgraden. Diese hat 'dickere "
                        "Raender' (Fat Tails) — extreme Moves sind wahrschein-"
                        "licher als die Normalverteilung vorhersagt. BTC bewegt "
                        "sich tatsaechlich so: 3-Sigma Events passieren ~10x "
                        "oefter als die Glockenkurve sagt."
                    ),
                },
                {
                    "step": 4,
                    "name": "Edge & Expected Value",
                    "formula": "edge = real_prob - implied_prob; EV = prob * gewinn - (1-prob) * verlust",
                    "explanation": (
                        "Edge = Differenz zwischen unserer Schaetzung und dem "
                        "Marktpreis. EV = erwarteter Gewinn pro Dollar Einsatz. "
                        "Wir wetten nur wenn beides positiv ist und der Edge "
                        "ueber dem Minimum liegt."
                    ),
                },
                {
                    "step": 5,
                    "name": "Hybrid Order Strategie",
                    "formula": "if time > 30s: maker (0% fee); else: taker (7.2%)",
                    "explanation": (
                        "Frueh im Fenster platzieren wir Limit Orders (Maker) "
                        "ohne Gebuehr. In den letzten 30 Sekunden wechseln wir "
                        "zu Market Orders (Taker) falls der EV auch nach 7.2% "
                        "Fee positiv bleibt."
                    ),
                },
            ],
            "edge_source": (
                "Der Markt underpriced Tail Events systematisch. Menschen "
                "bewerten unwahrscheinliche Ereignisse als noch unwahrschein-"
                "licher als sie sind (Probability Neglect). Bei BTC 5-Min "
                "Maerkten zeigt sich das besonders: Wenn BTC $80+ ueber dem "
                "Target liegt, bietet der Markt z.B. 200x — impliziert 0.5%. "
                "Statistisch liegt die reale Wahrscheinlichkeit aber bei "
                "~2-3%. Diese Diskrepanz ist unser Gewinn."
            ),
        },
    })


# --- Startup ---

def main():
    parser = argparse.ArgumentParser(description="PULSE Dashboard")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    formula = PulseFormula(df=STUDENT_T_DF)
    btc_feed = BTCFeed(window_seconds=VOL_WINDOW)

    bot_thread = threading.Thread(target=bot_loop, args=(formula, btc_feed),
                                  daemon=True)
    bot_thread.start()

    print(f"\n  BLACK DELTA / PULSE Dashboard")
    print(f"  http://localhost:{args.port}\n")

    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
