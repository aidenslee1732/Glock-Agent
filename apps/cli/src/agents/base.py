"""Base agent class and types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional
import uuid


class AgentModelTier(str, Enum):
    """Model tier for agent execution."""
    FAST = "fast"          # Haiku - quick, simple tasks
    STANDARD = "standard"  # Sonnet - balanced
    ADVANCED = "advanced"  # Opus - complex reasoning


@dataclass
class AgentContext:
    """Context passed to an agent during execution.

    Attributes:
        prompt: The task/prompt for the agent
        workspace_dir: Working directory
        session_id: Parent session ID
        parent_agent_id: ID of agent that spawned this one (if any)
        conversation_history: Previous messages (if agent has context access)
        max_turns: Maximum number of turns for this execution
        model_tier: Which model tier to use
        allowed_tools: List of allowed tool names (None = all)
        read_only: If True, agent cannot use write/edit tools
        metadata: Additional context metadata
    """
    prompt: str
    workspace_dir: str
    session_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
    conversation_history: list[dict] = field(default_factory=list)
    max_turns: int = 50
    model_tier: AgentModelTier = AgentModelTier.STANDARD
    allowed_tools: Optional[list[str]] = None
    read_only: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """Result from an agent execution.

    Attributes:
        agent_id: Unique ID for this agent execution
        status: "completed", "failed", "stopped", "max_turns_reached"
        output: Final output/response from the agent
        turns_used: Number of turns consumed
        tokens_used: Total tokens consumed
        tools_called: List of tools that were called
        files_modified: List of files that were modified
        error: Error message if failed
        started_at: When execution started
        completed_at: When execution completed
        metadata: Additional result metadata
    """
    agent_id: str
    status: str
    output: str = ""
    turns_used: int = 0
    tokens_used: int = 0
    tools_called: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    error: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "output": self.output,
            "turns_used": self.turns_used,
            "tokens_used": self.tokens_used,
            "tools_called": self.tools_called,
            "files_modified": self.files_modified,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class BaseAgent:
    """Base class for all specialized agents.

    Agents are specialized assistants that handle specific types of tasks.
    Each agent has:
    - A name and description
    - A system prompt with domain expertise
    - A list of allowed tools
    - Optional read-only mode
    - Model tier preference
    """

    # Agent identity
    name: str = "base"
    description: str = "Base agent"

    # System prompt (loaded from file or set directly)
    system_prompt: str = ""

    # Tool access
    allowed_tools: Optional[list[str]] = None  # None = all tools
    read_only: bool = False

    # Execution settings
    max_turns: int = 50
    model_tier: AgentModelTier = AgentModelTier.STANDARD

    # Context access
    has_context_access: bool = False  # Can see conversation history

    def __init__(self):
        """Initialize the agent."""
        self.agent_id = f"agent_{uuid.uuid4().hex[:12]}"

    def get_system_prompt(self, context: AgentContext) -> str:
        """Get the system prompt for this agent.

        Override this to customize prompt based on context.

        Args:
            context: The agent context

        Returns:
            System prompt string
        """
        return self.system_prompt

    def get_allowed_tools(self, context: AgentContext) -> Optional[list[str]]:
        """Get the list of allowed tools for this agent.

        Override this to customize based on context.

        Args:
            context: The agent context

        Returns:
            List of tool names, or None for all tools
        """
        if self.read_only or context.read_only:
            # Filter to read-only tools
            read_only_tools = [
                "read_file", "glob", "grep", "list_directory",
                "web_fetch", "web_search",
                "TaskList", "TaskGet",
            ]
            if self.allowed_tools:
                return [t for t in self.allowed_tools if t in read_only_tools]
            return read_only_tools

        return self.allowed_tools

    def validate_context(self, context: AgentContext) -> Optional[str]:
        """Validate the context before execution.

        Override this to add custom validation.

        Args:
            context: The agent context

        Returns:
            Error message if invalid, None if valid
        """
        if not context.prompt:
            return "Prompt is required"
        return None

    async def pre_execute(self, context: AgentContext) -> None:
        """Called before agent execution begins.

        Override this for setup logic.

        Args:
            context: The agent context
        """
        pass

    async def post_execute(self, context: AgentContext, result: AgentResult) -> AgentResult:
        """Called after agent execution completes.

        Override this for cleanup or result modification.

        Args:
            context: The agent context
            result: The execution result

        Returns:
            Modified result
        """
        return result

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name={self.name}, id={self.agent_id})>"
