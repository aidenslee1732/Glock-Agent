"""LLM Council - Multi-perspective deliberation system.

The council system enables multiple LLM perspectives to analyze tasks
and synthesize solutions through voting and consensus building.

v4 Enhancements:
- Structured output schema for reliable JSON parsing
- Test generation perspective
- Quality gate with test execution
"""

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
from .perspectives import (
    Perspective,
    PerspectiveType,
    PerspectiveResult,
    CorrectnessChecker,
    SecurityReviewer,
    SimplicityAdvocate,
    EdgeCaseFinder,
    PerformanceAnalyzer,
    MaintainabilityExpert,
    DataIntegrityGuard,
    AttackSurfaceAnalyzer,
    get_perspective,
    get_all_perspectives,
    PERSPECTIVE_REGISTRY,
)
from .synthesis import (
    SynthesisEngine,
    Vote,
    Conflict,
    ConsensusResult,
)
from .debate import (
    DebateEngine,
    DebateConfig,
    DebateResult,
    DebateRound,
    create_standard_debate_engine,
    create_quick_debate_engine,
    create_thorough_debate_engine,
)
from .quality_gate import (
    QualityGate,
    QualityGateConfig,
    QualityScore,
    QualityMetrics,
    QualityLevel,
    QualityGateWithTests,
)
from .executor import (
    CouncilExecutor,
    CouncilMiddleware,
    CouncilExecutionRequest,
    CouncilExecutionResult,
    CouncilResultCache,
    create_council_executor,
    create_council_middleware,
)
# v4: Structured output
from .schema import (
    StructuredPerspectiveOutput,
    StructuredIssue,
    StructuredTestOutput,
    parse_structured_response,
    create_structured_prompt,
)
# v4: Test perspective
from .test_perspective import (
    TestGeneratorPerspective,
    TestValidationPerspective,
)

__all__ = [
    # Orchestrator
    "CouncilOrchestrator",
    "CouncilResult",
    "CouncilConfig",
    "CouncilStrategy",
    "CouncilBuilder",
    "create_standard_council",
    "create_security_focused_council",
    "create_fast_council",
    "create_thorough_council",
    # Perspectives
    "Perspective",
    "PerspectiveType",
    "PerspectiveResult",
    "CorrectnessChecker",
    "SecurityReviewer",
    "SimplicityAdvocate",
    "EdgeCaseFinder",
    "PerformanceAnalyzer",
    "MaintainabilityExpert",
    "DataIntegrityGuard",
    "AttackSurfaceAnalyzer",
    "get_perspective",
    "get_all_perspectives",
    "PERSPECTIVE_REGISTRY",
    # Synthesis
    "SynthesisEngine",
    "Vote",
    "Conflict",
    "ConsensusResult",
    # Debate
    "DebateEngine",
    "DebateConfig",
    "DebateResult",
    "DebateRound",
    "create_standard_debate_engine",
    "create_quick_debate_engine",
    "create_thorough_debate_engine",
    # Quality
    "QualityGate",
    "QualityGateConfig",
    "QualityScore",
    "QualityMetrics",
    "QualityLevel",
    "QualityGateWithTests",
    # Executor
    "CouncilExecutor",
    "CouncilMiddleware",
    "CouncilExecutionRequest",
    "CouncilExecutionResult",
    "CouncilResultCache",
    "create_council_executor",
    "create_council_middleware",
    # v4: Structured output
    "StructuredPerspectiveOutput",
    "StructuredIssue",
    "StructuredTestOutput",
    "parse_structured_response",
    "create_structured_prompt",
    # v4: Test perspective
    "TestGeneratorPerspective",
    "TestValidationPerspective",
]
