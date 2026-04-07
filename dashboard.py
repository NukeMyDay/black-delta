"""
BLACK DELTA — Polymarket Trading Dashboard

Phase-based Bonereaper replication strategy with real-time BTC price feed.
Monitors Bonereaper via RTDS WebSocket for analysis and tracking.

Usage:
    python dashboard.py              # Start dashboard
    python dashboard.py --port 8080  # Custom port
"""

import argparse
import builtins
import csv
import io
import json
import os
import sys
import threading
import time
from collections import deque
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
from src.state import state
from src.logger import log_trade
from src.analysis_logger import AnalysisLogger
from src.binance_ws import BinancePriceFeed
from src.phase_strategy import PhaseStrategy
from src.pulse_strategy import PulseStrategy

load_dotenv()

# Follow mode config
FOLLOW_WALLETS = os.getenv("FOLLOW_WALLETS", "")

# ==================================================================
#  Log capture — ring buffer for dashboard log viewer
# ==================================================================
_log_buffer: deque[dict] = deque(maxlen=500)
_log_lock = threading.Lock()
_original_print = builtins.print


def _capturing_print(*args, **kwargs):
    """Intercept print() calls to capture log lines for the dashboard."""
    _original_print(*args, **kwargs)
    try:
        msg = " ".join(str(a) for a in args)
        with _log_lock:
            _log_buffer.append({
                "time": datetime.now(timezone.utc).isoformat(),
                "msg": msg,
            })
    except Exception:
        pass


builtins.print = _capturing_print

app = FastAPI(title="BLACK DELTA")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Module-level references
_follow_feed: FollowFeed | None = None
_executor: Executor | None = None
_redeemer: Redeemer | None = None
_analysis: AnalysisLogger | None = None
_btc_feed: BinancePriceFeed | None = None
_phase: PhaseStrategy | None = None
_pulse: PulseStrategy | None = None


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
                # Queue winning bets for auto-redeem (losing = $0 payout, not worth gas)
                has_win = any(
                    t.get("event_slug") == slug and t.get("outcome") == "win"
                    for t in state.follow_trades
                )
                if has_win and _redeemer and _redeemer.enabled:
                    _redeemer.queue_redeem(slug)
                slugs_seen[slug] = True
            else:
                slugs_seen[slug] = False
        except (ValueError, IndexError):
            slugs_seen[slug] = False




# ==================================================================
#  Background Resolution Loop
# ==================================================================

def _resolution_loop():
    """Background thread: sweep pending trades every 10s."""
    while True:
        try:
            resolve_follow_pending()
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
        try:
            if _pulse:
                _pulse.save_state()
        except Exception as e:
            print(f"[PULSE] Save failed: {e}")


def _sync_executor_limits():
    """Sync dynamic executor limits from current state (capital + risk level)."""
    if not _executor:
        return
    # Max bet = 1 full Kelly fraction of capital (actual bets are much smaller via ⅛-Kelly)
    _executor.max_bet_usd = round(state.betting_capital * 0.125, 2)  # 1/8 Kelly


