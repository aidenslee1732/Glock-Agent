"""MCP tool handlers for the broker."""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...mcp import MCPToolProxy

# Global proxy
_mcp_proxy: Optional["MCPToolProxy"] = None


def init_mcp_tools(proxy: Optional["MCPToolProxy"] = None) -> None:
    """Initialize MCP tools.

    Args:
        proxy: MCPToolProxy instance
    """
    global _mcp_proxy
    _mcp_proxy = proxy


def set_mcp_proxy(proxy: "MCPToolProxy") -> None:
    """Set MCP proxy after initialization."""
    global _mcp_proxy
    _mcp_proxy = proxy


async def _get_proxy() -> "MCPToolProxy":
    """Get or create MCP proxy."""
    global _mcp_proxy

    if _mcp_proxy is None:
        from ...mcp import MCPToolProxy
        _mcp_proxy = MCPToolProxy()
        await _mcp_proxy.initialize()

    return _mcp_proxy


async def mcp_invoke_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Invoke an MCP tool.

    Args:
        args: Dictionary containing:
            - tool: Tool name (e.g., "github:create_issue")
            - arguments: Tool arguments dict

    Returns:
        Tool result
    """
    tool_name = args.get("tool")
    if not tool_name:
        return {
            "status": "error",
            "error": "tool name is required",
        }

    arguments = args.get("arguments", {})

    proxy = await _get_proxy()
    return await proxy.invoke_tool(tool_name, arguments)


async def mcp_list_tools_handler(args: dict[str, Any]) -> dict[str, Any]:
    """List available MCP tools.

    Args:
        args: Empty dict

    Returns:
        Dictionary with tool list
    """
    proxy = await _get_proxy()
    return await proxy.list_tools()


async def mcp_server_status_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Get status of MCP servers.

    Args:
        args: Empty dict

    Returns:
        Dictionary with server status
    """
    proxy = await _get_proxy()

    # Get connected servers and their status
    servers = {}
    for name in proxy.discovery.list_servers():
        config = proxy.discovery.get_server_config(name)
        is_connected = name in proxy.client._clients
        servers[name] = {
            "connected": is_connected,
            "command": config.get("command", ""),
            "args": config.get("args", []),
        }

    return {
        "status": "success",
        "servers": servers,
        "total": len(servers),
        "connected": sum(1 for s in servers.values() if s["connected"]),
    }


async def mcp_add_server_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Add and connect to a new MCP server.

    Args:
        args: Dictionary containing:
            - name: Server name (required)
            - command: Command to run (required)
            - args: Command arguments (optional list)
            - env: Environment variables (optional dict)

    Returns:
        Dictionary with result
    """
    name = args.get("name")
    command = args.get("command")

    if not name:
        return {
            "status": "error",
            "error": "name is required",
        }

    if not command:
        return {
            "status": "error",
            "error": "command is required",
        }

    proxy = await _get_proxy()
    return await proxy.add_server(
        name=name,
        command=command,
        args=args.get("args", []),
        env=args.get("env"),
    )


async def mcp_remove_server_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Disconnect and remove an MCP server.

    Args:
        args: Dictionary containing:
            - name: Server name (required)

    Returns:
        Dictionary with result
    """
    name = args.get("name")

    if not name:
        return {
            "status": "error",
            "error": "name is required",
        }

    proxy = await _get_proxy()
    return await proxy.remove_server(name)


async def mcp_restart_server_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Restart an MCP server.

    Args:
        args: Dictionary containing:
            - name: Server name (required)

    Returns:
        Dictionary with result
    """
    name = args.get("name")

    if not name:
        return {
            "status": "error",
            "error": "name is required",
        }

    proxy = await _get_proxy()

    # Get current config
    config = proxy.discovery.get_server_config(name)
    if not config:
        return {
            "status": "error",
            "error": f"Server not found: {name}",
        }

    # Disconnect
    if name in proxy.client._clients:
        await proxy.client.disconnect(name)

    # Reconnect
    try:
        await proxy.client.connect(
            name=name,
            command=config.get("command", ""),
            args=config.get("args", []),
            env=config.get("env"),
        )
        return {
            "status": "success",
            "message": f"Server {name} restarted",
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to restart server: {str(e)}",
        }
