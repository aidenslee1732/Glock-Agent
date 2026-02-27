"""Git tool handlers with safety enforcement."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from .safety import GitSafetyChecker, SafetyLevel

# Global safety checker and hook manager
_safety_checker: Optional[GitSafetyChecker] = None
_workspace_dir: Optional[str] = None
_hook_manager: Optional[Any] = None


def init_git_tools(
    workspace_dir: Optional[str] = None,
    safety_checker: Optional[GitSafetyChecker] = None,
    hook_manager: Optional[Any] = None,
) -> None:
    """Initialize git tools.

    Args:
        workspace_dir: Workspace directory
        safety_checker: GitSafetyChecker instance
        hook_manager: HookManager instance
    """
    global _safety_checker, _workspace_dir, _hook_manager
    _safety_checker = safety_checker or GitSafetyChecker()
    _workspace_dir = workspace_dir
    _hook_manager = hook_manager


def set_hook_manager(hook_manager) -> None:
    """Set hook manager after initialization."""
    global _hook_manager
    _hook_manager = hook_manager


def _get_checker() -> GitSafetyChecker:
    """Get or create the safety checker."""
    global _safety_checker
    if _safety_checker is None:
        _safety_checker = GitSafetyChecker()
    return _safety_checker


async def _run_git_command(command: str, cwd: Optional[str] = None) -> dict[str, Any]:
    """Run a git command and return results.

    Args:
        command: Git command to run
        cwd: Working directory

    Returns:
        Dictionary with output and exit code
    """
    cwd = cwd or _workspace_dir or os.getcwd()

    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env={**os.environ, "NO_COLOR": "1"},
    )

    stdout, _ = await process.communicate()
    output = stdout.decode(errors="replace")

    return {
        "output": output,
        "exit_code": process.returncode,
    }


async def git_status_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Get git status (safe version - never uses -uall).

    Args:
        args: Dictionary containing:
            - short: Use short format (default True)
            - branch: Show branch info (default True)

    Returns:
        Dictionary with status info
    """
    short = args.get("short", True)
    branch = args.get("branch", True)

    cmd_parts = ["git", "status"]

    if short:
        cmd_parts.append("--porcelain")
    if branch:
        cmd_parts.append("--branch")

    # Never use -uall flag
    command = " ".join(cmd_parts)
    result = await _run_git_command(command)

    if result["exit_code"] != 0:
        return {
            "status": "error",
            "error": result["output"],
        }

    # Parse output
    output = result["output"]
    lines = output.strip().split("\n") if output.strip() else []

    staged = []
    unstaged = []
    untracked = []

    for line in lines:
        if not line or line.startswith("##"):
            continue

        status = line[:2] if len(line) >= 2 else ""
        file_path = line[3:].strip() if len(line) > 3 else ""

        if status[0] != " " and status[0] != "?":
            staged.append({"status": status[0], "path": file_path})
        if status[1] != " ":
            if status[1] == "?":
                untracked.append(file_path)
            else:
                unstaged.append({"status": status[1], "path": file_path})

    return {
        "status": "success",
        "raw_output": output,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
    }


