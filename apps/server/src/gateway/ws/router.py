"""Session routing - Redis-backed routing for client connections.

Model B Note: Runtime routing methods are deprecated.
In Model B, the client orchestrates directly and there are no runtime processes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from apps.server.src.storage.redis import RedisClient

logger = logging.getLogger(__name__)


@dataclass
class SessionRoute:
    """Complete routing information for a session."""
    session_id: str
    client_gateway_id: Optional[str] = None
    client_conn_id: Optional[str] = None
    client_status: str = "disconnected"
    runtime_id: Optional[str] = None
    runtime_conn_id: Optional[str] = None
    runtime_status: str = "none"
    host_id: Optional[str] = None
    bind_token: Optional[str] = None


@dataclass
class Allocation:
    """Runtime allocation result."""
    session_id: str
    runtime_id: str
    host_id: str
    bind_token: str
    allocated_at: float


class SessionRouter:
    """Redis-backed session routing.

    Manages the mapping between sessions, clients, and runtimes.
    Supports the gateway relay pattern where:
    - Client connects to gateway
    - Runtime connects outbound to gateway
    - Gateway relays messages between them
    """

    def __init__(self, redis: RedisClient, gateway_id: str):
        self.redis = redis
        self.gateway_id = gateway_id

    async def attach_client(
        self,
        session_id: str,
        conn_id: str,
    ) -> None:
        """Attach client connection to session.

        Args:
            session_id: Session identifier
            conn_id: WebSocket connection ID
        """
        await self.redis.set_client_route(
            session_id,
            gateway_id=self.gateway_id,
            conn_id=conn_id,
            connected_at=time.time(),
        )
        logger.info(f"Client attached: session={session_id}, conn={conn_id}")

    async def detach_client(self, session_id: str) -> None:
        """Detach client from session (on disconnect).

        Args:
            session_id: Session identifier
        """
        await self.redis.clear_client_route(session_id)
        logger.info(f"Client detached: session={session_id}")

    # =========================================================================
    # DEPRECATED: Runtime methods (not used in Model B)
    # =========================================================================

    async def attach_runtime(
        self,
        session_id: str,
        runtime_id: str,
        conn_id: str,
    ) -> None:
        """DEPRECATED: Model B does not use runtime processes."""
        logger.warning("attach_runtime called but is deprecated in Model B")

    async def detach_runtime(self, session_id: str) -> None:
        """DEPRECATED: Model B does not use runtime processes."""
        logger.warning("detach_runtime called but is deprecated in Model B")

    async def get_route(self, session_id: str) -> SessionRoute:
        """Get complete routing information for session.

        Args:
            session_id: Session identifier

        Returns:
            SessionRoute with all routing info
        """
        client = await self.redis.get_client_route(session_id)
        runtime = await self.redis.get_runtime_route(session_id)
        allocation = await self.redis.get_allocation(session_id)

        return SessionRoute(
            session_id=session_id,
            client_gateway_id=client.get("gateway_id"),
            client_conn_id=client.get("conn_id"),
            client_status=client.get("status", "disconnected"),
            runtime_id=runtime.get("runtime_id"),
            runtime_conn_id=runtime.get("conn_id"),
            runtime_status=runtime.get("status", "none"),
            host_id=allocation.get("host_id") if allocation else None,
            bind_token=allocation.get("bind_token") if allocation else None,
        )

    async def is_client_connected(self, session_id: str) -> bool:
        """Check if client is connected.

        Args:
            session_id: Session identifier

        Returns:
            True if client is connected
        """
        client = await self.redis.get_client_route(session_id)
        return client.get("status") == "connected"

    async def is_runtime_connected(self, session_id: str) -> bool:
        """DEPRECATED: Model B does not use runtime processes.

        Always returns False in Model B.
        """
        return False

    async def get_client_gateway(self, session_id: str) -> Optional[str]:
        """Get gateway ID handling client connection.

        Args:
            session_id: Session identifier

        Returns:
            Gateway ID or None if not connected
        """
        client = await self.redis.get_client_route(session_id)
        if client.get("status") == "connected":
            return client.get("gateway_id")
        return None

    async def store_allocation(
        self,
        session_id: str,
        runtime_id: str,
        host_id: str,
        bind_token: str,
    ) -> Allocation:
        """Store runtime allocation for session.

        Args:
            session_id: Session identifier
            runtime_id: Allocated runtime ID
            host_id: Host running the runtime
            bind_token: One-time token for runtime binding

        Returns:
            Allocation details
        """
        allocated_at = time.time()
        await self.redis.set_allocation(
            session_id,
            host_id=host_id,
            runtime_id=runtime_id,
            bind_token=bind_token,
            allocated_at=allocated_at,
        )
        logger.info(
            f"Allocation stored: session={session_id}, "
            f"runtime={runtime_id}, host={host_id}"
        )
        return Allocation(
            session_id=session_id,
            runtime_id=runtime_id,
            host_id=host_id,
            bind_token=bind_token,
            allocated_at=allocated_at,
        )

    async def verify_bind_token(
        self,
        session_id: str,
        bind_token: str,
    ) -> Optional[Allocation]:
        """Verify bind token for runtime binding.

        Args:
            session_id: Session identifier
            bind_token: Token provided by runtime

        Returns:
            Allocation if token matches, None otherwise
        """
        allocation = await self.redis.get_allocation(session_id)
        if not allocation:
            logger.warning(f"No allocation for session: {session_id}")
            return None

        if allocation.get("bind_token") != bind_token:
            logger.warning(f"Invalid bind token for session: {session_id}")
            return None

        return Allocation(
            session_id=session_id,
            runtime_id=allocation["runtime_id"],
            host_id=allocation["host_id"],
            bind_token=bind_token,
            allocated_at=allocation["allocated_at"],
        )

    async def clear_allocation(self, session_id: str) -> None:
        """Clear runtime allocation.

        Args:
            session_id: Session identifier
        """
        await self.redis.clear_allocation(session_id)
        logger.info(f"Allocation cleared: session={session_id}")

    async def update_presence(self, session_id: str, status: str) -> None:
        """Update session presence/heartbeat.

        Args:
            session_id: Session identifier
            status: Current status
        """
        await self.redis.update_presence(session_id, self.gateway_id, status)

    async def cleanup_session(self, session_id: str) -> None:
        """Clean up all routing state for ended session.

        Args:
            session_id: Session identifier
        """
        # Clear all routing keys
        await self.redis.clear_client_route(session_id)
        await self.redis.clear_allocation(session_id)
        # Runtime route will be cleared by detach_runtime
        logger.info(f"Session routing cleaned up: {session_id}")
