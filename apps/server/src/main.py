"""Glock Gateway Server - Main entry point.

Model B Architecture: Stateless LLM Proxy
- Client does all orchestration
- Server proxies LLM requests
- Checkpoint storage for resume
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

# Configure logging
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Dev mode flag
DEV_MODE = os.environ.get("DEV_MODE", "").lower() in ("1", "true", "yes")

# Gateway ID (unique per instance)
GATEWAY_ID = os.environ.get("GATEWAY_ID", f"gw_{secrets.token_hex(8)}")


# Global instances
redis_client = None
postgres_client = None
client_handler = None
checkpoint_store = None
llm_handler = None


class MockRedisClient:
    """In-memory mock Redis for dev mode."""

    def __init__(self):
        self._data = {}
        self._expiry = {}
        logger.info("Using MockRedisClient (DEV MODE)")

    async def connect(self):
        pass

    async def close(self):
        pass

    async def get(self, key: str) -> Optional[str]:
        return self._data.get(key)

    async def set(self, key: str, value: str, ex: int = None):
        self._data[key] = value

    async def delete(self, key: str):
        self._data.pop(key, None)

    async def hgetall(self, key: str) -> dict:
        return self._data.get(key, {})

    async def hset(self, key: str, mapping: dict):
        if key not in self._data:
            self._data[key] = {}
        self._data[key].update(mapping)

    async def hdel(self, key: str, *fields):
        if key in self._data:
            for f in fields:
                self._data[key].pop(f, None)

    async def publish(self, channel: str, message: str):
        pass

    async def ping(self):
        return True

    # Session routing methods
    async def set_client_route(self, session_id: str, **kwargs):
        key = f"sess:{session_id}:client"
        self._data[key] = {"status": "connected", **kwargs}

    async def get_client_route(self, session_id: str) -> dict:
        return self._data.get(f"sess:{session_id}:client", {})

    async def clear_client_route(self, session_id: str):
        self._data.pop(f"sess:{session_id}:client", None)

    async def get_runtime_route(self, session_id: str) -> dict:
        return {}  # Model B: No runtime

    async def get_allocation(self, session_id: str) -> Optional[dict]:
        return None  # Model B: No allocation

    async def set_allocation(self, session_id: str, **kwargs):
        pass

    async def clear_allocation(self, session_id: str):
        pass

    async def update_presence(self, session_id: str, gateway_id: str, status: str):
        pass

    async def check_rate_limit(self, user_id: str, per_minute: int, per_hour: int) -> tuple[bool, str]:
        """Mock rate limit check - always allow in dev mode."""
        return True, ""

    async def check_and_increment_rate_limit(self, user_id: str, per_minute: int, per_hour: int) -> tuple[bool, str]:
        """Mock atomic rate limit check - always allow in dev mode."""
        return True, ""

    async def count_user_sessions(self, user_id: str) -> int:
        """Count user sessions."""
        count = 0
        for key in self._data:
            if key.startswith(f"user:{user_id}:sessions"):
                count += len(self._data[key]) if isinstance(self._data[key], (list, set)) else 1
        return count

    async def add_user_session(self, user_id: str, session_id: str):
        """Add session to user's session list."""
        key = f"user:{user_id}:sessions"
        if key not in self._data:
            self._data[key] = set()
        self._data[key].add(session_id)

    async def remove_user_session(self, user_id: str, session_id: str):
        """Remove session from user's session list."""
        key = f"user:{user_id}:sessions"
        if key in self._data and isinstance(self._data[key], set):
            self._data[key].discard(session_id)

    async def increment_rate_counters(self, user_id: str):
        """Increment rate limit counters - no-op in dev mode."""
        pass

    async def set_session_state(self, session_id: str, state: dict):
        """Set session state."""
        key = f"sess:{session_id}:state"
        if key not in self._data:
            self._data[key] = {}
        self._data[key].update(state)

    async def setex(self, key: str, ttl: int, value: str):
        """Set with expiry."""
        self._data[key] = value

    async def lpush(self, key: str, value: str):
        """Push to list."""
        if key not in self._data:
            self._data[key] = []
        self._data[key].insert(0, value)

    async def ltrim(self, key: str, start: int, end: int):
        """Trim list."""
        if key in self._data and isinstance(self._data[key], list):
            self._data[key] = self._data[key][start:end + 1]

    async def llen(self, key: str) -> int:
        """Get list length."""
        if key in self._data and isinstance(self._data[key], list):
            return len(self._data[key])
        return 0

    async def lrange(self, key: str, start: int, end: int) -> list:
        """Get list range."""
        if key in self._data and isinstance(self._data[key], list):
            if end == -1:
                return self._data[key][start:]
            return self._data[key][start:end + 1]
        return []

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        """Increment hash field by amount."""
        if key not in self._data:
            self._data[key] = {}
        if not isinstance(self._data[key], dict):
            self._data[key] = {}
        current = int(self._data[key].get(field, 0))
        self._data[key][field] = str(current + amount)
        return current + amount


