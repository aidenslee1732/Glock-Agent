"""Council Orchestrator - Coordinates multi-perspective deliberation.

The orchestrator spawns perspective threads, manages parallel execution,
coordinates the synthesis of results, and produces final recommendations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional
from enum import Enum

from .perspectives import (
    Perspective,
    PerspectiveResult,
    PerspectiveType,
    get_perspective,
    PERSPECTIVE_REGISTRY,
)
from .synthesis import SynthesisEngine, ConsensusResult
from .debate import DebateEngine
from .quality_gate import QualityGate, QualityScore

logger = logging.getLogger(__name__)


class CouncilStrategy(str, Enum):
    """Strategies for council deliberation."""
    PARALLEL = "parallel"        # All perspectives run simultaneously
    SEQUENTIAL = "sequential"    # Perspectives run one after another
    TIERED = "tiered"           # Critical perspectives first, then others
    ADAPTIVE = "adaptive"        # Adjust based on early results


async def _execute_with_timeout(
    coro,
    timeout_seconds: float,
    perspective_name: str,
) -> PerspectiveResult:
    """Execute a coroutine with timeout, returning a timeout result on failure.

    Args:
        coro: Coroutine to execute
        timeout_seconds: Timeout in seconds
        perspective_name: Name of the perspective for logging

    Returns:
        PerspectiveResult from coroutine or timeout result
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning(f"Perspective {perspective_name} timed out after {timeout_seconds}s")
        return PerspectiveResult(
            perspective_type=PerspectiveType(perspective_name) if perspective_name in [e.value for e in PerspectiveType] else PerspectiveType.CORRECTNESS,
            approved=False,
            confidence=0.0,
            reasoning=f"Perspective timed out after {timeout_seconds}s",
            metadata={"error": "timeout", "timeout_seconds": timeout_seconds},
        )


@dataclass
class CouncilConfig:
    """Configuration for council deliberation."""
    perspectives: list[str] = field(default_factory=lambda: [
        "correctness", "security", "simplicity", "edge_cases"
    ])
    strategy: CouncilStrategy = CouncilStrategy.PARALLEL
    enable_debate: bool = False
    max_debate_rounds: int = 2
    timeout_per_perspective: float = 30.0
    total_timeout: float = 120.0
    require_quorum: int = 3  # Minimum perspectives that must respond
    enable_quality_gate: bool = True
    quality_gate_min_score: float = 60.0
    model_tier: str = "standard"


@dataclass
class CouncilResult:
    """Result of council deliberation."""
    approved: bool
    consensus: ConsensusResult
    quality_score: Optional[QualityScore]
    perspective_results: list[PerspectiveResult]
    execution_time_ms: int
    perspectives_completed: int
    perspectives_failed: int
    final_code: Optional[str] = None
    recommendations: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "approved": self.approved,
            "confidence": self.consensus.confidence,
            "vote_summary": {k.value: v for k, v in self.consensus.vote_summary.items()},
            "quality_score": self.quality_score.overall if self.quality_score else None,
            "quality_level": self.quality_score.level.value if self.quality_score else None,
            "perspectives_completed": self.perspectives_completed,
            "execution_time_ms": self.execution_time_ms,
            "recommendations": self.recommendations,
            "blocking_issues": [
                {"severity": i.severity.value, "message": i.message}
                for i in self.consensus.critical_issues
            ],
            "dissenting_perspectives": [
                p.value for p in self.consensus.dissenting_perspectives
            ],
        }


# Type alias for LLM callback
LLMCallback = Callable[[str, str, str], Coroutine[Any, Any, str]]


