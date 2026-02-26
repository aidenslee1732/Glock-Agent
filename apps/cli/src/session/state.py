"""
Local session state persistence for Glock CLI.

Manages session state on the client side, including:
- Session metadata and configuration
- Last known server state for resume
- Local workspace mappings
- Checkpoint data for recovery
"""

import json
import os
import hashlib
import sqlite3
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Dict, List, Any
from contextlib import contextmanager
import threading


@dataclass
class SessionMetadata:
    """Session metadata stored locally."""
    session_id: str
    user_id: str
    workspace_label: str
    repo_fingerprint: str
    branch_name: str
    created_at: str
    last_activity_at: str
    status: str = "idle"
    active_task_id: Optional[str] = None

    # Sequence tracking for resume
    last_server_seq_seen: int = 0
    last_client_seq_sent: int = 0

    # Server connection info
    gateway_url: Optional[str] = None

    # Local paths
    worktree_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionMetadata":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class TaskCheckpoint:
    """Checkpoint data for task recovery."""
    task_id: str
    session_id: str
    checkpoint_type: str  # 'tool_queue', 'plan_progress', 'conversation'
    attempt_no: int
    payload: Dict[str, Any]
    created_at: str


@dataclass
class PendingMessage:
    """Message pending acknowledgment from server."""
    message_id: str
    session_id: str
    seq: int
    message_type: str
    payload: Dict[str, Any]
    sent_at: str
    retries: int = 0


