"""Hook configuration types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import json
from pathlib import Path


class HookType(str, Enum):
    """Types of hooks that can be executed."""

    # User interaction hooks
    USER_PROMPT_SUBMIT = "user-prompt-submit"  # Before processing user prompt

    # Tool execution hooks
    PRE_TOOL_EXECUTE = "pre-tool-execute"      # Before any tool runs
    POST_TOOL_EXECUTE = "post-tool-execute"    # After any tool runs

    # Git hooks
    PRE_COMMIT = "pre-commit"                  # Before git commit
    POST_COMMIT = "post-commit"                # After git commit

    # Session hooks
    SESSION_START = "session-start"            # When session starts
    SESSION_END = "session-end"                # When session ends

    # Plan mode hooks
    PLAN_APPROVED = "plan-approved"            # When plan is approved
    PLAN_REJECTED = "plan-rejected"            # When plan is rejected


@dataclass
class HookDefinition:
    """Definition of a single hook.

    Attributes:
        command: Shell command to execute
        timeout: Timeout in milliseconds (default 5000)
        block_on_failure: If True, block the action on hook failure
        env: Additional environment variables
        working_dir: Working directory for the command
        description: Human-readable description
    """
    command: str
    timeout: int = 5000
    block_on_failure: bool = False
    env: dict[str, str] = field(default_factory=dict)
    working_dir: Optional[str] = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "command": self.command,
            "timeout": self.timeout,
            "block_on_failure": self.block_on_failure,
            "env": self.env,
            "working_dir": self.working_dir,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HookDefinition":
        """Create from dictionary."""
        return cls(
            command=data["command"],
            timeout=data.get("timeout", 5000),
            block_on_failure=data.get("block_on_failure", False),
            env=data.get("env", {}),
            working_dir=data.get("working_dir"),
            description=data.get("description", ""),
        )


@dataclass
class HookResult:
    """Result from hook execution.

    Attributes:
        hook_type: Type of hook that was executed
        command: Command that was run
        success: Whether the hook succeeded
        output: Command output
        exit_code: Exit code from command
        error: Error message if failed
        blocked: Whether this blocked the action
        duration_ms: Execution duration in milliseconds
    """
    hook_type: HookType
    command: str
    success: bool
    output: str = ""
    exit_code: int = 0
    error: Optional[str] = None
    blocked: bool = False
    duration_ms: int = 0


class HookConfig:
    """Manages hook configuration.

    Hooks are configured in ~/.glock/hooks.json
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize hook configuration.

        Args:
            config_path: Path to hooks.json. Defaults to ~/.glock/hooks.json
        """
        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = Path.home() / ".glock" / "hooks.json"

        self._hooks: dict[HookType, list[HookDefinition]] = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load hook configuration from file."""
        if not self.config_path.exists():
            self._hooks = {}
            return

        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)

            hooks_data = data.get("hooks", {})
            for hook_type_str, hook_list in hooks_data.items():
                try:
                    hook_type = HookType(hook_type_str)
                    self._hooks[hook_type] = [
                        HookDefinition.from_dict(h) for h in hook_list
                    ]
                except ValueError:
                    # Unknown hook type, skip
                    pass

        except (json.JSONDecodeError, IOError):
            self._hooks = {}

    def _save_config(self) -> None:
        """Save hook configuration to file."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "hooks": {
                hook_type.value: [h.to_dict() for h in hooks]
                for hook_type, hooks in self._hooks.items()
            }
        }

        with open(self.config_path, "w") as f:
            json.dump(data, f, indent=2)

    def get_hooks(self, hook_type: HookType) -> list[HookDefinition]:
        """Get hooks for a type.

        Args:
            hook_type: Type of hooks to get

        Returns:
            List of hook definitions
        """
        return self._hooks.get(hook_type, [])

    def add_hook(self, hook_type: HookType, hook: HookDefinition) -> None:
        """Add a hook.

        Args:
            hook_type: Type of hook
            hook: Hook definition to add
        """
        if hook_type not in self._hooks:
            self._hooks[hook_type] = []

        self._hooks[hook_type].append(hook)
        self._save_config()

    def remove_hook(self, hook_type: HookType, command: str) -> bool:
        """Remove a hook by command.

        Args:
            hook_type: Type of hook
            command: Command string to match

        Returns:
            True if removed
        """
        if hook_type not in self._hooks:
            return False

        hooks = self._hooks[hook_type]
        for i, hook in enumerate(hooks):
            if hook.command == command:
                hooks.pop(i)
                self._save_config()
                return True

        return False

    def remove_hook_by_index(self, hook_type: HookType, index: int) -> bool:
        """Remove a hook by index.

        Args:
            hook_type: Type of hook
            index: Index in the hooks list

        Returns:
            True if removed
        """
        if hook_type not in self._hooks:
            return False

        hooks = self._hooks[hook_type]
        if 0 <= index < len(hooks):
            hooks.pop(index)
            self._save_config()
            return True

        return False

    def clear_hooks(self, hook_type: Optional[HookType] = None) -> None:
        """Clear hooks.

        Args:
            hook_type: Type to clear, or None for all
        """
        if hook_type:
            self._hooks.pop(hook_type, None)
        else:
            self._hooks = {}

        self._save_config()

    def list_all_hooks(self) -> dict[str, list[dict]]:
        """List all configured hooks.

        Returns:
            Dictionary of hook type -> hook definitions
        """
        return {
            hook_type.value: [h.to_dict() for h in hooks]
            for hook_type, hooks in self._hooks.items()
        }
