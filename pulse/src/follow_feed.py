"""
Follow Feed — Copy-trade detection via Polymarket RTDS WebSocket.

Connects to Polymarket's Real-Time Data Socket and monitors all trades.
Filters for followed wallets and fires a callback when a match is found.

No authentication required (public trade feed).
"""

import asyncio
import json
import os
import threading
import time

import requests
import websockets

RTDS_WS = "wss://ws-live-data.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
WALLETS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "follow_wallets.json")

# Heartbeat interval (seconds)
PING_INTERVAL = 5


class FollowFeed:
    """Monitors Polymarket RTDS for trades by followed wallets."""

    def __init__(self):
        # Followed wallets: {normalized_address: info_dict}
        self.wallets: dict[str, dict] = {}
        # Set of all addresses to match (main + proxy, lowercase)
        self._match_set: set[str] = set()

        # WebSocket state
        self._ws_thread: threading.Thread | None = None
        self._ws_running = False
        self._ws_connected = False
        self._last_msg_ts: float = 0

        # Callback: called with (wallet_address, trade_payload) on match
        self.on_trade: callable | None = None

        # Recent detected trades (for dedup)
        self._seen_tx: set[str] = set()
        self._seen_tx_max = 500

    # ------------------------------------------------------------------
    #  Wallet Management
    # ------------------------------------------------------------------
    def add_wallet(self, address: str, label: str = "") -> dict:
        """Add a wallet to follow. Resolves proxy wallet automatically."""
        addr = address.lower().strip()
        if addr in self.wallets:
            # Update label if wallet already exists
            if label:
                self.wallets[addr]["label"] = label
                self._save_wallets()
            return self.wallets[addr]

        info = {
            "address": addr,
            "label": label,
            "proxy_wallet": None,
            "added_ts": time.time(),
            "trade_count": 0,
            "buy_count": 0,
            "sell_count": 0,
        }

        # Resolve proxy wallet via Polymarket data API
        proxy = self._resolve_proxy(addr)
        if proxy:
            info["proxy_wallet"] = proxy
            self._match_set.add(proxy)
            print(f"[FOLLOW] Resolved proxy wallet: {proxy[:10]}...")

        self._match_set.add(addr)
        self.wallets[addr] = info
        self._save_wallets()

        # Auto-start RTDS if this is the first wallet and not yet running
        if len(self.wallets) == 1 and not self._ws_running:
            self.start()

        print(f"[FOLLOW] Now following {label or addr[:10]}... ({len(self.wallets)} total)")
        return info

    def remove_wallet(self, address: str):
        """Stop following a wallet."""
        addr = address.lower().strip()
        info = self.wallets.pop(addr, None)
        if info:
            self._match_set.discard(addr)
            if info.get("proxy_wallet"):
                self._match_set.discard(info["proxy_wallet"])
            self._save_wallets()

    def load_wallets(self):
        """Load saved wallets from disk."""
        try:
            with open(WALLETS_FILE, "r") as f:
                saved = json.load(f)
            for entry in saved:
                addr = entry["address"].lower().strip()
                label = entry.get("label", "")
                self.add_wallet(addr, label)
            if saved:
                print(f"[FOLLOW] Loaded {len(saved)} wallet(s) from disk")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_wallets(self):
        """Persist wallets to disk."""
        os.makedirs(os.path.dirname(WALLETS_FILE), exist_ok=True)
        data = [
            {"address": info["address"], "label": info.get("label", ""),
             "proxy_wallet": info.get("proxy_wallet")}
            for info in self.wallets.values()
        ]
        with open(WALLETS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _resolve_proxy(self, address: str) -> str | None:
        """Resolve a user's proxy wallet via the Polymarket data API."""
        try:
            resp = requests.get(
                f"{DATA_API}/trades",
                params={"user": address, "limit": 1},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    proxy = data[0].get("proxyWallet", "")
                    if proxy:
                        return proxy.lower()
        except Exception as e:
            print(f"[FOLLOW] Proxy resolution failed: {e}")
        return None

    # ------------------------------------------------------------------
    #  WebSocket (RTDS)
    # ------------------------------------------------------------------
    def start(self):
        """Start the RTDS WebSocket feed in a background thread."""
        if self._ws_thread and self._ws_thread.is_alive():
            return
        if not self.wallets:
            print("[FOLLOW] No wallets configured, skipping start")
            return
        self._ws_running = True
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()
        print("[FOLLOW] RTDS trade monitor starting...")

    def stop(self):
        """Stop the RTDS feed."""
        self._ws_running = False

    def _ws_loop(self):
        """Background thread running the async RTDS WebSocket."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._ws_connect())

    async def _ws_connect(self):
        """Connect to Polymarket RTDS with auto-reconnect."""
        while self._ws_running:
            try:
                async with websockets.connect(
                    RTDS_WS, ping_interval=20, ping_timeout=10,
                ) as ws:
                    self._ws_connected = True
                    self._last_msg_ts = time.time()
                    print("[FOLLOW] Connected to Polymarket RTDS")

                    # Subscribe to all trade activity
                    sub_msg = json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "activity",
                            "type": "trades",
                        }],
                    })
                    await ws.send(sub_msg)

                    # Run heartbeat, reader, and watchdog concurrently
                    # FIRST_COMPLETED: if any task exits, reconnect
                    done, pending = await asyncio.wait(
                        [
                            asyncio.ensure_future(self._heartbeat(ws)),
                            asyncio.ensure_future(self._read_loop(ws)),
                            asyncio.ensure_future(self._watchdog(ws)),
                        ],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # Cancel remaining tasks
                    for task in pending:
                        task.cancel()
                    # Check if any raised
                    for task in done:
                        if task.exception():
                            raise task.exception()

            except Exception as e:
                self._ws_connected = False
                print(f"[FOLLOW] RTDS disconnected: {e}, reconnecting in 3s...")
                await asyncio.sleep(3)

    async def _heartbeat(self, ws):
        """Send text 'ping' every PING_INTERVAL seconds (RTDS protocol)."""
        while self._ws_running:
            try:
                await ws.send("ping")
            except Exception:
                return
            await asyncio.sleep(PING_INTERVAL)

    async def _watchdog(self, ws):
        """Force reconnect if no messages received for 30+ seconds."""
        while self._ws_running:
            await asyncio.sleep(10)
            if self._last_msg_ts > 0 and (time.time() - self._last_msg_ts) > 30:
                print("[FOLLOW] Watchdog: no messages for 30s, forcing reconnect")
                await ws.close()
                return

    async def _read_loop(self, ws):
        """Read messages from RTDS and filter for followed wallets."""
        async for msg in ws:
            if not self._ws_running:
                break
            self._last_msg_ts = time.time()

            if msg == "pong":
                continue

            try:
                data = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                continue

            # RTDS trade event
            if data.get("topic") == "activity" and data.get("type") == "trades":
                payload = data.get("payload")
                if not payload:
                    continue
                self._process_trade(payload)

    def _process_trade(self, payload: dict):
        """Check if a trade belongs to a followed wallet."""
        proxy = (payload.get("proxyWallet") or "").lower()
        if not proxy or proxy not in self._match_set:
            return

        # Dedup by transaction hash
        tx_hash = payload.get("transactionHash", "")
        if tx_hash and tx_hash in self._seen_tx:
            return
        if tx_hash:
            self._seen_tx.add(tx_hash)
            if len(self._seen_tx) > self._seen_tx_max:
                # Trim oldest (set doesn't preserve order, but good enough)
                self._seen_tx = set(list(self._seen_tx)[-200:])

        # Find which wallet this belongs to
        side = (payload.get("side") or "").upper()
        matched_wallet = None
        for addr, info in self.wallets.items():
            if proxy == addr or proxy == info.get("proxy_wallet"):
                matched_wallet = addr
                info["trade_count"] += 1
                if side == "BUY":
                    info["buy_count"] += 1
                else:
                    info["sell_count"] += 1
                break

        trade = {
            "wallet": matched_wallet or proxy,
            "proxy_wallet": proxy,
            "side": payload.get("side", ""),           # BUY or SELL
            "outcome": payload.get("outcome", ""),     # Up or Down
            "price": float(payload.get("price", 0)),
            "size": float(payload.get("size", 0)),
            "asset": payload.get("asset", ""),
            "condition_id": payload.get("conditionId", ""),
            "slug": payload.get("slug", ""),
            "event_slug": payload.get("eventSlug", ""),
            "title": payload.get("title", ""),
            "name": payload.get("name", ""),
            "tx_hash": tx_hash,
            "timestamp": payload.get("timestamp", 0),
            "detected_at": time.time(),
        }

        print(f"[FOLLOW] Trade detected: {trade['name'] or proxy[:10]} "
              f"{trade['side']} {trade['outcome']} @ ${trade['price']:.2f} "
              f"x{trade['size']:.1f} on {trade['slug']}")

        if self.on_trade:
            try:
                self.on_trade(trade)
            except Exception as e:
                print(f"[FOLLOW] Callback error: {e}")

    # ------------------------------------------------------------------
    #  Diagnostics
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        """Return diagnostic info about the follow feed."""
        msg_age = time.time() - self._last_msg_ts if self._last_msg_ts > 0 else -1
        return {
            "connected": self._ws_connected,
            "last_msg_age_seconds": round(msg_age, 1),
            "wallets_count": len(self.wallets),
            "wallets": [
                {
                    "address": info["address"],
                    "label": info.get("label", ""),
                    "proxy": info.get("proxy_wallet") or "",
                    "trades": info["trade_count"],
                    "buys": info.get("buy_count", 0),
                    "sells": info.get("sell_count", 0),
                }
                for info in self.wallets.values()
            ],
        }
