"""Hook management tool handlers."""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...hooks import HookManager

# Global hook manager
_hook_manager: Optional["HookManager"] = None


def set_hook_manager(hook_manager: "HookManager") -> None:
    """Set hook manager after initialization.

    Args:
        hook_manager: HookManager instance
    """
    global _hook_manager
    _hook_manager = hook_manager


def get_hook_manager() -> Optional["HookManager"]:
    """Get the hook manager instance."""
    return _hook_manager


async def hook_list_handler(args: dict[str, Any]) -> dict[str, Any]:
    """List all configured hooks.

    Args:
        args: Dictionary containing:
            - hook_type: Optional filter by hook type

    Returns:
        Dictionary with hooks list
    """
    if not _hook_manager:
        return {
            "status": "error",
            "error": "Hook manager not initialized",
        }

    from ...hooks.config import HookType

    hook_type_filter = args.get("hook_type")

    result: dict[str, list[dict]] = {}

    if hook_type_filter:
        # Filter by specific type
        try:
            hook_type = HookType(hook_type_filter)
            hooks = _hook_manager.config.get_hooks(hook_type)
            result[hook_type.value] = [
                {
                    "command": h.command,
                    "timeout": h.timeout,
                    "block_on_failure": h.block_on_failure,
                    "description": h.description,
                }
                for h in hooks
            ]
        except ValueError:
            return {
                "status": "error",
                "error": f"Unknown hook type: {hook_type_filter}",
                "available_types": [t.value for t in HookType],
            }
    else:
        # List all hooks
        for hook_type in HookType:
            hooks = _hook_manager.config.get_hooks(hook_type)
            if hooks:
                result[hook_type.value] = [
                    {
                        "command": h.command,
                        "timeout": h.timeout,
                        "block_on_failure": h.block_on_failure,
                        "description": h.description,
                    }
                    for h in hooks
                ]

    return {
        "status": "success",
        "hooks": result,
        "enabled": _hook_manager.is_enabled,
    }


async def hook_add_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Add a new hook.

    Args:
        args: Dictionary containing:
            - hook_type: Hook type (required)
            - command: Shell command to run (required)
            - timeout: Timeout in milliseconds (default 5000)
            - block_on_failure: Block action on failure (default False)
            - description: Human-readable description

    Returns:
        Dictionary with result
    """
    if not _hook_manager:
        return {
            "status": "error",
            "error": "Hook manager not initialized",
        }

    from ...hooks.config import HookType, HookDefinition

    hook_type_str = args.get("hook_type")
    command = args.get("command")

    if not hook_type_str:
        return {
            "status": "error",
            "error": "hook_type is required",
            "available_types": [t.value for t in HookType],
        }

    if not command:
        return {
            "status": "error",
            "error": "command is required",
        }

    try:
        hook_type = HookType(hook_type_str)
    except ValueError:
        return {
            "status": "error",
            "error": f"Unknown hook type: {hook_type_str}",
            "available_types": [t.value for t in HookType],
        }

    hook = HookDefinition(
        command=command,
        timeout=args.get("timeout", 5000),
        block_on_failure=args.get("block_on_failure", False),
        description=args.get("description", ""),
    )

    _hook_manager.config.add_hook(hook_type, hook)

    return {
        "status": "success",
        "message": f"Added hook for {hook_type.value}",
        "hook": {
            "command": hook.command,
            "timeout": hook.timeout,
            "block_on_failure": hook.block_on_failure,
        },
    }


async def hook_remove_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Remove a hook by command.

    Args:
        args: Dictionary containing:
            - hook_type: Hook type (required)
            - command: Command to remove (required)

    Returns:
        Dictionary with result
    """
    if not _hook_manager:
        return {
            "status": "error",
            "error": "Hook manager not initialized",
        }

    from ...hooks.config import HookType

    hook_type_str = args.get("hook_type")
    command = args.get("command")

    if not hook_type_str:
        return {
            "status": "error",
            "error": "hook_type is required",
        }

    if not command:
        return {
            "status": "error",
            "error": "command is required",
        }

    try:
        hook_type = HookType(hook_type_str)
    except ValueError:
        return {
            "status": "error",
            "error": f"Unknown hook type: {hook_type_str}",
        }

    removed = _hook_manager.config.remove_hook(hook_type, command)

    if removed:
        return {
            "status": "success",
            "message": f"Removed hook for {hook_type.value}",
        }
    else:
        return {
            "status": "error",
            "error": f"Hook not found: {command}",
        }


async def hook_enable_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Enable hook execution.

    Returns:
        Dictionary with result
    """
    if not _hook_manager:
        return {
            "status": "error",
            "error": "Hook manager not initialized",
        }

    _hook_manager.enable()

    return {
        "status": "success",
        "message": "Hooks enabled",
        "enabled": True,
    }


async def hook_disable_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Disable hook execution.

    Returns:
        Dictionary with result
    """
    if not _hook_manager:
        return {
            "status": "error",
            "error": "Hook manager not initialized",
        }

    _hook_manager.disable()

    return {
        "status": "success",
        "message": "Hooks disabled",
        "enabled": False,
    }
