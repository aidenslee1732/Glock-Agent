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
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware

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


class MockPostgresClient:
    """In-memory mock Postgres for dev mode."""

    def __init__(self):
        self._sessions = {}
        self._tasks = {}
        self._users = {}
        self._checkpoints = {}
        logger.info("Using MockPostgresClient (DEV MODE)")

    async def connect(self):
        pass

    async def close(self):
        pass

    async def create_session(self, user_id: str, workspace_label: str) -> str:
        session_id = f"sess_{secrets.token_hex(8)}"
        self._sessions[session_id] = {
            "id": session_id,
            "user_id": user_id,
            "workspace_label": workspace_label,
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

    # Initialize components
    from apps.server.src.gateway.ws.router import SessionRouter
    from apps.server.src.gateway.ws.replay import ReplayManager
    from apps.server.src.gateway.ws.client_handler import ClientHandler

    router = SessionRouter(redis_client, GATEWAY_ID)
    replay_manager = ReplayManager(redis_client)

    # Model B: Initialize LLM handler and checkpoint store
    try:
        from apps.server.src.gateway.ws.llm_handler import LLMHandler
        from apps.server.src.storage.checkpoint_store import ContextCheckpointStore
        from apps.server.src.context.rehydrator import ContextRehydrator

        master_key = os.environ.get("CONTEXT_MASTER_KEY", "0" * 64)
        checkpoint_store = ContextCheckpointStore(
            db=postgres_client,
            master_key=bytes.fromhex(master_key),
        )

        rehydrator = ContextRehydrator(checkpoint_store=checkpoint_store)

        llm_handler = LLMHandler(
            checkpoint_store=checkpoint_store,
            rehydrator=rehydrator,
            postgres=postgres_client,
        )
        logger.info("Model B LLM handler initialized")
    except ImportError as e:
        logger.warning(f"Could not initialize Model B components: {e}")
        llm_handler = None

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
async def websocket_client(websocket: WebSocket):
    """WebSocket endpoint for clients."""
    await websocket.accept()

    # Authentication
    skip_auth = os.environ.get("SKIP_AUTH", "").lower() in ("1", "true", "yes") or DEV_MODE

    if skip_auth:
        # Dev mode: use dummy user
        user_id = "user_dev"
        user_email = "dev@glock.local"
        plan_tier = "pro"
    else:
        # TODO: Implement proper JWT authentication
        user_id = "user_test123"
        user_email = "test@example.com"
        plan_tier = "pro"

    try:
        await client_handler.handle_connection(
            websocket=websocket,
            user_id=user_id,
            user_email=user_email,
            plan_tier=plan_tier,
        )
    except WebSocketDisconnect:
        logger.info("Client disconnected")


# REST API endpoints
@app.get("/api/v1/sessions")
async def list_sessions(user_id: str = "user_dev"):
    """List user's sessions."""
    sessions = await postgres_client.get_user_sessions(user_id)
    return {"sessions": sessions}


@app.get("/api/v1/sessions/{session_id}")
async def get_session(session_id: str):
    """Get session details."""
    session = await postgres_client.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.delete("/api/v1/sessions/{session_id}")
async def end_session(session_id: str):
    """End a session."""
    await postgres_client.end_session(session_id)
    return {"status": "ended"}


@app.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str):
    """Get task details."""
    task = await postgres_client.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/api/v1/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Cancel a running task."""
    await postgres_client.update_task_status(task_id, "cancelled")
    return {"status": "cancelled"}


@app.get("/api/v1/profile/usage")
async def get_usage(user_id: str = "user_dev"):
    """Get user's usage summary."""
    return {
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
