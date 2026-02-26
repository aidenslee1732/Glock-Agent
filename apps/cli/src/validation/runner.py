"""Client-side validation runner.

Executes plan-defined validations (test, lint, typecheck) locally
and reports results back to the server.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ValidationStatus(str, Enum):
    """Status of a validation step."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class TestFailure:
    """A single test failure."""

    test_name: str
    file: Optional[str] = None
    line: Optional[int] = None
    expected: Optional[str] = None
    actual: Optional[str] = None
    message: Optional[str] = None


@dataclass
class ValidationStep:
    """A validation step to execute."""

    name: str
    command: str
    timeout_ms: int = 120000
    working_dir: Optional[str] = None
    env: Optional[dict[str, str]] = None


@dataclass
class ValidationResult:
    """Result of a validation step."""

    step_name: str
    status: ValidationStatus
    command: str
    output_summary: str
    failures: list[TestFailure] = field(default_factory=list)
    duration_ms: int = 0
    exit_code: Optional[int] = None
    raw_output: Optional[str] = None


class ValidationRunner:
    """Executes validation steps locally.

    The runner:
    1. Receives validation steps from the server
    2. Executes each step in sequence
    3. Parses output to extract failures
    4. Reports results back to the server
    """

    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)

        # Command templates for common validators
        self._command_templates = {
            "test": "pytest -x --tb=short",
            "lint": "ruff check .",
            "typecheck": "mypy src/",
            "format": "ruff format --check .",
            "security": "bandit -r src/",
        }

    async def run_step(self, step: ValidationStep) -> ValidationResult:
        """Run a single validation step."""
        start_time = time.time()

        # Resolve command
        command = self._resolve_command(step)

        logger.info(f"Running validation step: {step.name}")
        logger.debug(f"Command: {command}")

        try:
            # Run command
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=step.working_dir or str(self.workspace_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=step.env
            )

            # Wait with timeout
            timeout_seconds = step.timeout_ms / 1000.0
            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_seconds
                )
                output = stdout.decode("utf-8", errors="replace")
                exit_code = process.returncode

            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return ValidationResult(
                    step_name=step.name,
                    status=ValidationStatus.TIMEOUT,
                    command=command,
                    output_summary=f"Command timed out after {timeout_seconds}s",
                    duration_ms=int((time.time() - start_time) * 1000)
                )

            duration_ms = int((time.time() - start_time) * 1000)

            # Determine status
            if exit_code == 0:
                status = ValidationStatus.PASSED
            else:
                status = ValidationStatus.FAILED

            # Parse failures
            failures = self._parse_failures(step.name, output)

            # Generate summary
            summary = self._generate_summary(step.name, output, failures)

            return ValidationResult(
                step_name=step.name,
                status=status,
                command=command,
                output_summary=summary,
                failures=failures,
                duration_ms=duration_ms,
                exit_code=exit_code,
                raw_output=output
            )

        except Exception as e:
            logger.exception(f"Validation step {step.name} failed with error")
            return ValidationResult(
                step_name=step.name,
                status=ValidationStatus.ERROR,
                command=command,
                output_summary=f"Error running command: {e}",
                duration_ms=int((time.time() - start_time) * 1000)
            )

    async def run_all(self, steps: list[ValidationStep]) -> list[ValidationResult]:
        """Run all validation steps in sequence."""
        results = []

        for step in steps:
            result = await self.run_step(step)
            results.append(result)

            # Stop on first failure (fail-fast)
            if result.status in (ValidationStatus.FAILED, ValidationStatus.ERROR):
                logger.info(f"Stopping validation at {step.name} due to failure")
                break

        return results

    def _resolve_command(self, step: ValidationStep) -> str:
        """Resolve command from step or template."""
        if step.command:
            return step.command

        # Use template if available
        return self._command_templates.get(step.name, step.name)

    def _parse_failures(self, step_name: str, output: str) -> list[TestFailure]:
        """Parse test failures from output."""
        if step_name == "test" or "pytest" in output.lower():
            return self._parse_pytest_failures(output)
        elif step_name == "lint" or "ruff" in output.lower():
            return self._parse_ruff_failures(output)
        elif step_name == "typecheck" or "mypy" in output.lower():
            return self._parse_mypy_failures(output)

        return []

    def _parse_pytest_failures(self, output: str) -> list[TestFailure]:
        """Parse pytest output for failures."""
        failures = []

        # Pattern for pytest failure headers
        failure_pattern = re.compile(
            r'FAILED\s+(\S+)::([\w_]+)(?:\[.*?\])?\s*-\s*(.*)',
            re.MULTILINE
        )

        for match in failure_pattern.finditer(output):
            file_path = match.group(1)
            test_name = match.group(2)
            message = match.group(3)

            failure = TestFailure(
                test_name=test_name,
                file=file_path,
                message=message
            )
            failures.append(failure)

        # Try to extract assertion details
        assertion_pattern = re.compile(
            r'AssertionError:\s*assert\s+(.+?)\s*==\s*(.+)',
            re.MULTILINE
        )

        for i, match in enumerate(assertion_pattern.finditer(output)):
            if i < len(failures):
                failures[i].actual = match.group(1).strip()
                failures[i].expected = match.group(2).strip()

        return failures

    def _parse_ruff_failures(self, output: str) -> list[TestFailure]:
        """Parse ruff lint output for failures."""
        failures = []

        # Pattern: path/to/file.py:line:col: CODE message
        lint_pattern = re.compile(
            r'^([^\s:]+):(\d+):(\d+):\s+(\w+)\s+(.+)$',
            re.MULTILINE
        )

        for match in lint_pattern.finditer(output):
            failure = TestFailure(
                test_name=match.group(4),  # Error code
                file=match.group(1),
                line=int(match.group(2)),
                message=match.group(5)
            )
            failures.append(failure)

        return failures

    def _parse_mypy_failures(self, output: str) -> list[TestFailure]:
        """Parse mypy output for type errors."""
        failures = []

        # Pattern: path/to/file.py:line: error: message
        mypy_pattern = re.compile(
            r'^([^\s:]+):(\d+):\s*error:\s*(.+)$',
            re.MULTILINE
        )

        for match in mypy_pattern.finditer(output):
            failure = TestFailure(
                test_name="type_error",
                file=match.group(1),
                line=int(match.group(2)),
                message=match.group(3)
            )
            failures.append(failure)

        return failures

    def _generate_summary(
        self,
        step_name: str,
        output: str,
        failures: list[TestFailure]
    ) -> str:
        """Generate a brief summary of the validation result."""
        if not failures:
            # Extract any summary line from output
            lines = output.strip().split("\n")
            for line in reversed(lines):
                if "passed" in line.lower() or "ok" in line.lower():
                    return line.strip()[:200]
            return "Validation passed"

        # Summarize failures
        if len(failures) == 1:
            f = failures[0]
            return f"{f.test_name}: {f.message or 'Failed'}"

        return f"{len(failures)} failures in {step_name}"

    def get_supported_validators(self) -> list[str]:
        """Get list of supported validator names."""
        return list(self._command_templates.keys())