class CouncilOrchestrator:
    """Orchestrates multi-perspective council deliberation.

    The orchestrator:
    1. Spawns perspective analysis threads
    2. Manages parallel execution with timeouts
    3. Runs optional quality gate
    4. Coordinates synthesis of results
    5. Optionally runs debate rounds
    6. Produces final recommendation
    """

    def __init__(
        self,
        config: Optional[CouncilConfig] = None,
        synthesis_engine: Optional[SynthesisEngine] = None,
        quality_gate: Optional[QualityGate] = None,
    ):
        """Initialize the council orchestrator.

        Args:
            config: Council configuration
            synthesis_engine: Engine for synthesizing results
            quality_gate: Gate for quality checks
        """
        self.config = config or CouncilConfig()
        self.synthesis = synthesis_engine or SynthesisEngine()
        self.quality_gate = quality_gate or QualityGate(
            min_score=self.config.quality_gate_min_score
        )
        self.debate = DebateEngine(max_rounds=self.config.max_debate_rounds)

        # Initialize perspectives
        self._perspectives: list[Perspective] = []
        self._init_perspectives()

    def _init_perspectives(self) -> None:
        """Initialize perspective instances."""
        self._perspectives = []
        for name in self.config.perspectives:
            if name in PERSPECTIVE_REGISTRY:
                self._perspectives.append(
                    get_perspective(name, self.config.model_tier)
                )
            else:
                logger.warning(f"Unknown perspective: {name}")

    async def deliberate(
        self,
        task: str,
        proposed_code: str,
        context: dict[str, Any],
        llm_callback: LLMCallback,
    ) -> CouncilResult:
        """Run council deliberation on proposed code.

        Args:
            task: The original task description
            proposed_code: Code to evaluate
            context: Additional context (files, requirements, etc.)
            llm_callback: Async function to call LLM (system, prompt, model_tier) -> response

        Returns:
            CouncilResult with decision and details
        """
        start_time = time.time()

        # Run quality gate first if enabled
        quality_score = None
        if self.config.enable_quality_gate:
            language = context.get("language", "python")
            quality_score = self.quality_gate.evaluate(proposed_code, language, context)

            # Early exit if quality gate fails hard
            if not quality_score.passed and quality_score.overall < 30:
                return self._create_quality_blocked_result(
                    quality_score, start_time
                )

        # Run perspectives based on strategy
        perspective_results = await self._run_perspectives(
            task=task,
            proposed_code=proposed_code,
            context=context,
            llm_callback=llm_callback,
        )

        # Check quorum
        if len(perspective_results) < self.config.require_quorum:
            logger.warning(
                f"Quorum not met: {len(perspective_results)}/{self.config.require_quorum}"
            )

        # Run debate if enabled and there's disagreement
        if self.config.enable_debate and self._has_disagreement(perspective_results):
            perspective_results = await self.debate.debate(
                initial_results=perspective_results,
                perspectives=self._perspectives,
                llm_callback=lambda s, p: llm_callback(s, p, self.config.model_tier),
            )

        # Synthesize results
        consensus = self.synthesis.synthesize(
            results=perspective_results,
            perspectives=self._perspectives,
        )

        # Calculate execution time
        execution_time_ms = int((time.time() - start_time) * 1000)

        # Count completed/failed
        completed = len(perspective_results)
        failed = len(self._perspectives) - completed

        # Build recommendations
        recommendations = self._build_recommendations(
            consensus=consensus,
            quality_score=quality_score,
        )

        # Determine final approval (considering quality gate)
        approved = consensus.approved
        if quality_score and not quality_score.passed:
            approved = False

        return CouncilResult(
            approved=approved,
            consensus=consensus,
            quality_score=quality_score,
            perspective_results=perspective_results,
            execution_time_ms=execution_time_ms,
            perspectives_completed=completed,
            perspectives_failed=failed,
            final_code=proposed_code if approved else None,
            recommendations=recommendations,
            metadata={
                "strategy": self.config.strategy.value,
                "debate_enabled": self.config.enable_debate,
                "quorum_met": completed >= self.config.require_quorum,
            }
        )

    async def _run_perspectives(
        self,
        task: str,
        proposed_code: str,
        context: dict[str, Any],
        llm_callback: LLMCallback,
    ) -> list[PerspectiveResult]:
        """Run all perspectives according to strategy."""
        if self.config.strategy == CouncilStrategy.PARALLEL:
            return await self._run_parallel(
                task, proposed_code, context, llm_callback
            )
        elif self.config.strategy == CouncilStrategy.SEQUENTIAL:
            return await self._run_sequential(
                task, proposed_code, context, llm_callback
            )
        elif self.config.strategy == CouncilStrategy.TIERED:
            return await self._run_tiered(
                task, proposed_code, context, llm_callback
            )
        else:  # ADAPTIVE
            return await self._run_adaptive(
                task, proposed_code, context, llm_callback
            )

    async def _run_parallel(
        self,
        task: str,
        proposed_code: str,
        context: dict[str, Any],
        llm_callback: LLMCallback,
    ) -> list[PerspectiveResult]:
        """Run all perspectives in parallel."""
        tasks = []

        for perspective in self._perspectives:
            task_coro = self._run_single_perspective(
                perspective=perspective,
                task=task,
                proposed_code=proposed_code,
                context=context,
                llm_callback=llm_callback,
            )
            tasks.append(asyncio.create_task(task_coro))

        # Wait for all with timeout
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self.config.total_timeout,
            )
        except asyncio.TimeoutError:
            # Cancel remaining tasks
            for t in tasks:
                if not t.done():
                    t.cancel()
            # Safely collect results, handling cancelled tasks
            results = []
            for t in tasks:
                if t.done() and not t.cancelled():
                    try:
                        results.append(t.result())
                    except Exception as e:
                        logger.error(f"Task failed: {e}")
                        results.append(None)
                else:
                    results.append(None)

        # Filter successful results
        perspective_results = []
        for result in results:
            if isinstance(result, PerspectiveResult):
                perspective_results.append(result)
            elif isinstance(result, Exception):
                logger.error(f"Perspective failed: {result}")

        return perspective_results

    async def _run_sequential(
        self,
        task: str,
        proposed_code: str,
        context: dict[str, Any],
        llm_callback: LLMCallback,
    ) -> list[PerspectiveResult]:
        """Run perspectives sequentially."""
        results = []

        for perspective in self._perspectives:
            try:
                result = await asyncio.wait_for(
                    self._run_single_perspective(
                        perspective=perspective,
                        task=task,
                        proposed_code=proposed_code,
                        context=context,
                        llm_callback=llm_callback,
                    ),
                    timeout=self.config.timeout_per_perspective,
                )
                results.append(result)
            except asyncio.TimeoutError:
                logger.warning(f"Perspective {perspective.perspective_type} timed out")
            except Exception as e:
                logger.error(f"Perspective {perspective.perspective_type} failed: {e}")

        return results

    async def _run_tiered(
        self,
        task: str,
        proposed_code: str,
        context: dict[str, Any],
        llm_callback: LLMCallback,
    ) -> list[PerspectiveResult]:
        """Run critical perspectives first, then others."""
        # Critical perspectives that must run first
        critical_types = {PerspectiveType.CORRECTNESS, PerspectiveType.SECURITY}

        critical = [p for p in self._perspectives if p.perspective_type in critical_types]
        others = [p for p in self._perspectives if p.perspective_type not in critical_types]

        # Run critical first
        critical_results = await self._run_parallel_subset(
            perspectives=critical,
            task=task,
            proposed_code=proposed_code,
            context=context,
            llm_callback=llm_callback,
        )

        # Check if critical perspectives rejected
        critical_rejections = [r for r in critical_results if not r.approved]
        if critical_rejections:
            # Early exit - don't bother with other perspectives
            return critical_results

        # Run remaining perspectives
        other_results = await self._run_parallel_subset(
            perspectives=others,
            task=task,
            proposed_code=proposed_code,
            context=context,
            llm_callback=llm_callback,
        )

        return critical_results + other_results

    async def _run_adaptive(
        self,
        task: str,
        proposed_code: str,
        context: dict[str, Any],
        llm_callback: LLMCallback,
    ) -> list[PerspectiveResult]:
        """Adaptively run perspectives based on early results."""
        # Start with correctness
        correctness_perspective = next(
            (p for p in self._perspectives if p.perspective_type == PerspectiveType.CORRECTNESS),
            None
        )

        if not correctness_perspective:
            # Fall back to parallel
            return await self._run_parallel(task, proposed_code, context, llm_callback)

        # Run correctness first
        correctness_result = await self._run_single_perspective(
            perspective=correctness_perspective,
            task=task,
            proposed_code=proposed_code,
            context=context,
            llm_callback=llm_callback,
        )

        if not correctness_result.approved:
            # Code is incorrect - no point checking other aspects
            return [correctness_result]

        # Code is correct - run security
        security_perspective = next(
            (p for p in self._perspectives if p.perspective_type == PerspectiveType.SECURITY),
            None
        )

        results = [correctness_result]

        if security_perspective:
            security_result = await self._run_single_perspective(
                perspective=security_perspective,
                task=task,
                proposed_code=proposed_code,
                context=context,
                llm_callback=llm_callback,
            )
            results.append(security_result)

            if not security_result.approved:
                # Security issues - may not need other perspectives
                return results

        # Run remaining perspectives in parallel
        remaining = [
            p for p in self._perspectives
            if p.perspective_type not in {PerspectiveType.CORRECTNESS, PerspectiveType.SECURITY}
        ]

        if remaining:
            remaining_results = await self._run_parallel_subset(
                perspectives=remaining,
                task=task,
                proposed_code=proposed_code,
                context=context,
                llm_callback=llm_callback,
            )
            results.extend(remaining_results)

        return results

    async def _run_parallel_subset(
        self,
        perspectives: list[Perspective],
        task: str,
        proposed_code: str,
        context: dict[str, Any],
        llm_callback: LLMCallback,
    ) -> list[PerspectiveResult]:
        """Run a subset of perspectives in parallel."""
        tasks = []

        for perspective in perspectives:
            task_coro = self._run_single_perspective(
                perspective=perspective,
                task=task,
                proposed_code=proposed_code,
                context=context,
                llm_callback=llm_callback,
            )
            tasks.append(asyncio.create_task(task_coro))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        return [r for r in results if isinstance(r, PerspectiveResult)]

    async def _run_single_perspective(
        self,
        perspective: Perspective,
        task: str,
        proposed_code: str,
        context: dict[str, Any],
        llm_callback: LLMCallback,
    ) -> PerspectiveResult:
        """Run a single perspective analysis."""
        system_prompt = perspective.get_system_prompt()
        analysis_prompt = perspective.get_analysis_prompt(task, proposed_code, context)

        try:
            response = await asyncio.wait_for(
                llm_callback(system_prompt, analysis_prompt, self.config.model_tier),
                timeout=self.config.timeout_per_perspective,
            )
            return perspective.parse_response(response)
        except asyncio.TimeoutError:
            # Return a timeout result
            return PerspectiveResult(
                perspective_type=perspective.perspective_type,
                approved=False,
                confidence=0.0,
                reasoning="Perspective timed out",
                metadata={"error": "timeout"},
            )
        except Exception as e:
            logger.error(f"Perspective {perspective.perspective_type} error: {e}")
            return PerspectiveResult(
                perspective_type=perspective.perspective_type,
                approved=False,
                confidence=0.0,
                reasoning=f"Perspective failed: {str(e)}",
                metadata={"error": str(e)},
            )

    def _has_disagreement(self, results: list[PerspectiveResult]) -> bool:
        """Check if perspectives disagree."""
        if len(results) < 2:
            return False
        approvals = [r.approved for r in results]
        return not all(approvals) and any(approvals)

    def _create_quality_blocked_result(
        self,
        quality_score: QualityScore,
        start_time: float,
    ) -> CouncilResult:
        """Create a result for quality gate block."""
        from .synthesis import ConsensusResult, VoteType

        return CouncilResult(
            approved=False,
            consensus=ConsensusResult(
                approved=False,
                confidence=0.9,
                vote_summary={VoteType.REJECT: 1, VoteType.APPROVE: 0, VoteType.ABSTAIN: 0},
                weighted_score=0.0,
                critical_issues=[],
                all_issues=[],
                conflicts=[],
                recommendations=quality_score.recommendations,
                reasoning=f"Quality gate blocked: score {quality_score.overall:.1f} < minimum",
                dissenting_perspectives=[],
            ),
            quality_score=quality_score,
            perspective_results=[],
            execution_time_ms=int((time.time() - start_time) * 1000),
            perspectives_completed=0,
            perspectives_failed=0,
            recommendations=quality_score.recommendations + quality_score.blocking_issues,
        )

    def _build_recommendations(
        self,
        consensus: ConsensusResult,
        quality_score: Optional[QualityScore],
    ) -> list[str]:
        """Build final list of recommendations."""
        recommendations = []

        # Add quality recommendations
        if quality_score:
            recommendations.extend(quality_score.recommendations)

        # Add consensus recommendations
        recommendations.extend(consensus.recommendations)

        # Deduplicate
        seen = set()
        unique = []
        for rec in recommendations:
            if rec.lower() not in seen:
                seen.add(rec.lower())
                unique.append(rec)

        return unique


