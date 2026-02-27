"""Agent session persistence for resume functionality."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


@dataclass
class AgentSession:
    """Persisted agent session state for resume.

    Stores all necessary state to resume an agent's execution.
    """
    agent_id: str
    agent_type: str
    prompt: str
    workspace_dir: str

    # Conversation state
    messages: list[dict] = field(default_factory=list)
    system_prompt: str = ""

    # Progress tracking
    turn_count: int = 0
    max_turns: int = 50
    tools_called: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    tokens_used: int = 0

    # Status
    status: str = "running"  # running, paused, completed, failed
    last_output: str = ""
    error: Optional[str] = None

    # Timestamps
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

    # Context
    session_id: Optional[str] = None
    parent_agent_id: Optional[str] = None
    model_tier: str = "standard"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        data = asdict(self)
        # Convert datetime objects to ISO format
        data["started_at"] = self.started_at.isoformat()
        data["last_activity"] = self.last_activity.isoformat()
        data["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSession":
        """Create from dictionary."""
        # Parse datetime strings
        data["started_at"] = datetime.fromisoformat(data["started_at"])
        data["last_activity"] = datetime.fromisoformat(data["last_activity"])
        if data.get("completed_at"):
            data["completed_at"] = datetime.fromisoformat(data["completed_at"])
        return cls(**data)


class AgentSessionStore:
    """Stores and retrieves agent sessions for resume functionality."""

    def __init__(self, sessions_dir: Optional[Path] = None):
        """Initialize the session store.

        Args:
            sessions_dir: Directory for session files. Defaults to ~/.glock/agent_sessions/
        """
        if sessions_dir is None:
            self.sessions_dir = Path.home() / ".glock" / "agent_sessions"
        else:
            self.sessions_dir = Path(sessions_dir)

        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_file(self, agent_id: str) -> Path:
        """Get the file path for a session."""
        return self.sessions_dir / f"{agent_id}.json"

    def save(self, session: AgentSession) -> None:
        """Save an agent session.

        Args:
            session: AgentSession to save
        """
        session.last_activity = datetime.utcnow()
        session_file = self._session_file(session.agent_id)

        with open(session_file, "w") as f:
            json.dump(session.to_dict(), f, indent=2)

    def load(self, agent_id: str) -> Optional[AgentSession]:
        """Load an agent session.

        Args:
            agent_id: ID of the agent session to load

        Returns:
            AgentSession if found, None otherwise
        """
        session_file = self._session_file(agent_id)

        if not session_file.exists():
            return None

        try:
            with open(session_file, "r") as f:
                data = json.load(f)
            return AgentSession.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def delete(self, agent_id: str) -> bool:
        """Delete an agent session.

        Args:
            agent_id: ID of the agent session to delete

        Returns:
            True if deleted, False if not found
        """
        session_file = self._session_file(agent_id)

        if session_file.exists():
            session_file.unlink()
            return True
        return False

    def list_sessions(
        self,
        status: Optional[str] = None,
        agent_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[AgentSession]:
        """List agent sessions.

        Args:
            status: Filter by status
            agent_type: Filter by agent type
            limit: Maximum number to return

        Returns:
            List of AgentSession objects
        """
        sessions = []

        for session_file in self.sessions_dir.glob("agent_*.json"):
            try:
                with open(session_file, "r") as f:
                    data = json.load(f)
                session = AgentSession.from_dict(data)

                # Apply filters
                if status and session.status != status:
                    continue
                if agent_type and session.agent_type != agent_type:
                    continue

                sessions.append(session)
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

        # Sort by last activity, most recent first
        sessions.sort(key=lambda s: s.last_activity, reverse=True)

        return sessions[:limit]

    def list_resumable(self, limit: int = 20) -> list[AgentSession]:
        """List sessions that can be resumed.

        Returns:
            List of paused or running sessions
        """
        return self.list_sessions(status="paused", limit=limit)

    def cleanup_old(self, max_age_hours: int = 72) -> int:
        """Clean up old completed/failed sessions.

        Args:
            max_age_hours: Delete sessions older than this

        Returns:
            Number of sessions deleted
        """
        cutoff = datetime.utcnow().timestamp() - (max_age_hours * 3600)
        deleted = 0

        for session_file in self.sessions_dir.glob("agent_*.json"):
            try:
                if session_file.stat().st_mtime < cutoff:
                    with open(session_file, "r") as f:
                        data = json.load(f)
                    # Only delete completed or failed sessions
                    if data.get("status") in ("completed", "failed"):
                        session_file.unlink()
                        deleted += 1
            except (json.JSONDecodeError, OSError):
                continue

        return deleted
