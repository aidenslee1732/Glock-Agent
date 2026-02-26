"""
Context Rehydrator for Model B.

Reconstructs full conversation context from:
- Checkpoint chain (encrypted snapshots + deltas)
- Current delta (new messages since last checkpoint)
- Context pack (rolling summary, pinned facts, file slices)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from packages.shared_protocol.types import (
    ContextPack,
    ContextDelta,
    Message,
    RollingSummary,
    PinnedFact,
    FileSlice,
)
from apps.server.src.storage.checkpoint_store import ContextCheckpointStore, Checkpoint

logger = logging.getLogger(__name__)


@dataclass
class RehydratedContext:
    """Fully rehydrated context ready for LLM."""
    system_prompt: str
    messages: list[dict[str, Any]]
    total_tokens: int
    turn_count: int
    context_ref: Optional[str] = None


@dataclass
class CheckpointPayload:
    """Parsed checkpoint payload."""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    rolling_summary: Optional[dict[str, Any]] = None
    pinned_facts: list[dict[str, Any]] = field(default_factory=list)
    file_slices: list[dict[str, Any]] = field(default_factory=list)
    token_count: int = 0
    turn_count: int = 0


class ContextRehydrator:
    """
    Rehydrates full context from checkpoint chain + delta.

    The rehydration process:
    1. Load checkpoint chain (walk from target back to root full snapshot)
    2. Apply each checkpoint in order to build message history
    3. Apply current delta (new messages since checkpoint)
    4. Build system prompt from context pack
    5. Return complete context ready for LLM
    """

    def __init__(self, checkpoint_store: ContextCheckpointStore):
        self.checkpoint_store = checkpoint_store

    async def rehydrate(
        self,
        session_id: str,
        user_id: str,
        context_ref: Optional[str],
        delta: dict[str, Any],
        context_pack: dict[str, Any],
    ) -> RehydratedContext:
        """
        Rehydrate full context from checkpoint + delta.

        Args:
            session_id: The session ID
            user_id: The user ID
            context_ref: Reference to the last checkpoint (or None for fresh start)
            delta: New context since last checkpoint
            context_pack: Stable context (summary, facts, slices)

        Returns:
            RehydratedContext with full message history
        """
        messages: list[dict[str, Any]] = []
        total_tokens = 0
        turn_count = 0

        # 1. Build system prompt from context pack
        system_prompt = self._build_system_prompt(context_pack)

        # 2. Rehydrate from checkpoint chain if available
        if context_ref:
            checkpoint_messages, cp_tokens, cp_turns = await self._rehydrate_from_checkpoints(
                session_id=session_id,
                user_id=user_id,
                context_ref=context_ref,
            )
            messages.extend(checkpoint_messages)
            total_tokens += cp_tokens
            turn_count = cp_turns

        # 3. Apply delta messages
        delta_messages = delta.get("messages", [])
        for msg_data in delta_messages:
            messages.append({
                "role": msg_data.get("role", "user"),
                "content": msg_data.get("content", ""),
            })
            if msg_data.get("role") in ("user", "assistant"):
                turn_count += 1

        # 4. Add compressed tool results from delta
        tool_results = delta.get("tool_results_compressed", [])
        for result in tool_results:
            # Tool results are added as tool messages
            messages.append({
                "role": "tool",
                "content": json.dumps(result, indent=2),
                "tool_call_id": result.get("tool_call_id", ""),
            })

        # Estimate delta tokens
        delta_token_count = delta.get("token_count", 0)
        if delta_token_count == 0:
            # Rough estimate: 4 chars per token
            delta_text = json.dumps(delta)
            delta_token_count = len(delta_text) // 4
        total_tokens += delta_token_count

        return RehydratedContext(
            system_prompt=system_prompt,
            messages=messages,
            total_tokens=total_tokens,
            turn_count=turn_count,
            context_ref=context_ref,
        )

    async def _rehydrate_from_checkpoints(
        self,
        session_id: str,
        user_id: str,
        context_ref: str,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """
        Rehydrate messages from checkpoint chain.

        Returns:
            Tuple of (messages, total_tokens, turn_count)
        """
        messages: list[dict[str, Any]] = []
        total_tokens = 0
        turn_count = 0

        try:
            # Get checkpoint chain (oldest to newest)
            chain = await self.checkpoint_store.get_checkpoint_chain(
                checkpoint_id=context_ref,
                session_id=session_id,
                user_id=user_id,
            )

            if not chain:
                logger.warning(f"No checkpoint chain found for {context_ref}")
                return messages, total_tokens, turn_count

            # Apply each checkpoint in order
            for checkpoint in chain:
                payload = self._parse_checkpoint_payload(checkpoint.payload)

                if checkpoint.is_full:
                    # Full snapshot replaces all previous context
                    messages = payload.messages.copy()
                    total_tokens = payload.token_count
                    turn_count = payload.turn_count
                else:
                    # Delta adds to existing context
                    messages.extend(payload.messages)
                    total_tokens += payload.token_count
                    turn_count += payload.turn_count

                # Add tool results
                for result in payload.tool_results:
                    messages.append({
                        "role": "tool",
                        "content": json.dumps(result, indent=2),
                        "tool_call_id": result.get("tool_call_id", ""),
                    })

        except Exception as e:
            logger.exception(f"Error rehydrating from checkpoints: {e}")

        return messages, total_tokens, turn_count

    def _parse_checkpoint_payload(self, payload_bytes: bytes) -> CheckpointPayload:
        """Parse a checkpoint payload from bytes."""
        try:
            data = json.loads(payload_bytes.decode("utf-8"))

            return CheckpointPayload(
                messages=data.get("messages", []),
                tool_results=data.get("tool_results", []),
                rolling_summary=data.get("rolling_summary"),
                pinned_facts=data.get("pinned_facts", []),
                file_slices=data.get("file_slices", []),
                token_count=data.get("token_count", 0),
                turn_count=data.get("turn_count", 0),
            )

        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error(f"Failed to parse checkpoint payload: {e}")
            return CheckpointPayload()

    def _build_system_prompt(self, context_pack: dict[str, Any]) -> str:
        """
        Build system prompt from context pack.

        The system prompt includes:
        - Rolling summary (task state, progress)
        - Pinned facts (important information to preserve)
        - File slices (relevant code context)
        """
        parts: list[str] = []

        # Base system prompt
        parts.append("You are Glock, an AI coding assistant. Help the user with their coding task.")
        parts.append("")

        # Rolling summary
        summary = context_pack.get("rolling_summary", {})
        if summary:
            task_desc = summary.get("task_description", "")
            if task_desc:
                parts.append("## Current Task")
                parts.append(task_desc)
                parts.append("")

            current_state = summary.get("current_state", "")
            if current_state:
                parts.append("## Current State")
                parts.append(current_state)
                parts.append("")

            files_modified = summary.get("files_modified", [])
            if files_modified:
                parts.append("## Files Modified")
                for f in files_modified:
                    parts.append(f"- {f}")
                parts.append("")

            files_read = summary.get("files_read", [])
            if files_read:
                parts.append("## Files Read")
                for f in files_read[:10]:  # Limit to 10
                    parts.append(f"- {f}")
                if len(files_read) > 10:
                    parts.append(f"... and {len(files_read) - 10} more")
                parts.append("")

            key_decisions = summary.get("key_decisions", [])
            if key_decisions:
                parts.append("## Key Decisions Made")
                for d in key_decisions:
                    parts.append(f"- {d}")
                parts.append("")

            errors = summary.get("errors_encountered", [])
            if errors:
                parts.append("## Errors Encountered")
                for e in errors:
                    parts.append(f"- {e}")
                parts.append("")

        # Pinned facts
        facts = context_pack.get("pinned_facts", [])
        if facts:
            parts.append("## Important Facts")
            for fact in facts[:30]:  # Max 30 facts
                key = fact.get("key", "")
                value = fact.get("value", "")
                if key and value:
                    parts.append(f"- {key}: {value}")
            parts.append("")

        # File slices
        slices = context_pack.get("file_slices", [])
        if slices:
            parts.append("## Relevant Code Context")
            for s in slices[:10]:  # Limit file slices
                file_path = s.get("file_path", "")
                start_line = s.get("start_line", 0)
                end_line = s.get("end_line", 0)
                content = s.get("content", "")
                reason = s.get("reason", "")

                if file_path and content:
                    parts.append(f"### {file_path} (lines {start_line}-{end_line})")
                    if reason:
                        parts.append(f"_Reason: {reason}_")
                    parts.append("```")
                    parts.append(content)
                    parts.append("```")
                    parts.append("")

        return "\n".join(parts)

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text."""
        # Rough estimate: ~4 characters per token
        return len(text) // 4 + 1


# Singleton instance
_rehydrator: Optional[ContextRehydrator] = None


def get_rehydrator(checkpoint_store: ContextCheckpointStore) -> ContextRehydrator:
    """Get or create context rehydrator singleton."""
    global _rehydrator
    if _rehydrator is None:
        _rehydrator = ContextRehydrator(checkpoint_store)
    return _rehydrator