class SessionStateStore:
    """
    SQLite-backed local session state storage.

    Thread-safe storage for session metadata, checkpoints,
    and pending messages that need to survive client restarts.
    """

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or self._default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "sessions.db"
        self._local = threading.local()
        self._init_db()

    def _default_data_dir(self) -> Path:
        """Get default data directory."""
        if os.name == 'nt':
            base = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
        else:
            base = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))
        return base / 'glock'

    @property
    def _conn(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path))
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    @contextmanager
    def _transaction(self):
        """Context manager for database transactions."""
        conn = self._conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_db(self):
        """Initialize database schema."""
        with self._transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    workspace_label TEXT,
                    repo_fingerprint TEXT,
                    branch_name TEXT,
                    created_at TEXT NOT NULL,
                    last_activity_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'idle',
                    active_task_id TEXT,
                    last_server_seq_seen INTEGER DEFAULT 0,
                    last_client_seq_sent INTEGER DEFAULT 0,
                    gateway_url TEXT,
                    worktree_path TEXT,
                    metadata_json TEXT
                );

                CREATE TABLE IF NOT EXISTS task_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    checkpoint_type TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_checkpoints_task
                    ON task_checkpoints(task_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS pending_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    message_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    retries INTEGER DEFAULT 0,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_pending_session
                    ON pending_messages(session_id, seq);

                CREATE TABLE IF NOT EXISTS workspace_mappings (
                    repo_fingerprint TEXT PRIMARY KEY,
                    workspace_path TEXT NOT NULL,
                    last_used_at TEXT NOT NULL
                );
            """)

    # Session operations

    def save_session(self, session: SessionMetadata) -> None:
        """Save or update session metadata."""
        with self._transaction() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO sessions (
                    session_id, user_id, workspace_label, repo_fingerprint,
                    branch_name, created_at, last_activity_at, status,
                    active_task_id, last_server_seq_seen, last_client_seq_sent,
                    gateway_url, worktree_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session.session_id, session.user_id, session.workspace_label,
                session.repo_fingerprint, session.branch_name, session.created_at,
                session.last_activity_at, session.status, session.active_task_id,
                session.last_server_seq_seen, session.last_client_seq_sent,
                session.gateway_url, session.worktree_path
            ))

    def get_session(self, session_id: str) -> Optional[SessionMetadata]:
        """Get session by ID."""
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        if row:
            return SessionMetadata(
                session_id=row['session_id'],
                user_id=row['user_id'],
                workspace_label=row['workspace_label'],
                repo_fingerprint=row['repo_fingerprint'],
                branch_name=row['branch_name'],
                created_at=row['created_at'],
                last_activity_at=row['last_activity_at'],
                status=row['status'],
                active_task_id=row['active_task_id'],
                last_server_seq_seen=row['last_server_seq_seen'],
                last_client_seq_sent=row['last_client_seq_sent'],
                gateway_url=row['gateway_url'],
                worktree_path=row['worktree_path']
            )
        return None

    def list_sessions(
        self,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50
    ) -> List[SessionMetadata]:
        """List sessions with optional filters."""
        query = "SELECT * FROM sessions WHERE 1=1"
        params: List[Any] = []

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY last_activity_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [
            SessionMetadata(
                session_id=row['session_id'],
                user_id=row['user_id'],
                workspace_label=row['workspace_label'],
                repo_fingerprint=row['repo_fingerprint'],
                branch_name=row['branch_name'],
                created_at=row['created_at'],
                last_activity_at=row['last_activity_at'],
                status=row['status'],
                active_task_id=row['active_task_id'],
                last_server_seq_seen=row['last_server_seq_seen'],
                last_client_seq_sent=row['last_client_seq_sent'],
                gateway_url=row['gateway_url'],
                worktree_path=row['worktree_path']
            )
            for row in rows
        ]

    def get_resumable_sessions(self) -> List[SessionMetadata]:
        """Get sessions that can be resumed."""
        return self.list_sessions(status='idle') + self.list_sessions(status='disconnected')

    def update_session_status(
        self,
        session_id: str,
        status: str,
        active_task_id: Optional[str] = None
    ) -> None:
        """Update session status."""
        with self._transaction() as conn:
            conn.execute("""
                UPDATE sessions
                SET status = ?, active_task_id = ?, last_activity_at = ?
                WHERE session_id = ?
            """, (status, active_task_id, datetime.utcnow().isoformat(), session_id))

    def update_session_seq(
        self,
        session_id: str,
        server_seq: Optional[int] = None,
        client_seq: Optional[int] = None
    ) -> None:
        """Update sequence numbers for resume."""
        updates = ["last_activity_at = ?"]
        params: List[Any] = [datetime.utcnow().isoformat()]

        if server_seq is not None:
            updates.append("last_server_seq_seen = ?")
            params.append(server_seq)

        if client_seq is not None:
            updates.append("last_client_seq_sent = ?")
            params.append(client_seq)

        params.append(session_id)

        with self._transaction() as conn:
            conn.execute(
                f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?",
                params
            )

    def delete_session(self, session_id: str) -> None:
        """Delete session and related data."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM pending_messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM task_checkpoints WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    # Checkpoint operations

    def save_checkpoint(self, checkpoint: TaskCheckpoint) -> None:
        """Save task checkpoint."""
        with self._transaction() as conn:
            conn.execute("""
                INSERT INTO task_checkpoints (
                    task_id, session_id, checkpoint_type, attempt_no,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                checkpoint.task_id, checkpoint.session_id, checkpoint.checkpoint_type,
                checkpoint.attempt_no, json.dumps(checkpoint.payload), checkpoint.created_at
            ))

    def get_latest_checkpoint(
        self,
        task_id: str,
        checkpoint_type: Optional[str] = None
    ) -> Optional[TaskCheckpoint]:
        """Get latest checkpoint for task."""
        query = "SELECT * FROM task_checkpoints WHERE task_id = ?"
        params: List[Any] = [task_id]

        if checkpoint_type:
            query += " AND checkpoint_type = ?"
            params.append(checkpoint_type)

        query += " ORDER BY created_at DESC LIMIT 1"

        row = self._conn.execute(query, params).fetchone()
        if row:
            return TaskCheckpoint(
                task_id=row['task_id'],
                session_id=row['session_id'],
                checkpoint_type=row['checkpoint_type'],
                attempt_no=row['attempt_no'],
                payload=json.loads(row['payload_json']),
                created_at=row['created_at']
            )
        return None

    def cleanup_old_checkpoints(self, task_id: str, keep_last: int = 5) -> None:
        """Clean up old checkpoints, keeping only the most recent."""
        with self._transaction() as conn:
            conn.execute("""
                DELETE FROM task_checkpoints
                WHERE task_id = ? AND id NOT IN (
                    SELECT id FROM task_checkpoints
                    WHERE task_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                )
            """, (task_id, task_id, keep_last))

    # Pending message operations

    def save_pending_message(self, message: PendingMessage) -> None:
        """Save message pending acknowledgment."""
        with self._transaction() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO pending_messages (
                    message_id, session_id, seq, message_type,
                    payload_json, sent_at, retries
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                message.message_id, message.session_id, message.seq,
                message.message_type, json.dumps(message.payload),
                message.sent_at, message.retries
            ))

    def get_pending_messages(
        self,
        session_id: str,
        since_seq: int = 0
    ) -> List[PendingMessage]:
        """Get pending messages for session since sequence number."""
        rows = self._conn.execute("""
            SELECT * FROM pending_messages
            WHERE session_id = ? AND seq > ?
            ORDER BY seq
        """, (session_id, since_seq)).fetchall()

        return [
            PendingMessage(
                message_id=row['message_id'],
                session_id=row['session_id'],
                seq=row['seq'],
                message_type=row['message_type'],
                payload=json.loads(row['payload_json']),
                sent_at=row['sent_at'],
                retries=row['retries']
            )
            for row in rows
        ]

    def ack_messages(self, session_id: str, up_to_seq: int) -> None:
        """Acknowledge messages up to sequence number."""
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM pending_messages WHERE session_id = ? AND seq <= ?",
                (session_id, up_to_seq)
            )

    def increment_retry(self, message_id: str) -> None:
        """Increment retry count for message."""
        with self._transaction() as conn:
            conn.execute(
                "UPDATE pending_messages SET retries = retries + 1 WHERE message_id = ?",
                (message_id,)
            )

    # Workspace mapping operations

    def save_workspace_mapping(self, repo_fingerprint: str, workspace_path: str) -> None:
        """Save repo fingerprint to workspace path mapping."""
        with self._transaction() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO workspace_mappings (
                    repo_fingerprint, workspace_path, last_used_at
                ) VALUES (?, ?, ?)
            """, (repo_fingerprint, workspace_path, datetime.utcnow().isoformat()))

    def get_workspace_path(self, repo_fingerprint: str) -> Optional[str]:
        """Get workspace path for repo fingerprint."""
        row = self._conn.execute(
            "SELECT workspace_path FROM workspace_mappings WHERE repo_fingerprint = ?",
            (repo_fingerprint,)
        ).fetchone()
        return row['workspace_path'] if row else None

    # Utility methods

    def compute_repo_fingerprint(self, repo_path: Path) -> str:
        """Compute fingerprint for repository."""
        git_dir = repo_path / '.git'
        if not git_dir.exists():
            # Not a git repo, use path hash
            return f"path:{hashlib.sha256(str(repo_path).encode()).hexdigest()[:16]}"

        # Use git remote origin URL + HEAD commit
        try:
            config_path = git_dir / 'config'
            head_path = git_dir / 'HEAD'

            fingerprint_data = ""
            if config_path.exists():
                fingerprint_data += config_path.read_text()
            if head_path.exists():
                fingerprint_data += head_path.read_text()

            return f"git:{hashlib.sha256(fingerprint_data.encode()).hexdigest()[:24]}"
        except Exception:
            return f"path:{hashlib.sha256(str(repo_path).encode()).hexdigest()[:16]}"

    def cleanup_stale_sessions(self, max_age_days: int = 30) -> int:
        """Clean up sessions older than max_age_days."""
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()

        with self._transaction() as conn:
            # Get sessions to delete
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE last_activity_at < ?",
                (cutoff,)
            ).fetchall()

            session_ids = [row['session_id'] for row in rows]

            for session_id in session_ids:
                conn.execute("DELETE FROM pending_messages WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM task_checkpoints WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

            return len(session_ids)

    def close(self):
        """Close database connection."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# Singleton instance
_store: Optional[SessionStateStore] = None


def get_session_store(data_dir: Optional[Path] = None) -> SessionStateStore:
    """Get or create session state store singleton."""
    global _store
    if _store is None:
        _store = SessionStateStore(data_dir)
    return _store
