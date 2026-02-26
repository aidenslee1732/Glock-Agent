"""
Central task orchestration for Glock server.

Coordinates task execution flow between:
- Gateway (client communication)
- Planner (plan compilation)
- Runtime (execution)
- Healer (validation/retry)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, Any, Callable, Awaitable
from uuid import uuid4

from ..storage.redis import RedisClient
from ..storage.repositories.tasks import TaskRepository
from ..storage.repositories.sessions import SessionRepository
from .compiler import PlanCompiler, CompiledPlan
from .router import TaskRouter, TaskAnalysis
from .memory import MemoryManager


logger = logging.getLogger(__name__)


class TaskState(Enum):
    """Task execution states."""
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_TOOL_RESULT = "waiting_tool_result"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_VALIDATION = "waiting_validation"
    VALIDATING = "validating"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskContext:
    """Context for task orchestration."""
    task_id: str
    session_id: str
    user_id: str
    org_id: Optional[str]

    # Task details
    prompt: str
    workspace_context: Dict[str, Any]

    # Execution state
    state: TaskState = TaskState.QUEUED
    plan: Optional[CompiledPlan] = None
    analysis: Optional[TaskAnalysis] = None

    # Attempt tracking
    attempt_no: int = 1
    max_retries: int = 2

    # Timing
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Results
    result_summary: Optional[str] = None
    files_modified: List[str] = field(default_factory=list)
    error_reason: Optional[str] = None


@dataclass
class OrchestratorConfig:
    """Configuration for the orchestrator."""
    # Timeouts
    planning_timeout_ms: int = 30000
    execution_timeout_ms: int = 3600000  # 1 hour
    validation_timeout_ms: int = 300000  # 5 minutes

    # Limits
    max_concurrent_tasks_per_session: int = 1
    max_retries: int = 2

    # Behavior
    auto_validate: bool = True
    require_validation_pass: bool = True


class TaskOrchestrator:
    """
    Central orchestrator for task execution.

    Coordinates the full lifecycle of a task:
    1. Receive task from gateway
    2. Analyze and compile plan
    3. Route to runtime for execution
    4. Handle tool results and approvals
    5. Validate and retry if needed
    6. Report completion/failure
    """

    def __init__(
        self,
        redis: RedisClient,
        task_repo: TaskRepository,
        session_repo: SessionRepository,
        plan_compiler: PlanCompiler,
        task_router: TaskRouter,
        memory_manager: MemoryManager,
        config: Optional[OrchestratorConfig] = None
    ):
        self.redis = redis
        self.task_repo = task_repo
        self.session_repo = session_repo
        self.plan_compiler = plan_compiler
        self.task_router = task_router
        self.memory = memory_manager
        self.config = config or OrchestratorConfig()

        # Active task contexts
        self.active_tasks: Dict[str, TaskContext] = {}

        # Event handlers
        self._handlers: Dict[str, List[Callable]] = {}

    def on(self, event: str, handler: Callable) -> None:
        """Register event handler."""
        if event not in self._handlers:
            self._handlers[event] = []
        self._handlers[event].append(handler)

    async def _emit(self, event: str, *args, **kwargs) -> None:
        """Emit event to handlers."""
        if event in self._handlers:
            for handler in self._handlers[event]:
                try:
                    result = handler(*args, **kwargs)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error(f"Event handler error: {e}")

    async def submit_task(
        self,
        session_id: str,
        user_id: str,
        prompt: str,
        workspace_context: Dict[str, Any],
        org_id: Optional[str] = None
    ) -> TaskContext:
        """Submit a new task for execution."""
        task_id = f"task_{uuid4().hex[:24]}"

        # Check session concurrency
        session = await self.session_repo.get(session_id)
        if session and session.active_task_id:
            raise ValueError("Session already has an active task")

        # Create task context
        context = TaskContext(
            task_id=task_id,
            session_id=session_id,
            user_id=user_id,
            org_id=org_id,
            prompt=prompt,
            workspace_context=workspace_context
        )

        # Store in database
        await self.task_repo.create(
            task_id=task_id,
            session_id=session_id,
            user_id=user_id,
            org_id=org_id,
            prompt=prompt,
            status="queued"
        )

        # Update session
        await self.session_repo.update(
            session_id,
            active_task_id=task_id,
            status="running"
        )

        # Store in Redis for quick access
        await self.redis.hset(f"task:{task_id}:state", {
            "status": "queued",
            "session_id": session_id,
            "user_id": user_id,
            "created_at": context.created_at.isoformat()
        })

        self.active_tasks[task_id] = context

        # Start orchestration
        asyncio.create_task(self._orchestrate_task(context))

        await self._emit('task_submitted', context)

        return context

    async def _orchestrate_task(self, context: TaskContext) -> None:
        """Main orchestration loop for a task."""
        try:
            # Phase 1: Analysis and Planning
            context.state = TaskState.PLANNING
            await self._update_task_state(context)

            analysis = await self._analyze_task(context)
            context.analysis = analysis

            plan = await self._compile_plan(context, analysis)
            context.plan = plan

            # Phase 2: Execution
            context.state = TaskState.RUNNING
            context.started_at = datetime.utcnow()
            await self._update_task_state(context)

            await self._emit('task_started', context, plan)

            # Execution is driven by runtime - we wait for completion
            # The runtime sends tool requests through the relay

        except asyncio.CancelledError:
            context.state = TaskState.CANCELLED
            await self._handle_cancellation(context)

        except Exception as e:
            logger.exception(f"Task orchestration failed: {context.task_id}")
            context.state = TaskState.FAILED
            context.error_reason = str(e)
            await self._handle_failure(context)

    async def _analyze_task(self, context: TaskContext) -> TaskAnalysis:
        """Analyze task to determine strategy."""
        # Get user preferences
        preferences = await self.memory.get_user_preferences(context.user_id)

        # Analyze task
        analysis = await self.task_router.analyze(
            prompt=context.prompt,
            workspace_context=context.workspace_context,
            user_preferences=preferences
        )

        logger.info(
            f"Task analysis for {context.task_id}: "
            f"type={analysis.task_type}, risk={analysis.risk_level}"
        )

        return analysis

    async def _compile_plan(
        self,
        context: TaskContext,
        analysis: TaskAnalysis
    ) -> CompiledPlan:
        """Compile execution plan for task."""
        plan = await asyncio.wait_for(
            self.plan_compiler.compile(
                task_id=context.task_id,
                session_id=context.session_id,
                user_id=context.user_id,
                prompt=context.prompt,
                workspace_context=context.workspace_context,
                analysis=analysis
            ),
            timeout=self.config.planning_timeout_ms / 1000
        )

        logger.info(f"Compiled plan for {context.task_id}: {plan.plan_id}")

        return plan

    async def handle_tool_result(
        self,
        task_id: str,
        tool_id: str,
        status: str,
        result: Any,
        duration_ms: int
    ) -> None:
        """Handle tool result from client."""
        context = self.active_tasks.get(task_id)
        if not context:
            logger.warning(f"Tool result for unknown task: {task_id}")
            return

        if context.state != TaskState.WAITING_TOOL_RESULT:
            logger.warning(
                f"Tool result in unexpected state: {context.state}"
            )

        context.state = TaskState.RUNNING
        await self._update_task_state(context)

        await self._emit('tool_result_received', context, tool_id, status, result)

    async def handle_approval_response(
        self,
        task_id: str,
        approval_id: str,
        approved: bool
    ) -> None:
        """Handle approval response from client."""
        context = self.active_tasks.get(task_id)
        if not context:
            return

        if approved:
            context.state = TaskState.RUNNING
        else:
            context.state = TaskState.FAILED
            context.error_reason = "User rejected approval request"

        await self._update_task_state(context)

        await self._emit('approval_received', context, approval_id, approved)

    async def handle_validation_result(
        self,
        task_id: str,
        step_name: str,
        status: str,
        failures: List[Dict[str, Any]]
    ) -> None:
        """Handle validation result from client."""
        context = self.active_tasks.get(task_id)
        if not context:
            return

        if status == "passed":
            # Check if all validations passed
            await self._emit('validation_passed', context, step_name)
        else:
            # Check if we should retry
            if context.attempt_no < context.max_retries:
                await self._handle_retry(context, failures)
            else:
                context.state = TaskState.FAILED
                context.error_reason = f"Validation failed: {step_name}"
                await self._handle_failure(context)

    async def handle_task_complete(
        self,
        task_id: str,
        summary: str,
        files_modified: List[str]
    ) -> None:
        """Handle task completion from runtime."""
        context = self.active_tasks.get(task_id)
        if not context:
            return

        context.result_summary = summary
        context.files_modified = files_modified

        # Run validation if configured
        if self.config.auto_validate and context.plan:
            context.state = TaskState.VALIDATING
            await self._update_task_state(context)

            await self._emit(
                'validation_requested',
                context,
                context.plan.validation_steps
            )
        else:
            await self._complete_task(context)

    async def _complete_task(self, context: TaskContext) -> None:
        """Mark task as completed."""
        context.state = TaskState.COMPLETED
        context.completed_at = datetime.utcnow()

        # Update database
        await self.task_repo.update(
            context.task_id,
            status="completed",
            completed_at=context.completed_at,
            summary=context.result_summary
        )

        # Update session
        await self.session_repo.update(
            context.session_id,
            active_task_id=None,
            status="idle"
        )

        # Clean up Redis
        await self.redis.delete(f"task:{context.task_id}:state")

        # Record in memory for learning
        await self.memory.record_task_completion(
            user_id=context.user_id,
            task_id=context.task_id,
            task_type=context.analysis.task_type if context.analysis else "unknown",
            success=True
        )

        # Remove from active tasks
        del self.active_tasks[context.task_id]

        await self._emit('task_completed', context)

        logger.info(f"Task completed: {context.task_id}")

    async def _handle_retry(
        self,
        context: TaskContext,
        failures: List[Dict[str, Any]]
    ) -> None:
        """Handle task retry after validation failure."""
        context.state = TaskState.RETRYING
        context.attempt_no += 1

        await self._update_task_state(context)

        logger.info(
            f"Retrying task {context.task_id} "
            f"(attempt {context.attempt_no}/{context.max_retries})"
        )

        # Compile new plan with failure context
        new_plan = await self.plan_compiler.compile_retry(
            task_id=context.task_id,
            session_id=context.session_id,
            user_id=context.user_id,
            original_plan=context.plan,
            failures=failures,
            attempt_no=context.attempt_no
        )

        context.plan = new_plan
        context.state = TaskState.RUNNING

        await self._update_task_state(context)

        await self._emit('retry_started', context, new_plan)

    async def _handle_failure(self, context: TaskContext) -> None:
        """Handle task failure."""
        context.state = TaskState.FAILED
        context.completed_at = datetime.utcnow()

        # Update database
        await self.task_repo.update(
            context.task_id,
            status="failed",
            completed_at=context.completed_at,
            failure_reason=context.error_reason
        )

        # Update session
        await self.session_repo.update(
            context.session_id,
            active_task_id=None,
            status="idle"
        )

        # Clean up Redis
        await self.redis.delete(f"task:{context.task_id}:state")

        # Record in memory
        await self.memory.record_task_completion(
            user_id=context.user_id,
            task_id=context.task_id,
            task_type=context.analysis.task_type if context.analysis else "unknown",
            success=False
        )

        # Remove from active tasks
        del self.active_tasks[context.task_id]

        await self._emit('task_failed', context)

        logger.error(f"Task failed: {context.task_id} - {context.error_reason}")

    async def _handle_cancellation(self, context: TaskContext) -> None:
        """Handle task cancellation."""
        context.state = TaskState.CANCELLED
        context.completed_at = datetime.utcnow()

        # Update database
        await self.task_repo.update(
            context.task_id,
            status="cancelled",
            completed_at=context.completed_at
        )

        # Update session
        await self.session_repo.update(
            context.session_id,
            active_task_id=None,
            status="idle"
        )

        # Clean up Redis
        await self.redis.delete(f"task:{context.task_id}:state")

        # Remove from active tasks
        if context.task_id in self.active_tasks:
            del self.active_tasks[context.task_id]

        await self._emit('task_cancelled', context)

        logger.info(f"Task cancelled: {context.task_id}")

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task."""
        context = self.active_tasks.get(task_id)
        if not context:
            return False

        if context.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            return False

        await self._handle_cancellation(context)
        return True

    async def _update_task_state(self, context: TaskContext) -> None:
        """Update task state in Redis."""
        await self.redis.hset(f"task:{context.task_id}:state", {
            "status": context.state.value,
            "attempt_no": str(context.attempt_no),
            "updated_at": datetime.utcnow().isoformat()
        })

    async def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get current task status."""
        # Check active tasks first
        if task_id in self.active_tasks:
            context = self.active_tasks[task_id]
            return {
                "task_id": task_id,
                "state": context.state.value,
                "attempt_no": context.attempt_no,
                "started_at": context.started_at.isoformat() if context.started_at else None,
                "plan_id": context.plan.plan_id if context.plan else None
            }

        # Check Redis
        state = await self.redis.hgetall(f"task:{task_id}:state")
        if state:
            return {
                "task_id": task_id,
                "state": state.get("status"),
                "attempt_no": int(state.get("attempt_no", 1)),
                "updated_at": state.get("updated_at")
            }

        # Check database
        task = await self.task_repo.get(task_id)
        if task:
            return {
                "task_id": task_id,
                "state": task.status,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None
            }

        return None

    async def handle_client_disconnect(self, session_id: str) -> None:
        """Handle client disconnection."""
        # Find active task for session
        for task_id, context in list(self.active_tasks.items()):
            if context.session_id == session_id:
                if context.state == TaskState.WAITING_TOOL_RESULT:
                    # Pause the task
                    context.state = TaskState.WAITING_APPROVAL
                    await self._update_task_state(context)

                    await self._emit('task_paused', context, 'client_disconnected')

                    logger.info(
                        f"Task {task_id} paused due to client disconnect"
                    )

    async def handle_client_reconnect(self, session_id: str) -> None:
        """Handle client reconnection."""
        # Find paused task for session
        for task_id, context in self.active_tasks.items():
            if context.session_id == session_id:
                # Resume if paused
                if context.state == TaskState.WAITING_APPROVAL:
                    context.state = TaskState.RUNNING
                    await self._update_task_state(context)

                    await self._emit('task_resumed', context)

                    logger.info(f"Task {task_id} resumed after reconnect")
