"""MCP (Model Context Protocol) integration for Glock CLI."""

from .client import MCPClient
from .discovery import MCPServerDiscovery
from .tools import MCPToolProxy

__all__ = [
    "MCPClient",
    "MCPServerDiscovery",
    "MCPToolProxy",
]