class MockPostgresClient:
    """In-memory mock Postgres for dev mode."""

    def __init__(self):
        self._sessions = {}
        self._tasks = {}
        self._users = {}
        self._checkpoints = {}
        self._errors = {}
        logger.info("Using MockPostgresClient (DEV MODE)")

    async def connect(self):
        pass

    async def close(self):
        pass

    async def create_session(
        self,
        session_id: str = None,
        user_id: str = None,
        client_id: str = None,
        workspace_label: str = None,
        repo_fingerprint: str = None,
        branch_name: str = None,
    ) -> str:
        if session_id is None:
            session_id = f"sess_{secrets.token_hex(8)}"
        self._sessions[session_id] = {
            "id": session_id,
            "user_id": user_id,
            "client_id": client_id,
            "workspace_label": workspace_label,
            "repo_fingerprint": repo_fingerprint,
            "branch_name": branch_name,
            "status": "active",
        }
        return session_id

    async def get_session(self, session_id: str) -> Optional[dict]:
        return self._sessions.get(session_id)

    async def get_user_sessions(self, user_id: str) -> list:
        return [s for s in self._sessions.values() if s["user_id"] == user_id]

    async def update_session_status(self, session_id: str, status: str):
        if session_id in self._sessions:
            self._sessions[session_id]["status"] = status

    async def end_session(self, session_id: str):
        if session_id in self._sessions:
            self._sessions[session_id]["status"] = "ended"

    async def touch_session(self, session_id: str):
        """Update session last activity timestamp."""
        if session_id in self._sessions:
            self._sessions[session_id]["last_activity"] = time.time()

    async def create_task(self, session_id: str, prompt: str) -> str:
        task_id = f"task_{secrets.token_hex(8)}"
        self._tasks[task_id] = {
            "id": task_id,
            "session_id": session_id,
            "prompt": prompt,
            "status": "running",
        }
        return task_id

    async def get_task(self, task_id: str) -> Optional[dict]:
        return self._tasks.get(task_id)

    async def update_task_status(self, task_id: str, status: str):
        if task_id in self._tasks:
            self._tasks[task_id]["status"] = status

    async def store_checkpoint(self, **kwargs) -> str:
        checkpoint_id = kwargs.get("checkpoint_id", f"cp_{secrets.token_hex(8)}")
        self._checkpoints[checkpoint_id] = kwargs
        return checkpoint_id

    async def get_checkpoint(self, checkpoint_id: str) -> Optional[dict]:
        return self._checkpoints.get(checkpoint_id)

    async def record_usage(self, **kwargs):
        pass

    async def store_error(
        self,
        error_id: str,
        error_type: str,
        error_message: str,
        stack_trace: str,
        severity: str = "error",
        component: str = None,
        user_id: str = None,
        session_id: str = None,
        task_id: str = None,
        request_id: str = None,
        context: dict = None,
    ) -> dict:
        """Mock error storage - just logs in dev mode."""
        self._errors[error_id] = {
            "id": error_id,
            "error_type": error_type,
            "error_message": error_message,
            "severity": severity,
            "component": component,
            "created_at": time.time(),
        }
        logger.info(f"[MockDB] Stored error {error_id}: {error_type}")
        return self._errors[error_id]

    async def get_recent_errors(self, limit: int = 100, **kwargs) -> list:
        """Get recent errors from mock storage."""
        errors = list(self._errors.values())
        errors.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return errors[:limit]


