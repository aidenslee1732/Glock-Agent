"""Pre-flight Checks for One-Shot Quality.

Runs syntax, lint, and type checks before finalizing code changes.
Catches errors early before they reach production.

Supports:
- Python: syntax check, ruff/flake8, mypy
- JavaScript/TypeScript: tsc, eslint
- Go: go build, go vet
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class CheckType(str, Enum):
    """Types of pre-flight checks."""
    SYNTAX = "syntax"
    LINT = "lint"
    TYPE = "type"
    FORMAT = "format"


class CheckSeverity(str, Enum):
    """Severity of check results."""
    ERROR = "error"     # Blocking - must fix
    WARNING = "warning"  # Should fix
    INFO = "info"       # Informational


@dataclass
class CheckIssue:
    """An issue found during pre-flight check."""
    check_type: CheckType
    severity: CheckSeverity
    message: str
    line: Optional[int] = None
    column: Optional[int] = None
    rule: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.check_type.value,
            "severity": self.severity.value,
            "message": self.message,
            "line": self.line,
            "column": self.column,
            "rule": self.rule,
        }


@dataclass
class PreflightResult:
    """Result of pre-flight checks."""
    passed: bool
    file_path: str
    checks_run: list[CheckType]
    issues: list[CheckIssue] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def blocking_errors(self) -> list[CheckIssue]:
        """Get blocking errors that must be fixed."""
        return [i for i in self.issues if i.severity == CheckSeverity.ERROR]

    @property
    def warnings(self) -> list[CheckIssue]:
        """Get warnings."""
        return [i for i in self.issues if i.severity == CheckSeverity.WARNING]

    def to_feedback(self) -> str:
        """Convert to feedback string for LLM context."""
        parts = []

        if self.passed:
            parts.append(f"✓ **PRE-FLIGHT PASSED** for {self.file_path}")
            if self.warnings:
                parts.append(f"  ({len(self.warnings)} warnings)")
        else:
            parts.append(f"✗ **PRE-FLIGHT FAILED** for {self.file_path}")
            parts.append("")
            parts.append("## Blocking Errors")
            for error in self.blocking_errors[:5]:  # Limit to 5
                loc = f":{error.line}" if error.line else ""
                parts.append(f"- [{error.check_type.value}]{loc}: {error.message}")

        if self.warnings and len(self.warnings) <= 3:
            parts.append("")
            parts.append("## Warnings")
            for warning in self.warnings:
                loc = f":{warning.line}" if warning.line else ""
                parts.append(f"- [{warning.check_type.value}]{loc}: {warning.message}")

        return "\n".join(parts)


class PreflightChecker:
    """Run pre-flight checks before finalizing code changes.

    Usage:
        checker = PreflightChecker(workspace_path="/path/to/project")
        result = await checker.check_file(Path("src/main.py"))
        if not result.passed:
            # Handle errors
            for error in result.blocking_errors:
                print(f"Error: {error.message}")
    """

    def __init__(
        self,
        workspace_path: Optional[str] = None,
        enable_syntax: bool = True,
        enable_lint: bool = True,
        enable_type: bool = False,  # Optional - can be slow
        timeout: float = 30.0,
    ):
        """Initialize pre-flight checker.

        Args:
            workspace_path: Path to workspace directory
            enable_syntax: Run syntax checks
            enable_lint: Run lint checks
            enable_type: Run type checks (slower)
            timeout: Timeout for each check in seconds
        """
        self.workspace_path = Path(workspace_path) if workspace_path else Path.cwd()
        self.enable_syntax = enable_syntax
        self.enable_lint = enable_lint
        self.enable_type = enable_type
        self.timeout = timeout

        # Cache tool availability
        self._tool_cache: dict[str, bool] = {}

    async def check_file(
        self,
        file_path: Path,
        content: Optional[str] = None,
    ) -> PreflightResult:
        """Run pre-flight checks on a file.

        Args:
            file_path: Path to the file
            content: Optional content to check (if not saved yet)

        Returns:
            PreflightResult with issues found
        """
        import time
        start_time = time.time()

        # Resolve path
        if not file_path.is_absolute():
            file_path = self.workspace_path / file_path

        suffix = file_path.suffix.lower()
        checks_run = []
        issues = []

        # Determine language and run appropriate checks
        if suffix == ".py":
            checks, found_issues = await self._check_python(file_path, content)
        elif suffix in (".js", ".jsx"):
            checks, found_issues = await self._check_javascript(file_path, content)
        elif suffix in (".ts", ".tsx"):
            checks, found_issues = await self._check_typescript(file_path, content)
        elif suffix == ".go":
            checks, found_issues = await self._check_go(file_path, content)
        else:
            # Unsupported language - pass
            checks = []
            found_issues = []

        checks_run.extend(checks)
        issues.extend(found_issues)

        duration_ms = int((time.time() - start_time) * 1000)

        # Determine if passed (no blocking errors)
        blocking = [i for i in issues if i.severity == CheckSeverity.ERROR]
        passed = len(blocking) == 0

        return PreflightResult(
            passed=passed,
            file_path=str(file_path),
            checks_run=checks_run,
            issues=issues,
            duration_ms=duration_ms,
        )

    async def check_content(
        self,
        content: str,
        language: str,
        file_name: str = "temp_file",
    ) -> PreflightResult:
        """Check content without a file.

        Creates a temporary file for checking.

        Args:
            content: Code content to check
            language: Programming language
            file_name: Name to use for temp file

        Returns:
            PreflightResult
        """
        suffix_map = {
            "python": ".py",
            "javascript": ".js",
            "typescript": ".ts",
            "go": ".go",
        }

        suffix = suffix_map.get(language, ".txt")

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=suffix,
            delete=False,
        ) as f:
            f.write(content)
            temp_path = Path(f.name)

        try:
            return await self.check_file(temp_path, content)
        finally:
            temp_path.unlink(missing_ok=True)

    async def _check_python(
        self,
        file_path: Path,
        content: Optional[str] = None,
    ) -> tuple[list[CheckType], list[CheckIssue]]:
        """Run Python pre-flight checks."""
        checks = []
        issues = []

        # Syntax check
        if self.enable_syntax:
            checks.append(CheckType.SYNTAX)
            syntax_issues = await self._python_syntax_check(file_path, content)
            issues.extend(syntax_issues)

            # If syntax fails, skip other checks
            if any(i.severity == CheckSeverity.ERROR for i in syntax_issues):
                return checks, issues

        # Lint check (ruff or flake8)
        if self.enable_lint:
            checks.append(CheckType.LINT)
            lint_issues = await self._python_lint_check(file_path)
            issues.extend(lint_issues)

        # Type check (mypy)
        if self.enable_type:
            checks.append(CheckType.TYPE)
            type_issues = await self._python_type_check(file_path)
            issues.extend(type_issues)

        return checks, issues

    async def _python_syntax_check(
        self,
        file_path: Path,
        content: Optional[str] = None,
    ) -> list[CheckIssue]:
        """Check Python syntax."""
        issues = []

        # Use ast to check syntax
        code = content if content else file_path.read_text()

        try:
            import ast
            ast.parse(code)
        except SyntaxError as e:
            issues.append(CheckIssue(
                check_type=CheckType.SYNTAX,
                severity=CheckSeverity.ERROR,
                message=str(e.msg),
                line=e.lineno,
                column=e.offset,
            ))

        return issues

    async def _python_lint_check(self, file_path: Path) -> list[CheckIssue]:
        """Run Python linter (ruff or flake8)."""
        issues = []

        # Try ruff first (faster)
        if await self._check_tool("ruff"):
            cmd = ["ruff", "check", str(file_path), "--output-format=json"]
            output = await self._run_check(cmd)
            if output:
                issues.extend(self._parse_ruff_output(output))
            return issues

        # Fall back to flake8
        if await self._check_tool("flake8"):
            cmd = ["flake8", str(file_path), "--format=json"]
            output = await self._run_check(cmd)
            if output:
                issues.extend(self._parse_flake8_output(output))
            return issues

        return issues

    def _parse_ruff_output(self, output: str) -> list[CheckIssue]:
        """Parse ruff JSON output."""
        import json
        issues = []

        try:
            data = json.loads(output)
            for item in data:
                severity = (
                    CheckSeverity.ERROR if item.get("code", "").startswith("E")
                    else CheckSeverity.WARNING
                )
                issues.append(CheckIssue(
                    check_type=CheckType.LINT,
                    severity=severity,
                    message=item.get("message", ""),
                    line=item.get("location", {}).get("row"),
                    column=item.get("location", {}).get("column"),
                    rule=item.get("code"),
                ))
        except json.JSONDecodeError:
            pass

        return issues

    def _parse_flake8_output(self, output: str) -> list[CheckIssue]:
        """Parse flake8 output."""
        import re
        issues = []

        for line in output.strip().split("\n"):
            match = re.match(r".*?:(\d+):(\d+): (\w+) (.*)", line)
            if match:
                line_num, col, code, message = match.groups()
                severity = (
                    CheckSeverity.ERROR if code.startswith("E")
                    else CheckSeverity.WARNING
                )
                issues.append(CheckIssue(
                    check_type=CheckType.LINT,
                    severity=severity,
                    message=message,
                    line=int(line_num),
                    column=int(col),
                    rule=code,
                ))

        return issues

    async def _python_type_check(self, file_path: Path) -> list[CheckIssue]:
        """Run mypy type check."""
        issues = []

        if not await self._check_tool("mypy"):
            return issues

        cmd = [
            "mypy",
            str(file_path),
            "--ignore-missing-imports",
            "--no-error-summary",
        ]
        output = await self._run_check(cmd)
        if output:
            issues.extend(self._parse_mypy_output(output))

        return issues

    def _parse_mypy_output(self, output: str) -> list[CheckIssue]:
        """Parse mypy output."""
        import re
        issues = []

        for line in output.strip().split("\n"):
            match = re.match(r".*?:(\d+): (error|warning|note): (.*)", line)
            if match:
                line_num, level, message = match.groups()
                severity_map = {
                    "error": CheckSeverity.ERROR,
                    "warning": CheckSeverity.WARNING,
                    "note": CheckSeverity.INFO,
                }
                issues.append(CheckIssue(
                    check_type=CheckType.TYPE,
                    severity=severity_map.get(level, CheckSeverity.WARNING),
                    message=message,
                    line=int(line_num),
                ))

        return issues

    async def _check_javascript(
        self,
        file_path: Path,
        content: Optional[str] = None,
    ) -> tuple[list[CheckType], list[CheckIssue]]:
        """Run JavaScript pre-flight checks."""
        checks = []
        issues = []

        # ESLint for lint
        if self.enable_lint and await self._check_tool("eslint"):
            checks.append(CheckType.LINT)
            cmd = ["eslint", str(file_path), "--format=json"]
            output = await self._run_check(cmd)
            if output:
                issues.extend(self._parse_eslint_output(output))

        return checks, issues

    async def _check_typescript(
        self,
        file_path: Path,
        content: Optional[str] = None,
    ) -> tuple[list[CheckType], list[CheckIssue]]:
        """Run TypeScript pre-flight checks."""
        checks = []
        issues = []

        # TypeScript compiler for type checking
        if self.enable_type and await self._check_tool("tsc"):
            checks.append(CheckType.TYPE)
            cmd = ["tsc", "--noEmit", str(file_path)]
            output = await self._run_check(cmd)
            if output:
                issues.extend(self._parse_tsc_output(output))

        # ESLint for lint
        if self.enable_lint and await self._check_tool("eslint"):
            checks.append(CheckType.LINT)
            cmd = ["eslint", str(file_path), "--format=json"]
            output = await self._run_check(cmd)
            if output:
                issues.extend(self._parse_eslint_output(output))

        return checks, issues

    def _parse_eslint_output(self, output: str) -> list[CheckIssue]:
        """Parse ESLint JSON output."""
        import json
        issues = []

        try:
            data = json.loads(output)
            for file_result in data:
                for msg in file_result.get("messages", []):
                    severity = (
                        CheckSeverity.ERROR if msg.get("severity") == 2
                        else CheckSeverity.WARNING
                    )
                    issues.append(CheckIssue(
                        check_type=CheckType.LINT,
                        severity=severity,
                        message=msg.get("message", ""),
                        line=msg.get("line"),
                        column=msg.get("column"),
                        rule=msg.get("ruleId"),
                    ))
        except json.JSONDecodeError:
            pass

        return issues

    def _parse_tsc_output(self, output: str) -> list[CheckIssue]:
        """Parse TypeScript compiler output."""
        import re
        issues = []

        for line in output.strip().split("\n"):
            match = re.match(r".*?\((\d+),(\d+)\): (error|warning) (\w+): (.*)", line)
            if match:
                line_num, col, level, code, message = match.groups()
                severity = (
                    CheckSeverity.ERROR if level == "error"
                    else CheckSeverity.WARNING
                )
                issues.append(CheckIssue(
                    check_type=CheckType.TYPE,
                    severity=severity,
                    message=message,
                    line=int(line_num),
                    column=int(col),
                    rule=code,
                ))

        return issues

    async def _check_go(
        self,
        file_path: Path,
        content: Optional[str] = None,
    ) -> tuple[list[CheckType], list[CheckIssue]]:
        """Run Go pre-flight checks."""
        checks = []
        issues = []

        # Syntax check via gofmt
        if self.enable_syntax and await self._check_tool("gofmt"):
            checks.append(CheckType.SYNTAX)
            cmd = ["gofmt", "-e", str(file_path)]
            output = await self._run_check(cmd, capture_stderr=True)
            if output:
                issues.extend(self._parse_gofmt_output(output))

        # go vet for lint-like checks
        if self.enable_lint and await self._check_tool("go"):
            checks.append(CheckType.LINT)
            cmd = ["go", "vet", str(file_path)]
            output = await self._run_check(cmd, capture_stderr=True)
            if output:
                issues.extend(self._parse_go_vet_output(output))

        return checks, issues

    def _parse_gofmt_output(self, output: str) -> list[CheckIssue]:
        """Parse gofmt error output."""
        import re
        issues = []

        for line in output.strip().split("\n"):
            match = re.match(r".*?:(\d+):(\d+): (.*)", line)
            if match:
                line_num, col, message = match.groups()
                issues.append(CheckIssue(
                    check_type=CheckType.SYNTAX,
                    severity=CheckSeverity.ERROR,
                    message=message,
                    line=int(line_num),
                    column=int(col),
                ))

        return issues

    def _parse_go_vet_output(self, output: str) -> list[CheckIssue]:
        """Parse go vet output."""
        import re
        issues = []

        for line in output.strip().split("\n"):
            match = re.match(r".*?:(\d+):(\d+): (.*)", line)
            if match:
                line_num, col, message = match.groups()
                issues.append(CheckIssue(
                    check_type=CheckType.LINT,
                    severity=CheckSeverity.WARNING,
                    message=message,
                    line=int(line_num),
                    column=int(col),
                ))

        return issues

    async def _check_tool(self, tool: str) -> bool:
        """Check if a tool is available."""
        if tool in self._tool_cache:
            return self._tool_cache[tool]

        try:
            result = subprocess.run(
                ["which", tool],
                capture_output=True,
                timeout=5,
            )
            available = result.returncode == 0
        except Exception:
            available = False

        self._tool_cache[tool] = available
        return available

    async def _run_check(
        self,
        cmd: list[str],
        capture_stderr: bool = False,
    ) -> Optional[str]:
        """Run a check command."""
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE if capture_stderr else asyncio.subprocess.DEVNULL,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout,
            )

            if capture_stderr:
                return (stdout.decode() + stderr.decode()).strip()
            return stdout.decode().strip()

        except asyncio.TimeoutError:
            logger.warning(f"Check timed out: {cmd[0]}")
            return None
        except Exception as e:
            logger.debug(f"Check failed: {cmd[0]}: {e}")
            return None
