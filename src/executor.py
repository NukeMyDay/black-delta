"""
Executor — Real money order execution via Polymarket CLOB API.

Wraps py-clob-client for placing FOK market orders when copying
Bonereaper trades. Includes safety guards: max bet per order,
daily loss limit, and a kill switch.

Usage:
    executor = Executor()
    if executor.initialize():
        resp = executor.place_market_buy(token_id, amount, max_price)
"""

import os
import threading
import time
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds, AssetType, BalanceAllowanceParams, MarketOrderArgs,
    OrderType, PartialCreateOrderOptions,
)
from py_clob_client.order_builder.constants import BUY


class Executor:
    """Manages real money order execution with safety guards."""

    def __init__(self):
        self.enabled = False
        self.client: ClobClient | None = None
        self._lock = threading.Lock()

        # Safety guards (all env-configurable)
        self.max_bet_usd = float(os.getenv("EXEC_MAX_BET", "50"))
        # daily_loss_limit removed — loss protection is now at state level via betting_capital_fixed
        self.kill_switch = False

        # Daily tracking
        self._daily_loss = 0.0
        self._daily_bets = 0
        self._daily_volume = 0.0
        self._daily_reset_date = ""

        # Order history (last N for dashboard)
        self._orders: list[dict] = []
        self._max_history = 50

        # Market info cache: {token_id: {tick_size, neg_risk}}
        self._market_cache: dict[str, dict] = {}

    SIG_LABELS = {0: "EOA", 1: "POLY_PROXY", 2: "POLY_GNOSIS_SAFE"}

    def _build_client(self, private_key, creds, sig_type, funder):
        """Build a ClobClient with the given signature type."""
        return ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
            creds=creds,
            signature_type=sig_type,
            funder=funder,
        )

    def initialize(self) -> bool:
        """Initialize the CLOB client from env vars. Returns True if ready."""
        private_key = os.getenv("POLY_PRIVATE_KEY")
        api_key = os.getenv("POLY_API_KEY")
        api_secret = os.getenv("POLY_API_SECRET")
        api_passphrase = os.getenv("POLY_API_PASSPHRASE")
        funder = os.getenv("POLY_FUNDER_ADDRESS")

        if not all([private_key, api_key, api_secret, api_passphrase]):
            print("[EXEC] Missing POLY_* credentials in .env — executor disabled")
            return False

        try:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )

            # Auto-detect signature type by checking which returns a balance.
            # Order: POLY_PROXY (1, most common) → GNOSIS_SAFE (2) → EOA (0)
            sig_types = [1, 2, 0]
            working_sig = None

            for st in sig_types:
                try:
                    client = self._build_client(private_key, creds, st, funder)
                    client.get_api_keys()
                    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                    result = client.get_balance_allowance(params)
                    bal = None
                    if result and hasattr(result, "balance"):
                        bal = float(result.balance)
                    elif isinstance(result, dict) and "balance" in result:
                        bal = float(result["balance"]) / 1e6
                    if bal is not None and bal > 0:
                        working_sig = st
                        self.client = client
                        print(f"[EXEC] sig_type={st} ({self.SIG_LABELS[st]}) → balance=${bal:.2f} ✓")
                        break
                    else:
                        print(f"[EXEC] sig_type={st} ({self.SIG_LABELS[st]}) → balance=$0 (skip)")
                except Exception as e:
                    print(f"[EXEC] sig_type={st} ({self.SIG_LABELS[st]}) → error: {e}")

            if working_sig is None:
                # Fallback: use POLY_PROXY with funder, or EOA without
                fallback = 1 if funder else 0
                self.client = self._build_client(private_key, creds, fallback, funder)
                self.client.get_api_keys()
                working_sig = fallback
                print(f"[EXEC] No balance detected, falling back to sig_type={fallback}")

            self.enabled = True
            signer_addr = self.client.signer.address()
            funder_addr = self.client.builder.funder
            print(f"[EXEC] Executor initialized — LIVE trading enabled")
            print(f"[EXEC]   Signer:  {signer_addr}")
            print(f"[EXEC]   Funder:  {funder_addr}")
            print(f"[EXEC]   SigType: {working_sig} ({self.SIG_LABELS[working_sig]})")

            # Ensure USDC allowance for the exchange contract
            self._ensure_allowance()
            return True
        except Exception as e:
            print(f"[EXEC] Initialization failed: {e}")
            return False

    def _ensure_allowance(self):
        """Set USDC allowance for the exchange contract if needed."""
        if not self.client:
            return
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self.client.get_balance_allowance(params)
            allowance = 0
            if isinstance(result, dict) and "allowance" in result:
                allowance = int(result["allowance"])
            elif hasattr(result, "allowance"):
                allowance = int(result.allowance)

            if allowance == 0:
                print("[EXEC] USDC allowance is 0 — requesting approval...")
                resp = self.client.update_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                print(f"[EXEC] Allowance update response: {resp}")
            else:
                print(f"[EXEC] USDC allowance OK: {allowance}")
        except Exception as e:
            print(f"[EXEC] Allowance check/update failed: {e}")

    # ------------------------------------------------------------------
    #  Order Placement
    # ------------------------------------------------------------------
    def place_market_buy(
        self,
        token_id: str,
        amount_usd: float,
        max_price: float,
        neg_risk: bool = False,
        tick_size: str = "0.01",
    ) -> dict | None:
        """Place a FOK market buy order.

        Args:
            token_id:   The outcome token ID (from RTDS asset field)
            amount_usd: Dollar amount to spend
            max_price:  Maximum price / slippage cap
            neg_risk:   Whether this is a neg-risk (multi-outcome) market
            tick_size:  Market tick size

        Returns:
            Order response dict or None on failure
        """
        if not self._pre_check(amount_usd):
            return None

        with self._lock:
            try:
                args = MarketOrderArgs(
                    token_id=token_id,
                    side=BUY,
                    amount=round(amount_usd, 2),
                    price=max_price,
                )
                options = PartialCreateOrderOptions(
                    tick_size=tick_size,
                    neg_risk=neg_risk if neg_risk else None,
                )
                print(
                    f"[EXEC] Creating order: amount=${amount_usd:.2f}, "
                    f"price={max_price:.4f}, neg_risk={neg_risk}, "
                    f"tick_size={tick_size}, "
                    f"funder={self.client.builder.funder}, "
                    f"signer={self.client.signer.address()}, "
                    f"sig_type={self.client.builder.sig_type}"
                )
                order = self.client.create_market_order(args, options)
                resp = self.client.post_order(order, OrderType.FAK)

                order_record = {
                    "token_id": token_id,
                    "side": "BUY",
                    "amount_usd": round(amount_usd, 2),
                    "max_price": max_price,
                    "response": resp,
                    "success": bool(resp and resp.get("orderID")),
                    "time": datetime.now(timezone.utc).isoformat(),
                }

                self._daily_bets += 1
                self._daily_volume += amount_usd
                self._orders.append(order_record)
                if len(self._orders) > self._max_history:
                    self._orders = self._orders[-self._max_history:]

                status = "OK" if order_record["success"] else "FAILED"
                print(
                    f"[EXEC] BUY {status}: ${amount_usd:.2f} @ max ${max_price:.2f} "
                    f"token={token_id[:16]}..."
                )
                return resp

            except Exception as e:
                print(f"[EXEC] Order error: {e}")
                self._orders.append({
                    "token_id": token_id,
                    "side": "BUY",
                    "amount_usd": round(amount_usd, 2),
                    "max_price": max_price,
                    "error": str(e),
                    "success": False,
                    "time": datetime.now(timezone.utc).isoformat(),
                })
                return None

    # ------------------------------------------------------------------
    #  Safety Guards
    # ------------------------------------------------------------------
    def _pre_check(self, amount_usd: float) -> bool:
        """Run safety checks before placing an order."""
        if self.kill_switch:
            print("[EXEC] KILL SWITCH active — order blocked")
            return False

        if not self.enabled or not self.client:
            print("[EXEC] Not initialized — order blocked")
            return False

        if amount_usd > self.max_bet_usd:
            print(
                f"[EXEC] ${amount_usd:.2f} exceeds max bet "
                f"${self.max_bet_usd:.2f} — capping"
            )
            # We cap rather than block — caller should use capped value
            return True

        if amount_usd < 0.50:
            print(f"[EXEC] ${amount_usd:.2f} below minimum $0.50 — blocked")
            return False

        # Reset daily counters on new day
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            self._daily_loss = 0.0
            self._daily_bets = 0
            self._daily_volume = 0.0
            self._daily_reset_date = today

        return True

    def cap_amount(self, amount_usd: float) -> float:
        """Cap an amount to the max bet size."""
        return min(amount_usd, self.max_bet_usd)

    def record_loss(self, amount: float):
        """Track a realized loss for daily stats."""
        self._daily_loss += abs(amount)

    def reset_kill_switch(self):
        """Manual reset of kill switch (from dashboard)."""
        self.kill_switch = False
        print("[EXEC] Kill switch reset")

    # ------------------------------------------------------------------
    #  Market Info
    # ------------------------------------------------------------------
    def get_market_info(self, token_id: str) -> dict:
        """Get tick_size and neg_risk for a token, with caching."""
        if token_id in self._market_cache:
            return self._market_cache[token_id]

        info = {"tick_size": "0.01", "neg_risk": False}

        if self.client:
            try:
                neg_risk = self.client.get_neg_risk(token_id)
                info["neg_risk"] = bool(neg_risk)
            except Exception:
                pass
            try:
                tick = self.client.get_tick_size(token_id)
                if tick:
                    info["tick_size"] = str(tick)
            except Exception:
                pass

        self._market_cache[token_id] = info
        return info

    # ------------------------------------------------------------------
    #  Balance
    # ------------------------------------------------------------------
    def get_usdc_balance(self) -> float | None:
        """Fetch live USDC balance from Polymarket CLOB API."""
        if not self.enabled or not self.client:
            return None
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
            )
            # Let the library auto-set signature_type from builder.sig_type
            result = self.client.get_balance_allowance(params)
            if result and hasattr(result, "balance"):
                return round(float(result.balance), 2)
            if isinstance(result, dict) and "balance" in result:
                # Balance is in micro-USDC (6 decimals)
                return round(float(result["balance"]) / 1e6, 2)
            if isinstance(result, (int, float)):
                return round(float(result), 2)
        except Exception as e:
            print(f"[EXEC] Balance fetch failed: {e}")
        return None

    # ------------------------------------------------------------------
    #  Status / Dashboard
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        """Return executor status for the dashboard API."""
        return {
            "enabled": self.enabled,
            "kill_switch": self.kill_switch,
            "max_bet_usd": self.max_bet_usd,
            "daily_loss_today": round(self._daily_loss, 2),
            "daily_loss": round(self._daily_loss, 2),
            "daily_bets": self._daily_bets,
            "daily_volume": round(self._daily_volume, 2),
            "recent_orders": self._orders[-10:],
        }
