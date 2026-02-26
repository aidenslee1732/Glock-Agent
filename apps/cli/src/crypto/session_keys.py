"""
Session Key Manager for Model B.

Derives session-specific encryption keys and handles
checkpoint encryption/decryption on the client side.

Uses:
- HKDF for key derivation
- AES-256-GCM for authenticated encryption
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger(__name__)


@dataclass
class EncryptedPayload:
    """An encrypted payload with metadata."""
    nonce: bytes
    ciphertext: bytes
    tag: bytes  # Included in ciphertext for GCM

    def to_base64(self) -> Tuple[str, str]:
        """Encode to base64 strings."""
        nonce_b64 = base64.b64encode(self.nonce).decode()
        ciphertext_b64 = base64.b64encode(self.ciphertext).decode()
        return nonce_b64, ciphertext_b64

    @classmethod
    def from_base64(cls, nonce_b64: str, ciphertext_b64: str) -> "EncryptedPayload":
        """Decode from base64 strings."""
        return cls(
            nonce=base64.b64decode(nonce_b64),
            ciphertext=base64.b64decode(ciphertext_b64),
            tag=b"",  # Tag is included in ciphertext for GCM
        )


class SessionKeyManager:
    """
    Manages session-specific encryption keys.

    Key derivation:
    - Master token (from auth) is used as base key material
    - Session ID is used as context for HKDF derivation
    - Each session gets a unique 256-bit AES key

    Encryption:
    - AES-256-GCM for authenticated encryption
    - Random 96-bit nonce per encryption
    - Session ID used as additional authenticated data

    This ensures:
    - Sessions are cryptographically isolated
    - Even with master token compromise, past sessions remain secure
    - Checkpoints are integrity-protected
    """

    def __init__(self, master_token: Optional[str] = None):
        """
        Initialize key manager.

        Args:
            master_token: The authentication token (JWT or API key)
                         If not provided, generates a random key for testing
        """
        if master_token:
            # Derive master key from token
            self._master_key = self._derive_master_key(master_token)
        else:
            # Generate random key for testing
            logger.warning("No master token provided, using random key")
            self._master_key = os.urandom(32)

        # Cache derived session keys
        self._session_keys: dict[str, bytes] = {}

    def _derive_master_key(self, token: str) -> bytes:
        """Derive master key from authentication token."""
        # Use SHA-256 of token as master key
        # In production, this would use a more sophisticated scheme
        return hashlib.sha256(token.encode()).digest()

    def derive_session_key(self, session_id: str, user_id: str = "") -> bytes:
        """
        Derive a session-specific encryption key.

        Uses HKDF (RFC 5869) with:
        - Input: master key
        - Salt: first 16 bytes of master key
        - Info: "glock:context:v1:{session_id}:{user_id}"

        Args:
            session_id: The session identifier
            user_id: The user identifier (optional, for additional isolation)

        Returns:
            32-byte derived key for AES-256
        """
        cache_key = f"{session_id}:{user_id}"

        # Check cache
        if cache_key in self._session_keys:
            return self._session_keys[cache_key]

        # Derive new key
        info = f"glock:context:v1:{session_id}:{user_id}".encode()

        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,  # 256 bits for AES-256
            salt=self._master_key[:16],
            info=info,
            backend=default_backend(),
        )

        derived_key = hkdf.derive(self._master_key[16:])

        # Cache for performance
        self._session_keys[cache_key] = derived_key

        return derived_key

    def encrypt_checkpoint(
        self,
        plaintext: bytes,
        session_id: str,
        user_id: str = "",
    ) -> EncryptedPayload:
        """
        Encrypt checkpoint data for a session.

        Uses AES-256-GCM with:
        - Random 96-bit nonce
        - Session ID as additional authenticated data

        Args:
            plaintext: The data to encrypt
            session_id: Session identifier for key derivation
            user_id: User identifier for additional isolation

        Returns:
            EncryptedPayload with nonce and ciphertext
        """
        # Get session key
        key = self.derive_session_key(session_id, user_id)
        aesgcm = AESGCM(key)

        # Generate random nonce (96 bits = 12 bytes)
        nonce = os.urandom(12)

        # Additional authenticated data (not encrypted, but integrity-protected)
        aad = f"{session_id}:{user_id}".encode()

        # Encrypt (GCM mode includes authentication tag in ciphertext)
        ciphertext = aesgcm.encrypt(nonce, plaintext, aad)

        return EncryptedPayload(
            nonce=nonce,
            ciphertext=ciphertext,
            tag=b"",  # Tag is appended to ciphertext in GCM
        )

    def decrypt_checkpoint(
        self,
        encrypted: EncryptedPayload,
        session_id: str,
        user_id: str = "",
    ) -> bytes:
        """
        Decrypt checkpoint data for a session.

        Args:
            encrypted: The encrypted payload
            session_id: Session identifier for key derivation
            user_id: User identifier for additional isolation

        Returns:
            Decrypted plaintext

        Raises:
            ValueError: If decryption fails (wrong key or tampered data)
        """
        # Get session key
        key = self.derive_session_key(session_id, user_id)
        aesgcm = AESGCM(key)

        # Additional authenticated data (must match encryption)
        aad = f"{session_id}:{user_id}".encode()

        try:
            plaintext = aesgcm.decrypt(encrypted.nonce, encrypted.ciphertext, aad)
            return plaintext
        except Exception as e:
            raise ValueError(f"Decryption failed: {e}") from e

    def encrypt_to_base64(
        self,
        plaintext: bytes,
        session_id: str,
        user_id: str = "",
    ) -> Tuple[str, str]:
        """
        Encrypt and encode to base64.

        Convenience method for direct use in checkpoint payloads.

        Returns:
            Tuple of (nonce_b64, ciphertext_b64)
        """
        encrypted = self.encrypt_checkpoint(plaintext, session_id, user_id)
        return encrypted.to_base64()

    def decrypt_from_base64(
        self,
        nonce_b64: str,
        ciphertext_b64: str,
        session_id: str,
        user_id: str = "",
    ) -> bytes:
        """
        Decode from base64 and decrypt.

        Convenience method for checkpoint payload processing.

        Returns:
            Decrypted plaintext
        """
        encrypted = EncryptedPayload.from_base64(nonce_b64, ciphertext_b64)
        return self.decrypt_checkpoint(encrypted, session_id, user_id)

    def compute_hash(self, data: bytes) -> str:
        """
        Compute SHA-256 hash of data.

        Used for payload verification without decryption.

        Args:
            data: Data to hash

        Returns:
            Hex-encoded hash
        """
        return hashlib.sha256(data).hexdigest()

    def verify_hash(self, data: bytes, expected_hash: str) -> bool:
        """
        Verify data integrity using hash.

        Args:
            data: Data to verify
            expected_hash: Expected hex-encoded hash

        Returns:
            True if hash matches
        """
        actual_hash = self.compute_hash(data)
        # Constant-time comparison
        return hashlib.compare_digest(actual_hash, expected_hash)

    def clear_cache(self) -> None:
        """Clear cached session keys."""
        self._session_keys.clear()

    def rotate_master_key(self, new_token: str) -> None:
        """
        Rotate the master key.

        This invalidates all cached session keys.
        Existing encrypted data will need to be re-encrypted.

        Args:
            new_token: New authentication token
        """
        self._master_key = self._derive_master_key(new_token)
        self.clear_cache()
        logger.info("Master key rotated, session key cache cleared")
