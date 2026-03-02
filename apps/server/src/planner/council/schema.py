"""Structured Output Schema for Council Perspectives.

Provides Pydantic models for structured LLM responses and utilities
for creating structured prompts that encourage JSON output.

This replaces fragile regex parsing with reliable JSON parsing,
falling back to heuristics only when necessary.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Optional, Union

try:
    from pydantic import BaseModel, Field, field_validator
    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False
    # Fallback for when pydantic is not installed
    BaseModel = object
    Field = lambda *args, **kwargs: None
    field_validator = lambda *args, **kwargs: lambda f: f

logger = logging.getLogger(__name__)


# ============================================================================
# Structured Output Models
# ============================================================================

class Decision(str, Enum):
    """Council decision types."""
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class IssueSeverity(str, Enum):
    """Severity levels for issues."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


if PYDANTIC_AVAILABLE:

    class StructuredIssue(BaseModel):
        """A structured issue identified by a perspective."""
        severity: IssueSeverity
        category: str = Field(description="Issue category (e.g., 'security', 'logic')")
        message: str = Field(description="Description of the issue")
        location: Optional[str] = Field(default=None, description="Location in code")
        suggestion: Optional[str] = Field(default=None, description="How to fix")

    class StructuredPerspectiveOutput(BaseModel):
        """Structured output from a council perspective."""
        decision: Literal["APPROVED", "REJECTED"]
        confidence: float = Field(ge=0.0, le=1.0, description="Confidence 0.0-1.0")
        issues: list[StructuredIssue] = Field(default_factory=list)
        suggestions: list[str] = Field(default_factory=list)
        reasoning: str = Field(description="Explanation of decision")

        @field_validator("confidence", mode="before")
        @classmethod
        def normalize_confidence(cls, v: Any) -> float:
            """Normalize confidence to 0.0-1.0 range."""
            if isinstance(v, (int, float)):
                if v > 1.0:
                    return v / 100.0
                return float(v)
            return 0.7

    class StructuredTestOutput(BaseModel):
        """Structured output from test generation perspective."""
        decision: Literal["APPROVED", "REJECTED"]
        confidence: float = Field(ge=0.0, le=1.0)
        test_code: str = Field(description="Generated test code")
        test_count: int = Field(description="Number of tests generated")
        coverage_areas: list[str] = Field(default_factory=list)
        uncovered_cases: list[str] = Field(default_factory=list)
        reasoning: str = ""

else:
    # Fallback dataclasses when pydantic is not available

    @dataclass
    class StructuredIssue:
        severity: str
        category: str
        message: str
        location: Optional[str] = None
        suggestion: Optional[str] = None

    @dataclass
    class StructuredPerspectiveOutput:
        decision: str
        confidence: float
        issues: list = None
        suggestions: list = None
        reasoning: str = ""

        def __post_init__(self):
            if self.issues is None:
                self.issues = []
            if self.suggestions is None:
                self.suggestions = []

    @dataclass
    class StructuredTestOutput:
        decision: str
        confidence: float
        test_code: str
        test_count: int
        coverage_areas: list = None
        uncovered_cases: list = None
        reasoning: str = ""

        def __post_init__(self):
            if self.coverage_areas is None:
                self.coverage_areas = []
            if self.uncovered_cases is None:
                self.uncovered_cases = []


# ============================================================================
# JSON Schema Generation
# ============================================================================

PERSPECTIVE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["APPROVED", "REJECTED"]
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence level from 0.0 to 1.0"
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["info", "warning", "error", "critical"]},
                    "category": {"type": "string"},
                    "message": {"type": "string"},
                    "location": {"type": "string"},
                    "suggestion": {"type": "string"}
                },
                "required": ["severity", "category", "message"]
            }
        },
        "suggestions": {
            "type": "array",
            "items": {"type": "string"}
        },
        "reasoning": {
            "type": "string",
            "description": "Explanation of the decision"
        }
    },
    "required": ["decision", "confidence", "reasoning"]
}


# ============================================================================
# Structured Prompt Creation
# ============================================================================

