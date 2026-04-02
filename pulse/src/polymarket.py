"""
Polymarket API client for BTC Up/Down 5-minute markets.

Endpoints:
  Gamma API: https://gamma-api.polymarket.com
  CLOB API:  https://clob.polymarket.com
"""

import time
import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
SERIES_SLUG = "btc-up-or-down-5m"


def get_current_event_slug() -> str | None:
    """Find the currently active BTC 5-min market slug."""
    # Current 5-min window: round down to nearest 5 minutes
    now = int(time.time())
    window_start = now - (now % 300)
    slug = f"btc-updown-5m-{window_start}"
    return slug


def get_next_event_slug() -> str:
    """Find the next upcoming BTC 5-min market slug."""
    now = int(time.time())
    window_start = now - (now % 300) + 300
    return f"btc-updown-5m-{window_start}"


def fetch_event(slug: str) -> dict | None:
    """Fetch event data from Gamma API."""
    resp = requests.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=5)
    if resp.status_code != 200:
        return None
    data = resp.json()
    if isinstance(data, list) and len(data) > 0:
        return data[0]
    return data if isinstance(data, dict) else None


def fetch_market(slug: str) -> dict | None:
    """Fetch market data from Gamma API."""
    resp = requests.get(f"{GAMMA_BASE}/markets", params={"slug": slug}, timeout=5)
    if resp.status_code != 200:
        return None
    data = resp.json()
    if isinstance(data, list) and len(data) > 0:
        return data[0]
    return data if isinstance(data, dict) else None


def fetch_orderbook(token_id: str) -> dict | None:
    """Fetch order book from CLOB API."""
    resp = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=5)
    if resp.status_code != 200:
        return None
    return resp.json()


def fetch_price(token_id: str, side: str = "buy") -> float | None:
    """Fetch current price for a token (buy or sell side)."""
    resp = requests.get(
        f"{CLOB_BASE}/price",
        params={"token_id": token_id, "side": side},
        timeout=5,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    return float(data.get("price", 0))


def fetch_midpoint(token_id: str) -> float | None:
    """Fetch midpoint price for a token."""
    resp = requests.get(
        f"{CLOB_BASE}/midpoint", params={"token_id": token_id}, timeout=5
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    return float(data.get("mid", 0))


def _parse_json_string(value) -> list:
    """Parse a JSON string or return the value if already a list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            import json
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def parse_market_data(market: dict) -> dict:
    """Extract the fields we need from a market response."""
    outcomes = _parse_json_string(market.get("outcomes", []))
    prices_raw = _parse_json_string(market.get("outcomePrices", []))
    token_ids = _parse_json_string(market.get("clobTokenIds", []))

    prices = [float(p) for p in prices_raw] if prices_raw else []

    # Outcomes are ["Up", "Down"] — map to indices
    up_idx = outcomes.index("Up") if "Up" in outcomes else 0
    down_idx = outcomes.index("Down") if "Down" in outcomes else 1

    return {
        "condition_id": market.get("conditionId"),
        "up_token_id": token_ids[up_idx] if len(token_ids) > up_idx else None,
        "down_token_id": token_ids[down_idx] if len(token_ids) > down_idx else None,
        "up_price": prices[up_idx] if len(prices) > up_idx else None,
        "down_price": prices[down_idx] if len(prices) > down_idx else None,
        "volume": float(market.get("volume", 0) or 0),
        "liquidity": float(market.get("liquidity", 0) or 0),
        "min_order_size": float(market.get("orderMinSize", 5) or 5),
        "tick_size": float(market.get("orderPriceMinTickSize", 0.01) or 0.01),
    }


def fetch_series_events(limit: int = 20) -> list[dict]:
    """Fetch recent events in the BTC 5-min series."""
    resp = requests.get(
        f"{GAMMA_BASE}/events",
        params={"series_slug": SERIES_SLUG, "limit": limit, "order": "startTime",
                "ascending": "false"},
        timeout=10,
    )
    if resp.status_code != 200:
        return []
    return resp.json() if isinstance(resp.json(), list) else []
