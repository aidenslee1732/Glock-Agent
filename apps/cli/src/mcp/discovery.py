"""MCP server discovery and configuration."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .client import MCPServerConfig

logger = logging.getLogger(__name__)


class MCPServerDiscovery:
    """Discovers and manages MCP server configurations.

    Server configurations are stored in ~/.glock/mcp.json
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize discovery.

        Args:
            config_path: Path to mcp.json. Defaults to ~/.glock/mcp.json
        """
        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = Path.home() / ".glock" / "mcp.json"

        self._servers: dict[str, MCPServerConfig] = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load server configurations from file."""
        if not self.config_path.exists():
            self._servers = {}
            return

        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)

            servers_data = data.get("servers", {})
            for name, server_data in servers_data.items():
                self._servers[name] = MCPServerConfig(
                    name=name,
                    command=server_data.get("command", ""),
                    args=server_data.get("args", []),
                    env=server_data.get("env", {}),
                    working_dir=server_data.get("working_dir"),
                )

        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load MCP config: {e}")
            self._servers = {}

    def _save_config(self) -> None:
        """Save server configurations to file."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "servers": {
                name: {
                    "command": config.command,
                    "args": config.args,
                    "env": config.env,
                    "working_dir": config.working_dir,
                }
                for name, config in self._servers.items()
            }
        }

        with open(self.config_path, "w") as f:
            json.dump(data, f, indent=2)

    def get_server(self, name: str) -> Optional[MCPServerConfig]:
        """Get a server configuration by name.

        Args:
            name: Server name

        Returns:
            MCPServerConfig if found
        """
        return self._servers.get(name)

    def list_servers(self) -> list[MCPServerConfig]:
        """List all configured servers.

        Returns:
            List of server configurations
        """
        return list(self._servers.values())

    def add_server(self, config: MCPServerConfig) -> None:
        """Add a server configuration.

        Args:
            config: Server configuration to add
        """
        self._servers[config.name] = config
        self._save_config()

    def remove_server(self, name: str) -> bool:
        """Remove a server configuration.

        Args:
            name: Server name to remove

        Returns:
            True if removed
        """
        if name in self._servers:
            del self._servers[name]
            self._save_config()
            return True
        return False

    def add_from_dict(self, name: str, data: dict[str, Any]) -> MCPServerConfig:
        """Add a server from dictionary configuration.

        Args:
            name: Server name
            data: Configuration dictionary

        Returns:
            Created MCPServerConfig
        """
        config = MCPServerConfig(
            name=name,
            command=data.get("command", ""),
            args=data.get("args", []),
            env=data.get("env", {}),
            working_dir=data.get("working_dir"),
        )
        self.add_server(config)
        return config

    def get_enabled_servers(self) -> list[MCPServerConfig]:
        """Get servers that should be auto-connected.

        Returns:
            List of enabled server configurations
        """
        # For now, return all servers
        # Could add an "enabled" flag to config
        return list(self._servers.values())


# Common MCP server presets
COMMON_SERVERS = {
    "github": {
        "command": "npx",
        "args": ["@anthropic/mcp-server-github"],
        "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
        "description": "GitHub integration (issues, PRs, repos)",
    },
    "filesystem": {
        "command": "npx",
        "args": ["@anthropic/mcp-server-filesystem"],
        "env": {},
        "description": "Extended filesystem operations",
    },
    "postgres": {
        "command": "npx",
        "args": ["@anthropic/mcp-server-postgres"],
        "env": {"DATABASE_URL": "${DATABASE_URL}"},
        "description": "PostgreSQL database access",
    },
    "slack": {
        "command": "npx",
        "args": ["@anthropic/mcp-server-slack"],
        "env": {"SLACK_TOKEN": "${SLACK_TOKEN}"},
        "description": "Slack integration",
    },
}


def get_preset_server(preset_name: str) -> Optional[dict[str, Any]]:
    """Get a preset server configuration.

    Args:
        preset_name: Name of the preset

    Returns:
        Server configuration dict or None
    """
    return COMMON_SERVERS.get(preset_name)
