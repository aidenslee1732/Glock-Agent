"""Task analyzer - analyzes tasks for type, complexity, and risk."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class TaskType(str, Enum):
    """Task type classification."""
    IMPLEMENT = "implement"
    DEBUG = "debug"
    REFACTOR = "refactor"
    SECURITY = "security"
    DEPLOY = "deploy"
    QUESTION = "question"
    REVIEW = "review"
    TEST = "test"
    OTHER = "other"


class Complexity(str, Enum):
    """Task complexity levels."""
    TRIVIAL = "trivial"      # Single file, obvious fix
    SIMPLE = "simple"        # Few files, clear approach
    MODERATE = "moderate"    # Multiple files, some analysis needed
    COMPLEX = "complex"      # Many files, significant changes
    CRITICAL = "critical"    # System-wide, architectural


class RiskLevel(str, Enum):
    """Task risk levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class TaskAnalysis:
    """Result of task analysis."""
    task_type: TaskType
    complexity: Complexity
    risk_level: RiskLevel
    risk_flags: list[str] = field(default_factory=list)
    suggested_tools: list[str] = field(default_factory=list)
    validation_steps: list[str] = field(default_factory=list)
    requires_approval: bool = False
    confidence: float = 0.8


# Risk patterns to detect
RISK_PATTERNS = {
    "auth": [
        r"\bauth(entication|orization)?\b",
        r"\blogin\b", r"\blogout\b",
        r"\bpassword\b", r"\bcredential\b",
        r"\btoken\b", r"\bjwt\b", r"\boauth\b",
        r"\bsession\b", r"\bpermission\b",
    ],
    "secrets": [
        r"\b(api[_-]?)?key\b", r"\bsecret\b",
        r"\bcredential\b", r"\bpassword\b",
        r"\.env\b", r"\bconfig\b.*\b(key|secret|password)\b",
    ],
    "data": [
        r"\bdelete\b.*\b(data|record|user)\b",
        r"\bdrop\b.*\btable\b",
        r"\bmigrat(e|ion)\b",
        r"\btruncate\b",
    ],
    "deploy": [
        r"\bdeploy\b", r"\bproduction\b",
        r"\brelease\b", r"\bpublish\b",
        r"\brollout\b", r"\brollback\b",
    ],
    "network": [
        r"\bfirewall\b", r"\bport\b",
        r"\bcors\b", r"\bssl\b", r"\btls\b",
        r"\bhttp(s)?\b.*\bexpose\b",
    ],
    "exec": [
        r"\bexec\b", r"\bspawn\b",
        r"\bshell\b", r"\bbash\b",
        r"\bsudo\b", r"\broot\b",
        r"\brm\s+-rf\b",
    ],
}

# Task type patterns
TYPE_PATTERNS = {
    TaskType.DEBUG: [
        r"\bfix\b", r"\bbug\b", r"\berror\b",
        r"\bissue\b", r"\bcrash\b", r"\bfail\b",
        r"\bbroken\b", r"\bnot working\b",
    ],
    TaskType.IMPLEMENT: [
        r"\badd\b", r"\bcreate\b", r"\bimplement\b",
        r"\bbuild\b", r"\bnew\b", r"\bfeature\b",
    ],
    TaskType.REFACTOR: [
        r"\brefactor\b", r"\bclean\b", r"\breorganize\b",
        r"\bimprove\b", r"\boptimize\b", r"\bsimplify\b",
    ],
    TaskType.SECURITY: [
        r"\bsecurity\b", r"\bvulnerability\b",
        r"\bauth\b", r"\bpermission\b",
        r"\bsanitize\b", r"\bvalidate input\b",
    ],
    TaskType.DEPLOY: [
        r"\bdeploy\b", r"\brelease\b",
        r"\bpublish\b", r"\bci/cd\b",
    ],
    TaskType.QUESTION: [
        r"\bwhat\b", r"\bhow\b", r"\bwhy\b",
        r"\bexplain\b", r"\bunderstand\b",
        r"^\?", r"\?$",
    ],
    TaskType.REVIEW: [
        r"\breview\b", r"\bcheck\b",
        r"\baudit\b", r"\banalyze\b",
    ],
    TaskType.TEST: [
        r"\btest\b", r"\bcoverage\b",
        r"\bunit test\b", r"\bintegration test\b",
    ],
}

