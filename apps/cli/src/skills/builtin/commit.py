"""Git commit skill - automates the commit workflow."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ..base import Skill, SkillResult


async def commit_handler(args: str, context: dict[str, Any]) -> SkillResult:
    """Execute the commit workflow.

    Steps:
    1. Run git status to see changes
    2. Run git diff to see what changed
    3. Analyze changes and generate commit message
    4. Stage files (selectively, avoiding sensitive files)
    5. Create commit with message and Co-Authored-By footer
    6. Verify success

    Args:
        args: Optional commit message override
        context: Execution context with tool_broker

    Returns:
        SkillResult with commit details
    """
    tool_broker = context.get("tool_broker")
    if not tool_broker:
        return SkillResult(
            status="failed",
            error="Tool broker not available",
        )

    try:
        # Step 1: Get git status
        status_result = await tool_broker.execute("bash", {
            "command": "git status --porcelain",
        })

        if status_result.get("exit_code") != 0:
            return SkillResult(
                status="failed",
                error=f"git status failed: {status_result.get('output')}",
            )

        status_output = status_result.get("output", "").strip()
        if not status_output:
            return SkillResult(
                status="completed",
                output="No changes to commit.",
            )

        # Parse status to get changed files
        changed_files = []
        staged_files = []
        untracked_files = []
        sensitive_files = []

        sensitive_patterns = [".env", "credentials", "secret", ".pem", ".key", "password"]

        for line in status_output.split("\n"):
            if not line.strip():
                continue

            status_code = line[:2]
            file_path = line[3:].strip()

            # Check for sensitive files
            is_sensitive = any(p in file_path.lower() for p in sensitive_patterns)
            if is_sensitive:
                sensitive_files.append(file_path)
                continue

            if status_code[0] != " " and status_code[0] != "?":
                staged_files.append(file_path)
            if status_code[1] != " ":
                changed_files.append(file_path)
            if status_code == "??":
                untracked_files.append(file_path)

        # Warn about sensitive files
        warnings = []
        if sensitive_files:
            warnings.append(f"Skipping sensitive files: {', '.join(sensitive_files)}")

        # Step 2: Get git diff
        diff_result = await tool_broker.execute("bash", {
            "command": "git diff --cached --stat && git diff --stat",
        })

        diff_output = diff_result.get("output", "")

        # Step 3: Get recent commits for style reference
        log_result = await tool_broker.execute("bash", {
            "command": "git log --oneline -5 2>/dev/null || echo 'No commits yet'",
        })

        # Step 4: Generate commit message if not provided
        commit_message = args.strip() if args else None

        if not commit_message:
            # Analyze changes to generate message
            commit_message = _generate_commit_message(
                changed_files + staged_files + untracked_files,
                diff_output,
            )

        # Step 5: Stage files (avoid using git add .)
        files_to_stage = changed_files + untracked_files
        files_to_stage = [f for f in files_to_stage if f not in sensitive_files]

        if files_to_stage:
            # Stage specific files
            stage_cmd = "git add " + " ".join(f'"{f}"' for f in files_to_stage[:50])
            stage_result = await tool_broker.execute("bash", {
                "command": stage_cmd,
            })

            if stage_result.get("exit_code") != 0:
                return SkillResult(
                    status="failed",
                    error=f"Failed to stage files: {stage_result.get('output')}",
                )

        # Step 6: Create commit with heredoc
        full_message = f"{commit_message}\n\nCo-Authored-By: Claude <noreply@anthropic.com>"

        commit_cmd = f'''git commit -m "$(cat <<'EOF'
{full_message}
EOF
)"'''

        commit_result = await tool_broker.execute("bash", {
            "command": commit_cmd,
        })

        if commit_result.get("exit_code") != 0:
            output = commit_result.get("output", "")
            if "nothing to commit" in output.lower():
                return SkillResult(
                    status="completed",
                    output="Nothing to commit (working tree clean).",
                )
            return SkillResult(
                status="failed",
                error=f"Commit failed: {output}",
            )

        # Step 7: Verify success
        verify_result = await tool_broker.execute("bash", {
            "command": "git log -1 --oneline",
        })

        output_parts = [
            f"Committed: {verify_result.get('output', '').strip()}",
            f"Files: {len(files_to_stage)} staged",
        ]

        if warnings:
            output_parts.append(f"Warnings: {'; '.join(warnings)}")

        return SkillResult(
            status="completed",
            output="\n".join(output_parts),
            metadata={
                "commit_message": commit_message,
                "files_committed": files_to_stage,
                "warnings": warnings,
            },
        )

    except Exception as e:
        return SkillResult(
            status="failed",
            error=str(e),
        )


def _generate_commit_message(files: list[str], diff_output: str) -> str:
    """Generate a commit message based on changed files.

    Args:
        files: List of changed files
        diff_output: Git diff output

    Returns:
        Generated commit message
    """
    if not files:
        return "Update files"

    # Analyze file patterns
    file_types = {}
    for f in files:
        if "/" in f:
            prefix = f.split("/")[0]
        else:
            prefix = "root"

        ext = f.split(".")[-1] if "." in f else "other"
        key = f"{prefix}/{ext}"
        file_types[key] = file_types.get(key, 0) + 1

    # Common patterns
    if any("test" in f.lower() for f in files):
        if all("test" in f.lower() for f in files):
            return "Add/update tests"
        return "Update code and tests"

    if any("readme" in f.lower() or "doc" in f.lower() for f in files):
        return "Update documentation"

    if any(".github" in f or "ci" in f.lower() for f in files):
        return "Update CI/CD configuration"

    if any("package.json" in f or "requirements" in f for f in files):
        return "Update dependencies"

    if len(files) == 1:
        action = "Update" if diff_output else "Add"
        return f"{action} {files[0]}"

    # Group by directory
    dirs = set(f.rsplit("/", 1)[0] if "/" in f else "" for f in files)
    if len(dirs) == 1 and dirs != {""}:
        return f"Update {list(dirs)[0]}"

    return f"Update {len(files)} files"


def get_skill() -> Skill:
    """Get the commit skill."""
    return Skill(
        name="commit",
        description="Create a git commit with automatic message generation",
        handler=commit_handler,
        aliases=["ci", "git-commit"],
        category="git",
        requires_tools=["bash"],
    )
