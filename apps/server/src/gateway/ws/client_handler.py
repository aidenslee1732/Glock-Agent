"""WebSocket handler for client connections."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional
from uuid import uuid4

from packages.shared_protocol.types import (
    MessageEnvelope,
    MessageType,
    SessionReadyPayload,
    SessionCaps,
    ResumeFromSeqPayload,
    SessionSyncPayload,
    TaskState,
    generate_session_id,
    generate_client_id,
)
from apps.server.src.storage.redis import RedisClient
from apps.server.src.storage.postgres import PostgresClient
from apps.server.src.gateway.protocol import GatewayProtocol, ClientSanitizer

from .router import SessionRouter
from .replay import ReplayManager

# Optional LLM handler import (may fail if dependencies missing)
try:
    from .llm_handler import LLMHandler
    HAS_LLM_HANDLER = True
except ImportError:
    LLMHandler = None
    HAS_LLM_HANDLER = False

logger = logging.getLogger(__name__)

# Configuration
HEARTBEAT_INTERVAL_MS = 30000
HEARTBEAT_TIMEOUT_MS = 90000
SESSION_IDLE_TIMEOUT_MS = 3600000
MAX_SESSIONS_PER_USER = 10
RATE_LIMIT_PER_MINUTE = 60
RATE_LIMIT_PER_HOUR = 1000


class ClientHandler:
    """Handles WebSocket connections from clients.

    Responsibilities:
    - Session lifecycle (start, resume, end)
    - Message routing to runtime via relay
    - Heartbeat/presence management
    - Rate limiting
    """

    def __init__(
        self,
        redis: RedisClient,
        postgres: PostgresClient,
        gateway_id: str,
        router: SessionRouter,
        replay_manager: ReplayManager,
        llm_handler: Optional[Any] = None,
    ):
        self.redis = redis
        self.postgres = postgres
        self.gateway_id = gateway_id
        self.router = router
        self.replay_manager = replay_manager

        # LLM handler is optional (for Model B)
        if llm_handler is not None:
            self.llm_handler = llm_handler
        elif HAS_LLM_HANDLER:
            self.llm_handler = LLMHandler(redis, postgres)
        else:
            self.llm_handler = None
            logger.warning("LLMHandler not available - LLM requests will fail")

        self.sanitizer = ClientSanitizer()

        # Active connections: conn_id → connection state
        self._connections: dict[str, dict[str, Any]] = {}

        # Heartbeat tasks
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}

    async def handle_connection(
        self,
        websocket: Any,
        user_id: str,
        user_email: str,
        plan_tier: str = "free",
    ) -> None:
        """Handle new client WebSocket connection.

        Args:
            websocket: WebSocket connection
            user_id: Authenticated user ID
            user_email: User's email
            plan_tier: User's plan tier
        """
        conn_id = str(uuid4())
        session_id: Optional[str] = None

        try:
            # Store connection state
            self._connections[conn_id] = {
                "websocket": websocket,
                "user_id": user_id,
                "user_email": user_email,
                "plan_tier": plan_tier,
                "session_id": None,
                "connected_at": time.time(),
                "last_message_at": time.time(),
                "server_seq": 0,
                "client_ack": 0,
            }

            logger.info(f"Client connected: conn={conn_id}, user={user_id}")

            # Message loop (FastAPI WebSocket)
            from starlette.websockets import WebSocketDisconnect
            while True:
                try:
                    message = await websocket.receive_text()
                    await self._handle_message(conn_id, message)
                except WebSocketDisconnect:
                    logger.info(f"Client disconnected normally: conn={conn_id}")
                    break

        except Exception as e:
            logger.error(f"Client connection error: conn={conn_id}, error={e}")

        finally:
            # Cleanup
            await self._handle_disconnect(conn_id)

    async def _handle_message(self, conn_id: str, raw_message: str | bytes) -> None:
        """Handle incoming client message."""
        conn = self._connections.get(conn_id)
        if not conn:
            return

        try:
            msg = GatewayProtocol.parse_message(raw_message)
            conn["last_message_at"] = time.time()

            # Update client ack
            if msg.ack > 0:
                conn["client_ack"] = msg.ack

            # Route by message type
            handlers = {
                MessageType.SESSION_START: self._handle_session_start,
                MessageType.RESUME_REQUEST: self._handle_resume_request,
                MessageType.SESSION_RESUME: self._handle_session_resume,
                MessageType.SESSION_END: self._handle_session_end,
                MessageType.HEARTBEAT: self._handle_heartbeat,
                # Model B: LLM Proxy messages (handled directly, no runtime relay)
                MessageType.LLM_REQUEST: self._handle_llm_request,
                MessageType.LLM_CANCEL: self._handle_llm_cancel,
                MessageType.CONTEXT_CHECKPOINT: self._handle_context_checkpoint,
                # Legacy: Task messages (still supported for compatibility)
                MessageType.TASK_START: self._handle_task_start,
                MessageType.TOOL_RESULT: self._handle_tool_result,
                MessageType.TOOL_APPROVAL_RESPONSE: self._handle_tool_approval,
                MessageType.VALIDATION_RESULT: self._handle_validation_result,
                MessageType.CANCEL_REQUESTED: self._handle_cancel,
            }

            handler = handlers.get(msg.type)
            if handler:
                await handler(conn_id, msg)
            else:
                logger.warning(f"Unknown message type: {msg.type}")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from client: {e}")
        except Exception as e:
            import traceback
            logger.error(f"Error handling client message: {e}\n{traceback.format_exc()}")

    async def _handle_session_start(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle session_start message."""
        conn = self._connections.get(conn_id)
        if not conn:
            return

        user_id = conn["user_id"]
        websocket = conn["websocket"]

        # Check rate limits
        allowed, reason = await self.redis.check_rate_limit(
            user_id,
            RATE_LIMIT_PER_MINUTE,
            RATE_LIMIT_PER_HOUR,
        )
        if not allowed:
            await self._send_error(websocket, "", "rate_limit", reason)
            return

        # Check session limits
        session_count = await self.redis.count_user_sessions(user_id)
        if session_count >= MAX_SESSIONS_PER_USER:
            await self._send_error(
                websocket,
                "",
                "session_limit_exceeded",
                f"Maximum {MAX_SESSIONS_PER_USER} sessions allowed",
            )
            return

        # Create session
        session_id = generate_session_id()
        client_id = generate_client_id()

        payload = msg.payload
        logger.debug(f"Session start payload type: {type(payload)}, value: {payload}")

        # Ensure payload is a dict
        if isinstance(payload, str):
            import json as json_module
            payload = json_module.loads(payload)

        await self.postgres.create_session(
            session_id=session_id,
            user_id=user_id,
            client_id=client_id,
            workspace_label=payload.get("workspace_label"),
            repo_fingerprint=payload.get("repo_fingerprint"),
            branch_name=payload.get("branch_name"),
        )

        # Track session
        await self.redis.add_user_session(user_id, session_id)
        conn["session_id"] = session_id
        conn["client_id"] = client_id

        # Register with router
        await self.router.attach_client(session_id, conn_id)

        # Initialize sequence numbers and session state
        conn["server_seq"] = 0
        await self.redis.hset(
            f"sess:{session_id}:state",
            mapping={
                "status": "active",
                "gateway_id": self.gateway_id,
                "user_id": user_id,
                "created_at": str(time.time()),
                "last_activity": str(time.time()),
                "turn_count": "0",
                "total_tokens": "0",
            },
        )

        # Model B: No runtime allocation needed - client does all orchestration

        # Start heartbeat
        self._start_heartbeat(conn_id, session_id)

        # Send session_ready
        await self._send_session_ready(
            websocket,
            session_id,
            user_id,
            conn["plan_tier"],
        )

        # Increment rate counters
        await self.redis.increment_rate_counters(user_id)

        logger.info(f"Session started: session={session_id}, user={user_id}")

    async def _handle_resume_request(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle resume_request message."""
        conn = self._connections.get(conn_id)
        if not conn:
            return

        user_id = conn["user_id"]
        websocket = conn["websocket"]
        payload = msg.payload

        session_id = payload.get("session_id")
        last_server_seq_seen = payload.get("last_server_seq_seen", 0)
        last_client_seq_sent = payload.get("last_client_seq_sent", 0)

        # Verify session ownership
        session = await self.postgres.get_session(session_id)
        if not session or str(session["user_id"]) != user_id:
            await self._send_error(
                websocket,
                session_id or "",
                "session_not_found",
                "Session not found or access denied",
            )
            return

        if session["status"] == "ended":
            await self._send_error(
                websocket,
                session_id,
                "session_ended",
                "Session has ended",
            )
            return

        # Get replay messages
        replay_messages, resume_seq = await self.replay_manager.handle_reconnect(
            session_id,
            last_server_seq_seen,
            last_client_seq_sent,
        )

        # Update connection state
        conn["session_id"] = session_id
        conn["server_seq"] = resume_seq - 1

        # Re-register with router (Model B: no relay needed)
        await self.router.attach_client(session_id, conn_id)

        # Update session status
        await self.postgres.update_session_status(session_id, "running")
        await self.redis.set_session_state(session_id, {
            "status": "connected",
            "gateway_id": self.gateway_id,
        })

        # Get task state
        task_state = None
        if session.get("active_task_id"):
            task = await self.postgres.get_task(str(session["active_task_id"]))
            if task and task["status"] in ("running", "validating", "retrying"):
                task_state = TaskState(
                    task_id=f"task_{task['id']}",
                    status=task["status"],
                )

        # Start heartbeat
        self._start_heartbeat(conn_id, session_id)

        # Send resume_from_seq
        seq = self._next_seq(conn_id)
        resume_msg = MessageEnvelope.create(
            msg_type=MessageType.RESUME_FROM_SEQ,
            session_id=session_id,
            payload=ResumeFromSeqPayload(
                resume_seq=resume_seq,
                replay_messages=replay_messages,
                task_state=task_state,
            ).to_dict(),
            seq=seq,
        )
        await websocket.send_text(json.dumps(resume_msg.to_dict()))

        logger.info(
            f"Session resumed: session={session_id}, "
            f"replay_count={len(replay_messages)}"
        )

    async def _handle_session_end(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle session_end message."""
        conn = self._connections.get(conn_id)
        if not conn:
            return

        session_id = conn.get("session_id")
        if not session_id:
            return

        user_id = conn["user_id"]

        # Model B: No runtime to notify - client handles all orchestration

        # Update database
        await self.postgres.end_session(session_id)
        await self.redis.remove_user_session(user_id, session_id)

        # Cleanup routing
        await self.router.cleanup_session(session_id)

        # Send confirmation
        websocket = conn["websocket"]
        seq = self._next_seq(conn_id)
        end_msg = MessageEnvelope.create(
            msg_type=MessageType.SESSION_END,
            session_id=session_id,
            payload={"ended": True},
            seq=seq,
        )
        await websocket.send_text(json.dumps(end_msg.to_dict()))

        logger.info(f"Session ended: session={session_id}")

    async def _handle_heartbeat(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle heartbeat message."""
        conn = self._connections.get(conn_id)
        if not conn:
            return

        session_id = conn.get("session_id")
        if session_id:
            await self.router.update_presence(session_id, "connected")
            await self.postgres.touch_session(session_id)

        # Send heartbeat_ack
        websocket = conn["websocket"]
        seq = self._next_seq(conn_id)
        ack_msg = MessageEnvelope.create(
            msg_type=MessageType.HEARTBEAT_ACK,
            session_id=session_id or "",
            payload={"server_time_ms": int(time.time() * 1000)},
            seq=seq,
            ack=conn.get("client_ack", 0),
        )
        await websocket.send_text(json.dumps(ack_msg.to_dict()))

    # =========================================================================
    # Model B: LLM Proxy Handlers
    # =========================================================================

    async def _handle_llm_request(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle LLM request from client (Model B)."""
        logger.info(f"LLM request received: conn={conn_id}")
        conn = self._connections.get(conn_id)
        if not conn:
            return

        session_id = conn.get("session_id")
        if not session_id:
            await self._send_error(
                conn["websocket"],
                "",
                "no_session",
                "No active session",
            )
            return

        user_id = conn["user_id"]
        websocket = conn["websocket"]

        # Create send callback for streaming responses
        async def send_callback(data: str) -> None:
            await websocket.send_text(data)

        # Handle LLM request via LLM handler
        await self.llm_handler.handle_llm_request(
            session_id=session_id,
            user_id=user_id,
            payload=msg.payload,
            send_callback=send_callback,
        )

    async def _handle_llm_cancel(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle LLM cancel request from client (Model B)."""
        conn = self._connections.get(conn_id)
        if not conn:
            return

        session_id = conn.get("session_id")
        if not session_id:
            return

        await self.llm_handler.handle_llm_cancel(
            session_id=session_id,
            payload=msg.payload,
        )

    async def _handle_context_checkpoint(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle context checkpoint from client (Model B)."""
        conn = self._connections.get(conn_id)
        if not conn:
            return

        session_id = conn.get("session_id")
        if not session_id:
            return

        user_id = conn["user_id"]
        websocket = conn["websocket"]

        async def send_callback(data: str) -> None:
            await websocket.send_text(data)

        await self.llm_handler.handle_context_checkpoint(
            session_id=session_id,
            user_id=user_id,
            payload=msg.payload,
            send_callback=send_callback,
        )

    async def _handle_session_resume(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle session resume request (Model B enhanced)."""
        conn = self._connections.get(conn_id)
        if not conn:
            return

        user_id = conn["user_id"]
        websocket = conn["websocket"]
        payload = msg.payload

        session_id = payload.get("session_id")
        client_state_hash = payload.get("client_state_hash", "")
        expected_context_ref = payload.get("expected_context_ref", "")

        # Verify session ownership
        session = await self.postgres.get_session(session_id)
        if not session or str(session["user_id"]) != user_id:
            await self._send_error(
                websocket,
                session_id or "",
                "session_not_found",
                "Session not found or access denied",
            )
            return

        if session["status"] == "ended":
            await self._send_error(
                websocket,
                session_id,
                "session_ended",
                "Session has ended",
            )
            return

        # Get session state from Redis
        state = await self.redis.hgetall(f"sess:{session_id}:state")

        last_context_ref = state.get("last_context_ref", "")
        turn_count = int(state.get("turn_count", "0"))
        total_tokens = int(state.get("total_tokens", "0"))
        workspace_hash = state.get("workspace_hash", "")

        # Check if client state is stale
        needs_resync = (
            expected_context_ref and
            expected_context_ref != last_context_ref
        )

        # Update connection state
        conn["session_id"] = session_id

        # Re-register with router
        await self.router.attach_client(session_id, conn_id)

        # Update session status
        await self.postgres.update_session_status(session_id, "running")
        await self.redis.hset(
            f"sess:{session_id}:state",
            mapping={
                "status": "active",
                "gateway_id": self.gateway_id,
                "last_activity": str(time.time()),
            },
        )

        # Start heartbeat
        self._start_heartbeat(conn_id, session_id)

        # Send session_sync response
        status = "stale" if needs_resync else "resumed"
        sync_payload = SessionSyncPayload(
            session_id=session_id,
            status=status,
            last_context_ref=last_context_ref,
            turn_count=turn_count,
            task_status=session.get("status"),
            needs_resync=needs_resync,
            resync_from=last_context_ref if needs_resync else None,
            total_tokens=total_tokens,
            workspace_hash=workspace_hash,
        )

        seq = self._next_seq(conn_id)
        sync_msg = MessageEnvelope.create(
            msg_type=MessageType.SESSION_SYNC,
            session_id=session_id,
            payload=sync_payload.to_dict(),
            seq=seq,
        )
        await websocket.send_text(json.dumps(sync_msg.to_dict()))

        logger.info(
            f"Session resumed (Model B): session={session_id}, "
            f"status={status}, context_ref={last_context_ref}"
        )

    # =========================================================================
    # Legacy Task Handlers (for compatibility)
    # =========================================================================

    async def _handle_task_start(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle task start - in Model B, client orchestrates tasks."""
        conn = self._connections.get(conn_id)
        if not conn:
            return

        session_id = conn.get("session_id")
        if not session_id:
            await self._send_error(
                conn["websocket"],
                "",
                "no_session",
                "No active session",
            )
            return

        # In Model B, we just acknowledge task start
        # The client will orchestrate using LLM_REQUEST messages
        logger.info(f"Task started (Model B): session={session_id}")

    async def _handle_tool_result(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle tool result - in Model B, client includes in context delta."""
        # Tool results are now sent as part of LLM_REQUEST delta
        logger.debug("Tool result received (Model B) - included in next LLM request")

    async def _handle_tool_approval(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle tool approval - in Model B, client handles approvals locally."""
        logger.debug("Tool approval received (Model B) - handled locally by client")

    async def _handle_validation_result(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle validation result - in Model B, client handles validation."""
        logger.debug("Validation result received (Model B) - handled locally by client")

    async def _handle_cancel(
        self,
        conn_id: str,
        msg: MessageEnvelope,
    ) -> None:
        """Handle cancel request - redirect to LLM cancel."""
        await self._handle_llm_cancel(conn_id, msg)

    async def _handle_disconnect(self, conn_id: str) -> None:
        """Handle client disconnection."""
        conn = self._connections.pop(conn_id, None)
        if not conn:
            return

        session_id = conn.get("session_id")
        user_id = conn["user_id"]

        # Stop heartbeat
        self._stop_heartbeat(conn_id)

        if session_id:
            # Update routing
            await self.router.detach_client(session_id)

            # Update session status (Model B: session remains pauseable)
            await self.postgres.update_session_status(session_id, "paused")
            await self.redis.hset(
                f"sess:{session_id}:state",
                mapping={
                    "status": "paused",
                    "last_activity": str(time.time()),
                },
            )

        logger.info(f"Client disconnected: conn={conn_id}, session={session_id}")

    def _next_seq(self, conn_id: str) -> int:
        """Get next server sequence number for connection."""
        conn = self._connections.get(conn_id)
        if conn:
            conn["server_seq"] = conn.get("server_seq", 0) + 1
            return conn["server_seq"]
        return 0

    def _start_heartbeat(self, conn_id: str, session_id: str) -> None:
        """Start heartbeat task for connection."""
        if conn_id in self._heartbeat_tasks:
            self._heartbeat_tasks[conn_id].cancel()

        async def heartbeat_loop():
            while conn_id in self._connections:
                await asyncio.sleep(HEARTBEAT_INTERVAL_MS / 1000)
                conn = self._connections.get(conn_id)
                if not conn:
                    break

                # Check for timeout
                last_msg = conn.get("last_message_at", 0)
                if time.time() - last_msg > HEARTBEAT_TIMEOUT_MS / 1000:
                    logger.warning(
                        f"Heartbeat timeout: conn={conn_id}, session={session_id}"
                    )
                    websocket = conn.get("websocket")
                    if websocket:
                        await websocket.close()
                    break

        self._heartbeat_tasks[conn_id] = asyncio.create_task(heartbeat_loop())

    def _stop_heartbeat(self, conn_id: str) -> None:
        """Stop heartbeat task for connection."""
        task = self._heartbeat_tasks.pop(conn_id, None)
        if task:
            task.cancel()

    async def _send_session_ready(
        self,
        websocket: Any,
        session_id: str,
        user_id: str,
        plan_tier: str,
    ) -> None:
        """Send session_ready message."""
        caps = SessionCaps(
            max_concurrent_tasks=3 if plan_tier != "free" else 1,
            max_retries=3 if plan_tier != "free" else 2,
        )
        payload = SessionReadyPayload(
            session_id=session_id,
            user_id=user_id,
            plan_tier=plan_tier,
            session_caps=caps,
            server_time_ms=int(time.time() * 1000),
        )
        msg = MessageEnvelope.create(
            msg_type=MessageType.SESSION_READY,
            session_id=session_id,
            payload=payload.to_dict(),
            seq=1,
        )
        await websocket.send_text(json.dumps(msg.to_dict()))

    async def _send_error(
        self,
        websocket: Any,
        session_id: str,
        error_code: str,
        message: str,
    ) -> None:
        """Send error message to client."""
        msg = GatewayProtocol.create_error(session_id, error_code, message)
        await websocket.send_text(json.dumps(msg.to_dict()))
