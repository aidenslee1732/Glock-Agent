"""Task manager - high-level task operations."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from .models import Task, TaskStatus
from .store import TaskStore

logger = logging.getLogger(__name__)


class TaskManager:
    """High-level task management operations.

    Provides a clean API for creating, updating, and querying tasks
    with automatic dependency resolution and validation.
    """

    def __init__(self, store: Optional[TaskStore] = None, session_id: Optional[str] = None):
        """Initialize the task manager.

        Args:
            store: TaskStore instance. Creates default if not provided.
            session_id: Current session ID for task association.
        """
        self.store = store or TaskStore()
        self.session_id = session_id
        self._task_counter = 0  # For simple numeric IDs

    def create_task(
        self,
        subject: str,
        description: str,
        active_form: Optional[str] = None,
        owner: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Task:
        """Create a new task.

        Args:
            subject: Brief title (imperative form, e.g., "Run tests")
            description: Detailed description
            active_form: Present continuous form for spinner (e.g., "Running tests")
            owner: Optional owner/agent ID
            metadata: Optional metadata dictionary

        Returns:
            Created task
        """
        # Generate simple numeric ID
        self._task_counter += 1
        task = Task(
            id=str(self._task_counter),
            subject=subject,
            description=description,
            status=TaskStatus.PENDING,
            active_form=active_form,
            owner=owner,
            metadata=metadata or {},
        )

        return self.store.create_task(task, self.session_id)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get a task by ID.

        Args:
            task_id: Task ID

        Returns:
            Task if found, None otherwise
        """
        return self.store.get_task(task_id)

    def update_task(
        self,
        task_id: str,
        status: Optional[str] = None,
        subject: Optional[str] = None,
        description: Optional[str] = None,
        active_form: Optional[str] = None,
        owner: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        add_blocks: Optional[list[str]] = None,
        add_blocked_by: Optional[list[str]] = None,
    ) -> Optional[Task]:
        """Update a task.

        Args:
            task_id: Task ID to update
            status: New status ("pending", "in_progress", "completed", "deleted")
            subject: New subject
            description: New description
            active_form: New active form text
            owner: New owner
            metadata: Metadata to merge (set key to None to delete)
            add_blocks: Task IDs to add to blocks list
            add_blocked_by: Task IDs to add to blocked_by list

        Returns:
            Updated task, or None if not found
        """
        task = self.store.get_task(task_id)
        if not task:
            return None

        # Update simple fields
        if subject is not None:
            task.subject = subject
        if description is not None:
            task.description = description
        if active_form is not None:
            task.active_form = active_form
        if owner is not None:
            task.owner = owner

        # Handle status changes
        if status is not None:
            if status == "deleted":
                task.status = TaskStatus.DELETED
            else:
                new_status = TaskStatus(status)

                # Auto-resolve dependencies when task completes
                if new_status == TaskStatus.COMPLETED and task.status != TaskStatus.COMPLETED:
                    self._resolve_completed(task_id)

                task.status = new_status

        # Merge metadata
        if metadata is not None:
            for key, value in metadata.items():
                if value is None:
                    task.metadata.pop(key, None)
                else:
                    task.metadata[key] = value

        # Handle dependency additions
        if add_blocks:
            for blocked_id in add_blocks:
                if blocked_id not in task.blocks:
                    task.blocks.append(blocked_id)
                    # Update the blocked task's blocked_by list
                    blocked_task = self.store.get_task(blocked_id)
                    if blocked_task and task_id not in blocked_task.blocked_by:
                        blocked_task.blocked_by.append(task_id)
                        self.store.update_task(blocked_task)

        if add_blocked_by:
            for blocker_id in add_blocked_by:
                if blocker_id not in task.blocked_by:
                    task.blocked_by.append(blocker_id)
                    # Update the blocker task's blocks list
                    blocker_task = self.store.get_task(blocker_id)
                    if blocker_task and task_id not in blocker_task.blocks:
                        blocker_task.blocks.append(task_id)
                        self.store.update_task(blocker_task)

        task.updated_at = datetime.utcnow()
        return self.store.update_task(task)

    def delete_task(self, task_id: str) -> bool:
        """Delete a task (marks as deleted).

        Args:
            task_id: Task ID to delete

        Returns:
            True if deleted
        """
        return self.store.delete_task(task_id)

    def list_tasks(
        self,
        status: Optional[str] = None,
        owner: Optional[str] = None,
        include_deleted: bool = False,
    ) -> list[Task]:
        """List tasks with optional filters.

        Args:
            status: Filter by status
            owner: Filter by owner
            include_deleted: Include deleted tasks

        Returns:
            List of tasks
        """
        status_enum = TaskStatus(status) if status else None
        return self.store.list_tasks(
            session_id=self.session_id,
            status=status_enum,
            owner=owner,
            include_deleted=include_deleted,
        )

    def get_available_tasks(self, owner: Optional[str] = None) -> list[Task]:
        """Get tasks available to work on.

        Returns tasks that are:
        - Status: pending
        - Not blocked by any incomplete tasks
        - Either unassigned or assigned to specified owner

        Args:
            owner: Optional owner to filter by

        Returns:
            List of available tasks
        """
        tasks = self.list_tasks(status="pending")
        available = []

        for task in tasks:
            # Skip if blocked
            if task.blocked_by:
                # Check if all blockers are completed
                all_blockers_done = True
                for blocker_id in task.blocked_by:
                    blocker = self.store.get_task(blocker_id)
                    if blocker and blocker.status != TaskStatus.COMPLETED:
                        all_blockers_done = False
                        break

                if not all_blockers_done:
                    continue

            # Filter by owner if specified
            if owner and task.owner and task.owner != owner:
                continue

            available.append(task)

        return available

    def get_next_task(self, owner: Optional[str] = None) -> Optional[Task]:
        """Get the next available task (lowest ID first).

        Args:
            owner: Optional owner to filter by

        Returns:
            Next available task, or None if none available
        """
        available = self.get_available_tasks(owner)
        if available:
            # Sort by ID (numeric) and return first
            return sorted(available, key=lambda t: int(t.id) if t.id.isdigit() else float('inf'))[0]
        return None

    def claim_task(self, task_id: str, owner: str) -> Optional[Task]:
        """Claim a task for an owner.

        Args:
            task_id: Task ID to claim
            owner: Owner claiming the task

        Returns:
            Updated task, or None if not found or not available
        """
        task = self.store.get_task(task_id)
        if not task:
            return None

        if task.status != TaskStatus.PENDING:
            logger.warning(f"Cannot claim task {task_id}: status is {task.status}")
            return None

        if task.owner and task.owner != owner:
            logger.warning(f"Cannot claim task {task_id}: already owned by {task.owner}")
            return None

        task.owner = owner
        task.status = TaskStatus.IN_PROGRESS
        task.updated_at = datetime.utcnow()

        return self.store.update_task(task)

    def complete_task(self, task_id: str) -> Optional[Task]:
        """Mark a task as completed and resolve dependencies.

        Args:
            task_id: Task ID to complete

        Returns:
            Updated task, or None if not found
        """
        task = self.store.get_task(task_id)
        if not task:
            return None

        task.status = TaskStatus.COMPLETED
        task.updated_at = datetime.utcnow()
        self.store.update_task(task)

        # Resolve dependencies
        self._resolve_completed(task_id)

        return task

    def _resolve_completed(self, task_id: str) -> list[str]:
        """Resolve dependencies when a task completes.

        Args:
            task_id: Completed task ID

        Returns:
            List of newly unblocked task IDs
        """
        unblocked = self.store.resolve_dependencies(task_id)
        if unblocked:
            logger.info(f"Tasks unblocked by {task_id}: {unblocked}")
        return unblocked

    def get_task_summary(self) -> dict[str, Any]:
        """Get a summary of all tasks.

        Returns:
            Dictionary with task counts by status
        """
        tasks = self.list_tasks(include_deleted=False)

        summary = {
            "total": len(tasks),
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "blocked": 0,
        }

        for task in tasks:
            if task.status == TaskStatus.PENDING:
                if task.blocked_by:
                    summary["blocked"] += 1
                else:
                    summary["pending"] += 1
            elif task.status == TaskStatus.IN_PROGRESS:
                summary["in_progress"] += 1
            elif task.status == TaskStatus.COMPLETED:
                summary["completed"] += 1

        return summary

    def format_task_list(self) -> str:
        """Format tasks as a readable list.

        Returns:
            Formatted task list string
        """
        tasks = self.list_tasks(include_deleted=False)

        if not tasks:
            return "No tasks."

        lines = []
        for task in tasks:
            status_icon = {
                TaskStatus.PENDING: "○",
                TaskStatus.IN_PROGRESS: "◐",
                TaskStatus.COMPLETED: "●",
            }.get(task.status, "?")

            blocked_indicator = " [BLOCKED]" if task.blocked_by else ""
            owner_indicator = f" (@{task.owner})" if task.owner else ""

            lines.append(
                f"#{task.id} {status_icon} {task.subject}{blocked_indicator}{owner_indicator}"
            )

            if task.blocked_by:
                blockers = ", ".join(f"#{b}" for b in task.blocked_by)
                lines.append(f"   └─ Blocked by: {blockers}")

        return "\n".join(lines)
