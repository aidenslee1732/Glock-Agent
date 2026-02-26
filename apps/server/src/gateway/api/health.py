"""Health check API endpoints."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...storage.postgres import PostgresClient
from ...storage.redis import RedisClient

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    version: str
    uptime_seconds: float


class ReadinessResponse(BaseModel):
    ready: bool
    checks: dict


class ComponentHealth(BaseModel):
    status: str
    latency_ms: float
    details: dict = {}


# Track server start time
_start_time = time.time()
_version = "1.0.0"


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Basic health check - is the server running?"""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc),
        version=_version,
        uptime_seconds=time.time() - _start_time
    )


@router.get("/ready", response_model=ReadinessResponse)
async def readiness_check(
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends()
):
    """Readiness check - are all dependencies available?"""
    checks = {}
    all_ready = True

    # Check PostgreSQL
    try:
        start = time.time()
        await db.health_check()
        latency = (time.time() - start) * 1000
        checks["postgres"] = ComponentHealth(
            status="healthy",
            latency_ms=latency
        )
    except Exception as e:
        checks["postgres"] = ComponentHealth(
            status="unhealthy",
            latency_ms=0,
            details={"error": str(e)}
        )
        all_ready = False

    # Check Redis
    try:
        start = time.time()
        await redis.health_check()
        latency = (time.time() - start) * 1000
        checks["redis"] = ComponentHealth(
            status="healthy",
            latency_ms=latency
        )
    except Exception as e:
        checks["redis"] = ComponentHealth(
            status="unhealthy",
            latency_ms=0,
            details={"error": str(e)}
        )
        all_ready = False

    return ReadinessResponse(
        ready=all_ready,
        checks={k: v.model_dump() for k, v in checks.items()}
    )


@router.get("/metrics")
async def metrics(
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends()
):
    """Prometheus-style metrics endpoint."""
    metrics_lines = []

    # Uptime
    metrics_lines.append(f"glock_uptime_seconds {time.time() - _start_time}")

    # Try to get connection pool stats
    try:
        pool_stats = await db.get_pool_stats()
        metrics_lines.append(f"glock_db_pool_size {pool_stats.get('size', 0)}")
        metrics_lines.append(f"glock_db_pool_available {pool_stats.get('available', 0)}")
    except Exception:
        pass

    # Try to get Redis stats
    try:
        redis_info = await redis.get_info()
        metrics_lines.append(f"glock_redis_connected_clients {redis_info.get('connected_clients', 0)}")
        metrics_lines.append(f"glock_redis_used_memory_bytes {redis_info.get('used_memory', 0)}")
    except Exception:
        pass

    return "\n".join(metrics_lines)


@router.get("/version")
async def version():
    """Get server version information."""
    return {
        "version": _version,
        "api_version": "v1",
        "build": "production"
    }
