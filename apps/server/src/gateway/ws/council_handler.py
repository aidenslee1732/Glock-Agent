"""Council Handler - Handles council deliberation requests via WebSocket.

Provides WebSocket integration for council deliberation, allowing
clients to request multi-perspective code review.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Awaitable, Optional
from uuid import uuid4

from packages.shared_protocol.types import (
    MessageEnvelope,
    MessageType,
)
from apps.server.src.planner.council import (
    CouncilOrchestrator,
    CouncilConfig,
    CouncilStrategy,
    CouncilExecutor,
    CouncilExecutionRequest,
    create_standard_council,
    create_security_focused_council,
    create_thorough_council,
)
from apps.server.src.planner.llm.gateway import LLMGateway, LLMConfig

logger = logging.getLogger(__name__)


class CouncilHandler:
    """Handles council deliberation requests.

    Processes COUNCIL_REQUEST messages and returns deliberation results.
    """

    def __init__(
        self,
        llm_gateway: Optional[LLMGateway] = None,
    ):
        self.llm_gateway = llm_gateway or LLMGateway(LLMConfig())
        self.executor = CouncilExecutor(self.llm_gateway)
        self._active_deliberations: dict[str, asyncio.Task] = {}

    async def handle_council_request(
        self,
        session_id: str,
        user_id: str,
        payload: dict[str, Any],
        send_callback: Callable[[str], Awaitable[None]],
    ) -> None:
        """Handle a council deliberation request.

        Args:
            session_id: Session ID
            user_id: User ID
            payload: Request payload containing:
                - request_id: Unique request ID
                - task: Task description
                - code: Proposed code to review
                - context: Additional context
                - council_type: Type of council (standard, security, thorough)
                - perspectives: Optional list of specific perspectives
            send_callback: Callback to send messages
        """
        request_id = payload.get("request_id", str(uuid4()))
        task = payload.get("task", "")
        code = payload.get("code", "")
        context = payload.get("context", {})
        council_type = payload.get("council_type", "standard")
        perspectives = payload.get("perspectives")

        logger.info(
            f"Council request: session={session_id}, type={council_type}, "
            f"code_length={len(code)}"
        )

        try:
            # Send acknowledgment
            await self._send_ack(send_callback, session_id, request_id)

            # Create execution request
            execution_request = CouncilExecutionRequest(
                task_id=request_id,
                session_id=session_id,
                user_id=user_id,
                task_description=task,
                proposed_code=code,
                context=context,
                council_perspectives=perspectives,
            )

            # Execute council deliberation
            result = await self.executor.execute(execution_request)

            # Send result
            await self._send_result(
                send_callback=send_callback,
                session_id=session_id,
                request_id=request_id,
                result=result,
            )

        except Exception as e:
            logger.exception(f"Council deliberation failed: {e}")
            await self._send_error(
                send_callback=send_callback,
                session_id=session_id,
                request_id=request_id,
                error=str(e),
            )

    async def _send_ack(
        self,
        send_callback: Callable[[str], Awaitable[None]],
        session_id: str,
        request_id: str,
    ) -> None:
        """Send acknowledgment message."""
        envelope = MessageEnvelope(
            type=MessageType.COUNCIL_ACK,
            session_id=session_id,
            payload={
                "request_id": request_id,
                "status": "deliberating",
            },
        )
        await send_callback(envelope.model_dump_json())

    async def _send_result(
        self,
        send_callback: Callable[[str], Awaitable[None]],
        session_id: str,
        request_id: str,
        result,
    ) -> None:
        """Send council result."""
        envelope = MessageEnvelope(
            type=MessageType.COUNCIL_RESULT,
            session_id=session_id,
            payload={
                "request_id": request_id,
                "approved": result.approved,
                "confidence": result.council_result.consensus.confidence,
                "vote_summary": {
                    k.value: v
                    for k, v in result.council_result.consensus.vote_summary.items()
                },
                "quality_score": (
                    result.council_result.quality_score.overall
                    if result.council_result.quality_score
                    else None
                ),
                "blocking_reasons": result.blocking_reasons,
                "recommendations": result.recommendations,
                "execution_time_ms": result.council_result.execution_time_ms,
                "perspectives_completed": result.council_result.perspectives_completed,
            },
        )
        await send_callback(envelope.model_dump_json())

    async def _send_error(
        self,
        send_callback: Callable[[str], Awaitable[None]],
        session_id: str,
        request_id: str,
        error: str,
    ) -> None:
        """Send error message."""
        envelope = MessageEnvelope(
            type=MessageType.COUNCIL_ERROR,
            session_id=session_id,
            payload={
                "request_id": request_id,
                "error": error,
            },
        )
        await send_callback(envelope.model_dump_json())


class InlineCouncilChecker:
    """Inline council checker for use within LLM request flow.

    Can be called to check proposed code during regular LLM interactions,
    without requiring a separate council request.
    """

    def __init__(
        self,
        llm_gateway: LLMGateway,
        auto_check: bool = False,
        min_code_length: int = 100,
    ):
        """Initialize inline checker.

        Args:
            llm_gateway: LLM gateway for council calls
            auto_check: Auto-check all code (vs explicit requests)
            min_code_length: Minimum code length to trigger check
        """
        self.executor = CouncilExecutor(llm_gateway)
        self.auto_check = auto_check
        self.min_code_length = min_code_length

    async def check_code(
        self,
        session_id: str,
        user_id: str,
        task: str,
        code: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Check code with quick council.

        Returns dict with:
            - approved: bool
            - confidence: float
            - issues: list of issues
            - recommendations: list of recommendations
        """
        if len(code) < self.min_code_length:
            return {
                "approved": True,
                "confidence": 1.0,
                "issues": [],
                "recommendations": [],
                "skipped": True,
                "reason": "Code too short for council check",
            }

        request = CouncilExecutionRequest(
            task_id=str(uuid4()),
            session_id=session_id,
            user_id=user_id,
            task_description=task,
            proposed_code=code,
            context=context,
            council_perspectives=["correctness", "security"],  # Quick check
        )

        result = await self.executor.execute(request)

        return {
            "approved": result.approved,
            "confidence": result.council_result.consensus.confidence,
            "issues": [
                {
                    "severity": i.severity.value,
                    "message": i.message,
                }
                for i in result.council_result.consensus.all_issues[:5]  # Top 5
            ],
            "recommendations": result.recommendations[:3],  # Top 3
            "skipped": False,
        }

    def should_check(self, code: str, task: str) -> bool:
        """Determine if code should be checked."""
        if not self.auto_check:
            return False

        # Check length
        if len(code) < self.min_code_length:
            return False

        # Check for keywords suggesting risky code
        risky_keywords = [
            "auth", "password", "secret", "token", "key",
            "sql", "exec", "eval", "shell", "sudo",
            "delete", "drop", "truncate", "destroy",
        ]

        code_lower = code.lower()
        task_lower = task.lower()

        return any(
            kw in code_lower or kw in task_lower
            for kw in risky_keywords
        )
