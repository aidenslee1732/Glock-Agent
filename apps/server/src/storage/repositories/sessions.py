"""Session repository for database access."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Any
from ..postgres import PostgresClient


@dataclass
class Session:
    """Session model."""
    id: str
    user_id: str
    org_id: Optional[str]
    client_id: str
    status: str
    workspace_label: Optional[str]
    repo_fingerprint: Optional[str]
    branch_name: Optional[str]
    active_task_id: Optional[str]
    last_client_seq_acked: int
    last_server_seq_sent: int
    last_seen_at: Optional[datetime]
    created_at: datetime
    ended_at: Optional[datetime]


class SessionRepository:
    """Repository for session operations."""

    def __init__(self, db: PostgresClient):
        self.db = db

    async def create(
        self,
        session_id: str,
        user_id: str,
        client_id: str,
        workspace_label: Optional[str] = None,
        repo_fingerprint: Optional[str] = None,
        branch_name: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> Session:
        """Create a new session."""
        data = await self.db.create_session(
            session_id=session_id,
            user_id=user_id,
            client_id=client_id,
            workspace_label=workspace_label,
            repo_fingerprint=repo_fingerprint,
            branch_name=branch_name,
            org_id=org_id,
        )
        return self._to_session(data)

    async def get(self, session_id: str) -> Optional[Session]:
        """Get session by ID."""
        data = await self.db.get_session(session_id)
        return self._to_session(data) if data else None

    async def list_by_user(
        self,
        user_id: str,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Session]:
        """List sessions for user."""
        rows = await self.db.get_user_sessions(user_id, status=status, limit=limit)
        return [self._to_session(r) for r in rows]

    async def update(
        self,
        session_id: str,
        status: Optional[str] = None,
        active_task_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Update session."""
        if status is not None:
            await self.db.update_session_status(
                session_id,
                status,
                active_task_id=active_task_id,
            )

    async def update_seqs(
        self,
        session_id: str,
        client_ack: Optional[int] = None,
        server_seq: Optional[int] = None,
    ) -> None:
        """Update sequence numbers."""
        await self.db.update_session_seqs(session_id, client_ack, server_seq)

    async def end(self, session_id: str) -> None:
        """End session."""
        await self.db.end_session(session_id)

    async def touch(self, session_id: str) -> None:
        """Update last seen timestamp."""
        await self.db.touch_session(session_id)

    def _to_session(self, data: dict) -> Session:
        """Convert dict to Session model."""
        return Session(
            id=str(data['id']),
            user_id=str(data['user_id']),
            org_id=str(data['org_id']) if data.get('org_id') else None,
            client_id=data.get('client_id', ''),
            status=data.get('status', 'idle'),
            workspace_label=data.get('workspace_label'),
            repo_fingerprint=data.get('repo_fingerprint'),
            branch_name=data.get('branch_name'),
            active_task_id=str(data['active_task_id']) if data.get('active_task_id') else None,
            last_client_seq_acked=data.get('last_client_seq_acked', 0),
            last_server_seq_sent=data.get('last_server_seq_sent', 0),
            last_seen_at=data.get('last_seen_at'),
            created_at=data.get('created_at', datetime.utcnow()),
            ended_at=data.get('ended_at'),
        )
