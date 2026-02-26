"""Task router - routes tasks to appropriate execution strategies."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .analyzer import TaskAnalyzer, TaskAnalysis, TaskType, Complexity, RiskLevel

logger = logging.getLogger(__name__)


class ExecutionStrategy(str, Enum):
    """Task execution strategies."""
    DIRECT = "direct"           # Execute directly with single LLM
    COUNCIL = "council"         # Multiple perspectives synthesize solution
    SPECIALIST = "specialist"   # Route to domain specialist
    PARALLEL = "parallel"       # Execute subtasks in parallel
    ITERATIVE = "iterative"     # Step-by-step refinement
    HYBRID = "hybrid"           # Combined approach


class ModelTier(str, Enum):
    """LLM model tiers."""
    FAST = "fast"               # Quick, cheap (for simple tasks)
    STANDARD = "standard"       # Balanced
    ADVANCED = "advanced"       # Most capable (for complex tasks)
    REASONING = "reasoning"     # Extended thinking


@dataclass
class ExecutionPlan:
    """Execution plan determined by router."""
    strategy: ExecutionStrategy
    model_tier: ModelTier
    specialist_type: Optional[str] = None
    parallel_subtasks: list[str] = field(default_factory=list)
    council_perspectives: list[str] = field(default_factory=list)
    confidence: float = 0.8
    rationale: str = ""


@dataclass
class HistoricalMetrics:
    """Historical success metrics for routing decisions."""
    task_type: TaskType
    complexity: Complexity
    strategy: ExecutionStrategy
    success_rate: float
    avg_duration_ms: int
    sample_count: int


class TaskRouter:
    """Routes tasks to appropriate execution strategies.

    The router considers:
    1. Task type and complexity
    2. Risk level
    3. Historical success rates
    4. User preferences
    """

    def __init__(
        self,
        analyzer: Optional[TaskAnalyzer] = None,
        historical_metrics: Optional[list[HistoricalMetrics]] = None,
    ):
        self.analyzer = analyzer or TaskAnalyzer()
        self._historical = historical_metrics or []
        self._build_metrics_index()

    def _build_metrics_index(self) -> None:
        """Build index for fast metrics lookup."""
        self._metrics_index: dict[tuple, HistoricalMetrics] = {}
        for m in self._historical:
            key = (m.task_type, m.complexity, m.strategy)
            self._metrics_index[key] = m

    def route(
        self,
        prompt: str,
        context: Optional[dict[str, Any]] = None,
        user_preferences: Optional[dict[str, Any]] = None,
    ) -> ExecutionPlan:
        """Route a task to an execution strategy.

        Args:
            prompt: User's task description
            context: Workspace context
            user_preferences: User's routing preferences

        Returns:
            ExecutionPlan with strategy and model tier
        """
        # Analyze the task
        analysis = self.analyzer.analyze(prompt, context or {})

        # Determine strategy
        strategy = self._select_strategy(analysis, user_preferences)

        # Determine model tier
        model_tier = self._select_model_tier(analysis, strategy)

        # Build execution plan
        plan = ExecutionPlan(
            strategy=strategy,
            model_tier=model_tier,
            confidence=analysis.confidence,
        )

        # Add strategy-specific details
        if strategy == ExecutionStrategy.SPECIALIST:
            plan.specialist_type = self._select_specialist(analysis)
            plan.rationale = f"Routing to {plan.specialist_type} specialist"

        elif strategy == ExecutionStrategy.COUNCIL:
            plan.council_perspectives = self._select_council_perspectives(analysis)
            plan.rationale = f"Council deliberation with {len(plan.council_perspectives)} perspectives"

        elif strategy == ExecutionStrategy.PARALLEL:
            plan.parallel_subtasks = self._decompose_to_subtasks(prompt, analysis)
            plan.rationale = f"Parallel execution of {len(plan.parallel_subtasks)} subtasks"

        else:
            plan.rationale = f"Direct execution with {model_tier.value} model"

        logger.info(
            f"Task routed: strategy={strategy.value}, model={model_tier.value}, "
            f"confidence={plan.confidence:.2f}"
        )

        return plan

    def _select_strategy(
        self,
        analysis: TaskAnalysis,
        user_preferences: Optional[dict[str, Any]],
    ) -> ExecutionStrategy:
        """Select execution strategy based on analysis."""
        preferences = user_preferences or {}

        # Check for user-specified strategy
        if "strategy" in preferences:
            try:
                return ExecutionStrategy(preferences["strategy"])
            except ValueError:
                pass

        # High-risk tasks use council for multiple perspectives
        if analysis.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return ExecutionStrategy.COUNCIL

        # Complex tasks may benefit from specialists
        if analysis.complexity in (Complexity.COMPLEX, Complexity.CRITICAL):
            if analysis.task_type in (TaskType.SECURITY, TaskType.DEBUG):
                return ExecutionStrategy.SPECIALIST
            return ExecutionStrategy.COUNCIL

        # Simple questions use direct
        if analysis.task_type == TaskType.QUESTION:
            return ExecutionStrategy.DIRECT

        # Check historical success rates
        best_strategy = self._check_historical_success(analysis)
        if best_strategy:
            return best_strategy

        # Default to direct for simple/moderate tasks
        if analysis.complexity in (Complexity.TRIVIAL, Complexity.SIMPLE):
            return ExecutionStrategy.DIRECT

        return ExecutionStrategy.ITERATIVE

    def _select_model_tier(
        self,
        analysis: TaskAnalysis,
        strategy: ExecutionStrategy,
    ) -> ModelTier:
        """Select model tier based on analysis and strategy."""
        # Council always uses advanced models
        if strategy == ExecutionStrategy.COUNCIL:
            return ModelTier.ADVANCED

        # Specialists use standard (they're optimized for domain)
        if strategy == ExecutionStrategy.SPECIALIST:
            return ModelTier.STANDARD

        # Questions can use fast models
        if analysis.task_type == TaskType.QUESTION:
            if analysis.complexity == Complexity.TRIVIAL:
                return ModelTier.FAST
            return ModelTier.STANDARD

        # Complex tasks use advanced
        if analysis.complexity in (Complexity.COMPLEX, Complexity.CRITICAL):
            return ModelTier.ADVANCED

        # High-risk uses advanced (extra care)
        if analysis.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return ModelTier.ADVANCED

        # Default
        if analysis.complexity == Complexity.TRIVIAL:
            return ModelTier.FAST
        return ModelTier.STANDARD

    def _select_specialist(self, analysis: TaskAnalysis) -> str:
        """Select specialist type based on task analysis."""
        type_to_specialist = {
            TaskType.DEBUG: "debugger",
            TaskType.SECURITY: "security",
            TaskType.REFACTOR: "refactor",
            TaskType.TEST: "testing",
            TaskType.DEPLOY: "devops",
        }

        specialist = type_to_specialist.get(analysis.task_type, "general")

        # Check for language-specific specialists from risk flags
        # (could be enhanced with file analysis)

        return specialist

    def _select_council_perspectives(self, analysis: TaskAnalysis) -> list[str]:
        """Select council perspectives for deliberation."""
        perspectives = ["correctness"]  # Always check correctness

        # Add type-specific perspectives
        if analysis.task_type == TaskType.SECURITY:
            perspectives.extend(["security", "attack_surface"])
        elif analysis.task_type == TaskType.REFACTOR:
            perspectives.extend(["simplicity", "maintainability"])
        elif analysis.task_type == TaskType.IMPLEMENT:
            perspectives.extend(["simplicity", "edge_cases"])

        # Add risk-based perspectives
        if "auth" in analysis.risk_flags:
            if "security" not in perspectives:
                perspectives.append("security")
        if "data" in analysis.risk_flags:
            perspectives.append("data_integrity")

        # Limit to 4 perspectives
        return perspectives[:4]

    def _decompose_to_subtasks(
        self,
        prompt: str,
        analysis: TaskAnalysis,
    ) -> list[str]:
        """Decompose task into parallel subtasks."""
        # Simple heuristic decomposition
        # In production, this would use an LLM to decompose

        subtasks = []

        # If multiple files mentioned, treat each as subtask
        # This is a placeholder - real implementation would be smarter

        if analysis.task_type == TaskType.IMPLEMENT:
            subtasks = [
                "Implement core functionality",
                "Add error handling",
                "Write tests",
            ]
        elif analysis.task_type == TaskType.REFACTOR:
            subtasks = [
                "Analyze current implementation",
                "Refactor core logic",
                "Update tests",
            ]

        return subtasks

    def _check_historical_success(
        self,
        analysis: TaskAnalysis,
    ) -> Optional[ExecutionStrategy]:
        """Check historical success rates for strategy selection."""
        best_strategy = None
        best_score = 0.0

        for strategy in ExecutionStrategy:
            key = (analysis.task_type, analysis.complexity, strategy)
            metrics = self._metrics_index.get(key)

            if metrics and metrics.sample_count >= 5:
                # Score combines success rate and sample size confidence
                confidence = min(1.0, metrics.sample_count / 20)
                score = metrics.success_rate * confidence

                if score > best_score and score > 0.7:
                    best_score = score
                    best_strategy = strategy

        return best_strategy

    def update_metrics(
        self,
        task_type: TaskType,
        complexity: Complexity,
        strategy: ExecutionStrategy,
        success: bool,
        duration_ms: int,
    ) -> None:
        """Update historical metrics with task outcome.

        Args:
            task_type: Type of task
            complexity: Task complexity
            strategy: Strategy used
            success: Whether task succeeded
            duration_ms: Execution duration
        """
        key = (task_type, complexity, strategy)
        metrics = self._metrics_index.get(key)

        if metrics:
            # Update existing metrics
            n = metrics.sample_count
            metrics.success_rate = (metrics.success_rate * n + (1 if success else 0)) / (n + 1)
            metrics.avg_duration_ms = (metrics.avg_duration_ms * n + duration_ms) // (n + 1)
            metrics.sample_count = n + 1
        else:
            # Create new metrics
            new_metrics = HistoricalMetrics(
                task_type=task_type,
                complexity=complexity,
                strategy=strategy,
                success_rate=1.0 if success else 0.0,
                avg_duration_ms=duration_ms,
                sample_count=1,
            )
            self._historical.append(new_metrics)
            self._metrics_index[key] = new_metrics
