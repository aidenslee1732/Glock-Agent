"""Plan enforcement for tool requests.

The enforcer validates that tool requests from the runtime comply with
the constraints defined in the compiled plan.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ToolRequestRejected(Exception):
    """Tool request was rejected by plan enforcement."""

    def __init__(self, reason: str, tool_name: str, requires_approval: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.tool_name = tool_name
        self.requires_approval = requires_approval


@dataclass
class EnforcementResult:
    """Result of plan enforcement check."""

    allowed: bool
    requires_approval: bool
    reason: Optional[str] = None
    modified_args: Optional[dict[str, Any]] = None


class PlanEnforcer:
    """Enforces plan constraints on tool requests.

    The enforcer checks:
    1. Tool is in allowed_tools list
    2. File paths are within workspace_scope / edit_scope
    3. Dangerous patterns trigger approval requirements
    4. Budget limits are respected
    """

    def __init__(self, plan_data: dict[str, Any]):
        self.plan_id = plan_data.get("plan_id", "")
        self.allowed_tools = set(plan_data.get("allowed_tools", []))
        self.workspace_scope = plan_data.get("workspace_scope")
        self.edit_scope = plan_data.get("edit_scope", [])
        self.approval_requirements = plan_data.get("approval_requirements", {})
        self.budgets = plan_data.get("budgets", {})

        # Track usage for budget enforcement
        self._tool_calls = 0
        self._iterations = 0

    def check_tool_request(
        self,
        tool_name: str,
        args: dict[str, Any]
    ) -> EnforcementResult:
        """Check if a tool request is allowed by the plan.

        Args:
            tool_name: Name of the tool being requested
            args: Arguments for the tool

        Returns:
            EnforcementResult indicating if allowed and any conditions
        """
        # Check if tool is allowed
        if self.allowed_tools and tool_name not in self.allowed_tools:
            return EnforcementResult(
                allowed=False,
                requires_approval=False,
                reason=f"Tool '{tool_name}' is not in allowed_tools list"
            )

        # Check budgets
        max_tool_calls = self.budgets.get("max_tool_calls")
        if max_tool_calls and self._tool_calls >= max_tool_calls:
            return EnforcementResult(
                allowed=False,
                requires_approval=False,
                reason=f"Tool call budget exceeded ({max_tool_calls})"
            )

        # Tool-specific checks
        if tool_name in ("read_file", "edit_file", "write_file"):
            return self._check_file_tool(tool_name, args)
        elif tool_name == "bash":
            return self._check_bash_tool(args)
        elif tool_name == "glob":
            return self._check_glob_tool(args)
        elif tool_name == "grep":
            return self._check_grep_tool(args)

        # Default: allowed
        return EnforcementResult(allowed=True, requires_approval=False)

    def record_tool_call(self):
        """Record that a tool call was made (for budget tracking)."""
        self._tool_calls += 1

    def record_iteration(self):
        """Record an iteration (for budget tracking)."""
        self._iterations += 1

    def check_budget(self) -> tuple[bool, Optional[str]]:
        """Check if any budgets are exceeded.

        Returns:
            Tuple of (within_budget, reason_if_exceeded)
        """
        max_tool_calls = self.budgets.get("max_tool_calls")
        if max_tool_calls and self._tool_calls >= max_tool_calls:
            return False, f"Tool call limit reached ({max_tool_calls})"

        max_iterations = self.budgets.get("max_iterations")
        if max_iterations and self._iterations >= max_iterations:
            return False, f"Iteration limit reached ({max_iterations})"

        return True, None

    def _check_file_tool(
        self,
        tool_name: str,
        args: dict[str, Any]
    ) -> EnforcementResult:
        """Check file read/edit/write tools."""
        path = args.get("path", "")

        # Check workspace scope
        if self.workspace_scope:
            if not self._path_in_scope(path, self.workspace_scope):
                return EnforcementResult(
                    allowed=False,
                    requires_approval=True,
                    reason=f"Path '{path}' is outside workspace scope"
                )

        # For edit/write, check edit scope
        if tool_name in ("edit_file", "write_file") and self.edit_scope:
            if not self._path_matches_patterns(path, self.edit_scope):
                # Check approval requirements
                approval_reqs = self.approval_requirements.get(tool_name, {})
                if approval_reqs.get("outside_scope"):
                    return EnforcementResult(
                        allowed=True,
                        requires_approval=True,
                        reason=f"Path '{path}' is outside edit scope"
                    )
                else:
                    return EnforcementResult(
                        allowed=False,
                        requires_approval=False,
                        reason=f"Path '{path}' is not in allowed edit scope"
                    )

        return EnforcementResult(allowed=True, requires_approval=False)

    def _check_bash_tool(self, args: dict[str, Any]) -> EnforcementResult:
        """Check bash command tool."""
        command = args.get("command", "")

        # Check approval patterns
        approval_reqs = self.approval_requirements.get("bash", {})
        patterns = approval_reqs.get("patterns", [])

        for pattern in patterns:
            if pattern in command or re.search(pattern, command):
                return EnforcementResult(
                    allowed=True,
                    requires_approval=True,
                    reason=f"Command matches dangerous pattern: {pattern}"
                )

        # Check for obviously dangerous commands
        dangerous_patterns = [
            r"\brm\s+-rf\s+[/~]",  # rm -rf on root or home
            r"\bsudo\b",           # sudo commands
            r"\bchmod\s+777\b",    # world-writable permissions
            r"\bcurl\b.*\|\s*sh",  # pipe curl to shell
            r"\bwget\b.*\|\s*sh",  # pipe wget to shell
            r"\bdd\s+if=",         # dd commands
            r"\bmkfs\b",           # format filesystems
            r"\bfdisk\b",          # partition editing
            r">\s*/etc/",          # overwrite system files
            r"\bkill\s+-9\s+1\b",  # kill init
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return EnforcementResult(
                    allowed=True,
                    requires_approval=True,
                    reason=f"Command contains potentially dangerous operation"
                )

        return EnforcementResult(allowed=True, requires_approval=False)

    def _check_glob_tool(self, args: dict[str, Any]) -> EnforcementResult:
        """Check glob pattern tool."""
        pattern = args.get("pattern", "")
        path = args.get("path", ".")

        # Check workspace scope
        if self.workspace_scope:
            if not self._path_in_scope(path, self.workspace_scope):
                return EnforcementResult(
                    allowed=True,
                    requires_approval=True,
                    reason=f"Glob path '{path}' is outside workspace scope"
                )

        return EnforcementResult(allowed=True, requires_approval=False)

    def _check_grep_tool(self, args: dict[str, Any]) -> EnforcementResult:
        """Check grep/search tool."""
        path = args.get("path", ".")

        # Check workspace scope
        if self.workspace_scope:
            if not self._path_in_scope(path, self.workspace_scope):
                return EnforcementResult(
                    allowed=True,
                    requires_approval=True,
                    reason=f"Grep path '{path}' is outside workspace scope"
                )

        return EnforcementResult(allowed=True, requires_approval=False)

    def _path_in_scope(self, path: str, scope: str) -> bool:
        """Check if path is within scope."""
        import os

        # Normalize paths
        path = os.path.normpath(os.path.abspath(path))
        scope = os.path.normpath(os.path.abspath(scope))

        # Check if path starts with scope
        return path.startswith(scope + os.sep) or path == scope

    def _path_matches_patterns(self, path: str, patterns: list[str]) -> bool:
        """Check if path matches any of the glob patterns."""
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
        return False
