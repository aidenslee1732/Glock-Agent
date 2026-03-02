"""Code Formatting Tools for Glock.

Phase 3 Feature 3.3: Code formatting integration.

Provides:
- Auto-detection of project formatters (prettier, black, gofmt, rustfmt, etc.)
- Format checking for files
- Auto-formatting with configurable options
- Integration with pre-flight checks

Supported formatters:
- Python: black, ruff format, autopep8, yapf
- JavaScript/TypeScript: prettier, eslint --fix
- Go: gofmt, goimports
- Rust: rustfmt
- Java: google-java-format
- C/C++: clang-format
- SQL: sqlfluff
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class FormatterType(str, Enum):
    """Supported formatter types."""
    BLACK = "black"
    RUFF = "ruff"
    AUTOPEP8 = "autopep8"
    YAPF = "yapf"
    PRETTIER = "prettier"
    ESLINT = "eslint"
    GOFMT = "gofmt"
    GOIMPORTS = "goimports"
    RUSTFMT = "rustfmt"
    CLANG_FORMAT = "clang-format"
    GOOGLE_JAVA_FORMAT = "google-java-format"
    SQLFLUFF = "sqlfluff"


@dataclass
class FormatterConfig:
    """Configuration for a formatter."""
    formatter_type: FormatterType
    executable: str
    check_args: list[str] = field(default_factory=list)
    format_args: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    file_patterns: list[str] = field(default_factory=list)


@dataclass
class FormatResult:
    """Result of a format operation."""
    success: bool
    file_path: str
    formatter: str
    changed: bool = False
    diff: Optional[str] = None
    error: Optional[str] = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "file_path": self.file_path,
            "formatter": self.formatter,
            "changed": self.changed,
            "diff": self.diff,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass
class FormatCheckResult:
    """Result of format checking multiple files."""
    passed: bool
    files_checked: int
    files_needing_format: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def to_feedback(self) -> str:
        """Convert to feedback string for LLM context."""
        if self.passed:
            return f"Format check passed for {self.files_checked} files."

        parts = [f"Format check failed: {len(self.files_needing_format)} files need formatting"]
        for f in self.files_needing_format[:10]:
            parts.append(f"  - {f}")
        if len(self.files_needing_format) > 10:
            parts.append(f"  ... and {len(self.files_needing_format) - 10} more")
        return "\n".join(parts)


# Default formatter configurations
FORMATTER_CONFIGS: dict[FormatterType, FormatterConfig] = {
    FormatterType.BLACK: FormatterConfig(
        formatter_type=FormatterType.BLACK,
        executable="black",
        check_args=["--check", "--diff"],
        format_args=[],
        config_files=["pyproject.toml", ".black.toml", "black.toml"],
        file_patterns=["*.py", "*.pyi"],
    ),
    FormatterType.RUFF: FormatterConfig(
        formatter_type=FormatterType.RUFF,
        executable="ruff",
        check_args=["format", "--check", "--diff"],
        format_args=["format"],
        config_files=["pyproject.toml", "ruff.toml", ".ruff.toml"],
        file_patterns=["*.py", "*.pyi"],
    ),
    FormatterType.PRETTIER: FormatterConfig(
        formatter_type=FormatterType.PRETTIER,
        executable="prettier",
        check_args=["--check"],
        format_args=["--write"],
        config_files=[".prettierrc", ".prettierrc.json", ".prettierrc.yaml", "prettier.config.js"],
        file_patterns=["*.js", "*.jsx", "*.ts", "*.tsx", "*.json", "*.md", "*.yaml", "*.yml", "*.css", "*.scss"],
    ),
    FormatterType.ESLINT: FormatterConfig(
        formatter_type=FormatterType.ESLINT,
        executable="eslint",
        check_args=["--format", "json"],
        format_args=["--fix"],
        config_files=[".eslintrc", ".eslintrc.json", ".eslintrc.js", "eslint.config.js"],
        file_patterns=["*.js", "*.jsx", "*.ts", "*.tsx"],
    ),
    FormatterType.GOFMT: FormatterConfig(
        formatter_type=FormatterType.GOFMT,
        executable="gofmt",
        check_args=["-l"],
        format_args=["-w"],
        config_files=[],
        file_patterns=["*.go"],
    ),
    FormatterType.GOIMPORTS: FormatterConfig(
        formatter_type=FormatterType.GOIMPORTS,
        executable="goimports",
        check_args=["-l"],
        format_args=["-w"],
        config_files=[],
        file_patterns=["*.go"],
    ),
    FormatterType.RUSTFMT: FormatterConfig(
        formatter_type=FormatterType.RUSTFMT,
        executable="rustfmt",
        check_args=["--check"],
        format_args=[],
        config_files=["rustfmt.toml", ".rustfmt.toml"],
        file_patterns=["*.rs"],
    ),
    FormatterType.CLANG_FORMAT: FormatterConfig(
        formatter_type=FormatterType.CLANG_FORMAT,
        executable="clang-format",
        check_args=["--dry-run", "--Werror"],
        format_args=["-i"],
        config_files=[".clang-format", "_clang-format"],
        file_patterns=["*.c", "*.h", "*.cpp", "*.hpp", "*.cc", "*.cxx"],
    ),
    FormatterType.SQLFLUFF: FormatterConfig(
        formatter_type=FormatterType.SQLFLUFF,
        executable="sqlfluff",
        check_args=["lint"],
        format_args=["fix"],
        config_files=[".sqlfluff", "pyproject.toml"],
        file_patterns=["*.sql"],
    ),
}

# Language to formatter mapping (priority order)
LANGUAGE_FORMATTERS: dict[str, list[FormatterType]] = {
    "python": [FormatterType.RUFF, FormatterType.BLACK, FormatterType.AUTOPEP8, FormatterType.YAPF],
    "javascript": [FormatterType.PRETTIER, FormatterType.ESLINT],
    "typescript": [FormatterType.PRETTIER, FormatterType.ESLINT],
    "go": [FormatterType.GOIMPORTS, FormatterType.GOFMT],
    "rust": [FormatterType.RUSTFMT],
    "c": [FormatterType.CLANG_FORMAT],
    "cpp": [FormatterType.CLANG_FORMAT],
    "sql": [FormatterType.SQLFLUFF],
}


class CodeFormatter:
    """Unified code formatter with auto-detection.

    Usage:
        formatter = CodeFormatter(workspace_path="/path/to/project")

        # Auto-detect and format a file
        result = await formatter.format_file("src/main.py")

        # Check formatting without modifying
        result = await formatter.check_format("src/main.py")

        # Format all files in directory
        results = await formatter.format_directory("src/")
    """

    def __init__(
        self,
        workspace_path: Optional[str] = None,
        timeout: float = 30.0,
    ):
        """Initialize code formatter.

        Args:
            workspace_path: Path to workspace directory
            timeout: Timeout for formatting operations in seconds
        """
        self.workspace_path = Path(workspace_path) if workspace_path else Path.cwd()
        self.timeout = timeout

        # Cache detected formatters
        self._available_formatters: dict[FormatterType, bool] = {}
        self._project_formatters: dict[str, FormatterType] = {}

    async def detect_formatters(self) -> dict[str, FormatterType]:
        """Detect available formatters for the project.

        Returns:
            Dict mapping file extensions to detected formatters
        """
        detected: dict[str, FormatterType] = {}

        for lang, formatter_types in LANGUAGE_FORMATTERS.items():
            for formatter_type in formatter_types:
                if await self._is_formatter_available(formatter_type):
                    config = FORMATTER_CONFIGS[formatter_type]
                    # Check for config files
                    has_config = any(
                        (self.workspace_path / cf).exists()
                        for cf in config.config_files
                    )

                    for pattern in config.file_patterns:
                        ext = pattern.replace("*", "")
                        if ext not in detected:
                            detected[ext] = formatter_type
                            logger.debug(f"Detected {formatter_type.value} for {ext}")

                    # If has config file, prioritize this formatter
                    if has_config:
                        for pattern in config.file_patterns:
                            ext = pattern.replace("*", "")
                            detected[ext] = formatter_type
                        break  # Use first formatter with config

        self._project_formatters = detected
        return detected

    async def _is_formatter_available(self, formatter_type: FormatterType) -> bool:
        """Check if a formatter is available."""
        if formatter_type in self._available_formatters:
            return self._available_formatters[formatter_type]

        config = FORMATTER_CONFIGS.get(formatter_type)
        if not config:
            self._available_formatters[formatter_type] = False
            return False

        available = shutil.which(config.executable) is not None
        self._available_formatters[formatter_type] = available
        return available

    def _get_formatter_for_file(self, file_path: Path) -> Optional[FormatterType]:
        """Get the appropriate formatter for a file."""
        suffix = file_path.suffix.lower()

        # Check project-detected formatters first
        if suffix in self._project_formatters:
            return self._project_formatters[suffix]

        # Fall back to language mapping
        for formatter_type, config in FORMATTER_CONFIGS.items():
            for pattern in config.file_patterns:
                if file_path.match(pattern):
                    return formatter_type

        return None

    async def format_file(
        self,
        file_path: str,
        check_only: bool = False,
    ) -> FormatResult:
        """Format a single file.

        Args:
            file_path: Path to file to format
            check_only: If True, only check formatting without modifying

        Returns:
            FormatResult with status and any diff
        """
        import time
        start_time = time.time()

        path = Path(file_path)
        if not path.is_absolute():
            path = self.workspace_path / path

        if not path.exists():
            return FormatResult(
                success=False,
                file_path=str(path),
                formatter="none",
                error=f"File not found: {path}",
            )

        formatter_type = self._get_formatter_for_file(path)
        if not formatter_type:
            return FormatResult(
                success=True,
                file_path=str(path),
                formatter="none",
                changed=False,
                error="No formatter available for this file type",
            )

        if not await self._is_formatter_available(formatter_type):
            return FormatResult(
                success=False,
                file_path=str(path),
                formatter=formatter_type.value,
                error=f"Formatter {formatter_type.value} not installed",
            )

        config = FORMATTER_CONFIGS[formatter_type]

        try:
            if check_only:
                result = await self._run_format_check(path, config)
            else:
                result = await self._run_format(path, config)

            result.duration_ms = int((time.time() - start_time) * 1000)
            return result

        except asyncio.TimeoutError:
            return FormatResult(
                success=False,
                file_path=str(path),
                formatter=formatter_type.value,
                error=f"Formatting timed out after {self.timeout}s",
                duration_ms=int((time.time() - start_time) * 1000),
            )
        except Exception as e:
            return FormatResult(
                success=False,
                file_path=str(path),
                formatter=formatter_type.value,
                error=str(e),
                duration_ms=int((time.time() - start_time) * 1000),
            )

    async def _run_format_check(
        self,
        file_path: Path,
        config: FormatterConfig,
    ) -> FormatResult:
        """Run format check on a file."""
        cmd = [config.executable] + config.check_args + [str(file_path)]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.workspace_path),
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=self.timeout,
        )

        output = stdout.decode() + stderr.decode()

        # Check result - different formatters have different exit codes
        if config.formatter_type in (FormatterType.BLACK, FormatterType.RUFF):
            # Exit code 1 means needs formatting, 0 means formatted
            needs_format = process.returncode == 1
            success = process.returncode in (0, 1)
        elif config.formatter_type == FormatterType.GOFMT:
            # gofmt -l outputs files that need formatting
            needs_format = bool(output.strip())
            success = process.returncode == 0
        elif config.formatter_type == FormatterType.PRETTIER:
            needs_format = process.returncode != 0
            success = True
        else:
            needs_format = process.returncode != 0
            success = process.returncode in (0, 1)

        return FormatResult(
            success=success,
            file_path=str(file_path),
            formatter=config.formatter_type.value,
            changed=needs_format,
            diff=output if needs_format else None,
            error=output if not success and process.returncode not in (0, 1) else None,
        )

    async def _run_format(
        self,
        file_path: Path,
        config: FormatterConfig,
    ) -> FormatResult:
        """Run formatter on a file."""
        # Read original content for diff
        original_content = file_path.read_text()

        cmd = [config.executable] + config.format_args + [str(file_path)]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.workspace_path),
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=self.timeout,
        )

        output = stdout.decode() + stderr.decode()
        success = process.returncode == 0

        if not success:
            return FormatResult(
                success=False,
                file_path=str(file_path),
                formatter=config.formatter_type.value,
                error=output,
            )

        # Check if file changed
        new_content = file_path.read_text()
        changed = original_content != new_content

        # Generate diff if changed
        diff = None
        if changed:
            import difflib
            diff_lines = difflib.unified_diff(
                original_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{file_path.name}",
                tofile=f"b/{file_path.name}",
            )
            diff = "".join(diff_lines)

        return FormatResult(
            success=True,
            file_path=str(file_path),
            formatter=config.formatter_type.value,
            changed=changed,
            diff=diff,
        )

    async def check_format(self, file_path: str) -> FormatResult:
        """Check if a file is properly formatted.

        Args:
            file_path: Path to file to check

        Returns:
            FormatResult with check status
        """
        return await self.format_file(file_path, check_only=True)

    async def format_directory(
        self,
        directory: str = ".",
        check_only: bool = False,
        file_patterns: Optional[list[str]] = None,
    ) -> FormatCheckResult:
        """Format all files in a directory.

        Args:
            directory: Directory to format
            check_only: If True, only check formatting
            file_patterns: Optional list of glob patterns to include

        Returns:
            FormatCheckResult with overall status
        """
        import time
        start_time = time.time()

        dir_path = Path(directory)
        if not dir_path.is_absolute():
            dir_path = self.workspace_path / dir_path

        # Collect files to format
        files_to_format: list[Path] = []

        if file_patterns:
            for pattern in file_patterns:
                files_to_format.extend(dir_path.rglob(pattern))
        else:
            # Use all supported patterns
            for config in FORMATTER_CONFIGS.values():
                for pattern in config.file_patterns:
                    files_to_format.extend(dir_path.rglob(pattern))

        # Remove duplicates and sort
        files_to_format = sorted(set(files_to_format))

        # Filter out common ignore patterns
        ignore_patterns = [
            "node_modules", ".git", "__pycache__", ".venv", "venv",
            "dist", "build", ".next", "target",
        ]
        files_to_format = [
            f for f in files_to_format
            if not any(ignore in str(f) for ignore in ignore_patterns)
        ]

        files_needing_format: list[str] = []
        errors: list[str] = []

        for file_path in files_to_format:
            result = await self.format_file(str(file_path), check_only=check_only)

            if not result.success:
                errors.append(f"{file_path}: {result.error}")
            elif result.changed:
                files_needing_format.append(str(file_path.relative_to(self.workspace_path)))

        duration_ms = int((time.time() - start_time) * 1000)

        return FormatCheckResult(
            passed=len(files_needing_format) == 0 and len(errors) == 0,
            files_checked=len(files_to_format),
            files_needing_format=files_needing_format,
            errors=errors,
            duration_ms=duration_ms,
        )

    def get_available_formatters(self) -> list[str]:
        """Get list of available formatters."""
        return [
            ft.value for ft, available in self._available_formatters.items()
            if available
        ]


# Tool handlers for integration with ToolBroker

async def format_file_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for formatting a single file.

    Args:
        file_path: Path to file to format
        check_only: If True, only check formatting (default: False)
        workspace: Optional workspace path

    Returns:
        Format result
    """
    file_path = args.get("file_path")
    if not file_path:
        return {"status": "error", "error": "file_path is required"}

    check_only = args.get("check_only", False)
    workspace = args.get("workspace")

    formatter = CodeFormatter(workspace_path=workspace)
    await formatter.detect_formatters()

    result = await formatter.format_file(file_path, check_only=check_only)
    return result.to_dict()


