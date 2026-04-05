"""
Auto-redeem resolved Polymarket positions.

After a binary market resolves, winning shares are worth $1 but the
USDC stays locked until `redeemPositions` is called on the CTF contract.
This module handles that automatically so capital is recycled.

For neg_risk markets: calls the NegRiskAdapter (different ABI + approval required).
For GNOSIS_SAFE wallets: executes through the Safe proxy.
Requires a small amount of MATIC on the signer EOA for gas.
"""

import os
import time
import threading
import requests as _requests
from web3 import Web3
from eth_abi import encode

from src.polymarket import fetch_market, _parse_json_string

# Polygon — fallback RPCs if env var not set or primary fails
POLYGON_RPCS = [
    os.getenv("POLYGON_RPC", ""),
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
    "https://polygon-rpc.com",
    "https://polygon.drpc.org",
    "https://polygon.meowrpc.com",
    "https://polygon-mainnet.public.blastapi.io",
]
POLYGON_RPCS = [r for r in POLYGON_RPCS if r]  # filter empty


def _test_rpc_raw(url: str, timeout: int = 20) -> tuple[bool, str]:
    """Test an RPC endpoint with a raw HTTP POST (bypasses Web3 layer).
    Returns (success, detail_message)."""
    try:
        resp = _requests.post(
            url,
            json={"jsonrpc": "2.0", "method": "eth_chainId", "params": [], "id": 1},
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code == 200:
            data = resp.json()
            chain_id = data.get("result", "unknown")
            return True, f"status=200 chainId={chain_id}"
        else:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except _requests.exceptions.ConnectionError as e:
        return False, f"ConnectionError: {e}"
    except _requests.exceptions.Timeout:
        return False, f"Timeout after {timeout}s"
    except _requests.exceptions.SSLError as e:
        return False, f"SSLError: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

# Contracts
CTF_ADDRESS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
NEG_RISK_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
ZERO_BYTES32 = b"\x00" * 32

# Minimal ABIs — CTF redeemPositions (4 params)
CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# NegRiskAdapter redeemPositions (2 params — different selector!)
NEG_RISK_REDEEM_ABI = [
    {
        "inputs": [
            {"name": "_conditionId", "type": "bytes32"},
            {"name": "_amounts", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# ERC-1155 balance + approval (for checking token holdings & approval)
ERC1155_ABI = [
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

SAFE_EXEC_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"},
        ],
        "name": "execTransaction",
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class Redeemer:
    """Auto-redeems resolved Polymarket positions."""

    def __init__(self):
        self.w3: Web3 | None = None
        self.private_key: str | None = None
        self.signer_address: str | None = None
        self.funder_address: str | None = None
        self.sig_type: int = 0
        self.enabled = False
        self._redeemed: set[str] = set()  # condition IDs already redeemed
        self._pending_slugs: dict[str, bool] = {}  # slug → neg_risk, awaiting on-chain resolution
        self._nonce: int | None = None  # locally tracked nonce
        self._neg_risk_approved = False  # cached: Safe approved NegRiskAdapter on CTF?

    def initialize(self, private_key: str, signer: str, funder: str, sig_type: int) -> bool:
        """Initialize web3 connection, trying multiple RPCs."""
        try:
            self.private_key = private_key
            self.signer_address = Web3.to_checksum_address(signer)
            self.funder_address = Web3.to_checksum_address(funder)
            self.sig_type = sig_type

            # Phase 1: Raw HTTP test to diagnose connectivity
            print(f"[REDEEM] Testing {len(POLYGON_RPCS)} Polygon RPCs...")
            working_rpc = None
            for rpc_url in POLYGON_RPCS:
                ok, detail = _test_rpc_raw(rpc_url)
                if ok:
                    print(f"[REDEEM] ✓ {rpc_url} — {detail}")
                    working_rpc = rpc_url
                    break
                else:
                    print(f"[REDEEM] ✗ {rpc_url} — {detail}")

            # Phase 2: Initialize Web3 with working RPC
            if working_rpc:
                self.w3 = Web3(Web3.HTTPProvider(working_rpc, request_kwargs={"timeout": 20}))
                if self.w3.is_connected():
                    chain_id = self.w3.eth.chain_id
                    print(f"[REDEEM] Web3 connected (chainId={chain_id})")

                    # Check MATIC balance for gas
                    balance = self.w3.eth.get_balance(self.signer_address)
                    matic = self.w3.from_wei(balance, "ether")
                    if matic < 0.001:
                        print(f"[REDEEM] WARNING: Signer has {matic:.4f} MATIC — need ~0.01 for gas")
                        print(f"[REDEEM]   Send MATIC to: {self.signer_address}")
                    else:
                        print(f"[REDEEM] Signer MATIC balance: {matic:.4f}")
                else:
                    print(f"[REDEEM] Web3 is_connected=False despite raw test passing")
            else:
                print(f"[REDEEM] All {len(POLYGON_RPCS)} RPCs failed — check network/DNS from container")

            # Always enable queuing — sweep loop retries RPC
            self.enabled = True
            print(f"[REDEEM] Auto-redeem {'initialized' if self.w3 and self.w3.is_connected() else 'queuing only (will retry RPC)'}")
            return True
        except Exception as e:
            print(f"[REDEEM] Init failed: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return False

    def queue_resolved_trades(self, trades):
        """Scan trade list on startup and queue all resolved slugs for redemption."""
        seen = set()
        queued = 0
        for t in trades:
            if t.get("outcome") in ("win", "lose") and t.get("event_slug"):
                slug = t["event_slug"]
                if slug not in seen:
                    seen.add(slug)
                    self.queue_redeem(slug)
                    queued += 1
        if queued:
            print(f"[REDEEM] Startup: queued {queued} resolved slugs for redemption")

    def get_market_info(self, slug: str) -> dict | None:
        """Get conditionId, neg_risk, and token_ids for a market from Gamma API."""
        try:
            market = fetch_market(slug)
            if not market:
                return None
            condition_id = market.get("conditionId")
            if not condition_id:
                return None

            neg_risk = bool(market.get("negRisk", False))
            token_ids = _parse_json_string(market.get("clobTokenIds", []))

            return {
                "condition_id": condition_id,
                "neg_risk": neg_risk,
                "token_ids": token_ids,
            }
        except Exception as e:
            print(f"[REDEEM] Market info lookup failed for {slug}: {e}")
            return None

    # Keep old method as alias for backward compatibility
    def get_condition_id(self, slug: str) -> str | None:
        info = self.get_market_info(slug)
        return info["condition_id"] if info else None

    def is_resolved(self, condition_id: str) -> bool:
        """Check if a condition has been resolved on-chain."""
        try:
            ctf = self.w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
            cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            denominator = ctf.functions.payoutDenominator(cid_bytes).call()
            return denominator > 0
        except Exception:
            return False

    def _get_nonce(self) -> int:
        """Get the next nonce, using local tracking to avoid collisions."""
        on_chain = self.w3.eth.get_transaction_count(self.signer_address)
        if self._nonce is None or self._nonce < on_chain:
            self._nonce = on_chain
        nonce = self._nonce
        self._nonce += 1
        return nonce

    # ── Token balance helpers ───────────────────────────────────────────

    def _get_token_holder(self) -> str:
        """Return the address that holds the conditional tokens."""
        return self.funder_address if self.sig_type != 0 else self.signer_address

    def _get_token_balances(self, token_ids: list[str]) -> list[int]:
        """Query ERC-1155 balances on CTF for the token holder."""
        holder = self._get_token_holder()
        ctf = self.w3.eth.contract(address=CTF_ADDRESS, abi=ERC1155_ABI)
        amounts = []
        for tid in token_ids:
            try:
                balance = ctf.functions.balanceOf(holder, int(tid)).call()
                amounts.append(balance)
            except Exception as e:
                print(f"[REDEEM] Balance query failed for token {str(tid)[:16]}...: {e}")
                amounts.append(0)
        return amounts

    # ── Approval for NegRiskAdapter ─────────────────────────────────────

    def _check_neg_risk_approval(self) -> bool:
        """Check if the token holder has approved NegRiskAdapter as CTF operator."""
        if self._neg_risk_approved:
            return True
        try:
            holder = self._get_token_holder()
            ctf = self.w3.eth.contract(address=CTF_ADDRESS, abi=ERC1155_ABI)
            approved = ctf.functions.isApprovedForAll(holder, NEG_RISK_ADAPTER).call()
            if approved:
                self._neg_risk_approved = True
            return approved
        except Exception as e:
            print(f"[REDEEM] Approval check failed: {e}")
            return False

    def _grant_neg_risk_approval(self) -> bool:
        """Approve NegRiskAdapter as operator on CTF (via Safe execTransaction)."""
        print(f"[REDEEM] Granting CTF approval for NegRiskAdapter...")
        ctf = self.w3.eth.contract(address=CTF_ADDRESS, abi=ERC1155_ABI)
        approve_data = ctf.encode_abi(
            "setApprovalForAll",
            args=[NEG_RISK_ADAPTER, True],
        )
        ok = self._simulate_and_send(CTF_ADDRESS, approve_data, "approval", "SetApproval")
        if ok:
            self._neg_risk_approved = True
            print(f"[REDEEM] ✓ NegRiskAdapter approved as CTF operator")
        return ok

    # ── Simulation + Send ───────────────────────────────────────────────

    def _simulate_and_send(self, target: str, call_data, condition_id: str, label: str) -> bool:
        """Simulate inner call with eth_call, then build TX, sign, send, wait.

        Simulation runs BEFORE nonce allocation — if it reverts, no gas is spent.
        """
        caller = self._get_token_holder()

        # Step 1: Simulate the inner call (from the token holder, i.e. Safe)
        try:
            sim_data = call_data if isinstance(call_data, str) else ("0x" + call_data.hex())
            self.w3.eth.call({
                "from": caller,
                "to": target,
                "data": sim_data,
            })
            print(f"[REDEEM] {label}: simulation OK ✓")
        except Exception as e:
            err = str(e)
            print(f"[REDEEM] {label}: simulation REVERTED — {err[:150]}")
            print(f"[REDEEM] {label}: skipping TX to save gas")
            return False

        # Step 2: Build the actual on-chain TX (allocates nonce)
        if self.sig_type == 0:
            tx = self._build_eoa_tx(target, call_data)
        else:
            tx = self._build_safe_tx(target, call_data)

        if not tx:
            return False

        # Step 3: Sign and send
        try:
            signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as e:
            err_msg = str(e)
            # TX was not accepted — nonce was NOT consumed, roll back
            if self._nonce is not None:
                self._nonce -= 1
            if "already known" in err_msg or "underpriced" in err_msg:
                stuck_nonce = tx.get("nonce", "?")
                print(f"[REDEEM] {label}: nonce {stuck_nonce} blocked ({err_msg[:60]}), attempting cancel...")
                self._cancel_single_nonce(stuck_nonce)
                return False
            if "nonce too low" in err_msg:
                self._nonce = None
                print(f"[REDEEM] {label}: nonce too low, resyncing")
                return False
            if "in-flight transaction limit" in err_msg or "delegated" in err_msg:
                print(f"[REDEEM] {label}: delegated account limit — abort sweep")
                self._nonce = None
                raise RuntimeError(f"delegated account limit: {err_msg[:60]}")
            if "gapped-nonce" in err_msg:
                print(f"[REDEEM] {label}: gapped nonce — abort sweep")
                self._nonce = None
                raise RuntimeError(f"gapped nonce: {err_msg[:60]}")
            raise

        print(f"[REDEEM] {label}: TX sent {tx_hash.hex()[:16]}... waiting for confirmation...")

        # Step 4: Wait for receipt
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        except Exception:
            print(f"[REDEEM] {label}: TX {tx_hash.hex()[:16]}... not confirmed in 60s — stuck in mempool")
            raise  # Let sweep_pending catch this and stop

        if receipt.status == 1:
            self._redeemed.add(condition_id)
            gas_cost = receipt.gasUsed * tx.get("gasPrice", 0)
            gas_matic = self.w3.from_wei(gas_cost, "ether")
            print(f"[REDEEM] ✓ {label}: tx={tx_hash.hex()[:16]}... gas={receipt.gasUsed} ({gas_matic:.4f} MATIC)")
            return True
        else:
            print(f"[REDEEM] ✗ {label} reverted on-chain (gas used: {receipt.gasUsed})")
            return False

    # ── Redeem paths ────────────────────────────────────────────────────

    def _redeem_ctf(self, slug: str, condition_id: str) -> bool:
        """Redeem positions on the CTF contract (non-neg_risk markets)."""
        cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
        ctf = self.w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
        redeem_data = ctf.encode_abi(
            "redeemPositions",
            args=[USDC_ADDRESS, ZERO_BYTES32, cid_bytes, [1, 2]],
        )
        return self._simulate_and_send(CTF_ADDRESS, redeem_data, condition_id, f"CTF({slug})")

    def _redeem_neg_risk(self, slug: str, condition_id: str, token_ids: list[str]) -> bool:
        """Redeem positions through the NegRiskAdapter (neg_risk markets).

        NegRiskAdapter.redeemPositions(bytes32 conditionId, uint256[] amounts)
        - amounts = [yesTokenBalance, noTokenBalance] in base units
        - Adapter pulls tokens from caller via safeBatchTransferFrom
        - Requires CTF.isApprovedForAll(caller, NegRiskAdapter) == true
        """
        cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))
        holder = self._get_token_holder()

        # Get token balances
        amounts = self._get_token_balances(token_ids)
        print(f"[REDEEM] NegRisk({slug}): holder={holder[:10]}... "
              f"tokens={len(token_ids)} balances={amounts}")

        if all(a == 0 for a in amounts):
            print(f"[REDEEM] NegRisk({slug}): no token balances — already redeemed or no position")
            self._redeemed.add(condition_id)
            return True

        # Ensure NegRiskAdapter is approved as CTF operator
        if not self._check_neg_risk_approval():
            print(f"[REDEEM] NegRisk({slug}): approval needed for NegRiskAdapter")
            if not self._grant_neg_risk_approval():
                print(f"[REDEEM] NegRisk({slug}): approval failed — cannot redeem")
                return False
            # Small delay after approval TX before redeem TX
            time.sleep(3)

        # Build NegRiskAdapter.redeemPositions(conditionId, amounts)
        adapter = self.w3.eth.contract(address=NEG_RISK_ADAPTER, abi=NEG_RISK_REDEEM_ABI)
        redeem_data = adapter.encode_abi(
            "redeemPositions",
            args=[cid_bytes, amounts],
        )
        return self._simulate_and_send(NEG_RISK_ADAPTER, redeem_data, condition_id, f"NegRisk({slug})")

    # ── TX builders ─────────────────────────────────────────────────────

    def _build_eoa_tx(self, to: str, data) -> dict | None:
        """Build a direct transaction from the signer."""
        try:
            nonce = self._get_nonce()
            gas_price = self.w3.eth.gas_price
            call_data = data if isinstance(data, str) else ("0x" + data.hex())
            # 5x gas price (cap 500 gwei) — delegated accounts need high gas for inclusion
            tx = {
                "to": to,
                "value": 0,
                "data": call_data,
                "nonce": nonce,
                "gas": 300_000,
                "gasPrice": min(gas_price * 5, self.w3.to_wei(500, "gwei")),
                "chainId": 137,
            }
            return tx
        except Exception as e:
            print(f"[REDEEM] EOA tx build failed: {e}")
            return None

    def _build_safe_tx(self, to: str, data) -> dict | None:
        """Build an execTransaction call on the Gnosis Safe."""
        try:
            safe = self.w3.eth.contract(
                address=self.funder_address, abi=SAFE_EXEC_ABI
            )
            zero_addr = "0x0000000000000000000000000000000000000000"

            # Pre-validated signature: r=signer address, s=0, v=1
            # This tells the Safe the signer has approved via being the sender
            sig = (
                self.signer_address[2:].lower().zfill(64)  # r = padded address
                + "0" * 64  # s = 0
                + "01"  # v = 1 (pre-validated)
            )
            signatures = bytes.fromhex(sig)

            # Ensure call_data is bytes for Safe encoding
            if isinstance(data, str):
                call_bytes = bytes.fromhex(data[2:]) if data.startswith("0x") else bytes.fromhex(data)
            else:
                call_bytes = data

            exec_data = safe.encode_abi(
                "execTransaction",
                args=[
                    Web3.to_checksum_address(to),  # to
                    0,  # value
                    call_bytes,  # data
                    0,  # operation (CALL)
                    0,  # safeTxGas
                    0,  # baseGas
                    0,  # gasPrice
                    zero_addr,  # gasToken
                    zero_addr,  # refundReceiver
                    signatures,  # signatures
                ],
            )

            nonce = self._get_nonce()
            gas_price = self.w3.eth.gas_price
            # 5x gas price (cap 500 gwei) — delegated accounts need high gas for inclusion
            tx = {
                "to": self.funder_address,
                "value": 0,
                "data": exec_data,
                "nonce": nonce,
                "gas": 500_000,
                "gasPrice": min(gas_price * 5, self.w3.to_wei(500, "gwei")),
                "chainId": 137,
            }
            return tx
        except Exception as e:
            print(f"[REDEEM] Safe tx build failed: {e}")
            return None

    # ── Queue and sweep ─────────────────────────────────────────────────

    def queue_redeem(self, slug: str, neg_risk: bool = False):
        """Queue a slug for redemption (will be retried by sweep loop)."""
        if slug not in self._redeemed and slug not in self._pending_slugs:
            self._pending_slugs[slug] = neg_risk
            print(f"[REDEEM] Queued: {slug}")

    def _ensure_rpc(self) -> bool:
        """Ensure we have an active RPC connection, retry if needed."""
        if self.w3:
            try:
                if self.w3.is_connected():
                    return True
            except Exception:
                pass
        for rpc_url in POLYGON_RPCS:
            ok, detail = _test_rpc_raw(rpc_url, timeout=15)
            if not ok:
                continue
            try:
                self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
                if self.w3.is_connected():
                    print(f"[REDEEM] Reconnected: {rpc_url}")
                    return True
            except Exception:
                continue
        return False

    def _cancel_single_nonce(self, nonce: int) -> bool:
        """Send a 0-value transfer to replace a stuck TX at a specific nonce.

        Uses a burn address as target (not self) and high gas limit to handle
        delegated/EIP-7702 accounts where self-transfer may revert with 21k gas.
        """
        BURN_ADDR = "0x000000000000000000000000000000000000dEaD"
        try:
            gas_price = self.w3.eth.gas_price
            replace_gas = min(gas_price * 10, self.w3.to_wei(500, "gwei"))
            cancel_tx = {
                "to": Web3.to_checksum_address(BURN_ADDR),
                "value": 0,
                "data": b"",
                "nonce": nonce,
                "gas": 100_000,  # 100k — handles delegated/smart accounts
                "gasPrice": replace_gas,
                "chainId": 137,
            }
            signed = self.w3.eth.account.sign_transaction(cancel_tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"[REDEEM] Cancel TX for nonce {nonce}: {tx_hash.hex()[:16]}... "
                  f"(gasPrice={self.w3.from_wei(replace_gas, 'gwei'):.0f} gwei)")
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
            if receipt.status == 1:
                print(f"[REDEEM] ✓ Nonce {nonce} unblocked (gas used: {receipt.gasUsed})")
                return True
            else:
                print(f"[REDEEM] ✗ Cancel TX reverted for nonce {nonce} "
                      f"(gas used: {receipt.gasUsed}/{cancel_tx['gas']})")
                return False
        except Exception as e:
            err = str(e)
            if "nonce too low" in err:
                print(f"[REDEEM] Nonce {nonce} already cleared on-chain")
                return True
            if "in-flight transaction limit" in err or "delegated" in err:
                print(f"[REDEEM] Nonce {nonce}: delegated account limit — waiting 30s")
                time.sleep(30)
                return False
            print(f"[REDEEM] Cancel nonce {nonce} failed: {err[:120]}")
            return False

    def _clear_stuck_nonces(self) -> bool:
        """Detect and cancel stuck transactions blocking the nonce sequence.

        Compares pending vs confirmed nonce — if pending > confirmed, there are
        TXs sitting in the mempool that never got mined.  We send a 0-value TX
        to a burn address at each stuck nonce with aggressive gas to replace them.
        Returns True if all stuck nonces were cleared (or none existed).
        """
        try:
            confirmed = self.w3.eth.get_transaction_count(self.signer_address, "latest")
            pending = self.w3.eth.get_transaction_count(self.signer_address, "pending")
        except Exception as e:
            print(f"[REDEEM] Stuck-nonce check failed: {e}")
            return False

        stuck_count = pending - confirmed
        if stuck_count <= 0:
            return True  # nothing stuck

        print(f"[REDEEM] Detected {stuck_count} stuck TX(s) in mempool "
              f"(confirmed nonce={confirmed}, pending nonce={pending})")

        # Replace each stuck nonce with a 0-value burn-addr transfer at high gas
        BURN_ADDR = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")
        gas_price = self.w3.eth.gas_price
        replace_gas_price = min(gas_price * 5, self.w3.to_wei(300, "gwei"))

        cleared = 0
        for nonce in range(confirmed, pending):
            try:
                cancel_tx = {
                    "to": BURN_ADDR,
                    "value": 0,
                    "data": b"",
                    "nonce": nonce,
                    "gas": 100_000,
                    "gasPrice": replace_gas_price,
                    "chainId": 137,
                }
                signed = self.w3.eth.account.sign_transaction(cancel_tx, self.private_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                print(f"[REDEEM] Cancel TX sent for nonce {nonce}: {tx_hash.hex()[:16]}... "
                      f"({self.w3.from_wei(replace_gas_price, 'gwei'):.0f} gwei)")

                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
                if receipt.status == 1:
                    cleared += 1
                    print(f"[REDEEM] ✓ Nonce {nonce} cleared (gas used: {receipt.gasUsed})")
                else:
                    print(f"[REDEEM] ✗ Cancel TX reverted for nonce {nonce} "
                          f"(gas used: {receipt.gasUsed}/{cancel_tx['gas']})")
            except Exception as e:
                err_msg = str(e)
                if "nonce too low" in err_msg:
                    cleared += 1
                    print(f"[REDEEM] Nonce {nonce} already confirmed on-chain")
                elif "already known" in err_msg:
                    print(f"[REDEEM] Nonce {nonce}: replacement still underpriced, will retry")
                elif "in-flight transaction limit" in err_msg or "delegated" in err_msg:
                    print(f"[REDEEM] Delegated account limit hit — stopping, will retry next sweep")
                    break
                else:
                    print(f"[REDEEM] Nonce {nonce} cancel failed: {err_msg[:80]}")

        if cleared == stuck_count:
            print(f"[REDEEM] All {stuck_count} stuck nonce(s) cleared — redeems unblocked")
            return True
        else:
            print(f"[REDEEM] Cleared {cleared}/{stuck_count} stuck nonces — some still pending")
            return cleared > 0

    def sweep_pending(self):
        """Try to redeem all queued slugs sequentially. Called periodically."""
        if not self.enabled or not self._pending_slugs:
            return
        if not self._ensure_rpc():
            print(f"[REDEEM] Sweep: no RPC available ({len(self._pending_slugs)} slugs pending)")
            return

        # Step 0: Clear any stuck mempool transactions before redeeming
        stuck_cleared = self._clear_stuck_nonces()
        if not stuck_cleared:
            # Still have stuck TXs — don't attempt redeems, they'll just pile up
            print(f"[REDEEM] Sweep: stuck nonces not cleared, skipping redeem attempts")
            return

        # Reset local nonce from chain at start of each sweep
        self._nonce = None

        slugs = list(self._pending_slugs.items())
        # Delegated accounts only allow 1 in-flight TX — try 1 slug per sweep
        max_per_sweep = 1
        batch = slugs[:max_per_sweep]
        print(f"[REDEEM] Sweep: trying {len(batch)}/{len(slugs)} pending slugs...")
        done = []
        errors = 0
        for slug, neg_risk in batch:
            try:
                result = self.try_redeem_slug(slug, neg_risk)
                if result:
                    done.append(slug)
                    time.sleep(5)  # longer wait for delegated accounts
                else:
                    errors += 1
                    if errors >= 2:
                        # Too many failures, likely nonce issues — stop and retry next sweep
                        print(f"[REDEEM] Sweep: {errors} failures, stopping early")
                        break
            except Exception as e:
                print(f"[REDEEM] Sweep error for {slug}: {type(e).__name__}: {str(e)[:80]}")
                self._nonce = None
                errors += 1
                # Any exception = stop immediately (likely stuck TX or timeout)
                print(f"[REDEEM] Sweep: stopping after exception")
                break
        for slug in done:
            self._pending_slugs.pop(slug, None)
        if done or errors:
            print(f"[REDEEM] Sweep: {len(done)} redeemed, {errors} failed, {len(self._pending_slugs)} remaining")

    def try_redeem_slug(self, slug: str, neg_risk_hint: bool = False) -> bool:
        """Try to redeem a resolved market by slug.

        Fetches market data from Gamma API to get:
        - conditionId (for the on-chain call)
        - negRisk (to choose CTF vs NegRiskAdapter — overrides the queued hint)
        - clobTokenIds (for ERC-1155 balance queries on neg_risk markets)
        """
        info = self.get_market_info(slug)
        if not info:
            print(f"[REDEEM] No market info for {slug}")
            return False

        cid = info["condition_id"]
        neg_risk = info["neg_risk"]  # use actual API value, not the queued hint
        token_ids = info["token_ids"]

        if cid in self._redeemed:
            return True

        if not self.is_resolved(cid):
            print(f"[REDEEM] Not yet resolved on-chain: {slug} (cid={cid[:16]}...)")
            return False

        print(f"[REDEEM] Attempting redeem: {slug} neg_risk={neg_risk} tokens={len(token_ids)}")

        if neg_risk and token_ids:
            return self._redeem_neg_risk(slug, cid, token_ids)
        else:
            return self._redeem_ctf(slug, cid)
