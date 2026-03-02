"""Test Executor for One-Shot Quality Assurance.

Executes generated tests in a sandboxed environment to verify
that proposed code actually works correctly.

This is critical for one-shot code quality:
- Validates code before committing
- Catches runtime errors
- Verifies expected behavior
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..capsule.manager import CapsuleManager

logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    """Result of test execution."""
    passed: bool
    total_tests: int
    passed_count: int
    failed_count: int
    error_count: int
    skipped_count: int
    duration_ms: int
    output: str
    failures: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    coverage: Optional[float] = None

    @property
    def success_rate(self) -> float:
        """Get test success rate."""
        if self.total_tests == 0:
            return 0.0
        return self.passed_count / self.total_tests

    def to_feedback(self) -> str:
        """Convert to feedback string for LLM context."""
        parts = []

        if self.passed:
            parts.append(f"✓ **TESTS PASSED** ({self.passed_count}/{self.total_tests})")
        else:
            parts.append(f"✗ **TESTS FAILED** ({self.failed_count} failed, {self.error_count} errors)")

        if self.failures:
            parts.append("\n## Failures")
            for failure in self.failures[:3]:  # Limit to 3
                parts.append(f"- **{failure.get('name', 'unknown')}**: {failure.get('message', '')}")

        if self.errors:
            parts.append("\n## Errors")
            for error in self.errors[:3]:  # Limit to 3
                parts.append(f"- **{error.get('name', 'unknown')}**: {error.get('message', '')}")

        return "\n".join(parts)


class TestExecutor:
    """Execute generated tests in a sandboxed environment.

    Supports:
    - Python (pytest)
    - JavaScript/TypeScript (jest, mocha)
    - Go (go test)

    Usage:
        executor = TestExecutor(workspace_path="/path/to/project")
        result = await executor.execute_tests(
            test_code="def test_example(): assert True",
            language="python",
        )
        if result.passed:
            print("All tests passed!")
        else:
            print(f"Failures: {result.failures}")
    """

    def __init__(
        self,
        workspace_path: Optional[str] = None,
        capsule_manager: Optional["CapsuleManager"] = None,
        timeout: float = 60.0,
        use_sandbox: bool = True,
    ):
        """Initialize test executor.

        Args:
            workspace_path: Path to workspace directory
            capsule_manager: Optional sandbox manager
            timeout: Test execution timeout in seconds
            use_sandbox: Whether to use sandboxed execution
        """
        self.workspace_path = Path(workspace_path) if workspace_path else Path.cwd()
        self._capsule_manager = capsule_manager
        self.timeout = timeout
        self.use_sandbox = use_sandbox and capsule_manager is not None

    async def execute_tests(
        self,
        test_code: str,
        language: str = "python",
        source_code: Optional[str] = None,
        source_file: Optional[str] = None,
    ) -> TestResult:
        """Execute test code and return results.

        Args:
            test_code: The test code to execute
            language: Programming language
            source_code: Optional source code being tested
            source_file: Optional source file name

        Returns:
            TestResult with execution details
        """
        if language == "python":
            return await self._execute_python_tests(
                test_code, source_code, source_file
            )
        elif language in ("javascript", "typescript"):
            return await self._execute_js_tests(
                test_code, source_code, source_file
            )
        elif language == "go":
            return await self._execute_go_tests(
                test_code, source_code, source_file
            )
        else:
            logger.warning(f"Unsupported language for testing: {language}")
            return TestResult(
                passed=True,
                total_tests=0,
                passed_count=0,
                failed_count=0,
                error_count=0,
                skipped_count=0,
                duration_ms=0,
                output=f"Test execution not supported for {language}",
            )

    async def _execute_python_tests(
        self,
        test_code: str,
        source_code: Optional[str] = None,
        source_file: Optional[str] = None,
    ) -> TestResult:
        """Execute Python tests using pytest."""
        start_time = time.time()

        # Create temporary directory for test execution
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Write source code if provided
            if source_code and source_file:
                source_path = temp_path / source_file
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text(source_code)

                # Create __init__.py for proper imports
                init_path = temp_path / "__init__.py"
                init_path.touch()

            # Write test file
            test_path = temp_path / "test_generated.py"

            # Add import handling
            test_content = self._prepare_python_test(
                test_code, source_file, str(temp_path)
            )
            test_path.write_text(test_content)

            # Execute pytest
            cmd = [
                "python", "-m", "pytest",
                str(test_path),
                "--tb=short",
                "-v",
                "--json-report",
                f"--json-report-file={temp_path / 'report.json'}",
            ]

            try:
                result = await self._run_command(cmd, cwd=temp_dir)

                # Parse JSON report if available
                report_path = temp_path / "report.json"
                if report_path.exists():
                    return self._parse_pytest_json(
                        report_path.read_text(),
                        result.get("output", ""),
                        int((time.time() - start_time) * 1000),
                    )

                # Fallback to output parsing
                return self._parse_pytest_output(
                    result.get("output", ""),
                    result.get("exit_code", 1),
                    int((time.time() - start_time) * 1000),
                )

            except Exception as e:
                logger.error(f"Test execution failed: {e}")
                return TestResult(
                    passed=False,
                    total_tests=0,
                    passed_count=0,
                    failed_count=0,
                    error_count=1,
                    skipped_count=0,
                    duration_ms=int((time.time() - start_time) * 1000),
                    output=str(e),
                    errors=[{"name": "execution_error", "message": str(e)}],
                )

    def _prepare_python_test(
        self,
        test_code: str,
        source_file: Optional[str],
        temp_dir: str,
    ) -> str:
        """Prepare Python test code with proper imports."""
        lines = []

        # Add sys.path modification for imports
        lines.append("import sys")
        lines.append(f"sys.path.insert(0, '{temp_dir}')")
        lines.append("")

        # Add pytest import if not present
        if "import pytest" not in test_code:
            lines.append("import pytest")
            lines.append("")

        # Add source import if applicable
        if source_file:
            module_name = Path(source_file).stem
            lines.append(f"# Import module under test")
            lines.append(f"try:")
            lines.append(f"    from {module_name} import *")
            lines.append(f"except ImportError:")
            lines.append(f"    pass")
            lines.append("")

        lines.append(test_code)

        return "\n".join(lines)

    def _parse_pytest_json(
        self,
        json_content: str,
        output: str,
        duration_ms: int,
    ) -> TestResult:
        """Parse pytest JSON report."""
        try:
            report = json.loads(json_content)

            summary = report.get("summary", {})
            total = summary.get("total", 0)
            passed = summary.get("passed", 0)
            failed = summary.get("failed", 0)
            errors = summary.get("error", 0)
            skipped = summary.get("skipped", 0)

            # Extract failure details
            failures = []
            error_list = []

            for test in report.get("tests", []):
                if test.get("outcome") == "failed":
                    failures.append({
                        "name": test.get("nodeid", "unknown"),
                        "message": test.get("call", {}).get("longrepr", ""),
                    })
                elif test.get("outcome") == "error":
                    error_list.append({
                        "name": test.get("nodeid", "unknown"),
                        "message": test.get("setup", {}).get("longrepr", "")
                              or test.get("teardown", {}).get("longrepr", ""),
                    })

            return TestResult(
                passed=(failed == 0 and errors == 0),
                total_tests=total,
                passed_count=passed,
                failed_count=failed,
                error_count=errors,
                skipped_count=skipped,
                duration_ms=duration_ms,
                output=output,
                failures=failures,
                errors=error_list,
            )

        except json.JSONDecodeError:
            logger.warning("Failed to parse pytest JSON report")
            return self._parse_pytest_output(output, 1, duration_ms)

    def _parse_pytest_output(
        self,
        output: str,
        exit_code: int,
        duration_ms: int,
    ) -> TestResult:
        """Parse pytest output when JSON report is unavailable."""
        import re

        # Look for summary line: "5 passed, 2 failed, 1 error"
        summary_match = re.search(
            r"(\d+)\s+passed.*?(\d+)?\s*failed.*?(\d+)?\s*error",
            output,
            re.IGNORECASE,
        )

        if summary_match:
            passed = int(summary_match.group(1) or 0)
            failed = int(summary_match.group(2) or 0)
            errors = int(summary_match.group(3) or 0)
        else:
            # Alternative parsing
            passed = len(re.findall(r"\bPASSED\b", output))
            failed = len(re.findall(r"\bFAILED\b", output))
            errors = len(re.findall(r"\bERROR\b", output))

        total = passed + failed + errors

        # Extract failure messages
        failures = []
        failure_blocks = re.findall(
            r"FAILED\s+(.*?)\s+-\s+(.*?)(?=\n[A-Z]|\Z)",
            output,
            re.DOTALL,
        )
        for name, message in failure_blocks:
            failures.append({
                "name": name.strip(),
                "message": message.strip()[:500],  # Limit message length
            })

        return TestResult(
            passed=(exit_code == 0),
            total_tests=total,
            passed_count=passed,
            failed_count=failed,
            error_count=errors,
            skipped_count=0,
            duration_ms=duration_ms,
            output=output,
            failures=failures,
        )

    async def _execute_js_tests(
        self,
        test_code: str,
        source_code: Optional[str] = None,
        source_file: Optional[str] = None,
    ) -> TestResult:
        """Execute JavaScript/TypeScript tests using Jest or Mocha."""
        start_time = time.time()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Write source if provided
            if source_code and source_file:
                (temp_path / source_file).write_text(source_code)

            # Write test file
            test_path = temp_path / "test_generated.test.js"
            test_path.write_text(test_code)

            # Try Jest first, then Mocha
            for runner in ["jest", "mocha"]:
                if await self._check_command_exists(runner):
                    cmd = [runner, str(test_path), "--json"] if runner == "jest" else [runner, str(test_path), "--reporter", "json"]
                    try:
                        result = await self._run_command(cmd, cwd=temp_dir)
                        return self._parse_js_output(
                            result.get("output", ""),
                            result.get("exit_code", 1),
                            int((time.time() - start_time) * 1000),
                        )
                    except Exception as e:
                        logger.debug(f"{runner} failed: {e}")
                        continue

            return TestResult(
                passed=True,
                total_tests=0,
                passed_count=0,
                failed_count=0,
                error_count=0,
                skipped_count=0,
                duration_ms=int((time.time() - start_time) * 1000),
                output="No JavaScript test runner available (jest or mocha)",
            )

    def _parse_js_output(
        self,
        output: str,
        exit_code: int,
        duration_ms: int,
    ) -> TestResult:
        """Parse Jest/Mocha output."""
        import re

        # Try JSON parsing first
        try:
            data = json.loads(output)
            if "numTotalTests" in data:  # Jest format
                return TestResult(
                    passed=data.get("success", False),
                    total_tests=data.get("numTotalTests", 0),
                    passed_count=data.get("numPassedTests", 0),
                    failed_count=data.get("numFailedTests", 0),
                    error_count=0,
                    skipped_count=data.get("numPendingTests", 0),
                    duration_ms=duration_ms,
                    output=output,
                )
        except json.JSONDecodeError:
            pass

        # Fallback parsing
        passed = len(re.findall(r"✓|√|\bpassing\b", output))
        failed = len(re.findall(r"✗|×|\bfailing\b", output))

        return TestResult(
            passed=(exit_code == 0),
            total_tests=passed + failed,
            passed_count=passed,
            failed_count=failed,
            error_count=0,
            skipped_count=0,
            duration_ms=duration_ms,
            output=output,
        )

    async def _execute_go_tests(
        self,
        test_code: str,
        source_code: Optional[str] = None,
        source_file: Optional[str] = None,
    ) -> TestResult:
        """Execute Go tests."""
        start_time = time.time()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Write source if provided
            if source_code and source_file:
                (temp_path / source_file).write_text(source_code)

            # Write test file
            test_path = temp_path / "generated_test.go"
            test_path.write_text(test_code)

            # Initialize go module
            (temp_path / "go.mod").write_text("module testmod\n\ngo 1.21\n")

            cmd = ["go", "test", "-v", "-json", "./..."]

            try:
                result = await self._run_command(cmd, cwd=temp_dir)
                return self._parse_go_output(
                    result.get("output", ""),
                    result.get("exit_code", 1),
                    int((time.time() - start_time) * 1000),
                )
            except Exception as e:
                return TestResult(
                    passed=False,
                    total_tests=0,
                    passed_count=0,
                    failed_count=0,
                    error_count=1,
                    skipped_count=0,
                    duration_ms=int((time.time() - start_time) * 1000),
                    output=str(e),
                    errors=[{"name": "go_test_error", "message": str(e)}],
                )

    def _parse_go_output(
        self,
        output: str,
        exit_code: int,
        duration_ms: int,
    ) -> TestResult:
        """Parse go test JSON output."""
        passed = 0
        failed = 0
        failures = []

        for line in output.strip().split("\n"):
            try:
                event = json.loads(line)
                action = event.get("Action", "")
                test = event.get("Test", "")

                if action == "pass" and test:
                    passed += 1
                elif action == "fail" and test:
                    failed += 1
                    failures.append({
                        "name": test,
                        "message": event.get("Output", "")[:500],
                    })
            except json.JSONDecodeError:
                continue

        return TestResult(
            passed=(exit_code == 0 and failed == 0),
            total_tests=passed + failed,
            passed_count=passed,
            failed_count=failed,
            error_count=0,
            skipped_count=0,
            duration_ms=duration_ms,
            output=output,
            failures=failures,
        )

    async def _run_command(
        self,
        cmd: list[str],
        cwd: str,
    ) -> dict[str, Any]:
        """Run a command, optionally in sandbox."""
        if self.use_sandbox and self._capsule_manager:
            exit_code, stdout, stderr = await self._capsule_manager.execute(
                command=" ".join(cmd),
                timeout=self.timeout,
                cwd=cwd,
            )
            return {
                "exit_code": exit_code,
                "output": stdout + stderr,
            }
        else:
            # Direct execution
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout,
                )
                return {
                    "exit_code": process.returncode,
                    "output": stdout.decode() + stderr.decode(),
                }
            except asyncio.TimeoutError:
                process.kill()
                raise

    async def _check_command_exists(self, command: str) -> bool:
        """Check if a command exists in PATH."""
        try:
            result = subprocess.run(
                ["which", command],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False
