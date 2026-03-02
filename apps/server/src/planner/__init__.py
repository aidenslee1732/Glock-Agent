"""Planner service - task analysis, plan compilation, and signing (Model B)."""

from .analyzer import TaskAnalyzer, TaskAnalysis, TaskType, Complexity, RiskLevel
from .compiler import PlanCompiler, CompilationContext
from .router import TaskRouter, ExecutionStrategy, ModelTier, ExecutionPlan
from .orchestrator import TaskOrchestrator, TaskContext, TaskState, OrchestratorConfig
from .memory import MemoryManager, UserPreferences, MemoryConfig

# Re-export CompiledPlan from shared protocol for convenience
from packages.shared_protocol.types import CompiledPlan

# Council system for multi-perspective deliberation
from .council import (
    CouncilOrchestrator,
    CouncilResult,
    CouncilConfig,
    CouncilStrategy,
    CouncilExecutor,
    CouncilMiddleware,
    create_standard_council,
    create_security_focused_council,
    create_fast_council,
    create_thorough_council,
    QualityGate,
    QualityScore,
)

__all__ = [
    # Analyzer
    "TaskAnalyzer",
    "TaskAnalysis",
    "TaskType",
    "Complexity",
    "RiskLevel",
    # Compiler
    "PlanCompiler",
    "CompilationContext",
    "CompiledPlan",
    # Router
    "TaskRouter",
    "ExecutionStrategy",
    "ModelTier",
    "ExecutionPlan",
    # Orchestrator
    "TaskOrchestrator",
    "TaskContext",
    "TaskState",
    "OrchestratorConfig",
    # Memory
    "MemoryManager",
    "UserPreferences",
    "MemoryConfig",
    # Council
    "CouncilOrchestrator",
    "CouncilResult",
    "CouncilConfig",
    "CouncilStrategy",
    "CouncilExecutor",
    "CouncilMiddleware",
    "create_standard_council",
    "create_security_focused_council",
    "create_fast_council",
    "create_thorough_council",
    "QualityGate",
    "QualityScore",
]
