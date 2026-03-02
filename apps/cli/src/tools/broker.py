"""Tool broker - executes tools locally."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# Optional PDF support
try:
    import pypdf
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

# Task tools
from .task_tools import (
    task_create_handler,
    task_list_handler,
    task_get_handler,
    task_update_handler,
    task_output_handler,
    task_stop_handler,
)
from .task_tools.handlers import init_task_tools

# User tools
from .user_tools import ask_user_question_handler

# Hook tools
from .hook_tools import (
    hook_list_handler,
    hook_add_handler,
    hook_remove_handler,
    hook_enable_handler,
    hook_disable_handler,
)

# Agent tools
from .agent_tools import task_spawn_handler, init_agent_tools

# Skill tools
from .skill_tools import skill_invoke_handler, init_skill_tools

# Plan mode tools
from .plan_tools import enter_plan_mode_handler, exit_plan_mode_handler, init_plan_tools

# Git tools
from .git import (
    git_status_handler,
    git_diff_handler,
    git_commit_handler,
    git_push_handler,
    git_log_handler,
    git_branch_handler,
    set_git_hook_manager,
)
from .git.handlers import init_git_tools

# MCP tools
from .mcp_tools import (
    mcp_invoke_handler,
    mcp_list_tools_handler,
    mcp_server_status_handler,
    mcp_add_server_handler,
    mcp_remove_server_handler,
    mcp_restart_server_handler,
    init_mcp_tools,
)

# Phase 3: Code formatting tools
from .formatter import (
    format_file_handler,
    format_directory_handler,
    detect_formatters_handler,
)

# Phase 3: Profiler tools
from .profiler import (
    profile_python_handler,
    profile_node_handler,
    profile_analyze_handler,
)

# Phase 3: Database tools
from .database import (
    db_connect_handler,
    db_query_handler,
    db_schema_handler,
    db_close_handler,
)

# Phase 3: Debugger tools
from ..debugger import (
    debug_start_handler,
    debug_breakpoint_handler,
    debug_continue_handler,
    debug_stack_handler,
    debug_evaluate_handler,
    debug_stop_handler,
)

# Phase 3: Dependency scanning tools
from ..security.dependency_scanner import scan_dependencies_handler

# Phase 3: CI/CD tools
from ..cicd import (
    ci_status_handler,
    ci_test_results_handler,
    ci_generate_workflow_handler,
    ci_trigger_handler,
)

if TYPE_CHECKING:
    from ..tasks import TaskManager, BackgroundTaskRunner
    from ..agents import AgentRegistry, AgentRunner
    from ..skills import SkillRegistry
    from ..planning import PlanMode
    from ..hooks import HookManager
    from ..mcp import MCPToolProxy
    from ..capsule.manager import CapsuleManager
    from ..capsule.policy import SandboxPolicy

logger = logging.getLogger(__name__)

# File size limits to prevent memory exhaustion DoS
MAX_TEXT_FILE_SIZE = 50 * 1024 * 1024  # 50 MB for text files
MAX_BINARY_FILE_SIZE = 100 * 1024 * 1024  # 100 MB for images/binaries
MAX_PDF_FILE_SIZE = 200 * 1024 * 1024  # 200 MB for PDFs
MAX_NOTEBOOK_FILE_SIZE = 50 * 1024 * 1024  # 50 MB for notebooks


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    duration_ms: int = 0
    truncated: bool = False
    error: Optional[str] = None


@dataclass
class CacheEntry:
    """Cache entry for tool results."""
    result: ToolResult
    timestamp: float
    hits: int = 0


class ToolBroker:
    """Executes tools locally on behalf of the runtime.

    Available tools:
    - read_file: Read file contents (including images, PDFs, notebooks)
    - edit_file: Edit file (old_string -> new_string)
    - write_file: Write new file
    - glob: Find files by pattern
    - grep: Search file contents (with multiline, context, offset support)
    - bash: Execute shell command
    - list_directory: List directory contents
    - web_fetch: Fetch and extract text from a URL
    - web_search: Search the web using DuckDuckGo

    Task Management tools:
    - TaskCreate: Create a new task
    - TaskList: List all tasks
    - TaskGet: Get task details
    - TaskUpdate: Update task status/details
    - TaskOutput: Get background task output
    - TaskStop: Stop running background task

    User Interaction tools:
    - AskUserQuestion: Ask user structured questions with options

    Web tools require: pip install aiohttp beautifulsoup4
    PDF tools require: pip install pypdf
    """

    def __init__(
        self,
        workspace_dir: str,
        task_manager: Optional["TaskManager"] = None,
        background_runner: Optional["BackgroundTaskRunner"] = None,
        agent_registry: Optional["AgentRegistry"] = None,
        agent_runner: Optional["AgentRunner"] = None,
        skill_registry: Optional["SkillRegistry"] = None,
        plan_mode: Optional["PlanMode"] = None,
        mcp_proxy: Optional["MCPToolProxy"] = None,
        hook_manager: Optional["HookManager"] = None,
        capsule_manager: Optional["CapsuleManager"] = None,
        session_id: Optional[str] = None,
        enable_sandbox: bool = False,
    ):
        self.workspace_dir = Path(workspace_dir).resolve()
        logger.info(f"ToolBroker initialized with workspace: {self.workspace_dir}")
        self._hook_manager: Optional["HookManager"] = hook_manager
        self._plan_mode: Optional["PlanMode"] = plan_mode
        self._capsule_manager: Optional["CapsuleManager"] = capsule_manager
        self._session_id = session_id
        self._enable_sandbox = enable_sandbox

        # v4 enhancement: Tool result cache
        self._cache: Dict[str, CacheEntry] = {}
        self._cache_ttl: float = 60.0  # Cache TTL in seconds
        self._cache_enabled: bool = True
        self._cacheable_tools: set[str] = {
            "read_file", "Read",
            "glob", "Glob",
            "grep", "Grep",
            "list_directory",
        }
        self._cache_hits: int = 0
        self._cache_misses: int = 0

        # Initialize capsule manager if sandbox is enabled but no manager provided
        if enable_sandbox and capsule_manager is None and session_id:
            try:
                from ..capsule.manager import CapsuleManager
                from ..capsule.policy import SandboxPolicy
                self._capsule_manager = CapsuleManager(
                    session_id=session_id,
                    workspace_path=str(self.workspace_dir),
                    policy=SandboxPolicy.standard(),
                )
                logger.info("CapsuleManager initialized for sandboxed execution")
            except ImportError:
                logger.warning("Capsule module not available, sandbox disabled")
                self._enable_sandbox = False

        # Initialize all tool subsystems
        init_task_tools(task_manager, background_runner)
        init_agent_tools(agent_registry, agent_runner, str(self.workspace_dir))
        init_skill_tools(skill_registry, self, str(self.workspace_dir))
        init_plan_tools(plan_mode)
        init_git_tools(str(self.workspace_dir))
        init_mcp_tools(mcp_proxy)

        # Tool registry - complete tool set
        self._tools: Dict[str, Callable] = {
            # ===== File Operations =====
            "read_file": self._read_file,
            "Read": self._read_file,  # Alias
            "edit_file": self._edit_file,
            "Edit": self._edit_file,  # Alias
            "write_file": self._write_file,
            "Write": self._write_file,  # Alias
            "glob": self._glob,
            "Glob": self._glob,  # Alias
            "grep": self._grep,
            "Grep": self._grep,  # Alias
            "bash": self._bash,
            "Bash": self._bash,  # Alias
            "list_directory": self._list_directory,

            # ===== Web Tools =====
            "web_fetch": self._web_fetch,
            "WebFetch": self._web_fetch,  # Alias
            "web_search": self._web_search,
            "WebSearch": self._web_search,  # Alias

            # ===== Task Management =====
            "TaskCreate": task_create_handler,
            "TaskList": task_list_handler,
            "TaskGet": task_get_handler,
            "TaskUpdate": task_update_handler,
            "TaskOutput": task_output_handler,
            "TaskStop": task_stop_handler,

            # ===== User Interaction =====
            "AskUserQuestion": ask_user_question_handler,

            # ===== Agent System =====
            "Task": task_spawn_handler,  # Spawn specialized agents

            # ===== Skills =====
            "Skill": skill_invoke_handler,

            # ===== Plan Mode =====
            "EnterPlanMode": enter_plan_mode_handler,
            "ExitPlanMode": exit_plan_mode_handler,

            # ===== Notebook =====
            "NotebookEdit": self._notebook_edit,

            # ===== Git Tools (with safety) =====
            "git_status": git_status_handler,
            "git_diff": git_diff_handler,
            "git_commit": git_commit_handler,
            "git_push": git_push_handler,
            "git_log": git_log_handler,
            "git_branch": git_branch_handler,

            # ===== Hook Management =====
            "hook_list": hook_list_handler,
            "hook_add": hook_add_handler,
            "hook_remove": hook_remove_handler,
            "hook_enable": hook_enable_handler,
            "hook_disable": hook_disable_handler,

            # ===== MCP =====
            "mcp_invoke": mcp_invoke_handler,
            "mcp_list_tools": mcp_list_tools_handler,
            "mcp_server_status": mcp_server_status_handler,
            "mcp_add_server": mcp_add_server_handler,
            "mcp_remove_server": mcp_remove_server_handler,
            "mcp_restart_server": mcp_restart_server_handler,

            # ===== Phase 3: Code Formatting =====
            "format_file": format_file_handler,
            "format_directory": format_directory_handler,
            "detect_formatters": detect_formatters_handler,

            # ===== Phase 3: Profiling =====
            "profile_python": profile_python_handler,
            "profile_node": profile_node_handler,
            "profile_analyze": profile_analyze_handler,

            # ===== Phase 3: Database =====
            "db_connect": db_connect_handler,
            "db_query": db_query_handler,
            "db_schema": db_schema_handler,
            "db_close": db_close_handler,

            # ===== Phase 3: Debugging =====
            "debug_start": debug_start_handler,
            "debug_breakpoint": debug_breakpoint_handler,
            "debug_continue": debug_continue_handler,
            "debug_stack": debug_stack_handler,
            "debug_evaluate": debug_evaluate_handler,
            "debug_stop": debug_stop_handler,

            # ===== Phase 3: Security =====
            "scan_dependencies": scan_dependencies_handler,

            # ===== Phase 3: CI/CD =====
            "ci_status": ci_status_handler,
            "ci_test_results": ci_test_results_handler,
            "ci_generate_workflow": ci_generate_workflow_handler,
            "ci_trigger": ci_trigger_handler,
        }

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        timeout: float = 120.0,
        workspace: Optional[Path] = None,
    ) -> ToolResult:
        """Execute a tool.

        Args:
            tool_name: Name of tool to execute
            args: Tool arguments
            timeout: Execution timeout in seconds
            workspace: Optional workspace path override

        Returns:
            ToolResult with execution details

        Raises:
            ValueError: If tool not found
            asyncio.TimeoutError: If execution times out
        """
        tool = self._tools.get(tool_name)
        if not tool:
            raise ValueError(f"Unknown tool: {tool_name}")

        # Bug fix 2.2: Check for parse error marker in arguments
        if isinstance(args, dict) and args.get("__parse_error__"):
            error_msg = args.get("__error_message__", "Invalid tool arguments")
            logger.error(f"Tool {tool_name} called with invalid arguments: {error_msg}")
            return ToolResult(
                success=False,
                output=None,
                duration_ms=0,
                error=f"Tool arguments parsing failed: {error_msg}",
            )

        # Run pre-tool hooks
        if self._hook_manager:
            try:
                allowed, block_message = await self._hook_manager.on_pre_tool(
                    tool_name=tool_name,
                    args=args,
                )
                if not allowed:
                    return ToolResult(
                        success=False,
                        output=None,
                        duration_ms=0,
                        error=f"Blocked by hook: {block_message}",
                    )
            except Exception as e:
                # Hook errors: store and show friendly message
                logger.error(f"Pre-tool hook error: {e}")
                from ..errors import report_error, ErrorContext, GlockClientError
                report_error(
                    e,
                    component="tools.broker.pre_hook",
                    context=ErrorContext(
                        component="tools.broker",
                        tool_name=tool_name,
                    ),
                    reraise=False,
                )
                raise GlockClientError(
                    f"Pre-tool hook failed: {e}",
                    original_error=e,
                    context=ErrorContext(component="tools.broker", tool_name=tool_name),
                ) from e

        # Plan mode enforcement - restrict tools when plan is active
        if self._plan_mode and self._plan_mode.is_active:
            enforcement = self._check_plan_enforcement(tool_name, args)
            if not enforcement.get("allowed", True):
                return ToolResult(
                    success=False,
                    output=None,
                    duration_ms=0,
                    error=enforcement.get("reason", "Tool not allowed by plan"),
                )

        start_time = time.time()

        # v4 enhancement: Check cache for cacheable tools
        cache_key = None
        if self._cache_enabled and tool_name in self._cacheable_tools:
            cache_key = self._get_cache_key(tool_name, args)
            cached = self._get_cached(cache_key)
            if cached is not None:
                self._cache_hits += 1
                logger.debug(f"Cache hit for {tool_name}")
                return cached

        try:
            # Execute with timeout
            result = await asyncio.wait_for(
                tool(args),
                timeout=timeout,
            )

            duration_ms = int((time.time() - start_time) * 1000)

            # Check if result indicates success
            if isinstance(result, dict):
                success = result.get("status") != "error"
                error = result.get("error") if not success else None
                truncated = result.get("truncated", False)
            else:
                success = True
                error = None
                truncated = False

            tool_result = ToolResult(
                success=success,
                output=result,
                duration_ms=duration_ms,
                truncated=truncated,
                error=error,
            )

            # v4 enhancement: Cache successful results
            if cache_key and success:
                self._set_cached(cache_key, tool_result)
                self._cache_misses += 1

            # Run post-tool hooks
            if self._hook_manager:
                try:
                    await self._hook_manager.on_post_tool(
                        tool_name=tool_name,
                        args=args,
                        result=result if isinstance(result, dict) else {"output": result},
                        success=success,
                    )
                except Exception as e:
                    # Post-tool hook errors: store and show friendly message
                    logger.error(f"Post-tool hook error: {e}")
                    from ..errors import report_error, ErrorContext, GlockClientError
                    report_error(
                        e,
                        component="tools.broker.post_hook",
                        context=ErrorContext(
                            component="tools.broker",
                            tool_name=tool_name,
                        ),
                        reraise=False,
                    )
                    raise GlockClientError(
                        f"Post-tool hook failed: {e}",
                        original_error=e,
                        context=ErrorContext(component="tools.broker", tool_name=tool_name),
                    ) from e

            return tool_result

        except asyncio.TimeoutError:
            duration_ms = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                output=None,
                duration_ms=duration_ms,
                error=f"Tool execution timed out after {timeout}s",
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                output=None,
                duration_ms=duration_ms,
                error=str(e),
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

    def _safe_open_file(self, file_path: Path, max_size: int, mode: str = "r") -> tuple:
        """Safely open a file with TOCTOU protection.

        Opens the file using file descriptor to prevent symlink attacks,
        then re-verifies the path is within workspace.

        Args:
            file_path: Path to open
            max_size: Maximum allowed file size
            mode: Open mode ('r' for text, 'rb' for binary)

        Returns:
            Tuple of (file_handle, file_size)

        Raises:
            ValueError: If path escapes workspace or file too large
            FileNotFoundError: If file doesn't exist
        """
        # First check: path safety before opening
        if not self._check_path_safety(file_path):
            raise ValueError(f"Path escapes workspace: {file_path}")

        # Open file by path - this follows symlinks
        try:
            fd = os.open(str(file_path), os.O_RDONLY)
        except FileNotFoundError:
            raise FileNotFoundError(f"File not found: {file_path}")

        try:
            # Get the real path via the file descriptor (TOCTOU protection)
            # This ensures we're checking the ACTUAL file we opened
            real_path = Path(f"/dev/fd/{fd}").resolve() if os.name != 'nt' else file_path.resolve()

            # Re-check path safety after opening (prevents symlink swap attacks)
            try:
                real_path.relative_to(self.workspace_dir)
            except ValueError:
                os.close(fd)
                raise ValueError(f"Path escapes workspace (symlink detected): {file_path}")

            # Check file size before reading
            file_stat = os.fstat(fd)
            file_size = file_stat.st_size

            if file_size > max_size:
                os.close(fd)
                raise ValueError(
                    f"File too large: {file_size:,} bytes "
                    f"(max {max_size:,} bytes). Path: {file_path}"
                )

            # Convert fd to file object
            file_handle = os.fdopen(fd, mode)
            return file_handle, file_size

        except Exception:
            # Ensure fd is closed on any error
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    async def _read_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read file contents.

        Supports:
        - Text files: Read as text with optional offset/limit
        - Images (png, jpg, gif, webp, svg): Return base64 encoded
        - PDFs: Extract text and images per page
        - Jupyter notebooks (.ipynb): Return all cells with outputs

        Security:
        - File size is checked before reading to prevent memory exhaustion
        - TOCTOU attacks are mitigated by re-checking path after opening
        """
        file_path = self._resolve_path(args["file_path"])

        if not self._check_path_safety(file_path):
            raise ValueError(f"Path escapes workspace: {file_path}")

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        suffix = file_path.suffix.lower()

        # Handle images
        image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
        if suffix in image_extensions:
            return await self._read_image(file_path)

        # Handle PDFs
        if suffix == ".pdf":
            return await self._read_pdf(file_path)

        # Handle Jupyter notebooks
        if suffix == ".ipynb":
            return await self._read_notebook(file_path)

        # Handle text files with size check and TOCTOU protection
        try:
            file_handle, file_size = self._safe_open_file(
                file_path, MAX_TEXT_FILE_SIZE, mode="r"
            )
            with file_handle:
                content = file_handle.read()
        except UnicodeDecodeError:
            # Try reading as binary and return base64
            file_handle, file_size = self._safe_open_file(
                file_path, MAX_BINARY_FILE_SIZE, mode="rb"
            )
            with file_handle:
                content_bytes = file_handle.read()
            return {
                "content_type": "binary",
                "content_base64": base64.b64encode(content_bytes).decode(),
                "path": str(file_path),
                "size": file_size,
            }

        # Apply offset/limit if specified
        offset = args.get("offset", 0)
        limit = args.get("limit", 2000)

        lines = content.split("\n")
        total_lines = len(lines)

        if offset:
            lines = lines[offset:]
        if limit:
            lines = lines[:limit]

        # Format with line numbers
        numbered_lines = []
        for i, line in enumerate(lines, start=(offset or 0) + 1):
            # Truncate long lines
            if len(line) > 2000:
                line = line[:2000] + "..."
            numbered_lines.append(f"{i:>6}\t{line}")

        content = "\n".join(numbered_lines)

        return {
            "content": content,
            "path": str(file_path),
            "size": file_size,
            "total_lines": total_lines,
        }

    async def _read_image(self, file_path: Path) -> Dict[str, Any]:
        """Read an image file and return base64 encoded content."""
        # Use safe open with size limit and TOCTOU protection
        file_handle, file_size = self._safe_open_file(
            file_path, MAX_BINARY_FILE_SIZE, mode="rb"
        )
        with file_handle:
            content_bytes = file_handle.read()

        # Determine mime type
        suffix = file_path.suffix.lower()
        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
            ".bmp": "image/bmp",
            ".ico": "image/x-icon",
        }
        mime_type = mime_types.get(suffix, "application/octet-stream")

        return {
            "content_type": "image",
            "mime_type": mime_type,
            "content_base64": base64.b64encode(content_bytes).decode(),
            "path": str(file_path),
            "size": file_size,
        }

    async def _read_pdf(self, file_path: Path) -> Dict[str, Any]:
        """Read a PDF file and extract text per page."""
        if not PYPDF_AVAILABLE:
            return {
                "status": "error",
                "error": "pypdf is required for PDF reading. Install with: pip install pypdf",
                "path": str(file_path),
            }

        # Check file size before processing
        file_size = file_path.stat().st_size
        if file_size > MAX_PDF_FILE_SIZE:
            return {
                "status": "error",
                "error": f"PDF too large: {file_size:,} bytes (max {MAX_PDF_FILE_SIZE:,} bytes)",
                "path": str(file_path),
            }

        # Re-check path safety (TOCTOU protection)
        if not self._check_path_safety(file_path.resolve()):
            raise ValueError(f"Path escapes workspace (symlink detected): {file_path}")

        try:
            reader = pypdf.PdfReader(str(file_path))
            pages = []

            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                pages.append({
                    "page_number": i + 1,
                    "text": text,
                })

            return {
                "content_type": "pdf",
                "path": str(file_path),
                "size": file_path.stat().st_size,
                "page_count": len(pages),
                "pages": pages,
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"Failed to read PDF: {str(e)}",
                "path": str(file_path),
            }

    async def _read_notebook(self, file_path: Path) -> Dict[str, Any]:
        """Read a Jupyter notebook and return cells with outputs."""
        try:
            # Use safe open with size limit and TOCTOU protection
            file_handle, file_size = self._safe_open_file(
                file_path, MAX_NOTEBOOK_FILE_SIZE, mode="r"
            )
            with file_handle:
                notebook = json.load(file_handle)

            cells = []
            for i, cell in enumerate(notebook.get("cells", [])):
                cell_info = {
                    "cell_number": i,
                    "cell_type": cell.get("cell_type", "code"),
                    "source": "".join(cell.get("source", [])),
                }

                # Include cell ID if present
                if "id" in cell:
                    cell_info["cell_id"] = cell["id"]

                # Include outputs for code cells
                if cell.get("cell_type") == "code":
                    outputs = []
                    for output in cell.get("outputs", []):
                        output_type = output.get("output_type")
                        if output_type == "stream":
                            outputs.append({
                                "type": "stream",
                                "name": output.get("name", "stdout"),
                                "text": "".join(output.get("text", [])),
                            })
                        elif output_type == "execute_result":
                            data = output.get("data", {})
                            if "text/plain" in data:
                                outputs.append({
                                    "type": "execute_result",
                                    "text": "".join(data["text/plain"]),
                                })
                        elif output_type == "error":
                            outputs.append({
                                "type": "error",
                                "ename": output.get("ename", ""),
                                "evalue": output.get("evalue", ""),
                            })

                    if outputs:
                        cell_info["outputs"] = outputs

                cells.append(cell_info)

            return {
                "content_type": "notebook",
                "path": str(file_path),
                "size": file_size,
                "cell_count": len(cells),
                "cells": cells,
            }
        except json.JSONDecodeError as e:
            return {
                "status": "error",
                "error": f"Invalid notebook format: {str(e)}",
                "path": str(file_path),
            }
        except ValueError as e:
            # File size or path safety error
            return {
                "status": "error",
                "error": str(e),
                "path": str(file_path),
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"Failed to read notebook: {str(e)}",
                "path": str(file_path),
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
        """Search file contents with ripgrep.

        Enhanced grep with support for:
        - Multiline matching
        - Context lines (-A/-B/-C)
        - Head limit with offset
        - File type filtering
        - Glob patterns
        - Case insensitive search
        """
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

            # Output mode
            if output_mode == "files_with_matches":
                cmd.append("-l")
            elif output_mode == "count":
                cmd.append("-c")
            else:  # content mode
                # Line numbers
                if args.get("-n", True):
                    cmd.append("-n")

            # Case insensitive
            if args.get("-i"):
                cmd.append("-i")

            # Multiline mode
            if args.get("multiline"):
                cmd.extend(["-U", "--multiline-dotall"])

            # Context lines (only for content mode)
            if output_mode == "content":
                if args.get("context") or args.get("-C"):
                    cmd.extend(["-C", str(args.get("context") or args.get("-C"))])
                else:
                    if args.get("-A"):
                        cmd.extend(["-A", str(args.get("-A"))])
                    if args.get("-B"):
                        cmd.extend(["-B", str(args.get("-B"))])

            # File type filter
            if args.get("type"):
                cmd.extend(["--type", args["type"]])

            # Glob pattern
            if args.get("glob"):
                cmd.extend(["--glob", args["glob"]])

            cmd.extend([pattern, str(base_path)])
        else:
            # Fallback to grep
            cmd = ["grep", "-r"]
            if output_mode == "files_with_matches":
                cmd.append("-l")
            if args.get("-i"):
                cmd.append("-i")
            cmd.extend([pattern, str(base_path)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(self.workspace_dir),
        )

        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

        # Apply offset and head_limit
        offset = args.get("offset", 0)
        head_limit = args.get("head_limit", 0)

        if offset:
            lines = lines[offset:]

        if head_limit and head_limit > 0:
            lines = lines[:head_limit]
        else:
            lines = lines[:100]  # Default limit

        return {
            "matches": lines,
            "total": len(lines),
        }

    async def _bash(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute shell command.

        If sandbox is enabled and capsule manager is available, executes
        command through the sandbox. Otherwise executes directly.
        """
        command = args["command"]
        timeout = args.get("timeout", 120)
        env = args.get("env")

        # Use capsule manager if sandbox is enabled
        if self._enable_sandbox and self._capsule_manager is not None:
            return await self._bash_sandboxed(command, timeout, env)

        # Direct execution (no sandbox)
        return await self._bash_direct(command, timeout, env)

    async def _bash_sandboxed(
        self,
        command: str,
        timeout: float,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Execute command through capsule sandbox."""
        try:
            exit_code, stdout, stderr = await self._capsule_manager.execute(
                command=command,
                timeout=timeout,
                env=env,
            )

            output = stdout + stderr

            # Truncate if too long
            if len(output) > 30000:
                output = output[:30000] + "\n... (truncated)"

            return {
                "output": output,
                "exit_code": exit_code,
                "sandboxed": True,
            }
        except Exception as e:
            logger.error(f"Sandboxed execution failed: {e}")
            return {
                "output": "",
                "exit_code": -1,
                "error": str(e),
                "sandboxed": True,
            }

    async def _bash_direct(
        self,
        command: str,
        timeout: float,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Execute command directly (no sandbox)."""
        env_dict = {**os.environ, "NO_COLOR": "1"}
        if env:
            env_dict.update(env)

        # Run in workspace directory
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.workspace_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env_dict,
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
            "sandboxed": False,
        }

    async def _list_directory(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """List directory contents."""
        path = args.get("path", ".")
        dir_path = self._resolve_path(path)
        logger.debug(f"list_directory: path={path}, resolved={dir_path}, workspace={self.workspace_dir}")

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

        logger.info(f"list_directory: found {len(entries)} entries in {dir_path}")
        if entries:
            logger.debug(f"list_directory first 5: {[e['name'] for e in entries[:5]]}")

        return {
            "path": str(dir_path),
            "entries": entries[:100],
            "total": len(entries),
        }

    async def _web_fetch(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch content from a URL and extract text.

        Args:
            url: The URL to fetch
            prompt: Optional prompt to guide content extraction
            extract_links: Whether to extract links (default False)
            max_length: Maximum content length to return (default 50000)
        """
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError(
                "aiohttp is required for web_fetch. "
                "Install with: pip install aiohttp"
            )

        url = args["url"]
        prompt = args.get("prompt", "")
        extract_links = args.get("extract_links", False)
        max_length = args.get("max_length", 50000)

        # Validate URL
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Invalid URL scheme: {parsed.scheme}. Only http/https allowed.")

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; GlockBot/1.0; +https://github.com/glock)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                    allow_redirects=True,
                ) as response:
                    # Check for redirect to different host
                    if response.history:
                        final_url = str(response.url)
                        original_host = urllib.parse.urlparse(url).netloc
                        final_host = urllib.parse.urlparse(final_url).netloc
                        if original_host != final_host:
                            return {
                                "status": "redirect",
                                "message": f"Redirected to different host: {final_host}",
                                "redirect_url": final_url,
                                "original_url": url,
                            }

                    content_type = response.headers.get("Content-Type", "")
                    status_code = response.status

                    if status_code >= 400:
                        return {
                            "status": "error",
                            "error": f"HTTP {status_code}",
                            "url": url,
                        }

                    # Read content
                    raw_content = await response.text()

        except aiohttp.ClientError as e:
            return {
                "status": "error",
                "error": str(e),
                "url": url,
            }
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "error": "Request timed out",
                "url": url,
            }

        # Extract text content
        text_content, links = self._extract_text_from_html(raw_content, url)

        # Truncate if needed
        if len(text_content) > max_length:
            text_content = text_content[:max_length] + "\n\n... (content truncated)"

        result: Dict[str, Any] = {
            "status": "success",
            "url": url,
            "content_type": content_type,
            "content": text_content,
            "content_length": len(text_content),
        }

        if extract_links:
            result["links"] = links[:50]  # Limit links returned

        if prompt:
            result["extraction_prompt"] = prompt

        return result

    def _extract_text_from_html(self, html_content: str, base_url: str) -> tuple[str, list[Dict[str, str]]]:
        """Extract readable text and links from HTML content."""
        links: list[Dict[str, str]] = []

        if BS4_AVAILABLE:
            soup = BeautifulSoup(html_content, "html.parser")

            # Remove script and style elements
            for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
                element.decompose()

            # Extract links
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                if href and text:
                    # Make absolute URL
                    if not href.startswith(("http://", "https://")):
                        href = urllib.parse.urljoin(base_url, href)
                    links.append({"text": text[:100], "url": href})

            # Get text
            text = soup.get_text(separator="\n", strip=True)

            # Clean up whitespace
            lines = [line.strip() for line in text.split("\n")]
            text = "\n".join(line for line in lines if line)

        else:
            # Fallback: simple regex-based extraction
            # Remove script/style tags
            text = re.sub(r"<script[^>]*>.*?</script>", "", html_content, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)

            # Extract links
            for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>', text, re.IGNORECASE):
                href, link_text = match.groups()
                if href and link_text.strip():
                    if not href.startswith(("http://", "https://")):
                        href = urllib.parse.urljoin(base_url, href)
                    links.append({"text": link_text.strip()[:100], "url": href})

            # Remove all HTML tags
            text = re.sub(r"<[^>]+>", " ", text)

            # Decode HTML entities
            text = html.unescape(text)

            # Clean up whitespace
            text = re.sub(r"\s+", " ", text).strip()

        return text, links

    async def _web_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Search the web using DuckDuckGo.

        Args:
            query: Search query
            max_results: Maximum number of results (default 10)
            allowed_domains: Only include results from these domains
            blocked_domains: Exclude results from these domains
        """
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError(
                "aiohttp is required for web_search. "
                "Install with: pip install aiohttp"
            )

        query = args["query"]
        max_results = args.get("max_results", 10)
        allowed_domains = args.get("allowed_domains", [])
        blocked_domains = args.get("blocked_domains", [])

        # Use DuckDuckGo HTML search (no API key required)
        search_url = "https://html.duckduckgo.com/html/"

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; GlockBot/1.0)",
            "Accept": "text/html",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    search_url,
                    data={"q": query},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        return {
                            "status": "error",
                            "error": f"Search failed with status {response.status}",
                            "query": query,
                        }

                    html_content = await response.text()

        except aiohttp.ClientError as e:
            return {
                "status": "error",
                "error": str(e),
                "query": query,
            }
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "error": "Search request timed out",
                "query": query,
            }

        # Parse search results (get more than needed for filtering)
        all_results = self._parse_duckduckgo_results(html_content, max_results * 3)

        # Apply domain filters
        filtered_results = []
        for result in all_results:
            url = result.get("url", "")
            if url:
                domain = urllib.parse.urlparse(url).netloc.lower()

                # Check blocked domains
                if blocked_domains:
                    blocked = False
                    for blocked_domain in blocked_domains:
                        if blocked_domain.lower() in domain:
                            blocked = True
                            break
                    if blocked:
                        continue

                # Check allowed domains
                if allowed_domains:
                    allowed = False
                    for allowed_domain in allowed_domains:
                        if allowed_domain.lower() in domain:
                            allowed = True
                            break
                    if not allowed:
                        continue

            filtered_results.append(result)
            if len(filtered_results) >= max_results:
                break

        return {
            "status": "success",
            "query": query,
            "results": filtered_results,
            "result_count": len(filtered_results),
        }

    def _parse_duckduckgo_results(self, html_content: str, max_results: int) -> list[Dict[str, str]]:
        """Parse DuckDuckGo HTML search results."""
        results: list[Dict[str, str]] = []

        if BS4_AVAILABLE:
            soup = BeautifulSoup(html_content, "html.parser")

            # Find result divs
            for result in soup.select(".result"):
                if len(results) >= max_results:
                    break

                # Get title and URL
                title_elem = result.select_one(".result__title a")
                snippet_elem = result.select_one(".result__snippet")

                if title_elem:
                    title = title_elem.get_text(strip=True)
                    url = title_elem.get("href", "")

                    # DuckDuckGo uses redirect URLs, extract actual URL
                    if "uddg=" in url:
                        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                        url = parsed.get("uddg", [url])[0]

                    snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

                    results.append({
                        "title": title,
                        "url": url,
                        "snippet": snippet,
                    })
        else:
            # Fallback regex parsing
            pattern = r'class="result__title"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>'
            for match in re.finditer(pattern, html_content, re.DOTALL):
                if len(results) >= max_results:
                    break

                url, title = match.groups()

                # Extract actual URL from DuckDuckGo redirect
                if "uddg=" in url:
                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                    url = parsed.get("uddg", [url])[0]

                results.append({
                    "title": html.unescape(title.strip()),
                    "url": url,
                    "snippet": "",
                })

        return results

    async def _notebook_edit(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Edit a Jupyter notebook cell.

        Args:
            notebook_path: Absolute path to the .ipynb file
            new_source: New source code for the cell
            cell_id: ID of cell to edit (for insert, new cell goes after this)
            cell_type: "code" or "markdown" (required for insert)
            edit_mode: "replace", "insert", or "delete" (default: replace)

        Returns:
            Dictionary with status and updated cell info
        """
        notebook_path = self._resolve_path(args["notebook_path"])
        new_source = args.get("new_source", "")
        cell_id = args.get("cell_id")
        cell_type = args.get("cell_type")
        edit_mode = args.get("edit_mode", "replace")

        if not self._check_path_safety(notebook_path):
            raise ValueError(f"Path escapes workspace: {notebook_path}")

        if not notebook_path.exists():
            raise FileNotFoundError(f"Notebook not found: {notebook_path}")

        if notebook_path.suffix.lower() != ".ipynb":
            raise ValueError(f"Not a notebook file: {notebook_path}")

        # Load notebook
        try:
            with open(notebook_path, "r") as f:
                notebook = json.load(f)
        except json.JSONDecodeError as e:
            return {
                "status": "error",
                "error": f"Invalid notebook format: {str(e)}",
            }
        except PermissionError as e:
            raise PermissionError(f"Cannot read notebook - permission denied: {notebook_path}") from e
        except OSError as e:
            raise OSError(f"Cannot read notebook - file system error: {e}") from e

        cells = notebook.get("cells", [])

        # Find cell by ID
        cell_index = None
        if cell_id:
            for i, cell in enumerate(cells):
                if cell.get("id") == cell_id:
                    cell_index = i
                    break
            if cell_index is None:
                return {
                    "status": "error",
                    "error": f"Cell not found with ID: {cell_id}",
                }

        if edit_mode == "delete":
            if cell_index is None:
                return {
                    "status": "error",
                    "error": "cell_id is required for delete mode",
                }

            deleted_cell = cells.pop(cell_index)
            notebook["cells"] = cells

            # Save notebook
            with open(notebook_path, "w") as f:
                json.dump(notebook, f, indent=1)

            return {
                "status": "success",
                "action": "deleted",
                "cell_index": cell_index,
                "cell_type": deleted_cell.get("cell_type"),
            }

        elif edit_mode == "insert":
            if not cell_type:
                return {
                    "status": "error",
                    "error": "cell_type is required for insert mode",
                }

            if cell_type not in ("code", "markdown"):
                return {
                    "status": "error",
                    "error": f"Invalid cell_type: {cell_type}. Must be 'code' or 'markdown'",
                }

            # Create new cell
            import uuid
            new_cell = {
                "id": str(uuid.uuid4())[:8],
                "cell_type": cell_type,
                "source": new_source.split("\n") if new_source else [],
                "metadata": {},
            }

            if cell_type == "code":
                new_cell["outputs"] = []
                new_cell["execution_count"] = None

            # Insert after specified cell or at beginning
            insert_index = (cell_index + 1) if cell_index is not None else 0
            cells.insert(insert_index, new_cell)
            notebook["cells"] = cells

            # Save notebook
            with open(notebook_path, "w") as f:
                json.dump(notebook, f, indent=1)

            return {
                "status": "success",
                "action": "inserted",
                "cell_index": insert_index,
                "cell_id": new_cell["id"],
                "cell_type": cell_type,
            }

        else:  # replace mode
            if cell_index is None:
                return {
                    "status": "error",
                    "error": "cell_id is required for replace mode",
                }

            cell = cells[cell_index]
            old_type = cell.get("cell_type", "code")

            # Update cell type if specified
            if cell_type and cell_type != old_type:
                cell["cell_type"] = cell_type
                if cell_type == "code" and "outputs" not in cell:
                    cell["outputs"] = []
                    cell["execution_count"] = None

            # Update source
            cell["source"] = new_source.split("\n") if new_source else []

            # Clear outputs for code cells when editing
            if cell.get("cell_type") == "code":
                cell["outputs"] = []
                cell["execution_count"] = None

            notebook["cells"] = cells

            # Save notebook
            with open(notebook_path, "w") as f:
                json.dump(notebook, f, indent=1)

            return {
                "status": "success",
                "action": "replaced",
                "cell_index": cell_index,
                "cell_id": cell.get("id"),
                "cell_type": cell.get("cell_type"),
            }

    # ==================== Subsystem Setters ====================
    # These allow deferred initialization after connection is established

    def set_task_manager(self, task_manager: "TaskManager") -> None:
        """Set the task manager after initialization."""
        init_task_tools(task_manager, None)

    def set_background_runner(self, runner: "BackgroundTaskRunner") -> None:
        """Set the background task runner after initialization."""
        from .task_tools.handlers import set_background_runner
        set_background_runner(runner)

    def set_agent_registry(self, registry: "AgentRegistry") -> None:
        """Set the agent registry after initialization."""
        from .agent_tools import set_agent_registry
        set_agent_registry(registry)

    def set_agent_runner(self, runner: "AgentRunner") -> None:
        """Set the agent runner after initialization."""
        from .agent_tools import set_agent_runner
        set_agent_runner(runner)

    def set_skill_registry(self, registry: "SkillRegistry") -> None:
        """Set the skill registry after initialization."""
        from .skill_tools import set_skill_registry
        set_skill_registry(registry)

    def set_plan_mode(self, plan_mode: "PlanMode") -> None:
        """Set the plan mode after initialization."""
        self._plan_mode = plan_mode
        from .plan_tools import set_plan_mode
        set_plan_mode(plan_mode)

    def set_hook_manager(self, hook_manager: "HookManager") -> None:
        """Set the hook manager after initialization."""
        self._hook_manager = hook_manager
        from .hook_tools import set_hook_manager
        set_hook_manager(hook_manager)
        # Also set for git tools (pre-commit, post-commit hooks)
        set_git_hook_manager(hook_manager)

    def set_mcp_proxy(self, mcp_proxy: "MCPToolProxy") -> None:
        """Set the MCP tool proxy after initialization."""
        from .mcp_tools import set_mcp_proxy
        set_mcp_proxy(mcp_proxy)

    def set_capsule_manager(self, capsule_manager: "CapsuleManager") -> None:
        """Set the capsule manager for sandboxed execution."""
        self._capsule_manager = capsule_manager
        self._enable_sandbox = True
        logger.info("CapsuleManager set for sandboxed execution")

    def enable_sandbox(self, enable: bool = True) -> None:
        """Enable or disable sandboxed execution."""
        self._enable_sandbox = enable and self._capsule_manager is not None
        logger.info(f"Sandbox {'enabled' if self._enable_sandbox else 'disabled'}")

    async def get_sandbox_status(self) -> Dict[str, Any]:
        """Get sandbox status."""
        if self._capsule_manager is None:
            return {
                "enabled": False,
                "available": False,
                "reason": "No capsule manager configured",
            }

        status = await self._capsule_manager.get_status()
        return {
            "enabled": self._enable_sandbox,
            "available": True,
            **status,
        }

    def _check_plan_enforcement(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Check if tool is allowed by the current plan.

        Args:
            tool_name: Name of the tool
            args: Tool arguments

        Returns:
            Dict with 'allowed' bool and optional 'reason'
        """
        if not self._plan_mode or not self._plan_mode.is_active:
            return {"allowed": True}

        # Get plan mode state
        state = self._plan_mode.state.value

        # In planning state, only allow read-only tools
        if state == "planning":
            read_only_tools = {
                "read_file", "Read",
                "glob", "Glob",
                "grep", "Grep",
                "list_directory",
                "web_fetch", "WebFetch",
                "web_search", "WebSearch",
                "TaskList", "TaskGet",
                "git_status", "git_diff", "git_log",
                "mcp_list_tools", "mcp_server_status",
                "hook_list",
            }

            if tool_name not in read_only_tools:
                return {
                    "allowed": False,
                    "reason": f"Tool '{tool_name}' not allowed during planning. Only read-only tools are permitted.",
                }

        # In pending_approval state, no tools allowed except plan status checks
        elif state == "pending_approval":
            allowed_tools = {"get_plan_status", "approve_plan", "reject_plan"}
            if tool_name not in allowed_tools:
                return {
                    "allowed": False,
                    "reason": f"Tool '{tool_name}' not allowed while plan is pending approval.",
                }

        # In executing state, check against allowed_prompts from the plan
        elif state == "executing":
            # Get allowed tool categories from plan
            allowed_prompts = self._plan_mode._context.allowed_prompts or []

            # If allowed_prompts is empty, allow all tools (no restrictions)
            if not allowed_prompts:
                return {"allowed": True}

            # Check if tool matches any allowed prompt category
            # This is a simple check - can be extended for more sophisticated matching
            tool_lower = tool_name.lower()
            for prompt in allowed_prompts:
                prompt_lower = prompt.lower()
                if tool_lower in prompt_lower or prompt_lower in tool_lower:
                    return {"allowed": True}

            # Check common tool categories
            if any(cat in args.get("prompt", "").lower() for cat in ["bash", "edit", "write", "read"]):
                for prompt in allowed_prompts:
                    if any(keyword in prompt.lower() for keyword in ["bash", "edit", "write", "run"]):
                        return {"allowed": True}

        return {"allowed": True}

    # ==================== v4 Cache Methods ====================

    def _get_cache_key(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Generate a cache key for a tool call."""
        import hashlib
        # Create a deterministic key from tool name and args
        args_str = json.dumps(args, sort_keys=True)
        key_str = f"{tool_name}:{args_str}"
        return hashlib.md5(key_str.encode()).hexdigest()

    def _get_cached(self, cache_key: str) -> Optional[ToolResult]:
        """Get a cached result if valid."""
        entry = self._cache.get(cache_key)
        if entry is None:
            return None

        # Check TTL
        if time.time() - entry.timestamp > self._cache_ttl:
            del self._cache[cache_key]
            return None

        entry.hits += 1
        return entry.result

    def _set_cached(self, cache_key: str, result: ToolResult) -> None:
        """Cache a tool result."""
        self._cache[cache_key] = CacheEntry(
            result=result,
            timestamp=time.time(),
        )

        # Bug fix 5.4: Proper LRU eviction with size-based cleanup
        # When cache exceeds max size, reduce to 75% capacity
        max_cache_size = 1000
        target_size = int(max_cache_size * 0.75)  # 750 entries

        if len(self._cache) > max_cache_size:
            # Remove oldest entries until we reach target size
            entries_to_remove = len(self._cache) - target_size
            sorted_keys = sorted(
                self._cache.keys(),
                key=lambda k: (self._cache[k].hits, self._cache[k].timestamp),  # LRU: least hits, then oldest
            )
            for key in sorted_keys[:entries_to_remove]:
                del self._cache[key]

    def invalidate_cache_for_file(self, file_path: str) -> None:
        """Invalidate cache entries related to a file.

        Call this after edit_file/write_file to ensure fresh reads.
        """
        file_path_str = str(file_path)
        keys_to_delete = []

        for key, entry in self._cache.items():
            # Check if the cached result relates to this file
            output = entry.result.output
            if isinstance(output, dict):
                cached_path = output.get("path", "")
                if cached_path and file_path_str in cached_path:
                    keys_to_delete.append(key)

        for key in keys_to_delete:
            del self._cache[key]

        if keys_to_delete:
            logger.debug(f"Invalidated {len(keys_to_delete)} cache entries for {file_path}")

    def clear_cache(self) -> None:
        """Clear all cached results."""
        self._cache.clear()
        logger.debug("Cache cleared")

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            "entries": len(self._cache),
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate": self._cache_hits / (self._cache_hits + self._cache_misses)
                       if (self._cache_hits + self._cache_misses) > 0 else 0,
            "enabled": self._cache_enabled,
            "ttl_seconds": self._cache_ttl,
        }

    def set_cache_enabled(self, enabled: bool) -> None:
        """Enable or disable caching."""
        self._cache_enabled = enabled
        if not enabled:
            self.clear_cache()

    def set_cache_ttl(self, ttl_seconds: float) -> None:
        """Set cache TTL."""
        self._cache_ttl = ttl_seconds
