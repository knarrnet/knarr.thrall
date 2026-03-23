"""Solana SPL Token transaction builder — no external SDK.

Builds minimal Solana transactions using only PyNaCl + stdlib.
Used by settlement-execute-lite for on-chain $KNARR transfers.

Supports:
- SPL Token Transfer (opcode 3)
- CreateAssociatedTokenAccountIdempotent (opcode 1)
- Transaction serialization (legacy v0 format)
- Recent blockhash fetching
- Associated Token Account (ATA) derivation

Transaction layout (legacy v0):
  [compact_u16(num_sigs)] [sig * 64 bytes]
  [header: 3 bytes] [compact_u16(num_keys)] [key * 32 bytes]
  [recent_blockhash: 32 bytes]
  [compact_u16(num_instructions)] [instruction...]

Each instruction:
  [program_id_index: u8]
  [compact_u16(num_accounts)] [account_index: u8]
  [compact_u16(data_len)] [data bytes]
"""

import base64
import hashlib
import json
import logging
import ssl
import struct
from typing import List, Tuple
from urllib.request import Request, urlopen

logger = logging.getLogger("thrall.solana_tx")

# ── Base58 ──────────────────────────────────────────────────────────────

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_MAP = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58decode(s: str) -> bytes:
    """Decode a base58 string to bytes."""
    n = 0
    for c in s:
        n = n * 58 + _B58_MAP[c]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    # Count only LEADING "1"s (each represents a 0x00 byte)
    pad = 0
    for c in s:
        if c == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


def b58encode(data: bytes) -> str:
    """Encode bytes to base58 string."""
    n = int.from_bytes(data, "big")
    if n == 0:
        pad = sum(1 for b in data if b == 0)
        return "1" * max(pad, 1)
    result = []
    while n > 0:
        n, r = divmod(n, 58)
        result.append(_B58_ALPHABET[r])
    for byte in data:
        if byte == 0:
            result.append("1")
        else:
            break
    return "".join(reversed(result))


# ── Well-known program IDs ──────────────────────────────────────────────

