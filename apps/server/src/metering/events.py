"""Usage event definitions and emission."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from ..storage.redis import RedisClient


class UsageEventType(str, Enum):
    """Types of usage events."""

    # Session events
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    SESSION_PARKED = "session_parked"
    SESSION_RESUMED = "session_resumed"

    # Task events
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_RETRIED = "task_retried"

    # Resource events
    TOKENS_USED = "tokens_used"
    TOOL_CALLED = "tool_called"
    VALIDATION_RUN = "validation_run"
    PLAN_COMPILED = "plan_compiled"

    # Runtime events
    RUNTIME_ALLOCATED = "runtime_allocated"
    RUNTIME_RELEASED = "runtime_released"


class UsageUnit(str, Enum):
    """Units for usage measurements."""

    COUNT = "count"
    TOKENS = "tokens"
    SECONDS = "seconds"
    BYTES = "bytes"
    VALIDATIONS = "validations"


@dataclass
class UsageEvent:
    """A usage event for metering."""

    event_type: UsageEventType
    user_id: str
    quantity: float
    unit: UsageUnit

    # Optional context
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    org_id: Optional[str] = None

    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    # Auto-generated
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "user_id": self.user_id,
            "org_id": self.org_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "quantity": self.quantity,
            "unit": self.unit.value,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat()
        }


# Global Redis client reference (set during app startup)
_redis_client: Optional[RedisClient] = None


def set_redis_client(client: RedisClient):
    """Set the Redis client for event emission."""
    global _redis_client
    _redis_client = client


async def emit_usage_event(
    event_type: UsageEventType,
    user_id: str,
    quantity: float = 1.0,
    unit: UsageUnit = UsageUnit.COUNT,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    org_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None
):
    """Emit a usage event to the metering queue.

    This is a fire-and-forget operation - failures are logged but don't
    block the caller.
    """
    if not _redis_client:
        return  # Silently skip if not initialized

    event = UsageEvent(
        event_type=event_type,
        user_id=user_id,
        quantity=quantity,
        unit=unit,
        session_id=session_id,
        task_id=task_id,
        org_id=org_id,
        metadata=metadata or {}
    )

    try:
        # Add to Redis stream
        await _redis_client.xadd(
            "q:metering",
            event.to_dict()
        )
    except Exception as e:
        # Log but don't fail
        import logging
        logging.getLogger(__name__).warning(f"Failed to emit usage event: {e}")


# Convenience functions for common events
async def emit_task_started(user_id: str, session_id: str, task_id: str, org_id: Optional[str] = None):
    """Emit task started event."""
    await emit_usage_event(
        UsageEventType.TASK_STARTED,
        user_id=user_id,
        session_id=session_id,
        task_id=task_id,
        org_id=org_id
    )


async def emit_task_completed(
    user_id: str,
    session_id: str,
    task_id: str,
    tokens_used: int = 0,
    tool_calls: int = 0,
    org_id: Optional[str] = None
):
    """Emit task completed event with resource usage."""
    await emit_usage_event(
        UsageEventType.TASK_COMPLETED,
        user_id=user_id,
        session_id=session_id,
        task_id=task_id,
        org_id=org_id,
        metadata={"tokens_used": tokens_used, "tool_calls": tool_calls}
    )

    # Also emit separate token event
    if tokens_used > 0:
        await emit_usage_event(
            UsageEventType.TOKENS_USED,
            user_id=user_id,
            quantity=tokens_used,
            unit=UsageUnit.TOKENS,
            session_id=session_id,
            task_id=task_id,
            org_id=org_id
        )


async def emit_tokens_used(
    user_id: str,
    tokens: int,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    org_id: Optional[str] = None,
    model_tier: Optional[str] = None
):
    """Emit tokens used event."""
    await emit_usage_event(
        UsageEventType.TOKENS_USED,
        user_id=user_id,
        quantity=tokens,
        unit=UsageUnit.TOKENS,
        session_id=session_id,
        task_id=task_id,
        org_id=org_id,
        metadata={"model_tier": model_tier} if model_tier else None
    )


async def emit_tool_called(
    user_id: str,
    session_id: str,
    task_id: str,
    tool_name: str,
    duration_ms: int = 0,
    org_id: Optional[str] = None
):
    """Emit tool called event."""
    await emit_usage_event(
        UsageEventType.TOOL_CALLED,
        user_id=user_id,
        session_id=session_id,
        task_id=task_id,
        org_id=org_id,
        metadata={"tool_name": tool_name, "duration_ms": duration_ms}
    )
