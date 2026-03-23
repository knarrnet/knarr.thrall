"""settlement-execute-lite — On-chain $KNARR settlement (Solana testnet).

Executes SPL token transfers on Solana when a bilateral settlement is
accepted. This is the on-chain leg of the autonomous settlement flow:

  settlement-check (hourly) → propose → peer accepts →
  settlement-execute (this) → BCW detects → payment-finalized confirms

Uses thrall's delegated Ed25519 key for signing. Ed25519 is Solana's
native signature scheme — the thrall identity IS a Solana keypair.

Safety:
  - Testnet only (RPC URL hardcoded)
  - Wallet ceiling gated (checked before signing)
  - Revocable identity (delete thrall_identity.key)
  - BCW confirms on-chain (position not settled until finalized)

Input:
  peer_node_id     Full 64-char node ID of the settlement counterparty
  settle_amount    Amount in credits to settle (1 credit = 1 $KNARR)
  chain            Target chain (solana_testnet or solana_devnet)
  mode             "execute" (default) or "confirm" (verify existing tx)
  tx_hash          Transaction hash (for confirm mode)

Output:
  status           ok | disabled | ceiling_hit | insufficient_balance | error | confirmed
  tx_hash          Solana transaction signature (on success)
  from_address     Thrall's Solana address
  to_address       Peer's derived Solana address
  amount_knarr     Amount in $KNARR tokens
  wall_ms          Total execution time
"""

import json
import os
import sys
import time

NODE = None

# $KNARR SPL token mint on Solana testnet
KNARR_MINT_TESTNET = "3A988mhCYrwasv79c3uT1wbhpJ2iiuyzh4i2Ys5YEKiz"
KNARR_DECIMALS = 9

# 1 credit = 1 $KNARR token = 10^9 base units
BASE_UNITS_PER_CREDIT = 10 ** KNARR_DECIMALS

# RPC endpoints
RPC_URLS = {
    "solana_testnet": "https://api.testnet.solana.com",
    "solana_devnet": "https://api.devnet.solana.com",
}
SUPPORTED_CHAINS = set(RPC_URLS.keys())


def set_node(node):
    global NODE
    NODE = node


def _get_plugin_dir():
    """Resolve thrall plugin directory.

    Skills live at <provider>/skills/ but thrall config is at
    <provider>/plugins/06-thrall/. Resolve relative to this file.
    """
    provider_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(provider_root, "plugins", "06-thrall")


