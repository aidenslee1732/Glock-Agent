"""Task management API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...storage.postgres import PostgresClient
from ...storage.redis import RedisClient
from .auth import get_current_user

router = APIRouter(prefix="/tasks", tags=["tasks"])


# Request/Response Models
class CreateTaskRequest(BaseModel):
    session_id: str
    prompt: str
    context: Optional[dict] = None


class TaskResponse(BaseModel):
    task_id: str
    session_id: str
    status: str
    task_type: Optional[str]
    complexity: Optional[str]
    risk_level: Optional[str]
    risk_flags: List[str]
    user_prompt: str
    retry_count: int
    max_retries: int
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    summary: Optional[str]
    failure_reason: Optional[str]


class TaskListResponse(BaseModel):
    tasks: List[TaskResponse]
    total: int


class TaskDetailResponse(TaskResponse):
    compiled_plan: Optional[dict] = None
    validations: List[dict] = []
    attempts: List[dict] = []


class CancelTaskResponse(BaseModel):
    task_id: str
    status: str
    cancelled: bool


def _task_to_response(task: dict) -> TaskResponse:
    """Convert database task to API response."""
    return TaskResponse(
        task_id=task["id"],
        session_id=task["session_id"],
        status=task["status"],
        task_type=task.get("task_type"),
        complexity=task.get("complexity"),
        risk_level=task.get("risk_level"),
        risk_flags=task.get("risk_flags", []),
        user_prompt=task["user_prompt"],
        retry_count=task.get("retry_count", 0),
        max_retries=task.get("max_retries", 2),
        created_at=task["created_at"],
        started_at=task.get("started_at"),
        completed_at=task.get("completed_at"),
        summary=task.get("summary"),
        failure_reason=task.get("failure_reason")
    )


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    session_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: PostgresClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """List user's tasks."""
    # If session_id provided, verify ownership
    if session_id:
        session = await db.get_session(session_id)
        if not session or session["user_id"] != user["id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access this session's tasks"
            )

    tasks = await db.list_tasks(
        user_id=user["id"],
        session_id=session_id,
        status=status_filter,
        limit=limit,
        offset=offset
    )

    total = await db.count_tasks(
        user_id=user["id"],
        session_id=session_id,
        status=status_filter
    )

    return TaskListResponse(
        tasks=[_task_to_response(t) for t in tasks],
        total=total
    )


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    request: CreateTaskRequest,
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Create a new task."""
    # Verify session ownership
    session = await db.get_session(request.session_id)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    if session["user_id"] != user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to create tasks in this session"
        )

    if session["status"] == "ended":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot create task in ended session"
        )

    # Check if session has an active task
    if session.get("active_task_id"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session already has an active task"
        )

    # Check concurrent task limits
    running_tasks = await db.count_tasks(
        user_id=user["id"],
        status="running"
    )

    plan_limits = {
        "free": 1,
        "pro": 3,
        "team": 10,
        "enterprise": 25
    }
    max_concurrent = plan_limits.get(user.get("plan_tier", "free"), 1)

    if running_tasks >= max_concurrent:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Concurrent task limit reached ({max_concurrent})"
        )

    # Create task
    task = await db.create_task(
        session_id=request.session_id,
        user_id=user["id"],
        user_prompt=request.prompt,
        context=request.context
    )

    # Update session's active task
    await db.update_session_active_task(request.session_id, task["id"])

    # Publish task created event
    await redis.publish_session_event(
        request.session_id,
        "task_created",
        {"task_id": task["id"], "prompt": request.prompt}
    )

    return _task_to_response(task)


@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: str,
    db: PostgresClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Get task details."""
    task = await db.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found"
        )

    if task["user_id"] != user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this task"
        )

    # Get compiled plan if exists
    compiled_plan = None
    if task.get("compiled_plan_id"):
        plan = await db.get_compiled_plan(task["compiled_plan_id"])
        if plan:
            compiled_plan = {
                "plan_id": plan["id"],
                "mode": plan.get("mode"),
                "allowed_tools": plan.get("allowed_tools", []),
                "validation_steps": plan.get("validation_steps", []),
                "created_at": plan["created_at"]
            }

    # Get validations
    validations = await db.list_task_validations(task_id)

    # Get attempts
    attempts = await db.list_task_attempts(task_id)

    return TaskDetailResponse(
        task_id=task["id"],
        session_id=task["session_id"],
        status=task["status"],
        task_type=task.get("task_type"),
        complexity=task.get("complexity"),
        risk_level=task.get("risk_level"),
        risk_flags=task.get("risk_flags", []),
        user_prompt=task["user_prompt"],
        retry_count=task.get("retry_count", 0),
        max_retries=task.get("max_retries", 2),
        created_at=task["created_at"],
        started_at=task.get("started_at"),
        completed_at=task.get("completed_at"),
        summary=task.get("summary"),
        failure_reason=task.get("failure_reason"),
        compiled_plan=compiled_plan,
        validations=validations,
        attempts=attempts
    )


@router.post("/{task_id}/cancel", response_model=CancelTaskResponse)
async def cancel_task(
    task_id: str,
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Cancel a running task."""
    task = await db.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found"
        )

    if task["user_id"] != user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to cancel this task"
        )

    # Check if task can be cancelled
    cancellable_statuses = ["queued", "running", "waiting_approval", "validating", "retrying"]

    if task["status"] not in cancellable_statuses:
        return CancelTaskResponse(
            task_id=task_id,
            status=task["status"],
            cancelled=False
        )

    # Update task status
    await db.update_task_status(task_id, "cancelled", failure_reason="Cancelled by user")

    # Clear session's active task
    session = await db.get_session(task["session_id"])
    if session and session.get("active_task_id") == task_id:
        await db.update_session_active_task(task["session_id"], None)

    # Publish cancel event
    await redis.publish_session_event(
        task["session_id"],
        "task_cancelled",
        {"task_id": task_id}
    )

    return CancelTaskResponse(
        task_id=task_id,
        status="cancelled",
        cancelled=True
    )


@router.post("/{task_id}/retry", response_model=TaskResponse)
async def retry_task(
    task_id: str,
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Manually retry a failed task."""
    task = await db.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found"
        )

    if task["user_id"] != user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to retry this task"
        )

    # Only failed tasks can be retried
    if task["status"] != "failed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only failed tasks can be retried"
        )

    # Check retry limit
    if task.get("retry_count", 0) >= task.get("max_retries", 2):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Task has exceeded retry limit"
        )

    # Update task for retry
    await db.update_task_for_retry(task_id)

    # Get updated task
    task = await db.get_task(task_id)

    # Update session's active task
    await db.update_session_active_task(task["session_id"], task_id)

    # Publish retry event
    await redis.publish_session_event(
        task["session_id"],
        "task_retry",
        {"task_id": task_id, "attempt_no": task.get("retry_count", 0) + 1}
    )

    return _task_to_response(task)