def get_storage_clients():
    """Get Redis and Postgres clients (real or mock based on DEV_MODE)."""
    global redis_client, postgres_client

    if DEV_MODE:
        if redis_client is None:
            redis_client = MockRedisClient()
        if postgres_client is None:
            postgres_client = MockPostgresClient()
    else:
        # Import real clients
        from apps.server.src.storage.redis import RedisClient
        from apps.server.src.storage.postgres import PostgresClient

        if redis_client is None:
            redis_client = RedisClient()
        if postgres_client is None:
            postgres_client = PostgresClient()

    return redis_client, postgres_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global redis_client, postgres_client, client_handler, checkpoint_store, llm_handler

    logger.info(f"Starting gateway server: {GATEWAY_ID}")
    if DEV_MODE:
        logger.info("*** DEV MODE ENABLED - Using mock storage ***")

    # Initialize storage
    redis_client, postgres_client = get_storage_clients()

    if not DEV_MODE:
        await redis_client.connect()
        await postgres_client.connect()

    # Initialize error store
    from apps.server.src.errors.handler import init_error_store
    init_error_store(postgres_client)
    logger.info("Error store initialized")

    # Initialize components
    from apps.server.src.gateway.ws.router import SessionRouter
    from apps.server.src.gateway.ws.replay import ReplayManager
    from apps.server.src.gateway.ws.client_handler import ClientHandler

    router = SessionRouter(redis_client, GATEWAY_ID)
    replay_manager = ReplayManager(redis_client)

    # Model B: Initialize LLM handler
    try:
        from apps.server.src.gateway.ws.llm_handler import LLMHandler

        llm_handler = LLMHandler(
            redis=redis_client,
            postgres=postgres_client,
        )
        logger.info("Model B LLM handler initialized")
    except ImportError as e:
        # In production, this is a critical error that should not be silently ignored
        if DEV_MODE:
            logger.warning(f"Could not initialize Model B components (dev mode): {e}")
            llm_handler = None
        else:
            logger.error(f"Failed to initialize LLM handler: {e}")
            raise RuntimeError(
                f"Critical component initialization failed: LLMHandler. "
                f"Cannot start server without LLM capabilities. Error: {e}"
            ) from e

    client_handler = ClientHandler(
        redis=redis_client,
        postgres=postgres_client,
        gateway_id=GATEWAY_ID,
        router=router,
        replay_manager=replay_manager,
        llm_handler=llm_handler,
    )

    logger.info("Gateway server started")

    yield

    # Shutdown
    logger.info("Shutting down gateway server")

    if not DEV_MODE:
        await postgres_client.close()
        await redis_client.close()

    logger.info("Gateway server stopped")


# Create FastAPI app
app = FastAPI(
    title="Glock Gateway",
    description="Glock AI Coding Assistant - Gateway Service (Model B)",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health endpoints
@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "gateway_id": GATEWAY_ID,
        "dev_mode": DEV_MODE,
    }


@app.get("/ready")
async def ready():
    """Readiness check endpoint."""
    if DEV_MODE:
        return {"status": "ready", "gateway_id": GATEWAY_ID, "dev_mode": True}

    # Check dependencies
    try:
        await redis_client.ping()
    except Exception:
        raise HTTPException(status_code=503, detail="Redis not ready")

    return {"status": "ready", "gateway_id": GATEWAY_ID}


