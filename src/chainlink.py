"""
Chainlink BTC/USD Price Feed — Polygon mainnet.

Polymarket resolves BTC Up/Down markets using this exact Chainlink oracle.
Reading from it directly gives us the TRUE reference price that determines
WIN/LOSE — not a Binance approximation that could diverge on close calls.

Resolution source (from Polymarket market data):
  https://data.chain.link/streams/btc-usd

Usage:
    price = get_btc_price()       # Latest BTC/USD from Chainlink
    price, ts = get_btc_price_with_timestamp()  # + update timestamp
"""

import logging
import time
import threading
from web3 import Web3

log = logging.getLogger("chainlink")

# Polygon mainnet public RPCs (fallback chain)
POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.drpc.org",
]

# Chainlink BTC/USD Aggregator on Polygon
# https://docs.chain.link/data-feeds/price-feeds/addresses?network=polygon
BTC_USD_FEED = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

# Minimal AggregatorV3Interface ABI (only what we need)
AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "_roundId", "type": "uint80"}],
        "name": "getRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Module-level cache (thread-safe via GIL for simple reads)
_cache_price: float = 0.0
_cache_ts: float = 0.0
_cache_updated_at: int = 0  # Chainlink's updatedAt timestamp
_cache_lock = threading.Lock()

# Cache TTL — Chainlink updates every ~27s on Polygon, no need to hammer
CACHE_TTL = 3.0  # seconds

_w3: Web3 | None = None
_contract = None
_decimals: int = 8


_init_attempted = False
_init_lock = threading.Lock()


def _init():
    """Lazy-init Web3 connection and contract, trying multiple RPCs."""
    global _w3, _contract, _decimals, _init_attempted
    if _w3 is not None:
        return True
    # All init work inside lock to prevent race conditions:
    # without this, Thread A could be mid-RPC-connect while Thread B
    # sees _init_attempted=True and returns False prematurely.
    with _init_lock:
        if _w3 is not None:
            return True
        if _init_attempted:
            return False
        _init_attempted = True

        for rpc in POLYGON_RPCS:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 3}))
                if not w3.is_connected():
                    continue
                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(BTC_USD_FEED),
                    abi=AGGREGATOR_ABI,
                )
                _decimals = contract.functions.decimals().call()
                _w3 = w3
                _contract = contract
                log.info("Chainlink BTC/USD initialized via %s (decimals=%d)", rpc, _decimals)
                return True
            except Exception as e:
                log.debug("Chainlink RPC %s failed: %s", rpc, e)
                continue
        log.error("Chainlink init failed: no working RPC — falling back to Binance for target")
        return False


def get_btc_price() -> float:
    """Get latest BTC/USD price from Chainlink on Polygon.

    Returns 0.0 on failure. Cached for CACHE_TTL seconds.
    """
    price, _ = get_btc_price_with_timestamp()
    return price


def _decode_round(result) -> tuple[int, float, int]:
    """Decode AggregatorV3 round data into (round_id, price, updated_at)."""
    round_id = int(result[0])
    answer = result[1]
    updated_at = int(result[3])
    price = answer / (10 ** _decimals)
    return round_id, price, updated_at


def _latest_round_data() -> tuple[int, float, int]:
    """Fetch the latest Chainlink round."""
    global _cache_price, _cache_ts, _cache_updated_at

    now = time.time()
    if now - _cache_ts < CACHE_TTL and _cache_price > 0:
        try:
            result = _contract.functions.latestRoundData().call()
            round_id = int(result[0])
            return round_id, _cache_price, _cache_updated_at
        except Exception:
            pass

    if not _init():
        return 0, _cache_price, _cache_updated_at

    result = _contract.functions.latestRoundData().call()
    round_id, price, updated_at = _decode_round(result)

    with _cache_lock:
        _cache_price = price
        _cache_ts = now
        _cache_updated_at = updated_at

    return round_id, price, updated_at


