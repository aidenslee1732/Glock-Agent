"""TUI components for enhanced visualization.

Phase 4 TUI improvements:
- Parallel agent progress visualization
- Collapsible tool output
- Progress bars
- Status line
- File tree view
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskID
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from rich.live import Live
from rich.layout import Layout
from rich.style import Style


# Color palette
ACCENT = "#10b981"
ACCENT_DIM = "#065f46"
MUTED = "#6b7280"
WARNING = "#f59e0b"
ERROR = "#ef4444"
SUCCESS = "#10b981"
INFO = "#3b82f6"


class ToolStatus(Enum):
    """Status of a tool execution."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ToolExecution:
    """Tracks a single tool execution."""
    tool_name: str
    args: dict[str, Any]
    status: ToolStatus = ToolStatus.PENDING
    result: Optional[dict] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    collapsed: bool = True

    def duration_ms(self) -> int:
        """Get execution duration in milliseconds."""
        if self.start_time and self.end_time:
            return int((self.end_time - self.start_time).total_seconds() * 1000)
        return 0


@dataclass
class ParallelBatch:
    """A batch of parallel tool executions."""
    tools: list[ToolExecution] = field(default_factory=list)
    label: str = ""

    def add(self, tool: ToolExecution) -> None:
        """Add a tool to the batch."""
        self.tools.append(tool)

    def completed_count(self) -> int:
        """Get count of completed tools."""
        return sum(1 for t in self.tools if t.status == ToolStatus.COMPLETED)

    def total_count(self) -> int:
        """Get total tool count."""
        return len(self.tools)

    def is_complete(self) -> bool:
        """Check if all tools are complete."""
        return all(
            t.status in (ToolStatus.COMPLETED, ToolStatus.FAILED)
            for t in self.tools
        )


class ParallelProgressTracker:
    """Tracks parallel tool executions for visualization.

    Implements Claude Code-style aggregate display:
    ⟳ Reading 5 files...
      ├─ src/auth.py ✓
      ├─ src/main.py ✓
      ├─ src/utils.py ⟳
      ├─ tests/test_auth.py
      └─ tests/conftest.py
    """

    def __init__(self, console: Console):
        """Initialize tracker.

        Args:
            console: Rich console for output
        """
        self.console = console
        self._current_batch: Optional[ParallelBatch] = None
        self._batches: list[ParallelBatch] = []

    def start_batch(self, label: str = "") -> ParallelBatch:
        """Start a new parallel batch.

        Args:
            label: Batch description (e.g., "Reading files")

        Returns:
            New ParallelBatch
        """
        batch = ParallelBatch(label=label)
        self._current_batch = batch
        self._batches.append(batch)
        return batch

    def add_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> ToolExecution:
        """Add a tool to the current batch.

        Args:
            tool_name: Tool name
            args: Tool arguments

        Returns:
            ToolExecution object
        """
        if self._current_batch is None:
            self.start_batch()

        tool = ToolExecution(
            tool_name=tool_name,
            args=args,
            status=ToolStatus.RUNNING,
            start_time=datetime.now(),
        )
        self._current_batch.add(tool)
        return tool

    def complete_tool(
        self,
        tool: ToolExecution,
        result: dict,
        success: bool = True,
    ) -> None:
        """Mark a tool as complete.

        Args:
            tool: Tool to complete
            result: Tool result
            success: Whether execution succeeded
        """
        tool.status = ToolStatus.COMPLETED if success else ToolStatus.FAILED
        tool.result = result
        tool.end_time = datetime.now()

    def render_batch(self, batch: ParallelBatch) -> RenderableType:
        """Render a parallel batch.

        Args:
            batch: Batch to render

        Returns:
            Rich renderable
        """
        if not batch.tools:
            return Text()

        # Determine batch label from tool types
        if not batch.label:
            tool_types = set(t.tool_name for t in batch.tools)
            if tool_types == {"read_file"} or tool_types == {"Read"}:
                batch.label = "Reading"
            elif tool_types == {"glob"} or tool_types == {"Glob"}:
                batch.label = "Searching"
            elif tool_types == {"grep"} or tool_types == {"Grep"}:
                batch.label = "Searching"
            elif tool_types == {"edit_file"} or tool_types == {"Edit"}:
                batch.label = "Editing"
            else:
                batch.label = "Running"

        # Create tree view
        completed = batch.completed_count()
        total = batch.total_count()

        if batch.is_complete():
            status_icon = f"[{SUCCESS}]✓[/{SUCCESS}]"
        else:
            status_icon = f"[{WARNING}]⟳[/{WARNING}]"

        header = Text()
        header.append(status_icon)
        header.append(f" {batch.label} ", style=MUTED)
        header.append(f"{completed}/{total}", style="dim")

        tree = Tree(header)

        for i, tool in enumerate(batch.tools):
            # Get target from args
            target = self._get_tool_target(tool)

            # Status indicator
            if tool.status == ToolStatus.COMPLETED:
                icon = f"[{SUCCESS}]✓[/{SUCCESS}]"
            elif tool.status == ToolStatus.FAILED:
                icon = f"[{ERROR}]✗[/{ERROR}]"
            elif tool.status == ToolStatus.RUNNING:
                icon = f"[{WARNING}]⟳[/{WARNING}]"
            else:
                icon = "[dim]○[/dim]"

            # Connector
            connector = "└─" if i == len(batch.tools) - 1 else "├─"

            tree.add(f"[dim]{connector}[/dim] {icon} [dim]{target}[/dim]")

        return tree

    def _get_tool_target(self, tool: ToolExecution) -> str:
        """Extract target string from tool args."""
        args = tool.args

        if tool.tool_name in ("read_file", "Read", "edit_file", "Edit", "write_file", "Write"):
            path = args.get("file_path", "")
            # Show just filename
            if "/" in path:
                return path.rsplit("/", 1)[-1]
            return path

        if tool.tool_name in ("glob", "Glob"):
            return args.get("pattern", "")

        if tool.tool_name in ("grep", "Grep"):
            pattern = args.get("pattern", "")
            return f'"{pattern[:30]}..."' if len(pattern) > 30 else f'"{pattern}"'

        if tool.tool_name in ("bash", "Bash"):
            cmd = args.get("command", "")
            return cmd[:40] + "..." if len(cmd) > 40 else cmd

        return tool.tool_name


