"""Security scan skill - scan code for security vulnerabilities."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..base import Skill, SkillResult

logger = logging.getLogger(__name__)


async def security_scan_handler(args: str, context: dict[str, Any]) -> SkillResult:
    """Scan code for security vulnerabilities.

    Args:
        args: Optional path or glob pattern to scan
        context: Execution context with workspace_dir

    Returns:
        SkillResult with security report
    """
    from ...security.scanner import SecurityScanner, SecurityReport

    workspace_path = Path(context.get("workspace_dir", "."))

    # Parse arguments for patterns
    patterns = None
    exclude_patterns = None

    if args.strip():
        # User specified patterns
        patterns = [p.strip() for p in args.split(",")]
    else:
        # Default patterns
        patterns = ["**/*.py", "**/*.js", "**/*.ts", "**/*.jsx", "**/*.tsx"]

    # Default exclusions
    exclude_patterns = [
        "**/node_modules/**",
        "**/.git/**",
        "**/venv/**",
        "**/__pycache__/**",
        "**/dist/**",
        "**/build/**",
    ]

    try:
        # Initialize scanner
        scanner = SecurityScanner(workspace_path=workspace_path)

        # Run scan
        report = await scanner.scan_workspace(
            patterns=patterns,
            exclude_patterns=exclude_patterns,
        )

        # Format output
        output = report.format_report()

        # Determine status based on findings
        if report.summary.get("critical", 0) > 0 or report.summary.get("high", 0) > 0:
            status = "completed"
            metadata = {
                "severity": "high",
                "action_required": True,
                **report.summary,
            }
        elif report.summary.get("medium", 0) > 0:
            status = "completed"
            metadata = {
                "severity": "medium",
                "action_required": True,
                **report.summary,
            }
        else:
            status = "completed"
            metadata = {
                "severity": "low",
                "action_required": False,
                **report.summary,
            }

        return SkillResult(
            status=status,
            output=output,
            metadata=metadata,
        )

    except ImportError as e:
        return SkillResult(
            status="failed",
            error=f"Security scanner not available: {e}",
        )
    except Exception as e:
        logger.error(f"Security scan failed: {e}")
        return SkillResult(
            status="failed",
            error=f"Security scan failed: {str(e)}",
        )


async def security_scan_file_handler(args: str, context: dict[str, Any]) -> SkillResult:
    """Scan a specific file for security vulnerabilities.

    Args:
        args: File path to scan
        context: Execution context

    Returns:
        SkillResult with file-specific security report
    """
    from ...security.scanner import SecurityScanner

    if not args.strip():
        return SkillResult(
            status="failed",
            error="Please provide a file path to scan.",
        )

    workspace_path = Path(context.get("workspace_dir", "."))
    file_path = workspace_path / args.strip()

    if not file_path.exists():
        return SkillResult(
            status="failed",
            error=f"File not found: {file_path}",
        )

    try:
        scanner = SecurityScanner(workspace_path=workspace_path)
        vulnerabilities = await scanner.scan_file(file_path)

        if not vulnerabilities:
            return SkillResult(
                status="completed",
                output=f"No vulnerabilities found in {args.strip()}",
                metadata={"total": 0},
            )

        # Format output
        lines = [
            f"# Security Scan: {args.strip()}",
            "",
            f"Found {len(vulnerabilities)} potential issue(s):",
            "",
        ]

        for vuln in vulnerabilities:
            lines.extend([
                f"### [{vuln.severity.value.upper()}] {vuln.title}",
                f"**Line {vuln.line_number}:** `{vuln.code_snippet}`",
                f"**Remediation:** {vuln.remediation}",
                "",
            ])

        return SkillResult(
            status="completed",
            output="\n".join(lines),
            metadata={
                "total": len(vulnerabilities),
                "by_severity": {
                    sev: sum(1 for v in vulnerabilities if v.severity.value == sev)
                    for sev in ["critical", "high", "medium", "low", "info"]
                },
            },
        )

    except Exception as e:
        logger.error(f"Security scan failed for {args}: {e}")
        return SkillResult(
            status="failed",
            error=f"Security scan failed: {str(e)}",
        )


def get_skill() -> Skill:
    """Get the security-scan skill."""
    return Skill(
        name="security-scan",
        description="Scan code for security vulnerabilities (OWASP, hardcoded secrets, injection risks)",
        handler=security_scan_handler,
        aliases=["scan", "sec-scan", "vuln-scan"],
        category="security",
        requires_tools=[],
    )


def get_scan_file_skill() -> Skill:
    """Get the scan-file skill."""
    return Skill(
        name="scan-file",
        description="Scan a specific file for security vulnerabilities",
        handler=security_scan_file_handler,
        aliases=["sf"],
        category="security",
        requires_tools=[],
    )