def create_structured_prompt(base_prompt: str, include_schema: bool = True) -> str:
    """Add JSON schema requirement to a prompt.

    Args:
        base_prompt: The original perspective prompt
        include_schema: Whether to include the full JSON schema

    Returns:
        Enhanced prompt with JSON output requirements
    """
    schema_section = ""
    if include_schema:
        schema_section = f"""
## JSON Schema
```json
{json.dumps(PERSPECTIVE_JSON_SCHEMA, indent=2)}
```
"""

    return f"""{base_prompt}

## RESPONSE FORMAT (REQUIRED)

You MUST respond with ONLY a valid JSON object. No other text before or after.
{schema_section}
## Example Response

```json
{{
    "decision": "APPROVED",
    "confidence": 0.85,
    "issues": [
        {{
            "severity": "warning",
            "category": "style",
            "message": "Consider using more descriptive variable names",
            "location": "line 15",
            "suggestion": "Rename 'x' to 'user_count'"
        }}
    ],
    "suggestions": [
        "Add input validation for edge cases"
    ],
    "reasoning": "The code correctly implements the required functionality with minor style improvements suggested."
}}
```

IMPORTANT: Return ONLY the JSON object. No markdown, no explanation, just the JSON.
"""


def create_test_generation_prompt(base_prompt: str) -> str:
    """Create a structured prompt for test generation.

    Args:
        base_prompt: The original test generation prompt

    Returns:
        Enhanced prompt for structured test output
    """
    return f"""{base_prompt}

## RESPONSE FORMAT (REQUIRED)

You MUST respond with ONLY a valid JSON object containing generated tests.

## JSON Schema

```json
{{
    "decision": "APPROVED or REJECTED",
    "confidence": 0.0-1.0,
    "test_code": "Complete test code as a string",
    "test_count": number_of_tests,
    "coverage_areas": ["list", "of", "tested", "areas"],
    "uncovered_cases": ["edge", "cases", "not", "covered"],
    "reasoning": "Explanation of test coverage"
}}
```

IMPORTANT: Return ONLY the JSON object. No markdown, no explanation, just the JSON.
"""


# ============================================================================
# Response Parsing
# ============================================================================

def extract_json_from_response(response: str) -> Optional[dict[str, Any]]:
    """Extract JSON from an LLM response.

    Handles various formats:
    - Pure JSON response
    - JSON in markdown code blocks
    - JSON embedded in text

    Args:
        response: The raw LLM response

    Returns:
        Parsed JSON dict or None if parsing fails
    """
    # Clean the response
    response = response.strip()

    # Try direct parse first
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code blocks
    code_block_patterns = [
        r"```json\s*\n?(.*?)\n?```",
        r"```\s*\n?(.*?)\n?```",
    ]

    for pattern in code_block_patterns:
        match = re.search(pattern, response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                continue

    # Try finding JSON object in the response
    json_object_pattern = r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}"
    matches = re.findall(json_object_pattern, response, re.DOTALL)

    for match in matches:
        try:
            return json.loads(match)
        except json.JSONDecodeError:
            continue

    return None


def parse_structured_response(
    response: str,
    perspective_type: str = "unknown",
) -> StructuredPerspectiveOutput:
    """Parse an LLM response into a structured output.

    Tries JSON parsing first, falls back to heuristic parsing.

    Args:
        response: The LLM response text
        perspective_type: Type of perspective for better error messages

    Returns:
        StructuredPerspectiveOutput
    """
    # Try JSON extraction
    json_data = extract_json_from_response(response)

    if json_data:
        try:
            if PYDANTIC_AVAILABLE:
                return StructuredPerspectiveOutput(**json_data)
            else:
                # Manual construction for fallback
                issues = []
                for issue_data in json_data.get("issues", []):
                    issues.append(StructuredIssue(**issue_data))

                return StructuredPerspectiveOutput(
                    decision=json_data.get("decision", "APPROVED"),
                    confidence=json_data.get("confidence", 0.7),
                    issues=issues,
                    suggestions=json_data.get("suggestions", []),
                    reasoning=json_data.get("reasoning", ""),
                )
        except Exception as e:
            logger.warning(f"Failed to parse structured JSON for {perspective_type}: {e}")

    # Fall back to heuristic parsing
    return _heuristic_parse(response, perspective_type)