class StatusLine:
    """Persistent status line at bottom of screen.

    Shows: session ID, model tier, plan mode status, token usage
    """

    def __init__(self, console: Console):
        """Initialize status line.

        Args:
            console: Rich console
        """
        self.console = console
        self._session_id: Optional[str] = None
        self._model_tier: str = "sonnet"
        self._plan_mode: bool = False
        self._tokens_used: int = 0
        self._tokens_budget: int = 100000
        self._state: str = "idle"

    def update(
        self,
        session_id: Optional[str] = None,
        model_tier: Optional[str] = None,
        plan_mode: Optional[bool] = None,
        tokens_used: Optional[int] = None,
        tokens_budget: Optional[int] = None,
        state: Optional[str] = None,
    ) -> None:
        """Update status line values.

        Args:
            session_id: Session ID
            model_tier: Model tier
            plan_mode: Plan mode active
            tokens_used: Tokens used
            tokens_budget: Token budget
            state: Current state
        """
        if session_id is not None:
            self._session_id = session_id
        if model_tier is not None:
            self._model_tier = model_tier
        if plan_mode is not None:
            self._plan_mode = plan_mode
        if tokens_used is not None:
            self._tokens_used = tokens_used
        if tokens_budget is not None:
            self._tokens_budget = tokens_budget
        if state is not None:
            self._state = state

    def render(self) -> Text:
        """Render the status line.

        Returns:
            Rich Text object
        """
        parts = []

        # Session ID
        if self._session_id:
            parts.append(f"[dim]session:[/dim] {self._session_id[:8]}")

        # Model tier
        model_style = {
            "haiku": "green",
            "sonnet": "blue",
            "opus": "magenta",
        }.get(self._model_tier.lower(), "dim")
        parts.append(f"[dim]model:[/dim] [{model_style}]{self._model_tier}[/{model_style}]")

        # Plan mode
        if self._plan_mode:
            parts.append(f"[{WARNING}]PLAN MODE[/{WARNING}]")

        # Token usage with color coding
        if self._tokens_budget > 0:
            ratio = self._tokens_used / self._tokens_budget
            if ratio < 0.5:
                token_style = SUCCESS
            elif ratio < 0.8:
                token_style = WARNING
            else:
                token_style = ERROR

            parts.append(
                f"[dim]tokens:[/dim] [{token_style}]{self._tokens_used:,}[/{token_style}]"
                f"[dim]/{self._tokens_budget:,}[/dim]"
            )
        else:
            parts.append(f"[dim]tokens:[/dim] {self._tokens_used:,}")

        # State indicator
        state_icons = {
            "idle": "[dim]●[/dim]",
            "running": f"[{WARNING}]●[/{WARNING}]",
            "waiting": f"[{INFO}]●[/{INFO}]",
            "error": f"[{ERROR}]●[/{ERROR}]",
        }
        state_icon = state_icons.get(self._state, "[dim]●[/dim]")
        parts.append(state_icon)

        return Text.from_markup(" │ ".join(parts))