async def format_directory_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for formatting a directory.

    Args:
        directory: Directory to format (default: ".")
        check_only: If True, only check formatting (default: False)
        file_patterns: Optional list of glob patterns
        workspace: Optional workspace path

    Returns:
        Format check result
    """
    directory = args.get("directory", ".")
    check_only = args.get("check_only", False)
    file_patterns = args.get("file_patterns")
    workspace = args.get("workspace")

    formatter = CodeFormatter(workspace_path=workspace)
    await formatter.detect_formatters()

    result = await formatter.format_directory(
        directory=directory,
        check_only=check_only,
        file_patterns=file_patterns,
    )

    return {
        "passed": result.passed,
        "files_checked": result.files_checked,
        "files_needing_format": result.files_needing_format,
        "errors": result.errors,
        "duration_ms": result.duration_ms,
        "feedback": result.to_feedback(),
    }


async def detect_formatters_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for detecting available formatters.

    Args:
        workspace: Optional workspace path

    Returns:
        Dict of detected formatters
    """
    workspace = args.get("workspace")

    formatter = CodeFormatter(workspace_path=workspace)
    detected = await formatter.detect_formatters()

    return {
        "detected": {ext: ft.value for ext, ft in detected.items()},
        "available": formatter.get_available_formatters(),
    }
