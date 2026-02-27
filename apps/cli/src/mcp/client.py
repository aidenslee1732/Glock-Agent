"""MCP client for communicating with MCP servers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional
import uuid

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server.

    Attributes:
        name: Server name (identifier)
        command: Command to start the server
        args: Command arguments
        env: Environment variables
        working_dir: Working directory for the server
    """
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    working_dir: Optional[str] = None

    def get_full_command(self) -> list[str]:
        """Get the full command with arguments."""
        return [self.command] + self.args


@dataclass
class MCPTool:
    """Definition of an MCP tool.

    Attributes:
        name: Tool name
        description: Tool description
        input_schema: JSON schema for input parameters
        server_name: Name of the server providing this tool
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    server_name: str


@dataclass
class MCPToolResult:
    """Result from an MCP tool invocation.

    Attributes:
        success: Whether the invocation succeeded
        content: Result content
        error: Error message if failed
        is_error: Whether this is an error response
    """
    success: bool
    content: Any = None
    error: Optional[str] = None
    is_error: bool = False


class MCPClient:
    """Client for communicating with MCP servers using JSON-RPC 2.0.

    MCP (Model Context Protocol) allows connecting to external tools
    and services through a standardized protocol.
    """

    def __init__(self):
        """Initialize the MCP client."""
        self._servers: dict[str, MCPServerConfig] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tools: dict[str, MCPTool] = {}
        self._request_id = 0

    async def connect(self, config: MCPServerConfig) -> bool:
        """Connect to an MCP server.

        Args:
            config: Server configuration

        Returns:
            True if connected successfully
        """
        if config.name in self._processes:
            logger.warning(f"Server {config.name} already connected")
            return True

        try:
            # Build environment
            env = {**os.environ}

            # Expand environment variable references in config
            for key, value in config.env.items():
                if value.startswith("${") and value.endswith("}"):
                    env_var = value[2:-1]
                    env[key] = os.environ.get(env_var, "")
                else:
                    env[key] = value

            # Start server process
            process = await asyncio.create_subprocess_exec(
                *config.get_full_command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=config.working_dir,
                env=env,
            )

            self._processes[config.name] = process
            self._servers[config.name] = config

            # Initialize connection
            await self._initialize(config.name)

            # List available tools
            await self._list_tools(config.name)

            logger.info(f"Connected to MCP server: {config.name}")
            return True

        except Exception as e:
            logger.exception(f"Failed to connect to MCP server {config.name}")
            return False

    async def disconnect(self, server_name: str) -> None:
        """Disconnect from an MCP server.

        Args:
            server_name: Name of server to disconnect
        """
        process = self._processes.pop(server_name, None)
        if process:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()

        self._servers.pop(server_name, None)

        # Remove tools from this server
        to_remove = [
            name for name, tool in self._tools.items()
            if tool.server_name == server_name
        ]
        for name in to_remove:
            del self._tools[name]

        logger.info(f"Disconnected from MCP server: {server_name}")

    async def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        server_names = list(self._processes.keys())
        for name in server_names:
            await self.disconnect(name)

    async def _send_request(
        self,
        server_name: str,
        method: str,
        params: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request to a server.

        Args:
            server_name: Target server
            method: RPC method name
            params: Method parameters

        Returns:
            Response data
        """
        process = self._processes.get(server_name)
        if not process or process.stdin is None:
            raise RuntimeError(f"Server {server_name} not connected")

        # Build request
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params:
            request["params"] = params

        # Send request
        request_line = json.dumps(request) + "\n"
        process.stdin.write(request_line.encode())
        await process.stdin.drain()

        # Read response
        response_line = await asyncio.wait_for(
            process.stdout.readline(),
            timeout=30.0,
        )

        if not response_line:
            raise RuntimeError(f"No response from server {server_name}")

        response = json.loads(response_line.decode())

        if "error" in response:
            error = response["error"]
            raise RuntimeError(f"RPC error: {error.get('message', 'Unknown error')}")

        return response.get("result", {})

    async def _initialize(self, server_name: str) -> dict:
        """Initialize connection with a server.

        Args:
            server_name: Server to initialize

        Returns:
            Server capabilities
        """
        result = await self._send_request(
            server_name,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "glock",
                    "version": "1.0.0",
                },
            },
        )

        # Send initialized notification
        await self._send_request(server_name, "notifications/initialized")

        return result

    async def _list_tools(self, server_name: str) -> list[MCPTool]:
        """List tools available from a server.

        Args:
            server_name: Server to query

        Returns:
            List of available tools
        """
        result = await self._send_request(server_name, "tools/list")

        tools = []
        for tool_data in result.get("tools", []):
            tool = MCPTool(
                name=tool_data["name"],
                description=tool_data.get("description", ""),
                input_schema=tool_data.get("inputSchema", {}),
                server_name=server_name,
            )
            # Register with unique name (server_name:tool_name)
            full_name = f"{server_name}:{tool.name}"
            self._tools[full_name] = tool
            # Also register short name if unique
            if tool.name not in self._tools:
                self._tools[tool.name] = tool
            tools.append(tool)

        logger.info(f"Found {len(tools)} tools on server {server_name}")
        return tools

    async def invoke_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> MCPToolResult:
        """Invoke an MCP tool.

        Args:
            tool_name: Tool name (can be "server:tool" or just "tool")
            arguments: Tool arguments

        Returns:
            MCPToolResult with response
        """
        # Find tool
        tool = self._tools.get(tool_name)
        if not tool:
            return MCPToolResult(
                success=False,
                error=f"Unknown tool: {tool_name}",
            )

        # Call tool on server
        try:
            result = await self._send_request(
                tool.server_name,
                "tools/call",
                {
                    "name": tool.name,
                    "arguments": arguments,
                },
            )

            # Parse response content
            content = result.get("content", [])
            is_error = result.get("isError", False)

            # Extract text content
            text_content = []
            for item in content:
                if item.get("type") == "text":
                    text_content.append(item.get("text", ""))

            return MCPToolResult(
                success=not is_error,
                content="\n".join(text_content) if text_content else content,
                is_error=is_error,
            )

        except Exception as e:
            logger.exception(f"Failed to invoke tool {tool_name}")
            return MCPToolResult(
                success=False,
                error=str(e),
            )

    def list_tools(self) -> list[MCPTool]:
        """List all available tools.

        Returns:
            List of tools from all connected servers
        """
        # Return unique tools (avoid duplicates from short names)
        seen = set()
        tools = []
        for name, tool in self._tools.items():
            full_name = f"{tool.server_name}:{tool.name}"
            if full_name not in seen:
                seen.add(full_name)
                tools.append(tool)
        return tools

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions for LLM.

        Returns:
            List of tool definitions in LLM format
        """
        definitions = []
        seen = set()

        for tool in self._tools.values():
            full_name = f"{tool.server_name}:{tool.name}"
            if full_name in seen:
                continue
            seen.add(full_name)

            definitions.append({
                "name": full_name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            })

        return definitions

    def is_connected(self, server_name: str) -> bool:
        """Check if a server is connected.

        Args:
            server_name: Server to check

        Returns:
            True if connected
        """
        return server_name in self._processes
