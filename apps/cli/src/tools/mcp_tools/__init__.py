"""MCP tool wrappers for Glock CLI."""

from .handlers import (
    mcp_invoke_handler,
    mcp_list_tools_handler,
    mcp_server_status_handler,
    mcp_add_server_handler,
    mcp_remove_server_handler,
    mcp_restart_server_handler,
    init_mcp_tools,
    set_mcp_proxy,
)

__all__ = [
    "mcp_invoke_handler",
    "mcp_list_tools_handler",
    "mcp_server_status_handler",
    "mcp_add_server_handler",
    "mcp_remove_server_handler",
    "mcp_restart_server_handler",
    "init_mcp_tools",
    "set_mcp_proxy",
]