def _heuristic_parse(
    response: str,
    perspective_type: str,
) -> StructuredPerspectiveOutput:
    """Fallback heuristic parsing when JSON fails.

    Args:
        response: The LLM response text
        perspective_type: Type of perspective

    Returns:
        StructuredPerspectiveOutput extracted via heuristics
    """
    response_lower = response.lower()

    # Extract decision
    if "[approved]" in response_lower or "**approved**" in response_lower:
        decision = "APPROVED"
    elif "[rejected]" in response_lower or "**rejected**" in response_lower:
        decision = "REJECTED"
    else:
        # Count indicators
        positive = sum(1 for w in ["approve", "accept", "pass", "good"]
                       if w in response_lower)
        negative = sum(1 for w in ["reject", "deny", "fail", "error", "critical"]
                       if w in response_lower)
        decision = "APPROVED" if positive >= negative else "REJECTED"

    # Extract confidence
    confidence = 0.7
    confidence_match = re.search(r"confidence[:\s]+(\d+(?:\.\d+)?)[%]?", response_lower)
    if confidence_match:
        conf_val = float(confidence_match.group(1))
        confidence = conf_val / 100 if conf_val > 1 else conf_val

    # Extract issues
    issues = []
    severity_patterns = [
        (IssueSeverity.CRITICAL, r"\*\*critical\*\*[:\s]*(.+?)(?:\n|$)"),
        (IssueSeverity.ERROR, r"\*\*error\*\*[:\s]*(.+?)(?:\n|$)"),
        (IssueSeverity.WARNING, r"\*\*warning\*\*[:\s]*(.+?)(?:\n|$)"),
        (IssueSeverity.INFO, r"\*\*info\*\*[:\s]*(.+?)(?:\n|$)"),
    ]

    for severity, pattern in severity_patterns:
        for match in re.finditer(pattern, response, re.IGNORECASE):
            if PYDANTIC_AVAILABLE:
                issues.append(StructuredIssue(
                    severity=severity,
                    category=perspective_type,
                    message=match.group(1).strip(),
                ))
            else:
                issues.append(StructuredIssue(
                    severity=severity.value,
                    category=perspective_type,
                    message=match.group(1).strip(),
                ))

    # Extract suggestions
    suggestions = []
    suggestion_patterns = [
        r"\*\*suggestion\*\*[:\s]*(.+?)(?:\n|$)",
        r"- suggest(?:ion)?[:\s]*(.+?)(?:\n|$)",
        r"recommend[:\s]*(.+?)(?:\n|$)",
    ]

    for pattern in suggestion_patterns:
        for match in re.finditer(pattern, response, re.IGNORECASE):
            suggestions.append(match.group(1).strip())

    return StructuredPerspectiveOutput(
        decision=decision,
        confidence=confidence,
        issues=issues,
        suggestions=suggestions,
        reasoning=response,
    )


def parse_test_output(response: str) -> StructuredTestOutput:
    """Parse test generation output.

    Args:
        response: The LLM response text

    Returns:
        StructuredTestOutput
    """
    # Try JSON extraction
    json_data = extract_json_from_response(response)

    if json_data:
        try:
            if PYDANTIC_AVAILABLE:
                return StructuredTestOutput(**json_data)
            else:
                return StructuredTestOutput(
                    decision=json_data.get("decision", "APPROVED"),
                    confidence=json_data.get("confidence", 0.7),
                    test_code=json_data.get("test_code", ""),
                    test_count=json_data.get("test_count", 0),
                    coverage_areas=json_data.get("coverage_areas", []),
                    uncovered_cases=json_data.get("uncovered_cases", []),
                    reasoning=json_data.get("reasoning", ""),
                )
        except Exception as e:
            logger.warning(f"Failed to parse test output JSON: {e}")

    # Fallback: extract test code from code blocks
    test_code = ""
    code_match = re.search(r"```(?:python)?\s*\n?(.*?)\n?```", response, re.DOTALL)
    if code_match:
        test_code = code_match.group(1).strip()

    # Count test functions
    test_count = len(re.findall(r"def test_", test_code))

    return StructuredTestOutput(
        decision="APPROVED" if test_code else "REJECTED",
        confidence=0.7 if test_code else 0.3,
        test_code=test_code,
        test_count=test_count,
        coverage_areas=[],
        uncovered_cases=[],
        reasoning=response,
    )
