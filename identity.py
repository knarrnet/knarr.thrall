"""Thrall Settlement Identity — Delegated Ed25519 keypair.

Gives thrall its own signing key, separate from the node's identity.
Peers can distinguish "thrall signed this" from "the operator signed this."

The 32-byte seed is stored in a keyfile inside the plugin directory.
Config-gated: only initializes if [config.thrall.identity] enabled = true.

v3.3.1: Aligned with v0.35.0 eddsa-jcs-2022 signing format (D-058).
Uses knarr.core.proof.sign_document() with verification_method
did:knarr:{node_id}#thrall-1 to distinguish from node signatures (#key-1).
"""

import logging
import os
import secrets

logger = logging.getLogger("thrall.identity")


def sign_bytes(signing_key, data: bytes) -> bytes:
    """Sign raw bytes with an Ed25519 SigningKey. Returns 64-byte signature."""
    signed = signing_key.sign(data)
    return signed.signature


def verify_bytes(pubkey_hex: str, data: bytes, signature: bytes) -> None:
    """Verify an Ed25519 signature over raw bytes.

    Raises nacl.exceptions.BadSignatureError on failure.
    """
    from nacl.signing import VerifyKey
    vk = VerifyKey(bytes.fromhex(pubkey_hex))
    vk.verify(data, signature)


class ThrallIdentity:
    """Delegated Ed25519 identity for autonomous thrall operations."""

    def __init__(self, plugin_dir: str, config: dict, node_id: str = ""):
        self._plugin_dir = plugin_dir
        self._enabled = config.get("enabled", False)
        self._signing_key = None
        self._public_key_hex = ""
        self._node_id = node_id
        self._verification_method = f"did:knarr:{node_id}#thrall-1" if node_id else ""

        if not self._enabled:
            logger.info("IDENTITY_DISABLED — no delegated keypair")
            return

        keyfile = config.get("keyfile", "thrall_identity.key")
        self._keyfile_path = os.path.join(plugin_dir, keyfile)
        self._load_or_generate()

    def _load_or_generate(self):
        """Load existing keypair or generate a new one."""
        from nacl.signing import SigningKey

        if os.path.exists(self._keyfile_path):
            with open(self._keyfile_path, "rb") as f:
                seed = f.read()
            if len(seed) != 32:
                logger.error(f"IDENTITY_ERROR keyfile corrupt ({len(seed)} bytes, expected 32)")
                return
            self._signing_key = SigningKey(seed)
            logger.info(f"IDENTITY_LOADED public_key={self.public_key_hex[:16]}...")
        else:
            seed = secrets.token_bytes(32)
            with open(self._keyfile_path, "wb") as f:
                f.write(seed)
            self._signing_key = SigningKey(seed)
            logger.info(f"IDENTITY_GENERATED public_key={self.public_key_hex[:16]}...")

    @property
    def enabled(self) -> bool:
        return self._enabled and self._signing_key is not None

    @property
    def public_key_hex(self) -> str:
        if not self._signing_key:
            return ""
        if not self._public_key_hex:
            self._public_key_hex = self._signing_key.verify_key.encode().hex()
        return self._public_key_hex

    @property
    def solana_address(self) -> str:
        """Derive Solana address from thrall's Ed25519 public key.

        Solana uses raw Ed25519 public keys as addresses (base58-encoded).
        This gives thrall its own on-chain identity, separate from the node's.
        """
        if not self._signing_key:
            return ""
        try:
            import base58
            pk_bytes = self._signing_key.verify_key.encode()
            return base58.b58encode(pk_bytes).decode()
        except ImportError:
            # base58 not available — return hex as fallback
            return self.public_key_hex

    @property
    def signing_key(self):
        """Access the raw signing key (for Solana transaction signing)."""
        return self._signing_key

    def sign_document(self, payload: dict) -> dict:
        """Sign a document using eddsa-jcs-2022 (RFC 8785 + Ed25519).

        Uses knarr.core.proof.sign_document() for format compliance with
        v0.35.0 receipt architecture. Returns a NEW dict with embedded
        proof object. Does NOT mutate the input.

        Raises RuntimeError if identity is disabled.
        """
        if not self._signing_key:
            raise RuntimeError("Thrall identity not initialized")

        from knarr.core.proof import sign_document
        return sign_document(
            payload, self._signing_key, self._verification_method)

    def revoke(self):
        """Delete the keyfile — operator escape hatch."""
        if os.path.exists(self._keyfile_path):
            os.remove(self._keyfile_path)
            logger.warning("IDENTITY_REVOKED — keyfile deleted")
        self._signing_key = None
        self._public_key_hex = ""
        self._enabled = False


def verify_thrall_signature(secured_document: dict) -> bool:
    """Verify a thrall-signed document (eddsa-jcs-2022 format).

    Expects a 'proof' object with verificationMethod containing a
    thrall_public_key field in the document body. Falls back to
    extracting thrall_public_key from the document itself.

    Returns True if signature is valid, False otherwise.
    """
    try:
        from nacl.signing import VerifyKey
        from knarr.core.proof import verify_document

        pk_hex = secured_document.get("thrall_public_key")
        if not pk_hex:
            return False

        vk = VerifyKey(bytes.fromhex(pk_hex))
        return verify_document(secured_document, vk)
    except Exception:
        return False