SYSTEM_PROGRAM = bytes(32)
TOKEN_PROGRAM = b58decode("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = b58decode("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM = b58decode("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


# ── Compact-u16 encoding ───────────────────────────────────────────────

def compact_u16(n: int) -> bytes:
    """Encode integer as Solana compact-u16."""
    buf = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            buf.append(byte | 0x80)
        else:
            buf.append(byte)
            break
    return bytes(buf)


# ── Ed25519 on-curve check ─────────────────────────────────────────────

# Constants for Ed25519 twisted Edwards curve: -x² + y² = 1 + d·x²·y²
_ED25519_P = 2**255 - 19
_ED25519_D = -121665 * pow(121666, -1, _ED25519_P) % _ED25519_P
_ED25519_SQRT_NEG1 = pow(2, (_ED25519_P - 1) // 4, _ED25519_P)


def _is_on_ed25519_curve(point_bytes: bytes) -> bool:
    """Check if 32 bytes decompress to a valid non-identity Ed25519 point.

    Matches Solana's on-curve check exactly (compressed Edwards Y
    decompression + identity exclusion). Does NOT check subgroup
    membership — this is intentional, as Solana's findProgramAddress
    only checks decompressibility, not subgroup.

    NaCl's crypto_sign_ed25519_pk_to_curve25519 is too strict: it also
    rejects small-order and non-main-subgroup points, which causes
    incorrect bump selection for PDA derivation.
    """
    if len(point_bytes) != 32:
        return False
    y = int.from_bytes(point_bytes, "little") & ((1 << 255) - 1)
    if y >= _ED25519_P:
        return False
    y2 = y * y % _ED25519_P
    num = (y2 - 1) % _ED25519_P
    den = (1 + _ED25519_D * y2) % _ED25519_P
    if den == 0:
        return False
    x2 = num * pow(den, -1, _ED25519_P) % _ED25519_P
    if x2 == 0:
        return y != 1  # (0, 1) is the identity point
    beta = pow(x2, (_ED25519_P + 3) // 8, _ED25519_P)
    if (beta * beta - x2) % _ED25519_P == 0:
        return True
    if (beta * beta + x2) % _ED25519_P == 0:
        return True
    return False  # not a quadratic residue → not on curve


# ── PDA and ATA derivation ──────────────────────────────────────────────

def find_program_address(seeds: List[bytes], program_id: bytes) -> Tuple[bytes, int]:
    """Derive a Program Derived Address (PDA).

    Equivalent to Solana's findProgramAddress: tries bumps from 255 down,
    returns the first candidate that is NOT on the Ed25519 curve.
    """
    for bump in range(255, -1, -1):
        h = hashlib.sha256()
        for seed in seeds:
            h.update(seed)
        h.update(bytes([bump]))
        h.update(program_id)
        h.update(b"ProgramDerivedAddress")
        candidate = h.digest()
        if not _is_on_ed25519_curve(candidate):
            return candidate, bump
    raise ValueError("Could not derive PDA (exhausted all bumps)")


def get_associated_token_address(owner: bytes, mint: bytes,
                                  token_program: bytes = None) -> bytes:
    """Derive the Associated Token Account address for an owner + mint.

    Uses TOKEN_2022_PROGRAM by default ($KNARR is a Token-2022 mint).
    Pass token_program=TOKEN_PROGRAM for legacy SPL tokens.
    """
    if token_program is None:
        token_program = TOKEN_2022_PROGRAM
    pda, _ = find_program_address(
        [owner, token_program, mint],
        ATA_PROGRAM,
    )
    return pda


# ── RPC helpers ─────────────────────────────────────────────────────────

def fetch_recent_blockhash(rpc_url: str) -> bytes:
    """Fetch a recent blockhash from Solana RPC. Returns 32 bytes."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getLatestBlockhash",
        "params": [{"commitment": "confirmed"}],
    }).encode()

    req = Request(rpc_url, data=payload,
                  headers={"Content-Type": "application/json"})
    ctx = ssl.create_default_context()
    resp = urlopen(req, timeout=15, context=ctx)
    data = json.loads(resp.read())

    if "error" in data:
        raise RuntimeError(f"getLatestBlockhash failed: {data['error']}")

    blockhash_b58 = data["result"]["value"]["blockhash"]
    return b58decode(blockhash_b58)


def get_token_balance(wallet_b58: str, mint_b58: str, rpc_url: str) -> float:
    """Get SPL token balance for a wallet. Returns 0.0 if no account."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet_b58,
            {"mint": mint_b58},
            {"encoding": "jsonParsed"},
        ],
    }).encode()

    req = Request(rpc_url, data=payload,
                  headers={"Content-Type": "application/json"})
    ctx = ssl.create_default_context()
    resp = urlopen(req, timeout=15, context=ctx)
    data = json.loads(resp.read())

    if "error" in data:
        return 0.0

    accounts = data.get("result", {}).get("value", [])
    total = 0.0
    for acct in accounts:
        try:
            info = acct["account"]["data"]["parsed"]["info"]["tokenAmount"]
            ui = info.get("uiAmount")
            if ui is not None and isinstance(ui, (int, float)):
                total += float(ui)
            else:
                raw = int(info.get("amount", 0))
                decimals = int(info.get("decimals", 0))
                total += raw / (10 ** decimals) if decimals else float(raw)
        except (KeyError, ValueError, TypeError):
            pass
    return total


def submit_transaction(tx_bytes: bytes, rpc_url: str) -> dict:
    """Submit a signed transaction to Solana. Returns {tx_hash, status} or {error}."""
    encoded = base64.b64encode(tx_bytes).decode("ascii")
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            encoded,
            {"encoding": "base64", "preflightCommitment": "confirmed"},
        ],
    }).encode()

    req = Request(rpc_url, data=payload,
                  headers={"Content-Type": "application/json"})
    ctx = ssl.create_default_context()
    try:
        resp = urlopen(req, timeout=30, context=ctx)
        data = json.loads(resp.read())
        if "error" in data:
            logger.error(f"sendTransaction RPC error: {data['error']}")
            return {"error": str(data["error"])}
        tx_hash = data.get("result", "")
        logger.info(f"SOLANA_TX submitted: {tx_hash}")
        return {"tx_hash": tx_hash, "status": "ok"}
    except Exception as e:
        logger.error(f"sendTransaction failed: {e}")
        return {"error": str(e)}


# ── Transaction builder ─────────────────────────────────────────────────

