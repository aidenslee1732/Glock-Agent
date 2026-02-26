"""Client-side plan signature verification.

The client verifies that plans received from the server are legitimately
signed by the Glock planner service.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PlanVerificationError(Exception):
    """Error verifying plan signature."""
    pass


@dataclass
class PublicKeyInfo:
    """Public key information for verification."""

    kid: str  # Key ID
    key_bytes: bytes  # Raw public key bytes
    algorithm: str  # Signing algorithm
    created_at: datetime
    expires_at: Optional[datetime] = None


class PlanVerifier:
    """Verifies compiled plan signatures on the client.

    The verifier uses the server's public key to verify that plans
    are legitimately signed and have not been tampered with.
    """

    def __init__(self):
        self._public_keys: dict[str, PublicKeyInfo] = {}
        self._default_kid: Optional[str] = None

    def add_public_key(
        self,
        kid: str,
        public_key_pem: str,
        algorithm: str = "ed25519",
        set_default: bool = False
    ):
        """Add a public key for verification.

        Args:
            kid: Key ID
            public_key_pem: PEM-encoded public key
            algorithm: Signing algorithm
            set_default: Whether to set as default key
        """
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key

            public_key = load_pem_public_key(public_key_pem.encode())

            # Extract raw bytes for Ed25519
            if algorithm == "ed25519":
                from cryptography.hazmat.primitives.serialization import (
                    Encoding,
                    PublicFormat,
                )
                key_bytes = public_key.public_bytes(
                    encoding=Encoding.Raw,
                    format=PublicFormat.Raw
                )
            else:
                key_bytes = public_key_pem.encode()

            key_info = PublicKeyInfo(
                kid=kid,
                key_bytes=key_bytes,
                algorithm=algorithm,
                created_at=datetime.now(timezone.utc)
            )

            self._public_keys[kid] = key_info

            if set_default or not self._default_kid:
                self._default_kid = kid

            logger.info(f"Added public key: {kid}")

        except Exception as e:
            raise PlanVerificationError(f"Failed to load public key: {e}") from e

    def add_public_key_raw(
        self,
        kid: str,
        key_bytes: bytes,
        algorithm: str = "ed25519",
        set_default: bool = False
    ):
        """Add a raw public key for verification."""
        key_info = PublicKeyInfo(
            kid=kid,
            key_bytes=key_bytes,
            algorithm=algorithm,
            created_at=datetime.now(timezone.utc)
        )

        self._public_keys[kid] = key_info

        if set_default or not self._default_kid:
            self._default_kid = kid

    def verify_plan(self, plan_data: dict[str, Any]) -> bool:
        """Verify a compiled plan's signature.

        Args:
            plan_data: The full plan payload including signature

        Returns:
            True if signature is valid

        Raises:
            PlanVerificationError: If verification fails
        """
        # Extract signature info
        signature_b64 = plan_data.get("signature")
        if not signature_b64:
            raise PlanVerificationError("Plan has no signature")

        algorithm = plan_data.get("signature_alg", "ed25519")
        kid = plan_data.get("kid")
        payload_hash = plan_data.get("payload_hash")

        # Get the appropriate public key
        if kid:
            key_info = self._public_keys.get(kid)
            if not key_info:
                raise PlanVerificationError(f"Unknown key ID: {kid}")
        elif self._default_kid:
            key_info = self._public_keys.get(self._default_kid)
        else:
            raise PlanVerificationError("No public keys available for verification")

        # Verify algorithm matches
        if key_info.algorithm != algorithm:
            raise PlanVerificationError(
                f"Algorithm mismatch: expected {key_info.algorithm}, got {algorithm}"
            )

        # Check key expiration
        if key_info.expires_at and datetime.now(timezone.utc) > key_info.expires_at:
            raise PlanVerificationError(f"Key {kid} has expired")

        # Build the canonical payload for verification
        canonical_payload = self._canonicalize_payload(plan_data)

        # Verify payload hash if provided
        if payload_hash:
            computed_hash = self._compute_payload_hash(canonical_payload)
            if payload_hash != computed_hash:
                raise PlanVerificationError("Payload hash mismatch")

        # Decode signature
        try:
            signature = base64.b64decode(signature_b64)
        except Exception as e:
            raise PlanVerificationError(f"Invalid signature encoding: {e}") from e

        # Verify signature based on algorithm
        if algorithm == "ed25519":
            return self._verify_ed25519(
                key_info.key_bytes,
                canonical_payload.encode("utf-8"),
                signature
            )
        else:
            raise PlanVerificationError(f"Unsupported algorithm: {algorithm}")

    def _canonicalize_payload(self, plan_data: dict[str, Any]) -> str:
        """Create canonical JSON representation for signing.

        The canonical form:
        - Sorted keys (recursive)
        - No whitespace
        - Excludes signature fields
        """
        # Fields to exclude from signature verification
        excluded_fields = {"signature", "signature_alg", "kid", "payload_hash"}

        def sort_dict(d: dict) -> dict:
            """Recursively sort dictionary keys."""
            result = {}
            for key in sorted(d.keys()):
                if key in excluded_fields:
                    continue
                value = d[key]
                if isinstance(value, dict):
                    result[key] = sort_dict(value)
                elif isinstance(value, list):
                    result[key] = [
                        sort_dict(item) if isinstance(item, dict) else item
                        for item in value
                    ]
                else:
                    result[key] = value
            return result

        sorted_data = sort_dict(plan_data)
        return json.dumps(sorted_data, separators=(",", ":"), sort_keys=True)

    def _compute_payload_hash(self, canonical_payload: str) -> str:
        """Compute SHA-256 hash of canonical payload."""
        hash_bytes = hashlib.sha256(canonical_payload.encode("utf-8")).digest()
        return f"sha256:{base64.b64encode(hash_bytes).decode()}"

    def _verify_ed25519(
        self,
        public_key_bytes: bytes,
        message: bytes,
        signature: bytes
    ) -> bool:
        """Verify Ed25519 signature."""
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
            public_key.verify(signature, message)
            return True

        except Exception as e:
            raise PlanVerificationError(f"Signature verification failed: {e}") from e

    def verify_plan_expiration(self, plan_data: dict[str, Any]) -> bool:
        """Check if plan has expired."""
        expires_at_str = plan_data.get("expires_at")
        if not expires_at_str:
            return True  # No expiration

        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) < expires_at
        except ValueError:
            return False

    def verify_plan_session(
        self,
        plan_data: dict[str, Any],
        expected_session_id: str
    ) -> bool:
        """Verify plan belongs to the expected session."""
        plan_session_id = plan_data.get("session_id")
        return plan_session_id == expected_session_id

    def full_verification(
        self,
        plan_data: dict[str, Any],
        session_id: str
    ) -> dict[str, bool]:
        """Run all verification checks.

        Returns dict of check names to pass/fail status.
        """
        results = {
            "signature_valid": False,
            "not_expired": False,
            "session_matches": False
        }

        try:
            results["signature_valid"] = self.verify_plan(plan_data)
        except PlanVerificationError as e:
            logger.warning(f"Signature verification failed: {e}")

        results["not_expired"] = self.verify_plan_expiration(plan_data)
        results["session_matches"] = self.verify_plan_session(plan_data, session_id)

        return results