async def git_diff_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Show git diff.

    Args:
        args: Dictionary containing:
            - staged: Show staged changes (default False)
            - ref1: First ref for comparison
            - ref2: Second ref for comparison
            - stat: Show stat only (default False)
            - file: Specific file to diff

    Returns:
        Dictionary with diff output
    """
    staged = args.get("staged", False)
    ref1 = args.get("ref1")
    ref2 = args.get("ref2")
    stat = args.get("stat", False)
    file_path = args.get("file")

    cmd_parts = ["git", "diff"]

    if staged:
        cmd_parts.append("--cached")

    if stat:
        cmd_parts.append("--stat")

    if ref1:
        if ref2:
            cmd_parts.append(f"{ref1}...{ref2}")
        else:
            cmd_parts.append(ref1)

    if file_path:
        cmd_parts.extend(["--", file_path])

    command = " ".join(cmd_parts)
    result = await _run_git_command(command)

    if result["exit_code"] != 0:
        return {
            "status": "error",
            "error": result["output"],
        }

    return {
        "status": "success",
        "diff": result["output"],
    }


async def git_commit_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Create a git commit with safety checks.

    Args:
        args: Dictionary containing:
            - message: Commit message (required)
            - amend: Amend previous commit (default False)
            - no_verify: Skip hooks (default False, blocked by default)
            - author: Override author

    Returns:
        Dictionary with commit result
    """
    checker = _get_checker()

    message = args.get("message")
    if not message:
        return {
            "status": "error",
            "error": "commit message is required",
        }

    amend = args.get("amend", False)
    no_verify = args.get("no_verify", False)

    # Safety checks
    check = checker.check_commit(
        no_verify=no_verify,
        amend=amend,
    )

    if check.level == SafetyLevel.BLOCK:
        return {
            "status": "blocked",
            "error": check.message,
            "suggestion": check.suggestion,
        }

    # Capture any warnings to include in response
    warning_message = None
    if check.level == SafetyLevel.WARN:
        warning_message = check.message

    # Get staged files for pre-commit hook
    staged_result = await _run_git_command("git diff --cached --name-only")
    staged_files = staged_result["output"].strip().split("\n") if staged_result["output"].strip() else []

    # Run pre-commit hooks
    if _hook_manager and not no_verify:
        allowed, block_message = await _hook_manager.on_pre_commit(message, staged_files)
        if not allowed:
            return {
                "status": "blocked",
                "error": f"Blocked by pre-commit hook: {block_message}",
            }

    # Build command using heredoc for proper message formatting
    cmd_parts = ["git", "commit"]

    if amend:
        cmd_parts.append("--amend")

    if no_verify:
        cmd_parts.append("--no-verify")

    # Add co-author footer
    full_message = f"{message}\n\nCo-Authored-By: Claude <noreply@anthropic.com>"

    # Use heredoc for message
    command = f'''{" ".join(cmd_parts)} -m "$(cat <<'EOF'
{full_message}
EOF
)"'''

    result = await _run_git_command(command)

    if result["exit_code"] != 0:
        output = result["output"]
        if "nothing to commit" in output.lower():
            return {
                "status": "success",
                "message": "Nothing to commit (working tree clean)",
            }
        return {
            "status": "error",
            "error": output,
        }

    # Run post-commit hooks
    commit_hash = ""
    if _hook_manager:
        # Get the commit hash
        hash_result = await _run_git_command("git rev-parse HEAD")
        if hash_result["exit_code"] == 0:
            commit_hash = hash_result["output"].strip()
            await _hook_manager.on_post_commit(commit_hash, message)

    response = {
        "status": "success",
        "output": result["output"],
        "commit_hash": commit_hash,
    }
    if warning_message:
        response["warning"] = warning_message
    return response


