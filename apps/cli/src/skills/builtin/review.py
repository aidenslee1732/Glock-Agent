"""Code review skill - review code changes for issues.

Uses the council system to analyze code changes from multiple perspectives:
- Security vulnerabilities
- Performance issues
- Maintainability concerns
- Best practice violations
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

from ..base import Skill, SkillResult

logger = logging.getLogger(__name__)


async def review_handler(args: str, context: dict[str, Any]) -> SkillResult:
    """Review code changes.

    Args:
        args: Optional git ref or file path to review
        context: Execution context with workspace_dir

    Returns:
        SkillResult with review findings
    """
    workspace_path = Path(context.get("workspace_dir", "."))

    # Determine what to review
    target = args.strip() if args.strip() else None

    # Get the diff to review
    diff = await _get_diff(workspace_path, target)

    if not diff:
        return SkillResult(
            status="completed",
            output="No changes to review. Stage changes or provide a git ref.",
            metadata={"has_changes": False},
        )

    # Format the review request
    review_lines = [
        "## Code Review Request",
        "",
        "Please review the following code changes:",
        "",
        "```diff",
        diff[:10000],  # Limit diff size
        "```",
        "",
    ]

    if len(diff) > 10000:
        review_lines.append(f"*(diff truncated - {len(diff)} total characters)*")
        review_lines.append("")

    # Add review guidelines
    review_lines.extend([
        "### Review Focus Areas:",
        "1. **Security**: Look for vulnerabilities, injection risks, hardcoded secrets",
        "2. **Performance**: Check for inefficient algorithms, unnecessary operations",
        "3. **Maintainability**: Assess code clarity, naming, documentation",
        "4. **Best Practices**: Verify adherence to coding standards",
        "",
        "### Please provide:",
        "- Summary of changes",
        "- Issues found (if any)",
        "- Suggestions for improvement",
        "- Overall assessment (approve/request changes)",
    ])

    output = "\n".join(review_lines)

    return SkillResult(
        status="completed",
        output=output,
        metadata={
            "has_changes": True,
            "diff_length": len(diff),
            "target": target or "staged/working",
        },
    )


async def review_file_handler(args: str, context: dict[str, Any]) -> SkillResult:
    """Review a specific file.

    Args:
        args: File path to review
        context: Execution context

    Returns:
        SkillResult with file review
    """
    if not args.strip():
        return SkillResult(
            status="failed",
            error="Please provide a file path to review.",
        )

    workspace_path = Path(context.get("workspace_dir", "."))
    file_path = workspace_path / args.strip()

    if not file_path.exists():
        return SkillResult(
            status="failed",
            error=f"File not found: {file_path}",
        )

    # Read file content
    try:
        content = file_path.read_text()
    except Exception as e:
        return SkillResult(
            status="failed",
            error=f"Failed to read file: {e}",
        )

    # Determine language
    suffix = file_path.suffix.lower()
    language_map = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "javascript",
        ".tsx": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".cpp": "cpp",
        ".c": "c",
    }
    language = language_map.get(suffix, "")

    # Format review request
    review_lines = [
        f"## Code Review: {args.strip()}",
        "",
        f"Please review this {language or 'code'} file:",
        "",
        f"```{language}",
        content[:15000],  # Limit content size
        "```",
        "",
    ]

    if len(content) > 15000:
        review_lines.append(f"*(file truncated - {len(content)} total characters)*")
        review_lines.append("")

    review_lines.extend([
        "### Review Focus:",
        "- Code quality and correctness",
        "- Security vulnerabilities",
        "- Performance considerations",
        "- Documentation and clarity",
        "",
    ])

    output = "\n".join(review_lines)

    return SkillResult(
        status="completed",
        output=output,
        metadata={
            "file": args.strip(),
            "language": language,
            "lines": len(content.split("\n")),
        },
    )


async def review_pr_diff_handler(args: str, context: dict[str, Any]) -> SkillResult:
    """Review a pull request by number or URL.

    Args:
        args: PR number or GitHub URL
        context: Execution context

    Returns:
        SkillResult with PR review
    """
    workspace_path = Path(context.get("workspace_dir", "."))
    pr_ref = args.strip()

    if not pr_ref:
        return SkillResult(
            status="failed",
            error="Please provide a PR number or URL.",
        )

    # Try to get PR diff using gh CLI
    try:
        result = subprocess.run(
            ["gh", "pr", "diff", pr_ref],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return SkillResult(
                status="failed",
                error=f"Failed to get PR diff: {result.stderr}",
            )

        diff = result.stdout
    except subprocess.TimeoutExpired:
        return SkillResult(
            status="failed",
            error="Timeout fetching PR diff",
        )
    except FileNotFoundError:
        return SkillResult(
            status="failed",
            error="GitHub CLI (gh) not found. Install with: https://cli.github.com/",
        )
    except Exception as e:
        return SkillResult(
            status="failed",
            error=f"Failed to fetch PR: {e}",
        )

    # Format review
    review_lines = [
        f"## Pull Request Review: {pr_ref}",
        "",
        "### Changes:",
        "",
        "```diff",
        diff[:15000],
        "```",
        "",
    ]

    if len(diff) > 15000:
        review_lines.append(f"*(diff truncated - {len(diff)} total characters)*")
        review_lines.append("")

    review_lines.extend([
        "### Review Checklist:",
        "- [ ] Changes are correct and complete",
        "- [ ] No security vulnerabilities introduced",
        "- [ ] Performance is acceptable",
        "- [ ] Code is maintainable and documented",
        "- [ ] Tests are adequate",
        "",
    ])

    output = "\n".join(review_lines)

    return SkillResult(
        status="completed",
        output=output,
        metadata={
            "pr": pr_ref,
            "diff_length": len(diff),
        },
    )


async def _get_diff(workspace: Path, target: Optional[str]) -> str:
    """Get diff for review.

    Args:
        workspace: Workspace path
        target: Optional git ref

    Returns:
        Diff string
    """
    try:
        if target:
            # Diff against specific ref
            result = subprocess.run(
                ["git", "diff", target],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            # Try staged changes first
            result = subprocess.run(
                ["git", "diff", "--cached"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=30,
            )

            # If no staged changes, try working directory
            if not result.stdout.strip():
                result = subprocess.run(
                    ["git", "diff"],
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

        if result.returncode == 0:
            return result.stdout

    except subprocess.TimeoutExpired:
        logger.warning("Git diff timed out")
    except FileNotFoundError:
        logger.warning("Git not found")
    except Exception as e:
        logger.warning(f"Failed to get diff: {e}")

    return ""


def get_skill() -> Skill:
    """Get the review skill."""
    return Skill(
        name="review",
        description="Review code changes for issues, security, and best practices",
        handler=review_handler,
        aliases=["code-review", "cr"],
        category="development",
        requires_tools=[],
    )


def get_review_file_skill() -> Skill:
    """Get the review-file skill."""
    return Skill(
        name="review-file",
        description="Review a specific file for code quality",
        handler=review_file_handler,
        aliases=["rf"],
        category="development",
        requires_tools=[],
    )


def get_review_pr_skill() -> Skill:
    """Get the review-pr-diff skill."""
    return Skill(
        name="review-pr-diff",
        description="Review a pull request diff",
        handler=review_pr_diff_handler,
        aliases=["rpd"],
        category="development",
        requires_tools=[],
    )
