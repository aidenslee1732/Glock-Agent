"""Task data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional
import uuid


class TaskStatus(str, Enum):
    """Task status enumeration."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DELETED = "deleted"


@dataclass
class Task:
    """Represents a task in the task management system.

    Attributes:
        id: Unique task identifier
        subject: Brief title for the task (imperative form, e.g., "Run tests")
        description: Detailed description of what needs to be done
        status: Current task status
        owner: Agent ID if assigned, empty if available
        active_form: Present continuous form shown in spinner when in_progress
                    (e.g., "Running tests")
        blocks: Task IDs that cannot start until this one completes
        blocked_by: Task IDs that must complete before this one can start
        metadata: Arbitrary metadata attached to the task
        created_at: When the task was created
        updated_at: When the task was last modified
    """
    id: str
    subject: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    owner: Optional[str] = None
    active_form: Optional[str] = None
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    @classmethod
    def create(
        cls,
        subject: str,
        description: str,
        active_form: Optional[str] = None,
        owner: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Task:
        """Create a new task with generated ID."""
        task_id = str(uuid.uuid4())[:8]
        return cls(
            id=task_id,
            subject=subject,
            description=description,
            active_form=active_form,
            owner=owner,
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "status": self.status.value,
            "owner": self.owner,
            "active_form": self.active_form,
            "blocks": self.blocks,
            "blocked_by": self.blocked_by,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Create from dictionary."""
        return cls(
            id=data["id"],
            subject=data["subject"],
            description=data["description"],
            status=TaskStatus(data["status"]),
            owner=data.get("owner"),
            active_form=data.get("active_form"),
            blocks=data.get("blocks", []),
            blocked_by=data.get("blocked_by", []),
            metadata=data.get("metadata", {}),
            created_at=datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else data.get("created_at", datetime.utcnow()),
            updated_at=datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else data.get("updated_at", datetime.utcnow()),
        )

    def is_blocked(self) -> bool:
        """Check if this task is blocked by other tasks."""
        return len(self.blocked_by) > 0

    def can_start(self) -> bool:
        """Check if this task can be started."""
        return self.status == TaskStatus.PENDING and not self.is_blocked()


@dataclass
class BackgroundTask:
    """Represents a background task execution.

    Attributes:
        id: Unique background task identifier
        task_id: Optional related task ID
        command: The command or prompt being executed
        status: Current execution status
        output_file: Path to the output file
        pid: Process ID if applicable
        started_at: When execution started
        completed_at: When execution completed (if finished)
        exit_code: Exit code if completed
        error: Error message if failed
    """
    id: str
    command: str
    status: str  # "running", "completed", "failed", "stopped"
    output_file: str
    task_id: Optional[str] = None
    pid: Optional[int] = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None

    @classmethod
    def create(
        cls,
        command: str,
        output_file: str,
        task_id: Optional[str] = None,
    ) -> BackgroundTask:
        """Create a new background task."""
        bg_id = f"bg_{uuid.uuid4().hex[:12]}"
        return cls(
            id=bg_id,
            command=command,
            status="running",
            output_file=output_file,
            task_id=task_id,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "command": self.command,
            "status": self.status,
            "output_file": self.output_file,
            "pid": self.pid,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "exit_code": self.exit_code,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackgroundTask:
        """Create from dictionary."""
        return cls(
            id=data["id"],
            task_id=data.get("task_id"),
            command=data["command"],
            status=data["status"],
            output_file=data["output_file"],
            pid=data.get("pid"),
            started_at=datetime.fromisoformat(data["started_at"]) if isinstance(data.get("started_at"), str) else datetime.utcnow(),
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            exit_code=data.get("exit_code"),
            error=data.get("error"),
        )
