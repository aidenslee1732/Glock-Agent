"""Sandbox policy configuration.

Defines policies for command execution sandboxing, including:
- Docker, process, or no isolation modes
- Resource limits (memory, CPU, time)
- Network access controls
- Workspace boundary enforcement
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class SandboxMode(str, Enum):
    """Sandbox execution modes."""
    DOCKER = "docker"    # Full Docker isolation
    PROCESS = "process"  # Process-level isolation (fallback)
    NONE = "none"        # No sandboxing (development only)


@dataclass
class SandboxPolicy:
    """Sandbox policy configuration.

    Defines how commands should be isolated and what resources they can access.
    """

    # Execution mode
    mode: SandboxMode = SandboxMode.DOCKER
    fallback_mode: SandboxMode = SandboxMode.PROCESS

    # Resource limits
    memory_limit: str = "2g"
    cpu_limit: float = 1.0
    timeout_seconds: int = 120
    pids_limit: int = 256

    # Network access
    allow_network: bool = False
    allowed_hosts: list[str] = field(default_factory=list)

    # Filesystem
    workspace_boundary: bool = True
    read_only_root: bool = True

    # Forbidden paths (never accessible)
    forbidden_paths: list[str] = field(default_factory=lambda: [
        "/etc/passwd",
        "/etc/shadow",
        "/etc/sudoers",
        "~/.ssh",
        "~/.aws",
        "~/.gcloud",
        "~/.kube",
        "~/.docker",
        "~/.gnupg",
        "/var/run/docker.sock",
    ])

    # Allowed commands (None = all allowed, empty = none allowed)
    allowed_commands: Optional[list[str]] = None

    # Blocked commands (always blocked)
    blocked_commands: list[str] = field(default_factory=lambda: [
        "rm -rf /",
        "dd if=/dev/zero",
        ":(){ :|:& };:",  # Fork bomb
        "chmod 777 /",
    ])

    @classmethod
    def development(cls) -> "SandboxPolicy":
        """Create a permissive policy for development."""
        return cls(
            mode=SandboxMode.NONE,
            fallback_mode=SandboxMode.NONE,
            allow_network=True,
            workspace_boundary=False,
            read_only_root=False,
            timeout_seconds=300,
        )

    @classmethod
    def standard(cls) -> "SandboxPolicy":
        """Create standard policy with Docker isolation."""
        return cls(
            mode=SandboxMode.DOCKER,
            fallback_mode=SandboxMode.PROCESS,
            allow_network=False,
            workspace_boundary=True,
        )

    @classmethod
    def strict(cls) -> "SandboxPolicy":
        """Create strict policy with maximum isolation."""
        return cls(
            mode=SandboxMode.DOCKER,
            fallback_mode=SandboxMode.PROCESS,
            memory_limit="1g",
            cpu_limit=0.5,
            timeout_seconds=60,
            pids_limit=64,
            allow_network=False,
            workspace_boundary=True,
            read_only_root=True,
        )

    @classmethod
    def network_allowed(cls) -> "SandboxPolicy":
        """Create policy with network access allowed."""
        return cls(
            mode=SandboxMode.DOCKER,
            fallback_mode=SandboxMode.PROCESS,
            allow_network=True,
            workspace_boundary=True,
        )

    def is_command_allowed(self, command: str) -> tuple[bool, str]:
        """Check if a command is allowed by policy.

        Args:
            command: Command to check

        Returns:
            Tuple of (allowed, reason)
        """
        # Check blocked commands
        for blocked in self.blocked_commands:
            if blocked in command:
                return False, f"Command matches blocked pattern: {blocked}"

        # Check allowed commands if specified
        if self.allowed_commands is not None:
            for allowed in self.allowed_commands:
                if allowed in command or command.startswith(allowed.split()[0]):
                    return True, ""
            return False, "Command not in allowed list"

        return True, ""

    def is_path_allowed(self, path: str | Path) -> tuple[bool, str]:
        """Check if a path is allowed by policy.

        Args:
            path: Path to check

        Returns:
            Tuple of (allowed, reason)
        """
        path_str = str(Path(path).expanduser().resolve())

        # Check forbidden paths
        for forbidden in self.forbidden_paths:
            forbidden_expanded = str(Path(forbidden).expanduser())
            if path_str.startswith(forbidden_expanded):
                return False, f"Path matches forbidden pattern: {forbidden}"

        return True, ""

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "mode": self.mode.value,
            "fallback_mode": self.fallback_mode.value,
            "memory_limit": self.memory_limit,
            "cpu_limit": self.cpu_limit,
            "timeout_seconds": self.timeout_seconds,
            "pids_limit": self.pids_limit,
            "allow_network": self.allow_network,
            "allowed_hosts": self.allowed_hosts,
            "workspace_boundary": self.workspace_boundary,
            "read_only_root": self.read_only_root,
            "forbidden_paths": self.forbidden_paths,
        }


def load_policy_from_config(config_path: Optional[Path] = None) -> SandboxPolicy:
    """Load sandbox policy from configuration file.

    Args:
        config_path: Path to config file (defaults to ~/.glock/sandbox.json)

    Returns:
        Loaded or default policy
    """
    import json

    if config_path is None:
        config_path = Path.home() / ".glock" / "sandbox.json"

    if not config_path.exists():
        return SandboxPolicy.standard()

    try:
        with open(config_path) as f:
            data = json.load(f)

        return SandboxPolicy(
            mode=SandboxMode(data.get("mode", "docker")),
            fallback_mode=SandboxMode(data.get("fallback_mode", "process")),
            memory_limit=data.get("memory_limit", "2g"),
            cpu_limit=data.get("cpu_limit", 1.0),
            timeout_seconds=data.get("timeout_seconds", 120),
            pids_limit=data.get("pids_limit", 256),
            allow_network=data.get("allow_network", False),
            allowed_hosts=data.get("allowed_hosts", []),
            workspace_boundary=data.get("workspace_boundary", True),
            read_only_root=data.get("read_only_root", True),
            forbidden_paths=data.get("forbidden_paths", SandboxPolicy().forbidden_paths),
            blocked_commands=data.get("blocked_commands", SandboxPolicy().blocked_commands),
        )
    except Exception as e:
        import logging
        logging.warning(f"Failed to load sandbox policy: {e}")
        return SandboxPolicy.standard()