class CouncilBuilder:
    """Builder for creating council configurations."""

    def __init__(self):
        self._config = CouncilConfig()

    def with_perspectives(self, *perspectives: str) -> "CouncilBuilder":
        """Set perspectives to use."""
        self._config.perspectives = list(perspectives)
        return self

    def with_strategy(self, strategy: CouncilStrategy) -> "CouncilBuilder":
        """Set execution strategy."""
        self._config.strategy = strategy
        return self

    def with_debate(self, enable: bool = True, max_rounds: int = 2) -> "CouncilBuilder":
        """Enable/disable debate."""
        self._config.enable_debate = enable
        self._config.max_debate_rounds = max_rounds
        return self

    def with_quality_gate(
        self,
        enable: bool = True,
        min_score: float = 60.0,
    ) -> "CouncilBuilder":
        """Configure quality gate."""
        self._config.enable_quality_gate = enable
        self._config.quality_gate_min_score = min_score
        return self

    def with_timeouts(
        self,
        per_perspective: float = 30.0,
        total: float = 120.0,
    ) -> "CouncilBuilder":
        """Set timeouts."""
        self._config.timeout_per_perspective = per_perspective
        self._config.total_timeout = total
        return self

    def with_quorum(self, quorum: int) -> "CouncilBuilder":
        """Set minimum quorum."""
        self._config.require_quorum = quorum
        return self

    def with_model_tier(self, tier: str) -> "CouncilBuilder":
        """Set model tier for perspectives."""
        self._config.model_tier = tier
        return self

    def build(self) -> CouncilOrchestrator:
        """Build the orchestrator."""
        return CouncilOrchestrator(config=self._config)