def build_spl_transfer_tx(
    signing_key,             # PyNaCl SigningKey (thrall identity)
    recipient: bytes,        # 32-byte recipient public key
    mint: bytes,             # 32-byte SPL token mint address
    amount: int,             # Amount in base units (1 token = 10^decimals)
    recent_blockhash: bytes, # 32-byte recent blockhash
    token_program: bytes = None,  # Token program (default: Token-2022)
) -> bytes:
    """Build a signed SPL token transfer transaction.

    Creates two instructions:
    1. CreateAssociatedTokenAccountIdempotent for recipient
       (no-op if ATA already exists, creates if not)
    2. SPL Token Transfer from sender's ATA to recipient's ATA

    Sender's ATA must already exist and have sufficient balance.
    Uses Token-2022 program by default ($KNARR is a Token-2022 mint).

    Returns the fully serialized, signed transaction ready for RPC submission.
    """
    if token_program is None:
        token_program = TOKEN_2022_PROGRAM

    sender = signing_key.verify_key.encode()  # 32-byte pubkey
    source_ata = get_associated_token_address(sender, mint, token_program)
    dest_ata = get_associated_token_address(recipient, mint, token_program)

    # Account keys — ordered per Solana convention:
    #   [writable signers] [readonly signers] [writable non-signers] [readonly non-signers]
    accounts = [
        sender,          # 0: fee payer + token authority (signer, writable)
        source_ata,      # 1: source token account (writable)
        dest_ata,        # 2: destination token account (writable)
        recipient,       # 3: recipient wallet (read-only, ATA owner)
        mint,            # 4: token mint (read-only)
        SYSTEM_PROGRAM,  # 5: system program (read-only)
        token_program,   # 6: token program — Token-2022 (read-only)
        ATA_PROGRAM,     # 7: ATA program (read-only)
    ]

    # Message header
    num_required_signatures = 1   # only sender signs
    num_readonly_signed = 0       # no read-only signers
    num_readonly_unsigned = 5     # indices 3,4,5,6,7

    # Instruction 1: CreateAssociatedTokenAccountIdempotent
    # ATA program (index 7), opcode 1 = CreateIdempotent
    # Accounts: [payer, ata, wallet, mint, system_program, token_program]
    ix1_program = 7
    ix1_accounts = bytes([0, 2, 3, 4, 5, 6])
    ix1_data = bytes([1])  # CreateIdempotent discriminator

    # Instruction 2: SPL Token Transfer
    # Token program (index 6), opcode 3 = Transfer
    # Accounts: [source, destination, authority]
    # Data: opcode(u8) + amount(u64 LE)
    ix2_program = 6
    ix2_accounts = bytes([1, 2, 0])
    ix2_data = bytes([3]) + struct.pack("<Q", amount)

    # ── Serialize message ──
    msg = bytearray()

    # Header (3 bytes)
    msg.append(num_required_signatures)
    msg.append(num_readonly_signed)
    msg.append(num_readonly_unsigned)

    # Account keys
    msg.extend(compact_u16(len(accounts)))
    for key in accounts:
        msg.extend(key)

    # Recent blockhash
    msg.extend(recent_blockhash)

    # Instructions (2)
    msg.extend(compact_u16(2))

    # Instruction 1
    msg.append(ix1_program)
    msg.extend(compact_u16(len(ix1_accounts)))
    msg.extend(ix1_accounts)
    msg.extend(compact_u16(len(ix1_data)))
    msg.extend(ix1_data)

    # Instruction 2
    msg.append(ix2_program)
    msg.extend(compact_u16(len(ix2_accounts)))
    msg.extend(ix2_accounts)
    msg.extend(compact_u16(len(ix2_data)))
    msg.extend(ix2_data)

    message_bytes = bytes(msg)

    # ── Sign ──
    signed = signing_key.sign(message_bytes)
    signature = signed.signature  # 64 bytes

    # ── Serialize full transaction ──
    tx = bytearray()
    tx.extend(compact_u16(1))   # 1 signature
    tx.extend(signature)        # 64-byte Ed25519 signature
    tx.extend(message_bytes)    # the message we signed

    logger.debug(f"TX built: {len(tx)} bytes, sender={b58encode(sender)[:12]}..., "
                 f"recipient={b58encode(recipient)[:12]}..., amount={amount}")

    return bytes(tx)
