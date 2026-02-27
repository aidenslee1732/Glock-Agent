"""Glock TUI - Terminal User Interface.

Modern, minimal interface. """

from __future__ import annotations

import asyncio
import sys
from typing import Optional, List

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.spinner import Spinner
from rich.style import Style
from rich.text import Text
from rich.table import Table
from rich.syntax import Syntax
from rich.rule import Rule

from apps.cli.src.session.host import SessionHost, SessionState
from apps.cli.src.tools.user_tools.handlers import (
    set_question_callback,
    Question,
    QuestionResult,
)


# Modern color palette
ACCENT = "#10b981"  # Emerald green
ACCENT_DIM = "#065f46"
MUTED = "#6b7280"
WARNING = "#f59e0b"
ERROR = "#ef4444"
SUCCESS = "#10b981"


class GlockTUI:
    """Modern terminal interface for Glock.

    Clean, minimal design with:
    - Inline streaming output (no boxes for text)
    - Spinner indicators for tool execution
    - Collapsible tool output
    - Markdown rendering
    """

    def __init__(self, session: SessionHost, console: Optional[Console] = None):
        self.session = session
        self.console = console or Console()

        self._running = False
        self._current_content = ""
        self._tool_depth = 0  # Track nested tool calls

        # Register the question callback for AskUserQuestion tool
        set_question_callback(self._handle_questions)

    async def run(self) -> None:
        """Run the TUI main loop."""
        self._running = True

        # Show minimal banner
        self._show_banner()

        # Main input loop
        while self._running:
            try:
                user_input = await self._get_input()

                if not user_input:
                    continue

                if user_input.startswith("/"):
                    await self._handle_command(user_input)
                    continue

                await self._submit_task(user_input)

            except KeyboardInterrupt:
                if self.session.state == SessionState.TASK_RUNNING:
                    self.console.print("\n[dim]Cancelling...[/dim]")
                    await self.session.cancel_task()
                else:
                    self._running = False

            except EOFError:
                self._running = False

        self.console.print("\n[dim]Goodbye![/dim]")

    def _show_banner(self) -> None:
        """Show minimal banner."""
        self.console.print()
        self.console.print(f"[bold {ACCENT}]Glock[/bold {ACCENT}] [dim]• session {self.session.session_id or 'connecting...'}[/dim]")
        self.console.print(f"[dim]Type your request. Use /help for commands.[/dim]")
        self.console.print()

    async def _get_input(self) -> str:
        """Get user input with styled prompt."""
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None,
                lambda: Prompt.ask(f"[bold {ACCENT}]>[/bold {ACCENT}]"),
            )
        except EOFError:
            return ""

    async def _submit_task(self, prompt: str) -> None:
        """Submit a task with modern streaming output."""
        self._current_content = ""
        self._tool_depth = 0

        # Run user-prompt-submit hooks
        if self.session.hook_manager:
            allowed, block_message = await self.session.hook_manager.on_user_prompt(prompt)
            if not allowed:
                self.console.print(f"\n[red]Blocked by hook:[/red] {block_message}")
                return

        self.console.print()

        # Track state for clean output
        last_event_type = None
        tool_results: list[tuple[str, str, dict]] = []  # (tool_name, status, result)
        accumulated_text = ""
        files_modified: set[str] = set()
        total_tokens = 0

        try:
            async for event in self.session.run_task(prompt):
                event_type = event.type.value if hasattr(event.type, 'value') else str(event.type)

                # Handle thinking indicator
                if event_type == "thinking":
                    if last_event_type != "thinking":
                        self.console.print(f"[dim]Thinking...[/dim]", end="")
                    last_event_type = event_type
                    continue

                # Clear thinking indicator
                if last_event_type == "thinking" and event_type != "thinking":
                    self.console.print("\r" + " " * 20 + "\r", end="")

                # Handle text streaming
                if event_type == "text_delta":
                    content = event.content or event.data.get("content", "")
                    if content:
                        accumulated_text += content
                        # Print text inline (streaming feel)
                        self.console.print(content, end="")
                    last_event_type = event_type
                    continue

                # Handle tool execution
                if event_type == "tool_start":
                    tool_name = event.tool_name or event.data.get("tool_name", "")
                    args = event.args or event.data.get("args", {})

                    # End any accumulated text
                    if accumulated_text:
                        self.console.print()
                        accumulated_text = ""

                    # Show tool with spinner
                    self._show_tool_start(tool_name, args)
                    self._tool_depth += 1
                    last_event_type = event_type
                    continue

                if event_type == "tool_end":
                    tool_name = event.tool_name or event.data.get("tool_name", "")
                    result = event.result or event.data.get("result", {})

                    self._tool_depth = max(0, self._tool_depth - 1)

                    # Track file modifications
                    if tool_name in ("edit_file", "write_file"):
                        file_path = event.data.get("args", {}).get("file_path", "")
                        if file_path:
                            files_modified.add(file_path)

                    # Show result inline
                    self._show_tool_end(tool_name, result)
                    last_event_type = event_type
                    continue

                # Handle edit proposals
                if event_type == "edit_proposal":
                    file_path = event.file_path or event.data.get("file_path", "")
                    diff = event.diff or event.data.get("diff", "")
                    new_content = event.new_content or event.data.get("new_content", "")

                    if accumulated_text:
                        self.console.print()
                        accumulated_text = ""

                    self._show_edit_proposal(file_path, diff, new_content)
                    last_event_type = event_type
                    continue

                # Handle errors
                if event_type == "error":
                    message = event.message or event.data.get("message", "Unknown error")

                    if accumulated_text:
                        self.console.print()
                        accumulated_text = ""

                    self.console.print(f"\n[{ERROR}]✗ {message}[/{ERROR}]")
                    last_event_type = event_type
                    continue

                # Handle checkpoints (silent)
                if event_type == "checkpoint_saved":
                    last_event_type = event_type
                    continue

                # Handle completion
                if event_type == "task_complete":
                    summary = event.summary or event.data.get("summary", "")

                    # Ensure we're on a new line
                    if accumulated_text:
                        self.console.print()
                        accumulated_text = ""

                    # Get final stats
                    if self.session._orchestrator:
                        files_modified = self.session._orchestrator._files_modified
                        total_tokens = self.session._orchestrator._total_tokens

                    self._show_completion(summary, files_modified, total_tokens)
                    break

                # Handle approval needed
                if self.session.state == SessionState.WAITING_APPROVAL:
                    await self._handle_approval()

                last_event_type = event_type

        except Exception as e:
            self.console.print(f"\n[{ERROR}]✗ Error: {e}[/{ERROR}]")

        self.console.print()

    def _show_tool_start(self, tool_name: str, args: dict) -> None:
        """Show tool execution start with modern styling."""
        # Human-friendly tool names
        tool_labels = {
            "read_file": "Reading",
            "edit_file": "Editing",
            "write_file": "Writing",
            "list_directory": "Listing",
            "glob": "Searching",
            "grep": "Searching",
            "bash": "Running",
        }

        label = tool_labels.get(tool_name, tool_name.replace("_", " ").title())

        # Get the key argument
        if tool_name in ("read_file", "edit_file", "write_file"):
            target = args.get("file_path", "")
        elif tool_name == "list_directory":
            target = args.get("path", ".")
        elif tool_name == "glob":
            target = args.get("pattern", "")
        elif tool_name == "grep":
            target = args.get("pattern", "")
        elif tool_name == "bash":
            cmd = args.get("command", "")
            target = cmd[:50] + "..." if len(cmd) > 50 else cmd
        else:
            target = ""

        # Print inline with spinner character
        indent = "  " * self._tool_depth
        self.console.print(f"\n{indent}[{MUTED}]⟳ {label}[/{MUTED}] [dim]{target}[/dim]", end="")

    def _show_tool_end(self, tool_name: str, result: dict) -> None:
        """Show tool completion inline."""
        status = result.get("status", "success") if isinstance(result, dict) else "success"

        if status == "success" or status is None:
            # Just show checkmark on same line
            self.console.print(f" [{SUCCESS}]✓[/{SUCCESS}]")
        else:
            error = result.get("error", "Failed") if isinstance(result, dict) else "Failed"
            self.console.print(f" [{ERROR}]✗ {error[:50]}[/{ERROR}]")

    def _show_edit_proposal(self, file_path: str, diff: str, new_content: str) -> None:
        """Show edit proposal with syntax highlighting."""
        self.console.print()
        self.console.print(f"[{WARNING}]┌─ Edit: {file_path}[/{WARNING}]")

        if diff:
            # Show diff with colors
            for line in diff.split("\n")[:10]:  # Limit to 10 lines
                if line.startswith("+"):
                    self.console.print(f"[green]│ {line}[/green]")
                elif line.startswith("-"):
                    self.console.print(f"[red]│ {line}[/red]")
                else:
                    self.console.print(f"[dim]│ {line}[/dim]")
        elif new_content:
            # Show preview of new content
            preview = new_content[:200]
            if len(new_content) > 200:
                preview += "..."
            for line in preview.split("\n")[:5]:
                self.console.print(f"[dim]│ {line}[/dim]")

        self.console.print(f"[{WARNING}]└─[/{WARNING}]")

    def _show_completion(self, summary: str, files_modified: set, total_tokens: int) -> None:
        """Show task completion summary."""
        self.console.print()

        # Success indicator
        self.console.print(f"[{SUCCESS}]✓ Done[/{SUCCESS}]", end="")

        # Files modified
        if files_modified:
            file_list = ", ".join(sorted(files_modified)[:3])
            if len(files_modified) > 3:
                file_list += f" +{len(files_modified) - 3} more"
            self.console.print(f" [dim]• {file_list}[/dim]", end="")

        # Tokens
        if total_tokens:
            self.console.print(f" [dim]• {total_tokens:,} tokens[/dim]")
        else:
            self.console.print()

    async def _handle_approval(self) -> None:
        """Handle tool approval request."""
        self.console.print()
        response = Prompt.ask(
            f"[{WARNING}]Approve?[/{WARNING}]",
            choices=["y", "n"],
            default="y",
        )

        await self.session.approve_tool("pending", response == "y")

    async def _handle_command(self, command: str) -> None:
        """Handle a slash command."""
        parts = command.split()
        cmd = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []

        commands = {
            "/help": self._cmd_help,
            "/clear": self._cmd_clear,
            "/status": self._cmd_status,
            "/exit": self._cmd_exit,
            "/quit": self._cmd_exit,
            "/tasks": self._cmd_tasks,
            "/plan": self._cmd_plan,
            "/commit": self._cmd_commit,
            "/pr": self._cmd_pr,
            "/model": self._cmd_model,
        }

        handler = commands.get(cmd)
        if handler:
            await handler(args)
        else:
            self.console.print(f"[{ERROR}]Unknown command: {cmd}[/{ERROR}]")

    async def _cmd_help(self, args: list[str]) -> None:
        """Show help."""
        self.console.print()
        self.console.print("[bold]Commands[/bold]")
        self.console.print(f"  [dim]/help[/dim]    Show this help")
        self.console.print(f"  [dim]/clear[/dim]   Clear screen")
        self.console.print(f"  [dim]/status[/dim]  Session status")
        self.console.print(f"  [dim]/tasks[/dim]   Show task list")
        self.console.print(f"  [dim]/plan[/dim]    Enter plan mode")
        self.console.print(f"  [dim]/commit[/dim]  Commit changes")
        self.console.print(f"  [dim]/pr[/dim]      Create pull request")
        self.console.print(f"  [dim]/model[/dim]   Change model (haiku/sonnet/opus)")
        self.console.print(f"  [dim]/exit[/dim]    Exit Glock")
        self.console.print()

    async def _cmd_clear(self, args: list[str]) -> None:
        """Clear the screen."""
        self.console.clear()
        self._show_banner()

    async def _cmd_status(self, args: list[str]) -> None:
        """Show session status."""
        self.console.print()
        self.console.print(f"[dim]Session:[/dim]   {self.session.session_id or 'none'}")
        self.console.print(f"[dim]State:[/dim]     {self.session.state.value}")
        self.console.print(f"[dim]Workspace:[/dim] {self.session.config.workspace_dir}")

        if self.session.current_task:
            self.console.print(f"[dim]Tokens:[/dim]    {self.session.current_task.tokens_used:,}")

        self.console.print()

    async def _cmd_exit(self, args: list[str]) -> None:
        """Exit the application."""
        self._running = False

    async def _cmd_tasks(self, args: list[str]) -> None:
        """Show task list."""
        self.console.print()

        task_manager = self.session.task_manager
        tasks = task_manager.list_tasks()

        if not tasks:
            self.console.print(f"[{MUTED}]No tasks.[/{MUTED}]")
            self.console.print()
            return

        # Create a table for tasks
        table = Table(show_header=True, header_style=f"bold {ACCENT}")
        table.add_column("ID", style="dim", width=6)
        table.add_column("Subject", width=40)
        table.add_column("Status", width=12)
        table.add_column("Owner", style="dim", width=15)

        for task in tasks:
            status_style = {
                "pending": MUTED,
                "in_progress": WARNING,
                "completed": SUCCESS,
            }.get(task.status.value, MUTED)

            table.add_row(
                task.id[:6],
                task.subject[:38] + "..." if len(task.subject) > 40 else task.subject,
                f"[{status_style}]{task.status.value}[/{status_style}]",
                task.owner or "-",
            )

        self.console.print(table)
        self.console.print()

    async def _cmd_plan(self, args: list[str]) -> None:
        """Enter plan mode."""
        self.console.print()

        plan_mode = self.session.plan_mode
        if plan_mode.is_active:
            self.console.print(f"[{WARNING}]Already in plan mode.[/{WARNING}]")
            self.console.print(f"[dim]Plan file: {plan_mode.plan_file_path}[/dim]")
        else:
            result = plan_mode.enter()
            if result["status"] == "success":
                self.console.print(f"[{SUCCESS}]Entered plan mode.[/{SUCCESS}]")
                self.console.print(f"[dim]Plan file: {result['plan_file']}[/dim]")
                self.console.print(f"[dim]Use ExitPlanMode when ready for approval.[/dim]")
            else:
                self.console.print(f"[{ERROR}]Failed to enter plan mode: {result.get('error', 'Unknown error')}[/{ERROR}]")

        self.console.print()

    async def _cmd_commit(self, args: list[str]) -> None:
        """Run commit skill."""
        self.console.print()
        self.console.print(f"[{MUTED}]Running commit workflow...[/{MUTED}]")
        self.console.print()

        # Invoke commit skill via task
        message = " ".join(args) if args else None
        prompt = f"Create a git commit"
        if message:
            prompt += f" with message: {message}"

        await self._submit_task(prompt + ". Follow the git commit protocol.")

    async def _cmd_pr(self, args: list[str]) -> None:
        """Run PR creation skill."""
        self.console.print()
        self.console.print(f"[{MUTED}]Creating pull request...[/{MUTED}]")
        self.console.print()

        title = " ".join(args) if args else None
        prompt = "Create a pull request"
        if title:
            prompt += f" with title: {title}"

        await self._submit_task(prompt + ". Follow the PR creation protocol.")

    async def _cmd_model(self, args: list[str]) -> None:
        """Change model tier."""
        self.console.print()

        valid_models = ["haiku", "sonnet", "opus"]
        model_map = {
            "haiku": "fast",
            "sonnet": "standard",
            "opus": "advanced",
        }

        if not args:
            current = self.session.config.model_tier
            self.console.print(f"[dim]Current model tier:[/dim] {current}")
            self.console.print(f"[dim]Available:[/dim] {', '.join(valid_models)}")
            self.console.print(f"[dim]Usage:[/dim] /model <tier>")
        elif args[0].lower() in valid_models:
            new_tier = model_map[args[0].lower()]
            self.session.config.model_tier = new_tier
            self.console.print(f"[{SUCCESS}]Model tier set to {args[0].lower()} ({new_tier})[/{SUCCESS}]")
        else:
            self.console.print(f"[{ERROR}]Invalid model. Choose from: {', '.join(valid_models)}[/{ERROR}]")

        self.console.print()

    async def _handle_questions(self, questions: List[Question]) -> List[QuestionResult]:
        """Handle AskUserQuestion tool - display interactive questions.

        Args:
            questions: List of Question objects to display

        Returns:
            List of QuestionResult objects with user's answers
        """
        results = []

        self.console.print()
        self.console.print(f"[bold {ACCENT}]Questions from assistant:[/bold {ACCENT}]")
        self.console.print()

        for i, question in enumerate(questions, 1):
            # Display the question header and text
            self.console.print(f"[bold]{question.header}[/bold]")
            self.console.print(f"  {question.question}")
            self.console.print()

            # Display options
            options_with_other = list(question.options) + [type('Option', (), {'label': 'Other', 'description': 'Enter custom response'})()]

            for j, opt in enumerate(options_with_other, 1):
                label = opt.label
                desc = opt.description if hasattr(opt, 'description') and opt.description else ""
                if desc:
                    self.console.print(f"  [{ACCENT}]{j}[/{ACCENT}]. {label}")
                    self.console.print(f"     [dim]{desc}[/dim]")
                else:
                    self.console.print(f"  [{ACCENT}]{j}[/{ACCENT}]. {label}")

            self.console.print()

            # Get user selection
            if question.multi_select:
                self.console.print(f"[dim]Select multiple (comma-separated, e.g., 1,2,3):[/dim]")
            else:
                self.console.print(f"[dim]Select an option (1-{len(options_with_other)}):[/dim]")

            loop = asyncio.get_event_loop()
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: Prompt.ask(f"[{ACCENT}]Choice[/{ACCENT}]"),
                )
            except (EOFError, KeyboardInterrupt):
                raise asyncio.CancelledError("User cancelled question")

            # Parse response
            selected = []
            custom_text = None

            if question.multi_select:
                # Parse comma-separated selections
                try:
                    choices = [int(c.strip()) for c in response.split(",")]
                    for choice in choices:
                        if 1 <= choice <= len(question.options):
                            selected.append(question.options[choice - 1].label)
                        elif choice == len(options_with_other):  # "Other" selected
                            custom_text = await self._get_custom_input()
                except ValueError:
                    # Treat as custom text
                    custom_text = response
            else:
                # Single selection
                try:
                    choice = int(response.strip())
                    if 1 <= choice <= len(question.options):
                        selected.append(question.options[choice - 1].label)
                    elif choice == len(options_with_other):  # "Other" selected
                        custom_text = await self._get_custom_input()
                    else:
                        # Invalid choice, treat as custom
                        custom_text = response
                except ValueError:
                    # Treat as custom text
                    custom_text = response

            results.append(QuestionResult(
                question=question.question,
                selected=selected,
                custom_text=custom_text,
            ))

            self.console.print()

        self.console.print(f"[{SUCCESS}]Answers recorded.[/{SUCCESS}]")
        self.console.print()

        return results

    async def _get_custom_input(self) -> str:
        """Get custom text input from user."""
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: Prompt.ask(f"[{ACCENT}]Enter your response[/{ACCENT}]"),
            )
            return response
        except (EOFError, KeyboardInterrupt):
            return ""