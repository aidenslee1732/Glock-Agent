"""Plan signing - Ed25519 signatures for compiled plans."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger(__name__)


@dataclass
class SignatureInfo:
    """Signature information."""
    signature: str  # Base64-encoded signature
    signature_alg: str  # "ed25519"
    kid: str  # Key ID
    payload_hash: str  # SHA256 hash of canonicalized payload
    signed_at: datetime


@dataclass
class KeyInfo:
    """Signing key information."""
    kid: str
    created_at: datetime
    expires_at: Optional[datetime] = None


class PlanSigner:
    """Signs and verifies compiled plans using Ed25519.

    Plan signing ensures:
    1. Plans are tamper-proof
    2. Plans come from authorized planner service
    3. Clients can verify plans without server round-trip
    """

    def __init__(
        self,
        private_key: Optional[Ed25519PrivateKey] = None,
        public_keys: Optional[dict[str, Ed25519PublicKey]] = None,
        current_kid: str = "key_001",
    ):
        self._private_key = private_key
        self._public_keys = public_keys or {}
        self._current_kid = current_kid

        if private_key:
            # Derive public key from private
            public_key = private_key.public_key()
            self._public_keys[current_kid] = public_key

    @classmethod
    def from_env(cls) -> PlanSigner:
        """Create signer from environment variables."""
        # Load private key (server-side only)
        private_key_b64 = os.environ.get("PLAN_SIGNING_PRIVATE_KEY")
        private_key = None
        if private_key_b64:
            private_key_bytes = base64.b64decode(private_key_b64)
            private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)

        # Load public keys (for verification)
        public_keys = {}
        for key, value in os.environ.items():
            if key.startswith("PLAN_SIGNING_PUBLIC_KEY_"):
                kid = key.replace("PLAN_SIGNING_PUBLIC_KEY_", "").lower()
                public_key_bytes = base64.b64decode(value)
                public_keys[kid] = Ed25519PublicKey.from_public_bytes(public_key_bytes)

        current_kid = os.environ.get("PLAN_SIGNING_KEY_ID", "key_001")

        return cls(
            private_key=private_key,
            public_keys=public_keys,
            current_kid=current_kid,
        )

    @classmethod
    def generate_keypair(cls) -> tuple[str, str]:
        """Generate new Ed25519 keypair.

        Returns:
            Tuple of (private_key_b64, public_key_b64)
        """
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        return (
            base64.b64encode(private_bytes).decode(),
            base64.b64encode(public_bytes).decode(),
        )

    def sign(self, plan_payload: dict[str, Any]) -> SignatureInfo:
        """Sign a plan payload.

        Args:
            plan_payload: Plan data to sign

        Returns:
            SignatureInfo with signature and metadata

        Raises:
            RuntimeError: If no private key configured
        """
        if not self._private_key:
            raise RuntimeError("No private key configured for signing")

        # Canonicalize payload (sorted keys, no whitespace)
        canonical = self._canonicalize(plan_payload)

        # Compute hash
        payload_hash = self._hash_payload(canonical)

        # Sign the canonical bytes
        signature_bytes = self._private_key.sign(canonical.encode("utf-8"))
        signature_b64 = base64.b64encode(signature_bytes).decode()

        return SignatureInfo(
            signature=signature_b64,
            signature_alg="ed25519",
            kid=self._current_kid,
            payload_hash=payload_hash,
            signed_at=datetime.utcnow(),
        )

    def verify(
        self,
        plan_payload: dict[str, Any],
        signature: str,
        kid: str,
        payload_hash: Optional[str] = None,
    ) -> bool:
        """Verify a plan signature.

        Args:
            plan_payload: Plan data that was signed
            signature: Base64-encoded signature
            kid: Key ID used for signing
            payload_hash: Optional hash to verify

        Returns:
            True if signature is valid
        """
        # Get public key for kid
        public_key = self._public_keys.get(kid)
        if not public_key:
            logger.warning(f"Unknown key ID: {kid}")
            return False

        # Canonicalize payload
        canonical = self._canonicalize(plan_payload)

        # Verify hash if provided
        if payload_hash:
            computed_hash = self._hash_payload(canonical)
            if computed_hash != payload_hash:
                logger.warning("Payload hash mismatch")
                return False

        # Verify signature
        try:
            signature_bytes = base64.b64decode(signature)
            public_key.verify(signature_bytes, canonical.encode("utf-8"))
            return True
        except InvalidSignature:
            logger.warning("Invalid signature")
            return False
        except Exception as e:
            logger.error(f"Signature verification error: {e}")
            return False

    def _canonicalize(self, data: dict[str, Any]) -> str:
        """Canonicalize dict to deterministic JSON string.

        Keys are sorted, no whitespace, deterministic output.
        """
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def _hash_payload(self, canonical: str) -> str:
        """Compute SHA256 hash of canonical payload."""
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def add_public_key(self, kid: str, public_key_b64: str) -> None:
        """Add a public key for verification.

        Args:
            kid: Key ID
            public_key_b64: Base64-encoded public key
        """
        public_key_bytes = base64.b64decode(public_key_b64)
        self._public_keys[kid] = Ed25519PublicKey.from_public_bytes(public_key_bytes)

    def get_public_key_b64(self, kid: Optional[str] = None) -> Optional[str]:
        """Get base64-encoded public key.

        Args:
            kid: Key ID (defaults to current)

        Returns:
            Base64-encoded public key or None
        """
        kid = kid or self._current_kid
        public_key = self._public_keys.get(kid)
        if not public_key:
            return None

        public_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(public_bytes).decode()


class PlanVerifier:
    """Client-side plan verifier (no private key needed)."""

    def __init__(self, public_keys: dict[str, str]):
        """Initialize with public keys.

        Args:
            public_keys: Dict of kid → base64-encoded public key
        """
        self._public_keys: dict[str, Ed25519PublicKey] = {}
        for kid, key_b64 in public_keys.items():
            key_bytes = base64.b64decode(key_b64)
            self._public_keys[kid] = Ed25519PublicKey.from_public_bytes(key_bytes)

    def verify(
        self,
        plan_payload: dict[str, Any],
        signature: str,
        kid: str,
    ) -> bool:
        """Verify plan signature.

        Args:
            plan_payload: Plan data
            signature: Base64-encoded signature
            kid: Key ID

        Returns:
            True if valid
        """
        public_key = self._public_keys.get(kid)
        if not public_key:
            return False

        # Canonicalize
        canonical = json.dumps(plan_payload, sort_keys=True, separators=(",", ":"))

        try:
            signature_bytes = base64.b64decode(signature)
            public_key.verify(signature_bytes, canonical.encode("utf-8"))
            return True
        except Exception:
            return False
