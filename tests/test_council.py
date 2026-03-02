"""Tests for LLM Council system."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from apps.server.src.planner.council import (
    CouncilOrchestrator,
    CouncilConfig,
    CouncilStrategy,
    CouncilBuilder,
    create_standard_council,
    create_fast_council,
    create_thorough_council,
    QualityGate,
    SynthesisEngine,
)
from apps.server.src.planner.council.perspectives import (
    PerspectiveType,
    CorrectnessChecker,
    SecurityReviewer,
    SimplicityAdvocate,
    get_perspective,
    get_all_perspectives,
)


class TestPerspectives:
    """Test perspective classes."""

    def test_get_perspective(self):
        """Test perspective factory."""
        checker = get_perspective("correctness")
        assert checker.perspective_type == PerspectiveType.CORRECTNESS

        reviewer = get_perspective("security")
        assert reviewer.perspective_type == PerspectiveType.SECURITY

    def test_get_all_perspectives(self):
        """Test getting all perspectives."""
        perspectives = get_all_perspectives()
        assert len(perspectives) >= 8
        types = [p.perspective_type for p in perspectives]
        assert PerspectiveType.CORRECTNESS in types
        assert PerspectiveType.SECURITY in types

    def test_correctness_checker_prompts(self):
        """Test correctness checker generates proper prompts."""
        checker = CorrectnessChecker()

        system = checker.get_system_prompt()
        assert "Correctness Checker" in system
        assert "[APPROVED]" in system
        assert "[REJECTED]" in system

        analysis = checker.get_analysis_prompt(
            task="Add a function to sum numbers",
            proposed_code="def sum_numbers(a, b): return a + b",
            context={"files": "existing code here"},
        )
        assert "Add a function" in analysis
        assert "def sum_numbers" in analysis

    def test_security_reviewer_prompts(self):
        """Test security reviewer generates proper prompts."""
        reviewer = SecurityReviewer()

        system = reviewer.get_system_prompt()
        assert "Security Reviewer" in system
        assert "OWASP" in system or "injection" in system.lower()

    def test_parse_response_approved(self):
        """Test parsing approved response."""
        checker = CorrectnessChecker()

        response = """
        **[APPROVED]**
        **Confidence: 85%**

        The code correctly implements the sum function.
        **INFO**: Consider adding type hints.
        """

        result = checker.parse_response(response)
        assert result.approved is True
        assert result.confidence == 0.85

    def test_parse_response_rejected(self):
        """Test parsing rejected response."""
        checker = CorrectnessChecker()

        response = """
        **[REJECTED]**
        **Confidence: 90%**

        **ERROR**: Function does not handle edge cases.
        The code fails when inputs are None.
        """

        result = checker.parse_response(response)
        assert result.approved is False
        assert result.confidence == 0.90


class TestQualityGate:
    """Test quality gate functionality."""

    def test_evaluate_simple_python(self):
        """Test evaluating simple Python code."""
        gate = QualityGate()

        code = '''
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b
'''
        score = gate.evaluate(code, "python")
        assert score.overall > 60
        assert score.passed is True

    def test_evaluate_complex_code(self):
        """Test evaluating complex code."""
        gate = QualityGate(max_complexity=5)

        # Code with high complexity
        code = '''
def complex_function(x):
    if x > 0:
        if x > 10:
            if x > 100:
                if x > 1000:
                    return "huge"
                return "large"
            return "medium"
        return "small"
    return "zero or negative"
'''
        score = gate.evaluate(code, "python")
        # Should detect deep nesting (complexity is 5, nesting is 4)
        assert score.metrics.complexity.max_nesting_depth == 4
        assert score.metrics.complexity.cyclomatic_complexity == 5

    def test_detect_security_issues(self):
        """Test security issue detection."""
        gate = QualityGate()

        # Code with security issues
        code = '''
import os

def run_command(cmd):
    os.system(cmd)  # Command injection risk

password = "hardcoded123"  # Hardcoded credential
'''
        score = gate.evaluate(code, "python")
        assert score.security_score < 100
        assert score.metrics.security.command_injection_risks > 0

    def test_quality_levels(self):
        """Test quality level classification."""
        gate = QualityGate()

        excellent_code = '''
def calculate_total(items: list[float]) -> float:
    """Calculate total of items.

    Args:
        items: List of numeric values

    Returns:
        Sum of all items
    """
    if not items:
        return 0.0
    return sum(items)
'''
        score = gate.evaluate(excellent_code, "python")
        # Good code should score well
        assert score.overall >= 60


class TestSynthesis:
    """Test synthesis engine."""

    def test_synthesize_unanimous_approval(self):
        """Test synthesis with unanimous approval."""
        from apps.server.src.planner.council.perspectives import PerspectiveResult

        engine = SynthesisEngine()

        results = [
            PerspectiveResult(
                perspective_type=PerspectiveType.CORRECTNESS,
                approved=True,
                confidence=0.9,
                reasoning="Code is correct",
            ),
            PerspectiveResult(
                perspective_type=PerspectiveType.SECURITY,
                approved=True,
                confidence=0.85,
                reasoning="No security issues",
            ),
        ]

        perspectives = [CorrectnessChecker(), SecurityReviewer()]

        consensus = engine.synthesize(results, perspectives)
        assert consensus.approved is True
        assert len(consensus.dissenting_perspectives) == 0

    def test_synthesize_with_rejection(self):
        """Test synthesis with security rejection."""
        from apps.server.src.planner.council.perspectives import PerspectiveResult, Issue, Severity

        engine = SynthesisEngine(require_security_approval=True)

        results = [
            PerspectiveResult(
                perspective_type=PerspectiveType.CORRECTNESS,
                approved=True,
                confidence=0.9,
                reasoning="Code is correct",
            ),
            PerspectiveResult(
                perspective_type=PerspectiveType.SECURITY,
                approved=False,
                confidence=0.95,
                issues=[Issue(
                    severity=Severity.CRITICAL,
                    category="security",
                    message="SQL injection vulnerability",
                )],
                reasoning="Security vulnerability found",
            ),
        ]

        perspectives = [CorrectnessChecker(), SecurityReviewer()]

        consensus = engine.synthesize(results, perspectives)
        assert consensus.approved is False  # Security rejection blocks


class TestCouncilOrchestrator:
    """Test council orchestrator."""

    def test_create_standard_council(self):
        """Test creating standard council."""
        council = create_standard_council()
        assert isinstance(council, CouncilOrchestrator)
        assert len(council._perspectives) == 4

    def test_create_fast_council(self):
        """Test creating fast council."""
        council = create_fast_council()
        assert isinstance(council, CouncilOrchestrator)
        assert len(council._perspectives) == 2

    def test_council_builder(self):
        """Test council builder pattern."""
        council = CouncilBuilder()\
            .with_perspectives("correctness", "security", "simplicity")\
            .with_strategy(CouncilStrategy.TIERED)\
            .with_quality_gate(True, 70.0)\
            .with_timeouts(20.0, 60.0)\
            .build()

        assert isinstance(council, CouncilOrchestrator)
        assert len(council._perspectives) == 3
        assert council.config.strategy == CouncilStrategy.TIERED
        assert council.config.quality_gate_min_score == 70.0

    @pytest.mark.asyncio
    async def test_deliberate_with_mock(self):
        """Test deliberation with mock LLM."""
        council = create_fast_council()

        # Mock LLM callback
        async def mock_llm(system: str, prompt: str, tier: str) -> str:
            return """
            **[APPROVED]**
            **Confidence: 85%**

            The code looks correct and secure.
            """

        result = await council.deliberate(
            task="Add a sum function",
            proposed_code="def sum(a, b): return a + b",
            context={},
            llm_callback=mock_llm,
        )

        assert result.perspectives_completed == 2
        assert result.execution_time_ms >= 0  # Can be 0 for fast mocks


class TestIntegration:
    """Integration tests."""

    @pytest.mark.asyncio
    async def test_full_council_flow(self):
        """Test full council deliberation flow."""
        # Create thorough council
        council = create_thorough_council()

        # Good code that should pass
        good_code = r'''
def validate_email(email: str) -> bool:
    """Validate email format.

    Args:
        email: Email address to validate

    Returns:
        True if valid, False otherwise
    """
    import re
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))
'''

        # Mock LLM that approves
        async def mock_llm(system: str, prompt: str, tier: str) -> str:
            return """
            **[APPROVED]**
            **Confidence: 90%**

            The code is well-written with proper validation.
            """

        result = await council.deliberate(
            task="Add email validation function",
            proposed_code=good_code,
            context={"language": "python"},
            llm_callback=mock_llm,
        )

        # Should complete with results
        assert result.perspectives_completed > 0
        assert result.consensus is not None

        # Quality gate should pass for good code
        if result.quality_score:
            assert result.quality_score.overall > 50


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