def _round_data(round_id: int) -> tuple[int, float, int]:
    """Fetch a specific Chainlink round."""
    if not _init():
        return 0, 0.0, 0
    if round_id <= 0:
        return 0, 0.0, 0
    result = _contract.functions.getRoundData(round_id).call()
    return _decode_round(result)


def get_btc_price_at_or_before_timestamp(target_ts: int,
                                         max_round_lookback: int = 32) -> tuple[float, int]:
    """Return the latest Chainlink price whose updatedAt is <= target_ts."""
    try:
        round_id, price, updated_at = _latest_round_data()
    except Exception as e:
        log.warning("Chainlink latest round fetch failed: %s", e)
        if _cache_price > 0 and _cache_updated_at > 0 and _cache_updated_at <= target_ts:
            return _cache_price, _cache_updated_at
        return 0.0, 0

    if price <= 0 or updated_at <= 0:
        return 0.0, 0
    if updated_at <= target_ts:
        return price, updated_at

    current_round = round_id
    for _ in range(max_round_lookback):
        current_round -= 1
        if current_round <= 0:
            break
        try:
            _, prev_price, prev_updated_at = _round_data(current_round)
        except Exception as e:
            log.debug("Chainlink historical round fetch failed (%s): %s", current_round, e)
            break
        if prev_price <= 0 or prev_updated_at <= 0:
            break
        if prev_updated_at <= target_ts:
            return prev_price, prev_updated_at

    log.warning("Chainlink could not locate round at/before ts=%s within %s rounds",
                target_ts, max_round_lookback)
    return 0.0, 0


def get_btc_price_at_or_after_timestamp(target_ts: int,
                                        max_round_lookback: int = 32) -> tuple[float, int]:
    """Return the first Chainlink price whose updatedAt is >= target_ts."""
    try:
        round_id, price, updated_at = _latest_round_data()
    except Exception as e:
        log.warning("Chainlink latest round fetch failed: %s", e)
        if _cache_price > 0 and _cache_updated_at >= target_ts:
            return _cache_price, _cache_updated_at
        return 0.0, 0

    if price <= 0 or updated_at <= 0:
        return 0.0, 0
    if updated_at < target_ts:
        return 0.0, 0

    current_round = round_id
    current_price = price
    current_updated_at = updated_at
    for _ in range(max_round_lookback):
        prev_round = current_round - 1
        if prev_round <= 0:
            return current_price, current_updated_at
        try:
            _, prev_price, prev_updated_at = _round_data(prev_round)
        except Exception as e:
            log.debug("Chainlink historical round fetch failed (%s): %s", prev_round, e)
            break
        if prev_price <= 0 or prev_updated_at <= 0:
            break
        if prev_updated_at < target_ts:
            return current_price, current_updated_at
        current_round = prev_round
        current_price = prev_price
        current_updated_at = prev_updated_at

    log.warning("Chainlink could not locate round at/after ts=%s within %s rounds",
                target_ts, max_round_lookback)
    return 0.0, 0


def get_btc_price_with_timestamp() -> tuple[float, int]:
    """Get BTC/USD price and Chainlink's updatedAt timestamp.

    Returns (price, updatedAt_unix) or (0.0, 0) on failure.
    """
    global _cache_price, _cache_ts, _cache_updated_at

    now = time.time()
    if now - _cache_ts < CACHE_TTL and _cache_price > 0:
        return _cache_price, _cache_updated_at

    if not _init():
        return _cache_price, _cache_updated_at  # return stale if available

    try:
        _, price, updated_at = _latest_round_data()
        return price, updated_at

    except Exception as e:
        log.warning("Chainlink price fetch failed: %s", e)
        # Allow retry on next call (don't permanently give up)
        return _cache_price, _cache_updated_at


def reset_init():
    """Allow re-attempting init (e.g. after network recovers)."""
    global _init_attempted, _w3, _contract
    with _init_lock:
        _init_attempted = False
        _w3 = None
        _contract = None
