"""Session management API endpoints."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...storage.postgres import PostgresClient
from ...storage.redis import RedisClient
from .auth import get_current_user

router = APIRouter(prefix="/sessions", tags=["sessions"])


# Request/Response Models
class CreateSessionRequest(BaseModel):
    workspace_label: Optional[str] = None
    repo_fingerprint: Optional[str] = None
    branch_name: Optional[str] = None
    repo_root_hint: Optional[str] = None


class SessionResponse(BaseModel):
    session_id: str
    status: str
    workspace_label: Optional[str]
    repo_fingerprint: Optional[str]
    branch_name: Optional[str]
    active_task_id: Optional[str]
    ws_url: str
    created_at: datetime
    last_seen_at: Optional[datetime]


class SessionListResponse(BaseModel):
    sessions: List[SessionResponse]
    total: int


class SessionDetailResponse(SessionResponse):
    client_id: str
    last_client_seq_acked: int
    last_server_seq_sent: int
    active_task: Optional[dict] = None


class ResumeSessionResponse(BaseModel):
    session_id: str
    resume_token: str
    last_seq: int
    ws_url: str


def _session_to_response(session: dict, ws_base_url: str = "wss://api.glock.dev") -> SessionResponse:
    """Convert database session to API response."""
    return SessionResponse(
        session_id=session["id"],
        status=session["status"],
        workspace_label=session.get("workspace_label"),
        repo_fingerprint=session.get("repo_fingerprint"),
        branch_name=session.get("branch_name"),
        active_task_id=session.get("active_task_id"),
        ws_url=f"{ws_base_url}/ws/client?session_id={session['id']}",
        created_at=session["created_at"],
        last_seen_at=session.get("last_seen_at")
    )


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    status_filter: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: PostgresClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """List user's sessions."""
    # Get sessions from database
    sessions = await db.list_sessions(
        user_id=user["id"],
        status=status_filter,
        limit=limit,
        offset=offset
    )

    total = await db.count_sessions(
        user_id=user["id"],
        status=status_filter
    )

    return SessionListResponse(
        sessions=[_session_to_response(s) for s in sessions],
        total=total
    )


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    request: CreateSessionRequest,
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Create a new session."""
    # Check session limits based on plan
    plan_limits = {
        "free": 2,
        "pro": 10,
        "team": 25,
        "enterprise": 100
    }
    max_sessions = plan_limits.get(user.get("plan_tier", "free"), 2)

    active_count = await db.count_sessions(
        user_id=user["id"],
        status="idle"  # Count active sessions
    )

    if active_count >= max_sessions:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Session limit reached ({max_sessions} for {user.get('plan_tier', 'free')} tier)"
        )

    # Generate client ID
    client_id = f"cli_{secrets.token_hex(8)}"

    # Create session in database
    session = await db.create_session(
        user_id=user["id"],
        client_id=client_id,
        workspace_label=request.workspace_label,
        repo_fingerprint=request.repo_fingerprint,
        branch_name=request.branch_name,
        repo_root_hint=request.repo_root_hint
    )

    # Initialize session state in Redis
    await redis.init_session_state(session["id"])

    # Track user's active sessions
    await redis.add_user_session(user["id"], session["id"])

    return _session_to_response(session)


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: str,
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Get session details."""
    session = await db.get_session(session_id)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    # Verify ownership
    if session["user_id"] != user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this session"
        )

    # Get active task if any
    active_task = None
    if session.get("active_task_id"):
        active_task = await db.get_task(session["active_task_id"])

    # Get Redis state for detailed info
    redis_state = await redis.get_session_state(session_id)

    return SessionDetailResponse(
        session_id=session["id"],
        status=session["status"],
        workspace_label=session.get("workspace_label"),
        repo_fingerprint=session.get("repo_fingerprint"),
        branch_name=session.get("branch_name"),
        active_task_id=session.get("active_task_id"),
        ws_url=f"wss://api.glock.dev/ws/client?session_id={session['id']}",
        created_at=session["created_at"],
        last_seen_at=session.get("last_seen_at"),
        client_id=session["client_id"],
        last_client_seq_acked=session.get("last_client_seq_acked", 0),
        last_server_seq_sent=session.get("last_server_seq_sent", 0),
        active_task=active_task
    )


@router.delete("/{session_id}")
async def end_session(
    session_id: str,
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """End a session and clean up resources."""
    session = await db.get_session(session_id)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    if session["user_id"] != user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to end this session"
        )

    if session["status"] == "ended":
        return {"status": "already_ended", "session_id": session_id}

    # Cancel any active tasks
    if session.get("active_task_id"):
        await db.update_task_status(
            session["active_task_id"],
            "cancelled",
            failure_reason="Session ended by user"
        )

    # Update session status
    await db.end_session(session_id)

    # Clean up Redis state
    await redis.cleanup_session(session_id)

    # Remove from user's active sessions
    await redis.remove_user_session(user["id"], session_id)

    return {"status": "ended", "session_id": session_id}


@router.post("/{session_id}/resume", response_model=ResumeSessionResponse)
async def resume_session(
    session_id: str,
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Resume a disconnected session."""
    session = await db.get_session(session_id)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    if session["user_id"] != user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to resume this session"
        )

    if session["status"] == "ended":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot resume ended session"
        )

    # Generate resume token
    resume_token = secrets.token_urlsafe(32)

    # Store resume token in Redis with short TTL
    await redis.set_resume_token(session_id, resume_token, ttl=60)

    # Get last sequence number
    last_seq = session.get("last_server_seq_sent", 0)

    return ResumeSessionResponse(
        session_id=session_id,
        resume_token=resume_token,
        last_seq=last_seq,
        ws_url=f"wss://api.glock.dev/ws/client?session_id={session_id}&resume=true"
    )


@router.post("/{session_id}/park")
async def park_session(
    session_id: str,
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Park a session (release runtime but keep state)."""
    session = await db.get_session(session_id)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    if session["user_id"] != user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to park this session"
        )

    if session.get("active_task_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot park session with active task"
        )

    # Update session state to parked
    await redis.set_session_substate(session_id, "PARKED")

    # Signal runtime to release (if bound)
    await redis.publish_session_event(session_id, "park_requested")

    return {"status": "parked", "session_id": session_id}
