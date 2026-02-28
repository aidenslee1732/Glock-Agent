"""
Delta Builder for Model B.

Builds the context delta with messages since the last checkpoint:
- New conversation messages
- Compressed tool results
- Token counting
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from packages.shared_protocol.types import ContextDelta, Message

from .compressor import ToolOutputCompressor

logger = logging.getLogger(__name__)


@dataclass
class DeltaConfig:
    """Configuration for delta building."""
    max_delta_tokens: int = 10000
    max_messages: int = 20
    max_tool_results: int = 50
    compress_tool_outputs: bool = True


@dataclass
class ConversationMessage:
    """A message in the conversation."""
    role: str
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    timestamp: Optional[float] = None


class DeltaBuilder:
    """
    Builds the context delta for LLM requests.

    The delta contains everything new since the last checkpoint:
    - User messages
    - Assistant responses
    - Tool calls and their compressed results

    This is what gets sent to the server along with the context_ref.
    """

    def __init__(
        self,
        config: Optional[DeltaConfig] = None,
        compressor: Optional[ToolOutputCompressor] = None,
    ):
        self.config = config or DeltaConfig()
        self.compressor = compressor or ToolOutputCompressor()

        # Messages accumulated since last checkpoint
        self._messages: list[ConversationMessage] = []
        self._checkpoint_index: int = 0  # Index of last checkpointed message

    @property
    def message_count(self) -> int:
        """Count of messages since last checkpoint."""
        return len(self._messages) - self._checkpoint_index

    def add_user_message(self, content: str) -> None:
        """Add a user message."""
        logger.debug(f"add_user_message: content_len={len(content)}")
        self._messages.append(ConversationMessage(
            role="user",
            content=content,
            timestamp=self._get_timestamp(),
        ))

    def add_assistant_message(
        self,
        content: str,
        tool_calls: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """Add an assistant message."""
        logger.debug(f"add_assistant_message: content_len={len(content) if content else 0}, tool_calls={len(tool_calls) if tool_calls else 0}")
        self._messages.append(ConversationMessage(
            role="assistant",
            content=content,
            tool_calls=tool_calls or [],
            timestamp=self._get_timestamp(),
        ))

    def add_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        result: dict[str, Any],
        compress: bool = True,
    ) -> None:
        """
        Add a tool result to the last assistant message.

        Args:
            tool_call_id: ID of the tool call
            tool_name: Name of the tool
            result: Tool execution result
            compress: Whether to compress the result
        """
        if not self._messages:
            logger.warning("No messages to attach tool result to")
            return

        logger.debug(f"add_tool_result: tool={tool_name}, id={tool_call_id}, last_msg_role={self._messages[-1].role}")

        # Compress if needed
        if compress and self.config.compress_tool_outputs:
            result = self.compressor.compress(tool_name, result)

        # Build tool result in the format expected by the server:
        # Server's _format_tool_result_content expects {"tool_name", "status", "result"}
        tool_result = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "status": result.get("status", "success"),
            "result": result.get("result", result),  # Use nested result if present, else whole result
        }

        # Attach to last message
        self._messages[-1].tool_results.append(tool_result)

    def build(self, include_all: bool = True) -> ContextDelta:
        """
        Build the context delta.

        Args:
            include_all: If True, include ALL messages (ignore checkpoint index).
                         This is safer when checkpoint storage isn't reliable.

        Returns:
            ContextDelta with messages
        """
        # Include all messages for now - checkpoint storage is not reliable
        # When checkpoints work properly, we can use self._checkpoint_index
        if include_all:
            delta_messages = self._messages
        else:
            delta_messages = self._messages[self._checkpoint_index:]

        logger.debug(f"DeltaBuilder.build: {len(delta_messages)} messages, include_all={include_all}")
        for i, msg in enumerate(delta_messages):
            logger.debug(f"  [{i}] role={msg.role}, content_len={len(msg.content) if msg.content else 0}, "
                        f"tool_calls={len(msg.tool_calls)}, tool_results={len(msg.tool_results)}")

        # Convert to Message objects
        messages: list[Message] = []
        tool_results_compressed: list[dict[str, Any]] = []

        for msg in delta_messages[-self.config.max_messages:]:
            messages.append(Message(
                role=msg.role,
                content=msg.content,
                tool_calls=msg.tool_calls if msg.tool_calls else None,
            ))

            # Collect tool results
            for result in msg.tool_results[:self.config.max_tool_results]:
                tool_results_compressed.append(result)

        # Estimate tokens
        token_count = self._estimate_tokens(messages, tool_results_compressed)

        return ContextDelta(
            messages=messages,
            tool_results_compressed=tool_results_compressed,
            token_count=token_count,
        )

    def mark_checkpoint(self) -> None:
        """Mark current position as checkpointed."""
        self._checkpoint_index = len(self._messages)

    def get_since_checkpoint(self) -> list[ConversationMessage]:
        """Get messages since last checkpoint."""
        return self._messages[self._checkpoint_index:]

    def truncate_to_fit(self, max_tokens: int) -> None:
        """
        Truncate delta to fit within token budget.

        Removes oldest messages first while keeping conversation coherent.
        """
        while self.message_count > 0:
            delta = self.build()
            if delta.token_count <= max_tokens:
                break

            # Remove oldest message after checkpoint
            if self._checkpoint_index < len(self._messages):
                self._messages.pop(self._checkpoint_index)
            else:
                break

    def _estimate_tokens(
        self,
        messages: list[Message],
        tool_results: list[dict[str, Any]],
    ) -> int:
        """Estimate token count."""
        total = 0

        for msg in messages:
            total += len(msg.content) // 4 + 10  # Content + overhead
            if msg.tool_calls:
                total += len(json.dumps(msg.tool_calls)) // 4

        for result in tool_results:
            total += len(json.dumps(result)) // 4

        return total

    def _get_timestamp(self) -> float:
        """Get current timestamp."""
        import time
        return time.time()

    def reset(self) -> None:
        """Reset all state."""
        self._messages = []
        self._checkpoint_index = 0

    def get_full_conversation(self) -> list[dict[str, Any]]:
        """Get full conversation for serialization."""
        return [
            {
                "role": msg.role,
                "content": msg.content,
                "tool_calls": msg.tool_calls,
                "tool_results": msg.tool_results,
                "timestamp": msg.timestamp,
            }
            for msg in self._messages
        ]

    def load_conversation(self, data: list[dict[str, Any]]) -> None:
        """Load conversation from serialized data."""
        self._messages = [
            ConversationMessage(
                role=msg["role"],
                content=msg["content"],
                tool_calls=msg.get("tool_calls", []),
                tool_results=msg.get("tool_results", []),
                timestamp=msg.get("timestamp"),
            )
            for msg in data
        ]
        self._checkpoint_index = len(self._messages)
