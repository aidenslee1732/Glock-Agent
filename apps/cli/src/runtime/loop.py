"""
Agentic execution loop for Glock CLI.

This module handles the client-side execution of tool requests
received from the server-side runtime. It processes tool calls,
manages approvals, and returns results.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, Any, Callable, Awaitable
from pathlib import Path

from ..tools.broker import ToolBroker, ToolResult
from ..plan.enforcer import PlanEnforcer
from ..plan.verifier import CompiledPlan
from ..transport.ws_client import GlockWebSocketClient
from ..session.state import SessionStateStore, TaskCheckpoint


logger = logging.getLogger(__name__)


class LoopState(Enum):
    """States for the agentic loop."""
    IDLE = "idle"
    WAITING_TASK = "waiting_task"
    EXECUTING = "executing"
    WAITING_APPROVAL = "waiting_approval"
    VALIDATING = "validating"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ToolRequest:
    """Tool execution request from server."""
    tool_id: str
    tool_name: str
    args: Dict[str, Any]
    requires_approval: bool = False
    risk_level: str = "low"
    approval_reason: Optional[str] = None


@dataclass
class ApprovalRequest:
    """Approval request for dangerous operations."""
    approval_id: str
    tool_name: str
    args: Dict[str, Any]
    risk_level: str
    reason: str
    diff_preview: Optional[str] = None


@dataclass
class LoopContext:
    """Context for a single loop execution."""
    session_id: str
    task_id: str
    plan: CompiledPlan
    workspace_path: Path

    # State tracking
    state: LoopState = LoopState.IDLE
    current_tool_id: Optional[str] = None
    pending_approval: Optional[ApprovalRequest] = None

    # Execution stats
    tool_calls_made: int = 0
    start_time: Optional[datetime] = None
    last_activity: Optional[datetime] = None

    # Error tracking
    consecutive_errors: int = 0
    max_consecutive_errors: int = 5


@dataclass
class LoopConfig:
    """Configuration for the agentic loop."""
    # Timeouts
    tool_timeout_ms: int = 120000  # 2 minutes
    approval_timeout_ms: int = 300000  # 5 minutes

    # Limits
    max_tool_calls_per_task: int = 200
    max_consecutive_errors: int = 5

    # Behavior
    auto_approve_safe_tools: bool = True
    checkpoint_interval: int = 10  # Checkpoint every N tool calls


class AgenticLoop:
    """
    Client-side agentic execution loop.

    Handles tool requests from the server-side runtime,
    manages user approvals, and returns results.
    """

    def __init__(
        self,
        ws_client: GlockWebSocketClient,
        tool_broker: ToolBroker,
        plan_enforcer: PlanEnforcer,
        state_store: SessionStateStore,
        config: Optional[LoopConfig] = None,
        approval_handler: Optional[Callable[[ApprovalRequest], Awaitable[bool]]] = None,
        diff_handler: Optional[Callable[[str, Dict[str, Any]], Awaitable[bool]]] = None
    ):
        self.ws_client = ws_client
        self.tool_broker = tool_broker
        self.plan_enforcer = plan_enforcer
        self.state_store = state_store
        self.config = config or LoopConfig()
        self.approval_handler = approval_handler
        self.diff_handler = diff_handler

        # Current context
        self.context: Optional[LoopContext] = None

        # Event handlers
        self._handlers: Dict[str, Callable] = {}

        # Control flags
        self._running = False
        self._paused = False

    def on(self, event: str, handler: Callable) -> None:
        """Register event handler."""
        self._handlers[event] = handler

    async def _emit(self, event: str, *args, **kwargs) -> None:
        """Emit event to registered handler."""
        if event in self._handlers:
            result = self._handlers[event](*args, **kwargs)
            if asyncio.iscoroutine(result):
                await result

    async def start_task(
        self,
        session_id: str,
        task_id: str,
        plan: CompiledPlan,
        workspace_path: Path
    ) -> None:
        """Start executing a new task."""
        self.context = LoopContext(
            session_id=session_id,
            task_id=task_id,
            plan=plan,
            workspace_path=workspace_path,
            state=LoopState.WAITING_TASK,
            start_time=datetime.utcnow()
        )

        # Set up plan enforcer with this plan
        self.plan_enforcer.set_plan(plan)

        self._running = True
        await self._emit('task_started', task_id, plan)

        logger.info(f"Started task loop for {task_id}")

    async def handle_tool_request(self, request: ToolRequest) -> None:
        """Handle incoming tool request from server."""
        if not self.context:
            logger.error("Received tool request without active context")
            await self._send_tool_error(
                request.tool_id,
                "no_active_task",
                "No active task context"
            )
            return

        self.context.state = LoopState.EXECUTING
        self.context.current_tool_id = request.tool_id
        self.context.last_activity = datetime.utcnow()

        logger.info(f"Handling tool request: {request.tool_name} ({request.tool_id})")

        try:
            # 1. Verify against plan constraints
            enforcement = self.plan_enforcer.check_tool_request(
                request.tool_name,
                request.args
            )

            if not enforcement.allowed:
                await self._send_tool_error(
                    request.tool_id,
                    "plan_violation",
                    enforcement.reason or "Tool request violates plan constraints"
                )
                return

            # 2. Check if approval is required
            needs_approval = (
                request.requires_approval or
                enforcement.requires_approval or
                not self._is_auto_approvable(request)
            )

            if needs_approval:
                approved = await self._request_approval(request, enforcement.reason)
                if not approved:
                    await self._send_tool_error(
                        request.tool_id,
                        "user_rejected",
                        "User rejected the tool execution"
                    )
                    return

            # 3. Execute the tool
            result = await self._execute_tool(request)

            # 4. Send result back to server
            await self._send_tool_result(request.tool_id, result)

            # 5. Update stats and checkpoint if needed
            self.context.tool_calls_made += 1
            self.context.consecutive_errors = 0

            if self.context.tool_calls_made % self.config.checkpoint_interval == 0:
                await self._save_checkpoint()

            await self._emit('tool_completed', request.tool_id, result)

        except asyncio.TimeoutError:
            logger.error(f"Tool execution timed out: {request.tool_name}")
            await self._send_tool_error(
                request.tool_id,
                "timeout",
                f"Tool execution timed out after {self.config.tool_timeout_ms}ms"
            )
            self.context.consecutive_errors += 1

        except Exception as e:
            logger.exception(f"Tool execution failed: {request.tool_name}")
            await self._send_tool_error(
                request.tool_id,
                "execution_error",
                str(e)
            )
            self.context.consecutive_errors += 1

        finally:
            self.context.state = LoopState.WAITING_TASK
            self.context.current_tool_id = None

            # Check if we've exceeded error threshold
            if self.context.consecutive_errors >= self.config.max_consecutive_errors:
                await self._handle_error_threshold_exceeded()

    def _is_auto_approvable(self, request: ToolRequest) -> bool:
        """Check if tool can be auto-approved."""
        if not self.config.auto_approve_safe_tools:
            return False

        # Safe read-only tools
        safe_tools = {'read_file', 'glob', 'grep', 'list_dir', 'git_status'}

        if request.tool_name in safe_tools:
            return True

        # Check risk level
        if request.risk_level == "low":
            return True

        return False

    async def _request_approval(
        self,
        request: ToolRequest,
        reason: Optional[str] = None
    ) -> bool:
        """Request user approval for tool execution."""
        self.context.state = LoopState.WAITING_APPROVAL

        approval_request = ApprovalRequest(
            approval_id=f"apr_{request.tool_id}",
            tool_name=request.tool_name,
            args=request.args,
            risk_level=request.risk_level,
            reason=reason or request.approval_reason or "Requires user approval"
        )

        # Generate diff preview for edit operations
        if request.tool_name == "edit_file" and self.diff_handler:
            try:
                approval_request.diff_preview = await self._generate_diff_preview(request)
            except Exception as e:
                logger.warning(f"Failed to generate diff preview: {e}")

        self.context.pending_approval = approval_request

        await self._emit('approval_requested', approval_request)

        # Use custom approval handler or default
        if self.approval_handler:
            try:
                approved = await asyncio.wait_for(
                    self.approval_handler(approval_request),
                    timeout=self.config.approval_timeout_ms / 1000
                )
            except asyncio.TimeoutError:
                logger.warning("Approval request timed out")
                approved = False
        else:
            # Default: auto-reject if no handler
            logger.warning("No approval handler, auto-rejecting")
            approved = False

        self.context.pending_approval = None

        # Notify server of approval decision
        await self.ws_client.send_message({
            'type': 'tool_approval_response',
            'approval_id': approval_request.approval_id,
            'approved': approved
        })

        return approved

    async def _generate_diff_preview(self, request: ToolRequest) -> str:
        """Generate diff preview for edit operations."""
        if request.tool_name != "edit_file":
            return ""

        file_path = request.args.get('path', '')
        old_string = request.args.get('old_string', '')
        new_string = request.args.get('new_string', '')

        # Generate unified diff
        import difflib

        old_lines = old_string.splitlines(keepends=True)
        new_lines = new_string.splitlines(keepends=True)

        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}"
        )

        return ''.join(diff)

    async def _execute_tool(self, request: ToolRequest) -> ToolResult:
        """Execute tool via tool broker."""
        return await asyncio.wait_for(
            self.tool_broker.execute(
                request.tool_name,
                request.args,
                workspace=self.context.workspace_path
            ),
            timeout=self.config.tool_timeout_ms / 1000
        )

    async def _send_tool_result(self, tool_id: str, result: ToolResult) -> None:
        """Send tool result back to server."""
        await self.ws_client.send_message({
            'type': 'tool_result',
            'tool_id': tool_id,
            'status': 'success' if result.success else 'error',
            'result': result.output,
            'duration_ms': result.duration_ms,
            'output_truncated': result.truncated
        })

    async def _send_tool_error(
        self,
        tool_id: str,
        error_code: str,
        error_message: str
    ) -> None:
        """Send tool error back to server."""
        await self.ws_client.send_message({
            'type': 'tool_error',
            'tool_id': tool_id,
            'error_code': error_code,
            'error_message': error_message
        })

    async def _save_checkpoint(self) -> None:
        """Save execution checkpoint for recovery."""
        if not self.context:
            return

        checkpoint = TaskCheckpoint(
            task_id=self.context.task_id,
            session_id=self.context.session_id,
            checkpoint_type='tool_queue',
            attempt_no=1,  # TODO: Track attempt number
            payload={
                'tool_calls_made': self.context.tool_calls_made,
                'state': self.context.state.value,
                'last_tool_id': self.context.current_tool_id
            },
            created_at=datetime.utcnow().isoformat()
        )

        self.state_store.save_checkpoint(checkpoint)
        logger.debug(f"Saved checkpoint at tool call {self.context.tool_calls_made}")

    async def _handle_error_threshold_exceeded(self) -> None:
        """Handle when error threshold is exceeded."""
        logger.error("Consecutive error threshold exceeded, pausing loop")

        self.context.state = LoopState.PAUSED
        self._paused = True

        await self.ws_client.send_message({
            'type': 'task_status',
            'task_id': self.context.task_id,
            'status': 'paused',
            'reason': 'consecutive_errors_exceeded'
        })

        await self._emit('loop_paused', 'consecutive_errors_exceeded')

    async def handle_task_complete(self, summary: str, files_modified: List[str]) -> None:
        """Handle task completion from server."""
        if not self.context:
            return

        self.context.state = LoopState.COMPLETED
        self._running = False

        logger.info(f"Task completed: {self.context.task_id}")

        await self._emit(
            'task_completed',
            self.context.task_id,
            summary,
            files_modified
        )

        # Clean up old checkpoints
        self.state_store.cleanup_old_checkpoints(self.context.task_id)

        self.context = None

    async def handle_task_failed(self, reason: str, suggestions: List[str]) -> None:
        """Handle task failure from server."""
        if not self.context:
            return

        self.context.state = LoopState.FAILED
        self._running = False

        logger.error(f"Task failed: {self.context.task_id} - {reason}")

        await self._emit(
            'task_failed',
            self.context.task_id,
            reason,
            suggestions
        )

        self.context = None

    async def pause(self) -> None:
        """Pause the execution loop."""
        if self.context:
            self.context.state = LoopState.PAUSED
        self._paused = True

        await self.ws_client.send_message({
            'type': 'task_status',
            'task_id': self.context.task_id if self.context else None,
            'status': 'paused',
            'reason': 'user_requested'
        })

        await self._emit('loop_paused', 'user_requested')

    async def resume(self) -> None:
        """Resume the execution loop."""
        if self.context:
            self.context.state = LoopState.WAITING_TASK
        self._paused = False

        await self.ws_client.send_message({
            'type': 'task_status',
            'task_id': self.context.task_id if self.context else None,
            'status': 'resumed'
        })

        await self._emit('loop_resumed')

    async def cancel(self) -> None:
        """Cancel the current task."""
        if not self.context:
            return

        task_id = self.context.task_id

        await self.ws_client.send_message({
            'type': 'cancel_requested',
            'task_id': task_id
        })

        self.context.state = LoopState.FAILED
        self._running = False

        await self._emit('task_cancelled', task_id)

        self.context = None

    @property
    def is_running(self) -> bool:
        """Check if loop is actively running."""
        return self._running and not self._paused

    @property
    def current_state(self) -> Optional[LoopState]:
        """Get current loop state."""
        return self.context.state if self.context else None
