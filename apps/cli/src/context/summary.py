"""
Rolling Summary Manager for Model B.

Maintains a concise summary of session progress:
- Task description
- Files modified/read
- Key decisions made
- Errors and resolutions
- Current state

Updated every 3 turns or on file edit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from packages.shared_protocol.types import RollingSummary

logger = logging.getLogger(__name__)


@dataclass
class SummaryConfig:
    """Configuration for rolling summary."""
    # Update frequency
    update_interval_turns: int = 3
    update_on_file_edit: bool = True

    # Size limits
    max_task_description_length: int = 500
    max_state_length: int = 300
    max_files_modified: int = 20
    max_files_read: int = 50
    max_decisions: int = 10
    max_errors: int = 10
    max_total_tokens: int = 4000


class RollingSummaryManager:
    """
    Manages the rolling summary for context packing.

    The summary provides continuity across context compaction:
    - When old messages are pruned, summary preserves key information
    - LLM can understand session history from summary alone
    - Automatically updated based on conversation progress
    """

    def __init__(self, config: Optional[SummaryConfig] = None):
        self.config = config or SummaryConfig()

        self._summary = RollingSummary(
            task_description="",
            files_modified=[],
            files_read=[],
            key_decisions=[],
            errors_encountered=[],
            current_state="Session started",
            turn_count=0,
        )

        self._last_update_turn = 0
        self._pending_updates: list[dict[str, Any]] = []

    @property
    def summary(self) -> RollingSummary:
        """Get current summary."""
        return self._summary

    def set_task(self, description: str) -> None:
        """Set the task description."""
        # Truncate if needed
        if len(description) > self.config.max_task_description_length:
            description = description[:self.config.max_task_description_length - 3] + "..."

        self._summary.task_description = description
        self._summary.current_state = "Starting task"

    def record_file_modified(self, file_path: str) -> None:
        """Record a file modification."""
        if file_path not in self._summary.files_modified:
            self._summary.files_modified.append(file_path)

            # Enforce limit
            if len(self._summary.files_modified) > self.config.max_files_modified:
                self._summary.files_modified = self._summary.files_modified[-self.config.max_files_modified:]

    def record_file_read(self, file_path: str) -> None:
        """Record a file read."""
        if file_path not in self._summary.files_read:
            self._summary.files_read.append(file_path)

            # Enforce limit
            if len(self._summary.files_read) > self.config.max_files_read:
                self._summary.files_read = self._summary.files_read[-self.config.max_files_read:]

    def record_decision(self, decision: str) -> None:
        """Record a key decision."""
        # Truncate long decisions
        if len(decision) > 200:
            decision = decision[:197] + "..."

        self._summary.key_decisions.append(decision)

        # Enforce limit
        if len(self._summary.key_decisions) > self.config.max_decisions:
            self._summary.key_decisions = self._summary.key_decisions[-self.config.max_decisions:]

    def record_error(self, error: str, resolved: bool = False) -> None:
        """Record an error."""
        # Truncate long errors
        if len(error) > 200:
            error = error[:197] + "..."

        if resolved:
            error = f"[RESOLVED] {error}"

        self._summary.errors_encountered.append(error)

        # Enforce limit
        if len(self._summary.errors_encountered) > self.config.max_errors:
            self._summary.errors_encountered = self._summary.errors_encountered[-self.config.max_errors:]

    def update_state(self, state: str) -> None:
        """Update current state description."""
        # Truncate if needed
        if len(state) > self.config.max_state_length:
            state = state[:self.config.max_state_length - 3] + "..."

        self._summary.current_state = state
        self._summary.last_updated_at = datetime.utcnow()

    def increment_turn(self) -> bool:
        """
        Increment turn count and check if update needed.

        Returns:
            True if summary should be updated
        """
        self._summary.turn_count += 1

        # Check if we should update
        turns_since_update = self._summary.turn_count - self._last_update_turn

        if turns_since_update >= self.config.update_interval_turns:
            self._last_update_turn = self._summary.turn_count
            return True

        return False

    def process_turn(
        self,
        assistant_content: str,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
    ) -> None:
        """
        Process a turn and extract summary updates.

        Args:
            assistant_content: The assistant's response text
            tool_calls: Tool calls made
            tool_results: Results of tool calls
        """
        # Process tool calls
        for tc in tool_calls:
            tool_name = tc.get("tool_name", tc.get("name", ""))
            args = tc.get("arguments", tc.get("args", {}))

            if tool_name in ("edit_file", "write_file"):
                file_path = args.get("file_path", "")
                if file_path:
                    self.record_file_modified(file_path)

            elif tool_name == "read_file":
                file_path = args.get("file_path", "")
                if file_path:
                    self.record_file_read(file_path)

        # Process tool results for errors
        for result in tool_results:
            if result.get("status") == "error":
                error = result.get("error", "Unknown error")
                tool_name = result.get("tool_name", "")
                self.record_error(f"{tool_name}: {error}")

        # Extract decisions from assistant content
        decision_indicators = [
            "I'll",
            "I will",
            "Let's",
            "We should",
            "The best approach",
            "I've decided",
            "I'm going to",
        ]

        for indicator in decision_indicators:
            if indicator in assistant_content:
                # Extract the sentence
                sentences = assistant_content.split(". ")
                for sentence in sentences:
                    if indicator in sentence:
                        self.record_decision(sentence.strip())
                        break
                break

        # Update turn count
        self.increment_turn()

    def get_summary_text(self) -> str:
        """Get summary as formatted text for context."""
        parts = []

        if self._summary.task_description:
            parts.append(f"Task: {self._summary.task_description}")

        parts.append(f"Current state: {self._summary.current_state}")
        parts.append(f"Turns: {self._summary.turn_count}")

        if self._summary.files_modified:
            files = ", ".join(self._summary.files_modified[-5:])
            if len(self._summary.files_modified) > 5:
                files += f" (+{len(self._summary.files_modified) - 5} more)"
            parts.append(f"Files modified: {files}")

        if self._summary.files_read:
            files = ", ".join(self._summary.files_read[-5:])
            if len(self._summary.files_read) > 5:
                files += f" (+{len(self._summary.files_read) - 5} more)"
            parts.append(f"Files read: {files}")

        if self._summary.key_decisions:
            parts.append("Key decisions:")
            for decision in self._summary.key_decisions[-3:]:
                parts.append(f"  - {decision}")

        if self._summary.errors_encountered:
            unresolved = [
                e for e in self._summary.errors_encountered
                if not e.startswith("[RESOLVED]")
            ]
            if unresolved:
                parts.append("Errors:")
                for error in unresolved[-3:]:
                    parts.append(f"  - {error}")

        return "\n".join(parts)

    def estimate_tokens(self) -> int:
        """Estimate token count of summary."""
        text = self.get_summary_text()
        return len(text) // 4 + 1

    def reset(self) -> None:
        """Reset summary for new task."""
        self._summary = RollingSummary(
            task_description="",
            files_modified=[],
            files_read=[],
            key_decisions=[],
            errors_encountered=[],
            current_state="Session started",
            turn_count=0,
        )
        self._last_update_turn = 0
        self._pending_updates = []
