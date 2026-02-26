"""Failure parser - parses test failures and error messages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class FailureType(str, Enum):
    """Types of failures."""
    TEST_FAILURE = "test_failure"
    LINT_ERROR = "lint_error"
    TYPE_ERROR = "type_error"
    RUNTIME_ERROR = "runtime_error"
    SYNTAX_ERROR = "syntax_error"
    IMPORT_ERROR = "import_error"
    ASSERTION_ERROR = "assertion_error"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass
class ParsedFailure:
    """Parsed failure information."""
    failure_type: FailureType
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    column: Optional[int] = None
    test_name: Optional[str] = None
    function_name: Optional[str] = None
    message: str = ""
    expected: Optional[str] = None
    actual: Optional[str] = None
    traceback: list[str] = field(default_factory=list)
    raw_output: str = ""


# Patterns for different test frameworks and tools
PYTEST_FAILURE_PATTERN = re.compile(
    r"(?P<file>[\w/._-]+\.py):(?P<line>\d+):\s*(?P<message>.*)"
)

PYTEST_TEST_PATTERN = re.compile(
    r"(?:FAILED|ERROR)\s+(?P<file>[\w/._-]+\.py)::(?P<class>\w+)?::?(?P<test>\w+)"
)

MYPY_ERROR_PATTERN = re.compile(
    r"(?P<file>[\w/._-]+\.py):(?P<line>\d+):(?P<col>\d+):\s*error:\s*(?P<message>.*)"
)

RUFF_ERROR_PATTERN = re.compile(
    r"(?P<file>[\w/._-]+\.py):(?P<line>\d+):(?P<col>\d+):\s*(?P<code>\w+)\s+(?P<message>.*)"
)

ASSERTION_PATTERN = re.compile(
    r"AssertionError:\s*(?:assert\s+)?(?P<expr>.*)"
)

EXPECTED_ACTUAL_PATTERN = re.compile(
    r"(?:Expected|expected)[:=\s]+(?P<expected>.*?)(?:\n|,\s*)(?:Actual|actual|got)[:=\s]+(?P<actual>.*)"
)


class FailureParser:
    """Parses validation failures from test/lint/typecheck output."""

    def parse(self, output: str, tool: str = "pytest") -> list[ParsedFailure]:
        """Parse failures from tool output.

        Args:
            output: Raw tool output
            tool: Tool that produced the output

        Returns:
            List of parsed failures
        """
        if tool == "pytest":
            return self._parse_pytest(output)
        elif tool == "mypy":
            return self._parse_mypy(output)
        elif tool in ("ruff", "lint"):
            return self._parse_ruff(output)
        else:
            return self._parse_generic(output)

    def _parse_pytest(self, output: str) -> list[ParsedFailure]:
        """Parse pytest output."""
        failures = []

        # Find failed tests
        for match in PYTEST_TEST_PATTERN.finditer(output):
            file_path = match.group("file")
            test_class = match.group("class")
            test_name = match.group("test")

            full_test_name = (
                f"{test_class}::{test_name}" if test_class else test_name
            )

            failure = ParsedFailure(
                failure_type=FailureType.TEST_FAILURE,
                file_path=file_path,
                test_name=full_test_name,
                raw_output=output,
            )

            # Try to extract more details
            self._extract_assertion_details(output, failure)
            self._extract_line_number(output, file_path, failure)

            failures.append(failure)

        # If no structured failures found, try generic parsing
        if not failures:
            failures = self._parse_generic(output)

        return failures

    def _parse_mypy(self, output: str) -> list[ParsedFailure]:
        """Parse mypy output."""
        failures = []

        for match in MYPY_ERROR_PATTERN.finditer(output):
            failure = ParsedFailure(
                failure_type=FailureType.TYPE_ERROR,
                file_path=match.group("file"),
                line_number=int(match.group("line")),
                column=int(match.group("col")),
                message=match.group("message"),
                raw_output=output,
            )
            failures.append(failure)

        return failures

    def _parse_ruff(self, output: str) -> list[ParsedFailure]:
        """Parse ruff/lint output."""
        failures = []

        for match in RUFF_ERROR_PATTERN.finditer(output):
            failure = ParsedFailure(
                failure_type=FailureType.LINT_ERROR,
                file_path=match.group("file"),
                line_number=int(match.group("line")),
                column=int(match.group("col")),
                message=f"[{match.group('code')}] {match.group('message')}",
                raw_output=output,
            )
            failures.append(failure)

        return failures

    def _parse_generic(self, output: str) -> list[ParsedFailure]:
        """Generic parsing for unknown output formats."""
        failures = []

        # Look for common error patterns
        error_keywords = [
            ("SyntaxError", FailureType.SYNTAX_ERROR),
            ("ImportError", FailureType.IMPORT_ERROR),
            ("ModuleNotFoundError", FailureType.IMPORT_ERROR),
            ("AssertionError", FailureType.ASSERTION_ERROR),
            ("RuntimeError", FailureType.RUNTIME_ERROR),
            ("TimeoutError", FailureType.TIMEOUT),
            ("Error", FailureType.UNKNOWN),
            ("Failed", FailureType.UNKNOWN),
        ]

        for keyword, failure_type in error_keywords:
            if keyword.lower() in output.lower():
                failure = ParsedFailure(
                    failure_type=failure_type,
                    message=self._extract_error_message(output, keyword),
                    raw_output=output,
                )

                # Try to extract file/line info
                for match in PYTEST_FAILURE_PATTERN.finditer(output):
                    failure.file_path = match.group("file")
                    failure.line_number = int(match.group("line"))
                    break

                failures.append(failure)
                break

        return failures

    def _extract_assertion_details(
        self,
        output: str,
        failure: ParsedFailure,
    ) -> None:
        """Extract assertion error details."""
        # Look for assertion message
        match = ASSERTION_PATTERN.search(output)
        if match:
            failure.message = match.group("expr")

        # Look for expected/actual values
        match = EXPECTED_ACTUAL_PATTERN.search(output)
        if match:
            failure.expected = match.group("expected").strip()
            failure.actual = match.group("actual").strip()

    def _extract_line_number(
        self,
        output: str,
        file_path: str,
        failure: ParsedFailure,
    ) -> None:
        """Extract line number for a file from output."""
        # Look for file:line pattern
        pattern = re.compile(
            rf"{re.escape(file_path)}:(\d+)",
            re.IGNORECASE,
        )
        match = pattern.search(output)
        if match:
            failure.line_number = int(match.group(1))

    def _extract_error_message(self, output: str, keyword: str) -> str:
        """Extract error message around a keyword."""
        lines = output.split("\n")
        for i, line in enumerate(lines):
            if keyword.lower() in line.lower():
                # Return this line and the next few for context
                end = min(i + 3, len(lines))
                return "\n".join(lines[i:end]).strip()
        return ""

    def summarize(self, failures: list[ParsedFailure]) -> str:
        """Create a summary of failures for the healer.

        Args:
            failures: List of parsed failures

        Returns:
            Human-readable summary
        """
        if not failures:
            return "No failures to summarize."

        lines = [f"Found {len(failures)} failure(s):", ""]

        for i, failure in enumerate(failures[:10], 1):  # Limit to 10
            lines.append(f"{i}. [{failure.failure_type.value}]")

            if failure.test_name:
                lines.append(f"   Test: {failure.test_name}")
            if failure.file_path:
                loc = failure.file_path
                if failure.line_number:
                    loc += f":{failure.line_number}"
                lines.append(f"   Location: {loc}")
            if failure.message:
                # Truncate long messages
                msg = failure.message[:200] + "..." if len(failure.message) > 200 else failure.message
                lines.append(f"   Message: {msg}")
            if failure.expected and failure.actual:
                lines.append(f"   Expected: {failure.expected}")
                lines.append(f"   Actual: {failure.actual}")

            lines.append("")

        if len(failures) > 10:
            lines.append(f"... and {len(failures) - 10} more failures")

        return "\n".join(lines)
