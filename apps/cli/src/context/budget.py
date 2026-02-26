"""
Token Budget Manager for Model B.

Manages token allocation across context components:
- System prompt
- Pinned facts
- Rolling summary
- File context
- Tool results
- Conversation history
- Delta
- Completion reserve
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TokenBudgetConfig:
    """Configuration for token budgets."""
    # Total context window size
    total_context: int = 100_000

    # Allocations (should sum to less than total_context)
    system_prompt: int = 2_000
    pinned_facts: int = 3_000        # ~30 items at ~100 tokens each
    rolling_summary: int = 4_000
    file_context: int = 15_000
    tool_results: int = 8_000
    conversation: int = 10_000
    delta: int = 5_000
    completion_reserve: int = 8_000

    def validate(self) -> bool:
        """Validate that allocations don't exceed total."""
        total_allocated = (
            self.system_prompt +
            self.pinned_facts +
            self.rolling_summary +
            self.file_context +
            self.tool_results +
            self.conversation +
            self.delta +
            self.completion_reserve
        )
        return total_allocated <= self.total_context


@dataclass
class BudgetUsage:
    """Current budget usage."""
    system_prompt: int = 0
    pinned_facts: int = 0
    rolling_summary: int = 0
    file_context: int = 0
    tool_results: int = 0
    conversation: int = 0
    delta: int = 0

    @property
    def total(self) -> int:
        """Total tokens used."""
        return (
            self.system_prompt +
            self.pinned_facts +
            self.rolling_summary +
            self.file_context +
            self.tool_results +
            self.conversation +
            self.delta
        )


class TokenBudgetManager:
    """
    Manages token allocation for context packing.

    The cost killer - careful budget management is key to
    reducing token usage by 40-60%.

    Budget philosophy:
    - Each component has a dedicated allocation
    - Overflow from one component doesn't steal from others
    - Completion reserve is sacred (never allocate)
    - Dynamic rebalancing based on task needs
    """

    def __init__(self, config: Optional[TokenBudgetConfig] = None):
        self.config = config or TokenBudgetConfig()
        if not self.config.validate():
            logger.warning("Token budget allocations exceed total context")

        self._usage = BudgetUsage()

    @property
    def usage(self) -> BudgetUsage:
        """Get current usage."""
        return self._usage

    @property
    def total_available(self) -> int:
        """Total tokens available (excluding completion reserve)."""
        return self.config.total_context - self.config.completion_reserve

    @property
    def total_used(self) -> int:
        """Total tokens currently used."""
        return self._usage.total

    @property
    def remaining(self) -> int:
        """Remaining tokens available."""
        return self.total_available - self.total_used

    def get_budget(self, component: str) -> int:
        """Get budget for a component."""
        return getattr(self.config, component, 0)

    def get_remaining(self, component: str) -> int:
        """Get remaining budget for a component."""
        budget = self.get_budget(component)
        used = getattr(self._usage, component, 0)
        return max(0, budget - used)

    def allocate(self, component: str, tokens: int) -> int:
        """
        Allocate tokens from a component's budget.

        Args:
            component: Budget component name
            tokens: Tokens to allocate

        Returns:
            Actual tokens allocated (may be less if budget exhausted)
        """
        remaining = self.get_remaining(component)
        allocated = min(tokens, remaining)

        current = getattr(self._usage, component, 0)
        setattr(self._usage, component, current + allocated)

        if allocated < tokens:
            logger.debug(
                f"Budget for {component} exhausted: "
                f"requested {tokens}, allocated {allocated}"
            )

        return allocated

    def can_allocate(self, component: str, tokens: int) -> bool:
        """Check if tokens can be allocated."""
        return self.get_remaining(component) >= tokens

    def reset(self) -> None:
        """Reset all usage to zero."""
        self._usage = BudgetUsage()

    def rebalance(self, priorities: dict[str, float]) -> None:
        """
        Rebalance budgets based on priorities.

        This allows dynamic adjustment based on task type.
        For example, a code-heavy task might prioritize file_context,
        while a conversational task might prioritize conversation.

        Args:
            priorities: Component -> priority weight (sum to 1.0)
        """
        # Calculate total rebalanceable budget
        # (everything except system_prompt and completion_reserve)
        rebalanceable = (
            self.config.pinned_facts +
            self.config.rolling_summary +
            self.config.file_context +
            self.config.tool_results +
            self.config.conversation +
            self.config.delta
        )

        # Normalize priorities
        total_priority = sum(priorities.values())
        if total_priority == 0:
            return

        normalized = {k: v / total_priority for k, v in priorities.items()}

        # Reallocate
        for component, priority in normalized.items():
            if hasattr(self.config, component):
                new_budget = int(rebalanceable * priority)
                setattr(self.config, component, new_budget)

        logger.debug(f"Rebalanced budgets: {priorities}")

    def get_summary(self) -> dict[str, dict[str, int]]:
        """Get budget summary."""
        components = [
            "system_prompt",
            "pinned_facts",
            "rolling_summary",
            "file_context",
            "tool_results",
            "conversation",
            "delta",
        ]

        summary = {}
        for component in components:
            summary[component] = {
                "budget": self.get_budget(component),
                "used": getattr(self._usage, component, 0),
                "remaining": self.get_remaining(component),
            }

        summary["_total"] = {
            "budget": self.total_available,
            "used": self.total_used,
            "remaining": self.remaining,
        }

        return summary

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text."""
        # Rough estimate: ~4 characters per token
        return len(text) // 4 + 1
