"""SQLite-based task persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

from .models import Task, TaskStatus, BackgroundTask

logger = logging.getLogger(__name__)


class TaskStore:
    """SQLite-based persistent storage for tasks.

    Stores tasks in ~/.glock/tasks.db by default.
    """

    def __init__(self, db_path: Optional[str] = None):
        """Initialize the task store.

        Args:
            db_path: Path to SQLite database. Defaults to ~/.glock/tasks.db
        """
        if db_path is None:
            glock_dir = Path.home() / ".glock"
            glock_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(glock_dir / "tasks.db")

        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection with context management."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        """Initialize database schema."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Tasks table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    owner TEXT,
                    active_form TEXT,
                    blocks TEXT DEFAULT '[]',
                    blocked_by TEXT DEFAULT '[]',
                    metadata TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # Background tasks table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS background_tasks (
                    id TEXT PRIMARY KEY,
                    task_id TEXT,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    output_file TEXT NOT NULL,
                    pid INTEGER,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    exit_code INTEGER,
                    error TEXT,
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
            """)

            # Session tasks table (tasks per session)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS session_tasks (
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    PRIMARY KEY (session_id, task_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
            """)

            # Create indexes
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_tasks_status
                ON tasks(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_tasks_owner
                ON tasks(owner)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_bg_tasks_status
                ON background_tasks(status)
            """)

    # ========== Task CRUD Operations ==========

    def create_task(self, task: Task, session_id: Optional[str] = None) -> Task:
        """Create a new task.

        Args:
            task: Task to create
            session_id: Optional session ID to associate with

        Returns:
            Created task with ID
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO tasks (
                    id, subject, description, status, owner, active_form,
                    blocks, blocked_by, metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task.id,
                task.subject,
                task.description,
                task.status.value,
                task.owner,
                task.active_form,
                json.dumps(task.blocks),
                json.dumps(task.blocked_by),
                json.dumps(task.metadata),
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
            ))

            # Associate with session if provided
            if session_id:
                cursor.execute("""
                    INSERT INTO session_tasks (session_id, task_id)
                    VALUES (?, ?)
                """, (session_id, task.id))

            return task

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get a task by ID.

        Args:
            task_id: Task ID

        Returns:
            Task if found, None otherwise
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT * FROM tasks WHERE id = ?
            """, (task_id,))

            row = cursor.fetchone()
            if row:
                return self._row_to_task(row)
            return None

    def update_task(self, task: Task) -> Task:
        """Update an existing task.

        Args:
            task: Task with updated values

        Returns:
            Updated task
        """
        task.updated_at = datetime.utcnow()

        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE tasks SET
                    subject = ?,
                    description = ?,
                    status = ?,
                    owner = ?,
                    active_form = ?,
                    blocks = ?,
                    blocked_by = ?,
                    metadata = ?,
                    updated_at = ?
                WHERE id = ?
            """, (
                task.subject,
                task.description,
                task.status.value,
                task.owner,
                task.active_form,
                json.dumps(task.blocks),
                json.dumps(task.blocked_by),
                json.dumps(task.metadata),
                task.updated_at.isoformat(),
                task.id,
            ))

            return task

    def delete_task(self, task_id: str) -> bool:
        """Delete a task (soft delete - marks as deleted).

        Args:
            task_id: Task ID to delete

        Returns:
            True if deleted, False if not found
        """
        task = self.get_task(task_id)
        if task:
            task.status = TaskStatus.DELETED
            self.update_task(task)
            return True
        return False

    def hard_delete_task(self, task_id: str) -> bool:
        """Permanently delete a task.

        Args:
            task_id: Task ID to delete

        Returns:
            True if deleted, False if not found
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Remove from session associations
            cursor.execute("""
                DELETE FROM session_tasks WHERE task_id = ?
            """, (task_id,))

            # Remove task
            cursor.execute("""
                DELETE FROM tasks WHERE id = ?
            """, (task_id,))

            return cursor.rowcount > 0

    def list_tasks(
        self,
        session_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
        owner: Optional[str] = None,
        include_deleted: bool = False,
    ) -> list[Task]:
        """List tasks with optional filters.

        Args:
            session_id: Filter by session
            status: Filter by status
            owner: Filter by owner
            include_deleted: Include deleted tasks

        Returns:
            List of matching tasks
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = "SELECT t.* FROM tasks t"
            conditions = []
            params = []

            if session_id:
                query += " JOIN session_tasks st ON t.id = st.task_id"
                conditions.append("st.session_id = ?")
                params.append(session_id)

            if status:
                conditions.append("t.status = ?")
                params.append(status.value)
            elif not include_deleted:
                conditions.append("t.status != ?")
                params.append(TaskStatus.DELETED.value)

            if owner:
                conditions.append("t.owner = ?")
                params.append(owner)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += " ORDER BY t.created_at ASC"

            cursor.execute(query, params)

            return [self._row_to_task(row) for row in cursor.fetchall()]

    def get_next_task_number(self) -> int:
        """Get the next sequential task number for display.

        Returns:
            Next task number (1-indexed)
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM tasks")
            count = cursor.fetchone()[0]
            return count + 1

    # ========== Background Task Operations ==========

    def create_background_task(self, bg_task: BackgroundTask) -> BackgroundTask:
        """Create a new background task.

        Args:
            bg_task: Background task to create

        Returns:
            Created background task
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO background_tasks (
                    id, task_id, command, status, output_file, pid,
                    started_at, completed_at, exit_code, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                bg_task.id,
                bg_task.task_id,
                bg_task.command,
                bg_task.status,
                bg_task.output_file,
                bg_task.pid,
                bg_task.started_at.isoformat(),
                bg_task.completed_at.isoformat() if bg_task.completed_at else None,
                bg_task.exit_code,
                bg_task.error,
            ))

            return bg_task

    def get_background_task(self, bg_task_id: str) -> Optional[BackgroundTask]:
        """Get a background task by ID.

        Args:
            bg_task_id: Background task ID

        Returns:
            Background task if found, None otherwise
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT * FROM background_tasks WHERE id = ?
            """, (bg_task_id,))

            row = cursor.fetchone()
            if row:
                return self._row_to_background_task(row)
            return None

    def update_background_task(self, bg_task: BackgroundTask) -> BackgroundTask:
        """Update a background task.

        Args:
            bg_task: Background task with updated values

        Returns:
            Updated background task
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE background_tasks SET
                    status = ?,
                    pid = ?,
                    completed_at = ?,
                    exit_code = ?,
                    error = ?
                WHERE id = ?
            """, (
                bg_task.status,
                bg_task.pid,
                bg_task.completed_at.isoformat() if bg_task.completed_at else None,
                bg_task.exit_code,
                bg_task.error,
                bg_task.id,
            ))

            return bg_task

    def list_background_tasks(
        self,
        status: Optional[str] = None,
    ) -> list[BackgroundTask]:
        """List background tasks with optional status filter.

        Args:
            status: Filter by status ("running", "completed", "failed", "stopped")

        Returns:
            List of matching background tasks
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if status:
                cursor.execute("""
                    SELECT * FROM background_tasks
                    WHERE status = ?
                    ORDER BY started_at DESC
                """, (status,))
            else:
                cursor.execute("""
                    SELECT * FROM background_tasks
                    ORDER BY started_at DESC
                """)

            return [self._row_to_background_task(row) for row in cursor.fetchall()]

    # ========== Dependency Operations ==========

    def add_dependency(self, task_id: str, blocked_by_id: str) -> bool:
        """Add a dependency (task_id is blocked by blocked_by_id).

        Args:
            task_id: Task that will be blocked
            blocked_by_id: Task that blocks

        Returns:
            True if dependency added
        """
        task = self.get_task(task_id)
        blocker = self.get_task(blocked_by_id)

        if not task or not blocker:
            return False

        # Add to blocked_by list
        if blocked_by_id not in task.blocked_by:
            task.blocked_by.append(blocked_by_id)
            self.update_task(task)

        # Add to blocks list
        if task_id not in blocker.blocks:
            blocker.blocks.append(task_id)
            self.update_task(blocker)

        return True

    def remove_dependency(self, task_id: str, blocked_by_id: str) -> bool:
        """Remove a dependency.

        Args:
            task_id: Task that was blocked
            blocked_by_id: Task that was blocking

        Returns:
            True if dependency removed
        """
        task = self.get_task(task_id)
        blocker = self.get_task(blocked_by_id)

        if not task or not blocker:
            return False

        # Remove from blocked_by list
        if blocked_by_id in task.blocked_by:
            task.blocked_by.remove(blocked_by_id)
            self.update_task(task)

        # Remove from blocks list
        if task_id in blocker.blocks:
            blocker.blocks.remove(task_id)
            self.update_task(blocker)

        return True

    def resolve_dependencies(self, completed_task_id: str) -> list[str]:
        """Resolve dependencies when a task is completed.

        Args:
            completed_task_id: ID of the completed task

        Returns:
            List of task IDs that are now unblocked
        """
        completed_task = self.get_task(completed_task_id)
        if not completed_task:
            return []

        unblocked = []

        # Find all tasks that were blocked by this one
        for blocked_id in completed_task.blocks:
            blocked_task = self.get_task(blocked_id)
            if blocked_task and completed_task_id in blocked_task.blocked_by:
                blocked_task.blocked_by.remove(completed_task_id)
                self.update_task(blocked_task)

                # Check if task is now fully unblocked
                if not blocked_task.blocked_by:
                    unblocked.append(blocked_id)

        return unblocked

    # ========== Helper Methods ==========

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        """Convert database row to Task object."""
        return Task(
            id=row["id"],
            subject=row["subject"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            owner=row["owner"],
            active_form=row["active_form"],
            blocks=json.loads(row["blocks"]),
            blocked_by=json.loads(row["blocked_by"]),
            metadata=json.loads(row["metadata"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_background_task(self, row: sqlite3.Row) -> BackgroundTask:
        """Convert database row to BackgroundTask object."""
        return BackgroundTask(
            id=row["id"],
            task_id=row["task_id"],
            command=row["command"],
            status=row["status"],
            output_file=row["output_file"],
            pid=row["pid"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            exit_code=row["exit_code"],
            error=row["error"],
        )

    def clear_session_tasks(self, session_id: str) -> int:
        """Clear all task associations for a session.

        Args:
            session_id: Session ID

        Returns:
            Number of associations removed
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM session_tasks WHERE session_id = ?
            """, (session_id,))
            return cursor.rowcount
