"""Client-side orchestration for Model B.

v4 Enhancements:
- Parallel tool execution
- Council integration for code review
- Pre-flight checks (lint, type, syntax)
- Retry logic for transient failures
- Execution tracing for debugging
"""

from .engine import (
    OrchestrationEngine,
    OrchestrationConfig,
    OrchestrationEvent,
    TaskResult,
    EventType,
)
from .sub_agent import (
    SubAgentCoordinator,
    SubAgent,
    SubAgentResult,
    SubAgentConfig,
    SubAgentStatus,
    create_sub_agent_coordinator,
)

# v4 components
from .parallel_executor import (
    ParallelToolExecutor,
    ToolCall,
    ParallelExecutionResult,
    ExecutionBatch,
)
from .retry import (
    RetryableOperation,
    RetryConfig,
    RetryResult,
    RetryAttempt,
    retry_on_failure,
    RetryContext,
)
from .council_integration import (
    CouncilIntegration,
    CouncilResult,
    detect_language,
)
from .preflight import (
    PreflightChecker,
    PreflightResult,
    CheckType,
    CheckSeverity,
    CheckIssue,
)
from .tracing import (
    ExecutionTracer,
    ExecutionTrace,
    TurnTrace,
    ToolTrace,
    TraceEvent,
    TraceEventType,
    TaskOutcome,
)

__all__ = [
    # Core
    "OrchestrationEngine",
    "OrchestrationConfig",
    "OrchestrationEvent",
    "TaskResult",
    "EventType",
    # Sub-agents
    "SubAgentCoordinator",
    "SubAgent",
    "SubAgentResult",
    "SubAgentConfig",
    "SubAgentStatus",
    "create_sub_agent_coordinator",
    # v4: Parallel execution
    "ParallelToolExecutor",
    "ToolCall",
    "ParallelExecutionResult",
    "ExecutionBatch",
    # v4: Retry
    "RetryableOperation",
    "RetryConfig",
    "RetryResult",
    "RetryAttempt",
    "retry_on_failure",
    "RetryContext",
    # v4: Council
    "CouncilIntegration",
    "CouncilResult",
    "detect_language",
    # v4: Pre-flight
    "PreflightChecker",
    "PreflightResult",
    "CheckType",
    "CheckSeverity",
    "CheckIssue",
    # v4: Tracing
    "ExecutionTracer",
    "ExecutionTrace",
    "TurnTrace",
    "ToolTrace",
    "TraceEvent",
    "TraceEventType",
    "TaskOutcome",
]
