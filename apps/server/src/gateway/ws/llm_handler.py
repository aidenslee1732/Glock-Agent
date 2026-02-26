"""
LLM Handler for Model B - Client-Orchestrated Architecture.

Handles LLM proxy requests from clients:
- Rehydrates context from checkpoints + delta
- Streams LLM responses to client
- Creates new checkpoints after responses
- Updates metering
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, AsyncIterator
from uuid import uuid4

from packages.shared_protocol.types import (
    MessageEnvelope,
    MessageType,
    LLMRequestPayload,
    LLMDeltaPayload,
    LLMResponseEndPayload,
    LLMErrorPayload,
    LLMCancelPayload,
    ContextCheckpointPayload,
    ContextCheckpointAckPayload,
    ToolCallResult,
    ContextPack,
    ContextDelta,
    Message,
    generate_checkpoint_id,
)
from apps.server.src.storage.redis import RedisClient
from apps.server.src.storage.postgres import PostgresClient
from apps.server.src.planner.llm.gateway import (
    LLMGateway,
    LLMConfig,
    ModelTier,
    Message as LLMMessage,
    ToolDefinition as LLMToolDefinition,
    StreamDelta,
    LLMError,
)

logger = logging.getLogger(__name__)


# Configuration
LLM_TIMEOUT_MS = 300000  # 5 minutes
MAX_CONTEXT_TOKENS = 200000
CHECKPOINT_TTL_HOURS = 24


@dataclass
class ActiveRequest:
    """Tracks an active LLM request."""
    request_id: str
    session_id: str
    user_id: str
    started_at: float
    cancelled: bool = False


class LLMHandler:
    """
    Handles LLM proxy requests in Model B architecture.

    Key responsibilities:
    - Process LLM_REQUEST messages from clients
    - Rehydrate context from checkpoint + delta
    - Stream LLM responses back to client
    - Create new checkpoints after responses
    - Track usage for metering
    """

    def __init__(
        self,
        redis: RedisClient,
        postgres: PostgresClient,
        llm_gateway: Optional[LLMGateway] = None,
    ):
        self.redis = redis
        self.postgres = postgres
        self.llm_gateway = llm_gateway or LLMGateway(LLMConfig())

        # Active requests: request_id → ActiveRequest
        self._active_requests: dict[str, ActiveRequest] = {}

        # Cancel signals: request_id → asyncio.Event
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def handle_llm_request(
        self,
        session_id: str,
        user_id: str,
        payload: dict[str, Any],
        send_callback: Any,  # async callable to send messages
    ) -> None:
        """
        Handle an LLM request from a client.

        Args:
            session_id: The session ID
            user_id: The authenticated user ID
            payload: The LLM request payload
            send_callback: Async function to send messages back to client
        """
        request_id = payload.get("request_id", str(uuid4()))

        # Track active request
        active_request = ActiveRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            started_at=time.time(),
        )
        self._active_requests[request_id] = active_request
        self._cancel_events[request_id] = asyncio.Event()

        try:
            # Parse request payload
            context_ref = payload.get("context_ref")
            delta_data = payload.get("delta", {})
            context_pack_data = payload.get("context_pack", {})
            tools_data = payload.get("tools", [])
            model_tier = payload.get("model_tier", "standard")
            max_tokens = payload.get("max_tokens", 8192)
            temperature = payload.get("temperature", 0.7)

            # Build messages from context
            messages = await self._build_messages(
                session_id=session_id,
                user_id=user_id,
                context_ref=context_ref,
                delta=delta_data,
                context_pack=context_pack_data,
            )

            # Build tools
            tools = self._build_tools(tools_data)

            # Get model tier
            tier = self._parse_model_tier(model_tier)

            # Stream LLM response
            total_content = ""
            tool_calls: list[ToolCallResult] = []
            input_tokens = 0
            output_tokens = 0
            finish_reason = "stop"
            index = 0

            # Accumulate tool call data for parsing
            current_tool_id: Optional[str] = None
            current_tool_name: Optional[str] = None
            current_tool_args: str = ""

            async for delta in self.llm_gateway.stream(
                messages=messages,
                tier=tier,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                user_id=user_id,
                session_id=session_id,
            ):
                # Check for cancellation
                if self._cancel_events[request_id].is_set():
                    finish_reason = "cancelled"
                    break

                # Send delta to client
                if delta.content:
                    total_content += delta.content
                    await self._send_delta(
                        send_callback=send_callback,
                        session_id=session_id,
                        request_id=request_id,
                        delta_type="text",
                        content=delta.content,
                        index=index,
                    )
                    index += 1

                # Handle tool calls
                if delta.tool_call_id:
                    # New tool call starting
                    if current_tool_id and current_tool_name:
                        # Save previous tool call
                        try:
                            args = json.loads(current_tool_args) if current_tool_args else {}
                        except json.JSONDecodeError:
                            args = {}
                        tool_calls.append(ToolCallResult(
                            tool_call_id=current_tool_id,
                            tool_name=current_tool_name,
                            arguments=args,
                        ))
                    current_tool_id = delta.tool_call_id
                    current_tool_name = delta.tool_call_name
                    current_tool_args = ""

                if delta.tool_call_args:
                    current_tool_args += delta.tool_call_args

                if delta.finish_reason:
                    finish_reason = delta.finish_reason
                    if finish_reason == "tool_calls":
                        finish_reason = "tool_use"

            # Save final tool call if any
            if current_tool_id and current_tool_name:
                try:
                    args = json.loads(current_tool_args) if current_tool_args else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCallResult(
                    tool_call_id=current_tool_id,
                    tool_name=current_tool_name,
                    arguments=args,
                ))

            # Estimate token usage (LiteLLM provides this in final chunk sometimes)
            input_tokens = self.llm_gateway.estimate_tokens(
                json.dumps([m.to_dict() for m in messages])
            )
            output_tokens = self.llm_gateway.estimate_tokens(total_content)

            # Create new checkpoint
            new_context_ref = generate_checkpoint_id()

            # Send response end
            await self._send_response_end(
                send_callback=send_callback,
                session_id=session_id,
                request_id=request_id,
                new_context_ref=new_context_ref,
                finish_reason=finish_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                tool_calls=tool_calls,
                content=total_content,
            )

            # Update session state in Redis
            await self._update_session_state(
                session_id=session_id,
                context_ref=new_context_ref,
                tokens_used=input_tokens + output_tokens,
            )

        except LLMError as e:
            logger.error(f"LLM error for request {request_id}: {e}")
            await self._send_error(
                send_callback=send_callback,
                session_id=session_id,
                request_id=request_id,
                error_code="provider_error",
                error_message=str(e),
                retryable=True,
            )

        except asyncio.TimeoutError:
            logger.error(f"LLM timeout for request {request_id}")
            await self._send_error(
                send_callback=send_callback,
                session_id=session_id,
                request_id=request_id,
                error_code="timeout",
                error_message="LLM request timed out",
                retryable=True,
            )

        except Exception as e:
            logger.exception(f"Unexpected error handling LLM request {request_id}")
            await self._send_error(
                send_callback=send_callback,
                session_id=session_id,
                request_id=request_id,
                error_code="internal_error",
                error_message=str(e),
                retryable=False,
            )

        finally:
            # Cleanup
            self._active_requests.pop(request_id, None)
            self._cancel_events.pop(request_id, None)

    async def handle_llm_cancel(
        self,
        session_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Handle LLM cancel request from client."""
        request_id = payload.get("request_id")
        if not request_id:
            return

        if request_id in self._cancel_events:
            self._cancel_events[request_id].set()
            logger.info(f"Cancelled LLM request: {request_id}")

    async def handle_context_checkpoint(
        self,
        session_id: str,
        user_id: str,
        payload: dict[str, Any],
        send_callback: Any,
    ) -> None:
        """
        Handle context checkpoint from client.

        Stores the encrypted checkpoint and acknowledges.
        """
        checkpoint_id = payload.get("checkpoint_id")
        parent_id = payload.get("parent_id")
        encrypted_payload = payload.get("encrypted_payload")
        payload_hash = payload.get("payload_hash")
        token_count = payload.get("token_count", 0)
        turn_count = payload.get("turn_count", 0)
        is_full = payload.get("is_full", False)

        if not checkpoint_id or not encrypted_payload:
            logger.warning(f"Invalid checkpoint payload for session {session_id}")
            return

        try:
            # Store checkpoint in database
            expires_at = datetime.utcnow() + timedelta(hours=CHECKPOINT_TTL_HOURS)

            await self.postgres.execute(
                """
                INSERT INTO context_checkpoints
                (id, session_id, user_id, parent_id, ciphertext_base64, payload_hash,
                 token_count, turn_count, is_full, created_at, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW(), $10)
                ON CONFLICT (id) DO UPDATE SET
                    ciphertext_base64 = EXCLUDED.ciphertext_base64,
                    payload_hash = EXCLUDED.payload_hash
                """,
                checkpoint_id,
                session_id,
                user_id,
                parent_id,
                encrypted_payload,
                payload_hash,
                token_count,
                turn_count,
                is_full,
                expires_at,
            )

            # Update session state
            await self.redis.hset(
                f"sess:{session_id}:state",
                mapping={
                    "last_context_ref": checkpoint_id,
                    "turn_count": str(turn_count),
                    "total_tokens": str(token_count),
                },
            )

            # Send acknowledgment
            stored_at = datetime.utcnow()
            ack_payload = ContextCheckpointAckPayload(
                checkpoint_id=checkpoint_id,
                stored_at=stored_at,
                expires_at=expires_at,
            )

            msg = MessageEnvelope.create(
                msg_type=MessageType.CONTEXT_CHECKPOINT_ACK,
                session_id=session_id,
                payload=ack_payload.to_dict(),
            )
            await send_callback(json.dumps(msg.to_dict()))

            logger.info(
                f"Stored checkpoint {checkpoint_id} for session {session_id} "
                f"(tokens={token_count}, turns={turn_count}, full={is_full})"
            )

        except Exception as e:
            logger.exception(f"Failed to store checkpoint {checkpoint_id}")

    async def _build_messages(
        self,
        session_id: str,
        user_id: str,
        context_ref: Optional[str],
        delta: dict[str, Any],
        context_pack: dict[str, Any],
    ) -> list[LLMMessage]:
        """
        Build LLM messages from context checkpoint, delta, and context pack.

        In Model B, context is assembled from:
        1. System prompt (built from context_pack)
        2. Rehydrated messages from checkpoint (if context_ref provided)
        3. Delta messages (new since last checkpoint)
        """
        messages: list[LLMMessage] = []

        # 1. Build system message from context pack
        system_content = self._build_system_prompt(context_pack)
        if system_content:
            messages.append(LLMMessage(role="system", content=system_content))

        # 2. Rehydrate from checkpoint if available
        if context_ref:
            checkpoint_messages = await self._load_checkpoint_messages(
                session_id=session_id,
                user_id=user_id,
                context_ref=context_ref,
            )
            messages.extend(checkpoint_messages)

        # 3. Add delta messages
        delta_messages = delta.get("messages", [])
        for msg_data in delta_messages:
            role = msg_data.get("role", "user")
            content = msg_data.get("content", "")
            messages.append(LLMMessage(role=role, content=content))

        # 4. Add compressed tool results
        tool_results = delta.get("tool_results_compressed", [])
        for result in tool_results:
            tool_content = json.dumps(result, indent=2)
            messages.append(LLMMessage(role="tool", content=tool_content))

        return messages

    def _build_system_prompt(self, context_pack: dict[str, Any]) -> str:
        """Build system prompt from context pack."""
        parts: list[str] = []

        # Rolling summary
        summary = context_pack.get("rolling_summary", {})
        if summary:
            task_desc = summary.get("task_description", "")
            if task_desc:
                parts.append(f"## Current Task\n{task_desc}")

            current_state = summary.get("current_state", "")
            if current_state:
                parts.append(f"## Current State\n{current_state}")

            files_modified = summary.get("files_modified", [])
            if files_modified:
                parts.append(f"## Files Modified\n" + "\n".join(f"- {f}" for f in files_modified))

            key_decisions = summary.get("key_decisions", [])
            if key_decisions:
                parts.append(f"## Key Decisions\n" + "\n".join(f"- {d}" for d in key_decisions))

        # Pinned facts
        facts = context_pack.get("pinned_facts", [])
        if facts:
            facts_text = "\n".join(f"- {f.get('key')}: {f.get('value')}" for f in facts)
            parts.append(f"## Important Facts\n{facts_text}")

        # File slices
        slices = context_pack.get("file_slices", [])
        if slices:
            slice_parts = []
            for s in slices:
                file_path = s.get("file_path", "")
                start = s.get("start_line", 0)
                end = s.get("end_line", 0)
                content = s.get("content", "")
                slice_parts.append(
                    f"### {file_path} (lines {start}-{end})\n```\n{content}\n```"
                )
            parts.append(f"## Relevant File Context\n" + "\n\n".join(slice_parts))

        return "\n\n".join(parts) if parts else ""

    async def _load_checkpoint_messages(
        self,
        session_id: str,
        user_id: str,
        context_ref: str,
    ) -> list[LLMMessage]:
        """
        Load and decrypt messages from a checkpoint.

        Note: In the full implementation, this would decrypt the checkpoint
        and walk the delta chain. For now, we return empty as the delta
        contains the recent messages.
        """
        # TODO: Implement checkpoint chain rehydration
        # This requires the ContextRehydrator which we'll implement next
        return []

    def _build_tools(self, tools_data: list[dict[str, Any]]) -> list[LLMToolDefinition]:
        """Build tool definitions for LLM."""
        tools: list[LLMToolDefinition] = []
        for tool in tools_data:
            tools.append(LLMToolDefinition(
                name=tool.get("name", ""),
                description=tool.get("description", ""),
                parameters=tool.get("parameters", {}),
            ))
        return tools

    def _parse_model_tier(self, tier_str: str) -> ModelTier:
        """Parse model tier from string."""
        tier_map = {
            "fast": ModelTier.FAST,
            "standard": ModelTier.STANDARD,
            "advanced": ModelTier.ADVANCED,
            "reasoning": ModelTier.REASONING,
        }
        return tier_map.get(tier_str.lower(), ModelTier.STANDARD)

    async def _send_delta(
        self,
        send_callback: Any,
        session_id: str,
        request_id: str,
        delta_type: str,
        content: str,
        index: int,
    ) -> None:
        """Send LLM delta to client."""
        payload = LLMDeltaPayload(
            request_id=request_id,
            delta_type=delta_type,
            content=content,
            index=index,
        )
        msg = MessageEnvelope.create(
            msg_type=MessageType.LLM_DELTA,
            session_id=session_id,
            payload=payload.to_dict(),
        )
        await send_callback(json.dumps(msg.to_dict()))

    async def _send_response_end(
        self,
        send_callback: Any,
        session_id: str,
        request_id: str,
        new_context_ref: str,
        finish_reason: str,
        input_tokens: int,
        output_tokens: int,
        tool_calls: list[ToolCallResult],
        content: str,
    ) -> None:
        """Send LLM response end to client."""
        payload = LLMResponseEndPayload(
            request_id=request_id,
            new_context_ref=new_context_ref,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            content=content,
        )
        msg = MessageEnvelope.create(
            msg_type=MessageType.LLM_RESPONSE_END,
            session_id=session_id,
            payload=payload.to_dict(),
        )
        await send_callback(json.dumps(msg.to_dict()))

    async def _send_error(
        self,
        send_callback: Any,
        session_id: str,
        request_id: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_after_ms: Optional[int] = None,
    ) -> None:
        """Send LLM error to client."""
        payload = LLMErrorPayload(
            request_id=request_id,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
        )
        msg = MessageEnvelope.create(
            msg_type=MessageType.LLM_ERROR,
            session_id=session_id,
            payload=payload.to_dict(),
        )
        await send_callback(json.dumps(msg.to_dict()))

    async def _update_session_state(
        self,
        session_id: str,
        context_ref: str,
        tokens_used: int,
    ) -> None:
        """Update session state in Redis."""
        await self.redis.hset(
            f"sess:{session_id}:state",
            mapping={
                "last_context_ref": context_ref,
                "last_activity": str(time.time()),
            },
        )

        # Increment total tokens
        await self.redis.hincrby(
            f"sess:{session_id}:state",
            "total_tokens",
            tokens_used,
        )