# WebSocket endpoints
@app.websocket("/ws/client")
async def websocket_client(
    websocket: WebSocket,
    token: str = Query(default=None, description="JWT access token"),
):
    """WebSocket endpoint for clients.

    Authentication is done via JWT token passed as query parameter:
    ws://host/ws/client?token=<jwt_token>

    In dev mode (SKIP_AUTH=1), authentication is bypassed.
    """
    # Check if authentication should be skipped
    skip_auth = os.environ.get("SKIP_AUTH", "").lower() in ("1", "true", "yes") or DEV_MODE

    if skip_auth:
        # Dev mode: accept and use dummy user
        await websocket.accept()
        user_id = "user_dev"
        user_email = "dev@glock.local"
        plan_tier = "pro"
        logger.debug("WebSocket connection accepted (auth skipped - dev mode)")
    else:
        # Production mode: verify JWT token before accepting
        from apps.server.src.gateway.api.auth import verify_websocket_token, AuthError

        try:
            token_payload = verify_websocket_token(token)
            user_id = token_payload.user_id
            user_email = token_payload.email
            plan_tier = token_payload.plan_tier

            # Accept the connection after successful authentication
            await websocket.accept()
            logger.info(f"WebSocket connection authenticated for user {user_id}")

        except AuthError as e:
            # Close connection with authentication error
            await websocket.close(code=4001, reason=f"Authentication failed: {e.message}")
            logger.warning(f"WebSocket authentication failed: {e.message} (code: {e.code})")
            return

    try:
        await client_handler.handle_connection(
            websocket=websocket,
            user_id=user_id,
            user_email=user_email,
            plan_tier=plan_tier,
        )
    except WebSocketDisconnect:
        logger.info(f"Client disconnected: {user_id}")


# REST API authentication dependency
from fastapi import Header


async def get_authenticated_user(
    authorization: str = Header(default=None, description="Bearer token"),
) -> dict:
    """Authenticate user from Authorization header.

    Returns user info dict with user_id, email, plan_tier.
    In dev mode, returns dummy user.
    """
    skip_auth = os.environ.get("SKIP_AUTH", "").lower() in ("1", "true", "yes") or DEV_MODE

    if skip_auth:
        return {
            "user_id": "user_dev",
            "email": "dev@glock.local",
            "plan_tier": "pro",
        }

    # Production: verify JWT
    from apps.server.src.gateway.api.auth import verify_token, AuthError

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
        )

    token = authorization[7:]  # Remove "Bearer " prefix

    try:
        payload = verify_token(token, require_type="access")
        return {
            "user_id": payload.user_id,
            "email": payload.email,
            "plan_tier": payload.plan_tier,
        }
    except AuthError as e:
        raise HTTPException(
            status_code=401,
            detail=e.message,
        )


# REST API endpoints
@app.get("/api/v1/sessions")
async def list_sessions(user: dict = Depends(get_authenticated_user)):
    """List user's sessions."""
    sessions = await postgres_client.get_user_sessions(user["user_id"])
    return {"sessions": sessions}


@app.get("/api/v1/sessions/{session_id}")
async def get_session(
    session_id: str,
    user: dict = Depends(get_authenticated_user),
):
    """Get session details (only if owned by user)."""
    session = await postgres_client.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Ownership check
    if session.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return session


@app.delete("/api/v1/sessions/{session_id}")
async def end_session(
    session_id: str,
    user: dict = Depends(get_authenticated_user),
):
    """End a session (only if owned by user)."""
    session = await postgres_client.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Ownership check
    if session.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    await postgres_client.end_session(session_id)
    return {"status": "ended"}


@app.get("/api/v1/tasks/{task_id}")
async def get_task(
    task_id: str,
    user: dict = Depends(get_authenticated_user),
):
    """Get task details (only if owned by user via session)."""
    task = await postgres_client.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Get session to verify ownership
    session = await postgres_client.get_session(task.get("session_id"))
    if session and session.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return task


@app.post("/api/v1/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    user: dict = Depends(get_authenticated_user),
):
    """Cancel a running task (only if owned by user via session)."""
    task = await postgres_client.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Get session to verify ownership
    session = await postgres_client.get_session(task.get("session_id"))
    if session and session.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    await postgres_client.update_task_status(task_id, "cancelled")
    return {"status": "cancelled"}


@app.get("/api/v1/profile/usage")
async def get_usage(user: dict = Depends(get_authenticated_user)):
    """Get user's usage summary."""
    return {
        "user_id": user["user_id"],
        "tokens_used": 0,
        "tasks_completed": 0,
        "sessions_count": 0,
    }


def main():
    """Run the server."""
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    uvicorn.run(
        "apps.server.src.main:app",
        host=host,
        port=port,
        reload=os.environ.get("DEBUG", "").lower() == "true",
    )


if __name__ == "__main__":
    main()
