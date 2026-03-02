"""Sub-Agent Coordination - Spawn and manage parallel agents.

Provides Claude Code-like sub-agent capabilities:
- Spawn sub-agents with limited tool access
- Execute multiple tasks in parallel
- Aggregate results from parallel execution
- Resource and concurrency management
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


class SubAgentStatus(str, Enum):
    """Status of a sub-agent."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass
class SubAgentResult:
    """Result from a sub-agent execution."""

    agent_id: str
    task: str
    status: SubAgentStatus
    output: Any = None
    error: Optional[str] = None
    execution_time_ms: int = 0
    tools_used: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubAgent:
    """A spawned sub-agent."""

    id: str
    task: str
    tools: list[str]
    status: SubAgentStatus = SubAgentStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[SubAgentResult] = None

    @property
    def execution_time_ms(self) -> int:
        """Get execution time in milliseconds."""
        if self.started_at is None:
            return 0
        end = self.completed_at or datetime.utcnow()
        return int((end - self.started_at).total_seconds() * 1000)


@dataclass
class SubAgentConfig:
    """Configuration for sub-agent coordination."""

    # Maximum concurrent sub-agents
    max_concurrent: int = 5

    # Default timeout per sub-agent (seconds)
    default_timeout: float = 120.0

    # Maximum total sub-agents in a session
    max_total_agents: int = 50

    # Default tools available to sub-agents
    default_tools: list[str] = field(default_factory=lambda: [
        "read_file",
        "grep",
        "glob",
        "list_directory",
    ])

    # Tools that require explicit permission
    restricted_tools: list[str] = field(default_factory=lambda: [
        "bash",
        "write_file",
        "edit_file",
        "web_fetch",
    ])