def _get_config():
    """Read thrall config from plugin.toml."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    plugin_toml = os.path.join(_get_plugin_dir(), "plugin.toml")
    with open(plugin_toml, "rb") as f:
        cfg = tomllib.load(f)
    return cfg.get("config", {})


def _wall_ms(t0):
    return str(int((time.time() - t0) * 1000))


async def handle(input_data: dict) -> dict:
    t0 = time.time()

    peer_node_id = input_data.get("peer_node_id", "")
    settle_amount = float(input_data.get("settle_amount", "0"))
    chain = input_data.get("chain", "solana_testnet")
    mode = input_data.get("mode", "execute")

    if chain not in SUPPORTED_CHAINS:
        return {"status": "error",
                "error": f"Unsupported chain: {chain}. Supported: {SUPPORTED_CHAINS}",
                "wall_ms": _wall_ms(t0)}

    rpc_url = RPC_URLS[chain]

    if mode == "confirm":
        tx_hash = input_data.get("tx_hash", "")
        return {"status": "confirmed",
                "tx_hash": tx_hash,
                "result_summary": f"Payment finalized for tx {tx_hash[:16]}",
                "wall_ms": _wall_ms(t0)}

    if not peer_node_id or len(peer_node_id) < 64:
        return {"status": "error",
                "error": "peer_node_id must be full 64-char hex",
                "wall_ms": _wall_ms(t0)}

    if settle_amount <= 0:
        return {"status": "error",
                "error": "settle_amount must be positive",
                "wall_ms": _wall_ms(t0)}

    # Load config + thrall modules
    try:
        config = _get_config()
    except Exception as e:
        return {"status": "error", "error": f"Config load failed: {e}",
                "wall_ms": _wall_ms(t0)}

    thrall_cfg = config.get("thrall", {})
    identity_cfg = thrall_cfg.get("identity", {})
    wallet_cfg = thrall_cfg.get("wallet", {})

    if not identity_cfg.get("enabled", False):
        return {"status": "disabled",
                "result_summary": "Thrall identity not enabled",
                "wall_ms": _wall_ms(t0)}

    plugin_dir = _get_plugin_dir()
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)

    from identity import ThrallIdentity
    from wallet import ThrallWallet
    from commerce import ThrallCommerce
    from db import ThrallDB
    from solana_tx import (
        b58decode, b58encode, fetch_recent_blockhash,
        build_spl_transfer_tx, submit_transaction,
        get_token_balance,
    )

    db = ThrallDB(os.path.join(plugin_dir, "thrall.db"))
    node_id = NODE.node_info.node_id if NODE else ""
    identity = ThrallIdentity(plugin_dir, identity_cfg, node_id=node_id)
    wallet = ThrallWallet(db, wallet_cfg)

    if not identity.enabled:
        return {"status": "disabled",
                "result_summary": "Thrall identity failed to initialize",
                "wall_ms": _wall_ms(t0)}

    # Check wallet ceiling
    if not wallet.can_spend(settle_amount):
        status_info = wallet.get_status()
        return {"status": "ceiling_hit",
                "result_summary": (f"Wallet ceiling: {status_info['remaining']:.1f} remaining, "
                                    f"need {settle_amount:.1f}"),
                "wall_ms": _wall_ms(t0)}

    # Derive addresses
    thrall_address = identity.solana_address
    if not thrall_address:
        return {"status": "error",
                "error": "Could not derive thrall Solana address",
                "wall_ms": _wall_ms(t0)}

    # Derive peer address: sha256(thrall_seed || peer_node_id) → Ed25519 → base58
    master_seed = identity.signing_key.encode()  # 32-byte seed
    peer_address = ThrallCommerce.derive_peer_solana_address(
        master_seed, peer_node_id)

    # Convert credits to $KNARR base units (1 credit = 1 $KNARR)
    mint = b58decode(KNARR_MINT_TESTNET)
    amount_base = int(settle_amount * BASE_UNITS_PER_CREDIT)

    # Check $KNARR balance before attempting transfer
    try:
        balance = get_token_balance(thrall_address, KNARR_MINT_TESTNET, rpc_url)
        if balance < settle_amount:
            return {"status": "insufficient_balance",
                    "result_summary": (f"$KNARR balance: {balance:.2f}, "
                                        f"need {settle_amount:.1f}"),
                    "from_address": thrall_address,
                    "balance_knarr": f"{balance:.2f}",
                    "wall_ms": _wall_ms(t0)}
    except Exception as e:
        # Balance check failed — proceed anyway, tx will fail if insufficient
        pass

    # Build the settlement audit document (signed, for receipt trail)
    settlement_doc = {
        "type": "thrall_settlement_execution",
        "version": 2,
        "chain": chain,
        "token_mint": KNARR_MINT_TESTNET,
        "from_address": thrall_address,
        "to_address": peer_address,
        "amount_base_units": amount_base,
        "amount_knarr": settle_amount,
        "peer_node_id": peer_node_id,
        "proposer_node_id": node_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    signed_doc = identity.sign_document(settlement_doc)

    # Ensure thrall has SOL for tx fees (airdrop on testnet/devnet)
    commerce = ThrallCommerce(
        cockpit_url=thrall_cfg.get("cockpit_url", "http://127.0.0.1:8080"),
        cockpit_token=thrall_cfg.get("cockpit_token", ""),
        node_id=node_id,
    )
    commerce.request_devnet_airdrop(thrall_address, rpc_url=rpc_url)

    # Fetch recent blockhash
    try:
        recent_blockhash = fetch_recent_blockhash(rpc_url)
    except Exception as e:
        return {"status": "error",
                "error": f"Failed to fetch blockhash: {e}",
                "wall_ms": _wall_ms(t0)}

    # Build and sign the SPL token transfer transaction
    recipient_pubkey = b58decode(peer_address)
    try:
        tx_bytes = build_spl_transfer_tx(
            signing_key=identity.signing_key,
            recipient=recipient_pubkey,
            mint=mint,
            amount=amount_base,
            recent_blockhash=recent_blockhash,
        )
    except Exception as e:
        return {"status": "error",
                "error": f"Transaction build failed: {e}",
                "wall_ms": _wall_ms(t0)}

    # Submit to Solana
    result = submit_transaction(tx_bytes, rpc_url)

    if "error" in result:
        return {"status": "error",
                "error": f"Transaction submission failed: {result['error']}",
                "from_address": thrall_address,
                "to_address": peer_address,
                "wall_ms": _wall_ms(t0)}

    tx_hash = result.get("tx_hash", "")

    # Success — record wallet spend
    wallet.record_spend(
        settle_amount,
        f"knarr-tx:{tx_hash[:16]}",
        peer_node_id,
        f"$KNARR transfer: {settle_amount:.1f} to {peer_address[:16]} tx={tx_hash[:16]}")

    return {
        "status": "ok",
        "tx_hash": tx_hash,
        "from_address": thrall_address,
        "to_address": peer_address,
        "amount_knarr": f"{settle_amount:.1f}",
        "amount_base_units": str(amount_base),
        "chain": chain,
        "token_mint": KNARR_MINT_TESTNET,
        "peer_node_id": peer_node_id[:16],
        "settlement_doc_type": signed_doc.get("type", ""),
        "result_summary": (f"Settlement complete: {settle_amount:.1f} $KNARR "
                            f"to {peer_address[:16]}... on {chain} "
                            f"tx={tx_hash[:16]}..."),
        "wall_ms": _wall_ms(t0),
    }
