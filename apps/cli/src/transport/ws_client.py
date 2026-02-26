"""WebSocket client for Glock gateway."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Callable, Optional

import websockets
from websockets.client import WebSocketClientProtocol

from packages.shared_protocol.types import (
    MessageEnvelope,
    MessageType,
    generate_client_id,
    LLMDeltaPayload,
    LLMResponseEndPayload,
    LLMErrorPayload,
    ContextCheckpointAckPayload,
    SessionSyncPayload,
)

logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    """WebSocket connection state."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    RESUMING = "resuming"


@dataclass
class ConnectionConfig:
    """WebSocket connection configuration."""
    server_url: str
    auth_token: Optional[str] = None
    heartbeat_interval_ms: int = 30000
    heartbeat_timeout_ms: int = 90000
    reconnect_delay_ms: int = 1000
    max_reconnect_delay_ms: int = 30000
    max_reconnect_attempts: int = 10


class WebSocketClient:
    """WebSocket client for connecting to Glock gateway.

    Handles:
    - Connection lifecycle
    - Heartbeat/keepalive
    - Reconnection with resume
    - Sequence number tracking
    - Model B LLM streaming
    """

    def __init__(self, config: ConnectionConfig):
        self.config = config
        self.state = ConnectionState.DISCONNECTED

        self._ws: Optional[WebSocketClientProtocol] = None
        self._client_id = generate_client_id()
        self._session_id: Optional[str] = None

        # Sequence tracking
        self._client_seq = 0
        self._server_ack = 0
        self._last_server_seq = 0

        # Message handlers
        self._message_handlers: dict[MessageType, Callable] = {}

        # Model B: Streaming handlers for LLM responses
        self._llm_delta_handler: Optional[Callable[[LLMDeltaPayload], None]] = None
        self._llm_response_handler: Optional[Callable[[LLMResponseEndPayload], None]] = None
        self._llm_error_handler: Optional[Callable[[LLMErrorPayload], None]] = None
        self._checkpoint_ack_handler: Optional[Callable[[ContextCheckpointAckPayload], None]] = None
        self._session_sync_handler: Optional[Callable[[SessionSyncPayload], None]] = None

        # Model B: Pending request tracking
        self._pending_requests: dict[str, asyncio.Future] = {}

        # Background tasks
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None

        # Events
        self._connected_event = asyncio.Event()
        self._disconnected_event = asyncio.Event()

    @property
    def session_id(self) -> Optional[str]:
        """Get current session ID."""
        return self._session_id

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self.state == ConnectionState.CONNECTED

    def on_message(self, msg_type: MessageType) -> Callable:
        """Decorator to register message handler."""
        def decorator(handler: Callable) -> Callable:
            self._message_handlers[msg_type] = handler
            return handler
        return decorator

    # =========================================================================
    # Model B: LLM Streaming Handlers
    # =========================================================================

    def on_llm_delta(self, handler: Callable[[LLMDeltaPayload], None]) -> None:
        """Register handler for LLM streaming deltas."""
        self._llm_delta_handler = handler

    def on_llm_response(self, handler: Callable[[LLMResponseEndPayload], None]) -> None:
        """Register handler for LLM response completion."""
        self._llm_response_handler = handler

    def on_llm_error(self, handler: Callable[[LLMErrorPayload], None]) -> None:
        """Register handler for LLM errors."""
        self._llm_error_handler = handler

    def on_checkpoint_ack(self, handler: Callable[[ContextCheckpointAckPayload], None]) -> None:
        """Register handler for checkpoint acknowledgments."""
        self._checkpoint_ack_handler = handler

    def on_session_sync(self, handler: Callable[[SessionSyncPayload], None]) -> None:
        """Register handler for session sync (resume)."""
        self._session_sync_handler = handler

    async def connect(self, workspace_label: str = "") -> str:
        """Connect to gateway and start a new session.

        Args:
            workspace_label: Label for the workspace

        Returns:
            Session ID

        Raises:
            ConnectionError: If connection fails
        """
        self.state = ConnectionState.CONNECTING

        try:
            # Connect WebSocket
            self._ws = await websockets.connect(
                f"{self.config.server_url}/ws/client",
                additional_headers=self._get_headers(),
            )

            # Start receive task
            self._receive_task = asyncio.create_task(self._receive_loop())

            # Send session_start
            await self._send(
                MessageType.SESSION_START,
                {
                    "workspace_label": workspace_label,
                    "client_version": "1.0.0",
                    "capabilities": ["docker", "git_worktree"],
                },
            )

            # Wait for session_ready
            await asyncio.wait_for(
                self._connected_event.wait(),
                timeout=30.0,
            )

            # Start heartbeat
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            self.state = ConnectionState.CONNECTED
            logger.info(f"Connected to gateway, session={self._session_id}")

            return self._session_id

        except Exception as e:
            self.state = ConnectionState.DISCONNECTED
            raise ConnectionError(f"Failed to connect: {e}") from e

    async def resume(self, session_id: str) -> None:
        """Resume a previous session.

        Args:
            session_id: Session ID to resume

        Raises:
            ConnectionError: If resume fails
        """
        self.state = ConnectionState.RESUMING
        self._session_id = session_id

        try:
            # Connect WebSocket
            self._ws = await websockets.connect(
                f"{self.config.server_url}/ws/client",
                additional_headers=self._get_headers(),
            )

            # Start receive task
            self._receive_task = asyncio.create_task(self._receive_loop())

            # Send resume_request
            await self._send(
                MessageType.RESUME_REQUEST,
                {
                    "session_id": session_id,
                    "last_server_seq_seen": self._last_server_seq,
                    "last_client_seq_sent": self._client_seq,
                },
            )

            # Wait for resume_from_seq
            await asyncio.wait_for(
                self._connected_event.wait(),
                timeout=30.0,
            )

            # Start heartbeat
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            self.state = ConnectionState.CONNECTED
            logger.info(f"Resumed session: {session_id}")

        except Exception as e:
            self.state = ConnectionState.DISCONNECTED
            raise ConnectionError(f"Failed to resume: {e}") from e

    async def disconnect(self) -> None:
        """Disconnect from gateway."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            # Send session_end
            try:
                await self._send(MessageType.SESSION_END, {})
            except Exception:
                pass

            await self._ws.close()

        self.state = ConnectionState.DISCONNECTED
        self._disconnected_event.set()
        logger.info("Disconnected from gateway")

    async def send(
        self,
        msg_type: MessageType,
        payload: dict[str, Any],
        task_id: Optional[str] = None,
    ) -> None:
        """Send a message to the gateway.

        Args:
            msg_type: Message type
            payload: Message payload
            task_id: Optional task ID
        """
        await self._send(msg_type, payload, task_id)

    async def _send(
        self,
        msg_type: MessageType,
        payload: dict[str, Any],
        task_id: Optional[str] = None,
    ) -> None:
        """Internal send implementation."""
        if not self._ws:
            raise ConnectionError("Not connected")

        self._client_seq += 1

        msg = MessageEnvelope.create(
            msg_type=msg_type,
            session_id=self._session_id or "",
            payload=payload,
            seq=self._client_seq,
            ack=self._last_server_seq,
            client_id=self._client_id,
            task_id=task_id,
        )

        await self._ws.send(json.dumps(msg.to_dict()))
        logger.debug(f"Sent: type={msg_type.value}, seq={self._client_seq}")

    async def _receive_loop(self) -> None:
        """Background loop to receive messages."""
        while True:
            try:
                raw = await self._ws.recv()
                data = json.loads(raw)
                msg = MessageEnvelope.from_dict(data)

                # Update sequence tracking
                self._last_server_seq = msg.seq
                if msg.ack > self._server_ack:
                    self._server_ack = msg.ack

                # Handle message
                await self._handle_message(msg)

            except websockets.ConnectionClosed:
                logger.warning("Connection closed")
                await self._handle_disconnect()
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Receive error: {e}")

    async def _handle_message(self, msg: MessageEnvelope) -> None:
        """Handle received message."""
        logger.debug(f"Received: type={msg.type}, seq={msg.seq}")

        # Handle session lifecycle
        if msg.type == MessageType.SESSION_READY:
            self._session_id = msg.payload.get("session_id")
            self._connected_event.set()
            return

        if msg.type == MessageType.RESUME_FROM_SEQ:
            # Process replay messages
            for replay_msg in msg.payload.get("replay_messages", []):
                replay_envelope = MessageEnvelope.from_dict(replay_msg)
                await self._dispatch_to_handler(replay_envelope)
            self._connected_event.set()
            return

        if msg.type == MessageType.HEARTBEAT_ACK:
            return  # Just update sequences (already done)

        if msg.type == MessageType.SESSION_ERROR:
            logger.error(f"Session error: {msg.payload}")
            return

        # =====================================================================
        # Model B: Handle LLM streaming messages
        # =====================================================================
        if msg.type == MessageType.LLM_DELTA:
            await self._handle_llm_delta(msg)
            return

        if msg.type == MessageType.LLM_RESPONSE_END:
            await self._handle_llm_response_end(msg)
            return

        if msg.type == MessageType.LLM_ERROR:
            await self._handle_llm_error(msg)
            return

        if msg.type == MessageType.CONTEXT_CHECKPOINT_ACK:
            await self._handle_checkpoint_ack(msg)
            return

        if msg.type == MessageType.SESSION_SYNC:
            await self._handle_session_sync(msg)
            return

        # Dispatch to registered handler
        await self._dispatch_to_handler(msg)

    async def _dispatch_to_handler(self, msg: MessageEnvelope) -> None:
        """Dispatch message to registered handler."""
        handler = self._message_handlers.get(msg.type)
        if handler:
            try:
                result = handler(msg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Handler error for {msg.type}: {e}")

    async def _heartbeat_loop(self) -> None:
        """Background heartbeat loop."""
        interval = self.config.heartbeat_interval_ms / 1000

        while True:
            try:
                await asyncio.sleep(interval)
                await self._send(MessageType.HEARTBEAT, {})
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

    async def _handle_disconnect(self) -> None:
        """Handle unexpected disconnection."""
        old_state = self.state
        self.state = ConnectionState.RECONNECTING

        if old_state == ConnectionState.CONNECTED and self._session_id:
            # Attempt reconnection
            await self._reconnect()

    async def _reconnect(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        delay = self.config.reconnect_delay_ms / 1000
        max_delay = self.config.max_reconnect_delay_ms / 1000

        for attempt in range(self.config.max_reconnect_attempts):
            logger.info(f"Reconnection attempt {attempt + 1}")

            try:
                await self.resume(self._session_id)
                return
            except Exception as e:
                logger.warning(f"Reconnection failed: {e}")

            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

        logger.error("Max reconnection attempts reached")
        self.state = ConnectionState.DISCONNECTED
        self._disconnected_event.set()

    def _get_headers(self) -> dict[str, str]:
        """Get headers for WebSocket connection."""
        headers = {}
        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"
        return headers

    async def wait_for_disconnect(self) -> None:
        """Wait for disconnection."""
        await self._disconnected_event.wait()

    # =========================================================================
    # Model B: LLM Request/Response Methods
    # =========================================================================

    async def send_llm_request(
        self,
        request_id: str,
        context_ref: Optional[str],
        delta: dict[str, Any],
        context_pack: dict[str, Any],
        tools: list[dict[str, Any]],
        model_tier: str = "standard",
        max_tokens: int = 8192,
        temperature: float = 0.7,
    ) -> None:
        """Send an LLM request to the server.

        Args:
            request_id: Unique request identifier
            context_ref: Reference to stored checkpoint (or None for first request)
            delta: Context delta since last checkpoint
            context_pack: Stable context (summary, facts, slices)
            tools: Tool definitions
            model_tier: Model tier (fast/standard/advanced)
            max_tokens: Maximum tokens for response
            temperature: Sampling temperature
        """
        await self._send(
            MessageType.LLM_REQUEST,
            {
                "request_id": request_id,
                "context_ref": context_ref,
                "delta": delta,
                "context_pack": context_pack,
                "tools": tools,
                "model_tier": model_tier,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )

    async def send_llm_cancel(self, request_id: str, reason: str = "user_cancelled") -> None:
        """Cancel an in-progress LLM request.

        Args:
            request_id: Request ID to cancel
            reason: Cancellation reason
        """
        await self._send(
            MessageType.LLM_CANCEL,
            {
                "request_id": request_id,
                "reason": reason,
            },
        )

    async def send_context_checkpoint(
        self,
        checkpoint_id: str,
        parent_id: Optional[str],
        encrypted_payload: bytes,
        payload_hash: str,
        token_count: int,
        is_full: bool = False,
    ) -> None:
        """Send a context checkpoint to the server.

        Args:
            checkpoint_id: Unique checkpoint ID
            parent_id: Parent checkpoint ID (for delta chains)
            encrypted_payload: Client-encrypted payload
            payload_hash: Hash for verification
            token_count: Token count of the context
            is_full: Whether this is a full snapshot
        """
        import base64

        await self._send(
            MessageType.CONTEXT_CHECKPOINT,
            {
                "checkpoint_id": checkpoint_id,
                "parent_id": parent_id,
                "encrypted_payload_base64": base64.b64encode(encrypted_payload).decode(),
                "payload_hash": payload_hash,
                "token_count": token_count,
                "is_full": is_full,
            },
        )

    async def send_session_resume(
        self,
        session_id: str,
        client_state_hash: str,
        expected_context_ref: Optional[str] = None,
    ) -> None:
        """Send session resume request.

        Args:
            session_id: Session ID to resume
            client_state_hash: Hash of local state for verification
            expected_context_ref: Last known checkpoint reference
        """
        await self._send(
            MessageType.SESSION_RESUME,
            {
                "session_id": session_id,
                "client_state_hash": client_state_hash,
                "expected_context_ref": expected_context_ref,
            },
        )

    # =========================================================================
    # Model B: Internal Message Handlers
    # =========================================================================

    async def _handle_llm_delta(self, msg: MessageEnvelope) -> None:
        """Handle LLM streaming delta."""
        payload = LLMDeltaPayload(
            request_id=msg.payload.get("request_id", ""),
            delta_type=msg.payload.get("delta_type", "text"),
            content=msg.payload.get("content", ""),
            index=msg.payload.get("index", 0),
        )

        if self._llm_delta_handler:
            try:
                result = self._llm_delta_handler(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"LLM delta handler error: {e}")

    async def _handle_llm_response_end(self, msg: MessageEnvelope) -> None:
        """Handle LLM response completion."""
        # Parse tool calls if present
        tool_calls = []
        if msg.payload.get("tool_calls"):
            from packages.shared_protocol.types import ToolCallResult
            tool_calls = [
                ToolCallResult(
                    tool_call_id=r.get("tool_call_id", ""),
                    tool_name=r.get("tool_name", ""),
                    arguments=r.get("arguments", {}),
                )
                for r in msg.payload.get("tool_calls", [])
            ]

        payload = LLMResponseEndPayload(
            request_id=msg.payload.get("request_id", ""),
            new_context_ref=msg.payload.get("new_context_ref", ""),
            finish_reason=msg.payload.get("finish_reason", "stop"),
            input_tokens=msg.payload.get("input_tokens", 0),
            output_tokens=msg.payload.get("output_tokens", 0),
            tool_calls=tool_calls,
            content=msg.payload.get("content", ""),
        )

        # Complete pending request future if exists
        request_id = payload.request_id
        if request_id in self._pending_requests:
            self._pending_requests[request_id].set_result(payload)
            del self._pending_requests[request_id]

        if self._llm_response_handler:
            try:
                result = self._llm_response_handler(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"LLM response handler error: {e}")

    async def _handle_llm_error(self, msg: MessageEnvelope) -> None:
        """Handle LLM error."""
        payload = LLMErrorPayload(
            request_id=msg.payload.get("request_id", ""),
            error_code=msg.payload.get("error_code", "unknown"),
            error_message=msg.payload.get("error_message", "Unknown error"),
            retryable=msg.payload.get("retryable", False),
            retry_after_ms=msg.payload.get("retry_after_ms"),
        )

        # Complete pending request future with exception
        request_id = payload.request_id
        if request_id in self._pending_requests:
            self._pending_requests[request_id].set_exception(
                RuntimeError(f"LLM Error [{payload.error_code}]: {payload.error_message}")
            )
            del self._pending_requests[request_id]

        if self._llm_error_handler:
            try:
                result = self._llm_error_handler(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"LLM error handler error: {e}")

    async def _handle_checkpoint_ack(self, msg: MessageEnvelope) -> None:
        """Handle checkpoint acknowledgment."""
        payload = ContextCheckpointAckPayload(
            checkpoint_id=msg.payload.get("checkpoint_id", ""),
            stored=msg.payload.get("stored", False),
            expires_at=msg.payload.get("expires_at"),
        )

        if self._checkpoint_ack_handler:
            try:
                result = self._checkpoint_ack_handler(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Checkpoint ack handler error: {e}")

    async def _handle_session_sync(self, msg: MessageEnvelope) -> None:
        """Handle session sync (for resume)."""
        payload = SessionSyncPayload(
            session_id=msg.payload.get("session_id", ""),
            status=msg.payload.get("status", "resumed"),
            last_context_ref=msg.payload.get("last_context_ref"),
            turn_count=msg.payload.get("turn_count", 0),
            task_status=msg.payload.get("task_status"),
            needs_resync=msg.payload.get("needs_resync", False),
            resync_from=msg.payload.get("resync_from"),
        )

        # Set connected if session resumed successfully
        if payload.status == "resumed":
            self._session_id = payload.session_id
            self._connected_event.set()

        if self._session_sync_handler:
            try:
                result = self._session_sync_handler(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Session sync handler error: {e}")

    def create_pending_request(self, request_id: str) -> asyncio.Future:
        """Create a future for tracking a pending LLM request.

        Args:
            request_id: Request ID to track

        Returns:
            Future that will be completed when response received
        """
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future
        return future