async def git_push_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Push to remote with safety checks.

    Args:
        args: Dictionary containing:
            - remote: Remote name (default "origin")
            - branch: Branch to push
            - force: Force push (default False, checked for safety)
            - set_upstream: Set upstream with -u

    Returns:
        Dictionary with push result
    """
    checker = _get_checker()

    remote = args.get("remote", "origin")
    branch = args.get("branch")
    force = args.get("force", False)
    set_upstream = args.get("set_upstream", False)

    # Get current branch if not specified
    if not branch:
        branch_result = await _run_git_command("git branch --show-current")
        branch = branch_result["output"].strip()

    # Safety checks
    check = checker.check_push(branch, force, remote)

    if check.level == SafetyLevel.BLOCK:
        return {
            "status": "blocked",
            "error": check.message,
            "suggestion": check.suggestion,
        }

    warnings = []
    if check.level == SafetyLevel.WARN:
        warnings.append(check.message)

    # Build command
    cmd_parts = ["git", "push"]

    if set_upstream:
        cmd_parts.append("-u")

    if force:
        # Use --force-with-lease for safer force push
        cmd_parts.append("--force-with-lease")

    cmd_parts.extend([remote, branch])

    command = " ".join(cmd_parts)
    result = await _run_git_command(command)

    if result["exit_code"] != 0:
        return {
            "status": "error",
            "error": result["output"],
        }

    return {
        "status": "success",
        "output": result["output"],
        "warnings": warnings if warnings else None,
    }


async def git_log_handler(args: dict[str, Any]) -> dict[str, Any]:
    """View commit history.

    Args:
        args: Dictionary containing:
            - count: Number of commits (default 10)
            - branch: Branch to show
            - oneline: Use oneline format (default True)
            - since: Show commits since date
            - author: Filter by author

    Returns:
        Dictionary with log output
    """
    count = args.get("count", 10)
    branch = args.get("branch")
    oneline = args.get("oneline", True)
    since = args.get("since")
    author = args.get("author")

    cmd_parts = ["git", "log"]

    cmd_parts.append(f"-{count}")

    if oneline:
        cmd_parts.append("--oneline")

    if since:
        cmd_parts.append(f"--since={since}")

    if author:
        cmd_parts.append(f"--author={author}")

    if branch:
        cmd_parts.append(branch)

    command = " ".join(cmd_parts)
    result = await _run_git_command(command)

    if result["exit_code"] != 0:
        return {
            "status": "error",
            "error": result["output"],
        }

    # Parse commits if oneline
    commits = []
    if oneline:
        for line in result["output"].strip().split("\n"):
            if line:
                parts = line.split(" ", 1)
                if len(parts) >= 2:
                    commits.append({
                        "hash": parts[0],
                        "message": parts[1],
                    })

    return {
        "status": "success",
        "log": result["output"],
        "commits": commits if oneline else None,
    }


async def git_branch_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Branch operations with safety checks.

    Args:
        args: Dictionary containing:
            - action: "list", "create", "delete", "switch" (default "list")
            - name: Branch name (required for create/delete/switch)
            - force: Force delete with -D

    Returns:
        Dictionary with branch result
    """
    checker = _get_checker()

    action = args.get("action", "list")
    name = args.get("name")
    force = args.get("force", False)

    if action == "list":
        command = "git branch -a"
        result = await _run_git_command(command)

        branches = []
        current = None
        for line in result["output"].strip().split("\n"):
            if line.startswith("*"):
                branch = line[2:].strip()
                current = branch
            else:
                branch = line.strip()
            if branch:
                branches.append(branch)

        return {
            "status": "success",
            "branches": branches,
            "current": current,
        }

    if not name:
        return {
            "status": "error",
            "error": "branch name is required for this action",
        }

    if action == "create":
        command = f"git checkout -b {name}"
        result = await _run_git_command(command)

        if result["exit_code"] != 0:
            return {
                "status": "error",
                "error": result["output"],
            }

        return {
            "status": "success",
            "message": f"Created and switched to branch: {name}",
        }

    if action == "delete":
        # Safety check
        check = checker.check_branch_delete(name, force)

        if check.level == SafetyLevel.BLOCK:
            return {
                "status": "blocked",
                "error": check.message,
            }

        warnings = []
        if check.level == SafetyLevel.WARN:
            warnings.append(check.message)

        flag = "-D" if force else "-d"
        command = f"git branch {flag} {name}"
        result = await _run_git_command(command)

        if result["exit_code"] != 0:
            return {
                "status": "error",
                "error": result["output"],
            }

        return {
            "status": "success",
            "message": f"Deleted branch: {name}",
            "warnings": warnings if warnings else None,
        }

    if action == "switch":
        command = f"git checkout {name}"
        result = await _run_git_command(command)

        if result["exit_code"] != 0:
            return {
                "status": "error",
                "error": result["output"],
            }

        return {
            "status": "success",
            "message": f"Switched to branch: {name}",
        }

    return {
        "status": "error",
        "error": f"Unknown action: {action}",
    }