class FileTreeView:
    """File tree view for multi-file operations.

    Modified 4 files:
      ├─ src/
      │   ├─ auth.py (M) +15/-3
      │   └─ utils.py (M) +5/-1
      └─ tests/
          ├─ test_auth.py (A) +45
          └─ test_utils.py (M) +10/-2
    """

    def __init__(self, console: Console):
        """Initialize file tree view.

        Args:
            console: Rich console
        """
        self.console = console

    def render(
        self,
        files: list[dict[str, Any]],
        title: str = "Modified files",
    ) -> RenderableType:
        """Render file tree.

        Args:
            files: List of file dicts with path, action, additions, deletions
            title: Tree title

        Returns:
            Rich renderable
        """
        if not files:
            return Text()

        # Build directory structure
        dirs: dict[str, list[dict]] = {}
        for f in files:
            path = f.get("path", "")
            if "/" in path:
                dir_path = path.rsplit("/", 1)[0]
                filename = path.rsplit("/", 1)[1]
            else:
                dir_path = ""
                filename = path

            if dir_path not in dirs:
                dirs[dir_path] = []
            dirs[dir_path].append({
                "name": filename,
                **f,
            })

        # Create tree
        tree = Tree(f"[bold]{title}[/bold] ({len(files)} files)")

        # Sort directories
        for dir_path in sorted(dirs.keys()):
            dir_files = dirs[dir_path]

            if dir_path:
                # Add directory branch
                dir_branch = tree.add(f"[{INFO}]{dir_path}/[/{INFO}]")
                parent = dir_branch
            else:
                parent = tree

            # Add files
            for i, f in enumerate(dir_files):
                action = f.get("action", "M")
                action_style = {
                    "A": SUCCESS,
                    "M": WARNING,
                    "D": ERROR,
                }.get(action, MUTED)

                additions = f.get("additions", 0)
                deletions = f.get("deletions", 0)

                stats = ""
                if additions or deletions:
                    stats = f" [{SUCCESS}]+{additions}[/{SUCCESS}]" if additions else ""
                    stats += f" [{ERROR}]-{deletions}[/{ERROR}]" if deletions else ""

                parent.add(
                    f"{f['name']} [{action_style}]({action})[/{action_style}]{stats}"
                )

        return tree


class CollapsibleOutput:
    """Collapsible tool output with expand/collapse.

    Shows first 3 lines + "... N more lines"
    """

    def __init__(
        self,
        content: str,
        title: str = "",
        collapsed: bool = True,
        preview_lines: int = 3,
    ):
        """Initialize collapsible output.

        Args:
            content: Full content
            title: Output title
            collapsed: Whether initially collapsed
            preview_lines: Lines to show when collapsed
        """
        self.content = content
        self.title = title
        self.collapsed = collapsed
        self.preview_lines = preview_lines

    def render(self) -> RenderableType:
        """Render the output.

        Returns:
            Rich renderable
        """
        lines = self.content.split("\n")
        total_lines = len(lines)

        if self.collapsed and total_lines > self.preview_lines:
            preview = "\n".join(lines[:self.preview_lines])
            hidden = total_lines - self.preview_lines

            text = Text()
            text.append(preview + "\n", style="dim")
            text.append(f"... {hidden} more lines", style=MUTED)

            if self.title:
                return Panel(text, title=self.title, border_style="dim")
            return text
        else:
            if self.title:
                return Panel(Text(self.content, style="dim"), title=self.title, border_style="dim")
            return Text(self.content, style="dim")


