"""PR review skill - reviews pull requests."""

from __future__ import annotations

import re
from typing import Any

from ..base import Skill, SkillResult


async def review_pr_handler(args: str, context: dict[str, Any]) -> SkillResult:
    """Review a pull request.

    Steps:
    1. Get PR details using gh CLI
    2. Get PR diff
    3. Analyze changes
    4. Provide review feedback

    Args:
        args: PR number or URL
        context: Execution context with tool_broker

    Returns:
        SkillResult with review feedback
    """
    tool_broker = context.get("tool_broker")
    if not tool_broker:
        return SkillResult(
            status="failed",
            error="Tool broker not available",
        )

    # Parse PR number from args
    pr_number = _extract_pr_number(args)
    if not pr_number:
        return SkillResult(
            status="failed",
            error="Please provide a PR number (e.g., /review-pr 123)",
        )

    try:
        # Step 1: Get PR details
        pr_result = await tool_broker.execute("bash", {
            "command": f"gh pr view {pr_number} --json title,body,state,author,additions,deletions,files,commits",
        })

        if pr_result.get("exit_code") != 0:
            return SkillResult(
                status="failed",
                error=f"Failed to get PR details: {pr_result.get('output')}",
            )

        import json
        try:
            pr_data = json.loads(pr_result.get("output", "{}"))
        except json.JSONDecodeError:
            return SkillResult(
                status="failed",
                error="Failed to parse PR data",
            )

        # Step 2: Get PR diff
        diff_result = await tool_broker.execute("bash", {
            "command": f"gh pr diff {pr_number}",
        })

        diff_output = diff_result.get("output", "")

        # Step 3: Get PR comments
        comments_result = await tool_broker.execute("bash", {
            "command": f"gh api repos/:owner/:repo/pulls/{pr_number}/comments --jq '.[].body' 2>/dev/null | head -20",
        })

        # Step 4: Analyze and generate review
        review = _generate_review(pr_data, diff_output)

        return SkillResult(
            status="completed",
            output=review,
            metadata={
                "pr_number": pr_number,
                "title": pr_data.get("title"),
                "author": pr_data.get("author", {}).get("login"),
                "additions": pr_data.get("additions"),
                "deletions": pr_data.get("deletions"),
            },
        )

    except Exception as e:
        return SkillResult(
            status="failed",
            error=str(e),
        )


def _extract_pr_number(args: str) -> str | None:
    """Extract PR number from args.

    Args:
        args: User input (number, URL, or #number)

    Returns:
        PR number string or None
    """
    args = args.strip()

    # Direct number
    if args.isdigit():
        return args

    # #123 format
    if args.startswith("#") and args[1:].isdigit():
        return args[1:]

    # URL format
    match = re.search(r"/pull/(\d+)", args)
    if match:
        return match.group(1)

    return None


def _generate_review(pr_data: dict, diff_output: str) -> str:
    """Generate a review summary.

    Args:
        pr_data: PR metadata from GitHub
        diff_output: Diff content

    Returns:
        Formatted review text
    """
    title = pr_data.get("title", "Unknown")
    body = pr_data.get("body", "")
    additions = pr_data.get("additions", 0)
    deletions = pr_data.get("deletions", 0)
    files = pr_data.get("files", [])
    commits = pr_data.get("commits", [])

    lines = [
        f"## PR Review: {title}",
        "",
        f"**Changes:** +{additions} / -{deletions} lines across {len(files)} files",
        f"**Commits:** {len(commits)}",
        "",
    ]

    # Files changed
    if files:
        lines.append("### Files Changed")
        for f in files[:15]:
            path = f.get("path", "unknown")
            adds = f.get("additions", 0)
            dels = f.get("deletions", 0)
            lines.append(f"- `{path}` (+{adds}/-{dels})")
        if len(files) > 15:
            lines.append(f"- ... and {len(files) - 15} more files")
        lines.append("")

    # Summary
    lines.append("### Summary")
    if body:
        # Truncate long descriptions
        if len(body) > 500:
            body = body[:500] + "..."
        lines.append(body)
    else:
        lines.append("No description provided.")
    lines.append("")

    # Basic checks
    lines.append("### Checklist")
    lines.append(f"- [{'x' if body else ' '}] Has description")
    lines.append(f"- [{'x' if len(files) < 20 else ' '}] Reasonable PR size (<20 files)")
    lines.append(f"- [ ] Tests included (check manually)")
    lines.append(f"- [ ] No sensitive data exposed")
    lines.append("")

    lines.append("*Review the diff carefully for logic errors, security issues, and code quality.*")

    return "\n".join(lines)


def get_skill() -> Skill:
    """Get the review-pr skill."""
    return Skill(
        name="review-pr",
        description="Review a GitHub pull request",
        handler=review_pr_handler,
        aliases=["pr-review", "review"],
        category="git",
        requires_tools=["bash"],
    )
