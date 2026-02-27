"""PR creation skill - creates pull requests."""

from __future__ import annotations

import json
from typing import Any

from ..base import Skill, SkillResult


async def create_pr_handler(args: str, context: dict[str, Any]) -> SkillResult:
    """Create a pull request.

    Steps:
    1. Check git status for uncommitted changes
    2. Get commits since base branch
    3. Generate PR title and body
    4. Push branch if needed
    5. Create PR using gh CLI

    Args:
        args: Optional PR title
        context: Execution context with tool_broker

    Returns:
        SkillResult with PR URL
    """
    tool_broker = context.get("tool_broker")
    if not tool_broker:
        return SkillResult(
            status="failed",
            error="Tool broker not available",
        )

    try:
        # Step 1: Check for uncommitted changes
        status_result = await tool_broker.execute("bash", {
            "command": "git status --porcelain",
        })

        if status_result.get("output", "").strip():
            return SkillResult(
                status="failed",
                error="You have uncommitted changes. Please commit or stash them first.",
            )

        # Step 2: Get current branch
        branch_result = await tool_broker.execute("bash", {
            "command": "git branch --show-current",
        })

        current_branch = branch_result.get("output", "").strip()
        if not current_branch:
            return SkillResult(
                status="failed",
                error="Could not determine current branch",
            )

        if current_branch in ("main", "master"):
            return SkillResult(
                status="failed",
                error="Cannot create PR from main/master branch. Please create a feature branch.",
            )

        # Step 3: Detect base branch
        base_result = await tool_broker.execute("bash", {
            "command": "git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}' || echo 'main'",
        })
        base_branch = base_result.get("output", "main").strip()

        # Step 4: Get commits since base
        log_result = await tool_broker.execute("bash", {
            "command": f"git log {base_branch}..HEAD --oneline 2>/dev/null || git log -10 --oneline",
        })

        commits = log_result.get("output", "").strip().split("\n")
        commits = [c for c in commits if c.strip()]

        if not commits:
            return SkillResult(
                status="failed",
                error=f"No commits found between {base_branch} and {current_branch}",
            )

        # Step 5: Get diff stats
        diff_result = await tool_broker.execute("bash", {
            "command": f"git diff {base_branch}...HEAD --stat 2>/dev/null || git diff HEAD~{len(commits)}..HEAD --stat",
        })

        diff_stats = diff_result.get("output", "").strip()

        # Step 6: Generate PR title and body
        pr_title = args.strip() if args else _generate_pr_title(commits, current_branch)
        pr_body = _generate_pr_body(commits, diff_stats)

        # Step 7: Check if branch is pushed
        tracking_result = await tool_broker.execute("bash", {
            "command": f"git config --get branch.{current_branch}.remote 2>/dev/null || echo ''",
        })

        remote = tracking_result.get("output", "").strip()

        if not remote:
            # Push branch
            push_result = await tool_broker.execute("bash", {
                "command": f"git push -u origin {current_branch}",
            })

            if push_result.get("exit_code") != 0:
                return SkillResult(
                    status="failed",
                    error=f"Failed to push branch: {push_result.get('output')}",
                )

        # Step 8: Create PR using gh CLI with heredoc
        pr_cmd = f'''gh pr create --title "{pr_title}" --base {base_branch} --body "$(cat <<'EOF'
{pr_body}
EOF
)"'''

        pr_result = await tool_broker.execute("bash", {
            "command": pr_cmd,
        })

        if pr_result.get("exit_code") != 0:
            output = pr_result.get("output", "")
            if "already exists" in output.lower():
                # Get existing PR URL
                existing_result = await tool_broker.execute("bash", {
                    "command": f"gh pr view {current_branch} --json url --jq '.url'",
                })
                pr_url = existing_result.get("output", "").strip()
                return SkillResult(
                    status="completed",
                    output=f"PR already exists: {pr_url}",
                    metadata={"pr_url": pr_url, "existing": True},
                )

            return SkillResult(
                status="failed",
                error=f"Failed to create PR: {output}",
            )

        # Extract PR URL from output
        pr_url = pr_result.get("output", "").strip()

        return SkillResult(
            status="completed",
            output=f"PR created: {pr_url}",
            metadata={
                "pr_url": pr_url,
                "title": pr_title,
                "base_branch": base_branch,
                "commits": len(commits),
            },
        )

    except Exception as e:
        return SkillResult(
            status="failed",
            error=str(e),
        )


def _generate_pr_title(commits: list[str], branch: str) -> str:
    """Generate PR title from commits or branch name.

    Args:
        commits: List of commit messages
        branch: Branch name

    Returns:
        Generated title
    """
    # If single commit, use its message
    if len(commits) == 1:
        # Extract message part (after hash)
        parts = commits[0].split(" ", 1)
        if len(parts) > 1:
            return parts[1][:70]

    # Use branch name
    # Convert feature/add-auth -> Add auth
    title = branch.replace("-", " ").replace("_", " ")
    for prefix in ("feature/", "fix/", "bugfix/", "hotfix/", "chore/"):
        if title.startswith(prefix):
            title = title[len(prefix):]
            break

    # Capitalize first letter
    if title:
        title = title[0].upper() + title[1:]

    return title[:70]


def _generate_pr_body(commits: list[str], diff_stats: str) -> str:
    """Generate PR body.

    Args:
        commits: List of commit messages
        diff_stats: Diff statistics

    Returns:
        Formatted PR body
    """
    lines = [
        "## Summary",
        "",
    ]

    # Add commit summaries
    for commit in commits[:5]:
        parts = commit.split(" ", 1)
        if len(parts) > 1:
            lines.append(f"- {parts[1]}")
        else:
            lines.append(f"- {commit}")

    if len(commits) > 5:
        lines.append(f"- ... and {len(commits) - 5} more commits")

    lines.extend([
        "",
        "## Test plan",
        "- [ ] Automated tests pass",
        "- [ ] Manual testing completed",
        "- [ ] Documentation updated (if applicable)",
        "",
        "🤖 Generated with [Glock](https://github.com/glock)",
    ])

    return "\n".join(lines)


def get_skill() -> Skill:
    """Get the create-pr skill."""
    return Skill(
        name="pr",
        description="Create a GitHub pull request",
        handler=create_pr_handler,
        aliases=["create-pr", "pull-request"],
        category="git",
        requires_tools=["bash"],
    )
