"""Plan mode state management."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .files import PlanFileManager, Plan


class PlanModeState(str, Enum):
    """Plan mode states."""
    INACTIVE = "inactive"
    PLANNING = "planning"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EXECUTING = "executing"


@dataclass
class PlanModeContext:
    """Context for plan mode execution.

    Attributes:
        state: Current plan mode state
        plan: Current plan being worked on
        plan_file_path: Path to the plan file
        read_only_enforced: Whether read-only mode is enforced
        allowed_prompts: Bash prompts approved during planning
    """
    state: PlanModeState = PlanModeState.INACTIVE
    plan: Optional[Plan] = None
    plan_file_path: Optional[str] = None
    read_only_enforced: bool = False
    allowed_prompts: list[dict] = None

    def __post_init__(self):
        if self.allowed_prompts is None:
            self.allowed_prompts = []


class PlanMode:
    """Manages plan mode lifecycle.

    Plan mode workflow:
    1. Enter plan mode (read-only, exploration)
    2. Create/edit plan file
    3. Exit plan mode (request approval)
    4. User approves or rejects
    5. If approved, execute plan

    During plan mode:
    - Only read-only tools available (except writing to plan file)
    - Agent explores codebase and designs solution
    - Plan is written to dedicated plan file
    """

    def __init__(
        self,
        file_manager: Optional[PlanFileManager] = None,
        hook_manager: Optional[Any] = None,
    ):
        """Initialize plan mode.

        Args:
            file_manager: PlanFileManager instance
            hook_manager: HookManager instance for triggering plan hooks
        """
        self.file_manager = file_manager or PlanFileManager()
        self._context = PlanModeContext()
        self._hook_manager = hook_manager

    def set_hook_manager(self, hook_manager) -> None:
        """Set hook manager after initialization."""
        self._hook_manager = hook_manager

    @property
    def is_active(self) -> bool:
        """Check if plan mode is active."""
        return self._context.state != PlanModeState.INACTIVE

    @property
    def state(self) -> PlanModeState:
        """Get current state."""
        return self._context.state

    @property
    def current_plan(self) -> Optional[Plan]:
        """Get current plan."""
        return self._context.plan

    @property
    def plan_file_path(self) -> Optional[str]:
        """Get current plan file path."""
        return self._context.plan_file_path

    def enter(self, title: str = "Implementation Plan") -> dict[str, Any]:
        """Enter plan mode.

        Args:
            title: Title for the new plan

        Returns:
            Result dict with plan info
        """
        if self.is_active:
            return {
                "status": "error",
                "error": "Already in plan mode",
                "state": self._context.state.value,
            }

        # Create new plan
        plan = self.file_manager.create_plan(title)

        # Update context
        self._context.state = PlanModeState.PLANNING
        self._context.plan = plan
        self._context.plan_file_path = str(self.file_manager.get_plan_file_path(plan.id))
        self._context.read_only_enforced = True
        self._context.allowed_prompts = []

        return {
            "status": "success",
            "message": "Entered plan mode",
            "state": self._context.state.value,
            "plan_id": plan.id,
            "plan_file": self._context.plan_file_path,
        }

    def exit(self, allowed_prompts: Optional[list[dict]] = None) -> dict[str, Any]:
        """Exit plan mode and request approval.

        Args:
            allowed_prompts: List of bash prompts to approve

        Returns:
            Result dict with plan info
        """
        if not self.is_active:
            return {
                "status": "error",
                "error": "Not in plan mode",
            }

        if self._context.state != PlanModeState.PLANNING:
            return {
                "status": "error",
                "error": f"Cannot exit from state: {self._context.state.value}",
            }

        # Update plan status
        plan = self._context.plan
        plan.status = "pending_approval"
        self.file_manager.update_plan(plan)

        # Store allowed prompts
        if allowed_prompts:
            self._context.allowed_prompts = allowed_prompts

        # Update context
        self._context.state = PlanModeState.PENDING_APPROVAL
        self._context.read_only_enforced = False

        return {
            "status": "success",
            "message": "Plan submitted for approval",
            "state": self._context.state.value,
            "plan_id": plan.id,
            "plan_file": self._context.plan_file_path,
            "allowed_prompts": self._context.allowed_prompts,
        }

    async def approve(self) -> dict[str, Any]:
        """Approve the current plan.

        Returns:
            Result dict
        """
        if self._context.state != PlanModeState.PENDING_APPROVAL:
            return {
                "status": "error",
                "error": f"Cannot approve from state: {self._context.state.value}",
            }

        # Update plan status
        from datetime import datetime
        plan = self._context.plan
        plan.status = "approved"
        plan.approved_at = datetime.utcnow()
        self.file_manager.update_plan(plan)

        # Update context
        self._context.state = PlanModeState.APPROVED

        # Trigger plan-approved hooks
        if self._hook_manager:
            await self._hook_manager.on_plan_approved(plan.id)

        return {
            "status": "success",
            "message": "Plan approved",
            "state": self._context.state.value,
            "plan_id": plan.id,
        }

    async def reject(self, reason: str = "") -> dict[str, Any]:
        """Reject the current plan.

        Args:
            reason: Reason for rejection

        Returns:
            Result dict
        """
        if self._context.state != PlanModeState.PENDING_APPROVAL:
            return {
                "status": "error",
                "error": f"Cannot reject from state: {self._context.state.value}",
            }

        # Update plan status
        plan = self._context.plan
        plan_id = plan.id
        plan.status = "rejected"
        if reason:
            plan.metadata["rejection_reason"] = reason
        self.file_manager.update_plan(plan)

        # Trigger plan-rejected hooks
        if self._hook_manager:
            await self._hook_manager.on_plan_rejected(plan_id, reason)

        # Reset context
        self._context = PlanModeContext()

        return {
            "status": "success",
            "message": "Plan rejected",
            "reason": reason,
        }

    def start_execution(self) -> dict[str, Any]:
        """Start executing the approved plan.

        Returns:
            Result dict
        """
        if self._context.state != PlanModeState.APPROVED:
            return {
                "status": "error",
                "error": f"Cannot execute from state: {self._context.state.value}",
            }

        # Update plan status
        plan = self._context.plan
        plan.status = "executing"
        self.file_manager.update_plan(plan)

        # Update context
        self._context.state = PlanModeState.EXECUTING
        self._context.read_only_enforced = False

        return {
            "status": "success",
            "message": "Execution started",
            "state": self._context.state.value,
            "plan_id": plan.id,
            "allowed_prompts": self._context.allowed_prompts,
        }

    def complete(self) -> dict[str, Any]:
        """Mark plan execution as complete.

        Returns:
            Result dict
        """
        if self._context.state != PlanModeState.EXECUTING:
            return {
                "status": "error",
                "error": f"Cannot complete from state: {self._context.state.value}",
            }

        # Update plan status
        plan = self._context.plan
        plan.status = "completed"
        self.file_manager.update_plan(plan)

        # Reset context
        plan_id = plan.id
        self._context = PlanModeContext()

        return {
            "status": "success",
            "message": "Plan completed",
            "plan_id": plan_id,
        }

    def cancel(self) -> dict[str, Any]:
        """Cancel plan mode.

        Returns:
            Result dict
        """
        if not self.is_active:
            return {
                "status": "error",
                "error": "Not in plan mode",
            }

        old_state = self._context.state.value
        plan_id = self._context.plan.id if self._context.plan else None

        # Reset context
        self._context = PlanModeContext()

        return {
            "status": "success",
            "message": "Plan mode cancelled",
            "previous_state": old_state,
            "plan_id": plan_id,
        }

    def get_allowed_tools(self) -> Optional[list[str]]:
        """Get list of allowed tools in current state.

        Returns:
            List of tool names, or None for all tools
        """
        if not self.is_active:
            return None

        if self._context.read_only_enforced:
            # Read-only tools plus writing to plan file
            return [
                "read_file", "glob", "grep", "list_directory",
                "web_fetch", "web_search",
                "TaskList", "TaskGet",
                # Plan file operations are handled specially
            ]

        return None  # All tools allowed

    def can_write_file(self, file_path: str) -> bool:
        """Check if writing to a file is allowed.

        Args:
            file_path: Path to check

        Returns:
            True if writing is allowed
        """
        if not self.is_active:
            return True

        if not self._context.read_only_enforced:
            return True

        # Only allow writing to plan file
        if self._context.plan_file_path:
            return file_path == self._context.plan_file_path

        return False

    def get_context(self) -> PlanModeContext:
        """Get the current context.

        Returns:
            PlanModeContext
        """
        return self._context
