"""Integration tests for Council system."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from apps.server.src.planner.orchestrator import (
    TaskOrchestrator,
    TaskContext,
    TaskState,
    OrchestratorConfig,
)
from apps.server.src.planner.router import (
    TaskRouter,
    ExecutionStrategy,
    ExecutionPlan,
    ModelTier,
)
from apps.server.src.planner.analyzer import (
    TaskAnalyzer,
    TaskAnalysis,
    TaskType,
    Complexity,
    RiskLevel,
)
from apps.server.src.planner.council.executor import (
    CouncilExecutor,
    CouncilExecutionRequest,
    CouncilExecutionResult,
)


class TestCouncilIntegration:
    """Test council integration with orchestrator."""

    @pytest.fixture
    def mock_redis(self):
        """Create mock Redis client."""
        redis = MagicMock()
        redis.hset = AsyncMock()
        redis.delete = AsyncMock()
        return redis

    @pytest.fixture
    def mock_task_repo(self):
        """Create mock task repository."""
        repo = MagicMock()
        repo.create = AsyncMock()
        repo.update = AsyncMock()
        repo.get = AsyncMock(return_value=None)
        return repo

    @pytest.fixture
    def mock_session_repo(self):
        """Create mock session repository."""
        repo = MagicMock()
        repo.get = AsyncMock(return_value=MagicMock(active_task_id=None))
        repo.update = AsyncMock()
        return repo

    @pytest.fixture
    def mock_plan_compiler(self):
        """Create mock plan compiler."""
        compiler = MagicMock()
        compiler.compile = AsyncMock(return_value=MagicMock(plan_id="plan_123"))
        return compiler

    @pytest.fixture
    def mock_memory_manager(self):
        """Create mock memory manager."""
        memory = MagicMock()
        memory.get_user_preferences = AsyncMock(return_value={})
        memory.record_task_completion = AsyncMock()
        return memory

    @pytest.fixture
    def mock_council_executor(self):
        """Create mock council executor."""
        executor = MagicMock(spec=CouncilExecutor)
        return executor

    @pytest.mark.asyncio
    async def test_council_strategy_triggers_council_execution(
        self,
        mock_redis,
        mock_task_repo,
        mock_session_repo,
        mock_plan_compiler,
        mock_memory_manager,
        mock_council_executor,
    ):
        """Test that COUNCIL strategy routes to council executor."""
        # Create task router that returns COUNCIL strategy
        analyzer = TaskAnalyzer()
        router = TaskRouter(analyzer=analyzer)

        # Create orchestrator with council executor
        orchestrator = TaskOrchestrator(
            redis=mock_redis,
            task_repo=mock_task_repo,
            session_repo=mock_session_repo,
            plan_compiler=mock_plan_compiler,
            task_router=router,
            memory_manager=mock_memory_manager,
            council_executor=mock_council_executor,
        )

        # Track emitted events
        emitted_events = []

        async def track_emit(event, *args, **kwargs):
            emitted_events.append(event)

        orchestrator._emit = track_emit

        # Mock council executor to return approved result
        mock_council_result = MagicMock()
        mock_council_result.approved = True
        mock_council_result.blocking_reasons = []
        mock_council_executor.execute = AsyncMock(return_value=mock_council_result)

        # Submit a high-risk task that should trigger COUNCIL strategy
        context = TaskContext(
            task_id="task_123",
            session_id="sess_123",
            user_id="user_123",
            org_id=None,
            prompt="Implement authentication with password hashing",
            workspace_context={"project_type": "python"},
        )

        # Run orchestration directly
        await orchestrator._orchestrate_task(context)

        # Verify council executor was called
        mock_council_executor.execute.assert_called_once()

        # Verify council_result event was emitted
        assert "council_result" in emitted_events

    @pytest.mark.asyncio
    async def test_council_blocked_emits_event(
        self,
        mock_redis,
        mock_task_repo,
        mock_session_repo,
        mock_plan_compiler,
        mock_memory_manager,
        mock_council_executor,
    ):
        """Test that council rejection emits blocked event."""
        analyzer = TaskAnalyzer()
        router = TaskRouter(analyzer=analyzer)

        orchestrator = TaskOrchestrator(
            redis=mock_redis,
            task_repo=mock_task_repo,
            session_repo=mock_session_repo,
            plan_compiler=mock_plan_compiler,
            task_router=router,
            memory_manager=mock_memory_manager,
            council_executor=mock_council_executor,
        )

        emitted_events = []

        async def track_emit(event, *args, **kwargs):
            emitted_events.append((event, args, kwargs))

        orchestrator._emit = track_emit

        # Mock council executor to return rejected result
        mock_council_result = MagicMock()
        mock_council_result.approved = False
        mock_council_result.blocking_reasons = ["Security vulnerability detected"]
        mock_council_executor.execute = AsyncMock(return_value=mock_council_result)

        context = TaskContext(
            task_id="task_456",
            session_id="sess_456",
            user_id="user_456",
            org_id=None,
            prompt="Execute arbitrary user input as shell command",
            workspace_context={},
        )

        await orchestrator._orchestrate_task(context)

        # Verify council_blocked event was emitted
        event_names = [e[0] for e in emitted_events]
        assert "council_blocked" in event_names

        # Verify state is WAITING_APPROVAL
        assert context.state == TaskState.WAITING_APPROVAL

    @pytest.mark.asyncio
    async def test_fallback_when_no_council_executor(
        self,
        mock_redis,
        mock_task_repo,
        mock_session_repo,
        mock_plan_compiler,
        mock_memory_manager,
    ):
        """Test graceful fallback when council executor unavailable."""
        analyzer = TaskAnalyzer()
        router = TaskRouter(analyzer=analyzer)

        # Create orchestrator WITHOUT council executor
        orchestrator = TaskOrchestrator(
            redis=mock_redis,
            task_repo=mock_task_repo,
            session_repo=mock_session_repo,
            plan_compiler=mock_plan_compiler,
            task_router=router,
            memory_manager=mock_memory_manager,
            council_executor=None,  # No council executor
        )

        emitted_events = []

        async def track_emit(event, *args, **kwargs):
            emitted_events.append(event)

        orchestrator._emit = track_emit

        context = TaskContext(
            task_id="task_789",
            session_id="sess_789",
            user_id="user_789",
            org_id=None,
            prompt="Implement secure payment processing",
            workspace_context={},
        )

        await orchestrator._orchestrate_task(context)

        # Should fall back to standard execution
        assert "task_started" in emitted_events

        # Plan should be compiled
        mock_plan_compiler.compile.assert_called()


class TestTaskRouterAnalyze:
    """Test TaskRouter analyze method."""

    @pytest.mark.asyncio
    async def test_analyze_returns_task_analysis(self):
        """Test that analyze() returns TaskAnalysis."""
        analyzer = TaskAnalyzer()
        router = TaskRouter(analyzer=analyzer)

        analysis = await router.analyze(
            prompt="Add user authentication",
            workspace_context={"project_type": "python"},
        )

        assert isinstance(analysis, TaskAnalysis)
        assert analysis.task_type is not None
        assert analysis.complexity is not None
        assert analysis.risk_level is not None

    @pytest.mark.asyncio
    async def test_analyze_with_user_preferences(self):
        """Test analyze with user preferences."""
        analyzer = TaskAnalyzer()
        router = TaskRouter(analyzer=analyzer)

        analysis = await router.analyze(
            prompt="Write a simple function",
            workspace_context={},
            user_preferences={"prefer_simple": True},
        )

        assert isinstance(analysis, TaskAnalysis)

    @pytest.mark.asyncio
    async def test_analyze_security_task(self):
        """Test that security tasks are detected."""
        analyzer = TaskAnalyzer()
        router = TaskRouter(analyzer=analyzer)

        analysis = await router.analyze(
            prompt="Fix SQL injection vulnerability in login form",
            workspace_context={},
        )

        # Security tasks should be detected
        assert analysis.task_type == TaskType.SECURITY or analysis.risk_level in (
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        )


class TestCouncilExecutionRequest:
    """Test CouncilExecutionRequest dataclass."""

    def test_create_request(self):
        """Test creating a council execution request."""
        request = CouncilExecutionRequest(
            task_id="task_123",
            session_id="sess_123",
            user_id="user_123",
            task_description="Add authentication",
            proposed_code="def authenticate(): pass",
            context={"files": ["auth.py"]},
            council_perspectives=["security", "correctness"],
        )

        assert request.task_id == "task_123"
        assert request.task_description == "Add authentication"
        assert len(request.council_perspectives) == 2

    def test_optional_analysis(self):
        """Test that analysis is optional."""
        request = CouncilExecutionRequest(
            task_id="task_456",
            session_id="sess_456",
            user_id="user_456",
            task_description="Simple task",
            proposed_code="x = 1",
            context={},
        )

        assert request.analysis is None
        assert request.council_perspectives is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