class SubAgentCoordinator:
    """Coordinates sub-agent spawning and execution.

    Manages parallel execution of sub-agents with:
    - Concurrency limits
    - Tool access control
    - Result aggregation
    - Timeout handling
    """

    def __init__(
        self,
        config: Optional[SubAgentConfig] = None,
        tool_executor: Optional[Callable[[str, dict], Coroutine[Any, Any, Any]]] = None,
        llm_callback: Optional[Callable[[str, str], Coroutine[Any, Any, str]]] = None,
    ):
        """Initialize coordinator.

        Args:
            config: Coordination configuration
            tool_executor: Function to execute tools
            llm_callback: Function to call LLM for agent reasoning
        """
        self.config = config or SubAgentConfig()
        self._tool_executor = tool_executor
        self._llm_callback = llm_callback

        self._agents: dict[str, SubAgent] = {}
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent)
        self._total_spawned = 0

    async def spawn(
        self,
        task: str,
        tools: Optional[list[str]] = None,
        timeout: Optional[float] = None,
    ) -> SubAgent:
        """Spawn a new sub-agent.

        Args:
            task: Task description for the sub-agent
            tools: List of tools the agent can use
            timeout: Timeout for this agent (uses default if None)

        Returns:
            Spawned SubAgent

        Raises:
            RuntimeError: If max total agents exceeded
        """
        if self._total_spawned >= self.config.max_total_agents:
            raise RuntimeError(
                f"Maximum total agents ({self.config.max_total_agents}) exceeded"
            )

        # Determine tools
        if tools is None:
            tools = self.config.default_tools.copy()
        else:
            # Filter to allowed tools
            tools = [t for t in tools if self._is_tool_allowed(t)]

        agent_id = str(uuid.uuid4())[:8]
        agent = SubAgent(
            id=agent_id,
            task=task,
            tools=tools,
        )

        self._agents[agent_id] = agent
        self._total_spawned += 1

        logger.info(f"Spawned sub-agent {agent_id}: {task[:50]}...")
        return agent

    async def execute(
        self,
        agent: SubAgent,
        timeout: Optional[float] = None,
    ) -> SubAgentResult:
        """Execute a sub-agent's task.

        Args:
            agent: SubAgent to execute
            timeout: Execution timeout

        Returns:
            SubAgentResult with execution details
        """
        timeout = timeout or self.config.default_timeout

        async with self._semaphore:
            agent.status = SubAgentStatus.RUNNING
            agent.started_at = datetime.utcnow()

            try:
                result = await asyncio.wait_for(
                    self._execute_agent(agent),
                    timeout=timeout,
                )
                agent.status = SubAgentStatus.COMPLETED
                agent.result = result

            except asyncio.TimeoutError:
                agent.status = SubAgentStatus.TIMEOUT
                result = SubAgentResult(
                    agent_id=agent.id,
                    task=agent.task,
                    status=SubAgentStatus.TIMEOUT,
                    error=f"Execution timed out after {timeout}s",
                )
                agent.result = result

            except Exception as e:
                agent.status = SubAgentStatus.FAILED
                result = SubAgentResult(
                    agent_id=agent.id,
                    task=agent.task,
                    status=SubAgentStatus.FAILED,
                    error=str(e),
                )
                agent.result = result

            agent.completed_at = datetime.utcnow()
            return result

    async def execute_parallel(
        self,
        tasks: list[str],
        tools: Optional[list[str]] = None,
        timeout: Optional[float] = None,
    ) -> list[SubAgentResult]:
        """Execute multiple tasks in parallel.

        Args:
            tasks: List of task descriptions
            tools: Tools available to all agents
            timeout: Timeout per agent

        Returns:
            List of SubAgentResults
        """
        # Spawn all agents
        agents = []
        for task in tasks:
            try:
                agent = await self.spawn(task, tools, timeout)
                agents.append(agent)
            except RuntimeError as e:
                logger.warning(f"Failed to spawn agent: {e}")
                break

        # Execute in parallel
        execution_tasks = [
            self.execute(agent, timeout)
            for agent in agents
        ]

        results = await asyncio.gather(*execution_tasks, return_exceptions=True)

        # Process results
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                final_results.append(SubAgentResult(
                    agent_id=agents[i].id,
                    task=agents[i].task,
                    status=SubAgentStatus.FAILED,
                    error=str(result),
                ))
            else:
                final_results.append(result)

        return final_results

    async def _execute_agent(self, agent: SubAgent) -> SubAgentResult:
        """Execute an agent's task using LLM and tools.

        Args:
            agent: SubAgent to execute

        Returns:
            SubAgentResult
        """
        tools_used = []
        output = None

        if self._llm_callback is None:
            # Simple execution without LLM
            return SubAgentResult(
                agent_id=agent.id,
                task=agent.task,
                status=SubAgentStatus.COMPLETED,
                output=f"Task '{agent.task}' would be executed with tools: {agent.tools}",
                tools_used=tools_used,
            )

        # Use LLM to reason about task
        system = f"""You are a sub-agent with access to the following tools: {', '.join(agent.tools)}.

Complete the assigned task efficiently. Focus on:
1. Understanding what information is needed
2. Using the minimum necessary tools
3. Providing a clear, concise result

Available tools: {agent.tools}
"""

        prompt = f"Task: {agent.task}\n\nComplete this task and provide the result."

        try:
            response = await self._llm_callback(system, prompt)
            output = response

            return SubAgentResult(
                agent_id=agent.id,
                task=agent.task,
                status=SubAgentStatus.COMPLETED,
                output=output,
                tools_used=tools_used,
                execution_time_ms=agent.execution_time_ms,
            )
        except Exception as e:
            return SubAgentResult(
                agent_id=agent.id,
                task=agent.task,
                status=SubAgentStatus.FAILED,
                error=str(e),
                tools_used=tools_used,
            )

    def _is_tool_allowed(self, tool: str) -> bool:
        """Check if a tool is allowed for sub-agents."""
        if tool in self.config.restricted_tools:
            return False
        return True

    async def cancel(self, agent_id: str) -> bool:
        """Cancel a running sub-agent.

        Args:
            agent_id: Agent to cancel

        Returns:
            True if cancelled, False if not found or not running
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            return False

        if agent.status != SubAgentStatus.RUNNING:
            return False

        agent.status = SubAgentStatus.CANCELLED
        agent.completed_at = datetime.utcnow()
        return True

    def get_agent(self, agent_id: str) -> Optional[SubAgent]:
        """Get a sub-agent by ID."""
        return self._agents.get(agent_id)

    def get_all_agents(self) -> list[SubAgent]:
        """Get all sub-agents."""
        return list(self._agents.values())

    def get_active_agents(self) -> list[SubAgent]:
        """Get currently running sub-agents."""
        return [
            a for a in self._agents.values()
            if a.status in (SubAgentStatus.PENDING, SubAgentStatus.RUNNING)
        ]

    def stats(self) -> dict[str, Any]:
        """Get coordinator statistics."""
        agents = list(self._agents.values())
        return {
            "total_spawned": self._total_spawned,
            "active": len(self.get_active_agents()),
            "completed": len([a for a in agents if a.status == SubAgentStatus.COMPLETED]),
            "failed": len([a for a in agents if a.status == SubAgentStatus.FAILED]),
            "cancelled": len([a for a in agents if a.status == SubAgentStatus.CANCELLED]),
            "timeout": len([a for a in agents if a.status == SubAgentStatus.TIMEOUT]),
            "max_concurrent": self.config.max_concurrent,
            "max_total": self.config.max_total_agents,
        }

    def clear_completed(self) -> int:
        """Clear completed sub-agents from memory.

        Returns:
            Number of agents cleared
        """
        to_remove = [
            agent_id for agent_id, agent in self._agents.items()
            if agent.status in (
                SubAgentStatus.COMPLETED,
                SubAgentStatus.FAILED,
                SubAgentStatus.CANCELLED,
                SubAgentStatus.TIMEOUT,
            )
        ]

        for agent_id in to_remove:
            del self._agents[agent_id]

        return len(to_remove)


# Factory functions


def create_sub_agent_coordinator(
    tool_executor: Optional[Callable] = None,
    llm_callback: Optional[Callable] = None,
    max_concurrent: int = 5,
) -> SubAgentCoordinator:
    """Create a sub-agent coordinator.

    Args:
        tool_executor: Tool execution function
        llm_callback: LLM callback function
        max_concurrent: Maximum concurrent agents

    Returns:
        Configured SubAgentCoordinator
    """
    config = SubAgentConfig(max_concurrent=max_concurrent)
    return SubAgentCoordinator(
        config=config,
        tool_executor=tool_executor,
        llm_callback=llm_callback,
    )
