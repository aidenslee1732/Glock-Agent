"""Parallel Tool Executor for Model B.

Executes independent tool calls in parallel while preserving
sequential ordering for dependent operations (writes to same file).

This provides 5-10x speedup for multi-file operations like:
- Reading multiple files simultaneously
- Running multiple grep/glob searches
- Parallel web fetches
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..tools.broker import ToolBroker, ToolResult

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """A tool call to execute."""
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ParallelExecutionResult:
    """Result of parallel tool execution."""
    tool_call_id: str
    tool_name: str
    result: Any  # ToolResult
    duration_ms: int
    error: Optional[str] = None
    was_parallel: bool = False


@dataclass
class ExecutionBatch:
    """A batch of tool calls to execute."""
    independent: list[ToolCall] = field(default_factory=list)
    dependent: list[ToolCall] = field(default_factory=list)


class ParallelToolExecutor:
    """Execute independent tool calls in parallel.

    Analyzes dependencies between tool calls:
    - Read operations can execute in parallel
    - Write operations to different files can execute in parallel
    - Write operations to the same file must be sequential
    - Bash commands are executed sequentially by default (may have side effects)

    Example speedup:
    - Sequential: 10 file reads @ 100ms each = 1000ms
    - Parallel: 10 file reads @ 100ms each = ~150ms (with overhead)
    """

    # Tools that are safe to parallelize (read-only or idempotent)
    PARALLELIZABLE_TOOLS = frozenset({
        "read_file", "Read",
        "glob", "Glob",
        "grep", "Grep",
        "list_directory",
        "web_fetch", "WebFetch",
        "web_search", "WebSearch",
        "git_status", "git_diff", "git_log",
        "TaskList", "TaskGet",
        "mcp_list_tools", "mcp_server_status",
        "hook_list",
    })

    # Tools that modify files (need to check for conflicts)
    FILE_MODIFYING_TOOLS = frozenset({
        "edit_file", "Edit",
        "write_file", "Write",
        "NotebookEdit",
    })

    # Tools that should always run sequentially (side effects)
    SEQUENTIAL_TOOLS = frozenset({
        "bash", "Bash",
        "git_commit", "git_push", "git_branch",
        "TaskCreate", "TaskUpdate", "TaskStop",
        "Task",  # Agent spawning
        "Skill",  # Skill invocation
        "EnterPlanMode", "ExitPlanMode",
        "AskUserQuestion",
    })

    def __init__(
        self,
        tool_broker: "ToolBroker",
        max_concurrency: int = 10,
        enable_parallel: bool = True,
    ):
        """Initialize parallel executor.

        Args:
            tool_broker: The tool broker for execution
            max_concurrency: Maximum concurrent tool executions
            enable_parallel: Whether to enable parallel execution
        """
        self.tool_broker = tool_broker
        self.max_concurrency = max_concurrency
        self.enable_parallel = enable_parallel
        self._semaphore = asyncio.Semaphore(max_concurrency)

        # Track metrics
        self._total_executions = 0
        self._parallel_executions = 0
        self._time_saved_ms = 0

    def analyze_dependencies(
        self,
        tool_calls: list[ToolCall],
    ) -> ExecutionBatch:
        """Analyze tool calls and separate into independent and dependent groups.

        Args:
            tool_calls: List of tool calls to analyze

        Returns:
            ExecutionBatch with independent and dependent tool calls
        """
        if not self.enable_parallel or len(tool_calls) <= 1:
            return ExecutionBatch(dependent=tool_calls)

        independent: list[ToolCall] = []
        dependent: list[ToolCall] = []

        # Track files being modified to detect conflicts
        files_being_modified: set[str] = set()

        for tc in tool_calls:
            tool_name = tc.tool_name

            # Always sequential tools
            if tool_name in self.SEQUENTIAL_TOOLS:
                dependent.append(tc)
                continue

            # Parallelizable read-only tools
            if tool_name in self.PARALLELIZABLE_TOOLS:
                independent.append(tc)
                continue

            # File-modifying tools - check for conflicts
            if tool_name in self.FILE_MODIFYING_TOOLS:
                file_path = tc.arguments.get("file_path", "")
                if file_path in files_being_modified:
                    # Conflict - must be sequential
                    dependent.append(tc)
                else:
                    # No conflict - can parallelize with other writes
                    files_being_modified.add(file_path)
                    independent.append(tc)
                continue

            # Unknown tools - default to sequential for safety
            dependent.append(tc)

        logger.debug(
            f"Dependency analysis: {len(independent)} independent, "
            f"{len(dependent)} dependent from {len(tool_calls)} total"
        )

        return ExecutionBatch(independent=independent, dependent=dependent)

    async def execute_batch(
        self,
        tool_calls: list[ToolCall],
        on_tool_start: Optional[Callable[[ToolCall], None]] = None,
        on_tool_end: Optional[Callable[[ParallelExecutionResult], None]] = None,
    ) -> list[ParallelExecutionResult]:
        """Execute a batch of tool calls, parallelizing where safe.

        Args:
            tool_calls: Tool calls to execute
            on_tool_start: Callback when tool starts
            on_tool_end: Callback when tool ends

        Returns:
            List of execution results in original order
        """
        if not tool_calls:
            return []

        start_time = time.time()
        batch = self.analyze_dependencies(tool_calls)

        results: dict[str, ParallelExecutionResult] = {}

        # Execute independent tools in parallel
        if batch.independent:
            parallel_results = await self._execute_parallel(
                batch.independent,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
            )
            for result in parallel_results:
                results[result.tool_call_id] = result
                self._parallel_executions += 1

        # Execute dependent tools sequentially
        for tc in batch.dependent:
            result = await self._execute_single(
                tc,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
            )
            results[result.tool_call_id] = result

        # Calculate time saved
        total_time = int((time.time() - start_time) * 1000)
        sequential_time = sum(r.duration_ms for r in results.values())
        if sequential_time > total_time:
            self._time_saved_ms += sequential_time - total_time

        self._total_executions += len(tool_calls)

        # Return results in original order
        ordered_results = []
        for tc in tool_calls:
            if tc.tool_call_id in results:
                ordered_results.append(results[tc.tool_call_id])

        return ordered_results

    async def _execute_parallel(
        self,
        tool_calls: list[ToolCall],
        on_tool_start: Optional[Callable[[ToolCall], None]] = None,
        on_tool_end: Optional[Callable[[ParallelExecutionResult], None]] = None,
    ) -> list[ParallelExecutionResult]:
        """Execute multiple tool calls in parallel."""
        tasks = []

        for tc in tool_calls:
            task = asyncio.create_task(
                self._execute_with_semaphore(
                    tc,
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    mark_parallel=True,
                )
            )
            tasks.append(task)

        # Wait for all tasks
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        execution_results = []
        for i, result in enumerate(results):
            if isinstance(result, ParallelExecutionResult):
                execution_results.append(result)
            elif isinstance(result, Exception):
                # Create error result
                tc = tool_calls[i]
                execution_results.append(ParallelExecutionResult(
                    tool_call_id=tc.tool_call_id,
                    tool_name=tc.tool_name,
                    result=None,
                    duration_ms=0,
                    error=str(result),
                    was_parallel=True,
                ))
                logger.error(f"Parallel execution failed for {tc.tool_name}: {result}")

        return execution_results

    async def _execute_with_semaphore(
        self,
        tool_call: ToolCall,
        on_tool_start: Optional[Callable[[ToolCall], None]] = None,
        on_tool_end: Optional[Callable[[ParallelExecutionResult], None]] = None,
        mark_parallel: bool = False,
    ) -> ParallelExecutionResult:
        """Execute a tool call with semaphore for concurrency control."""
        async with self._semaphore:
            return await self._execute_single(
                tool_call,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
                mark_parallel=mark_parallel,
            )

    async def _execute_single(
        self,
        tool_call: ToolCall,
        on_tool_start: Optional[Callable[[ToolCall], None]] = None,
        on_tool_end: Optional[Callable[[ParallelExecutionResult], None]] = None,
        mark_parallel: bool = False,
    ) -> ParallelExecutionResult:
        """Execute a single tool call."""
        if on_tool_start:
            on_tool_start(tool_call)

        start_time = time.time()
        error = None
        result = None

        try:
            result = await self.tool_broker.execute(
                tool_call.tool_name,
                tool_call.arguments,
            )
        except Exception as e:
            error = str(e)
            logger.error(f"Tool execution error: {tool_call.tool_name}: {e}")

        duration_ms = int((time.time() - start_time) * 1000)

        execution_result = ParallelExecutionResult(
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.tool_name,
            result=result,
            duration_ms=duration_ms,
            error=error,
            was_parallel=mark_parallel,
        )

        if on_tool_end:
            on_tool_end(execution_result)

        return execution_result

    def get_metrics(self) -> dict[str, Any]:
        """Get execution metrics."""
        return {
            "total_executions": self._total_executions,
            "parallel_executions": self._parallel_executions,
            "parallelization_rate": (
                self._parallel_executions / self._total_executions
                if self._total_executions > 0 else 0
            ),
            "estimated_time_saved_ms": self._time_saved_ms,
            "max_concurrency": self.max_concurrency,
        }

    def reset_metrics(self) -> None:
        """Reset execution metrics."""
        self._total_executions = 0
        self._parallel_executions = 0
        self._time_saved_ms = 0
