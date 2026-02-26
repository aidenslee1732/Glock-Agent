"""
Context Checkpoint Store for Model B.

Handles encrypted context checkpoint storage with:
- Per-session derived encryption keys (HKDF)
- Delta chain management
- Checkpoint lifecycle (TTL, cleanup)
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

from packages.shared_protocol.types import generate_checkpoint_id
from .postgres import PostgresClient
from .redis import RedisClient

logger = logging.getLogger(__name__)


# Configuration
CHECKPOINT_TTL_HOURS = 24
MAX_DELTAS_BEFORE_FULL = 5
MAX_DELTA_SIZE_BYTES = 100000  # 100KB
MAX_CHECKPOINTS_PER_SESSION = 20


@dataclass
class Checkpoint:
    """A context checkpoint."""
    id: str
    session_id: str
    user_id: str
    parent_id: Optional[str]
    payload: bytes  # Decrypted payload
    payload_hash: str
    token_count: int
    turn_count: int
    is_full: bool
    created_at: datetime
    expires_at: datetime


@dataclass
class CheckpointChainInfo:
    """Information about a checkpoint chain."""
    last_checkpoint_id: Optional[str]
    last_full_id: Optional[str]
    delta_count: int
    total_tokens: int


class ContextCheckpointStore:
    """
    Encrypted context checkpoint storage.

    Features:
    - Per-session encryption keys derived via HKDF
    - AES-256-GCM encryption
    - Delta chain management (full snapshots + deltas)
    - Automatic cleanup of expired checkpoints
    """

    def __init__(
        self,
        postgres: PostgresClient,
        redis: RedisClient,
        master_key: Optional[bytes] = None,
    ):
        self.postgres = postgres
        self.redis = redis

        # Master key for deriving session keys
        if master_key:
            self._master_key = master_key
        else:
            # Load from environment or generate for testing
            key_hex = os.environ.get("CONTEXT_MASTER_KEY", "")
            if key_hex:
                self._master_key = bytes.fromhex(key_hex)
            else:
                logger.warning("No CONTEXT_MASTER_KEY set, using random key (not for production)")
                self._master_key = os.urandom(32)

    def derive_session_key(self, session_id: str, user_id: str) -> bytes:
        """
        Derive a session-specific encryption key using HKDF.

        Each session gets a unique key derived from:
        - Master key
        - Session ID
        - User ID

        This ensures:
        - Session isolation (sessions can't decrypt each other's data)
        - Key rotation (new master key = new derived keys)
        """
        info = f"glock:context:v1:{session_id}:{user_id}".encode()

        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,  # 256-bit key for AES-256
            salt=self._master_key[:16],
            info=info,
            backend=default_backend(),
        )
        return hkdf.derive(self._master_key[16:])

    def encrypt_payload(
        self,
        payload: bytes,
        session_id: str,
        user_id: str,
    ) -> tuple[bytes, bytes]:
        """
        Encrypt a checkpoint payload.

        Returns:
            Tuple of (nonce, ciphertext)
        """
        key = self.derive_session_key(session_id, user_id)
        aesgcm = AESGCM(key)

        # Generate random 96-bit nonce
        nonce = os.urandom(12)

        # Additional authenticated data for integrity
        aad = f"{session_id}:{user_id}".encode()

        ciphertext = aesgcm.encrypt(nonce, payload, aad)
        return nonce, ciphertext

    def decrypt_payload(
        self,
        nonce: bytes,
        ciphertext: bytes,
        session_id: str,
        user_id: str,
    ) -> bytes:
        """
        Decrypt a checkpoint payload.

        Raises:
            ValueError: If decryption fails (wrong key or tampered data)
        """
        key = self.derive_session_key(session_id, user_id)
        aesgcm = AESGCM(key)

        # Additional authenticated data must match encryption
        aad = f"{session_id}:{user_id}".encode()

        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
            return plaintext
        except Exception as e:
            raise ValueError(f"Decryption failed: {e}") from e

    async def store_checkpoint(
        self,
        session_id: str,
        user_id: str,
        payload: bytes,
        parent_id: Optional[str] = None,
        token_count: int = 0,
        turn_count: int = 0,
        force_full: bool = False,
    ) -> str:
        """
        Store a context checkpoint.

        Automatically decides whether to create a full snapshot or delta
        based on:
        - Number of deltas since last full
        - Size of the delta
        - force_full parameter

        Returns:
            The checkpoint ID
        """
        checkpoint_id = generate_checkpoint_id()

        # Get chain info to decide full vs delta
        chain_info = await self._get_chain_info(session_id)

        # Decide if we should create a full snapshot
        is_full = (
            force_full or
            chain_info.last_full_id is None or
            chain_info.delta_count >= MAX_DELTAS_BEFORE_FULL or
            len(payload) > MAX_DELTA_SIZE_BYTES
        )

        # If creating full snapshot, clear parent reference
        if is_full:
            parent_id = None

        # Compute payload hash
        payload_hash = hashlib.sha256(payload).hexdigest()

        # Encrypt payload
        nonce, ciphertext = self.encrypt_payload(payload, session_id, user_id)

        # Encode for storage
        nonce_b64 = base64.b64encode(nonce).decode()
        ciphertext_b64 = base64.b64encode(ciphertext).decode()

        # Calculate expiration
        expires_at = datetime.utcnow() + timedelta(hours=CHECKPOINT_TTL_HOURS)

        # Store in database
        await self.postgres.pool.execute(
            """
            INSERT INTO context_checkpoints
            (id, session_id, user_id, parent_id, enc_alg, nonce_base64, ciphertext_base64,
             payload_hash, token_count, turn_count, is_full, created_at, expires_at)
            VALUES ($1, $2, $3, $4, 'aes-256-gcm', $5, $6, $7, $8, $9, $10, NOW(), $11)
            """,
            checkpoint_id,
            session_id,
            user_id,
            parent_id,
            nonce_b64,
            ciphertext_b64,
            payload_hash,
            token_count,
            turn_count,
            is_full,
            expires_at,
        )

        # Update session state in Redis
        await self.redis.hset(
            f"sess:{session_id}:state",
            mapping={
                "last_context_ref": checkpoint_id,
                "turn_count": str(turn_count),
            },
        )

        # Prune old checkpoints if needed
        await self._prune_old_checkpoints(session_id, user_id)

        logger.info(
            f"Stored checkpoint {checkpoint_id} for session {session_id} "
            f"(is_full={is_full}, tokens={token_count}, turns={turn_count})"
        )

        return checkpoint_id

    async def load_checkpoint(
        self,
        checkpoint_id: str,
        session_id: str,
        user_id: str,
    ) -> Optional[Checkpoint]:
        """
        Load and decrypt a checkpoint.

        Returns:
            The decrypted Checkpoint, or None if not found
        """
        row = await self.postgres.pool.fetchrow(
            """
            SELECT id, session_id, user_id, parent_id, nonce_base64, ciphertext_base64,
                   payload_hash, token_count, turn_count, is_full, created_at, expires_at
            FROM context_checkpoints
            WHERE id = $1 AND session_id = $2 AND user_id = $3
            """,
            checkpoint_id,
            session_id,
            user_id,
        )

        if not row:
            return None

        # Decode encrypted data
        nonce = base64.b64decode(row["nonce_base64"])
        ciphertext = base64.b64decode(row["ciphertext_base64"])

        # Decrypt
        try:
            payload = self.decrypt_payload(nonce, ciphertext, session_id, user_id)
        except ValueError as e:
            logger.error(f"Failed to decrypt checkpoint {checkpoint_id}: {e}")
            return None

        return Checkpoint(
            id=row["id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            parent_id=row["parent_id"],
            payload=payload,
            payload_hash=row["payload_hash"],
            token_count=row["token_count"],
            turn_count=row["turn_count"],
            is_full=row["is_full"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    async def get_checkpoint_chain(
        self,
        checkpoint_id: str,
        session_id: str,
        user_id: str,
    ) -> list[Checkpoint]:
        """
        Get the full checkpoint chain from a checkpoint back to its root full snapshot.

        Returns checkpoints in order from oldest (full snapshot) to newest (target).
        """
        chain: list[Checkpoint] = []
        current_id: Optional[str] = checkpoint_id

        # Walk back through parent chain
        while current_id:
            checkpoint = await self.load_checkpoint(current_id, session_id, user_id)
            if not checkpoint:
                break

            chain.append(checkpoint)

            if checkpoint.is_full:
                # Reached the root full snapshot
                break

            current_id = checkpoint.parent_id

        # Reverse to get oldest-first order
        chain.reverse()
        return chain

    async def _get_chain_info(self, session_id: str) -> CheckpointChainInfo:
        """Get information about the current checkpoint chain."""
        # Get latest checkpoint
        latest = await self.postgres.pool.fetchrow(
            """
            SELECT id, is_full, token_count
            FROM context_checkpoints
            WHERE session_id = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            session_id,
        )

        if not latest:
            return CheckpointChainInfo(
                last_checkpoint_id=None,
                last_full_id=None,
                delta_count=0,
                total_tokens=0,
            )

        # Get last full snapshot
        last_full = await self.postgres.pool.fetchrow(
            """
            SELECT id
            FROM context_checkpoints
            WHERE session_id = $1 AND is_full = true
            ORDER BY created_at DESC
            LIMIT 1
            """,
            session_id,
        )

        # Count deltas since last full
        if last_full:
            delta_count_row = await self.postgres.pool.fetchrow(
                """
                SELECT COUNT(*) as count
                FROM context_checkpoints
                WHERE session_id = $1 AND is_full = false
                  AND created_at > (
                      SELECT created_at FROM context_checkpoints WHERE id = $2
                  )
                """,
                session_id,
                last_full["id"],
            )
            delta_count = delta_count_row["count"] if delta_count_row else 0
        else:
            delta_count = 0

        return CheckpointChainInfo(
            last_checkpoint_id=latest["id"],
            last_full_id=last_full["id"] if last_full else None,
            delta_count=delta_count,
            total_tokens=latest["token_count"],
        )

    async def _prune_old_checkpoints(self, session_id: str, user_id: str) -> None:
        """Prune old checkpoints to stay within limits."""
        # Count checkpoints
        count_row = await self.postgres.pool.fetchrow(
            """
            SELECT COUNT(*) as count
            FROM context_checkpoints
            WHERE session_id = $1 AND user_id = $2
            """,
            session_id,
            user_id,
        )

        if not count_row or count_row["count"] <= MAX_CHECKPOINTS_PER_SESSION:
            return

        # Delete oldest checkpoints beyond limit
        # But always keep at least the latest full snapshot and its dependents
        to_delete = count_row["count"] - MAX_CHECKPOINTS_PER_SESSION

        # Get IDs of checkpoints to delete (oldest first, excluding latest full and dependents)
        rows = await self.postgres.pool.fetch(
            """
            WITH latest_full AS (
                SELECT id, created_at
                FROM context_checkpoints
                WHERE session_id = $1 AND user_id = $2 AND is_full = true
                ORDER BY created_at DESC
                LIMIT 1
            )
            SELECT cp.id
            FROM context_checkpoints cp
            LEFT JOIN latest_full lf ON 1=1
            WHERE cp.session_id = $1 AND cp.user_id = $2
              AND (lf.id IS NULL OR cp.created_at < lf.created_at)
            ORDER BY cp.created_at ASC
            LIMIT $3
            """,
            session_id,
            user_id,
            to_delete,
        )

        if rows:
            ids_to_delete = [row["id"] for row in rows]
            await self.postgres.pool.execute(
                """
                DELETE FROM context_checkpoints
                WHERE id = ANY($1)
                """,
                ids_to_delete,
            )
            logger.info(f"Pruned {len(ids_to_delete)} old checkpoints for session {session_id}")

    async def cleanup_expired(self) -> int:
        """
        Delete expired checkpoints across all sessions.

        Returns:
            Number of deleted checkpoints
        """
        result = await self.postgres.pool.execute(
            """
            DELETE FROM context_checkpoints
            WHERE expires_at < NOW()
            """
        )

        # Parse result to get count
        deleted = 0
        if result:
            # asyncpg returns "DELETE N" where N is count
            parts = result.split()
            if len(parts) >= 2:
                deleted = int(parts[1])

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired checkpoints")

        return deleted

    async def delete_session_checkpoints(self, session_id: str, user_id: str) -> None:
        """Delete all checkpoints for a session."""
        await self.postgres.pool.execute(
            """
            DELETE FROM context_checkpoints
            WHERE session_id = $1 AND user_id = $2
            """,
            session_id,
            user_id,
        )
        logger.info(f"Deleted all checkpoints for session {session_id}")


# Singleton instance
_checkpoint_store: Optional[ContextCheckpointStore] = None


async def get_checkpoint_store(
    postgres: PostgresClient,
    redis: RedisClient,
) -> ContextCheckpointStore:
    """Get or create checkpoint store singleton."""
    global _checkpoint_store
    if _checkpoint_store is None:
        _checkpoint_store = ContextCheckpointStore(postgres, redis)
    return _checkpoint_store
