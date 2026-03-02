"""Council Perspectives - Specialized viewpoints for code analysis.

Each perspective represents a specific concern or expertise area
that evaluates proposed code changes from its unique angle.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PerspectiveType(str, Enum):
    """Types of council perspectives."""
    CORRECTNESS = "correctness"
    SECURITY = "security"
    SIMPLICITY = "simplicity"
    EDGE_CASES = "edge_cases"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    DATA_INTEGRITY = "data_integrity"
    ATTACK_SURFACE = "attack_surface"
    TESTING = "testing"
    ARCHITECTURE = "architecture"


class Severity(str, Enum):
    """Severity of identified issues."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class Issue:
    """An issue identified by a perspective."""
    severity: Severity
    category: str
    message: str
    location: Optional[str] = None  # file:line or general area
    suggestion: Optional[str] = None
    confidence: float = 0.8


@dataclass
class PerspectiveResult:
    """Result from a single perspective's analysis."""
    perspective_type: PerspectiveType
    approved: bool
    confidence: float
    issues: list[Issue] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    reasoning: str = ""
    code_modifications: Optional[str] = None  # Suggested code changes
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def critical_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.CRITICAL]

    @property
    def error_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def has_blocking_issues(self) -> bool:
        return len(self.critical_issues) > 0 or len(self.error_issues) > 0


class Perspective(ABC):
    """Base class for council perspectives.

    Each perspective analyzes proposed changes from a specific viewpoint
    and provides approval/rejection with reasoning.
    """

    perspective_type: PerspectiveType
    weight: float = 1.0  # Voting weight

    def __init__(self, model_tier: str = "standard"):
        self.model_tier = model_tier

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Get the system prompt for this perspective."""
        pass

    @abstractmethod
    def get_analysis_prompt(
        self,
        task: str,
        proposed_code: str,
        context: dict[str, Any],
    ) -> str:
        """Build the analysis prompt for this perspective."""
        pass

    def parse_response(self, response: str) -> PerspectiveResult:
        """Parse LLM response into structured result.

        v4 enhancement: Try JSON parsing first, fallback to heuristics.
        """
        # Try structured JSON parsing first (v4 enhancement)
        try:
            from .schema import parse_structured_response, StructuredPerspectiveOutput
            structured = parse_structured_response(response, self.perspective_type.value)

            # Convert structured output to PerspectiveResult
            issues = []
            for issue in structured.issues:
                issues.append(Issue(
                    severity=Severity(issue.severity.value if hasattr(issue.severity, 'value') else issue.severity),
                    category=self.perspective_type.value,
                    message=issue.message,
                    location=issue.location,
                    suggestion=issue.suggestion,
                ))

            return PerspectiveResult(
                perspective_type=self.perspective_type,
                approved=(structured.decision == "APPROVED"),
                confidence=structured.confidence,
                issues=issues,
                suggestions=structured.suggestions,
                reasoning=structured.reasoning,
            )
        except Exception as e:
            logger.debug(f"Structured parsing failed, using heuristics: {e}")

        # Fallback to heuristic parsing
        approved = self._extract_approval(response)
        confidence = self._extract_confidence(response)
        issues = self._extract_issues(response)
        suggestions = self._extract_suggestions(response)

        return PerspectiveResult(
            perspective_type=self.perspective_type,
            approved=approved,
            confidence=confidence,
            issues=issues,
            suggestions=suggestions,
            reasoning=response,
        )

    def _extract_approval(self, response: str) -> bool:
        """Extract approval decision from response."""
        response_lower = response.lower()
        # Look for explicit approval markers
        if "**approved**" in response_lower or "[approved]" in response_lower:
            return True
        if "**rejected**" in response_lower or "[rejected]" in response_lower:
            return False
        # Heuristic: count positive vs negative indicators
        positive = sum(1 for word in ["approve", "accept", "good", "correct", "pass"]
                       if word in response_lower)
        negative = sum(1 for word in ["reject", "deny", "fail", "issue", "problem", "error"]
                       if word in response_lower)
        return positive >= negative

    def _extract_confidence(self, response: str) -> float:
        """Extract confidence score from response."""
        import re
        # Look for explicit confidence markers
        match = re.search(r"confidence[:\s]+(\d+(?:\.\d+)?)[%]?", response.lower())
        if match:
            score = float(match.group(1))
            return score / 100 if score > 1 else score
        return 0.7  # Default confidence

    def _extract_issues(self, response: str) -> list[Issue]:
        """Extract issues from response."""
        import re
        issues = []

        # Look for severity markers
        patterns = [
            (Severity.CRITICAL, r"\*\*critical\*\*[:\s]*(.+?)(?:\n|$)"),
            (Severity.ERROR, r"\*\*error\*\*[:\s]*(.+?)(?:\n|$)"),
            (Severity.WARNING, r"\*\*warning\*\*[:\s]*(.+?)(?:\n|$)"),
            (Severity.INFO, r"\*\*info\*\*[:\s]*(.+?)(?:\n|$)"),
        ]

        for severity, pattern in patterns:
            for match in re.finditer(pattern, response, re.IGNORECASE):
                issues.append(Issue(
                    severity=severity,
                    category=self.perspective_type.value,
                    message=match.group(1).strip(),
                ))

        return issues

    def _extract_suggestions(self, response: str) -> list[str]:
        """Extract suggestions from response."""
        import re
        suggestions = []

        # Look for suggestion markers
        patterns = [
            r"\*\*suggestion\*\*[:\s]*(.+?)(?:\n|$)",
            r"- suggest(?:ion)?[:\s]*(.+?)(?:\n|$)",
            r"recommend[:\s]*(.+?)(?:\n|$)",
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, response, re.IGNORECASE):
                suggestions.append(match.group(1).strip())

        return suggestions


class CorrectnessChecker(Perspective):
    """Verifies code correctness and functional requirements."""

    perspective_type = PerspectiveType.CORRECTNESS
    weight = 1.5  # Higher weight - correctness is fundamental

    def get_system_prompt(self) -> str:
        return """You are a Correctness Checker in an LLM Council. Your role is to verify that proposed code:

