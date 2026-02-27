"""Base skill class and types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Optional


@dataclass
class SkillResult:
    """Result from skill execution.

    Attributes:
        status: "completed", "failed", "cancelled"
        output: Output text/data from the skill
        error: Error message if failed
        metadata: Additional result metadata
    """
    status: str
    output: str = ""
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata,
        }


@dataclass
class Skill:
    """Represents a skill that can be invoked via slash commands.

    Skills are predefined workflows that can be invoked by users
    via slash commands (e.g., /commit, /review-pr).

    Attributes:
        name: Skill name (used as slash command)
        description: Human-readable description
        handler: Async function to execute the skill
        args_schema: Optional JSON schema for arguments
        aliases: Alternative names for this skill
        category: Category for organization
        requires_tools: List of required tools
    """
    name: str
    description: str
    handler: Callable[..., Coroutine[Any, Any, SkillResult]]
    args_schema: Optional[dict] = None
    aliases: list[str] = field(default_factory=list)
    category: str = "general"
    requires_tools: list[str] = field(default_factory=list)

    def __post_init__(self):
        """Validate skill configuration."""
        if not self.name:
            raise ValueError("Skill name is required")
        if not self.handler:
            raise ValueError("Skill handler is required")

    async def invoke(self, args: str = "", context: Optional[dict] = None) -> SkillResult:
        """Invoke the skill.

        Args:
            args: Arguments string from user
            context: Execution context (workspace_dir, tool_broker, etc.)

        Returns:
            SkillResult with output
        """
        context = context or {}
        try:
            return await self.handler(args, context)
        except Exception as e:
            return SkillResult(
                status="failed",
                error=str(e),
            )


# Type alias for skill handlers
SkillHandler = Callable[[str, dict], Coroutine[Any, Any, SkillResult]]