# Complexity indicators
COMPLEXITY_INDICATORS = {
    Complexity.TRIVIAL: [
        r"\btypo\b", r"\bspelling\b",
        r"\bcomment\b", r"\bdocstring\b",
        r"\bone line\b", r"\bsingle\b",
    ],
    Complexity.SIMPLE: [
        r"\bsmall\b", r"\bquick\b",
        r"\bsimple\b", r"\beasy\b",
        r"\bminor\b",
    ],
    Complexity.COMPLEX: [
        r"\barchitecture\b", r"\bsystem\b",
        r"\bmultiple files\b", r"\brefactor\b",
        r"\bintegration\b",
    ],
    Complexity.CRITICAL: [
        r"\bcritical\b", r"\burgent\b",
        r"\bproduction\b", r"\bdatabase\b",
        r"\bmigration\b",
    ],
}


class TaskAnalyzer:
    """Analyzes tasks to determine type, complexity, and risk."""

    def __init__(self):
        # Compile patterns for efficiency
        self._risk_patterns = {
            flag: [re.compile(p, re.IGNORECASE) for p in patterns]
            for flag, patterns in RISK_PATTERNS.items()
        }
        self._type_patterns = {
            t: [re.compile(p, re.IGNORECASE) for p in patterns]
            for t, patterns in TYPE_PATTERNS.items()
        }
        self._complexity_patterns = {
            c: [re.compile(p, re.IGNORECASE) for p in patterns]
            for c, patterns in COMPLEXITY_INDICATORS.items()
        }

    def analyze(
        self,
        prompt: str,
        context: Optional[dict[str, Any]] = None,
    ) -> TaskAnalysis:
        """Analyze a task from user prompt and context.

        Args:
            prompt: User's task description
            context: Optional workspace context

        Returns:
            TaskAnalysis with type, complexity, risk, etc.
        """
        context = context or {}

        # Detect task type
        task_type = self._detect_type(prompt)

        # Detect risk flags
        risk_flags = self._detect_risk_flags(prompt, context)

        # Determine complexity
        complexity = self._assess_complexity(prompt, context, risk_flags)

        # Calculate risk level
        risk_level = self._calculate_risk_level(risk_flags, task_type, complexity)

        # Suggest tools based on type
        suggested_tools = self._suggest_tools(task_type, context)

        # Determine validation steps
        validation_steps = self._determine_validation(task_type, context)

        # Determine if approval required
        requires_approval = self._requires_approval(risk_level, risk_flags)

        # Confidence based on pattern matches
        confidence = self._calculate_confidence(prompt, task_type)

        return TaskAnalysis(
            task_type=task_type,
            complexity=complexity,
            risk_level=risk_level,
            risk_flags=risk_flags,
            suggested_tools=suggested_tools,
            validation_steps=validation_steps,
            requires_approval=requires_approval,
            confidence=confidence,
        )

    def _detect_type(self, prompt: str) -> TaskType:
        """Detect task type from prompt."""
        scores: dict[TaskType, int] = {}

        for task_type, patterns in self._type_patterns.items():
            score = sum(1 for p in patterns if p.search(prompt))
            if score > 0:
                scores[task_type] = score

        if not scores:
            return TaskType.OTHER

        # Return highest scoring type
        return max(scores, key=scores.get)

    def _detect_risk_flags(
        self,
        prompt: str,
        context: dict[str, Any],
    ) -> list[str]:
        """Detect risk flags from prompt and context."""
        flags = []

        # Check prompt for risk patterns
        for flag, patterns in self._risk_patterns.items():
            for p in patterns:
                if p.search(prompt):
                    flags.append(flag)
                    break

        # Check active files for sensitive paths
        active_files = context.get("active_files", [])
        for file_path in active_files:
            if any(x in file_path.lower() for x in ["auth", "login", "session"]):
                if "auth" not in flags:
                    flags.append("auth")
            if any(x in file_path.lower() for x in [".env", "secret", "config"]):
                if "secrets" not in flags:
                    flags.append("secrets")
            if "deploy" in file_path.lower() or "ci" in file_path.lower():
                if "deploy" not in flags:
                    flags.append("deploy")

        return flags

    def _assess_complexity(
        self,
        prompt: str,
        context: dict[str, Any],
        risk_flags: list[str],
    ) -> Complexity:
        """Assess task complexity."""
        # Check explicit indicators
        for complexity, patterns in self._complexity_patterns.items():
            for p in patterns:
                if p.search(prompt):
                    return complexity

        # Infer from context
        active_files = context.get("active_files", [])
        if len(active_files) == 0:
            # Unknown scope - assume moderate
            return Complexity.MODERATE
        elif len(active_files) == 1:
            return Complexity.SIMPLE
        elif len(active_files) <= 3:
            return Complexity.MODERATE
        else:
            return Complexity.COMPLEX

        # Increase complexity for risky tasks
        if risk_flags:
            if complexity == Complexity.TRIVIAL:
                return Complexity.SIMPLE
            elif complexity == Complexity.SIMPLE:
                return Complexity.MODERATE

    def _calculate_risk_level(
        self,
        risk_flags: list[str],
        task_type: TaskType,
        complexity: Complexity,
    ) -> RiskLevel:
        """Calculate overall risk level."""
        if not risk_flags:
            if complexity in (Complexity.TRIVIAL, Complexity.SIMPLE):
                return RiskLevel.LOW
            return RiskLevel.MEDIUM

        # Critical risk flags
        critical_flags = {"secrets", "exec", "deploy"}
        high_flags = {"auth", "data", "network"}

        if critical_flags & set(risk_flags):
            return RiskLevel.CRITICAL

        if high_flags & set(risk_flags):
            return RiskLevel.HIGH

        if len(risk_flags) >= 2:
            return RiskLevel.HIGH

        return RiskLevel.MEDIUM

    def _suggest_tools(
        self,
        task_type: TaskType,
        context: dict[str, Any],
    ) -> list[str]:
        """Suggest tools based on task type."""
        # Base tools always available
        base_tools = ["read_file", "glob", "grep"]

        # Type-specific tools
        type_tools = {
            TaskType.IMPLEMENT: ["edit_file", "write_file", "bash"],
            TaskType.DEBUG: ["edit_file", "bash", "list_directory"],
            TaskType.REFACTOR: ["edit_file", "bash"],
            TaskType.SECURITY: ["edit_file", "bash"],
            TaskType.DEPLOY: ["bash"],
            TaskType.QUESTION: [],
            TaskType.REVIEW: ["list_directory"],
            TaskType.TEST: ["bash", "edit_file"],
            TaskType.OTHER: ["edit_file", "bash"],
        }

        tools = base_tools + type_tools.get(task_type, [])

        # Add git tools if in repo
        if context.get("git_status"):
            tools.extend(["git_diff", "git_log", "git_status"])

        # Add web tools if might need docs
        if task_type == TaskType.QUESTION:
            tools.extend(["web_search", "web_fetch"])

        return list(set(tools))

    def _determine_validation(
        self,
        task_type: TaskType,
        context: dict[str, Any],
    ) -> list[str]:
        """Determine validation steps."""
        validations = []

        available = context.get("available_validations", [])

        # Always run tests if available
        if "pytest" in available or "test" in available:
            validations.append("test")

        # Run linter for code changes
        if task_type in (TaskType.IMPLEMENT, TaskType.DEBUG, TaskType.REFACTOR):
            if "ruff" in available or "lint" in available:
                validations.append("lint")
            if "mypy" in available or "typecheck" in available:
                validations.append("typecheck")

        return validations

    def _requires_approval(
        self,
        risk_level: RiskLevel,
        risk_flags: list[str],
    ) -> bool:
        """Determine if task requires user approval."""
        if risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return True

        # Always require approval for certain flags
        approval_flags = {"deploy", "exec", "secrets", "data"}
        if approval_flags & set(risk_flags):
            return True

        return False

    def _calculate_confidence(self, prompt: str, task_type: TaskType) -> float:
        """Calculate confidence in the analysis."""
        # Count pattern matches
        patterns = self._type_patterns.get(task_type, [])
        matches = sum(1 for p in patterns if p.search(prompt))

        if matches >= 3:
            return 0.95
        elif matches >= 2:
            return 0.85
        elif matches >= 1:
            return 0.75
        else:
            return 0.6