class ProgressTracker:
    """Progress tracking for long operations.

    Shows progress bar for operations with known total,
    spinner for unknown-length operations.
    """

    def __init__(self, console: Console):
        """Initialize progress tracker.

        Args:
            console: Rich console
        """
        self.console = console
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console,
        )
        self._tasks: dict[str, TaskID] = {}

    def start(self) -> None:
        """Start progress display."""
        self._progress.start()

    def stop(self) -> None:
        """Stop progress display."""
        self._progress.stop()

    def add_task(
        self,
        task_id: str,
        description: str,
        total: Optional[int] = None,
    ) -> None:
        """Add a progress task.

        Args:
            task_id: Unique task ID
            description: Task description
            total: Total steps (None for indeterminate)
        """
        self._tasks[task_id] = self._progress.add_task(
            description,
            total=total or 100,
        )

    def update_task(
        self,
        task_id: str,
        advance: int = 1,
        description: Optional[str] = None,
    ) -> None:
        """Update task progress.

        Args:
            task_id: Task ID
            advance: Steps to advance
            description: New description
        """
        if task_id in self._tasks:
            self._progress.update(
                self._tasks[task_id],
                advance=advance,
                description=description,
            )

    def complete_task(self, task_id: str) -> None:
        """Mark task as complete.

        Args:
            task_id: Task ID
        """
        if task_id in self._tasks:
            self._progress.update(
                self._tasks[task_id],
                completed=100,
            )


class ExploreAgentPanel:
    """Panel for explore agent visualization.

    ┌─ Exploring: authentication patterns ─────────────┐
    │ ⟳ Agent 1: Searching auth implementations       │
    │   Found: 3 files (src/auth.py, src/login.py...) │
    │ ✓ Agent 2: Checking test coverage               │
    │   Found: 2 test files                           │
    │ ⟳ Agent 3: Looking for security patterns        │
    │   Scanning...                                    │
    └──────────────────────────────────────────────────┘
    """

    def __init__(self, console: Console):
        """Initialize explore panel.

        Args:
            console: Rich console
        """
        self.console = console
        self._agents: list[dict[str, Any]] = []

    def add_agent(
        self,
        agent_id: str,
        description: str,
        focus: str = "",
    ) -> None:
        """Add an explore agent.

        Args:
            agent_id: Agent ID
            description: Agent description
            focus: Focus area
        """
        self._agents.append({
            "id": agent_id,
            "description": description,
            "focus": focus,
            "status": "running",
            "results": [],
        })

    def update_agent(
        self,
        agent_id: str,
        status: Optional[str] = None,
        results: Optional[list] = None,
    ) -> None:
        """Update agent status.

        Args:
            agent_id: Agent ID
            status: New status
            results: Results found
        """
        for agent in self._agents:
            if agent["id"] == agent_id:
                if status:
                    agent["status"] = status
                if results:
                    agent["results"] = results
                break

    def render(self, title: str = "Exploring") -> RenderableType:
        """Render the explore panel.

        Args:
            title: Panel title

        Returns:
            Rich renderable
        """
        if not self._agents:
            return Text()

        lines = []
        for agent in self._agents:
            # Status icon
            if agent["status"] == "completed":
                icon = f"[{SUCCESS}]✓[/{SUCCESS}]"
            elif agent["status"] == "failed":
                icon = f"[{ERROR}]✗[/{ERROR}]"
            else:
                icon = f"[{WARNING}]⟳[/{WARNING}]"

            lines.append(f"{icon} [bold]Agent {agent['id']}:[/bold] {agent['description']}")

            # Results
            if agent["results"]:
                result_count = len(agent["results"])
                preview = ", ".join(str(r)[:20] for r in agent["results"][:3])
                if result_count > 3:
                    preview += "..."
                lines.append(f"  [dim]Found: {result_count} items ({preview})[/dim]")
            elif agent["status"] == "running":
                lines.append(f"  [dim]Scanning...[/dim]")

        content = "\n".join(lines)
        return Panel(
            content,
            title=f"[{ACCENT}]{title}[/{ACCENT}]",
            border_style=ACCENT_DIM,
        )

    def clear(self) -> None:
        """Clear all agents."""
        self._agents = []