1. **Correctly implements** the requested functionality
2. **Handles all specified requirements**
3. **Has proper logic flow** without bugs
4. **Uses correct types and signatures**
5. **Follows language idioms correctly**

## Response Format

Start with: **[APPROVED]** or **[REJECTED]**
Then provide confidence: **Confidence: XX%**

If issues found, list them as:
**ERROR**: [description]
**WARNING**: [description]

End with suggestions if any.

Be thorough but fair. Approve code that is functionally correct even if style could improve."""

    def get_analysis_prompt(
        self,
        task: str,
        proposed_code: str,
        context: dict[str, Any],
    ) -> str:
        files_context = context.get("files", "")
        return f"""## Task
{task}

## Proposed Code
```
{proposed_code}
```

## Existing Context
{files_context}

## Analysis Required
1. Does the code correctly implement what was requested?
2. Are there any logic errors or bugs?
3. Are all edge cases in the requirements handled?
4. Are types and signatures correct?

Provide your verdict with **[APPROVED]** or **[REJECTED]** and confidence level."""


class SecurityReviewer(Perspective):
    """Reviews code for security vulnerabilities."""

    perspective_type = PerspectiveType.SECURITY
    weight = 1.5  # Security is critical

    def get_system_prompt(self) -> str:
        return """You are a Security Reviewer in an LLM Council. Your role is to identify security vulnerabilities:

## Check For
1. **Injection attacks**: SQL, command, XSS, template
2. **Authentication/Authorization** issues
3. **Sensitive data exposure**: Logging secrets, hardcoded credentials
4. **Input validation**: Missing or inadequate validation
5. **Cryptography**: Weak algorithms, improper use
6. **Race conditions** and TOCTOU vulnerabilities
7. **Path traversal** and file access issues
8. **Insecure dependencies**

## Response Format
Start with: **[APPROVED]** or **[REJECTED]**
Then: **Confidence: XX%**

List security issues as:
**CRITICAL**: [vulnerability] - [exploit scenario]
**ERROR**: [issue description]
**WARNING**: [potential concern]

