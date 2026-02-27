"""Git safety protocols and checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class SafetyLevel(str, Enum):
    """Safety check result levels."""
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"


@dataclass
class SafetyCheckResult:
    """Result of a safety check.

    Attributes:
        level: Safety level (allow, warn, block)
        message: Warning or error message
        suggestion: Suggested alternative action
    """
    level: SafetyLevel
    message: str = ""
    suggestion: str = ""


# Sensitive file patterns
SENSITIVE_PATTERNS = [
    r"\.env$",
    r"\.env\.",
    r"credentials",
    r"secrets?",
    r"\.pem$",
    r"\.key$",
    r"\.p12$",
    r"password",
    r"api[_-]?key",
    r"auth[_-]?token",
    r"\.aws/",
    r"\.ssh/",
]

# Protected branches
PROTECTED_BRANCHES = ["main", "master", "production", "prod"]


class GitSafetyChecker:
    """Checks git operations for safety violations.

    Safety rules enforced:
    - Block force push to main/master
    - Block --no-verify unless explicit
    - Block --no-gpg-sign unless explicit
    - Warn on reset --hard
    - Warn on checkout . / restore .
    - Block clean -f unless explicit
    - Warn on branch -D
    - Warn about staging sensitive files
    - Warn about git add -A or git add .
    - Block amend after hook failure
    """

    def __init__(self):
        """Initialize the safety checker."""
        self._explicit_overrides: set[str] = set()

    def allow_override(self, rule: str) -> None:
        """Allow an explicit override of a safety rule.

        Args:
            rule: Rule to override (e.g., "force-push", "no-verify")
        """
        self._explicit_overrides.add(rule)

    def clear_overrides(self) -> None:
        """Clear all explicit overrides."""
        self._explicit_overrides.clear()

    def check_push(
        self,
        branch: str,
        force: bool = False,
        remote: str = "origin",
    ) -> SafetyCheckResult:
        """Check if a push operation is safe.

        Args:
            branch: Branch being pushed
            force: Whether force push is requested
            remote: Remote name

        Returns:
            SafetyCheckResult
        """
        # Block force push to protected branches
        if force and branch in PROTECTED_BRANCHES:
            if "force-push-protected" not in self._explicit_overrides:
                return SafetyCheckResult(
                    level=SafetyLevel.BLOCK,
                    message=f"Force push to {branch} is blocked for safety",
                    suggestion=f"Create a new branch instead, or explicitly allow this with override",
                )

        # Warn on force push to other branches
        if force:
            if "force-push" not in self._explicit_overrides:
                return SafetyCheckResult(
                    level=SafetyLevel.WARN,
                    message=f"Force push to {branch} may overwrite history",
                    suggestion="Consider using --force-with-lease instead",
                )

        return SafetyCheckResult(level=SafetyLevel.ALLOW)

    def check_commit(
        self,
        no_verify: bool = False,
        no_gpg_sign: bool = False,
        amend: bool = False,
        hook_failed: bool = False,
    ) -> SafetyCheckResult:
        """Check if a commit operation is safe.

        Args:
            no_verify: Whether --no-verify is used
            no_gpg_sign: Whether --no-gpg-sign is used
            amend: Whether --amend is used
            hook_failed: Whether a previous hook failed

        Returns:
            SafetyCheckResult
        """
        # Block --no-verify unless explicit
        if no_verify and "no-verify" not in self._explicit_overrides:
            return SafetyCheckResult(
                level=SafetyLevel.BLOCK,
                message="--no-verify bypasses safety hooks",
                suggestion="Remove --no-verify or explicitly allow this override",
            )

        # Block --no-gpg-sign unless explicit
        if no_gpg_sign and "no-gpg-sign" not in self._explicit_overrides:
            return SafetyCheckResult(
                level=SafetyLevel.BLOCK,
                message="--no-gpg-sign disables commit signing",
                suggestion="Remove --no-gpg-sign or explicitly allow this override",
            )

        # Block amend after hook failure
        if amend and hook_failed and "amend-after-hook" not in self._explicit_overrides:
            return SafetyCheckResult(
                level=SafetyLevel.BLOCK,
                message="Amend after hook failure may modify the wrong commit",
                suggestion="Create a new commit instead - the previous commit was not created",
            )

        return SafetyCheckResult(level=SafetyLevel.ALLOW)

    def check_reset(
        self,
        hard: bool = False,
        target: str = "HEAD",
    ) -> SafetyCheckResult:
        """Check if a reset operation is safe.

        Args:
            hard: Whether --hard is used
            target: Reset target

        Returns:
            SafetyCheckResult
        """
        if hard:
            if "reset-hard" not in self._explicit_overrides:
                return SafetyCheckResult(
                    level=SafetyLevel.WARN,
                    message="git reset --hard will discard all uncommitted changes",
                    suggestion="Stash changes first with 'git stash'",
                )

        return SafetyCheckResult(level=SafetyLevel.ALLOW)

    def check_checkout(
        self,
        path: str,
        discard_all: bool = False,
    ) -> SafetyCheckResult:
        """Check if a checkout operation is safe.

        Args:
            path: Path being checked out
            discard_all: Whether checking out '.' or all files

        Returns:
            SafetyCheckResult
        """
        if discard_all or path == ".":
            if "checkout-all" not in self._explicit_overrides:
                return SafetyCheckResult(
                    level=SafetyLevel.WARN,
                    message="This will discard all local changes",
                    suggestion="Use 'git diff' to review changes first",
                )

        return SafetyCheckResult(level=SafetyLevel.ALLOW)

    def check_clean(
        self,
        force: bool = False,
        directories: bool = False,
    ) -> SafetyCheckResult:
        """Check if a clean operation is safe.

        Args:
            force: Whether -f is used
            directories: Whether -d is used

        Returns:
            SafetyCheckResult
        """
        if force:
            if "clean-force" not in self._explicit_overrides:
                return SafetyCheckResult(
                    level=SafetyLevel.BLOCK,
                    message="git clean -f permanently deletes untracked files",
                    suggestion="Use 'git clean -n' to preview what would be deleted",
                )

        return SafetyCheckResult(level=SafetyLevel.ALLOW)

    def check_branch_delete(
        self,
        branch: str,
        force: bool = False,
    ) -> SafetyCheckResult:
        """Check if a branch delete is safe.

        Args:
            branch: Branch to delete
            force: Whether -D (force) is used

        Returns:
            SafetyCheckResult
        """
        # Block deleting protected branches
        if branch in PROTECTED_BRANCHES:
            return SafetyCheckResult(
                level=SafetyLevel.BLOCK,
                message=f"Cannot delete protected branch: {branch}",
            )

        if force:
            if "branch-force-delete" not in self._explicit_overrides:
                return SafetyCheckResult(
                    level=SafetyLevel.WARN,
                    message=f"-D may delete unmerged commits on {branch}",
                    suggestion="Use -d instead to ensure branch is fully merged",
                )

        return SafetyCheckResult(level=SafetyLevel.ALLOW)

    def check_staging(self, files: list[str]) -> SafetyCheckResult:
        """Check if staging files is safe.

        Args:
            files: List of files being staged

        Returns:
            SafetyCheckResult with any warnings
        """
        # Check for git add -A or git add .
        if "." in files or "-A" in files or "--all" in files:
            return SafetyCheckResult(
                level=SafetyLevel.WARN,
                message="git add -A/. may stage unintended files",
                suggestion="Stage specific files by name instead",
            )

        # Check for sensitive files
        sensitive = []
        for file in files:
            for pattern in SENSITIVE_PATTERNS:
                if re.search(pattern, file, re.IGNORECASE):
                    sensitive.append(file)
                    break

        if sensitive:
            return SafetyCheckResult(
                level=SafetyLevel.WARN,
                message=f"Staging potentially sensitive files: {', '.join(sensitive)}",
                suggestion="Review these files carefully before committing",
            )

        return SafetyCheckResult(level=SafetyLevel.ALLOW)

    def parse_git_command(self, command: str) -> dict[str, Any]:
        """Parse a git command and extract components.

        Args:
            command: Git command string

        Returns:
            Dictionary with parsed components
        """
        parts = command.split()
        if not parts or parts[0] != "git":
            return {"valid": False}

        if len(parts) < 2:
            return {"valid": False}

        subcommand = parts[1]
        args = parts[2:]

        result = {
            "valid": True,
            "subcommand": subcommand,
            "args": args,
        }

        # Parse common flags
        result["force"] = "-f" in args or "--force" in args or "--force-with-lease" in args
        result["no_verify"] = "--no-verify" in args
        result["hard"] = "--hard" in args
        result["amend"] = "--amend" in args

        return result
