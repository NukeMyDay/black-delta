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

    def _try_redeem_target(self, condition_id: str, cid_bytes: bytes, target: str, label: str) -> bool:
        """Attempt redemption against a specific contract target."""
        ctf = self.w3.eth.contract(address=target, abi=CTF_ABI)
        redeem_data = ctf.encodeABI(
            fn_name="redeemPositions",
            args=[USDC_ADDRESS, ZERO_BYTES32, cid_bytes, [1, 2]],
        )

        if self.sig_type == 0:
            tx = self._build_eoa_tx(target, redeem_data)
        else:
            tx = self._build_safe_tx(target, redeem_data)

        if not tx:
            return False

        signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

        if receipt.status == 1:
            self._redeemed.add(condition_id)
            print(f"[REDEEM] OK via {label}: {condition_id[:16]}... tx={tx_hash.hex()[:16]}...")
            return True
        else:
            print(f"[REDEEM] TX reverted via {label}: {condition_id[:16]}...")
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
                print(f"[REDEEM] {label} failed: {e}")

        return False

    def _build_eoa_tx(self, to: str, data: bytes) -> dict | None:
        """Build a direct transaction from the signer."""
        try:
            nonce = self.w3.eth.get_transaction_count(self.signer_address)
            gas_price = self.w3.eth.gas_price
            tx = {
                "to": to,
                "value": 0,
                "data": data,
                "nonce": nonce,
                "gas": 200_000,
                "gasPrice": min(gas_price * 2, self.w3.to_wei(100, "gwei")),
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

            exec_data = safe.encodeABI(
                fn_name="execTransaction",
                args=[
                    Web3.to_checksum_address(to),  # to
                    0,  # value
                    bytes.fromhex(data.hex() if isinstance(data, bytes) else data[2:]),  # data
                    0,  # operation (CALL)
                    0,  # safeTxGas
                    0,  # baseGas
                    0,  # gasPrice
                    zero_addr,  # gasToken
                    zero_addr,  # refundReceiver
                    signatures,  # signatures
                ],
            )

            nonce = self.w3.eth.get_transaction_count(self.signer_address)
            gas_price = self.w3.eth.gas_price
            tx = {
                "to": self.funder_address,
                "value": 0,
                "data": exec_data,
                "nonce": nonce,
                "gas": 300_000,
                "gasPrice": min(gas_price * 2, self.w3.to_wei(100, "gwei")),
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

    def sweep_pending(self):
        """Try to redeem all queued slugs. Called periodically by background loop."""
        if not self.enabled or not self._pending_slugs:
            return
        if not self._ensure_rpc():
            print(f"[REDEEM] Sweep: no RPC available ({len(self._pending_slugs)} slugs pending)")
            return
        done = []
        for slug, neg_risk in list(self._pending_slugs.items()):
            try:
                if self.try_redeem_slug(slug, neg_risk):
                    done.append(slug)
            except Exception as e:
                print(f"[REDEEM] Sweep error for {slug}: {e}")
        for slug in done:
            self._pending_slugs.pop(slug, None)

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
