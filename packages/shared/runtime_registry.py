"""Centralized runtime registry for routing and lifecycle management.

This module is used by both the gateway and runtime-host to avoid
duplicating routing logic.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class Allocation:
    """Runtime allocation details."""

    allocation_id: str
    session_id: str
    user_id: str
    runtime_id: str
    host_id: str
    bind_token: str
    allocated_at: datetime
    status: str = "pending"  # pending, bound, active, released

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocation_id": self.allocation_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "runtime_id": self.runtime_id,
            "host_id": self.host_id,
            "bind_token": self.bind_token,
            "allocated_at": self.allocated_at.isoformat(),
            "status": self.status
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Allocation":
        return cls(
            allocation_id=data["allocation_id"],
            session_id=data["session_id"],
            user_id=data["user_id"],
            runtime_id=data["runtime_id"],
            host_id=data["host_id"],
            bind_token=data["bind_token"],
            allocated_at=datetime.fromisoformat(data["allocated_at"]),
            status=data.get("status", "pending")
        )


@dataclass
class SessionRoute:
    """Routing information for a session."""

    session_id: str
    user_id: str

    # Client connection
    client_connected: bool = False
    client_gateway_id: Optional[str] = None
    client_conn_id: Optional[str] = None

    # Runtime connection
    runtime_connected: bool = False
    runtime_id: Optional[str] = None
    runtime_conn_id: Optional[str] = None

    # Allocation
    allocation: Optional[Allocation] = None

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_client_seen: Optional[datetime] = None
    last_runtime_seen: Optional[datetime] = None


class RuntimeRegistry:
    """Centralized routing and lifecycle management.

    Uses Redis for distributed state:
    - Route lookups for message relay
    - Allocation tracking
    - Heartbeat tracking
    - Cross-gateway pubsub
    """

    def __init__(self, redis_client):
        self.redis = redis_client

    # ----- Allocation -----

    async def allocate(
        self,
        session_id: str,
        user_id: str,
        runtime_id: str,
        host_id: str,
        bind_token: str
    ) -> Allocation:
        """Create a new runtime allocation for a session."""
        import uuid

        allocation = Allocation(
            allocation_id=f"alloc_{uuid.uuid4().hex[:16]}",
            session_id=session_id,
            user_id=user_id,
            runtime_id=runtime_id,
            host_id=host_id,
            bind_token=bind_token,
            allocated_at=datetime.now(timezone.utc),
            status="pending"
        )

        # Store allocation
        await self.redis.set(
            f"route:sess:{session_id}:allocation",
            json.dumps(allocation.to_dict()),
            ex=3600  # 1 hour TTL
        )

        # Track runtime assignment
        await self.redis.set(
            f"route:runtime:{runtime_id}:session",
            session_id,
            ex=3600
        )

        logger.info(f"Allocated runtime {runtime_id} to session {session_id}")
        return allocation

    async def get_allocation(self, session_id: str) -> Optional[Allocation]:
        """Get allocation for a session."""
        data = await self.redis.get(f"route:sess:{session_id}:allocation")
        if data:
            return Allocation.from_dict(json.loads(data))
        return None

    # ----- Runtime Binding -----

    async def bind_runtime(
        self,
        session_id: str,
        runtime_id: str,
        conn_id: str
    ) -> bool:
        """Bind a runtime connection to a session."""
        # Verify allocation exists
        allocation = await self.get_allocation(session_id)
        if not allocation:
            logger.warning(f"No allocation found for session {session_id}")
            return False

        if allocation.runtime_id != runtime_id:
            logger.warning(f"Runtime {runtime_id} doesn't match allocation")
            return False

        # Store runtime connection info
        await self.redis.hset(f"route:sess:{session_id}:runtime", mapping={
            "runtime_id": runtime_id,
            "conn_id": conn_id,
            "status": "connected",
            "connected_at": datetime.now(timezone.utc).isoformat()
        })

        # Update allocation status
        allocation.status = "bound"
        await self.redis.set(
            f"route:sess:{session_id}:allocation",
            json.dumps(allocation.to_dict()),
            ex=3600
        )

        # Update runtime status
        await self.redis.set(f"route:runtime:{runtime_id}:status", "busy")

        logger.info(f"Runtime {runtime_id} bound to session {session_id}")
        return True

    async def unbind_runtime(self, session_id: str) -> bool:
        """Unbind runtime from session."""
        runtime_info = await self.redis.hgetall(f"route:sess:{session_id}:runtime")
        if not runtime_info:
            return False

        runtime_id = runtime_info.get("runtime_id")

        # Clear runtime connection
        await self.redis.delete(f"route:sess:{session_id}:runtime")

        # Update runtime status
        if runtime_id:
            await self.redis.set(f"route:runtime:{runtime_id}:status", "ready")
            await self.redis.delete(f"route:runtime:{runtime_id}:session")

        logger.info(f"Runtime unbound from session {session_id}")
        return True

    # ----- Client Connection -----

    async def attach_client(
        self,
        session_id: str,
        client_conn_id: str,
        gateway_id: str
    ) -> bool:
        """Attach a client connection to a session."""
        await self.redis.hset(f"route:sess:{session_id}:client", mapping={
            "conn_id": client_conn_id,
            "gateway_id": gateway_id,
            "status": "connected",
            "connected_at": datetime.now(timezone.utc).isoformat()
        })

        logger.info(f"Client attached to session {session_id} via gateway {gateway_id}")
        return True

    async def detach_client(self, session_id: str) -> bool:
        """Detach client from session."""
        await self.redis.hset(f"route:sess:{session_id}:client", mapping={
            "status": "disconnected",
            "disconnected_at": datetime.now(timezone.utc).isoformat()
        })

        logger.info(f"Client detached from session {session_id}")
        return True

    # ----- Routing -----

    async def get_route(self, session_id: str) -> Optional[SessionRoute]:
        """Get full routing information for a session."""
        # Get client info
        client_info = await self.redis.hgetall(f"route:sess:{session_id}:client")

        # Get runtime info
        runtime_info = await self.redis.hgetall(f"route:sess:{session_id}:runtime")

        # Get allocation
        allocation = await self.get_allocation(session_id)

        if not client_info and not runtime_info and not allocation:
            return None

        return SessionRoute(
            session_id=session_id,
            user_id=allocation.user_id if allocation else "",
            client_connected=client_info.get("status") == "connected",
            client_gateway_id=client_info.get("gateway_id"),
            client_conn_id=client_info.get("conn_id"),
            runtime_connected=runtime_info.get("status") == "connected",
            runtime_id=runtime_info.get("runtime_id"),
            runtime_conn_id=runtime_info.get("conn_id"),
            allocation=allocation
        )

    async def get_runtime_route(
        self,
        session_id: str
    ) -> Optional[tuple[str, str]]:
        """Get runtime connection info for relaying client messages.

        Returns tuple of (runtime_id, conn_id) or None.
        """
        info = await self.redis.hgetall(f"route:sess:{session_id}:runtime")
        if info and info.get("status") == "connected":
            return info.get("runtime_id"), info.get("conn_id")
        return None

    async def get_client_route(
        self,
        session_id: str
    ) -> Optional[tuple[str, str]]:
        """Get client connection info for relaying runtime messages.

        Returns tuple of (gateway_id, conn_id) or None.
        """
        info = await self.redis.hgetall(f"route:sess:{session_id}:client")
        if info and info.get("status") == "connected":
            return info.get("gateway_id"), info.get("conn_id")
        return None

    # ----- Lifecycle -----

    async def release(self, session_id: str, recycle: bool = True) -> bool:
        """Release all resources for a session."""
        # Unbind runtime
        await self.unbind_runtime(session_id)

        # Clear routing info
        await self.redis.delete(f"route:sess:{session_id}:client")
        await self.redis.delete(f"route:sess:{session_id}:runtime")
        await self.redis.delete(f"route:sess:{session_id}:allocation")

        logger.info(f"Released session {session_id}")
        return True

    async def park_session(self, session_id: str) -> bool:
        """Park a session - release runtime but keep client routing."""
        # Unbind runtime
        await self.unbind_runtime(session_id)

        # Clear allocation (will need new one on unpark)
        await self.redis.delete(f"route:sess:{session_id}:allocation")

        # Keep client connection info
        logger.info(f"Parked session {session_id}")
        return True

    async def unpark_session(
        self,
        session_id: str,
        runtime_id: str,
        host_id: str,
        bind_token: str
    ) -> Optional[Allocation]:
        """Unpark a session - allocate new runtime."""
        # Get existing client info
        client_info = await self.redis.hgetall(f"route:sess:{session_id}:client")
        if not client_info:
            logger.warning(f"Cannot unpark session {session_id} - no client info")
            return None

        user_id = client_info.get("user_id", "")

        # Create new allocation
        allocation = await self.allocate(
            session_id=session_id,
            user_id=user_id,
            runtime_id=runtime_id,
            host_id=host_id,
            bind_token=bind_token
        )

        logger.info(f"Unparked session {session_id}")
        return allocation

    # ----- Heartbeats -----

    async def heartbeat_runtime(self, runtime_id: str) -> bool:
        """Record runtime heartbeat."""
        await self.redis.set(
            f"route:runtime:{runtime_id}:heartbeat",
            datetime.now(timezone.utc).isoformat(),
            ex=120  # 2 minute TTL
        )
        return True

    async def heartbeat_client(self, session_id: str, conn_id: str) -> bool:
        """Record client heartbeat."""
        await self.redis.hset(f"route:sess:{session_id}:client", mapping={
            "last_heartbeat": datetime.now(timezone.utc).isoformat()
        })
        return True

    async def check_runtime_alive(self, runtime_id: str) -> bool:
        """Check if runtime heartbeat is recent."""
        heartbeat = await self.redis.get(f"route:runtime:{runtime_id}:heartbeat")
        return heartbeat is not None
