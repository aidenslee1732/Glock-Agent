"""Glock TUI - Terminal User Interface."""

from __future__ import annotations

import asyncio
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.spinner import Spinner
from rich.style import Style
from rich.text import Text
from rich.table import Table

from apps.cli.src.session.host import SessionHost, SessionState


# Glock brand colors (orange gradient, white-labeled - no Claude references)
GLOCK_ORANGE = "#f97316"
GLOCK_DARK = "#431407"
GLOCK_TEAL = "#0d9488"


class GlockTUI:
    """Terminal User Interface for Glock.

    Provides an interactive chat interface with:
    - Streaming text output
    - Tool execution display
    - Diff previews
    - Approval prompts
    """

    def __init__(self, session: SessionHost, console: Optional[Console] = None):
        self.session = session
        self.console = console or Console()

        self._running = False
        self._current_content = ""
        self._thinking = False

        # Register session callbacks
        self._setup_callbacks()

    def _setup_callbacks(self) -> None:
        """Set up session event callbacks."""

        def on_delta(delta_type: str, content: str) -> None:
            if delta_type == "thinking":
                self._thinking = True
                self._show_thinking(content)
            else:
                self._thinking = False
                self._append_content(content)

        def on_tool_request(request: dict) -> None:
            self._show_tool_request(request)

        def on_task_complete(result: dict) -> None:
            self._show_completion(result)

        def on_error(message: str) -> None:
            self._show_error(message)

        self.session.on_delta(on_delta)
        self.session.on_tool_request(on_tool_request)
        self.session.on_task_complete(on_task_complete)
        self.session.on_error(on_error)

    async def run(self) -> None:
        """Run the TUI main loop."""
        self._running = True

        # Show banner
        self._show_banner()

        # Main input loop
        while self._running:
            try:
                # Show prompt
                user_input = await self._get_input()

                if not user_input:
                    continue

                # Handle commands
                if user_input.startswith("/"):
                    await self._handle_command(user_input)
                    continue

                # Submit task
                await self._submit_task(user_input)

            except KeyboardInterrupt:
                if self.session.state == SessionState.TASK_RUNNING:
                    self.console.print("\n[yellow]Cancelling task...[/yellow]")
                    await self.session.cancel_task()
                else:
                    self._running = False

            except EOFError:
                self._running = False

        self.console.print("\n[dim]Goodbye![/dim]")

    def _show_banner(self) -> None:
        """Show the Glock banner."""
        banner = Text()
        banner.append("  ____  _            _    \n", style=GLOCK_ORANGE)
        banner.append(" / ___|| | ___   ___| | __\n", style=GLOCK_ORANGE)
        banner.append("| |  _ | |/ _ \\ / __| |/ /\n", style=GLOCK_ORANGE)
        banner.append("| |_| || | (_) | (__|   < \n", style=GLOCK_ORANGE)
        banner.append(" \\____||_|\\___/ \\___|_|\\_\\\n", style=GLOCK_ORANGE)

        panel = Panel(
            banner,
            subtitle=f"[dim]session: {self.session.session_id or 'connecting...'}[/dim]",
            border_style=GLOCK_ORANGE,
        )
        self.console.print(panel)
        self.console.print()

        # Tips
        self.console.print("[dim]Type your request below. Use /help for commands.[/dim]")
        self.console.print()

    async def _get_input(self) -> str:
        """Get user input."""
        # Use asyncio to allow background message processing
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: Prompt.ask("[bold cyan]>[/bold cyan]"),
        )

    async def _submit_task(self, prompt: str) -> None:
        """Submit a task and stream results."""
        self._current_content = ""

        self.console.print()

        try:
            # Run task with orchestration (sends LLM_REQUEST messages)
            async for event in self.session.run_task(prompt):
                # Handle events from orchestration
                if event.type.value == "thinking":
                    self._show_thinking(event.content)
                elif event.type.value == "text_delta":
                    self._append_content(event.content)
                elif event.type.value == "tool_start":
                    self._show_tool_request({
                        "tool_name": event.tool_name,
                        "args": event.args,
                    })
                elif event.type.value == "tool_end":
                    pass  # Tool completed
                elif event.type.value == "edit_proposal":
                    self._show_edit_proposal(event)
                elif event.type.value == "error":
                    self._show_error(event.message)
                elif event.type.value == "task_complete":
                    self._show_completion({
                        "summary": event.summary,
                        "files_modified": list(self.session._orchestrator._files_modified) if self.session._orchestrator else [],
                        "total_tokens": self.session._orchestrator._total_tokens if self.session._orchestrator else 0,
                    })

                # Check for approval needed
                if self.session.state == SessionState.WAITING_APPROVAL:
                    await self._handle_approval()

        except Exception as e:
            self._show_error(str(e))

        self.console.print()

    async def _handle_approval(self) -> None:
        """Handle tool approval request."""
        self.console.print()
        response = Prompt.ask(
            "[yellow]Approve this action?[/yellow]",
            choices=["y", "n", "diff"],
            default="y",
        )

        if response == "y":
            # Get pending approval ID (would be stored from callback)
            # For now, just approve
            await self.session.approve_tool("pending", True)
        elif response == "n":
            await self.session.approve_tool("pending", False)

    async def _handle_command(self, command: str) -> None:
        """Handle a slash command."""
        parts = command.split()
        cmd = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []

        commands = {
            "/help": self._cmd_help,
            "/clear": self._cmd_clear,
            "/status": self._cmd_status,
            "/sessions": self._cmd_sessions,
            "/exit": self._cmd_exit,
            "/quit": self._cmd_exit,
        }

        handler = commands.get(cmd)
        if handler:
            await handler(args)
        else:
            self.console.print(f"[red]Unknown command: {cmd}[/red]")
            self.console.print("[dim]Type /help for available commands[/dim]")

    async def _cmd_help(self, args: list[str]) -> None:
        """Show help."""
        table = Table(title="Commands", show_header=False, box=None)
        table.add_column("Command", style="cyan")
        table.add_column("Description")

        commands = [
            ("/help", "Show this help message"),
            ("/clear", "Clear the screen"),
            ("/status", "Show session status"),
            ("/sessions", "List previous sessions"),
            ("/exit", "Exit Glock"),
        ]

        for cmd, desc in commands:
            table.add_row(cmd, desc)

        self.console.print(table)

    async def _cmd_clear(self, args: list[str]) -> None:
        """Clear the screen."""
        self.console.clear()
        self._show_banner()

    async def _cmd_status(self, args: list[str]) -> None:
        """Show session status."""
        status = Table(show_header=False, box=None)
        status.add_column("Key", style="dim")
        status.add_column("Value")

        status.add_row("Session", self.session.session_id or "none")
        status.add_row("State", self.session.state.value)
        status.add_row("Workspace", self.session.config.workspace_dir)

        if self.session.current_task:
            status.add_row("Task", self.session.current_task.task_id)
            status.add_row("Tokens", str(self.session.current_task.tokens_used))

        self.console.print(Panel(status, title="Status", border_style="dim"))

    async def _cmd_sessions(self, args: list[str]) -> None:
        """List previous sessions."""
        self.console.print("[dim]Session history not implemented yet[/dim]")

    async def _cmd_exit(self, args: list[str]) -> None:
        """Exit the application."""
        self._running = False

    def _append_content(self, content: str) -> None:
        """Append content to the current output."""
        self._current_content += content
        # Stream to console (in real impl would use Live)
        self.console.print(content, end="")

    def _show_thinking(self, content: str) -> None:
        """Show thinking content."""
        self.console.print(f"[dim italic]{content}[/dim italic]", end="")

    def _show_tool_request(self, request: dict) -> None:
        """Show tool request."""
        tool_name = request.get("tool_name", "unknown")
        args = request.get("args", {})

        self.console.print()
        self.console.print(
            Panel(
                f"[bold]{tool_name}[/bold]\n{self._format_args(args)}",
                title="[teal]Tool Request[/teal]",
                border_style=GLOCK_TEAL,
            )
        )

    def _show_edit_proposal(self, event) -> None:
        """Show edit proposal."""
        self.console.print()
        self.console.print(
            Panel(
                f"[bold]{event.file_path}[/bold]\n\n{event.diff or event.new_content[:200]}",
                title="[yellow]Edit Proposal[/yellow]",
                border_style="yellow",
            )
        )

    def _show_completion(self, result: dict) -> None:
        """Show task completion."""
        summary = result.get("summary", "Task completed")
        files = result.get("files_modified", [])
        tokens = result.get("total_tokens", 0)

        self.console.print()
        self.console.print(
            Panel(
                f"{summary}\n\n"
                f"[dim]Files: {', '.join(files) if files else 'none'}[/dim]\n"
                f"[dim]Tokens: {tokens}[/dim]",
                title="[green]Complete[/green]",
                border_style="green",
            )
        )

    def _show_error(self, message: str) -> None:
        """Show error message."""
        self.console.print()
        self.console.print(
            Panel(
                message,
                title="[red]Error[/red]",
                border_style="red",
            )
        )

    def _format_args(self, args: dict) -> str:
        """Format tool arguments for display."""
        lines = []
        for key, value in args.items():
            if isinstance(value, str) and len(value) > 100:
                value = value[:100] + "..."
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)
