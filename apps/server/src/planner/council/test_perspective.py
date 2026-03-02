"""Test Generation Perspective for Council.

Generates tests as part of council review to validate
that proposed code actually works correctly.

This is a critical component for one-shot code quality:
- Happy path tests
- Edge case tests
- Error handling tests
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .perspectives import (
    Perspective,
    PerspectiveResult,
    PerspectiveType,
    Issue,
    Severity,
)
from .schema import (
    create_test_generation_prompt,
    parse_test_output,
    StructuredTestOutput,
)

logger = logging.getLogger(__name__)


class TestGeneratorPerspective(Perspective):
    """Generates tests to validate proposed code.

    This perspective:
    1. Analyzes the proposed code
    2. Generates comprehensive tests
    3. Identifies coverage gaps
    4. Returns test code for execution

    The generated tests can be executed by the TestExecutor
    to verify the code actually works.
    """

    perspective_type = PerspectiveType.TESTING
    weight = 1.3  # High weight - tests are critical for one-shot

    def __init__(
        self,
        model_tier: str = "standard",
        target_test_count: int = 5,
        include_edge_cases: bool = True,
        include_error_handling: bool = True,
    ):
        """Initialize test generator.

        Args:
            model_tier: Model tier for generation
            target_test_count: Target number of tests to generate
            include_edge_cases: Generate edge case tests
            include_error_handling: Generate error handling tests
        """
        super().__init__(model_tier)
        self.target_test_count = target_test_count
        self.include_edge_cases = include_edge_cases
        self.include_error_handling = include_error_handling

    def get_system_prompt(self) -> str:
        return """You are a Test Generator in an LLM Council. Your role is to generate comprehensive tests for proposed code.

## Your Mission

Generate tests that:
1. **Verify correctness** - Test the happy path
2. **Cover edge cases** - Test boundary conditions
3. **Handle errors** - Test error scenarios
4. **Are executable** - Tests should run without modification

## Test Categories

1. **Unit Tests**
   - Test individual functions/methods
   - Mock external dependencies
   - Fast execution

2. **Integration Tests** (when applicable)
   - Test component interactions
   - Use realistic inputs

## Guidelines

- Use pytest conventions
- Include clear test names (test_<what>_<condition>_<expectation>)
- Include docstrings explaining what's tested
- Use fixtures for common setup
- Assert specific values, not just truthy

## Response Format

Respond with a JSON object:
```json
{
    "decision": "APPROVED",
    "confidence": 0.85,
    "test_code": "import pytest\\n...",
    "test_count": 5,
    "coverage_areas": ["happy_path", "edge_cases", "error_handling"],
    "uncovered_cases": ["network_timeout"],
    "reasoning": "Generated 5 tests covering main functionality..."
}
```

IMPORTANT: The test_code should be complete, runnable pytest code."""

    def get_analysis_prompt(
        self,
        task: str,
        proposed_code: str,
        context: dict[str, Any],
    ) -> str:
        language = context.get("language", "python")
        file_path = context.get("file_path", "unknown")

        prompt = f"""## Task
{task}

## Proposed Code
```{language}
{proposed_code}
```

## File Path
{file_path}

## Requirements

Generate {self.target_test_count} or more tests that:
1. Test the main functionality (happy path)
2. Test edge cases (empty inputs, boundary values)
3. Test error handling (invalid inputs, failures)
4. Are complete and runnable with pytest

## Test Generation Guidelines

- Import necessary modules
- Use appropriate fixtures
- Test both success and failure cases
- Include assertions with clear messages
- Handle async functions if needed

Generate comprehensive tests now."""

        return create_test_generation_prompt(prompt)

    def parse_response(self, response: str) -> PerspectiveResult:
        """Parse test generation response."""
        test_output = parse_test_output(response)

        # Build issues based on coverage
        issues = []
        suggestions = []

        if test_output.test_count < self.target_test_count:
            issues.append(Issue(
                severity=Severity.WARNING,
                category="test_coverage",
                message=f"Generated {test_output.test_count} tests, "
                       f"target was {self.target_test_count}",
            ))

        if test_output.uncovered_cases:
            for case in test_output.uncovered_cases:
                suggestions.append(f"Consider adding test for: {case}")

        if not test_output.test_code:
            issues.append(Issue(
                severity=Severity.ERROR,
                category="test_generation",
                message="No test code was generated",
            ))

        # Store test code in metadata for execution
        return PerspectiveResult(
            perspective_type=self.perspective_type,
            approved=test_output.decision == "APPROVED",
            confidence=test_output.confidence,
            issues=issues,
            suggestions=suggestions,
            reasoning=test_output.reasoning,
            code_modifications=test_output.test_code,  # Test code stored here
            metadata={
                "test_code": test_output.test_code,
                "test_count": test_output.test_count,
                "coverage_areas": test_output.coverage_areas,
                "uncovered_cases": test_output.uncovered_cases,
            },
        )


class TestValidationPerspective(Perspective):
    """Validates that proposed code is testable.

    This perspective checks:
    1. Code structure allows testing
    2. Dependencies are mockable
    3. Functions have clear inputs/outputs
    """

    perspective_type = PerspectiveType.TESTING
    weight = 0.8

    def get_system_prompt(self) -> str:
        return """You are a Test Validation expert in an LLM Council. Your role is to assess code testability.

## Check For

1. **Testable Structure**
   - Functions have clear inputs and outputs
   - Side effects are isolated
   - Dependencies can be injected/mocked

2. **Test Barriers**
   - Global state
   - Tight coupling
   - Hidden dependencies
   - Complex setup requirements

3. **Mockability**
   - External service calls
   - Database access
   - File system operations

## Response Format

**[APPROVED]** or **[REJECTED]**
**Confidence: XX%**

List issues as:
**ERROR**: Hard to test - [reason]
**WARNING**: Testing concern - [issue]
**INFO**: Testability improvement - [suggestion]

APPROVE if code is reasonably testable with standard techniques."""

    def get_analysis_prompt(
        self,
        task: str,
        proposed_code: str,
        context: dict[str, Any],
    ) -> str:
        return f"""## Task
{task}

## Proposed Code
```
{proposed_code}
```

## Testability Analysis

Evaluate:
1. Can this code be unit tested easily?
2. Are there external dependencies to mock?
3. Are there any testing anti-patterns?
4. What setup would tests require?
5. Is there tight coupling that prevents testing?

Provide testability assessment and verdict."""


# Register the test perspectives
def register_test_perspectives():
    """Register test perspectives in the global registry."""
    from .perspectives import PERSPECTIVE_REGISTRY

    PERSPECTIVE_REGISTRY["testing"] = TestGeneratorPerspective
    PERSPECTIVE_REGISTRY["test_generator"] = TestGeneratorPerspective
    PERSPECTIVE_REGISTRY["test_validation"] = TestValidationPerspective


# Auto-register on import
try:
    register_test_perspectives()
except ImportError:
    pass  # perspectives module not available
