"""Plan mode tool handlers."""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...planning import PlanMode

# Global instance
_plan_mode: Optional["PlanMode"] = None


def init_plan_tools(plan_mode: Optional["PlanMode"] = None) -> None:
    """Initialize plan tools.

    Args:
        plan_mode: PlanMode instance
    """
    global _plan_mode

    if plan_mode:
        _plan_mode = plan_mode


def set_plan_mode(plan_mode: "PlanMode") -> None:
    """Set plan mode after initialization."""
    global _plan_mode
    _plan_mode = plan_mode


def _get_plan_mode() -> "PlanMode":
    """Get or create plan mode instance."""
    global _plan_mode

    if _plan_mode is None:
        from ...planning import PlanMode
        _plan_mode = PlanMode()

    return _plan_mode


async def enter_plan_mode_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Enter plan mode for structured implementation planning.

    This tool transitions to plan mode where the agent can explore
    the codebase and design an implementation approach for user approval.

    Args:
        args: Empty dict (no parameters needed)

    Returns:
        Dictionary with plan mode status
    """
    plan_mode = _get_plan_mode()

    if plan_mode.is_active:
        return {
            "status": "error",
            "error": "Already in plan mode",
            "state": plan_mode.state.value,
            "plan_file": plan_mode.plan_file_path,
        }

    result = plan_mode.enter()

    if result["status"] == "success":
        return {
            "status": "success",
            "message": "Entered plan mode. You can now explore the codebase and write your plan.",
            "instructions": [
                "1. Use read-only tools (read_file, glob, grep) to explore",
                "2. Write your implementation plan to the plan file",
                "3. When ready, use ExitPlanMode to request user approval",
            ],
            "plan_file": result["plan_file"],
            "state": result["state"],
        }

    return result


async def exit_plan_mode_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Exit plan mode and request user approval.

    This tool is used when the plan is complete and ready for review.
    The plan file will be presented to the user for approval.

    Args:
        args: Dictionary containing:
            - allowedPrompts: List of bash prompts needed for implementation
            - pushToRemote: Whether to push to remote (not implemented)

    Returns:
        Dictionary with plan approval status
    """
    plan_mode = _get_plan_mode()

    if not plan_mode.is_active:
        return {
            "status": "error",
            "error": "Not in plan mode. Use EnterPlanMode first.",
        }

    # Parse allowed prompts
    allowed_prompts = args.get("allowedPrompts", [])

    result = plan_mode.exit(allowed_prompts)

    if result["status"] == "success":
        return {
            "status": "pending_approval",
            "message": "Plan submitted for user approval",
            "plan_file": result["plan_file"],
            "plan_id": result["plan_id"],
            "allowed_prompts": result.get("allowed_prompts", []),
        }

    return result


async def approve_plan_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Approve the current plan (called by user).

    Args:
        args: Empty dict

    Returns:
        Dictionary with approval status
    """
    plan_mode = _get_plan_mode()

    result = await plan_mode.approve()
    return result


async def reject_plan_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Reject the current plan (called by user).

    Args:
        args: Dictionary containing:
            - reason: Optional rejection reason

    Returns:
        Dictionary with rejection status
    """
    plan_mode = _get_plan_mode()

    reason = args.get("reason", "")
    result = await plan_mode.reject(reason)
    return result


async def get_plan_status_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Get current plan mode status.

    Args:
        args: Empty dict

    Returns:
        Dictionary with plan mode status
    """
    plan_mode = _get_plan_mode()

    if not plan_mode.is_active:
        return {
            "status": "success",
            "plan_mode_active": False,
            "state": "inactive",
        }

    plan = plan_mode.current_plan
    return {
        "status": "success",
        "plan_mode_active": True,
        "state": plan_mode.state.value,
        "plan_id": plan.id if plan else None,
        "plan_title": plan.title if plan else None,
        "plan_file": plan_mode.plan_file_path,
        "read_only_enforced": plan_mode.get_context().read_only_enforced,
    }
