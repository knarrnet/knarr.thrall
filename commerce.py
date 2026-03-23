"""Thrall Settlement Identity — Commerce wrappers.

Thin wrappers around cockpit API endpoints that give thrall structured
access to the credit/settlement system. Uses the same HTTP pattern as
handler.py:_cockpit_execute().

v3.3.1: Receipt queries use PluginContext.query_receipts() (D-051) when
available, falling back to cockpit HTTP. Ledger/economy queries still
use cockpit HTTP (no PluginContext equivalent exists).

v3.5.0: Added WM quarantine operations (approve/reject/review) and
Solana devnet settlement execution (derive address, build tx, submit).

Methods:
    query_ledger()          → GET /api/ledger
    get_economy()           → GET /api/economy
    query_receipt(ref)      → ctx.query_receipts() or GET /api/receipts/{ref}
    check_positions()       → computed from ledger
    build_netting_doc()     → signed by ThrallIdentity (eddsa-jcs-2022)
    submit_settlement()     → send settle_request mail to peer
    approve_quarantine()    → POST /api/wm/approve
    reject_quarantine()     → POST /api/wm/reject
    review_quarantine()     → GET /api/wm/review/{id}
    derive_peer_solana_address() → sha256(seed||node_id) → Ed25519 → base58
    build_solana_transfer()     → Solana transfer instruction
    submit_solana_tx()          → Solana RPC sendTransaction
    request_devnet_airdrop()    → Solana RPC requestAirdrop
"""

import asyncio
import json
import logging
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("thrall.commerce")


