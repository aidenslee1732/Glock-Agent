"""Execution Tracing for Model B.

Provides full observability for debugging and analysis:
- Turn-by-turn execution recording
- Tool call tracking
- Token usage monitoring
- Failure analysis

This enables post-mortem analysis and continuous improvement.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TraceEventType(str, Enum):
    """Types of trace events."""
    TASK_START = "task_start"
    TASK_END = "task_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    COUNCIL_START = "council_start"
    COUNCIL_END = "council_end"
    PREFLIGHT_START = "preflight_start"
    PREFLIGHT_END = "preflight_end"
    ERROR = "error"
    CHECKPOINT = "checkpoint"


class TaskOutcome(str, Enum):
    """Outcomes for completed tasks."""
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass
class TraceEvent:
    """A single trace event."""
    event_type: TraceEventType
    timestamp: float
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.event_type.value,
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(self.timestamp).isoformat(),
            "data": self.data,
        }


@dataclass
class ToolTrace:
    """Trace of a single tool call."""
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    start_time: float
    end_time: float
    duration_ms: int
    success: bool
    result_summary: str
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments_summary": self._summarize_args(),
            "duration_ms": self.duration_ms,
            "success": self.success,
            "result_summary": self.result_summary[:500],
            "error": self.error,
        }

    def _summarize_args(self) -> dict[str, Any]:
        """Summarize arguments, truncating large values."""
        summary = {}
        for key, value in self.arguments.items():
            if isinstance(value, str) and len(value) > 100:
                summary[key] = value[:100] + "..."
            elif isinstance(value, (list, dict)) and len(str(value)) > 200:
                summary[key] = f"<{type(value).__name__} with {len(value)} items>"
            else:
                summary[key] = value
        return summary


@dataclass
class TurnTrace:
    """Trace of a single conversation turn."""
    turn_number: int
    start_time: float
    end_time: float
    input_tokens: int
    output_tokens: int
    tool_calls: list[ToolTrace] = field(default_factory=list)
    response_preview: str = ""
    had_error: bool = False
    error_message: Optional[str] = None

    @property
    def duration_ms(self) -> int:
        return int((self.end_time - self.start_time) * 1000)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_number": self.turn_number,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "response_preview": self.response_preview[:200],
            "had_error": self.had_error,
            "error_message": self.error_message,
        }


@dataclass
class ExecutionTrace:
    """Complete trace of a task execution."""
    task_id: str
    task_description: str
    start_time: float
    end_time: Optional[float] = None
    outcome: Optional[TaskOutcome] = None
    turns: list[TurnTrace] = field(default_factory=list)
    events: list[TraceEvent] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    error_message: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        if self.end_time is None:
            return int((time.time() - self.start_time) * 1000)
        return int((self.end_time - self.start_time) * 1000)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def tool_call_count(self) -> int:
        return sum(len(t.tool_calls) for t in self.turns)

    @property
    def success_rate(self) -> float:
        """Tool call success rate."""
        total = 0
        success = 0
        for turn in self.turns:
            for tc in turn.tool_calls:
                total += 1
                if tc.success:
                    success += 1
        return success / total if total > 0 else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_description": self.task_description[:200],
            "outcome": self.outcome.value if self.outcome else None,
            "duration_ms": self.duration_ms,
            "turn_count": self.turn_count,
            "tool_call_count": self.tool_call_count,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "files_modified": self.files_modified,
            "success_rate": self.success_rate,
            "error_message": self.error_message,
            "turns": [t.to_dict() for t in self.turns],
            "metadata": self.metadata,
        }

    def to_json(self, indent: int = 2) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)


class ExecutionTracer:
    """Records execution traces for debugging and analysis.

    Usage:
        tracer = ExecutionTracer()

        # Start a task
        tracer.start_task("task-123", "Implement login feature")

        # Record turns
        tracer.start_turn(1)
        tracer.record_llm_request(tokens=1000)
        tracer.record_tool_call("read_file", {"path": "..."}, result, 50)
        tracer.end_turn(input_tokens=1000, output_tokens=500)

        # End task
        trace = tracer.end_task(TaskOutcome.SUCCESS)

        # Analyze
        analysis = tracer.explain_failure(trace)
    """

    def __init__(
        self,
        persist_path: Optional[Path] = None,
        max_traces: int = 100,
    ):
        """Initialize tracer.

        Args:
            persist_path: Optional path to persist traces
            max_traces: Maximum traces to keep in memory
        """
        self._persist_path = persist_path
        self._max_traces = max_traces

        self._current_trace: Optional[ExecutionTrace] = None
        self._current_turn: Optional[TurnTrace] = None
        self._traces: list[ExecutionTrace] = []

    @property
    def current_trace(self) -> Optional[ExecutionTrace]:
        """Get the current active trace."""
        return self._current_trace

    def start_task(
        self,
        task_id: str,
        task_description: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Start tracing a new task."""
        self._current_trace = ExecutionTrace(
            task_id=task_id,
            task_description=task_description,
            start_time=time.time(),
            metadata=metadata or {},
        )

        self._add_event(TraceEventType.TASK_START, {
            "task_id": task_id,
            "description": task_description[:200],
        })

    def end_task(
        self,
        outcome: TaskOutcome,
        error: Optional[str] = None,
    ) -> ExecutionTrace:
        """End the current task and return the trace."""
        if self._current_trace is None:
            raise ValueError("No active task to end")

        self._current_trace.end_time = time.time()
        self._current_trace.outcome = outcome
        self._current_trace.error_message = error

        self._add_event(TraceEventType.TASK_END, {
            "outcome": outcome.value,
            "duration_ms": self._current_trace.duration_ms,
            "turns": self._current_trace.turn_count,
            "tokens": self._current_trace.total_tokens,
        })

        # Store trace
        trace = self._current_trace
        self._traces.append(trace)

        # Trim old traces
        while len(self._traces) > self._max_traces:
            self._traces.pop(0)

        # Persist if configured
        if self._persist_path:
            self._persist_trace(trace)

        self._current_trace = None
        self._current_turn = None

        return trace

    def start_turn(self, turn_number: int) -> None:
        """Start a new turn."""
        if self._current_trace is None:
            return

        self._current_turn = TurnTrace(
            turn_number=turn_number,
            start_time=time.time(),
            end_time=0,
            input_tokens=0,
            output_tokens=0,
        )

        self._add_event(TraceEventType.TURN_START, {
            "turn_number": turn_number,
        })

    def end_turn(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        response_preview: str = "",
        error: Optional[str] = None,
    ) -> None:
        """End the current turn."""
        if self._current_turn is None:
            return

        self._current_turn.end_time = time.time()
        self._current_turn.input_tokens = input_tokens
        self._current_turn.output_tokens = output_tokens
        self._current_turn.response_preview = response_preview
        self._current_turn.had_error = error is not None
        self._current_turn.error_message = error

        # Update totals
        if self._current_trace:
            self._current_trace.total_input_tokens += input_tokens
            self._current_trace.total_output_tokens += output_tokens
            self._current_trace.turns.append(self._current_turn)

        self._add_event(TraceEventType.TURN_END, {
            "turn_number": self._current_turn.turn_number,
            "duration_ms": self._current_turn.duration_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tool_calls": len(self._current_turn.tool_calls),
        })

        self._current_turn = None

    def record_tool_call(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        success: bool,
        result_summary: str,
        duration_ms: int,
        error: Optional[str] = None,
    ) -> None:
        """Record a tool call."""
        if self._current_turn is None:
            return

        tool_trace = ToolTrace(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            start_time=time.time() - duration_ms / 1000,
            end_time=time.time(),
            duration_ms=duration_ms,
            success=success,
            result_summary=result_summary,
            error=error,
        )

        self._current_turn.tool_calls.append(tool_trace)

        self._add_event(TraceEventType.TOOL_END, {
            "tool_name": tool_name,
            "success": success,
            "duration_ms": duration_ms,
        })

    def record_file_modification(self, file_path: str) -> None:
        """Record a file modification."""
        if self._current_trace and file_path not in self._current_trace.files_modified:
            self._current_trace.files_modified.append(file_path)

    def record_error(self, error: str, context: Optional[dict] = None) -> None:
        """Record an error event."""
        self._add_event(TraceEventType.ERROR, {
            "error": error,
            "context": context or {},
        })

        if self._current_turn:
            self._current_turn.had_error = True
            self._current_turn.error_message = error

    def record_council(
        self,
        approved: bool,
        confidence: float,
        issues: list[str],
        duration_ms: int,
    ) -> None:
        """Record a council evaluation."""
        self._add_event(TraceEventType.COUNCIL_END, {
            "approved": approved,
            "confidence": confidence,
            "issues_count": len(issues),
            "duration_ms": duration_ms,
        })

    def record_preflight(
        self,
        passed: bool,
        errors: int,
        warnings: int,
        duration_ms: int,
    ) -> None:
        """Record preflight check results."""
        self._add_event(TraceEventType.PREFLIGHT_END, {
            "passed": passed,
            "errors": errors,
            "warnings": warnings,
            "duration_ms": duration_ms,
        })

    def _add_event(self, event_type: TraceEventType, data: dict) -> None:
        """Add an event to the current trace."""
        if self._current_trace:
            self._current_trace.events.append(TraceEvent(
                event_type=event_type,
                timestamp=time.time(),
                data=data,
            ))

    def _persist_trace(self, trace: ExecutionTrace) -> None:
        """Persist trace to disk."""
        if not self._persist_path:
            return

        try:
            self._persist_path.mkdir(parents=True, exist_ok=True)
            trace_file = self._persist_path / f"{trace.task_id}.json"
            trace_file.write_text(trace.to_json())
        except Exception as e:
            logger.error(f"Failed to persist trace: {e}")

    def explain_failure(self, trace: ExecutionTrace) -> str:
        """Generate human-readable failure explanation.

        Args:
            trace: The execution trace to analyze

        Returns:
            Explanation of what went wrong
        """
        if trace.outcome == TaskOutcome.SUCCESS:
            return "Task completed successfully."

        parts = []

        # Basic info
        parts.append(f"## Task Failure Analysis")
        parts.append(f"**Task**: {trace.task_description[:100]}")
        parts.append(f"**Outcome**: {trace.outcome.value if trace.outcome else 'unknown'}")
        parts.append(f"**Duration**: {trace.duration_ms}ms")
        parts.append(f"**Turns**: {trace.turn_count}")
        parts.append(f"**Total Tokens**: {trace.total_tokens:,}")
        parts.append("")

        # Error message
        if trace.error_message:
            parts.append(f"### Error")
            parts.append(f"```")
            parts.append(trace.error_message)
            parts.append(f"```")
            parts.append("")

        # Find error events
        errors = [e for e in trace.events if e.event_type == TraceEventType.ERROR]
        if errors:
            parts.append(f"### Error Events ({len(errors)})")
            for error in errors[:5]:  # Limit to 5
                parts.append(f"- {error.data.get('error', 'unknown')}")
            parts.append("")

        # Tool failures
        failed_tools = []
        for turn in trace.turns:
            for tc in turn.tool_calls:
                if not tc.success:
                    failed_tools.append(tc)

        if failed_tools:
            parts.append(f"### Failed Tool Calls ({len(failed_tools)})")
            for tc in failed_tools[:5]:
                parts.append(f"- **{tc.tool_name}**: {tc.error or 'unknown error'}")
            parts.append("")

        # Token usage pattern
        if trace.turns:
            parts.append(f"### Token Usage Pattern")
            for turn in trace.turns[-5:]:  # Last 5 turns
                parts.append(
                    f"- Turn {turn.turn_number}: "
                    f"{turn.input_tokens:,} in, {turn.output_tokens:,} out, "
                    f"{len(turn.tool_calls)} tools"
                )
            parts.append("")

        # Recommendations
        parts.append(f"### Recommendations")

        if trace.outcome == TaskOutcome.TIMEOUT:
            parts.append("- Consider increasing timeout or simplifying task")

        if failed_tools:
            parts.append("- Investigate tool failures above")

        if trace.total_tokens > 200000:
            parts.append("- Token usage was high - consider breaking into smaller tasks")

        if trace.turn_count > 50:
            parts.append("- Many turns used - task may be too complex")

        return "\n".join(parts)

    def get_summary(self) -> dict[str, Any]:
        """Get summary statistics for all traces."""
        if not self._traces:
            return {"total": 0}

        total = len(self._traces)
        success = sum(1 for t in self._traces if t.outcome == TaskOutcome.SUCCESS)
        partial = sum(1 for t in self._traces if t.outcome == TaskOutcome.PARTIAL)
        failure = sum(1 for t in self._traces if t.outcome == TaskOutcome.FAILURE)

        total_tokens = sum(t.total_tokens for t in self._traces)
        total_turns = sum(t.turn_count for t in self._traces)
        total_duration = sum(t.duration_ms for t in self._traces)

        return {
            "total": total,
            "success": success,
            "partial": partial,
            "failure": failure,
            "success_rate": success / total,
            "total_tokens": total_tokens,
            "avg_tokens": total_tokens / total,
            "total_turns": total_turns,
            "avg_turns": total_turns / total,
            "total_duration_ms": total_duration,
            "avg_duration_ms": total_duration / total,
        }

    def get_recent_traces(self, count: int = 10) -> list[ExecutionTrace]:
        """Get most recent traces."""
        return self._traces[-count:]
