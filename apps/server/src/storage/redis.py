"""Redis client for Glock server.

Key families:
- sess:{sid}:seq:server      - Server sequence number
- sess:{sid}:seq:client_ack  - Last acked client seq
- sess:{sid}:replay          - Recent server events (STREAM)
- sess:{sid}:presence        - {gateway_id, last_seen, status}
- sess:{sid}:state           - {status, active_task_id, ...}
- task:{tid}:state           - {status, attempt_no, ...}
- task:{tid}:approval        - Pending approval details
- task:{tid}:lock            - Distributed lock (TTL: 60s)
- rate:{uid}:minute          - Requests this minute (TTL: 60s)
- rate:{uid}:hour            - Requests this hour (TTL: 3600s)
- idempotent:{key}           - Cached result (TTL: 5min)
- route:sess:{sid}:client    - Client connection routing
- route:sess:{sid}:runtime   - Runtime connection routing
- route:sess:{sid}:allocation - Runtime allocation info
- route:pool:warm            - Queue of warm runtimes
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)


@dataclass
class RedisConfig:
    """Redis connection configuration."""
    url: str = ""
    host: str = "localhost"
    port: int = 6379
    password: Optional[str] = None
    db: int = 0
    max_connections: int = 50
    decode_responses: bool = True

    @classmethod
    def from_env(cls) -> RedisConfig:
        """Create config from environment variables."""
        url = os.environ.get("REDIS_URL", "")
        if url:
            return cls(url=url)
        return cls(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("REDIS_PASSWORD"),
            db=int(os.environ.get("REDIS_DB", "0")),
        )


# TTL policies (in seconds)
REDIS_TTLS = {
    "sess:replay": 3600,           # 1 hour
    "sess:presence": 120,          # 2 minutes (refreshed by heartbeat)
    "task:lock": 60,               # 1 minute
    "task:approval": 300,          # 5 minutes
    "rate:minute": 60,
    "rate:hour": 3600,
    "idempotent": 300,             # 5 minutes
    "route:client": 120,           # 2 minutes
    "route:runtime": 120,          # 2 minutes
    "route:allocation": 3600,      # 1 hour
}


class RedisClient:
    """Async Redis client for Glock server."""

    def __init__(self, config: Optional[RedisConfig] = None):
        self.config = config or RedisConfig.from_env()
        self._pool: Optional[redis.ConnectionPool] = None
        self._client: Optional[redis.Redis] = None

    async def connect(self) -> None:
        """Initialize Redis connection pool."""
        if self.config.url:
            self._pool = redis.ConnectionPool.from_url(
                self.config.url,
                max_connections=self.config.max_connections,
                decode_responses=self.config.decode_responses,
            )
        else:
            self._pool = redis.ConnectionPool(
                host=self.config.host,
                port=self.config.port,
                password=self.config.password,
                db=self.config.db,
                max_connections=self.config.max_connections,
                decode_responses=self.config.decode_responses,
            )
        self._client = redis.Redis(connection_pool=self._pool)
        # Test connection
        await self._client.ping()
        logger.info("Redis connection established")

    async def close(self) -> None:
        """Close Redis connection pool."""
        if self._client:
            await self._client.aclose()
        if self._pool:
            await self._pool.disconnect()
        logger.info("Redis connection closed")

    @property
    def client(self) -> redis.Redis:
        """Get Redis client, ensuring connection."""
        if not self._client:
            raise RuntimeError("Redis client not connected. Call connect() first.")
        return self._client

    # =========================================================================
    # Session State
    # =========================================================================

    async def get_session_seq(self, session_id: str) -> tuple[int, int]:
        """Get server sequence and last client ack."""
        pipe = self.client.pipeline()
        pipe.get(f"sess:{session_id}:seq:server")
        pipe.get(f"sess:{session_id}:seq:client_ack")
        results = await pipe.execute()
        return (
            int(results[0] or 0),
            int(results[1] or 0),
        )

    async def increment_server_seq(self, session_id: str) -> int:
        """Increment and return server sequence number."""
        return await self.client.incr(f"sess:{session_id}:seq:server")

    async def set_client_ack(self, session_id: str, ack: int) -> None:
        """Update last client ack."""
        await self.client.set(f"sess:{session_id}:seq:client_ack", ack)

    async def get_session_state(self, session_id: str) -> dict[str, Any]:
        """Get session state hash."""
        state = await self.client.hgetall(f"sess:{session_id}:state")
        return state or {}

    async def set_session_state(self, session_id: str, state: dict[str, Any]) -> None:
        """Set session state hash."""
        if state:
            await self.client.hset(f"sess:{session_id}:state", mapping=state)

    async def update_session_state(self, session_id: str, **kwargs: Any) -> None:
        """Update specific fields in session state."""
        if kwargs:
            await self.client.hset(f"sess:{session_id}:state", mapping=kwargs)

    # =========================================================================
    # Replay Buffer (Redis Streams)
    # =========================================================================

    async def append_replay(
        self, session_id: str, message: dict[str, Any], direction: str = "server"
    ) -> str:
        """Append message to replay stream. Returns stream ID."""
        stream_key = f"sess:{session_id}:replay:{direction}"
        # Store as JSON in single field for simplicity
        stream_id = await self.client.xadd(
            stream_key,
            {"data": json.dumps(message)},
            maxlen=100,  # Keep last 100 messages per direction
        )
        return stream_id

    async def get_replay_since(
        self, session_id: str, since_id: str = "0", direction: str = "server"
    ) -> list[dict[str, Any]]:
        """Get replay messages since given stream ID."""
        stream_key = f"sess:{session_id}:replay:{direction}"
        # XRANGE returns list of (id, fields) tuples
        entries = await self.client.xrange(stream_key, min=f"({since_id}")
        messages = []
        for _, fields in entries:
            if "data" in fields:
                messages.append(json.loads(fields["data"]))
        return messages

    async def trim_replay(self, session_id: str, direction: str = "server") -> None:
        """Trim replay stream to last 100 entries."""
        stream_key = f"sess:{session_id}:replay:{direction}"
        await self.client.xtrim(stream_key, maxlen=100, approximate=True)

    # =========================================================================
    # Task State
    # =========================================================================

    async def get_task_state(self, task_id: str) -> dict[str, Any]:
        """Get task state hash."""
        state = await self.client.hgetall(f"task:{task_id}:state")
        return state or {}

    async def set_task_state(self, task_id: str, state: dict[str, Any]) -> None:
        """Set task state hash."""
        if state:
            await self.client.hset(f"task:{task_id}:state", mapping=state)

    async def acquire_task_lock(self, task_id: str, owner: str, ttl: int = 60) -> bool:
        """Acquire distributed lock for task. Returns True if acquired."""
        return await self.client.set(
            f"task:{task_id}:lock",
            owner,
            nx=True,
            ex=ttl,
        )

    async def release_task_lock(self, task_id: str, owner: str) -> bool:
        """Release task lock if owned. Returns True if released."""
        # Use Lua script for atomic check-and-delete
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        result = await self.client.eval(script, 1, f"task:{task_id}:lock", owner)
        return bool(result)

    # =========================================================================
    # Rate Limiting
    # =========================================================================

    async def check_rate_limit(
        self, user_id: str, limit_per_minute: int, limit_per_hour: int
    ) -> tuple[bool, str]:
        """Check rate limits. Returns (allowed, reason)."""
        pipe = self.client.pipeline()
        minute_key = f"rate:{user_id}:minute"
        hour_key = f"rate:{user_id}:hour"

        # Get current counts
        pipe.get(minute_key)
        pipe.get(hour_key)
        results = await pipe.execute()

        minute_count = int(results[0] or 0)
        hour_count = int(results[1] or 0)

        if minute_count >= limit_per_minute:
            return False, "rate_limit_minute"
        if hour_count >= limit_per_hour:
            return False, "rate_limit_hour"

        return True, ""

    async def increment_rate_counters(self, user_id: str) -> None:
        """Increment rate limit counters."""
        pipe = self.client.pipeline()
        minute_key = f"rate:{user_id}:minute"
        hour_key = f"rate:{user_id}:hour"

        pipe.incr(minute_key)
        pipe.expire(minute_key, REDIS_TTLS["rate:minute"])
        pipe.incr(hour_key)
        pipe.expire(hour_key, REDIS_TTLS["rate:hour"])
        await pipe.execute()

    # =========================================================================
    # Idempotency
    # =========================================================================

    async def check_idempotency(self, key: str) -> Optional[dict[str, Any]]:
        """Check if idempotency key exists. Returns cached result or None."""
        result = await self.client.get(f"idempotent:{key}")
        if result:
            return json.loads(result)
        return None

    async def set_idempotency(self, key: str, result: dict[str, Any]) -> None:
        """Store idempotency result with TTL."""
        await self.client.setex(
            f"idempotent:{key}",
            REDIS_TTLS["idempotent"],
            json.dumps(result),
        )

    # =========================================================================
    # Routing
    # =========================================================================

    async def set_client_route(
        self,
        session_id: str,
        gateway_id: str,
        conn_id: str,
        connected_at: float,
    ) -> None:
        """Set client connection routing."""
        await self.client.hset(
            f"route:sess:{session_id}:client",
            mapping={
                "gateway_id": gateway_id,
                "conn_id": conn_id,
                "connected_at": str(connected_at),
                "status": "connected",
            },
        )
        await self.client.expire(
            f"route:sess:{session_id}:client",
            REDIS_TTLS["route:client"],
        )

    async def get_client_route(self, session_id: str) -> dict[str, str]:
        """Get client routing info."""
        return await self.client.hgetall(f"route:sess:{session_id}:client") or {}

    async def clear_client_route(self, session_id: str) -> None:
        """Clear client routing (on disconnect)."""
        await self.client.hset(
            f"route:sess:{session_id}:client",
            "status",
            "disconnected",
        )

    async def set_runtime_route(
        self,
        session_id: str,
        runtime_id: str,
        conn_id: str,
        connected_at: float,
        status: str = "connected",
    ) -> None:
        """Set runtime connection routing."""
        await self.client.hset(
            f"route:sess:{session_id}:runtime",
            mapping={
                "runtime_id": runtime_id,
                "conn_id": conn_id,
                "connected_at": str(connected_at),
                "status": status,
            },
        )
        await self.client.expire(
            f"route:sess:{session_id}:runtime",
            REDIS_TTLS["route:runtime"],
        )

    async def get_runtime_route(self, session_id: str) -> dict[str, str]:
        """Get runtime routing info."""
        return await self.client.hgetall(f"route:sess:{session_id}:runtime") or {}

    async def set_allocation(
        self,
        session_id: str,
        host_id: str,
        runtime_id: str,
        bind_token: str,
        allocated_at: float,
    ) -> None:
        """Store runtime allocation."""
        await self.client.setex(
            f"route:sess:{session_id}:allocation",
            REDIS_TTLS["route:allocation"],
            json.dumps({
                "host_id": host_id,
                "runtime_id": runtime_id,
                "bind_token": bind_token,
                "allocated_at": allocated_at,
            }),
        )

    async def get_allocation(self, session_id: str) -> Optional[dict[str, Any]]:
        """Get runtime allocation."""
        data = await self.client.get(f"route:sess:{session_id}:allocation")
        if data:
            return json.loads(data)
        return None

    async def clear_allocation(self, session_id: str) -> None:
        """Clear runtime allocation."""
        await self.client.delete(f"route:sess:{session_id}:allocation")

    # =========================================================================
    # Warm Pool
    # =========================================================================

    async def push_warm_runtime(self, runtime_id: str) -> None:
        """Add runtime to warm pool."""
        await self.client.rpush("route:pool:warm", runtime_id)

    async def pop_warm_runtime(self) -> Optional[str]:
        """Get and remove runtime from warm pool."""
        return await self.client.lpop("route:pool:warm")

    async def warm_pool_size(self) -> int:
        """Get warm pool size."""
        return await self.client.llen("route:pool:warm")

    # =========================================================================
    # Pub/Sub
    # =========================================================================

    async def publish(self, channel: str, message: str) -> int:
        """Publish message to channel."""
        return await self.client.publish(channel, message)

    @asynccontextmanager
    async def subscribe(self, *channels: str) -> AsyncIterator[redis.client.PubSub]:
        """Subscribe to channels."""
        pubsub = self.client.pubsub()
        await pubsub.subscribe(*channels)
        try:
            yield pubsub
        finally:
            await pubsub.unsubscribe(*channels)
            await pubsub.aclose()

    # =========================================================================
    # User Sessions Tracking
    # =========================================================================

    async def add_user_session(self, user_id: str, session_id: str) -> None:
        """Track user's active session."""
        await self.client.sadd(f"user:{user_id}:sessions", session_id)

    async def remove_user_session(self, user_id: str, session_id: str) -> None:
        """Remove session from user's active set."""
        await self.client.srem(f"user:{user_id}:sessions", session_id)

    async def get_user_sessions(self, user_id: str) -> set[str]:
        """Get user's active sessions."""
        return await self.client.smembers(f"user:{user_id}:sessions")

    async def count_user_sessions(self, user_id: str) -> int:
        """Count user's active sessions."""
        return await self.client.scard(f"user:{user_id}:sessions")

    # =========================================================================
    # Heartbeat
    # =========================================================================

    async def update_presence(
        self, session_id: str, gateway_id: str, status: str
    ) -> None:
        """Update session presence."""
        await self.client.hset(
            f"sess:{session_id}:presence",
            mapping={
                "gateway_id": gateway_id,
                "last_seen": str(int(time.time() * 1000)),
                "status": status,
            },
        )
        await self.client.expire(
            f"sess:{session_id}:presence",
            REDIS_TTLS["sess:presence"],
        )

    async def get_presence(self, session_id: str) -> dict[str, str]:
        """Get session presence."""
        return await self.client.hgetall(f"sess:{session_id}:presence") or {}


# Singleton instance
_redis_client: Optional[RedisClient] = None


async def get_redis() -> RedisClient:
    """Get or create Redis client singleton."""
    global _redis_client
    if _redis_client is None:
        _redis_client = RedisClient()
        await _redis_client.connect()
    return _redis_client


import time  # noqa: E402 - imported at end for type hints
