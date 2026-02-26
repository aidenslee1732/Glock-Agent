"""User profile and usage API endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...storage.postgres import PostgresClient
from ...storage.redis import RedisClient
from .auth import get_current_user

router = APIRouter(prefix="/profile", tags=["profile"])


# Response Models
class UserProfileResponse(BaseModel):
    user_id: str
    email: str
    name: Optional[str]
    plan_tier: str
    status: str
    created_at: datetime
    last_seen_at: Optional[datetime]


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None


class UsageMetric(BaseModel):
    metric: str
    value: float
    unit: str


class UsagePeriod(BaseModel):
    period_start: datetime
    period_end: datetime
    metrics: List[UsageMetric]


class UsageResponse(BaseModel):
    user_id: str
    plan_tier: str
    current_period: UsagePeriod
    limits: dict


class PreferencesResponse(BaseModel):
    user_id: str
    preferences: dict
    confidence: dict
    learning_enabled: bool
    updated_at: datetime


class UpdatePreferencesRequest(BaseModel):
    preferences: Optional[dict] = None
    learning_enabled: Optional[bool] = None


@router.get("", response_model=UserProfileResponse)
async def get_profile(
    db: PostgresClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Get current user's profile."""
    return UserProfileResponse(
        user_id=user["id"],
        email=user["email"],
        name=user.get("name"),
        plan_tier=user.get("plan_tier", "free"),
        status=user["status"],
        created_at=user["created_at"],
        last_seen_at=user.get("last_seen_at")
    )


@router.put("", response_model=UserProfileResponse)
async def update_profile(
    request: UpdateProfileRequest,
    db: PostgresClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Update current user's profile."""
    updates = {}

    if request.name is not None:
        updates["name"] = request.name

    if updates:
        await db.update_user(user["id"], updates)
        user = await db.get_user(user["id"])

    return UserProfileResponse(
        user_id=user["id"],
        email=user["email"],
        name=user.get("name"),
        plan_tier=user.get("plan_tier", "free"),
        status=user["status"],
        created_at=user["created_at"],
        last_seen_at=user.get("last_seen_at")
    )


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Get current user's usage statistics."""
    # Calculate current billing period (monthly)
    now = datetime.now(timezone.utc)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if now.month == 12:
        period_end = period_start.replace(year=now.year + 1, month=1)
    else:
        period_end = period_start.replace(month=now.month + 1)

    # Get usage rollups from database
    rollups = await db.get_usage_rollups(
        user_id=user["id"],
        start_time=period_start,
        end_time=now
    )

    # Aggregate metrics
    metrics_map = {}
    for rollup in rollups:
        metric = rollup["metric"]
        if metric not in metrics_map:
            metrics_map[metric] = {"value": 0, "unit": "count"}
        metrics_map[metric]["value"] += rollup.get("value", 0)

    # Set units for known metrics
    metric_units = {
        "tasks_completed": "count",
        "tasks_failed": "count",
        "tokens_used": "tokens",
        "tool_calls": "count",
        "validation_runs": "count",
        "session_time": "seconds"
    }

    metrics = [
        UsageMetric(
            metric=name,
            value=data["value"],
            unit=metric_units.get(name, "count")
        )
        for name, data in metrics_map.items()
    ]

    # Get plan limits
    plan_limits = {
        "free": {
            "max_sessions": 2,
            "max_concurrent_tasks": 1,
            "tokens_per_month": 100000,
            "tasks_per_month": 50
        },
        "pro": {
            "max_sessions": 10,
            "max_concurrent_tasks": 3,
            "tokens_per_month": 1000000,
            "tasks_per_month": 500
        },
        "team": {
            "max_sessions": 25,
            "max_concurrent_tasks": 10,
            "tokens_per_month": 5000000,
            "tasks_per_month": 2500
        },
        "enterprise": {
            "max_sessions": 100,
            "max_concurrent_tasks": 25,
            "tokens_per_month": -1,  # Unlimited
            "tasks_per_month": -1
        }
    }

    user_plan = user.get("plan_tier", "free")
    limits = plan_limits.get(user_plan, plan_limits["free"])

    return UsageResponse(
        user_id=user["id"],
        plan_tier=user_plan,
        current_period=UsagePeriod(
            period_start=period_start,
            period_end=period_end,
            metrics=metrics
        ),
        limits=limits
    )


@router.get("/preferences", response_model=PreferencesResponse)
async def get_preferences(
    db: PostgresClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Get user preferences."""
    prefs = await db.get_user_preferences(user["id"])

    if not prefs:
        # Return defaults
        return PreferencesResponse(
            user_id=user["id"],
            preferences={},
            confidence={},
            learning_enabled=True,
            updated_at=datetime.now(timezone.utc)
        )

    return PreferencesResponse(
        user_id=user["id"],
        preferences=prefs.get("prefs", {}),
        confidence=prefs.get("confidence", {}),
        learning_enabled=prefs.get("learning_enabled", True),
        updated_at=prefs.get("updated_at", datetime.now(timezone.utc))
    )


@router.put("/preferences", response_model=PreferencesResponse)
async def update_preferences(
    request: UpdatePreferencesRequest,
    db: PostgresClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Update user preferences."""
    updates = {}

    if request.preferences is not None:
        updates["prefs"] = request.preferences

    if request.learning_enabled is not None:
        updates["learning_enabled"] = request.learning_enabled

    if updates:
        await db.upsert_user_preferences(user["id"], updates)

    return await get_preferences(db=db, user=user)


@router.get("/activity")
async def get_activity(
    days: int = 7,
    db: PostgresClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Get recent activity summary."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Get recent tasks
    tasks = await db.list_tasks(
        user_id=user["id"],
        since=since,
        limit=100
    )

    # Calculate stats
    total_tasks = len(tasks)
    completed = sum(1 for t in tasks if t["status"] == "completed")
    failed = sum(1 for t in tasks if t["status"] == "failed")
    cancelled = sum(1 for t in tasks if t["status"] == "cancelled")

    # Group by day
    daily_counts = {}
    for task in tasks:
        day = task["created_at"].date().isoformat()
        if day not in daily_counts:
            daily_counts[day] = {"total": 0, "completed": 0, "failed": 0}
        daily_counts[day]["total"] += 1
        if task["status"] == "completed":
            daily_counts[day]["completed"] += 1
        elif task["status"] == "failed":
            daily_counts[day]["failed"] += 1

    return {
        "period_days": days,
        "summary": {
            "total_tasks": total_tasks,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "success_rate": completed / total_tasks if total_tasks > 0 else 0
        },
        "daily": daily_counts
    }
