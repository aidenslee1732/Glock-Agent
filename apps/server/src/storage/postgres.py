"""PostgreSQL/Supabase client for Glock server."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


@dataclass
class PostgresConfig:
    """PostgreSQL connection configuration."""
    url: str = ""
    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: str = ""
    database: str = "glock"
    min_connections: int = 5
    max_connections: int = 20

    @classmethod
    def from_env(cls) -> PostgresConfig:
        """Create config from environment variables."""
        url = os.environ.get("DATABASE_URL", "")
        if url:
            return cls(url=url)
        return cls(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", "5432")),
            user=os.environ.get("DB_USER", "postgres"),
            password=os.environ.get("DB_PASSWORD", ""),
            database=os.environ.get("DB_NAME", "glock"),
        )


class PostgresClient:
    """Async PostgreSQL client for Glock server."""

    def __init__(self, config: Optional[PostgresConfig] = None):
        self.config = config or PostgresConfig.from_env()
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Initialize connection pool."""
        if self.config.url:
            self._pool = await asyncpg.create_pool(
                self.config.url,
                min_size=self.config.min_connections,
                max_size=self.config.max_connections,
            )
        else:
            self._pool = await asyncpg.create_pool(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                min_size=self.config.min_connections,
                max_size=self.config.max_connections,
            )
        logger.info("PostgreSQL connection pool established")

    async def close(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
        logger.info("PostgreSQL connection pool closed")

    @property
    def pool(self) -> asyncpg.Pool:
        """Get connection pool, ensuring connection."""
        if not self._pool:
            raise RuntimeError("PostgreSQL pool not connected. Call connect() first.")
        return self._pool

    # =========================================================================
    # Users
    # =========================================================================

    async def get_user(self, user_id: str) -> Optional[dict[str, Any]]:
        """Get user by ID."""
        row = await self.pool.fetchrow(
            """
            SELECT id, email, name, status, plan_tier, created_at, updated_at, last_seen_at
            FROM users WHERE id = $1
            """,
            UUID(user_id),
        )
        return dict(row) if row else None

    async def get_user_by_email(self, email: str) -> Optional[dict[str, Any]]:
        """Get user by email."""
        row = await self.pool.fetchrow(
            """
            SELECT id, email, name, status, plan_tier, created_at, updated_at, last_seen_at
            FROM users WHERE email = $1
            """,
            email,
        )
        return dict(row) if row else None

    async def create_user(
        self,
        email: str,
        name: Optional[str] = None,
        plan_tier: str = "free",
    ) -> dict[str, Any]:
        """Create a new user."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO users (email, name, plan_tier)
            VALUES ($1, $2, $3)
            RETURNING id, email, name, status, plan_tier, created_at
            """,
            email,
            name,
            plan_tier,
        )
        return dict(row)

    async def update_user_last_seen(self, user_id: str) -> None:
        """Update user's last seen timestamp."""
        await self.pool.execute(
            "UPDATE users SET last_seen_at = NOW() WHERE id = $1",
            UUID(user_id),
        )

    # =========================================================================
    # Sessions
    # =========================================================================

    async def create_session(
        self,
        session_id: str,
        user_id: str,
        client_id: str,
        workspace_label: Optional[str] = None,
        repo_fingerprint: Optional[str] = None,
        branch_name: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a new session."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO sessions (id, user_id, org_id, client_id, workspace_label,
                                  repo_fingerprint, branch_name, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'idle')
            RETURNING id, user_id, org_id, client_id, status, workspace_label,
                      repo_fingerprint, branch_name, created_at
            """,
            UUID(session_id.replace("sess_", "")),
            UUID(user_id),
            UUID(org_id) if org_id else None,
            client_id,
            workspace_label,
            repo_fingerprint,
            branch_name,
        )
        return dict(row)

    async def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        """Get session by ID."""
        row = await self.pool.fetchrow(
            """
            SELECT id, user_id, org_id, client_id, status, workspace_label,
                   repo_fingerprint, branch_name, active_task_id,
                   last_client_seq_acked, last_server_seq_sent,
                   last_seen_at, created_at, ended_at
            FROM sessions WHERE id = $1
            """,
            UUID(session_id.replace("sess_", "")),
        )
        return dict(row) if row else None

    async def get_user_sessions(
        self,
        user_id: str,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get user's sessions."""
        if status:
            rows = await self.pool.fetch(
                """
                SELECT id, user_id, status, workspace_label, repo_fingerprint,
                       branch_name, active_task_id, last_seen_at, created_at
                FROM sessions
                WHERE user_id = $1 AND status = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                UUID(user_id),
                status,
                limit,
            )
        else:
            rows = await self.pool.fetch(
                """
                SELECT id, user_id, status, workspace_label, repo_fingerprint,
                       branch_name, active_task_id, last_seen_at, created_at
                FROM sessions
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                UUID(user_id),
                limit,
            )
        return [dict(row) for row in rows]

    async def update_session_status(
        self,
        session_id: str,
        status: str,
        active_task_id: Optional[str] = None,
    ) -> None:
        """Update session status."""
        if active_task_id:
            await self.pool.execute(
                """
                UPDATE sessions
                SET status = $2, active_task_id = $3, updated_at = NOW()
                WHERE id = $1
                """,
                UUID(session_id.replace("sess_", "")),
                status,
                UUID(active_task_id.replace("task_", "")),
            )
        else:
            await self.pool.execute(
                """
                UPDATE sessions
                SET status = $2, updated_at = NOW()
                WHERE id = $1
                """,
                UUID(session_id.replace("sess_", "")),
                status,
            )

    async def update_session_seqs(
        self,
        session_id: str,
        client_ack: Optional[int] = None,
        server_seq: Optional[int] = None,
    ) -> None:
        """Update session sequence numbers."""
        updates = []
        params = [UUID(session_id.replace("sess_", ""))]
        param_idx = 2

        if client_ack is not None:
            updates.append(f"last_client_seq_acked = ${param_idx}")
            params.append(client_ack)
            param_idx += 1

        if server_seq is not None:
            updates.append(f"last_server_seq_sent = ${param_idx}")
            params.append(server_seq)
            param_idx += 1

        if updates:
            query = f"UPDATE sessions SET {', '.join(updates)}, updated_at = NOW() WHERE id = $1"
            await self.pool.execute(query, *params)

    async def end_session(self, session_id: str) -> None:
        """End a session."""
        await self.pool.execute(
            """
            UPDATE sessions
            SET status = 'ended', ended_at = NOW(), updated_at = NOW()
            WHERE id = $1
            """,
            UUID(session_id.replace("sess_", "")),
        )

    async def touch_session(self, session_id: str) -> None:
        """Update session last_seen_at."""
        await self.pool.execute(
            "UPDATE sessions SET last_seen_at = NOW() WHERE id = $1",
            UUID(session_id.replace("sess_", "")),
        )

    # =========================================================================
    # Tasks
    # =========================================================================

    async def create_task(
        self,
        task_id: str,
        session_id: str,
        user_id: str,
        user_prompt: str,
        org_id: Optional[str] = None,
        task_type: Optional[str] = None,
        complexity: Optional[str] = None,
        risk_level: Optional[str] = None,
        risk_flags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Create a new task."""
        import json
        row = await self.pool.fetchrow(
            """
            INSERT INTO tasks (id, session_id, user_id, org_id, user_prompt,
                               task_type, complexity, risk_level, risk_flags, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'queued')
            RETURNING id, session_id, user_id, status, user_prompt, task_type,
                      complexity, risk_level, risk_flags, created_at
            """,
            UUID(task_id.replace("task_", "")),
            UUID(session_id.replace("sess_", "")),
            UUID(user_id),
            UUID(org_id) if org_id else None,
            user_prompt,
            task_type,
            complexity,
            risk_level,
            json.dumps(risk_flags or []),
        )
        return dict(row)

    async def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """Get task by ID."""
        row = await self.pool.fetchrow(
            """
            SELECT id, session_id, user_id, org_id, status, task_type, complexity,
                   risk_level, risk_flags, user_prompt, compiled_plan_id,
                   retry_count, max_retries, started_at, completed_at,
                   created_at, failure_reason, summary
            FROM tasks WHERE id = $1
            """,
            UUID(task_id.replace("task_", "")),
        )
        return dict(row) if row else None

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        failure_reason: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> None:
        """Update task status."""
        updates = ["status = $2", "updated_at = NOW()"]
        params: list[Any] = [UUID(task_id.replace("task_", "")), status]
        param_idx = 3

        if started_at:
            updates.append(f"started_at = ${param_idx}")
            params.append(started_at)
            param_idx += 1

        if completed_at:
            updates.append(f"completed_at = ${param_idx}")
            params.append(completed_at)
            param_idx += 1

        if failure_reason:
            updates.append(f"failure_reason = ${param_idx}")
            params.append(failure_reason)
            param_idx += 1

        if summary:
            updates.append(f"summary = ${param_idx}")
            params.append(summary)
            param_idx += 1

        query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = $1"
        await self.pool.execute(query, *params)

    async def increment_task_retry(self, task_id: str) -> int:
        """Increment task retry count. Returns new count."""
        row = await self.pool.fetchrow(
            """
            UPDATE tasks SET retry_count = retry_count + 1, updated_at = NOW()
            WHERE id = $1
            RETURNING retry_count
            """,
            UUID(task_id.replace("task_", "")),
        )
        return row["retry_count"] if row else 0

    async def set_task_plan(self, task_id: str, plan_id: str) -> None:
        """Set task's compiled plan ID."""
        await self.pool.execute(
            """
            UPDATE tasks SET compiled_plan_id = $2, updated_at = NOW()
            WHERE id = $1
            """,
            UUID(task_id.replace("task_", "")),
            UUID(plan_id.replace("plan_", "")),
        )

    # =========================================================================
    # Task Attempts
    # =========================================================================

    async def create_task_attempt(
        self,
        task_id: str,
        attempt_no: int,
        plan_id: Optional[str] = None,
        trigger: str = "initial",
    ) -> dict[str, Any]:
        """Create a task attempt record."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO task_attempts (task_id, attempt_no, plan_id, trigger, status)
            VALUES ($1, $2, $3, $4, 'running')
            RETURNING id, task_id, attempt_no, plan_id, trigger, status, started_at
            """,
            UUID(task_id.replace("task_", "")),
            attempt_no,
            UUID(plan_id.replace("plan_", "")) if plan_id else None,
            trigger,
        )
        return dict(row)

    async def complete_task_attempt(
        self,
        task_id: str,
        attempt_no: int,
        status: str,
        failure_class: Optional[str] = None,
    ) -> None:
        """Complete a task attempt."""
        await self.pool.execute(
            """
            UPDATE task_attempts
            SET status = $3, completed_at = NOW(), failure_class = $4
            WHERE task_id = $1 AND attempt_no = $2
            """,
            UUID(task_id.replace("task_", "")),
            attempt_no,
            status,
            failure_class,
        )

    # =========================================================================
    # Compiled Plans
    # =========================================================================

    async def create_plan(
        self,
        plan_id: str,
        task_id: str,
        session_id: str,
        plan_payload: dict[str, Any],
        plan_signature: str,
        expires_at: datetime,
        mode: str = "direct",
        allowed_tools: Optional[list[str]] = None,
        risk_flags: Optional[list[str]] = None,
        workspace_scope: Optional[str] = None,
        edit_scope: Optional[list[str]] = None,
        validation_steps: Optional[list[str]] = None,
        approval_requirements: Optional[dict[str, Any]] = None,
        budgets: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Create a compiled plan."""
        import json
        row = await self.pool.fetchrow(
            """
            INSERT INTO compiled_plans (id, task_id, session_id, mode, risk_flags,
                                        allowed_tools, workspace_scope, edit_scope,
                                        validation_steps, approval_requirements, budgets,
                                        plan_payload, plan_signature, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            RETURNING id, task_id, session_id, mode, created_at, expires_at
            """,
            UUID(plan_id.replace("plan_", "")),
            UUID(task_id.replace("task_", "")),
            UUID(session_id.replace("sess_", "")),
            mode,
            json.dumps(risk_flags or []),
            json.dumps(allowed_tools or []),
            workspace_scope,
            json.dumps(edit_scope or []),
            json.dumps(validation_steps or []),
            json.dumps(approval_requirements or {}),
            json.dumps(budgets or {}),
            json.dumps(plan_payload),
            plan_signature,
            expires_at,
        )
        return dict(row)

    async def get_plan(self, plan_id: str) -> Optional[dict[str, Any]]:
        """Get plan by ID."""
        row = await self.pool.fetchrow(
            """
            SELECT id, task_id, session_id, version, mode, risk_flags, allowed_tools,
                   workspace_scope, edit_scope, validation_steps, approval_requirements,
                   budgets, plan_payload, plan_signature, expires_at, created_at
            FROM compiled_plans WHERE id = $1
            """,
            UUID(plan_id.replace("plan_", "")),
        )
        return dict(row) if row else None

    # =========================================================================
    # Validations
    # =========================================================================

    async def create_validation(
        self,
        task_id: str,
        attempt_no: int,
        step_name: str,
        status: str,
        tool_name: Optional[str] = None,
        command_summary: Optional[str] = None,
        result_summary: Optional[str] = None,
        failures: Optional[list[dict]] = None,
    ) -> dict[str, Any]:
        """Create a validation record."""
        import json
        row = await self.pool.fetchrow(
            """
            INSERT INTO task_validations (task_id, attempt_no, step_name, status,
                                          tool_name, command_summary, result_summary, failures)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id, task_id, attempt_no, step_name, status, created_at
            """,
            UUID(task_id.replace("task_", "")),
            attempt_no,
            step_name,
            status,
            tool_name,
            command_summary,
            result_summary,
            json.dumps(failures or []),
        )
        return dict(row)

    # =========================================================================
    # Usage Events
    # =========================================================================

    async def create_usage_event(
        self,
        event_type: str,
        user_id: str,
        quantity: float,
        unit: str,
        org_id: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Create a usage event."""
        import json
        await self.pool.execute(
            """
            INSERT INTO usage_events (event_type, user_id, org_id, session_id, task_id,
                                      quantity, unit, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            event_type,
            UUID(user_id),
            UUID(org_id) if org_id else None,
            UUID(session_id.replace("sess_", "")) if session_id else None,
            UUID(task_id.replace("task_", "")) if task_id else None,
            quantity,
            unit,
            json.dumps(metadata or {}),
        )

    # =========================================================================
    # Audit Logs
    # =========================================================================

    async def create_audit_log(
        self,
        action: str,
        actor_type: str,
        actor_id: str,
        user_id: str,
        org_id: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        severity: str = "info",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """Create an audit log entry."""
        import json
        await self.pool.execute(
            """
            INSERT INTO audit_logs (action, actor_type, actor_id, user_id, org_id,
                                    session_id, task_id, severity, details)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            action,
            actor_type,
            actor_id,
            UUID(user_id),
            UUID(org_id) if org_id else None,
            UUID(session_id.replace("sess_", "")) if session_id else None,
            UUID(task_id.replace("task_", "")) if task_id else None,
            severity,
            json.dumps(details or {}),
        )

    # =========================================================================
    # Errors
    # =========================================================================

    async def store_error(
        self,
        error_id: str,
        error_type: str,
        error_message: str,
        stack_trace: str,
        severity: str = "error",
        component: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        request_id: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Store an error in the errors table for analysis and debugging.

        Args:
            error_id: Unique error identifier
            error_type: Type/class of the error (e.g., 'CommandInjectionError')
            error_message: The error message
            stack_trace: Full stack trace
            severity: Error severity ('critical', 'error', 'warning')
            component: Component where error occurred (e.g., 'hooks', 'llm_handler')
            user_id: User ID if available
            session_id: Session ID if available
            task_id: Task ID if available
            request_id: Request ID if available
            context: Additional context as JSON

        Returns:
            Dict with stored error info
        """
        import json
        row = await self.pool.fetchrow(
            """
            INSERT INTO errors (
                id, error_type, error_message, stack_trace, severity,
                component, user_id, session_id, task_id, request_id, context
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id, error_type, severity, component, created_at
            """,
            error_id,
            error_type,
            error_message[:2000],  # Truncate message if too long
            stack_trace[:10000],   # Truncate stack trace if too long
            severity,
            component,
            UUID(user_id) if user_id else None,
            UUID(session_id.replace("sess_", "")) if session_id else None,
            UUID(task_id.replace("task_", "")) if task_id else None,
            request_id,
            json.dumps(context or {}),
        )
        return dict(row)

    async def get_recent_errors(
        self,
        limit: int = 100,
        severity: Optional[str] = None,
        component: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Get recent errors for monitoring/debugging."""
        if severity and component:
            rows = await self.pool.fetch(
                """
                SELECT id, error_type, error_message, severity, component,
                       user_id, session_id, task_id, created_at
                FROM errors
                WHERE severity = $1 AND component = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                severity,
                component,
                limit,
            )
        elif severity:
            rows = await self.pool.fetch(
                """
                SELECT id, error_type, error_message, severity, component,
                       user_id, session_id, task_id, created_at
                FROM errors
                WHERE severity = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                severity,
                limit,
            )
        elif component:
            rows = await self.pool.fetch(
                """
                SELECT id, error_type, error_message, severity, component,
                       user_id, session_id, task_id, created_at
                FROM errors
                WHERE component = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                component,
                limit,
            )
        else:
            rows = await self.pool.fetch(
                """
                SELECT id, error_type, error_message, severity, component,
                       user_id, session_id, task_id, created_at
                FROM errors
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [dict(row) for row in rows]

    # =========================================================================
    # Session Checkpoints
    # =========================================================================

    async def create_checkpoint(
        self,
        session_id: str,
        checkpoint_type: str,
        payload: dict[str, Any],
        task_id: Optional[str] = None,
        attempt_no: Optional[int] = None,
    ) -> dict[str, Any]:
        """Create a session checkpoint."""
        import json
        row = await self.pool.fetchrow(
            """
            INSERT INTO session_checkpoints (session_id, task_id, attempt_no,
                                             checkpoint_type, payload)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, session_id, checkpoint_type, created_at
            """,
            UUID(session_id.replace("sess_", "")),
            UUID(task_id.replace("task_", "")) if task_id else None,
            attempt_no,
            checkpoint_type,
            json.dumps(payload),
        )
        return dict(row)

    async def get_latest_checkpoint(
        self,
        session_id: str,
        checkpoint_type: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Get latest checkpoint for session."""
        if checkpoint_type:
            row = await self.pool.fetchrow(
                """
                SELECT id, session_id, task_id, attempt_no, checkpoint_type, payload, created_at
                FROM session_checkpoints
                WHERE session_id = $1 AND checkpoint_type = $2
                ORDER BY created_at DESC
                LIMIT 1
                """,
                UUID(session_id.replace("sess_", "")),
                checkpoint_type,
            )
        else:
            row = await self.pool.fetchrow(
                """
                SELECT id, session_id, task_id, attempt_no, checkpoint_type, payload, created_at
                FROM session_checkpoints
                WHERE session_id = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                UUID(session_id.replace("sess_", "")),
            )
        return dict(row) if row else None


# Singleton instance
_postgres_client: Optional[PostgresClient] = None


async def get_postgres() -> PostgresClient:
    """Get or create PostgreSQL client singleton."""
    global _postgres_client
    if _postgres_client is None:
        _postgres_client = PostgresClient()
        await _postgres_client.connect()
    return _postgres_client
