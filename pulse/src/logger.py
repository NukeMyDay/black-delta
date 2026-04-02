"""
Trade logger for PULSE simulation and live trading.

Logs every decision (bet or skip) to CSV for later analysis.
"""

import csv
import os
from datetime import datetime, timezone

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def _ensure_dirs():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)


def get_trade_log_path() -> str:
    _ensure_dirs()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"trades_{today}.csv")


def get_summary_path() -> str:
    _ensure_dirs()
    return os.path.join(DATA_DIR, "simulation_summary.csv")


TRADE_FIELDS = [
    "timestamp",
    "event_slug",
    "direction",       # "down" or "up"
    "btc_price",
    "target_price",
    "distance",
    "time_left_seconds",
    "contract_price",
    "real_prob",
    "implied_prob",
    "edge",
    "ev_per_dollar",
    "sigma_move",
    "volatility",
    "payout_multiplier",
    "should_bet",
    "bet_placed",       # True if actually bet (or would have in sim)
    "stake_usd",
    "mode",             # "simulation" or "live"
    "outcome",          # "win", "lose", or "pending"
    "pnl_usd",
]


def log_trade(data: dict) -> None:
    """Append a trade/decision record to today's CSV log."""
    path = get_trade_log_path()
    file_exists = os.path.exists(path)

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if not file_exists:
            writer.writeheader()
        row = {field: data.get(field, "") for field in TRADE_FIELDS}
        row["timestamp"] = datetime.now(timezone.utc).isoformat()
        writer.writerow(row)


def log_summary(stats: dict) -> None:
    """Append a daily summary row."""
    path = get_summary_path()
    fields = [
        "date", "total_opportunities", "bets_placed", "wins", "losses",
        "pending", "total_pnl", "win_rate", "avg_edge", "avg_ev",
        "capital_start", "capital_end",
    ]
    file_exists = os.path.exists(path)

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        row = {field: stats.get(field, "") for field in fields}
        writer.writerow(row)
