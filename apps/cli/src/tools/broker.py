"""Tool broker - executes tools locally."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ToolBroker:
    """Executes tools locally on behalf of the runtime.

    Available tools:
    - read_file: Read file contents
    - edit_file: Edit file (old_string -> new_string)
    - write_file: Write new file
    - glob: Find files by pattern
    - grep: Search file contents
    - bash: Execute shell command
    - list_directory: List directory contents
    """

    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir).resolve()

        # Tool registry
        self._tools: Dict[str, Callable] = {
            "read_file": self._read_file,
            "edit_file": self._edit_file,
            "write_file": self._write_file,
            "glob": self._glob,
            "grep": self._grep,
            "bash": self._bash,
            "list_directory": self._list_directory,
        }

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        timeout: float = 120.0,
    ) -> Dict[str, Any]:
        """Execute a tool.

        Args:
            tool_name: Name of tool to execute
            args: Tool arguments
            timeout: Execution timeout in seconds

        Returns:
            Tool result

        Raises:
            ValueError: If tool not found
            asyncio.TimeoutError: If execution times out
        """
        tool = self._tools.get(tool_name)
        if not tool:
            raise ValueError(f"Unknown tool: {tool_name}")

        # Execute with timeout
        return await asyncio.wait_for(
            tool(args),
            timeout=timeout,
        )

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to workspace."""
        p = Path(path)
        if not p.is_absolute():
            p = self.workspace_dir / p
        return p.resolve()

    def _check_path_safety(self, path: Path) -> bool:
        """Check if path is within workspace (no escaping)."""
        try:
            path.resolve().relative_to(self.workspace_dir)
            return True
        except ValueError:
            return False

    async def _read_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read file contents."""
        file_path = self._resolve_path(args["file_path"])

        if not self._check_path_safety(file_path):
            raise ValueError(f"Path escapes workspace: {file_path}")

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        content = file_path.read_text()

        # Apply offset/limit if specified
        offset = args.get("offset", 0)
        limit = args.get("limit")

        if offset or limit:
            lines = content.split("\n")
            if offset:
                lines = lines[offset:]
            if limit:
                lines = lines[:limit]
            content = "\n".join(lines)

        return {
            "content": content,
            "path": str(file_path),
            "size": file_path.stat().st_size,
        }

    async def _edit_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Edit file by replacing text."""
        file_path = self._resolve_path(args["file_path"])
        old_string = args["old_string"]
        new_string = args["new_string"]
        replace_all = args.get("replace_all", False)

        if not self._check_path_safety(file_path):
            raise ValueError(f"Path escapes workspace: {file_path}")

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        content = file_path.read_text()

        if old_string not in content:
            raise ValueError(f"String not found in file: {old_string[:50]}...")

        if replace_all:
            new_content = content.replace(old_string, new_string)
            count = content.count(old_string)
        else:
            new_content = content.replace(old_string, new_string, 1)
            count = 1

        file_path.write_text(new_content)

        return {
            "path": str(file_path),
            "replacements": count,
        }

    async def _write_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Write new file."""
        file_path = self._resolve_path(args["file_path"])
        content = args["content"]

        if not self._check_path_safety(file_path):
            raise ValueError(f"Path escapes workspace: {file_path}")

        # Create parent directories
        file_path.parent.mkdir(parents=True, exist_ok=True)

        file_path.write_text(content)

        return {
            "path": str(file_path),
            "bytes_written": len(content.encode()),
        }

    async def _glob(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Find files by glob pattern."""
        pattern = args["pattern"]
        path = args.get("path", ".")

        base_path = self._resolve_path(path)

        if not self._check_path_safety(base_path):
            raise ValueError(f"Path escapes workspace: {base_path}")

        matches = list(base_path.glob(pattern))

        # Filter to workspace
        matches = [
            m for m in matches
            if self._check_path_safety(m)
        ]

        # Convert to relative paths
        rel_paths = [
            str(m.relative_to(self.workspace_dir))
            for m in matches
        ]

        return {
            "matches": rel_paths[:100],  # Limit results
            "total": len(matches),
        }

    async def _grep(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Search file contents."""
        pattern = args["pattern"]
        path = args.get("path", ".")
        output_mode = args.get("output_mode", "files_with_matches")

        base_path = self._resolve_path(path)

        if not self._check_path_safety(base_path):
            raise ValueError(f"Path escapes workspace: {base_path}")

        # Use ripgrep if available, fall back to grep
        rg_path = subprocess.run(
            ["which", "rg"],
            capture_output=True,
        ).stdout.strip()

        if rg_path:
            cmd = ["rg", "--no-heading"]
            if output_mode == "files_with_matches":
                cmd.append("-l")
            cmd.extend([pattern, str(base_path)])
        else:
            cmd = ["grep", "-r"]
            if output_mode == "files_with_matches":
                cmd.append("-l")
            cmd.extend([pattern, str(base_path)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(self.workspace_dir),
        )

        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

        return {
            "matches": lines[:100],
            "total": len(lines),
        }

    async def _bash(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute shell command."""
        command = args["command"]
        timeout = args.get("timeout", 120)

        # Run in workspace directory
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.workspace_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "NO_COLOR": "1",
            },
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            raise

        output = stdout.decode() + stderr.decode()

        # Truncate if too long
        if len(output) > 30000:
            output = output[:30000] + "\n... (truncated)"

        return {
            "output": output,
            "exit_code": process.returncode,
        }

    async def _list_directory(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """List directory contents."""
        path = args.get("path", ".")
        dir_path = self._resolve_path(path)

        if not self._check_path_safety(dir_path):
            raise ValueError(f"Path escapes workspace: {dir_path}")

        if not dir_path.is_dir():
            raise NotADirectoryError(f"Not a directory: {dir_path}")

        entries = []
        for entry in sorted(dir_path.iterdir()):
            entries.append({
                "name": entry.name,
                "type": "directory" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else None,
            })

        return {
            "path": str(dir_path),
            "entries": entries[:100],
            "total": len(entries),
        }
