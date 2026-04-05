"""
Auto-redeem resolved Polymarket positions.

After a binary market resolves, winning shares are worth $1 but the
USDC stays locked until `redeemPositions` is called on the CTF contract.
This module handles that automatically so capital is recycled.

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
NEG_RISK_ADAPTER = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
ZERO_BYTES32 = b"\x00" * 32

# Minimal ABIs
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

    def get_condition_id(self, slug: str) -> str | None:
        """Get conditionId for a market from Gamma API."""
        try:
            market = fetch_market(slug)
            if market and market.get("conditionId"):
                return market["conditionId"]
        except Exception as e:
            print(f"[REDEEM] conditionId lookup failed for {slug}: {e}")
        return None

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

    def _try_redeem_target(self, condition_id: str, cid_bytes: bytes, target: str, label: str) -> bool:
        """Attempt redemption against a specific contract target."""
        ctf = self.w3.eth.contract(address=target, abi=CTF_ABI)
        redeem_data = ctf.encode_abi(
            "redeemPositions",
            args=[USDC_ADDRESS, ZERO_BYTES32, cid_bytes, [1, 2]],
        )

        if self.sig_type == 0:
            tx = self._build_eoa_tx(target, redeem_data)
        else:
            tx = self._build_safe_tx(target, redeem_data)

        if not tx:
            return False

        try:
            signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as e:
            err_msg = str(e)
            # TX was not accepted — nonce was NOT consumed, roll back
            if self._nonce is not None:
                self._nonce -= 1
            if "already known" in err_msg or "underpriced" in err_msg:
                # TX with this nonce already in mempool — try to clear it now
                stuck_nonce = tx.get("nonce", "?")
                print(f"[REDEEM] {label}: nonce {stuck_nonce} blocked ({err_msg[:60]}), attempting cancel...")
                self._cancel_single_nonce(stuck_nonce)
                return False
            if "nonce too low" in err_msg:
                # Nonce already used on-chain — resync
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

        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        except Exception as timeout_err:
            # TX sent but not mined — it's stuck in mempool, don't proceed with more TXs
            print(f"[REDEEM] {label}: TX {tx_hash.hex()[:16]}... not confirmed in 60s — stuck in mempool")
            # This nonce is consumed (TX is pending), but don't send more until it clears
            raise  # Let sweep_pending catch this and stop

        if receipt.status == 1:
            self._redeemed.add(condition_id)
            print(f"[REDEEM] ✓ {label}: {condition_id[:16]}... tx={tx_hash.hex()[:16]}...")
            return True
        else:
            print(f"[REDEEM] ✗ {label} reverted: {condition_id[:16]}... (nonce used, trying next target)")
            return False

    def redeem(self, condition_id: str, neg_risk: bool = False) -> bool:
        """Redeem positions for a resolved condition. Tries both CTF and NegRiskAdapter."""
        if not self.enabled or not self.w3:
            return False

        if condition_id in self._redeemed:
            return True

        cid_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        # Try preferred target first, then fallback to the other
        targets = [
            (NEG_RISK_ADAPTER, "NegRisk") if neg_risk else (CTF_ADDRESS, "CTF"),
            (CTF_ADDRESS, "CTF") if neg_risk else (NEG_RISK_ADAPTER, "NegRisk"),
        ]

        for target, label in targets:
            try:
                if self._try_redeem_target(condition_id, cid_bytes, target, label):
                    return True
            except Exception as e:
                # TX timeout or critical error — don't try next target, it'll just pile up
                print(f"[REDEEM] {label} failed: {type(e).__name__}: {str(e)[:60]}")
                raise  # Let sweep_pending handle it and stop

        return False

    def _build_eoa_tx(self, to: str, data: bytes) -> dict | None:
        """Build a direct transaction from the signer."""
        try:
            nonce = self._get_nonce()
            gas_price = self.w3.eth.gas_price
            # 5x gas price (cap 500 gwei) — delegated accounts need high gas for inclusion
            tx = {
                "to": to,
                "value": 0,
                "data": data,
                "nonce": nonce,
                "gas": 300_000,
                "gasPrice": min(gas_price * 5, self.w3.to_wei(500, "gwei")),
                "chainId": 137,
            }
            return tx
        except Exception as e:
            print(f"[REDEEM] EOA tx build failed: {e}")
            return None

    def _build_safe_tx(self, to: str, data: bytes) -> dict | None:
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

            exec_data = safe.encode_abi(
                "execTransaction",
                args=[
                    Web3.to_checksum_address(to),  # to
                    0,  # value
                    bytes.fromhex(data[2:]) if isinstance(data, str) else data,  # data
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
        to ourselves at each stuck nonce with aggressive gas to replace them.
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
        # Uses burn addr (not self) + 100k gas to handle delegated/EIP-7702 accounts
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

    def try_redeem_slug(self, slug: str, neg_risk: bool = False) -> bool:
        """Try to redeem a resolved market by slug."""
        cid = self.get_condition_id(slug)
        if not cid:
            print(f"[REDEEM] No conditionId for {slug}")
            return False

        if cid in self._redeemed:
            return True

        if not self.is_resolved(cid):
            print(f"[REDEEM] Not yet resolved on-chain: {slug} (cid={cid[:16]}...)")
            return False

        return self.redeem(cid, neg_risk)
