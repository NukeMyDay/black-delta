"""
BLACK DELTA — Polymarket Copy-Trading Dashboard

Monitors Bonereaper's trades via RTDS WebSocket and places bets
using two strategies:
  - Signal: filtered bets (scout→flip check→Kelly sizing, ~73% WR)
  - Follow: proportional copy of all trades (Kelly sizing)

Capital management with configurable risk level, reinvestment rate,
and strategy mix. Supports live trading via Polymarket CLOB API.

Usage:
    python dashboard.py              # Start dashboard
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

from src.polymarket import fetch_market_outcome
from src.follow_feed import FollowFeed
from src.executor import Executor
from src.redeemer import Redeemer
from src.signal import SignalAggregator
from src.state import state
from src.logger import log_trade

load_dotenv()

# Follow mode config
FOLLOW_WALLETS = os.getenv("FOLLOW_WALLETS", "")

app = FastAPI(title="BLACK DELTA")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Module-level references
_follow_feed: FollowFeed | None = None
_signal: SignalAggregator | None = None
_executor: Executor | None = None
_redeemer: Redeemer | None = None


# ==================================================================
#  Signal / Follow Resolution (background sweep)
# ==================================================================

def resolve_follow_pending():
    """Sweep pending follow trades, grouped by slug to minimize API calls."""
    now = time.time()
    pending = [t for t in state.follow_trades if t.get("outcome") == "pending"]
    slugs_seen: dict[str, bool] = {}

    for trade in pending:
        slug = trade.get("event_slug")
        if not slug or slug in slugs_seen:
            continue
        try:
            parts = slug.split("-")
            window_start_ts = int(parts[-1])
            if "15m" in slug:
                window_dur = 900
            elif "1h" in slug:
                window_dur = 3600
            else:
                window_dur = 300
            window_end_ts = window_start_ts + window_dur

            if now < window_end_ts + 15:
                slugs_seen[slug] = False
                continue

            market_winner = fetch_market_outcome(slug)
            if market_winner:
                state.resolve_follow_trade(slug, market_winner, 0)
                # Track daily stats for resolved bets
                for t in state.follow_trades:
                    if t.get("event_slug") == slug and t.get("outcome") in ("win", "lose"):
                        pnl = t.get("pnl_usd", 0)
                        won = t.get("outcome") == "win"
                        state.record_daily_bet(pnl, won)
                # Auto-redeem resolved position
                if _redeemer and _redeemer.enabled:
                    try:
                        _redeemer.try_redeem_slug(slug)
                    except Exception as e:
                        print(f"[REDEEM] Error for {slug}: {e}")
                slugs_seen[slug] = True
            else:
                slugs_seen[slug] = False
        except (ValueError, IndexError):
            slugs_seen[slug] = False


def resolve_signal_pending():
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

            if now < window_end_ts + 15:
                continue

            market_winner = fetch_market_outcome(slug)
            if market_winner:
                _signal.resolve_signal(slug, market_winner)
        except (ValueError, IndexError):
            continue


# ==================================================================
#  Follow Trade Handler
# ==================================================================

def _compute_kelly_stake(entry_price: float, allocation: float) -> float:
    """Compute Kelly-sized bet from entry price and capital allocation.

    Uses the same win rate tiers as the signal module.
    """
    from src.signal import WIN_RATE_FULL, WIN_RATE_HALF, ENTRY_FULL, ENTRY_HALF

    if entry_price >= ENTRY_HALF or entry_price <= 0.01:
        return 0.0

    b = 1.0 / entry_price - 1
    if b <= 0:
        return 0.0

    p = WIN_RATE_FULL if entry_price < ENTRY_FULL else WIN_RATE_HALF
    q = 1 - p
    kelly = max(0, (p * b - q) / b)
    fraction = kelly * state.kelly_fraction
    return round(allocation * fraction, 2)


def handle_follow_trade(trade_data: dict):
    """Callback when a followed user trades. Handles BUY (open) and SELL (close)."""
    # Kill switch check
    if state.kill_switch:
        return

    side = trade_data.get("side", "").upper()
    outcome_raw = trade_data.get("outcome", "").lower()
    direction = "up" if outcome_raw == "up" else "down" if outcome_raw == "down" else outcome_raw
    price = trade_data.get("price", 0)
    size = trade_data.get("size", 0)
    event_slug = trade_data.get("event_slug", "")

    if price <= 0 or price >= 1:
        return

    if side == "SELL":
        _handle_follow_sell(trade_data, direction, price, size, event_slug)
        return

    if side != "BUY":
        return

    # --- BUY: open a new position ---
    # Capital allocation: follow gets (100 - signal_pct)% of betting capital
    follow_pct = 100 - state.signal_pct
    follow_allocation = state.betting_capital * (follow_pct / 100)

    # Kelly-sized bet
    stake = _compute_kelly_stake(price, follow_allocation)
    if stake < 0.50:
        return  # No edge or allocation too small

    is_live = _executor and _executor.enabled and not state.sim_mode
    mode = "live" if is_live else "simulation"

    # --- Live execution ---
    order_resp = None
    if is_live and _executor:
        token_id = trade_data.get("asset", "")
        if token_id:
            stake = _executor.cap_amount(stake)
            slippage = 0.10
            max_price = min(price + slippage, 0.95)
            # Tier-aware cap: full tier (<0.60) caps at 0.65, half tier (0.60-0.70) caps at 0.72
            tier_cap = 0.72 if price >= 0.55 else 0.65
            if max_price > tier_cap:
                max_price = tier_cap
            if price > 0.70:
                print(f"[FOLLOW] Entry ${price:.2f} > $0.70 — SKIP")
                return
            market_info = _executor.get_market_info(token_id)
            order_resp = _executor.place_market_buy(
                token_id=token_id,
                amount_usd=stake,
                max_price=max_price,
                neg_risk=market_info.get("neg_risk", False),
                tick_size=market_info.get("tick_size", "0.01"),
            )
            if not order_resp:
                mode = "simulation"
        else:
            mode = "simulation"

    tag = "LIVE" if mode == "live" else "SIM"
    print(f"[FOLLOW] [{tag}] BUY {direction.upper()} @ ${price:.2f} "
          f"${stake:.2f} from {trade_data.get('name', '') or 'unknown'}")

    # Only record bets that were actually placed on Polymarket
    if mode != "live" or not order_resp:
        return

    # Use max_price as recorded entry — closest estimate to actual fill price
    fill_estimate = max_price
    payout_multiplier = round(1.0 / fill_estimate, 2) if fill_estimate > 0 else 0
    copy_trade = {
        "event_slug": event_slug,
        "direction": direction,
        "contract_price": fill_estimate,
        "payout_multiplier": payout_multiplier,
        "stake_usd": stake,
        "strategy": "follow",
        "source_wallet": trade_data.get("wallet", ""),
        "source_name": trade_data.get("name", ""),
        "source_size": size,
        "source_cost": round(price * size, 2),
        "source_price": price,
        "source_entry": price,  # Source user's entry for reference
        "tx_hash": trade_data.get("tx_hash", ""),
        "slug": trade_data.get("slug", ""),
        "title": trade_data.get("title", ""),
        "outcome": "pending",
        "pnl_usd": 0,
        "bet_placed": True,
        "mode": "live",
        "order_id": order_resp.get("orderID"),
        "time": datetime.now(timezone.utc).isoformat(),
        "detected_at": trade_data.get("detected_at", time.time()),
    }
    state.record_follow_trade(copy_trade)


def _handle_follow_sell(trade_data: dict, direction: str, sell_price: float,
                        sell_size: float, event_slug: str):
    """Close pending follow positions when the source user sells."""
    state.record_follow_sell_event()
    closed = state.close_follow_trades(event_slug, direction, sell_price, sell_size)
    if closed > 0:
        name = trade_data.get("name", "") or "unknown"
        print(f"[FOLLOW] SELL: closed {closed} {direction.upper()} position(s) "
              f"@ ${sell_price:.2f} x{sell_size:.1f} from {name}")


# ==================================================================
#  Signal Handler
# ==================================================================

def _handle_signal(signal: dict):
    """Callback when Signal module emits a bet signal."""
    if state.kill_switch:
        return

    direction = signal.get("direction", "?").upper()
    slug = signal.get("slug", "")
    entry_price = signal.get("avg_entry_price", 0)
    entry_tier = signal.get("entry_tier", "?")

    # Capital allocation: signal gets signal_pct% of betting capital
    signal_allocation = state.betting_capital * (state.signal_pct / 100)

    # Kelly-sized bet using allocation
    stake = _compute_kelly_stake(entry_price, signal_allocation)
    if stake < 1.0:
        return

    is_live = _executor and _executor.enabled and not state.sim_mode
    mode = "live" if is_live else "simulation"

    # Update the signal's suggested bet to match our capital model
    signal["suggested_bet_usd"] = stake

    # --- Live execution: place real order ---
    order_resp = None
    token_id = signal.get("token_id", "")
    if is_live and _executor and token_id and stake >= 0.50:
        stake = _executor.cap_amount(stake)
        slippage = 0.10
        max_price = min(entry_price + slippage, 0.95)
        # Tier-aware cap: full tier (<0.60) caps at 0.65, half tier (0.60-0.70) caps at 0.72
        tier_cap = 0.72 if entry_price >= 0.55 else 0.65
        if max_price > tier_cap:
            max_price = tier_cap
        if entry_price > 0.70:
            print(f"[SIGNAL] Entry ${entry_price:.2f} > $0.70 — SKIP")
            return
        market_info = _executor.get_market_info(token_id)
        order_resp = _executor.place_market_buy(
            token_id=token_id,
            amount_usd=stake,
            max_price=max_price,
            neg_risk=market_info.get("neg_risk", False),
            tick_size=market_info.get("tick_size", "0.01"),
        )
        if not order_resp:
            mode = "simulation"
    elif is_live and not token_id:
        mode = "simulation"

    bias = signal.get("bias", 0)
    trade_count = signal.get("trade_count", 0)

    tag = "LIVE" if mode == "live" else "SIM"
    print(f"[SIGNAL] [{tag}] BET {direction} on {slug} "
          f"(src=${entry_price:.2f}, max=${max_price:.2f} [{entry_tier}], "
          f"bet=${stake:.2f}, bias={bias:.0%}, {trade_count} trades)")

    # Only record bets that were actually placed on Polymarket
    if mode != "live" or not order_resp:
        return

    # Use max_price as recorded entry — closest estimate to actual fill price
    # (CLOB API doesn't return fill price; Bonereaper's entry would be misleading)
    fill_estimate = max_price
    payout_multiplier = round(1.0 / fill_estimate, 2) if fill_estimate > 0 else 0
    bet_record = {
        "event_slug": slug,
        "direction": direction.lower(),
        "contract_price": fill_estimate,
        "payout_multiplier": payout_multiplier,
        "stake_usd": stake,
        "strategy": "signal",
        "source_wallet": "signal",
        "source_name": "Signal",
        "source_entry": entry_price,  # Bonereaper's entry for reference
        "slug": slug,
        "title": slug,
        "outcome": "pending",
        "pnl_usd": 0,
        "bet_placed": True,
        "mode": "live",
        "order_id": order_resp.get("orderID"),
        "entry_tier": entry_tier,
        "bias": round(bias, 4),
        "time": datetime.now(timezone.utc).isoformat(),
    }
    state.record_follow_trade(bet_record)


# ==================================================================
#  Background Resolution Loop
# ==================================================================

def _resolution_loop():
    """Background thread: sweep pending trades every 10s."""
    while True:
        try:
            resolve_follow_pending()
            resolve_signal_pending()
        except Exception as e:
            print(f"[RESOLVE] Error: {e}")
        time.sleep(10)


def _state_saver_loop(interval: int = 60):
    """Background thread: save state to disk every `interval` seconds."""
    while True:
        time.sleep(interval)
        try:
            state.save_follow_state()
        except Exception as e:
            print(f"[STATE] Save failed: {e}")


def _sync_executor_limits():
    """Sync dynamic executor limits from current state (capital + risk level)."""
    if not _executor:
        return
    # Max bet = 1 full Kelly fraction of capital (actual bets are much smaller via ⅛-Kelly)
    _executor.max_bet_usd = round(state.betting_capital * state.kelly_fraction, 2)
    # Daily loss limit = configured % of capital
    _executor.daily_loss_limit = round(state.betting_capital * state.daily_loss_limit_pct / 100, 2)


def _balance_sync_loop(interval: int = 60):
    """Background thread: sync live Polymarket USDC balance into state."""
    while True:
        time.sleep(interval)
        try:
            if _executor and _executor.enabled:
                balance = _executor.get_usdc_balance()
                if balance is not None:
                    state.polymarket_balance = balance
                    # Track start-of-day balance for today's P&L
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    if state._daily_date != today or state._start_of_day_balance is None:
                        state._start_of_day_balance = balance
                    # Keep peak tracking up to date
                    if balance > state._peak_capital:
                        state._peak_capital = balance
                    # Append to PnL curve for chart (real balance, not internal tracking)
                    state.follow_pnl_curve.append({
                        "time": datetime.now(timezone.utc).isoformat(),
                        "capital": round(balance, 2),
                        "pnl": round(balance - state.base_capital, 2),
                    })
                    # Re-sync executor limits with updated capital
                    _sync_executor_limits()
        except Exception as e:
            print(f"[BALANCE] Sync failed: {e}")


# ==================================================================
#  API Endpoints
# ==================================================================

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("templates/dashboard.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/dashboard")
async def api_dashboard():
    """Unified dashboard endpoint — single poll for all data."""
    # Capital — balance from Polymarket, profit from resolved bets only
    balance = state.polymarket_balance or state.betting_capital
    capital_data = {
        "base": state.base_capital,
        "balance": round(balance, 2),
        "profit": round(state.follow_pnl, 2),
        "reserved": round(state.reserved, 2),
        "betting": round(state.betting_capital, 2),
        "max_drawdown": state.max_drawdown,
        "accrued_fees": state.total_accrued_fees,
        "nav_per_share": round(state.nav_per_share, 4),
        "total_shares": round(state.total_shares, 4),
    }

    # Config
    config_data = {
        "risk_level": state.risk_level,
        "kelly_fraction": round(state.kelly_fraction, 4),
        "reinvest_rate": state.reinvest_rate,
        "signal_pct": state.signal_pct,
        "follow_pct": 100 - state.signal_pct,
        "kill_switch": state.kill_switch,
        "sim_mode": state.sim_mode,
        "daily_loss_limit_pct": state.daily_loss_limit_pct,
        "daily_loss_limit_usd": round(state.betting_capital * state.daily_loss_limit_pct / 100, 2),
    }

    # Daily summary — from resolved bets only (not affected by deposits)
    daily_data = {
        "date": state._daily_date,
        "pnl": round(state._daily_pnl, 2),
        "bets": state._daily_bets,
        "wins": state._daily_wins,
        "wr": round(state._daily_wins / state._daily_bets * 100, 1) if state._daily_bets > 0 else 0,
    }

    # Connections
    connections = {
        "rtds": _follow_feed._ws_connected if _follow_feed else False,
        "clob": _executor.enabled if _executor else False,
    }

    # Open positions (pending follow trades)
    now = time.time()
    open_positions = []
    for t in state.follow_trades:
        if t.get("outcome") == "pending":
            slug = t.get("event_slug", "")
            try:
                parts = slug.split("-")
                window_start = int(parts[-1])
                window_dur = 900 if "15m" in slug else 3600 if "1h" in slug else 300
                remaining = (window_start + window_dur) - now
            except (ValueError, IndexError):
                remaining = 0
            open_positions.append({
                "slug": slug,
                "direction": t.get("direction"),
                "stake": t.get("stake_usd"),
                "entry_price": t.get("contract_price"),
                "strategy": t.get("strategy", "follow"),
                "countdown_s": max(0, round(remaining)),
                "time": t.get("time"),
            })

    # Follow stats
    follow_snapshot = state.get_follow_snapshot()

    # Signal stats
    signal_snapshot = _signal.get_snapshot() if _signal else {}

    # Executor status
    executor_data = _executor.get_status() if _executor else {"enabled": False}

    # Feed status
    feed_data = _follow_feed.get_status() if _follow_feed else {"connected": False}

    # Investors
    investor_snapshot = state.get_investor_snapshot()

    return JSONResponse({
        "capital": capital_data,
        "config": config_data,
        "daily": daily_data,
        "connections": connections,
        "open_positions": open_positions[:20],
        "follow": follow_snapshot,
        "signal": signal_snapshot,
        "executor": executor_data,
        "feed": feed_data,
        "investors": investor_snapshot,
    })


@app.patch("/api/config")
async def api_update_config(request: Request):
    """Update capital/risk config at runtime."""
    body = await request.json()

    if "risk_level" in body:
        state.risk_level = max(1, min(10, int(body["risk_level"])))
    if "reinvest_rate" in body:
        state.reinvest_rate = max(0, min(1, float(body["reinvest_rate"])))
    if "signal_pct" in body:
        state.signal_pct = max(0, min(100, float(body["signal_pct"])))
    if "kill_switch" in body:
        state.kill_switch = bool(body["kill_switch"])
    if "sim_mode" in body:
        state.sim_mode = bool(body["sim_mode"])
    if "daily_loss_limit_pct" in body:
        state.daily_loss_limit_pct = max(1, min(90, float(body["daily_loss_limit_pct"])))

    # Re-sync all executor limits whenever risk or loss limit changes
    _sync_executor_limits()

    # Update signal module's capital if available
    if _signal:
        sig_alloc = state.betting_capital * (state.signal_pct / 100)
        _signal.sim_capital = sig_alloc

    return JSONResponse({
        "ok": True,
        "config": {
            "risk_level": state.risk_level,
            "kelly_fraction": round(state.kelly_fraction, 4),
            "reinvest_rate": state.reinvest_rate,
            "signal_pct": state.signal_pct,
            "follow_pct": 100 - state.signal_pct,
            "kill_switch": state.kill_switch,
            "betting_capital": round(state.betting_capital, 2),
        }
    })


# --- Investor Endpoints ---

@app.post("/api/investors")
async def api_add_investor(request: Request):
    """Add a new investor."""
    body = await request.json()
    name = body.get("name", "").strip()
    fee_pct = body.get("fee_pct", 5.0) / 100  # percentage → fraction
    if not name:
        return JSONResponse({"error": "Name required"}, status_code=400)
    inv = state.add_investor(name, fee_pct)
    state.save_follow_state()
    return JSONResponse({"ok": True, "investor": inv})


@app.patch("/api/investors/{index}")
async def api_update_investor(index: int, request: Request):
    """Update investor name or fee percentage."""
    body = await request.json()
    name = body.get("name")
    fee_pct = body.get("fee_pct")
    if fee_pct is not None:
        fee_pct = fee_pct / 100
    ok = state.update_investor(index, name=name, fee_pct=fee_pct)
    if not ok:
        return JSONResponse({"error": "Invalid investor"}, status_code=400)
    state.save_follow_state()
    return JSONResponse({"ok": True})


@app.delete("/api/investors/{index}")
async def api_remove_investor(index: int):
    """Remove an investor (must have 0 shares, cannot remove owner)."""
    ok = state.remove_investor(index)
    if not ok:
        return JSONResponse({"error": "Cannot remove (owner or has shares)"}, status_code=400)
    state.save_follow_state()
    return JSONResponse({"ok": True})


@app.post("/api/investors/{index}/deposit")
async def api_investor_deposit(index: int, request: Request):
    """Deposit cash — creates shares at current NAV."""
    body = await request.json()
    amount = float(body.get("amount", 0))
    if amount <= 0:
        return JSONResponse({"error": "Amount must be positive"}, status_code=400)
    ok = state.deposit(index, amount)
    if not ok:
        return JSONResponse({"error": "Invalid deposit"}, status_code=400)
    state.save_follow_state()
    return JSONResponse({"ok": True, "nav_per_share": round(state.nav_per_share, 4)})


@app.post("/api/investors/{index}/withdraw")
async def api_investor_withdraw(index: int, request: Request):
    """Withdraw capital. Fee on profit goes to owner as shares."""
    body = await request.json()
    amount = body.get("amount")
    if amount is not None:
        amount = float(amount)
    result = state.withdraw(index, amount)
    if result is None:
        return JSONResponse({"error": "Invalid withdrawal"}, status_code=400)
    state.save_follow_state()
    return JSONResponse({"ok": True, "withdrawal": result})


# --- Signal Endpoints ---

@app.get("/api/signal")
async def api_signal():
    """Signal module state."""
    if _signal:
        return JSONResponse(_signal.get_snapshot())
    return JSONResponse({"error": "signal module not initialized"})


@app.get("/api/signal/download")
async def api_signal_download(request: Request):
    """Download signal history as CSV or JSON."""
    if not _signal:
        return JSONResponse({"error": "signal module not initialized"}, status_code=500)
    fmt = request.query_params.get("format", "csv")
    signals = list(_signal.signals)

    if fmt == "json":
        return JSONResponse(signals, headers={
            "Content-Disposition": "attachment; filename=signal_history.json"
        })

    if not signals:
        return Response("No signals", media_type="text/csv")
    cols = ["time", "slug", "direction", "avg_entry_price", "entry_tier",
            "outcome", "sim_pnl", "suggested_bet_usd", "bias",
            "total_usdc", "trade_count", "confidence",
            "scout_direction", "market_winner"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for s in signals:
        writer.writerow(s)
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=signal_history.csv"},
    )


# --- Follow Endpoints ---

@app.get("/api/follow")
async def api_follow():
    """Follow mode state."""
    data = state.get_follow_snapshot()
    if _follow_feed:
        data["feed"] = _follow_feed.get_status()
    return JSONResponse(data)


@app.get("/api/follow/export")
async def api_follow_export(format: str = "csv"):
    """Export follow trades as CSV or JSON."""
    trades = list(state.follow_trades)
    if format == "json":
        return Response(
            json.dumps(trades, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=follow_trades.json"},
        )
    fields = ["time", "event_slug", "title", "direction", "contract_price",
              "payout_multiplier", "source_cost", "stake_usd", "outcome",
              "pnl_usd", "sell_price", "strategy", "source_name",
              "source_size", "mode"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(trades)
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=follow_trades.csv"},
    )


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
    """Follow feed health."""
    if _follow_feed:
        return JSONResponse(_follow_feed.get_status())
    return JSONResponse({"error": "follow feed not initialized"})


# ==================================================================
#  Startup
# ==================================================================

def main():
    parser = argparse.ArgumentParser(description="BLACK DELTA Dashboard")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    global _follow_feed, _signal, _executor, _redeemer

    # Restore state from previous run
    state.load_follow_state()

    # --- Signal Module ---
    signal_agg = SignalAggregator()
    _signal = signal_agg
    signal_agg.on_signal = _handle_signal
    # Set signal capital from state
    sig_alloc = state.betting_capital * (state.signal_pct / 100)
    signal_agg.sim_capital = sig_alloc

    # --- Resolution + State Saver + Balance Sync ---
    threading.Thread(target=_resolution_loop, daemon=True).start()
    threading.Thread(target=_state_saver_loop, args=(60,), daemon=True).start()
    threading.Thread(target=_balance_sync_loop, args=(60,), daemon=True).start()

    # --- Follow Feed ---
    follow_feed = FollowFeed()
    _follow_feed = follow_feed

    def _on_rtds_trade(trade):
        handle_follow_trade(trade)
        signal_agg.ingest(trade)

    follow_feed.on_trade = _on_rtds_trade
    follow_feed.load_wallets()

    # Ensure default wallet (Bonereaper)
    BONEREAPER = "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30"
    follow_feed.add_wallet(BONEREAPER, "Bonereaper (default)")

    if FOLLOW_WALLETS:
        for wallet in FOLLOW_WALLETS.split(","):
            wallet = wallet.strip()
            if wallet:
                follow_feed.add_wallet(wallet)

    if follow_feed.wallets:
        follow_feed.start()

    # --- Executor ---
    executor = Executor()
    _executor = executor
    if executor.initialize():
        print("  Mode: LIVE TRADING")
        # Fetch initial balance first so limits are based on real capital
        balance = executor.get_usdc_balance()
        if balance is not None:
            state.polymarket_balance = balance
            state._peak_capital = max(state._peak_capital, balance)
            print(f"  Polymarket Balance: ${balance:.2f}")
        # Sync dynamic limits (max_bet and daily_loss_limit) from capital + risk level
        _sync_executor_limits()
        print(f"  Max bet: ${executor.max_bet_usd:.2f} | Daily loss limit: ${executor.daily_loss_limit:.2f}")

        # --- Auto-Redeemer ---
        redeemer = Redeemer()
        _redeemer = redeemer
        private_key = os.getenv("POLY_PRIVATE_KEY", "")
        signer = executor.client.signer.address()
        funder = executor.client.builder.funder
        sig_type = executor.client.builder.sig_type
        redeemer.initialize(private_key, signer, funder, sig_type)
    else:
        print("  Mode: SIMULATION (set POLY_* env vars for live)")

    print(f"\n  BLACK DELTA")
    print(f"  Capital: ${state.betting_capital:.2f} | Risk: {state.risk_level}/10 | "
          f"Kelly: {state.kelly_fraction:.1%}")
    print(f"  Strategy: Signal {state.signal_pct}% / Follow {100 - state.signal_pct}%")
    print(f"  Wallets: {len(follow_feed.wallets)}")
    print(f"  http://localhost:{args.port}\n")

    try:
        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
    finally:
        print("[STATE] Saving state on shutdown...")
        state.save_follow_state()


if __name__ == "__main__":
    main()
