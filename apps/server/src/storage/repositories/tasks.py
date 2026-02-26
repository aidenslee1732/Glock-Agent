"""Task repository for database access."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Any, Dict
from ..postgres import PostgresClient


@dataclass
class Task:
    """Task model."""
    id: str
    session_id: str
    user_id: str
    org_id: Optional[str]
    status: str
    task_type: Optional[str]
    complexity: Optional[str]
    risk_level: Optional[str]
    risk_flags: List[str]
    user_prompt: str
    compiled_plan_id: Optional[str]
    retry_count: int
    max_retries: int
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime
    failure_reason: Optional[str]
    summary: Optional[str]


class TaskRepository:
    """Repository for task operations."""

    def __init__(self, db: PostgresClient):
        self.db = db

    async def create(
        self,
        task_id: str,
        session_id: str,
        user_id: str,
        prompt: str,
        org_id: Optional[str] = None,
        task_type: Optional[str] = None,
        complexity: Optional[str] = None,
        risk_level: Optional[str] = None,
        risk_flags: Optional[List[str]] = None,
        status: str = "queued",
    ) -> Task:
        """Create a new task."""
        data = await self.db.create_task(
            task_id=task_id,
            session_id=session_id,
            user_id=user_id,
            user_prompt=prompt,
            org_id=org_id,
            task_type=task_type,
            complexity=complexity,
            risk_level=risk_level,
            risk_flags=risk_flags,
        )
        return self._to_task(data)

    async def get(self, task_id: str) -> Optional[Task]:
        """Get task by ID."""
        data = await self.db.get_task(task_id)
        return self._to_task(data) if data else None

    async def update(
        self,
        task_id: str,
        status: Optional[str] = None,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        failure_reason: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> None:
        """Update task."""
        if status is not None:
            await self.db.update_task_status(
                task_id,
                status,
                started_at=started_at,
                completed_at=completed_at,
                failure_reason=failure_reason,
                summary=summary,
            )

    async def increment_retry(self, task_id: str) -> int:
        """Increment retry count."""
        return await self.db.increment_task_retry(task_id)

    async def set_plan(self, task_id: str, plan_id: str) -> None:
        """Set compiled plan ID."""
        await self.db.set_task_plan(task_id, plan_id)

    async def create_attempt(
        self,
        task_id: str,
        attempt_no: int,
        plan_id: Optional[str] = None,
        trigger: str = "initial",
    ) -> Dict[str, Any]:
        """Create task attempt."""
        return await self.db.create_task_attempt(
            task_id, attempt_no, plan_id, trigger
        )

    async def complete_attempt(
        self,
        task_id: str,
        attempt_no: int,
        status: str,
        failure_class: Optional[str] = None,
    ) -> None:
        """Complete task attempt."""
        await self.db.complete_task_attempt(
            task_id, attempt_no, status, failure_class
        )

    def _to_task(self, data: dict) -> Task:
        """Convert dict to Task model."""
        import json
        risk_flags = data.get('risk_flags', [])
        if isinstance(risk_flags, str):
            risk_flags = json.loads(risk_flags)

        return Task(
            id=str(data['id']),
            session_id=str(data['session_id']),
            user_id=str(data['user_id']),
            org_id=str(data['org_id']) if data.get('org_id') else None,
            status=data.get('status', 'queued'),
            task_type=data.get('task_type'),
            complexity=data.get('complexity'),
            risk_level=data.get('risk_level'),
            risk_flags=risk_flags,
            user_prompt=data.get('user_prompt', ''),
            compiled_plan_id=str(data['compiled_plan_id']) if data.get('compiled_plan_id') else None,
            retry_count=data.get('retry_count', 0),
            max_retries=data.get('max_retries', 2),
            started_at=data.get('started_at'),
            completed_at=data.get('completed_at'),
            created_at=data.get('created_at', datetime.utcnow()),
            failure_reason=data.get('failure_reason'),
            summary=data.get('summary'),
        )
