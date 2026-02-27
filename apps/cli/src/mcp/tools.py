"""MCP tool proxy for integrating MCP tools into the tool broker."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .client import MCPClient, MCPServerConfig
from .discovery import MCPServerDiscovery

logger = logging.getLogger(__name__)


class MCPToolProxy:
    """Proxy for MCP tools to integrate with the tool broker.

    This class manages MCP server connections and provides a unified
    interface for invoking MCP tools.
    """

    def __init__(
        self,
        discovery: Optional[MCPServerDiscovery] = None,
        client: Optional[MCPClient] = None,
    ):
        """Initialize the tool proxy.

        Args:
            discovery: MCPServerDiscovery instance
            client: MCPClient instance
        """
        self.discovery = discovery or MCPServerDiscovery()
        self.client = client or MCPClient()
        self._initialized = False

    async def initialize(self) -> int:
        """Initialize and connect to all configured servers.

        Returns:
            Number of servers connected
        """
        if self._initialized:
            return len(self.client._processes)

        servers = self.discovery.get_enabled_servers()
        connected = 0

        for config in servers:
            try:
                if await self.client.connect(config):
                    connected += 1
            except Exception as e:
                logger.warning(f"Failed to connect to MCP server {config.name}: {e}")

        self._initialized = True
        logger.info(f"Connected to {connected}/{len(servers)} MCP servers")
        return connected

    async def shutdown(self) -> None:
        """Disconnect from all servers."""
        await self.client.disconnect_all()
        self._initialized = False

    async def invoke_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke an MCP tool.

        Args:
            tool_name: Tool name (can be "server:tool" or just "tool")
            arguments: Tool arguments

        Returns:
            Tool result dictionary
        """
        if not self._initialized:
            await self.initialize()

        result = await self.client.invoke_tool(tool_name, arguments)

        if result.success:
            return {
                "status": "success",
                "content": result.content,
            }
        else:
            return {
                "status": "error",
                "error": result.error or "Unknown error",
            }

    async def list_tools(self) -> dict[str, Any]:
        """List all available MCP tools.

        Returns:
            Dictionary with tool list
        """
        if not self._initialized:
            await self.initialize()

        tools = self.client.list_tools()

        return {
            "status": "success",
            "tools": [
                {
                    "name": f"{t.server_name}:{t.name}",
                    "description": t.description,
                    "server": t.server_name,
                    "input_schema": t.input_schema,
                }
                for t in tools
            ],
            "total": len(tools),
        }

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Get MCP tool definitions for LLM.

        Returns:
            List of tool definitions
        """
        return self.client.get_tool_definitions()

    def has_tool(self, tool_name: str) -> bool:
        """Check if a tool is available.

        Args:
            tool_name: Tool name to check

        Returns:
            True if tool is available
        """
        return tool_name in self.client._tools

    async def add_server(
        self,
        name: str,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Add and connect to a new MCP server.

        Args:
            name: Server name
            command: Server command
            args: Command arguments
            env: Environment variables

        Returns:
            Status dictionary
        """
        config = MCPServerConfig(
            name=name,
            command=command,
            args=args or [],
            env=env or {},
        )

        # Save to config
        self.discovery.add_server(config)

        # Connect
        success = await self.client.connect(config)

        if success:
            tools = self.client.list_tools()
            return {
                "status": "success",
                "message": f"Connected to server: {name}",
                "tools_available": len([t for t in tools if t.server_name == name]),
            }
        else:
            return {
                "status": "error",
                "error": f"Failed to connect to server: {name}",
            }

    async def remove_server(self, name: str) -> dict[str, Any]:
        """Disconnect and remove an MCP server.

        Args:
            name: Server name

        Returns:
            Status dictionary
        """
        # Disconnect
        await self.client.disconnect(name)

        # Remove from config
        if self.discovery.remove_server(name):
            return {
                "status": "success",
                "message": f"Removed server: {name}",
            }
        else:
            return {
                "status": "error",
                "error": f"Server not found: {name}",
            }


# Tool handlers for the broker

_mcp_proxy: Optional[MCPToolProxy] = None


def init_mcp_tools(proxy: Optional[MCPToolProxy] = None) -> None:
    """Initialize MCP tools.

    Args:
        proxy: MCPToolProxy instance
    """
    global _mcp_proxy
    _mcp_proxy = proxy or MCPToolProxy()


async def mcp_invoke_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handler for mcp_invoke tool.

    Args:
        args: Dictionary containing:
            - tool: Tool name (required)
            - arguments: Tool arguments (required)

    Returns:
        Tool result
    """
    global _mcp_proxy

    if _mcp_proxy is None:
        init_mcp_tools()

    tool_name = args.get("tool")
    if not tool_name:
        return {
            "status": "error",
            "error": "tool name is required",
        }

    arguments = args.get("arguments", {})

    return await _mcp_proxy.invoke_tool(tool_name, arguments)


async def mcp_list_tools_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Handler for mcp_list_tools tool.

    Args:
        args: Empty dict

    Returns:
        Dictionary with tool list
    """
    global _mcp_proxy

    if _mcp_proxy is None:
        init_mcp_tools()

    return await _mcp_proxy.list_tools()
