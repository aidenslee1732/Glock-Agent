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
from typing import Any, Optional, Callable, Awaitable
from uuid import uuid4

from packages.shared_protocol.types import (
    MessageEnvelope,
    MessageType,
    LLMDeltaPayload,
    LLMResponseEndPayload,
    LLMErrorPayload,
    ContextCheckpointAckPayload,
    ToolCallResult,
    generate_checkpoint_id,
)
from apps.server.src.storage.redis import RedisClient
from apps.server.src.storage.postgres import PostgresClient
from apps.server.src.storage.checkpoint_store import ContextCheckpointStore
from apps.server.src.planner.llm.gateway import (
    LLMGateway,
    LLMConfig,
    ModelTier,
    Message as LLMMessage,
    ToolCallMessage,
    FunctionCall,
    ToolDefinition as LLMToolDefinition,
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
        checkpoint_store: Optional[ContextCheckpointStore] = None,
    ):
        self.redis = redis
        self.postgres = postgres
        self.llm_gateway = llm_gateway or LLMGateway(LLMConfig())
        self.checkpoint_store = checkpoint_store or ContextCheckpointStore(postgres, redis)

        self._active_requests: dict[str, ActiveRequest] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def handle_llm_request(
        self,
        session_id: str,
        user_id: str,
        payload: dict[str, Any],
        send_callback: Callable[[str], Awaitable[None]],
    ) -> None:
        """Handle an LLM request from a client."""
        request_id = payload.get("request_id", str(uuid4()))

        active_request = ActiveRequest(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            started_at=time.time(),
        )
        self._active_requests[request_id] = active_request
        self._cancel_events[request_id] = asyncio.Event()

        try:
            context_ref = payload.get("context_ref")
            delta_data = payload.get("delta", {})
            context_pack_data = payload.get("context_pack", {})
            tools_data = payload.get("tools", [])
            model_tier = payload.get("model_tier", "standard")
            max_tokens = payload.get("max_tokens", 8192)
            temperature = payload.get("temperature", 0.7)

            messages = await self._build_messages(
                session_id=session_id,
                user_id=user_id,
                context_ref=context_ref,
                delta=delta_data,
                context_pack=context_pack_data,
            )

            tools = self._build_tools(tools_data)
            tier = self._parse_model_tier(model_tier)

            total_content = ""
            tool_calls: list[ToolCallResult] = []
            input_tokens = 0
            output_tokens = 0
            finish_reason = "stop"
            index = 0

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
                if self._cancel_events[request_id].is_set():
                    finish_reason = "cancelled"
                    break

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

                if delta.tool_call_id:
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
                    current_tool_id = delta.tool_call_id
                    current_tool_name = delta.tool_call_name
                    current_tool_args = ""

                if delta.tool_call_args:
                    current_tool_args += delta.tool_call_args

                if delta.finish_reason:
                    finish_reason = delta.finish_reason
                    if finish_reason == "tool_calls":
                        finish_reason = "tool_use"

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

            input_tokens = self.llm_gateway.estimate_tokens(
                json.dumps([m.to_dict() for m in messages])
            )
            output_tokens = self.llm_gateway.estimate_tokens(total_content)

            new_context_ref = generate_checkpoint_id()

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
        send_callback: Callable[[str], Awaitable[None]],
    ) -> None:
        """Handle context checkpoint from client."""
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

            await self.redis.hset(
                f"sess:{session_id}:state",
                mapping={
                    "last_context_ref": checkpoint_id,
                    "turn_count": str(turn_count),
                    "total_tokens": str(token_count),
                },
            )

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
            # Store error in database and raise user-friendly error
            from apps.server.src.errors import handle_error, ErrorContext, GlockError
            try:
                import asyncio
                asyncio.create_task(handle_error(
                    e,
                    component="llm_handler.checkpoint",
                    context=ErrorContext(
                        session_id=session_id,
                        user_id=user_id,
                        additional={"checkpoint_id": checkpoint_id},
                    ),
                    reraise=False,
                ))
            except Exception:
                pass  # Don't fail if error storage fails
            raise GlockError(
                f"Failed to store checkpoint {checkpoint_id}: {e}",
                original_error=e,
                severity="critical",
                context=ErrorContext(session_id=session_id, user_id=user_id),
            ) from e

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

        Ensures proper message ordering for tool calls:
        - Assistant messages with tool_calls must be followed by tool result messages
        - Tool result messages must reference valid tool_call_ids from the preceding assistant
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

        # 3. Process delta messages and tool results together to maintain proper ordering
        delta_messages = delta.get("messages", [])
        tool_results = delta.get("tool_results_compressed", [])

        # Build a map of tool_call_id to tool result for quick lookup
        tool_results_by_id: dict[str, dict[str, Any]] = {
            result.get("tool_call_id"): result
            for result in tool_results
            if result.get("tool_call_id")
        }

        # Track which tool results we've added
        added_tool_result_ids: set[str] = set()

        for msg_data in delta_messages:
            role = msg_data.get("role", "user")
            content = msg_data.get("content")
            raw_tool_calls = msg_data.get("tool_calls")

            # Convert tool_calls to proper format if present
            tool_calls: Optional[list[ToolCallMessage]] = None
            if raw_tool_calls and role == "assistant":
                tool_calls = self._convert_tool_calls(raw_tool_calls)

            messages.append(LLMMessage(
                role=role,
                content=content,
                tool_calls=tool_calls,
            ))

            # If this assistant message has tool_calls, add corresponding tool results right after
            if tool_calls:
                for tc in tool_calls:
                    if tc.id in tool_results_by_id and tc.id not in added_tool_result_ids:
                        result = tool_results_by_id[tc.id]
                        tool_content = self._format_tool_result_content(result)
                        messages.append(LLMMessage(
                            role="tool",
                            content=tool_content,
                            tool_call_id=tc.id,
                        ))
                        added_tool_result_ids.add(tc.id)

        # 4. Handle remaining tool results that weren't matched to an assistant message
        # We MUST create an assistant message with tool_calls before adding tool results
        # Otherwise the API will reject with "unexpected tool_use_id" error
        unmatched_results = [
            result for result in tool_results
            if result.get("tool_call_id") and result.get("tool_call_id") not in added_tool_result_ids
        ]

        if unmatched_results:
            logger.warning(
                f"Found {len(unmatched_results)} tool results without matching assistant tool_calls. "
                "Reconstructing assistant message."
            )

            # Create tool_calls for the assistant message from the unmatched results
            reconstructed_tool_calls: list[ToolCallMessage] = []
            for result in unmatched_results:
                tool_call_id = result.get("tool_call_id", "")
                tool_name = result.get("tool_name", "unknown_tool")

                reconstructed_tool_calls.append(ToolCallMessage(
                    id=tool_call_id,
                    type="function",
                    function=FunctionCall(
                        name=tool_name,
                        arguments="{}",  # We don't have the original arguments
                    ),
                ))

            # Add the reconstructed assistant message with tool_calls
            messages.append(LLMMessage(
                role="assistant",
                content=None,  # Assistant messages with tool_calls can have null content
                tool_calls=reconstructed_tool_calls,
            ))

            # Now add the tool results
            for result in unmatched_results:
                tool_call_id = result.get("tool_call_id")
                tool_content = self._format_tool_result_content(result)
                messages.append(LLMMessage(
                    role="tool",
                    content=tool_content,
                    tool_call_id=tool_call_id,
                ))
                added_tool_result_ids.add(tool_call_id)

        # 5. Ensure at least one non-system message exists (Anthropic requirement)
        has_non_system = any(m.role != "system" for m in messages)
        if not has_non_system:
            logger.warning("No non-system messages found, adding default user message")
            messages.append(LLMMessage(
                role="user",
                content="Please continue with the current task.",
            ))

        return messages

    def _convert_tool_calls(
        self,
        raw_tool_calls: list[dict[str, Any]],
    ) -> list[ToolCallMessage]:
        """Convert raw tool call dicts to ToolCallMessage objects."""
        tool_calls: list[ToolCallMessage] = []

        for tc in raw_tool_calls:
            # Handle both OpenAI format and simplified format
            tc_id = tc.get("id") or tc.get("tool_call_id", "")

            # Get function details
            function_data = tc.get("function", {})
            if function_data:
                name = function_data.get("name", "")
                arguments = function_data.get("arguments", "{}")
            else:
                # Simplified format: name and arguments at top level
                name = tc.get("name", tc.get("tool_name", ""))
                arguments = tc.get("arguments", {})

            # Ensure arguments is a JSON string
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments)

            tool_calls.append(ToolCallMessage(
                id=tc_id,
                type="function",
                function=FunctionCall(name=name, arguments=arguments),
            ))

        return tool_calls

    def _format_tool_result_content(self, result: dict[str, Any]) -> str:
        """Format tool result as content string."""
        # For simple string results, return directly
        if isinstance(result.get("result"), str) and not result.get("tool_name"):
            return result["result"]

        # For structured results, format as JSON
        tool_content_data = {
            "tool_name": result.get("tool_name"),
            "status": result.get("status"),
            "result": result.get("result"),
        }
        return json.dumps(tool_content_data, indent=2)

    def _build_system_prompt(self, context_pack: dict[str, Any]) -> str:
        """Build system prompt from context pack."""
        parts: list[str] = []

        # Base system instructions with default stack
        base_prompt = """You are Glock, an AI coding assistant. You help users build software projects.

## Default Technology Stack

When creating new projects, use these defaults unless the user specifies otherwise:

- **Frontend**: Next.js 14+ with TypeScript, Tailwind CSS, and shadcn/ui
- **Backend**: FastAPI with Python 3.11+
- **Fullstack**: Both of the above with CORS pre-configured

Always use the default stack for new projects. Only deviate if the user explicitly requests a different technology.

## Project Creation Guidelines

When creating new projects, ALWAYS use proper CLI tools:

### Frontend (Next.js)
```bash
npx create-next-app@latest <project-name> --typescript --tailwind --eslint --app --src-dir --import-alias "@/*" --use-npm
```
Then add shadcn/ui:
```bash
cd <project-name> && npx shadcn@latest init -y && npx shadcn@latest add button input card
```

### Backend (FastAPI)
Create the directory and write main.py with FastAPI app including CORS middleware for localhost:3000.

## Code Guidelines

- Write clean, production-ready code
- Follow best practices for each technology
- Include proper error handling
- Add helpful comments where needed
- Create complete, working implementations
- ALWAYS wait for CLI commands to complete before proceeding"""

        parts.append(base_prompt)

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
                parts.append("## Files Modified\n" + "\n".join(f"- {f}" for f in files_modified))

            key_decisions = summary.get("key_decisions", [])
            if key_decisions:
                parts.append("## Key Decisions\n" + "\n".join(f"- {d}" for d in key_decisions))

        facts = context_pack.get("pinned_facts", [])
        if facts:
            facts_text = "\n".join(f"- {f.get('key')}: {f.get('value')}" for f in facts)
            parts.append(f"## Important Facts\n{facts_text}")

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
            parts.append("## Relevant File Context\n" + "\n\n".join(slice_parts))

        return "\n\n".join(parts) if parts else ""

    async def _load_checkpoint_messages(
        self,
        session_id: str,
        user_id: str,
        context_ref: str,
    ) -> list[LLMMessage]:
        """Load and decrypt messages from a checkpoint chain.

        Walks the checkpoint chain from the full snapshot to the target
        checkpoint, rehydrating all messages in order.

        Args:
            session_id: The session ID
            user_id: The user ID
            context_ref: The checkpoint ID to load from

        Returns:
            List of LLMMessage objects from the checkpoint chain
        """
        messages: list[LLMMessage] = []

        try:
            # Get the full checkpoint chain (oldest to newest)
            chain = await self.checkpoint_store.get_checkpoint_chain(
                checkpoint_id=context_ref,
                session_id=session_id,
                user_id=user_id,
            )

            if not chain:
                logger.warning(f"No checkpoint chain found for {context_ref}")
                return []

            # Process each checkpoint in the chain
            for checkpoint in chain:
                try:
                    # Parse the decrypted payload as JSON
                    payload_data = json.loads(checkpoint.payload.decode("utf-8"))

                    # Extract messages from the payload
                    checkpoint_messages = payload_data.get("messages", [])

                    for msg_data in checkpoint_messages:
                        role = msg_data.get("role", "user")
                        content = msg_data.get("content")
                        raw_tool_calls = msg_data.get("tool_calls")

                        # Convert tool_calls if present
                        tool_calls: Optional[list[ToolCallMessage]] = None
                        if raw_tool_calls and role == "assistant":
                            tool_calls = self._convert_tool_calls(raw_tool_calls)

                        messages.append(LLMMessage(
                            role=role,
                            content=content,
                            tool_calls=tool_calls,
                        ))

                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse checkpoint {checkpoint.id}: {e}")
                    continue

            logger.debug(
                f"Rehydrated {len(messages)} messages from checkpoint chain "
                f"({len(chain)} checkpoints)"
            )

        except Exception as e:
            logger.error(f"Failed to load checkpoint messages: {e}")
            # Return empty list rather than failing the request
            return []

        return messages

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
        send_callback: Callable[[str], Awaitable[None]],
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
        send_callback: Callable[[str], Awaitable[None]],
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
        send_callback: Callable[[str], Awaitable[None]],
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

        await self.redis.hincrby(
            f"sess:{session_id}:state",
            "total_tokens",
            tokens_used,
        )