# Convenience functions
def create_standard_council() -> CouncilOrchestrator:
    """Create a standard council with default settings."""
    return CouncilBuilder()\
        .with_perspectives("correctness", "security", "simplicity", "edge_cases")\
        .with_strategy(CouncilStrategy.PARALLEL)\
        .with_quality_gate(True, 60.0)\
        .build()


def create_security_focused_council() -> CouncilOrchestrator:
    """Create a security-focused council."""
    return CouncilBuilder()\
        .with_perspectives("security", "attack_surface", "data_integrity", "correctness")\
        .with_strategy(CouncilStrategy.TIERED)\
        .with_quality_gate(True, 70.0)\
        .build()


def create_fast_council() -> CouncilOrchestrator:
    """Create a fast council for quick checks."""
    return CouncilBuilder()\
        .with_perspectives("correctness", "security")\
        .with_strategy(CouncilStrategy.PARALLEL)\
        .with_timeouts(15.0, 45.0)\
        .with_quorum(2)\
        .with_model_tier("fast")\
        .build()


def create_thorough_council() -> CouncilOrchestrator:
    """Create a thorough council for critical code."""
    return CouncilBuilder()\
        .with_perspectives(
            "correctness", "security", "simplicity", "edge_cases",
            "performance", "maintainability", "data_integrity"
        )\
        .with_strategy(CouncilStrategy.ADAPTIVE)\
        .with_debate(True, 3)\
        .with_quality_gate(True, 75.0)\
        .with_model_tier("advanced")\
        .build()
