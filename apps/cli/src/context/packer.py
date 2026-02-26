"""
Context Packer for Model B.

The main context packing coordinator - assembles all context components
into efficient packages for LLM requests.

Components:
- TokenBudgetManager: Allocates token budget across components
- ToolOutputCompressor: Compresses tool results
- SelectiveFileSlicer: Extracts relevant file slices
- RollingSummaryManager: Maintains session summary
- PinnedFactsManager: Manages important facts
- DeltaBuilder: Builds context deltas
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from packages.shared_protocol.types import (
    ContextPack,
    ContextDelta,
    Message,
    TokenBudget,
)

from .budget import TokenBudgetManager, TokenBudgetConfig
from .compressor import ToolOutputCompressor, CompressionConfig
from .slicer import SelectiveFileSlicer, SliceConfig, SliceRequest
from .summary import RollingSummaryManager, SummaryConfig
from .facts import PinnedFactsManager, FactsConfig
from .delta import DeltaBuilder, DeltaConfig

logger = logging.getLogger(__name__)


@dataclass
class PackerConfig:
    """Configuration for context packing."""
    budget: TokenBudgetConfig = None
    compression: CompressionConfig = None
    slicer: SliceConfig = None
    summary: SummaryConfig = None
    facts: FactsConfig = None
    delta: DeltaConfig = None

    def __post_init__(self):
        self.budget = self.budget or TokenBudgetConfig()
        self.compression = self.compression or CompressionConfig()
        self.slicer = self.slicer or SliceConfig()
        self.summary = self.summary or SummaryConfig()
        self.facts = self.facts or FactsConfig()
        self.delta = self.delta or DeltaConfig()


class ContextPacker:
    """
    The main context packing coordinator.

    This is the "cost killer" - responsible for 40-60% token reduction.

    Workflow:
    1. Process each turn (extract facts, update summary)
    2. Build context pack (stable elements)
    3. Build delta (new messages since checkpoint)
    4. Return pack + delta for LLM request

    The server rehydrates:
    - Context from checkpoint (if context_ref provided)
    - Applies delta
    - Adds context pack as system context
    """

    def __init__(
        self,
        workspace_dir: str,
        config: Optional[PackerConfig] = None,
    ):
        self.config = config or PackerConfig()
        self.workspace_dir = workspace_dir

        # Initialize components
        self.budget = TokenBudgetManager(self.config.budget)
        self.compressor = ToolOutputCompressor(self.config.compression)
        self.slicer = SelectiveFileSlicer(workspace_dir, self.config.slicer)
        self.summary = RollingSummaryManager(self.config.summary)
        self.facts = PinnedFactsManager(self.config.facts)
        self.delta = DeltaBuilder(self.config.delta, self.compressor)

        # Slice requests accumulated during session
        self._slice_requests: list[SliceRequest] = []

    def set_task(self, description: str) -> None:
        """Set the current task description."""
        self.summary.set_task(description)

    def process_user_message(self, content: str) -> None:
        """Process a user message."""
        self.delta.add_user_message(content)
        self.facts.extract_from_content(content, role="user")

    def process_assistant_response(
        self,
        content: str,
        tool_calls: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """Process an assistant response."""
        self.delta.add_assistant_message(content, tool_calls)
        self.facts.extract_from_content(content, role="assistant")

    def process_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """
        Process a tool result.

        Compresses the result and updates context components.
        """
        # Add to delta (compression happens inside)
        self.delta.add_tool_result(tool_call_id, tool_name, result)

        # Extract facts
        self.facts.extract_from_tool_result(tool_name, args, result)

        # Update summary
        self.summary.process_turn(
            assistant_content="",
            tool_calls=[{"tool_name": tool_name, "args": args}],
            tool_results=[{"tool_name": tool_name, "status": result.get("status"), "result": result}],
        )

        # Add slice requests for relevant tools
        if tool_name == "grep":
            matches = result.get("result", {}).get("matches", [])
            for match in matches[:5]:
                if ":" in match:
                    parts = match.split(":")
                    file_path = parts[0]
                    try:
                        line_num = int(parts[1])
                        self._slice_requests.append(SliceRequest(
                            file_path=file_path,
                            line_number=line_num,
                            reason="grep_hit",
                            context=args.get("pattern", ""),
                            priority=2,
                        ))
                    except (ValueError, IndexError) as e:
                        logger.debug(f"Failed to parse grep result line '{line}': {e}")

        elif tool_name == "read_file":
            file_path = args.get("file_path", "")
            if file_path:
                self._slice_requests.append(SliceRequest(
                    file_path=file_path,
                    line_number=1,
                    reason="file_read",
                    priority=1,
                ))

    def process_error(self, error: str, file_path: str = "", line_num: int = 0) -> None:
        """Process an error (e.g., from traceback)."""
        self.summary.record_error(error)

        if file_path and line_num > 0:
            self._slice_requests.append(SliceRequest(
                file_path=file_path,
                line_number=line_num,
                reason="traceback",
                context=error,
                priority=5,
            ))

    def build(self) -> Tuple[ContextPack, ContextDelta]:
        """
        Build the context pack and delta.

        Returns:
            Tuple of (ContextPack, ContextDelta)
        """
        # Reset budget
        self.budget.reset()

        # Generate file slices
        slices = self.slicer.slice(self._slice_requests)

        # Build context pack
        pack = ContextPack(
            rolling_summary=self.summary.summary,
            pinned_facts=self.facts.facts,
            file_slices=slices,
            token_count=self._estimate_pack_tokens(slices),
        )

        # Allocate budget
        self.budget.allocate("rolling_summary", self.summary.estimate_tokens())
        self.budget.allocate("pinned_facts", self.facts.estimate_tokens())
        self.budget.allocate("file_context", sum(len(s.content) // 4 for s in slices))

        # Build delta
        remaining = self.budget.get_remaining("delta")
        self.delta.truncate_to_fit(remaining)
        delta = self.delta.build()

        self.budget.allocate("delta", delta.token_count)

        logger.debug(f"Context pack built: {self.budget.get_summary()}")

        return pack, delta

    def mark_checkpoint(self) -> None:
        """Mark current state as checkpointed."""
        self.delta.mark_checkpoint()
        self._slice_requests.clear()  # Clear old requests

    def _estimate_pack_tokens(self, slices: list) -> int:
        """Estimate total tokens in context pack."""
        tokens = 0
        tokens += self.summary.estimate_tokens()
        tokens += self.facts.estimate_tokens()
        tokens += sum(len(s.content) // 4 for s in slices)
        return tokens

    def get_budget_summary(self) -> dict[str, dict[str, int]]:
        """Get current budget usage summary."""
        return self.budget.get_summary()

    def reset(self) -> None:
        """Reset all state for new task."""
        self.budget.reset()
        self.summary.reset()
        self.facts.reset()
        self.delta.reset()
        self._slice_requests.clear()
        self.slicer.clear_cache()

    def serialize_state(self) -> dict[str, Any]:
        """Serialize packer state for checkpointing."""
        return {
            "summary": self.summary.summary.to_dict(),
            "facts": [f.to_dict() for f in self.facts.facts],
            "conversation": self.delta.get_full_conversation(),
            "slice_requests": [
                {
                    "file_path": r.file_path,
                    "line_number": r.line_number,
                    "reason": r.reason,
                    "context": r.context,
                    "priority": r.priority,
                }
                for r in self._slice_requests
            ],
        }

    def load_state(self, data: dict[str, Any]) -> None:
        """Load packer state from checkpoint."""
        from packages.shared_protocol.types import RollingSummary, PinnedFact
        from datetime import datetime

        # Load summary
        summary_data = data.get("summary", {})
        self.summary._summary = RollingSummary(
            task_description=summary_data.get("task_description", ""),
            files_modified=summary_data.get("files_modified", []),
            files_read=summary_data.get("files_read", []),
            key_decisions=summary_data.get("key_decisions", []),
            errors_encountered=summary_data.get("errors_encountered", []),
            current_state=summary_data.get("current_state", ""),
            turn_count=summary_data.get("turn_count", 0),
            last_updated_at=datetime.fromisoformat(summary_data["last_updated_at"])
                if summary_data.get("last_updated_at") else None,
        )

        # Load facts
        self.facts._facts.clear()
        for fact_data in data.get("facts", []):
            fact = PinnedFact(
                key=fact_data["key"],
                value=fact_data["value"],
                category=fact_data["category"],
                importance=fact_data.get("importance", 1.0),
                use_count=fact_data.get("use_count", 0),
                created_at=datetime.fromisoformat(fact_data["created_at"])
                    if fact_data.get("created_at") else None,
                last_used_at=datetime.fromisoformat(fact_data["last_used_at"])
                    if fact_data.get("last_used_at") else None,
            )
            self.facts._facts[fact.key] = fact

        # Load conversation
        self.delta.load_conversation(data.get("conversation", []))

        # Load slice requests
        self._slice_requests = [
            SliceRequest(
                file_path=r["file_path"],
                line_number=r["line_number"],
                reason=r["reason"],
                context=r.get("context", ""),
                priority=r.get("priority", 1),
            )
            for r in data.get("slice_requests", [])
        ]