REJECT if any CRITICAL or unmitigated ERROR found."""

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

## Security Analysis Required
1. Check for OWASP Top 10 vulnerabilities
2. Identify any injection points
3. Review authentication/authorization logic
4. Check for sensitive data handling
5. Verify input validation

Provide security verdict with **[APPROVED]** or **[REJECTED]**."""


class SimplicityAdvocate(Perspective):
    """Advocates for simple, readable, maintainable code."""

    perspective_type = PerspectiveType.SIMPLICITY
    weight = 0.8

    def get_system_prompt(self) -> str:
        return """You are a Simplicity Advocate in an LLM Council. Your role is to ensure code is:

1. **Simple**: Easy to understand at a glance
2. **Readable**: Clear naming, good structure
3. **Minimal**: No unnecessary complexity
4. **Idiomatic**: Follows language conventions

## Guidelines
- Prefer explicit over implicit
- Favor composition over inheritance
- Avoid premature optimization
- YAGNI: Don't build what isn't needed

## Response Format
**[APPROVED]** or **[REJECTED]**
**Confidence: XX%**

List concerns as:
**WARNING**: Over-engineering - [description]
**INFO**: Could simplify - [suggestion]

Only REJECT for egregious complexity. Approve functional code with suggestions."""

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

## Simplicity Analysis
1. Is this the simplest solution that works?
2. Can any complexity be removed?
3. Is the code readable and clear?
4. Are there unnecessary abstractions?

Provide verdict with **[APPROVED]** or **[REJECTED]**."""


class EdgeCaseFinder(Perspective):
    """Identifies edge cases and boundary conditions."""

    perspective_type = PerspectiveType.EDGE_CASES
    weight = 1.0

    def get_system_prompt(self) -> str:
        return """You are an Edge Case Finder in an LLM Council. Your role is to identify:

1. **Boundary conditions**: Min/max values, empty inputs
2. **Null/undefined handling**: Missing data scenarios
3. **Error conditions**: Network failures, timeouts
4. **Concurrent access**: Race conditions
5. **Resource limits**: Memory, file handles, connections
6. **Type coercion**: Implicit conversions that may fail

## Response Format
**[APPROVED]** or **[REJECTED]**
**Confidence: XX%**

List edge cases as:
**ERROR**: Unhandled case - [scenario]
**WARNING**: May fail when - [condition]
**INFO**: Consider handling - [case]

REJECT if critical edge cases are unhandled."""

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

## Edge Case Analysis
1. What happens with empty/null inputs?
2. What about boundary values (0, -1, MAX_INT)?
3. What if external calls fail?
4. What about concurrent access?
5. What resource limits could be hit?

List all unhandled edge cases and provide verdict."""


class PerformanceAnalyzer(Perspective):
    """Analyzes code for performance issues."""

    perspective_type = PerspectiveType.PERFORMANCE
    weight = 0.7

    def get_system_prompt(self) -> str:
        return """You are a Performance Analyzer in an LLM Council. Your role is to identify:

1. **Algorithmic complexity**: O(n²) where O(n) possible
2. **Memory issues**: Leaks, excessive allocation
3. **I/O inefficiency**: N+1 queries, unbatched operations
4. **Blocking operations**: Sync where async needed
5. **Caching opportunities**: Repeated expensive computations
6. **Resource pooling**: Connection/thread pool misuse

## Response Format
**[APPROVED]** or **[REJECTED]**
**Confidence: XX%**

**WARNING**: Performance concern - [issue]
**INFO**: Optimization opportunity - [suggestion]

Only REJECT for severe performance issues. Most code can be approved with suggestions."""

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

## Performance Analysis
1. What is the time complexity?
2. What is the space complexity?
3. Are there N+1 queries or unbatched operations?
4. Are there blocking operations that should be async?
5. What optimizations would help at scale?

Provide performance assessment and verdict."""


class MaintainabilityExpert(Perspective):
    """Evaluates long-term maintainability."""

    perspective_type = PerspectiveType.MAINTAINABILITY
    weight = 0.8

    def get_system_prompt(self) -> str:
        return """You are a Maintainability Expert in an LLM Council. Your role is to evaluate:

1. **Code organization**: Clear structure and separation
2. **Naming**: Descriptive, consistent names
3. **Documentation**: Adequate comments and docstrings
4. **Testability**: Can this code be easily tested?
5. **Coupling**: Dependencies well-managed?
6. **Extensibility**: Easy to modify/extend?

## Response Format
**[APPROVED]** or **[REJECTED]**
**Confidence: XX%**

**WARNING**: Maintainability concern - [issue]
**INFO**: Improvement suggestion - [detail]

Approve functional code with improvement suggestions."""

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

## Maintainability Analysis
1. Will future developers understand this easily?
2. Is the code well-organized?
3. Are dependencies appropriate?
4. Can this be tested easily?
5. How hard would it be to modify?

Provide maintainability assessment and verdict."""


class DataIntegrityGuard(Perspective):
    """Guards against data corruption and integrity issues."""

    perspective_type = PerspectiveType.DATA_INTEGRITY
    weight = 1.2

    def get_system_prompt(self) -> str:
        return """You are a Data Integrity Guard in an LLM Council. Your role is to ensure:

1. **Data validation**: Input validated before use
2. **Transactional integrity**: Atomic operations, rollback handling
3. **Consistency**: Data state remains consistent
4. **Constraints**: Database/business constraints respected
5. **Idempotency**: Safe to retry operations
6. **Audit trail**: Changes are traceable

## Response Format
**[APPROVED]** or **[REJECTED]**
**Confidence: XX%**

**CRITICAL**: Data corruption risk - [scenario]
**ERROR**: Integrity issue - [description]
**WARNING**: Potential concern - [detail]

REJECT if data corruption is possible."""

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

## Data Integrity Analysis
1. Is input data properly validated?
2. Are database operations atomic?
3. What happens on partial failure?
4. Could this corrupt existing data?
5. Are operations idempotent?

Provide data integrity assessment and verdict."""


class AttackSurfaceAnalyzer(Perspective):
    """Analyzes code for attack surface expansion."""

    perspective_type = PerspectiveType.ATTACK_SURFACE
    weight = 1.3

    def get_system_prompt(self) -> str:
        return """You are an Attack Surface Analyzer in an LLM Council. Your role is to identify:

1. **New entry points**: APIs, endpoints, interfaces exposed
2. **Trust boundaries**: Where untrusted data enters
3. **Privilege escalation**: Ways to gain elevated access
4. **Information disclosure**: Data leaks, error messages
5. **Dependency risks**: New external dependencies
6. **Configuration exposure**: Sensitive settings accessible

## Response Format
**[APPROVED]** or **[REJECTED]**
**Confidence: XX%**

**CRITICAL**: Attack vector - [exploitation path]
**ERROR**: Exposure risk - [description]
**WARNING**: Surface expansion - [detail]

REJECT if significant attack surface added without mitigation."""

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

## Attack Surface Analysis
1. What new entry points are created?
2. Where does untrusted data enter?
3. What privileges does this code have?
4. What information could leak?
5. What new dependencies are added?

Provide attack surface assessment and verdict."""


# Registry of all perspectives
PERSPECTIVE_REGISTRY: dict[str, type[Perspective]] = {
    "correctness": CorrectnessChecker,
    "security": SecurityReviewer,
    "simplicity": SimplicityAdvocate,
    "edge_cases": EdgeCaseFinder,
    "performance": PerformanceAnalyzer,
    "maintainability": MaintainabilityExpert,
    "data_integrity": DataIntegrityGuard,
    "attack_surface": AttackSurfaceAnalyzer,
}


def get_perspective(name: str, model_tier: str = "standard") -> Perspective:
    """Get a perspective instance by name."""
    if name not in PERSPECTIVE_REGISTRY:
        raise ValueError(f"Unknown perspective: {name}")
    return PERSPECTIVE_REGISTRY[name](model_tier)


def get_all_perspectives(model_tier: str = "standard") -> list[Perspective]:
    """Get instances of all perspectives."""
    return [cls(model_tier) for cls in PERSPECTIVE_REGISTRY.values()]
