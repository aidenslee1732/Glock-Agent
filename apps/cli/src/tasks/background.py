"""Background task execution."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from .models import BackgroundTask
from .store import TaskStore

logger = logging.getLogger(__name__)


class BackgroundTaskRunner:
    """Executes and manages background tasks.

    Background tasks run asynchronously and write their output to files.
    Progress can be monitored via TaskOutput tool.
    """

    def __init__(
        self,
        store: Optional[TaskStore] = None,
        output_dir: Optional[str] = None,
    ):
        """Initialize the background task runner.

        Args:
            store: TaskStore instance for persistence
            output_dir: Directory for output files. Defaults to ~/.glock/output/
        """
        self.store = store or TaskStore()

        if output_dir is None:
            self.output_dir = Path.home() / ".glock" / "output"
        else:
            self.output_dir = Path(output_dir)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Track running tasks
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._task_processes: dict[str, asyncio.subprocess.Process] = {}

    async def spawn_command(
        self,
        command: str,
        task_id: Optional[str] = None,
        timeout: Optional[float] = None,
        cwd: Optional[str] = None,
    ) -> BackgroundTask:
        """Spawn a background shell command.

        Args:
            command: Shell command to execute
            task_id: Optional related task ID
            timeout: Optional timeout in seconds
            cwd: Working directory

        Returns:
            BackgroundTask with output file path
        """
        # Create output file
        output_file = self.output_dir / f"bg_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.urandom(4).hex()}.txt"

        # Create background task record
        bg_task = BackgroundTask.create(
            command=command,
            output_file=str(output_file),
            task_id=task_id,
        )
        self.store.create_background_task(bg_task)

        # Start execution
        async_task = asyncio.create_task(
            self._execute_command(bg_task, command, output_file, timeout, cwd)
        )
        self._running_tasks[bg_task.id] = async_task

        return bg_task

    async def spawn_coroutine(
        self,
        coro: Callable[..., Coroutine[Any, Any, Any]],
        args: tuple = (),
        kwargs: Optional[dict] = None,
        name: str = "background_task",
        task_id: Optional[str] = None,
    ) -> BackgroundTask:
        """Spawn a background coroutine.

        Args:
            coro: Coroutine function to execute
            args: Positional arguments for the coroutine
            kwargs: Keyword arguments for the coroutine
            name: Name for the task
            task_id: Optional related task ID

        Returns:
            BackgroundTask with output file path
        """
        kwargs = kwargs or {}

        # Create output file
        output_file = self.output_dir / f"bg_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.urandom(4).hex()}.txt"

        # Create background task record
        bg_task = BackgroundTask.create(
            command=name,
            output_file=str(output_file),
            task_id=task_id,
        )
        self.store.create_background_task(bg_task)

        # Start execution
        async_task = asyncio.create_task(
            self._execute_coroutine(bg_task, coro, args, kwargs, output_file)
        )
        self._running_tasks[bg_task.id] = async_task

        return bg_task

    async def _execute_command(
        self,
        bg_task: BackgroundTask,
        command: str,
        output_file: Path,
        timeout: Optional[float],
        cwd: Optional[str],
    ) -> None:
        """Execute a shell command in the background."""
        try:
            with open(output_file, "w") as f:
                f.write(f"=== Background Task: {bg_task.id} ===\n")
                f.write(f"Command: {command}\n")
                f.write(f"Started: {datetime.now().isoformat()}\n")
                f.write("=" * 50 + "\n\n")

            # Create subprocess
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env={**os.environ, "NO_COLOR": "1"},
            )

            bg_task.pid = process.pid
            self._task_processes[bg_task.id] = process
            self.store.update_background_task(bg_task)

            # Stream output to file
            with open(output_file, "a") as f:
                try:
                    if timeout:
                        stdout, _ = await asyncio.wait_for(
                            process.communicate(),
                            timeout=timeout,
                        )
                    else:
                        stdout, _ = await process.communicate()

                    if stdout:
                        f.write(stdout.decode(errors="replace"))

                except asyncio.TimeoutError:
                    process.kill()
                    f.write("\n\n=== TIMEOUT ===\n")
                    bg_task.status = "failed"
                    bg_task.error = f"Command timed out after {timeout} seconds"

            # Update completion status
            bg_task.completed_at = datetime.utcnow()

            if bg_task.status != "failed":
                bg_task.exit_code = process.returncode
                bg_task.status = "completed" if process.returncode == 0 else "failed"
                if process.returncode != 0:
                    bg_task.error = f"Command exited with code {process.returncode}"

            with open(output_file, "a") as f:
                f.write(f"\n\n=== Completed: {datetime.now().isoformat()} ===\n")
                f.write(f"Exit code: {bg_task.exit_code}\n")

        except Exception as e:
            logger.exception(f"Background task {bg_task.id} failed")
            bg_task.status = "failed"
            bg_task.error = str(e)
            bg_task.completed_at = datetime.utcnow()

            with open(output_file, "a") as f:
                f.write(f"\n\n=== ERROR ===\n{str(e)}\n")

        finally:
            self.store.update_background_task(bg_task)
            self._running_tasks.pop(bg_task.id, None)
            self._task_processes.pop(bg_task.id, None)

    async def _execute_coroutine(
        self,
        bg_task: BackgroundTask,
        coro: Callable,
        args: tuple,
        kwargs: dict,
        output_file: Path,
    ) -> None:
        """Execute a coroutine in the background."""
        try:
            with open(output_file, "w") as f:
                f.write(f"=== Background Task: {bg_task.id} ===\n")
                f.write(f"Task: {bg_task.command}\n")
                f.write(f"Started: {datetime.now().isoformat()}\n")
                f.write("=" * 50 + "\n\n")

            # Execute the coroutine
            result = await coro(*args, **kwargs)

            # Write result
            with open(output_file, "a") as f:
                f.write(f"Result:\n{result}\n")
                f.write(f"\n\n=== Completed: {datetime.now().isoformat()} ===\n")

            bg_task.status = "completed"
            bg_task.exit_code = 0
            bg_task.completed_at = datetime.utcnow()

        except Exception as e:
            logger.exception(f"Background task {bg_task.id} failed")
            bg_task.status = "failed"
            bg_task.error = str(e)
            bg_task.completed_at = datetime.utcnow()

            with open(output_file, "a") as f:
                f.write(f"\n\n=== ERROR ===\n{str(e)}\n")

        finally:
            self.store.update_background_task(bg_task)
            self._running_tasks.pop(bg_task.id, None)

    async def get_output(
        self,
        task_id: str,
        block: bool = True,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Get output from a background task.

        Args:
            task_id: Background task ID
            block: Wait for completion if still running
            timeout: Max wait time in seconds

        Returns:
            Dictionary with status and output
        """
        bg_task = self.store.get_background_task(task_id)
        if not bg_task:
            return {
                "status": "error",
                "error": f"Background task not found: {task_id}",
            }

        # Wait for completion if requested
        if block and bg_task.status == "running":
            async_task = self._running_tasks.get(task_id)
            if async_task:
                try:
                    await asyncio.wait_for(async_task, timeout=timeout)
                except asyncio.TimeoutError:
                    pass

            # Refresh task status
            bg_task = self.store.get_background_task(task_id)

        # Read output file
        output_file = Path(bg_task.output_file)
        if output_file.exists():
            output = output_file.read_text()
            # Truncate if too long
            if len(output) > 50000:
                output = output[-50000:]
                output = "... (truncated)\n" + output
        else:
            output = ""

        return {
            "status": bg_task.status,
            "task_id": task_id,
            "command": bg_task.command,
            "output": output,
            "output_file": str(output_file),
            "started_at": bg_task.started_at.isoformat(),
            "completed_at": bg_task.completed_at.isoformat() if bg_task.completed_at else None,
            "exit_code": bg_task.exit_code,
            "error": bg_task.error,
        }

    async def stop_task(self, task_id: str) -> dict[str, Any]:
        """Stop a running background task.

        Args:
            task_id: Background task ID to stop

        Returns:
            Dictionary with status
        """
        bg_task = self.store.get_background_task(task_id)
        if not bg_task:
            return {
                "status": "error",
                "error": f"Background task not found: {task_id}",
            }

        if bg_task.status != "running":
            return {
                "status": "error",
                "error": f"Task is not running (status: {bg_task.status})",
            }

        # Try to kill the process
        process = self._task_processes.get(task_id)
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.sleep(0.5)
                if process.returncode is None:
                    process.kill()
            except ProcessLookupError:
                pass

        # Cancel the async task
        async_task = self._running_tasks.get(task_id)
        if async_task and not async_task.done():
            async_task.cancel()
            try:
                await async_task
            except asyncio.CancelledError:
                pass

        # Update status
        bg_task.status = "stopped"
        bg_task.completed_at = datetime.utcnow()
        self.store.update_background_task(bg_task)

        # Write to output file
        output_file = Path(bg_task.output_file)
        if output_file.exists():
            with open(output_file, "a") as f:
                f.write(f"\n\n=== STOPPED by user at {datetime.now().isoformat()} ===\n")

        return {
            "status": "success",
            "message": f"Task {task_id} stopped",
        }

    def list_running(self) -> list[BackgroundTask]:
        """List all running background tasks.

        Returns:
            List of running background tasks
        """
        return self.store.list_background_tasks(status="running")

    def list_all(self, limit: int = 50) -> list[BackgroundTask]:
        """List recent background tasks.

        Args:
            limit: Maximum number of tasks to return

        Returns:
            List of background tasks
        """
        all_tasks = self.store.list_background_tasks()
        return all_tasks[:limit]

    async def cleanup_completed(self, max_age_hours: int = 24) -> int:
        """Clean up old completed task output files.

        Args:
            max_age_hours: Delete output files older than this

        Returns:
            Number of files deleted
        """
        cutoff = datetime.utcnow().timestamp() - (max_age_hours * 3600)
        deleted = 0

        for output_file in self.output_dir.glob("bg_*.txt"):
            if output_file.stat().st_mtime < cutoff:
                output_file.unlink()
                deleted += 1

        return deleted
