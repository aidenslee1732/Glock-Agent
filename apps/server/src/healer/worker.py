"""Healer worker - handles validation failures and produces retry plans."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from packages.shared_protocol.types import (
    MessageEnvelope,
    MessageType,
    ValidationResultPayload,
    ValidationStatus,
)
from apps.server.src.storage.redis import RedisClient
from apps.server.src.storage.postgres import PostgresClient
from apps.server.src.planner.compiler import PlanCompiler, CompilationContext

from .parser import FailureParser, ParsedFailure

logger = logging.getLogger(__name__)


@dataclass
class HealerConfig:
    """Configuration for the healer worker."""
    max_retries: int = 3
    job_timeout_ms: int = 300000
    concurrency: int = 5
    retry_delay_ms: int = 1000


@dataclass
class HealJob:
    """A healing job for a failed validation."""
    job_id: str
    task_id: str
    session_id: str
    user_id: str
    attempt_no: int
    validation_results: list[dict[str, Any]]
    current_plan_id: str
    created_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow()


@dataclass
class HealResult:
    """Result of a healing attempt."""
    should_retry: bool
    new_plan_id: Optional[str] = None
    terminal_reason: Optional[str] = None
    failure_summary: str = ""


class HealerWorker:
    """Worker that processes validation failures and produces retry plans.

    The healer:
    1. Receives validation failure results
    2. Parses and categorizes failures
    3. Determines if retry is appropriate
    4. Compiles a new plan focused on fixing failures
    5. Returns retry plan or terminal result
    """

    def __init__(
        self,
        redis: RedisClient,
        postgres: PostgresClient,
        plan_compiler: PlanCompiler,
        config: Optional[HealerConfig] = None,
    ):
        self.redis = redis
        self.postgres = postgres
        self.compiler = plan_compiler
        self.config = config or HealerConfig()
        self.parser = FailureParser()

        # Job queue
        self._queue: asyncio.Queue[HealJob] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        """Start the healer worker."""
        self._running = True

        # Start worker tasks
        for i in range(self.config.concurrency):
            task = asyncio.create_task(self._worker_loop(i))
            self._workers.append(task)

        logger.info(f"Healer worker started with {self.config.concurrency} workers")

    async def stop(self) -> None:
        """Stop the healer worker."""
        self._running = False

        # Cancel workers
        for worker in self._workers:
            worker.cancel()

        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        logger.info("Healer worker stopped")

    async def enqueue(
        self,
        task_id: str,
        session_id: str,
        user_id: str,
        attempt_no: int,
        validation_results: list[dict[str, Any]],
        current_plan_id: str,
    ) -> str:
        """Enqueue a healing job.

        Args:
            task_id: Task that failed validation
            session_id: Session ID
            user_id: User ID
            attempt_no: Current attempt number
            validation_results: List of validation results
            current_plan_id: Current plan ID

        Returns:
            Job ID
        """
        import secrets
        job_id = f"heal_{secrets.token_hex(8)}"

        job = HealJob(
            job_id=job_id,
            task_id=task_id,
            session_id=session_id,
            user_id=user_id,
            attempt_no=attempt_no,
            validation_results=validation_results,
            current_plan_id=current_plan_id,
        )

        await self._queue.put(job)
        logger.info(f"Healer job enqueued: {job_id} for task {task_id}")

        return job_id

    async def _worker_loop(self, worker_id: int) -> None:
        """Worker loop to process healing jobs."""
        while self._running:
            try:
                # Get next job
                job = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=5.0,
                )

                # Process job
                result = await self._process_job(job)

                # Send result
                await self._send_result(job, result)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Healer worker {worker_id} error: {e}")
                await asyncio.sleep(1)

    async def _process_job(self, job: HealJob) -> HealResult:
        """Process a healing job.

        Args:
            job: Job to process

        Returns:
            HealResult with retry decision
        """
        logger.info(f"Processing heal job: {job.job_id}")

        # Get task details
        task = await self.postgres.get_task(job.task_id)
        if not task:
            return HealResult(
                should_retry=False,
                terminal_reason="task_not_found",
            )

        # Check retry budget
        retry_count = task.get("retry_count", 0)
        max_retries = task.get("max_retries", self.config.max_retries)

        if retry_count >= max_retries:
            return HealResult(
                should_retry=False,
                terminal_reason="retry_budget_exhausted",
                failure_summary=self._summarize_validations(job.validation_results),
            )

        # Parse failures
        all_failures: list[ParsedFailure] = []
        for result in job.validation_results:
            if result.get("status") == "failed":
                raw_output = result.get("output_summary", "")
                tool = result.get("step_name", "pytest")
                failures = self.parser.parse(raw_output, tool)
                all_failures.extend(failures)

        if not all_failures:
            # No parseable failures - might be transient
            return HealResult(
                should_retry=True,
                terminal_reason=None,
                failure_summary="No specific failures identified, retrying",
            )

        # Check if failures are fixable
        if self._is_terminal_failure(all_failures):
            return HealResult(
                should_retry=False,
                terminal_reason="unfixable_failure",
                failure_summary=self.parser.summarize(all_failures),
            )

        # Compile retry plan
        try:
            context = CompilationContext(
                session_id=job.session_id,
                task_id=job.task_id,
                user_id=job.user_id,
                user_prompt=task.get("user_prompt", ""),
                workspace_scope=None,  # Will be filled from session
            )

            # Get current plan
            current_plan = await self.postgres.get_plan(job.current_plan_id)
            if current_plan:
                context.workspace_scope = current_plan.get("workspace_scope")

            # Compile retry plan with failure context
            new_plan = self.compiler.compile_retry(
                context,
                current_plan,
                [f.__dict__ for f in all_failures],
            )

            # Store plan
            await self.postgres.create_plan(
                plan_id=new_plan.plan_id,
                task_id=job.task_id,
                session_id=job.session_id,
                plan_payload=new_plan.to_dict(),
                plan_signature=new_plan.signature,
                expires_at=new_plan.expires_at,
                mode="retry",
                allowed_tools=new_plan.allowed_tools,
                risk_flags=new_plan.risk_flags,
            )

            # Update task retry count
            await self.postgres.increment_task_retry(job.task_id)

            return HealResult(
                should_retry=True,
                new_plan_id=new_plan.plan_id,
                failure_summary=self.parser.summarize(all_failures),
            )

        except Exception as e:
            logger.error(f"Failed to compile retry plan: {e}")
            return HealResult(
                should_retry=False,
                terminal_reason="plan_compilation_failed",
                failure_summary=str(e),
            )

    def _is_terminal_failure(self, failures: list[ParsedFailure]) -> bool:
        """Check if failures are terminal (can't be fixed by retry).

        Args:
            failures: List of parsed failures

        Returns:
            True if failures are terminal
        """
        from .parser import FailureType

        terminal_types = {
            FailureType.SYNTAX_ERROR,
            FailureType.IMPORT_ERROR,
        }

        # If most failures are terminal types, don't retry
        terminal_count = sum(
            1 for f in failures if f.failure_type in terminal_types
        )

        return terminal_count > len(failures) / 2

    def _summarize_validations(
        self,
        validation_results: list[dict[str, Any]],
    ) -> str:
        """Summarize validation results.

        Args:
            validation_results: List of validation results

        Returns:
            Summary string
        """
        lines = []
        for result in validation_results:
            step = result.get("step_name", "unknown")
            status = result.get("status", "unknown")
            lines.append(f"- {step}: {status}")

            if status == "failed":
                summary = result.get("output_summary", "")
                if summary:
                    lines.append(f"  {summary[:100]}...")

        return "\n".join(lines)

    async def _send_result(self, job: HealJob, result: HealResult) -> None:
        """Send healing result back to gateway/runtime.

        Args:
            job: The heal job
            result: Healing result
        """
        if result.should_retry:
            # Send retry_plan_ready
            msg = MessageEnvelope.create(
                msg_type=MessageType.RETRY_PLAN_READY,
                session_id=job.session_id,
                task_id=job.task_id,
                payload={
                    "task_id": job.task_id,
                    "attempt_no": job.attempt_no + 1,
                    "plan_id": result.new_plan_id,
                    "focus": result.failure_summary[:500],
                },
            )
        else:
            # Send retry_exhausted
            msg = MessageEnvelope.create(
                msg_type=MessageType.RETRY_EXHAUSTED,
                session_id=job.session_id,
                task_id=job.task_id,
                payload={
                    "task_id": job.task_id,
                    "attempts": job.attempt_no,
                    "final_status": "failed",
                    "blocker_summary": result.terminal_reason,
                    "suggestions": self._generate_suggestions(result),
                },
            )

        # Publish to Redis for gateway to relay
        await self.redis.publish(
            f"healer:result:{job.session_id}",
            msg.to_dict().__str__(),
        )

        logger.info(
            f"Heal result sent: job={job.job_id}, retry={result.should_retry}"
        )

    def _generate_suggestions(self, result: HealResult) -> list[str]:
        """Generate suggestions for user based on failure.

        Args:
            result: Heal result

        Returns:
            List of suggestions
        """
        suggestions = []

        if result.terminal_reason == "retry_budget_exhausted":
            suggestions.append("Review the error messages and fix manually")
            suggestions.append("Consider breaking the task into smaller steps")

        elif result.terminal_reason == "unfixable_failure":
            suggestions.append("Check for syntax errors in your code")
            suggestions.append("Verify all imports are correct")

        elif result.terminal_reason == "plan_compilation_failed":
            suggestions.append("Try simplifying the task description")
            suggestions.append("Ensure the workspace is in a valid state")

        return suggestions


async def handle_validation_result(
    healer: HealerWorker,
    session_id: str,
    user_id: str,
    result: ValidationResultPayload,
    current_plan_id: str,
) -> None:
    """Handle a validation result from the client.

    Args:
        healer: Healer worker instance
        session_id: Session ID
        user_id: User ID
        result: Validation result
        current_plan_id: Current plan ID
    """
    if result.status == ValidationStatus.FAILED:
        await healer.enqueue(
            task_id=result.task_id,
            session_id=session_id,
            user_id=user_id,
            attempt_no=result.attempt_no,
            validation_results=[result.to_dict()],
            current_plan_id=current_plan_id,
        )
