"""Council Executor - Executes council deliberation within task orchestration.

Bridges the council system with the main task orchestrator, handling
the integration points between council deliberation and task execution.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Coroutine, Optional

from .orchestrator import (
    CouncilOrchestrator,
    CouncilResult,
    CouncilConfig,
    CouncilStrategy,
    CouncilBuilder,
    create_standard_council,
    create_security_focused_council,
    create_fast_council,
    create_thorough_council,
)
from .perspectives import PerspectiveType
from ..analyzer import TaskAnalysis, TaskType, Complexity, RiskLevel

logger = logging.getLogger(__name__)


@dataclass
class CouncilExecutionRequest:
    """Request for council execution."""
    task_id: str
    session_id: str
    user_id: str
    task_description: str
    proposed_code: str
    context: dict[str, Any]
    analysis: Optional[TaskAnalysis] = None
    council_perspectives: Optional[list[str]] = None


@dataclass
class CouncilExecutionResult:
    """Result from council execution."""
    approved: bool
    council_result: CouncilResult
    should_proceed: bool
    modifications_suggested: bool
    suggested_code: Optional[str] = None
    blocking_reasons: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


def _compute_cache_key(
    task: str,
    proposed_code: str,
    context: dict[str, Any],
    perspectives: Optional[list[str]] = None,
) -> str:
    """Compute a cache key for council results.

    Args:
        task: Task description
        proposed_code: Code to evaluate
        context: Evaluation context
        perspectives: List of perspective names

    Returns:
        SHA256 hash as hex string
    """
    # Create a stable string representation
    key_parts = [
        task,
        proposed_code,
        str(sorted(context.items())),
        str(sorted(perspectives or [])),
    ]
    key_string = "|".join(key_parts)
    return hashlib.sha256(key_string.encode()).hexdigest()


class CouncilResultCache:
    """LRU cache for council results.

    Caches results by hash of (task, code, context, perspectives)
    to avoid re-executing identical council deliberations.
    """

    def __init__(self, maxsize: int = 128):
        """Initialize cache.

        Args:
            maxsize: Maximum number of cached results
        """
        self._cache: dict[str, CouncilResult] = {}
        self._order: list[str] = []
        self._maxsize = maxsize

    def get(self, key: str) -> Optional[CouncilResult]:
        """Get cached result if available."""
        if key in self._cache:
            # Move to end (most recently used)
            self._order.remove(key)
            self._order.append(key)
            logger.debug(f"Council cache hit: {key[:16]}...")
            return self._cache[key]
        return None

    def set(self, key: str, result: CouncilResult) -> None:
        """Cache a result."""
        if key in self._cache:
            self._order.remove(key)
        elif len(self._cache) >= self._maxsize:
            # Evict oldest
            oldest = self._order.pop(0)
            del self._cache[oldest]
            logger.debug(f"Council cache evicted: {oldest[:16]}...")

        self._cache[key] = result
        self._order.append(key)
        logger.debug(f"Council cache set: {key[:16]}...")

    def clear(self) -> None:
        """Clear the cache."""
        self._cache.clear()
        self._order.clear()

    @property
    def size(self) -> int:
        """Current cache size."""
        return len(self._cache)


class CouncilExecutor:
    """Executes council deliberation as part of task orchestration.

    This executor:
    1. Receives proposed code from the main execution loop
    2. Determines appropriate council configuration
    3. Runs council deliberation (with caching)
    4. Returns approval/rejection with feedback
    """

    def __init__(
        self,
        llm_gateway,  # LLMGateway instance
        default_config: Optional[CouncilConfig] = None,
        enable_cache: bool = True,
        cache_maxsize: int = 128,
    ):
        """Initialize council executor.

        Args:
            llm_gateway: LLM gateway for making model calls
            default_config: Default council configuration
            enable_cache: Whether to enable result caching
            cache_maxsize: Maximum cache size
        """
        self.llm_gateway = llm_gateway
        self.default_config = default_config or CouncilConfig()
        self._councils: dict[str, CouncilOrchestrator] = {}
        self._cache_enabled = enable_cache
        self._cache = CouncilResultCache(maxsize=cache_maxsize) if enable_cache else None

    async def execute(
        self,
        request: CouncilExecutionRequest,
        skip_cache: bool = False,
    ) -> CouncilExecutionResult:
        """Execute council deliberation on proposed code.

        Args:
            request: Council execution request
            skip_cache: If True, bypass cache lookup

        Returns:
            CouncilExecutionResult with approval decision
        """
        # Check cache first
        cache_key = None
        if self._cache_enabled and self._cache and not skip_cache:
            cache_key = _compute_cache_key(
                task=request.task_description,
                proposed_code=request.proposed_code,
                context=request.context,
                perspectives=request.council_perspectives,
            )
            cached_result = self._cache.get(cache_key)
            if cached_result is not None:
                logger.info("Using cached council result")
                return self._build_execution_result(cached_result)

        # Select appropriate council based on task analysis
        council = self._select_council(request)

        # Build LLM callback
        llm_callback = self._create_llm_callback(request.user_id, request.session_id)

        # Run deliberation
        result = await council.deliberate(
            task=request.task_description,
            proposed_code=request.proposed_code,
            context=request.context,
            llm_callback=llm_callback,
        )

        # Cache result
        if self._cache_enabled and self._cache and cache_key:
            self._cache.set(cache_key, result)

        # Build execution result
        return self._build_execution_result(result)

    def clear_cache(self) -> None:
        """Clear the result cache."""
        if self._cache:
            self._cache.clear()
            logger.info("Council result cache cleared")

    @property
    def cache_size(self) -> int:
        """Get current cache size."""
        return self._cache.size if self._cache else 0

    def _select_council(self, request: CouncilExecutionRequest) -> CouncilOrchestrator:
        """Select appropriate council based on request."""
        # Check for specific perspectives requested
        if request.council_perspectives:
            return CouncilBuilder()\
                .with_perspectives(*request.council_perspectives)\
                .with_strategy(CouncilStrategy.PARALLEL)\
                .build()

        # Select based on task analysis
        if request.analysis:
            return self._council_for_analysis(request.analysis)

        # Default council
        return create_standard_council()

    def _council_for_analysis(self, analysis: TaskAnalysis) -> CouncilOrchestrator:
        """Select council based on task analysis."""
        # Security-focused for high-risk or security tasks
        if analysis.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return create_security_focused_council()

        if analysis.task_type == TaskType.SECURITY:
            return create_security_focused_council()

        # Thorough council for complex/critical tasks
        if analysis.complexity in (Complexity.COMPLEX, Complexity.CRITICAL):
            return create_thorough_council()

        # Fast council for simple tasks
        if analysis.complexity in (Complexity.TRIVIAL, Complexity.SIMPLE):
            return create_fast_council()

        # Standard council for everything else
        return create_standard_council()

    def _create_llm_callback(
        self,
        user_id: str,
        session_id: str,
    ) -> Callable[[str, str, str], Coroutine[Any, Any, str]]:
        """Create LLM callback for council perspectives."""
        async def callback(system: str, prompt: str, model_tier: str = "standard") -> str:
            """Call LLM for perspective analysis.

            Args:
                system: System prompt for the perspective
                prompt: User prompt with analysis request
                model_tier: Model tier to use (default: standard)

            Returns:
                LLM response content
            """
            try:
                from ..llm.gateway import Message, ModelTier

                messages = [
                    Message(role="system", content=system),
                    Message(role="user", content=prompt),
                ]

                tier = ModelTier(model_tier) if model_tier else ModelTier.STANDARD

                # Use non-streaming completion for perspectives
                response = await self.llm_gateway.complete(
                    messages=messages,
                    tier=tier,
                    user_id=user_id,
                    session_id=session_id,
                )

                return response.content
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                return f"[Error: LLM call failed - {str(e)}]"

        return callback

    def _build_execution_result(self, result: CouncilResult) -> CouncilExecutionResult:
        """Build execution result from council result."""
        # Determine if modifications were suggested
        modifications_suggested = bool(result.recommendations)

        # Extract blocking reasons
        blocking_reasons = []
        if not result.approved:
            for issue in result.consensus.critical_issues:
                blocking_reasons.append(f"[{issue.severity.value}] {issue.message}")

            if result.quality_score and not result.quality_score.passed:
                blocking_reasons.extend(result.quality_score.blocking_issues)

        return CouncilExecutionResult(
            approved=result.approved,
            council_result=result,
            should_proceed=result.approved,
            modifications_suggested=modifications_suggested,
            suggested_code=result.final_code,
            blocking_reasons=blocking_reasons,
            recommendations=result.recommendations,
        )


class CouncilMiddleware:
    """Middleware for injecting council checks into execution flow.

    Can be used to wrap tool execution or code generation to
    automatically run council checks on proposed changes.
    """

    def __init__(self, executor: CouncilExecutor):
        self.executor = executor
        self._enabled = True
        self._skip_patterns: list[str] = []

    def enable(self) -> None:
        """Enable council checks."""
        self._enabled = True

    def disable(self) -> None:
        """Disable council checks."""
        self._enabled = False

    def skip_pattern(self, pattern: str) -> None:
        """Add pattern to skip council for."""
        self._skip_patterns.append(pattern)

    async def check_code(
        self,
        task_id: str,
        session_id: str,
        user_id: str,
        task_description: str,
        code: str,
        context: dict[str, Any],
    ) -> tuple[bool, Optional[str], list[str]]:
        """Check code with council.

        Returns:
            Tuple of (approved, modified_code, recommendations)
        """
        if not self._enabled:
            return True, None, []

        # Check skip patterns
        import re
        for pattern in self._skip_patterns:
            if re.search(pattern, task_description, re.IGNORECASE):
                return True, None, []

        request = CouncilExecutionRequest(
            task_id=task_id,
            session_id=session_id,
            user_id=user_id,
            task_description=task_description,
            proposed_code=code,
            context=context,
        )

        result = await self.executor.execute(request)

        return (
            result.approved,
            result.suggested_code,
            result.recommendations,
        )


# Integration helpers

def create_council_executor(llm_gateway) -> CouncilExecutor:
    """Create a council executor with default config."""
    return CouncilExecutor(llm_gateway)


def create_council_middleware(llm_gateway) -> CouncilMiddleware:
    """Create council middleware for injection into execution flow."""
    executor = create_council_executor(llm_gateway)
    return CouncilMiddleware(executor)