def _redeem_sweep_loop(interval: int = 120):
    """Background thread: retry pending redemptions every `interval` seconds."""
    while True:
        time.sleep(interval)
        try:
            if _redeemer and _redeemer.enabled:
                _redeemer.sweep_pending()
        except Exception as e:
            print(f"[REDEEM] Sweep loop error: {e}")


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
                    # Portfolio = cash + pending position value (at cost)
                    portfolio = state.portfolio_value
                    # Keep peak tracking up to date (based on portfolio, not just cash)
                    if portfolio > state._peak_capital:
                        state._peak_capital = portfolio
                    # Append to PnL curve for chart (portfolio value, not cash-only)
                    state.follow_pnl_curve.append({
                        "time": datetime.now(timezone.utc).isoformat(),
                        "capital": round(portfolio, 2),
                        "pnl": round(portfolio - state.base_capital, 2),
                    })
                    # Check loss limit (auto-pause if betting capital exhausted)
                    state.check_loss_limit()
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
    # Capital — portfolio = cash + pending positions
    cash = state.polymarket_balance if state.polymarket_balance is not None else state.betting_capital
    portfolio = state.portfolio_value
    pending_value = state.pending_stakes
    capital_data = {
        "base": state.base_capital,
        "portfolio": round(portfolio, 2),
        "cash": round(cash, 2),
        "pending_value": round(pending_value, 2),
        "balance": round(portfolio, 2),  # backward compat
        "profit": round(state.computed_pnl, 2),
        "capital_mode": "fixed" if state.betting_capital_fixed else "live",
        "betting": round(state.betting_capital, 2),
        "max_drawdown": state.max_drawdown,
        "accrued_fees": state.total_accrued_fees,
        "nav_per_share": round(state.nav_per_share, 4),
        "total_shares": round(state.total_shares, 4),
    }

    # Config
    config_data = {
        "betting_capital_fixed": state.betting_capital_fixed,
        "max_bet_per_window": getattr(state, "max_bet_per_window", None),
        "kill_switch": state.kill_switch,
        "pause_reason": state.pause_reason,
        "sim_mode": state.sim_mode,
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
            elapsed = now - window_start if window_start else 0
            open_positions.append({
                "slug": slug,
                "direction": t.get("direction"),
                "stake": t.get("stake_usd"),
                "entry_price": t.get("contract_price"),
                "source_entry": t.get("source_entry"),
                "fill_price": t.get("fill_price") or t.get("contract_price"),
                "payout_multiplier": t.get("payout_multiplier"),
                "strategy": t.get("strategy", "follow"),
                "entry_tier": t.get("entry_tier", ""),
                "win_rate": t.get("win_rate"),
                "bias": t.get("bias"),
                "countdown_s": max(0, round(remaining)),
                "elapsed_s": round(elapsed),
                "time": t.get("time"),
            })

    # Follow stats
    follow_snapshot = state.get_follow_snapshot()

    # Executor status
    executor_data = _executor.get_status() if _executor else {"enabled": False}

    # Feed status
    feed_data = _follow_feed.get_status() if _follow_feed else {"connected": False}

    # Investors
    investor_snapshot = state.get_investor_snapshot()

    # Analysis logger status
    analysis_data = _analysis.get_status() if _analysis else {}

    # Phase strategy + BTC feed status
    phase_data = _phase.get_status() if _phase else {"running": False}
    btc_data = _btc_feed.status() if _btc_feed else {"connected": False}

    # PULSE strategy status
    pulse_data = _pulse.get_status() if _pulse else {"running": False}

    return JSONResponse({
        "capital": capital_data,
        "config": config_data,
        "daily": daily_data,
        "connections": connections,
        "open_positions": open_positions[:20],
        "follow": follow_snapshot,
        "executor": executor_data,
        "feed": feed_data,
        "investors": investor_snapshot,
        "analysis": analysis_data,
        "phase": phase_data,
        "btc_feed": btc_data,
        "pulse": pulse_data,
    })


@app.patch("/api/config")
async def api_update_config(request: Request):
    """Update capital/risk config at runtime."""
    body = await request.json()

    if "betting_capital_fixed" in body:
        val = body["betting_capital_fixed"]
        if val is None or val == "" or val == "auto":
            state.betting_capital_fixed = None  # use live balance
        else:
            state.betting_capital_fixed = max(10, float(val))  # min $10
    if "max_bet_per_window" in body:
        val = body["max_bet_per_window"]
        if val is None or val == "" or val == 0:
            state.max_bet_per_window = None
        else:
            state.max_bet_per_window = max(5, min(500, float(val)))
    if "kill_switch" in body:
        new_val = bool(body["kill_switch"])
        state.kill_switch = new_val
        if not new_val:
            state.pause_reason = None  # clear auto-pause reason on manual resume
    if "sim_mode" in body:
        state.sim_mode = bool(body["sim_mode"])

    # Re-sync executor limits
    _sync_executor_limits()

    # Sync phase strategy settings
    if _phase:
        _phase.paper_mode = state.sim_mode or not (_executor and _executor.enabled)
        if state.max_bet_per_window is not None:
            _phase.max_budget = state.max_bet_per_window

    return JSONResponse({
        "ok": True,
        "config": {
            "betting_capital_fixed": state.betting_capital_fixed,
            "max_bet_per_window": state.max_bet_per_window,
            "kill_switch": state.kill_switch,
            "pause_reason": state.pause_reason,
            "betting_capital": round(state.betting_capital, 2),
        }
    })


# --- PULSE Config Endpoint ---

@app.patch("/api/pulse/config")
async def api_pulse_config(request: Request):
    """Update PULSE strategy config at runtime."""
    body = await request.json()

    if not _pulse:
        return JSONResponse({"error": "PULSE not initialized"}, status_code=500)

    if "max_bet_cap" in body:
        val = body["max_bet_cap"]
        if val is None or val == "" or val == 0:
            _pulse.max_bet_cap = 50.0  # reset to default
        else:
            _pulse.max_bet_cap = max(1, min(500, float(val)))

    return JSONResponse({
        "ok": True,
        "pulse_config": {
            "max_bet_cap": _pulse.max_bet_cap,
        }
    })


@app.get("/api/pulse/export")
async def api_pulse_export(format: str = "csv"):
    """Export PULSE bets as CSV or JSON."""
    if not _pulse:
        return JSONResponse({"error": "PULSE not initialized"}, status_code=500)

    bets = [b.to_dict() for b in _pulse.bets if b.outcome != "skip"]

    if format == "json":
        return Response(
            json.dumps(bets, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=pulse_bets.json"},
        )

    fields = ["time", "slug", "direction", "btc_price", "target_price",
              "entry_price", "distance", "effective_wr", "edge_pct",
              "stake", "outcome", "pnl"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(bets)
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pulse_bets.csv"},
    )


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


@app.get("/api/logs")
async def api_logs(request: Request):
    """Return recent log lines. Optional ?since=<iso> to get only new lines."""
    since = request.query_params.get("since", "")
    with _log_lock:
        logs = list(_log_buffer)
    if since:
        logs = [l for l in logs if l["time"] > since]
    return JSONResponse({"logs": logs[-200:]})


# --- Analysis Logger Endpoints ---

@app.get("/api/analysis")
async def api_analysis():
    """Analysis logger status — active windows + stats."""
    if _analysis:
        return JSONResponse(_analysis.get_status())
    return JSONResponse({"error": "analysis logger not initialized"})


@app.get("/api/analysis/windows")
async def api_analysis_windows(request: Request):
    """Get saved window summaries. Optional ?date=YYYY-MM-DD."""
    if not _analysis:
        return JSONResponse({"error": "analysis logger not initialized"})
    date = request.query_params.get("date")
    summaries = _analysis.get_window_summaries(date)
    return JSONResponse({"date": date, "windows": summaries})


@app.get("/api/analysis/export")
async def api_analysis_export(request: Request):
    """Export analysis data. ?type=trades|windows&date=YYYY-MM-DD&format=csv|json"""
    if not _analysis:
        return JSONResponse({"error": "analysis logger not initialized"})

    date = request.query_params.get(
        "date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    data_type = request.query_params.get("type", "windows")
    fmt = request.query_params.get("format", "json")

    if data_type == "windows":
        summaries = _analysis.get_window_summaries(date)
        if fmt == "csv" and summaries:
            buf = io.StringIO()
            writer = csv.DictWriter(
                buf, fieldnames=list(summaries[0].keys()), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(summaries)
            return Response(
                buf.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition":
                          f"attachment; filename=analysis_windows_{date}.csv"},
            )
        return JSONResponse(summaries, headers={
            "Content-Disposition":
                f"attachment; filename=analysis_windows_{date}.json"
        })

    if data_type == "trades":
        trades_path = os.path.join("logs", f"analysis_trades_{date}.csv")
        if os.path.exists(trades_path):
            with open(trades_path, "r", encoding="utf-8") as f:
                content = f.read()
            return Response(
                content,
                media_type="text/csv",
                headers={"Content-Disposition":
                          f"attachment; filename=analysis_trades_{date}.csv"},
            )
        return JSONResponse({"error": f"No trades file for {date}"}, status_code=404)

    return JSONResponse({"error": "type must be 'trades' or 'windows'"}, status_code=400)


# ==================================================================
#  Startup
# ==================================================================

def main():
    parser = argparse.ArgumentParser(description="BLACK DELTA Dashboard")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    global _follow_feed, _executor, _redeemer, _analysis, _btc_feed, _phase, _pulse

    # Restore state from previous run
    state.load_follow_state()

    # --- Analysis Logger (Bonereaper strategy research) ---
    analysis = AnalysisLogger()
    _analysis = analysis
    analysis.start()

    # --- Resolution + State Saver + Balance Sync + Redeem Sweep ---
    threading.Thread(target=_resolution_loop, daemon=True).start()
    threading.Thread(target=_state_saver_loop, args=(60,), daemon=True).start()
    threading.Thread(target=_balance_sync_loop, args=(60,), daemon=True).start()
    threading.Thread(target=_redeem_sweep_loop, args=(120,), daemon=True).start()

    # --- Follow Feed ---
    follow_feed = FollowFeed()
    _follow_feed = follow_feed

    def _on_rtds_trade(trade):
        if _analysis:
            _analysis.log_trade(trade)

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
        _sync_executor_limits()
        print(f"  Max bet: ${executor.max_bet_usd:.2f}")

        # --- Auto-Redeemer (only in live mode, not paper/sim) ---
        if not state.sim_mode:
            redeemer = Redeemer()
            _redeemer = redeemer
            private_key = os.getenv("POLY_PRIVATE_KEY", "")
            signer = executor.client.signer.address()
            funder = executor.client.builder.funder
            sig_type = executor.client.builder.sig_type
            redeemer.initialize(private_key, signer, funder, sig_type)
            redeemer.queue_resolved_trades(state.follow_trades)
        else:
            print("  Redeemer: SKIPPED (paper mode)")
    else:
        print("  Mode: SIMULATION (set POLY_* env vars for live)")

    # --- Binance Futures WebSocket (BTC real-time price) ---
    btc_feed = BinancePriceFeed(use_futures=True)
    _btc_feed = btc_feed
    btc_feed.start()
    print("[BTC] Binance Futures WebSocket starting...")

    # --- Phase Strategy (Bonereaper replication) ---
    phase = PhaseStrategy(btc_feed, executor, state)
    _phase = phase
    phase.paper_mode = state.sim_mode or not executor.enabled
    phase.start()
    phase_mode = "PAPER" if phase.paper_mode else "LIVE"
    print(f"[PHASE] Phase Strategy started ({phase_mode} mode)")
    print(f"[PHASE] Max budget: ${phase.max_budget}/window")
    if state.max_bet_per_window:
        print(f"[PHASE] Dashboard cap: ${state.max_bet_per_window}/window")

    # --- PULSE Strategy (momentum paper trading) ---
    pulse = PulseStrategy(btc_feed, executor)
    _pulse = pulse
    pulse.paper_mode = True  # Always paper until validated
    pulse.load_state()
    pulse.start()
    print(f"[PULSE] PULSE Strategy started (PAPER mode)")

    print(f"\n  BLACK DELTA")
    print(f"  Capital: ${state.betting_capital:.2f}")
    if state.betting_capital_fixed:
        print(f"  Loss limit: ${state.betting_capital_fixed:.0f} "
              f"(auto-pause at ${state._peak_capital - state.betting_capital_fixed:.0f})")
    max_bet_str = f"${state.max_bet_per_window:.0f}" if state.max_bet_per_window else f"${phase.max_budget:.0f} (default)"
    print(f"  Strategy: Phase ({phase_mode}) | Max bet: {max_bet_str}")
    print(f"  BTC Feed: Binance Futures bookTicker")
    print(f"  Wallets: {len(follow_feed.wallets)}")
    print(f"  http://localhost:{args.port}\n")

    try:
        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
    finally:
        print("[STATE] Saving state on shutdown...")
        state.save_follow_state()
        if _pulse:
            print("[PULSE] Saving PULSE state...")
            _pulse.save_state()
        if _phase:
            print("[PHASE] Stopping phase strategy...")
            _phase.stop()
        if _follow_feed:
            print("[FEED] Stopping follow feed...")
            _follow_feed.stop()
        if _btc_feed:
            print("[BTC] Stopping Binance feed...")
            _btc_feed.stop()
        if _analysis:
            print("[ANALYSIS] Flushing active windows on shutdown...")
            _analysis.flush_active_windows()


if __name__ == "__main__":
    main()