class ThrallCommerce:
    """Cockpit API wrappers for credit system access."""

    def __init__(self, cockpit_url: str, cockpit_token: str,
                 node_id: str = "", default_policy: dict = None,
                 query_receipts_fn: Callable = None):
        self._url = cockpit_url
        self._token = cockpit_token
        self._node_id = node_id
        self._query_receipts_fn = query_receipts_fn
        # Default credit policy for threshold calculation
        self._initial_credit = float((default_policy or {}).get("initial_credit", 100))
        self._min_balance = float((default_policy or {}).get("min_balance", -50))
        self._ssl_ctx = None
        if self._url.startswith("https"):
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _get_sync(self, path: str, timeout: int = 10) -> Any:
        """Synchronous GET request to cockpit API (used by asyncio.to_thread)."""
        req = Request(
            f"{self._url}{path}",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        try:
            resp = urlopen(req, timeout=timeout, context=self._ssl_ctx)
            return json.loads(resp.read())
        except HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            logger.error(f"COMMERCE_API_ERROR {path}: {e} body={body}")
            return {"error": str(e)}
        except (URLError, Exception) as e:
            logger.error(f"COMMERCE_API_ERROR {path}: {e}")
            return {"error": str(e)}

    async def _get(self, path: str, timeout: int = 10) -> Any:
        """Async GET request — offloads blocking HTTP to thread pool."""
        return await asyncio.to_thread(self._get_sync, path, timeout)

    async def query_ledger(self) -> List[dict]:
        """Fetch all bilateral positions from cockpit."""
        result = await self._get("/api/ledger")
        if isinstance(result, list):
            logger.debug(f"LEDGER_QUERY positions={len(result)}")
            return result
        if isinstance(result, dict) and "error" in result:
            return []
        return []

    async def get_economy(self) -> dict:
        """Fetch aggregated economy summary."""
        return await self._get("/api/economy")

    async def query_receipt(self, reference: str) -> Optional[dict]:
        """Fetch credit note by job_id reference.

        Uses PluginContext.query_receipts() when available (v0.35.0+),
        falls back to cockpit HTTP.
        """
        if self._query_receipts_fn:
            try:
                results = self._query_receipts_fn(
                    document_type=None, counterparty=None,
                    since=None, limit=50)
                for r in results:
                    if r.get("order_ref") == reference:
                        return r
            except Exception as e:
                logger.debug(f"query_receipts_fn failed, falling back to HTTP: {e}")

        result = await self._get(f"/api/receipts/{reference}")
        if isinstance(result, dict) and "error" not in result:
            return result
        return None

    async def check_positions(self, threshold: float = 0.8) -> List[dict]:
        """Find bilateral positions above utilization threshold.

        Mirrors the logic in knarr/commerce/netting.py:run_netting_cycle().
        Returns list of dicts with peer info, balance, utilization, settle_amount.
        """
        entries = await self.query_ledger()
        over_threshold = []

        for entry in entries:
            pk = entry.get("peer_public_key", "")
            balance = float(entry.get("balance", 0))

            # Use default policy for threshold calculation
            ic = self._initial_credit
            mb = self._min_balance
            credit_range = ic - mb
            if credit_range <= 0:
                continue

            utilization = (ic - balance) / credit_range
            if utilization < threshold:
                continue

            # Calculate settlement to reach 50% utilization
            soft_target = 0.5
            target_balance = ic - (soft_target * credit_range)
            settle_amount = target_balance - balance

            if settle_amount < 10.0:  # min_settlement_amount
                continue

            over_threshold.append({
                "peer_public_key": pk,
                "balance": round(balance, 2),
                "utilization_pct": round(utilization * 100, 1),
                "settle_amount": round(settle_amount, 2),
                "target_balance": round(target_balance, 2),
            })

        if over_threshold:
            logger.info(f"POSITION_CHECK found={len(over_threshold)} "
                        f"above {threshold * 100:.0f}% threshold")
        return over_threshold

    def build_netting_doc(self, peer_pk: str, settle_amount: float,
                          current_balance: float, target_balance: float,
                          identity) -> dict:
        """Build a signed netting proposal document (eddsa-jcs-2022).

        Args:
            peer_pk: Peer's public key hex.
            settle_amount: Credits to settle.
            current_balance: Current bilateral balance.
            target_balance: Target balance after settlement.
            identity: ThrallIdentity instance for signing.

        Returns:
            Signed dict with embedded proof object.
        """
        payload = {
            "type": "thrall_netting_proposal",
            "version": 1,
            "proposer_node_id": self._node_id,
            "thrall_public_key": identity.public_key_hex,
            "peer_public_key": peer_pk,
            "current_balance": current_balance,
            "settle_amount": settle_amount,
            "target_balance": target_balance,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        signed = identity.sign_document(payload)
        logger.info(f"NETTING_DOC peer={peer_pk[:16]} amount={settle_amount:.1f}")
        return signed

    async def approve_quarantine(self, document_id: str) -> dict:
        """Approve a WM-held document, promoting it to the internal bus.

        Calls wm.approve(document_id) via cockpit API.
        """
        def _do():
            payload = json.dumps({"document_id": document_id}).encode()
            req = Request(
                f"{self._url}/api/wm/approve",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._token}",
                },
                method="POST",
            )
            try:
                resp = urlopen(req, timeout=10, context=self._ssl_ctx)
                result = json.loads(resp.read())
                logger.info(f"WM_APPROVE doc={document_id[:16]} result={result}")
                return result
            except (HTTPError, URLError) as e:
                logger.error(f"WM_APPROVE failed: {e}")
                return {"error": str(e)}
        return await asyncio.to_thread(_do)

    async def reject_quarantine(self, document_id: str, reason: str = "") -> dict:
        """Reject a WM-held document, discarding it with a logged reason.

        Calls wm.reject(document_id, reason) via cockpit API.
        """
        def _do():
            payload = json.dumps({
                "document_id": document_id,
                "reason": reason,
            }).encode()
            req = Request(
                f"{self._url}/api/wm/reject",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._token}",
                },
                method="POST",
            )
            try:
                resp = urlopen(req, timeout=10, context=self._ssl_ctx)
                result = json.loads(resp.read())
                logger.info(f"WM_REJECT doc={document_id[:16]} reason={reason[:40]}")
                return result
            except (HTTPError, URLError) as e:
                logger.error(f"WM_REJECT failed: {e}")
                return {"error": str(e)}
        return await asyncio.to_thread(_do)

    async def review_quarantine(self, document_id: str) -> dict:
        """Pull a WM-held document for review without promoting it.

        Calls wm.request_review(document_id) via cockpit API.
        Returns the document payload for inspection.
        """
        result = await self._get(f"/api/wm/review/{document_id}")
        if isinstance(result, dict) and "error" not in result:
            logger.info(f"WM_REVIEW doc={document_id[:16]} type={result.get('document_type', '?')}")
        return result

    def submit_settlement(self, peer_pk: str, doc: dict,
                          send_mail_fn: Callable) -> bool:
        """Submit settlement proposal via knarr-mail.

        Sends as msg_type 'knarr/commerce/settle_request' which the
        peer's commerce handler (handlers.py:handle_settle_request)
        already processes.
        """
        try:
            body = {
                "type": "knarr/commerce/settle_request",
                "proposal": doc,
            }
            send_mail_fn(peer_pk, "knarr/commerce/settle_request", body)
            logger.info(f"SETTLEMENT_SUBMITTED peer={peer_pk[:16]}")
            return True
        except Exception as e:
            logger.error(f"SETTLEMENT_SUBMIT_FAILED peer={peer_pk[:16]}: {e}")
            return False

    # ── Solana devnet settlement execution ──

    @staticmethod
    def derive_peer_solana_address(master_seed: bytes, peer_node_id: str) -> str:
        """Derive a deterministic Solana address for a peer.

        Uses sha256(master_seed || node_id) → Ed25519 seed → public key → base58.
        Same derivation as knarr/commerce/wallet.py but using thrall's seed.
        Full 64-char node_id required (not prefix).
        """
        import hashlib
        derived_seed = hashlib.sha256(master_seed + peer_node_id.encode()).digest()

        from nacl.signing import SigningKey
        sk = SigningKey(derived_seed)
        pk_bytes = sk.verify_key.encode()

        try:
            import base58
            return base58.b58encode(pk_bytes).decode()
        except ImportError:
            return pk_bytes.hex()

    @staticmethod
    def build_solana_transfer(from_pubkey: bytes, to_pubkey: bytes,
                               lamports: int) -> bytes:
        """Build a raw Solana SystemProgram.Transfer instruction.

        Constructs the minimal wire format for a SOL transfer.
        No external Solana SDK needed — just byte packing.

        Returns the serialized transaction message (unsigned).
        """
        import struct

        # SystemProgram ID (all zeros)
        system_program = b'\x00' * 32

        # Transfer instruction index = 2
        # Data: u32 instruction (2) + u64 lamports
        ix_data = struct.pack('<I', 2) + struct.pack('<Q', lamports)

        # Compact instruction format
        instruction = {
            "program_id": system_program,
            "accounts": [
                {"pubkey": from_pubkey, "is_signer": True, "is_writable": True},
                {"pubkey": to_pubkey, "is_signer": False, "is_writable": True},
            ],
            "data": ix_data,
        }

        return instruction

    async def submit_solana_tx(self, signed_tx_base64: str,
                               rpc_url: str = "https://api.devnet.solana.com") -> dict:
        """Submit a signed transaction to Solana via JSON-RPC.

        Devnet only — the URL is locked in config.
        Returns: {"tx_hash": "...", "status": "ok"} or {"error": "..."}
        """
        def _do():
            payload = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    signed_tx_base64,
                    {"encoding": "base64", "preflightCommitment": "confirmed"},
                ],
            }).encode()
            req = Request(
                rpc_url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            ssl_ctx = ssl.create_default_context()
            try:
                resp = urlopen(req, timeout=30, context=ssl_ctx)
                result = json.loads(resp.read())
                if "error" in result:
                    err = result["error"]
                    logger.error(f"SOLANA_TX_ERROR: {err}")
                    return {"error": str(err)}
                tx_hash = result.get("result", "")
                logger.info(f"SOLANA_TX_SUBMITTED tx={tx_hash[:16]}...")
                return {"tx_hash": tx_hash, "status": "ok"}
            except Exception as e:
                logger.error(f"SOLANA_TX_FAILED: {e}")
                return {"error": str(e)}
        return await asyncio.to_thread(_do)

    async def request_devnet_airdrop(self, address: str,
                                     lamports: int = 2_000_000_000,
                                     rpc_url: str = "https://api.devnet.solana.com") -> dict:
        """Request SOL airdrop on devnet for funding test transactions.

        Default: 2 SOL (2_000_000_000 lamports).
        Only works on devnet — will fail silently on mainnet.
        """
        def _do():
            payload = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "requestAirdrop",
                "params": [address, lamports],
            }).encode()
            req = Request(
                rpc_url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            ssl_ctx = ssl.create_default_context()
            try:
                resp = urlopen(req, timeout=30, context=ssl_ctx)
                result = json.loads(resp.read())
                if "error" in result:
                    logger.warning(f"AIRDROP_ERROR: {result['error']}")
                    return {"error": str(result["error"])}
                tx_sig = result.get("result", "")
                logger.info(f"AIRDROP_OK address={address[:16]}... tx={tx_sig[:16]}...")
                return {"tx_hash": tx_sig, "status": "ok", "lamports": lamports}
            except Exception as e:
                logger.warning(f"AIRDROP_FAILED: {e}")
                return {"error": str(e)}
        return await asyncio.to_thread(_do)
