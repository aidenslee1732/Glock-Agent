"""Quality Gate - Evaluates code quality before execution.

The quality gate performs static analysis and scoring to determine
if code meets minimum quality thresholds before being executed.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class QualityGateConfig:
    """Configuration for quality gate thresholds."""

    # Minimum overall score to pass (0-100)
    min_score: float = 60.0

    # Maximum cyclomatic complexity per function
    max_complexity: int = 15

    # Maximum lines per function
    max_function_length: int = 50

    # Whether to require docstrings for public functions
    require_docstrings: bool = False

    # Whether to require type hints
    require_type_hints: bool = False

    # Whether to block on any security issues
    block_on_security: bool = True

    # Weights for scoring components
    complexity_weight: float = 0.25
    security_weight: float = 0.35
    style_weight: float = 0.15
    maintainability_weight: float = 0.25

    @classmethod
    def strict(cls) -> "QualityGateConfig":
        """Create strict quality gate config."""
        return cls(
            min_score=75.0,
            max_complexity=10,
            max_function_length=30,
            require_docstrings=True,
            require_type_hints=True,
            block_on_security=True,
        )

    @classmethod
    def lenient(cls) -> "QualityGateConfig":
        """Create lenient quality gate config."""
        return cls(
            min_score=40.0,
            max_complexity=25,
            max_function_length=100,
            require_docstrings=False,
            require_type_hints=False,
            block_on_security=False,
        )


class QualityLevel(str, Enum):
    """Quality level thresholds."""
    EXCELLENT = "excellent"  # 90-100
    GOOD = "good"           # 75-89
    ACCEPTABLE = "acceptable"  # 60-74
    POOR = "poor"           # 40-59
    FAILING = "failing"     # 0-39


@dataclass
class ComplexityMetrics:
    """Code complexity metrics."""
    cyclomatic_complexity: int = 0
    cognitive_complexity: int = 0
    max_nesting_depth: int = 0
    lines_of_code: int = 0
    comment_ratio: float = 0.0
    function_count: int = 0
    class_count: int = 0
    avg_function_length: float = 0.0


@dataclass
class SecurityMetrics:
    """Security-related metrics."""
    hardcoded_secrets: int = 0
    sql_injection_risks: int = 0
    command_injection_risks: int = 0
    path_traversal_risks: int = 0
    insecure_functions: int = 0
    missing_input_validation: int = 0


@dataclass
class StyleMetrics:
    """Code style metrics."""
    naming_violations: int = 0
    line_length_violations: int = 0
    import_violations: int = 0
    docstring_coverage: float = 0.0
    type_hint_coverage: float = 0.0


@dataclass
class QualityMetrics:
    """Combined quality metrics."""
    complexity: ComplexityMetrics = field(default_factory=ComplexityMetrics)
    security: SecurityMetrics = field(default_factory=SecurityMetrics)
    style: StyleMetrics = field(default_factory=StyleMetrics)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class QualityScore:
    """Quality score with breakdown."""
    overall: float  # 0-100
    level: QualityLevel
    complexity_score: float
    security_score: float
    style_score: float
    maintainability_score: float
    passed: bool
    blocking_issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    metrics: Optional[QualityMetrics] = None


class QualityGate:
    """Evaluates code quality and enforces minimum standards.

    The gate performs static analysis on proposed code changes
    and determines if they meet quality thresholds.
    """

    def __init__(
        self,
        min_score: float = 60.0,
        max_complexity: int = 15,
        max_function_length: int = 50,
        require_docstrings: bool = False,
        require_type_hints: bool = False,
        block_on_security: bool = True,
        config: Optional[QualityGateConfig] = None,
    ):
        """Initialize quality gate.

        Args:
            min_score: Minimum overall score to pass (0-100)
            max_complexity: Maximum cyclomatic complexity per function
            max_function_length: Maximum lines per function
            require_docstrings: Require docstrings for public functions
            require_type_hints: Require type hints
            block_on_security: Block on any security issues
            config: Optional config object (overrides individual params)
        """
        # Use config if provided, otherwise use individual params
        if config is not None:
            self.config = config
            self.min_score = config.min_score
            self.max_complexity = config.max_complexity
            self.max_function_length = config.max_function_length
            self.require_docstrings = config.require_docstrings
            self.require_type_hints = config.require_type_hints
            self.block_on_security = config.block_on_security
        else:
            self.config = QualityGateConfig(
                min_score=min_score,
                max_complexity=max_complexity,
                max_function_length=max_function_length,
                require_docstrings=require_docstrings,
                require_type_hints=require_type_hints,
                block_on_security=block_on_security,
            )
            self.min_score = min_score
            self.max_complexity = max_complexity
            self.max_function_length = max_function_length
            self.require_docstrings = require_docstrings
            self.require_type_hints = require_type_hints
            self.block_on_security = block_on_security

        # Security patterns to detect
        self._security_patterns = {
            "hardcoded_secrets": [
                r"(?:password|secret|api_key|token)\s*=\s*['\"][^'\"]+['\"]",
                r"(?:PASSWORD|SECRET|API_KEY|TOKEN)\s*=\s*['\"][^'\"]+['\"]",
            ],
            "sql_injection": [
                r"execute\s*\([^)]*%\s*\(",
                r"execute\s*\([^)]*\.format\(",
                r"execute\s*\([^)]*f['\"]",
            ],
            "command_injection": [
                r"os\.system\s*\(",
                r"subprocess\..*shell\s*=\s*True",
                r"eval\s*\(",
                r"exec\s*\(",
            ],
            "path_traversal": [
                r"open\s*\([^)]*\+",
                r"Path\s*\([^)]*\+",
            ],
        }

        # Insecure functions
        self._insecure_functions = {
            "pickle.loads": "Arbitrary code execution risk",
            "yaml.load": "Use yaml.safe_load instead",
            "eval": "Arbitrary code execution",
            "exec": "Arbitrary code execution",
            "os.system": "Use subprocess with shell=False",
            "__import__": "Dynamic import risk",
        }

    def evaluate(
        self,
        code: str,
        language: str = "python",
        context: Optional[dict[str, Any]] = None,
    ) -> QualityScore:
        """Evaluate code quality.

        Args:
            code: Code to evaluate
            language: Programming language
            context: Additional context

        Returns:
            QualityScore with detailed breakdown
        """
        metrics = QualityMetrics()
        blocking_issues = []
        recommendations = []

        # Analyze based on language
        if language == "python":
            self._analyze_python(code, metrics)
        elif language in ("javascript", "typescript"):
            self._analyze_javascript(code, metrics)
        else:
            # Generic analysis
            self._analyze_generic(code, metrics)

        # Calculate component scores
        complexity_score = self._score_complexity(metrics.complexity)
        security_score = self._score_security(metrics.security)
        style_score = self._score_style(metrics.style)
        maintainability_score = self._score_maintainability(metrics)

        # Check for blocking issues
        if self.block_on_security and security_score < 80:
            blocking_issues.append("Security issues detected")

        if metrics.complexity.cyclomatic_complexity > self.max_complexity:
            blocking_issues.append(
                f"Cyclomatic complexity {metrics.complexity.cyclomatic_complexity} "
                f"exceeds maximum {self.max_complexity}"
            )

        # Generate recommendations
        if complexity_score < 70:
            recommendations.append("Consider breaking down complex functions")
        if security_score < 90:
            recommendations.append("Review and fix security issues")
        if style_score < 70:
            recommendations.append("Improve code style and documentation")
        if maintainability_score < 70:
            recommendations.append("Improve code maintainability")

        # Calculate overall score (weighted average using config)
        overall = (
            complexity_score * self.config.complexity_weight +
            security_score * self.config.security_weight +
            style_score * self.config.style_weight +
            maintainability_score * self.config.maintainability_weight
        )

        # Determine level
        level = self._score_to_level(overall)

        # Determine pass/fail
        passed = overall >= self.min_score and not blocking_issues

        return QualityScore(
            overall=overall,
            level=level,
            complexity_score=complexity_score,
            security_score=security_score,
            style_score=style_score,
            maintainability_score=maintainability_score,
            passed=passed,
            blocking_issues=blocking_issues,
            recommendations=recommendations,
            metrics=metrics,
        )

    def _analyze_python(self, code: str, metrics: QualityMetrics) -> None:
        """Analyze Python code."""
        lines = code.split("\n")
        metrics.complexity.lines_of_code = len([l for l in lines if l.strip() and not l.strip().startswith("#")])

        # Count comments
        comment_lines = len([l for l in lines if l.strip().startswith("#")])
        if metrics.complexity.lines_of_code > 0:
            metrics.complexity.comment_ratio = comment_lines / metrics.complexity.lines_of_code

        # Try to parse AST
        try:
            tree = ast.parse(code)
            self._analyze_python_ast(tree, metrics)
        except SyntaxError as e:
            metrics.errors.append(f"Syntax error: {e}")

        # Check security patterns
        self._check_security_patterns(code, metrics.security)

        # Check style
        self._check_python_style(code, lines, metrics.style)

    def _analyze_python_ast(self, tree: ast.AST, metrics: QualityMetrics) -> None:
        """Analyze Python AST."""
        functions = []
        classes = []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                functions.append(node)
                # Calculate cyclomatic complexity
                complexity = self._calculate_cyclomatic_complexity(node)
                metrics.complexity.cyclomatic_complexity = max(
                    metrics.complexity.cyclomatic_complexity, complexity
                )
                # Calculate nesting depth
                depth = self._calculate_nesting_depth(node)
                metrics.complexity.max_nesting_depth = max(
                    metrics.complexity.max_nesting_depth, depth
                )
            elif isinstance(node, ast.ClassDef):
                classes.append(node)

        metrics.complexity.function_count = len(functions)
        metrics.complexity.class_count = len(classes)

        if functions:
            total_lines = sum(
                (getattr(f, 'end_lineno', 0) or 0) - f.lineno + 1
                for f in functions
            )
            metrics.complexity.avg_function_length = total_lines / len(functions)

        # Check docstring coverage
        functions_with_docstrings = sum(
            1 for f in functions
            if ast.get_docstring(f) is not None
        )
        if functions:
            metrics.style.docstring_coverage = functions_with_docstrings / len(functions)

        # Check type hint coverage
        functions_with_hints = sum(
            1 for f in functions
            if f.returns is not None or any(a.annotation for a in f.args.args)
        )
        if functions:
            metrics.style.type_hint_coverage = functions_with_hints / len(functions)

    def _calculate_cyclomatic_complexity(self, node: ast.AST) -> int:
        """Calculate cyclomatic complexity of a function."""
        complexity = 1  # Base complexity

        for child in ast.walk(node):
            # Each branch increases complexity
            if isinstance(child, (ast.If, ast.While, ast.For)):
                complexity += 1
            elif isinstance(child, ast.ExceptHandler):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
            elif isinstance(child, ast.comprehension):
                complexity += 1
                if child.ifs:
                    complexity += len(child.ifs)

        return complexity

    def _calculate_nesting_depth(self, node: ast.AST, depth: int = 0) -> int:
        """Calculate maximum nesting depth."""
        max_depth = depth

        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.With, ast.Try)):
                child_depth = self._calculate_nesting_depth(child, depth + 1)
                max_depth = max(max_depth, child_depth)
            else:
                child_depth = self._calculate_nesting_depth(child, depth)
                max_depth = max(max_depth, child_depth)

        return max_depth

    def _analyze_javascript(self, code: str, metrics: QualityMetrics) -> None:
        """Analyze JavaScript/TypeScript code."""
        lines = code.split("\n")
        metrics.complexity.lines_of_code = len([
            l for l in lines
            if l.strip() and not l.strip().startswith("//")
        ])

        # Count functions (simple heuristic)
        metrics.complexity.function_count = len(re.findall(
            r"(?:function\s+\w+|(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>))",
            code
        ))

        # Estimate complexity from control flow
        complexity = 1
        complexity += len(re.findall(r"\bif\s*\(", code))
        complexity += len(re.findall(r"\bwhile\s*\(", code))
        complexity += len(re.findall(r"\bfor\s*\(", code))
        complexity += len(re.findall(r"\bcase\s+", code))
        complexity += len(re.findall(r"\bcatch\s*\(", code))
        metrics.complexity.cyclomatic_complexity = complexity

        # Check security
        self._check_security_patterns(code, metrics.security)

    def _analyze_generic(self, code: str, metrics: QualityMetrics) -> None:
        """Generic code analysis."""
        lines = code.split("\n")
        metrics.complexity.lines_of_code = len([l for l in lines if l.strip()])

        # Basic complexity estimation
        metrics.complexity.cyclomatic_complexity = (
            len(re.findall(r"\bif\b", code)) +
            len(re.findall(r"\bwhile\b", code)) +
            len(re.findall(r"\bfor\b", code)) +
            1
        )

        self._check_security_patterns(code, metrics.security)

    def _check_security_patterns(self, code: str, security: SecurityMetrics) -> None:
        """Check for security patterns in code."""
        code_lower = code.lower()

        # Check each pattern category
        for category, patterns in self._security_patterns.items():
            count = sum(len(re.findall(p, code, re.IGNORECASE)) for p in patterns)
            if category == "hardcoded_secrets":
                security.hardcoded_secrets = count
            elif category == "sql_injection":
                security.sql_injection_risks = count
            elif category == "command_injection":
                security.command_injection_risks = count
            elif category == "path_traversal":
                security.path_traversal_risks = count

        # Check insecure functions
        for func, _ in self._insecure_functions.items():
            if func in code:
                security.insecure_functions += 1

    def _check_python_style(
        self,
        code: str,
        lines: list[str],
        style: StyleMetrics,
    ) -> None:
        """Check Python code style."""
        # Line length violations (PEP 8: 79 chars, relaxed to 100)
        style.line_length_violations = sum(1 for l in lines if len(l) > 100)

        # Naming violations (simple check for snake_case in functions)
        camel_case_funcs = len(re.findall(r"def\s+[a-z]+[A-Z]", code))
        style.naming_violations = camel_case_funcs

        # Import violations (star imports)
        style.import_violations = len(re.findall(r"from\s+\S+\s+import\s+\*", code))

    def _score_complexity(self, complexity: ComplexityMetrics) -> float:
        """Score complexity metrics (0-100)."""
        score = 100.0

        # Penalize high cyclomatic complexity
        if complexity.cyclomatic_complexity > 10:
            score -= (complexity.cyclomatic_complexity - 10) * 5
        if complexity.cyclomatic_complexity > 20:
            score -= (complexity.cyclomatic_complexity - 20) * 10

        # Penalize deep nesting
        if complexity.max_nesting_depth > 4:
            score -= (complexity.max_nesting_depth - 4) * 10

        # Penalize long functions
        if complexity.avg_function_length > 30:
            score -= (complexity.avg_function_length - 30) * 2

        # Bonus for good comment ratio
        if complexity.comment_ratio >= 0.1:
            score += 5

        return max(0.0, min(100.0, score))

    def _score_security(self, security: SecurityMetrics) -> float:
        """Score security metrics (0-100)."""
        score = 100.0

        # Critical issues
        score -= security.hardcoded_secrets * 25
        score -= security.sql_injection_risks * 30
        score -= security.command_injection_risks * 30

        # Moderate issues
        score -= security.path_traversal_risks * 15
        score -= security.insecure_functions * 10
        score -= security.missing_input_validation * 5

        return max(0.0, min(100.0, score))

    def _score_style(self, style: StyleMetrics) -> float:
        """Score style metrics (0-100)."""
        score = 100.0

        # Penalize violations
        score -= style.naming_violations * 5
        score -= style.line_length_violations * 1
        score -= style.import_violations * 10

        # Reward good practices
        if style.docstring_coverage >= 0.8:
            score += 5
        if style.type_hint_coverage >= 0.8:
            score += 5

        return max(0.0, min(100.0, score))

    def _score_maintainability(self, metrics: QualityMetrics) -> float:
        """Score overall maintainability (0-100)."""
        score = 100.0

        # Factor in complexity
        if metrics.complexity.cyclomatic_complexity > 10:
            score -= 10
        if metrics.complexity.max_nesting_depth > 3:
            score -= 10
        if metrics.complexity.avg_function_length > 25:
            score -= 10

        # Factor in style
        if metrics.style.docstring_coverage < 0.5:
            score -= 10
        if metrics.style.type_hint_coverage < 0.5:
            score -= 5

        # Factor in errors/warnings
        score -= len(metrics.errors) * 10
        score -= len(metrics.warnings) * 2

        return max(0.0, min(100.0, score))

    def _score_to_level(self, score: float) -> QualityLevel:
        """Convert numeric score to quality level."""
        if score >= 90:
            return QualityLevel.EXCELLENT
        elif score >= 75:
            return QualityLevel.GOOD
        elif score >= 60:
            return QualityLevel.ACCEPTABLE
        elif score >= 40:
            return QualityLevel.POOR
        else:
            return QualityLevel.FAILING


class QualityGateResult:
    """Result of quality gate check with before/after comparison."""

    def __init__(
        self,
        before: Optional[QualityScore],
        after: QualityScore,
        passed: bool,
        reason: str,
    ):
        self.before = before
        self.after = after
        self.passed = passed
        self.reason = reason

    @property
    def improved(self) -> bool:
        """Check if quality improved."""
        if self.before is None:
            return True
        return self.after.overall > self.before.overall

    @property
    def delta(self) -> float:
        """Get quality change delta."""
        if self.before is None:
            return 0.0
        return self.after.overall - self.before.overall

    def summary(self) -> str:
        """Get human-readable summary."""
        status = "PASSED" if self.passed else "BLOCKED"
        parts = [
            f"Quality Gate: {status}",
            f"Score: {self.after.overall:.1f}/100 ({self.after.level.value})",
        ]

        if self.before:
            delta = self.delta
            direction = "+" if delta > 0 else ""
            parts.append(f"Change: {direction}{delta:.1f}")

        if not self.passed:
            parts.append(f"Reason: {self.reason}")

        if self.after.blocking_issues:
            parts.append(f"Blocking: {', '.join(self.after.blocking_issues)}")

        return " | ".join(parts)


# v4 Enhancement: Quality gate with test execution
class QualityGateWithTests(QualityGate):
    """Extended quality gate that includes test execution.

    This gate runs static analysis AND generated tests to provide
    comprehensive quality assessment for one-shot code quality.
    """

    def __init__(
        self,
        test_executor=None,
        require_passing_tests: bool = True,
        **kwargs,
    ):
        """Initialize quality gate with test support.

        Args:
            test_executor: TestExecutor instance for running tests
            require_passing_tests: Whether tests must pass
            **kwargs: Arguments for base QualityGate
        """
        super().__init__(**kwargs)
        self._test_executor = test_executor
        self._require_passing_tests = require_passing_tests

    async def evaluate_with_tests(
        self,
        code: str,
        test_code: str,
        language: str = "python",
        context: Optional[dict] = None,
    ) -> QualityScore:
        """Evaluate code with test execution.

        Args:
            code: Code to evaluate
            test_code: Generated tests to run
            language: Programming language
            context: Additional context

        Returns:
            QualityScore including test results
        """
        # Run static analysis first
        score = self.evaluate(code, language, context)

        # Run tests if executor available
        if self._test_executor and test_code:
            try:
                test_result = await self._test_executor.execute_tests(
                    test_code=test_code,
                    language=language,
                    source_code=code,
                )

                # Adjust score based on test results
                if test_result.passed:
                    # Bonus for passing tests
                    test_bonus = 10.0 * test_result.success_rate
                    score.maintainability_score = min(100, score.maintainability_score + test_bonus)
                else:
                    # Penalty for failing tests
                    test_penalty = 20.0 * (1 - test_result.success_rate)
                    score.maintainability_score = max(0, score.maintainability_score - test_penalty)

                    if self._require_passing_tests:
                        score.blocking_issues.append(
                            f"Tests failed: {test_result.failed_count} of {test_result.total_tests}"
                        )
                        score.passed = False

                # Recalculate overall score
                score.overall = (
                    score.complexity_score * self.config.complexity_weight +
                    score.security_score * self.config.security_weight +
                    score.style_score * self.config.style_weight +
                    score.maintainability_score * self.config.maintainability_weight
                )
                score.level = self._score_to_level(score.overall)

                # Add test info to recommendations
                if test_result.total_tests > 0:
                    score.recommendations.insert(0,
                        f"Tests: {test_result.passed_count}/{test_result.total_tests} passed"
                    )

            except Exception as e:
                logger.warning(f"Test execution failed: {e}")
                score.recommendations.append(f"Test execution failed: {str(e)[:100]}")

        return score
