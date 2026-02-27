"""Centralized error handling for Glock server.

This module provides:
1. GlockError - Base exception that stores errors in Supabase (prod) and shows friendly messages
2. ErrorStore - Stores errors in the database for analysis
3. handle_error() - Async function to process and store errors
4. handle_error_sync() - Sync wrapper for non-async contexts
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..storage.postgres import PostgresClient

logger = logging.getLogger(__name__)

# User-friendly error message
USER_FRIENDLY_MESSAGE = "We are experiencing some issues; please bear with us"

# Environment check
DEV_MODE = os.environ.get("DEV_MODE", "").lower() in ("1", "true", "yes")


@dataclass
class ErrorContext:
    """Context information for error tracking."""
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    request_id: Optional[str] = None
    component: Optional[str] = None
    additional: Optional[dict[str, Any]] = None


class GlockError(Exception):
    """Base exception for Glock that stores errors in production.

    In production:
    - Stores the full error details in Supabase
    - Returns a user-friendly message to the client

    In development:
    - Logs the full error
    - Returns the actual error message for debugging
    """

    def __init__(
        self,
        message: str,
        *,
        original_error: Optional[Exception] = None,
        severity: str = "error",
        context: Optional[ErrorContext] = None,
        error_id: Optional[str] = None,
    ):
        self.original_message = message
        self.original_error = original_error
        self.severity = severity
        self.context = context or ErrorContext()
        self.error_id = error_id or f"err_{uuid.uuid4().hex[:16]}"
        self.stored = False

        # In production, show friendly message; in dev, show actual error
        if DEV_MODE:
            super().__init__(message)
        else:
            super().__init__(USER_FRIENDLY_MESSAGE)

    @property
    def full_message(self) -> str:
        """Get the full error message (for logging/storage)."""
        return self.original_message

    @property
    def user_message(self) -> str:
        """Get the user-facing message."""
        if DEV_MODE:
            return self.original_message
        return USER_FRIENDLY_MESSAGE

    def get_stack_trace(self) -> str:
        """Get the full stack trace."""
        if self.original_error:
            return "".join(traceback.format_exception(
                type(self.original_error),
                self.original_error,
                self.original_error.__traceback__,
            ))
        return traceback.format_exc()


class UserFacingError(GlockError):
    """Error that should be shown to the user as-is.

    Use this for errors that are user-caused (e.g., invalid input)
    where the message is safe to show.
    """

    def __init__(
        self,
        message: str,
        *,
        original_error: Optional[Exception] = None,
        severity: str = "warning",
        context: Optional[ErrorContext] = None,
    ):
        super().__init__(
            message,
            original_error=original_error,
            severity=severity,
            context=context,
        )
        # Override to always show the actual message
        Exception.__init__(self, message)


class ErrorStore:
    """Stores errors in Supabase for analysis.

    Provides both sync and async interfaces for error storage.
    In dev mode, errors are only logged, not stored.
    """

    def __init__(self, postgres: Optional["PostgresClient"] = None):
        self._postgres = postgres
        self._pending_errors: list[dict[str, Any]] = []

    def set_postgres(self, postgres: "PostgresClient") -> None:
        """Set the Postgres client (for deferred initialization)."""
        self._postgres = postgres

    async def store_error(
        self,
        error: Exception,
        component: Optional[str] = None,
        context: Optional[ErrorContext] = None,
    ) -> str:
        """Store an error in the database.

        Args:
            error: The exception to store
            component: Component where error occurred
            context: Additional context

        Returns:
            Error ID for reference
        """
        # Generate error ID
        if isinstance(error, GlockError):
            error_id = error.error_id
            error_type = type(error.original_error).__name__ if error.original_error else type(error).__name__
            error_message = error.full_message
            stack_trace = error.get_stack_trace()
            severity = error.severity
            ctx = error.context
        else:
            error_id = f"err_{uuid.uuid4().hex[:16]}"
            error_type = type(error).__name__
            error_message = str(error)
            stack_trace = traceback.format_exc()
            severity = "error"
            ctx = context or ErrorContext()

        # Merge contexts
        if context:
            ctx.user_id = ctx.user_id or context.user_id
            ctx.session_id = ctx.session_id or context.session_id
            ctx.task_id = ctx.task_id or context.task_id
            ctx.request_id = ctx.request_id or context.request_id
            ctx.component = ctx.component or context.component

        # Use component parameter if not in context
        final_component = ctx.component or component

        # In dev mode, just log
        if DEV_MODE:
            logger.error(
                f"[{severity.upper()}] {error_type} in {final_component}: {error_message}\n"
                f"Error ID: {error_id}\n"
                f"Stack trace:\n{stack_trace}"
            )
            return error_id

        # In production, store in Supabase
        if self._postgres:
            try:
                await self._postgres.store_error(
                    error_id=error_id,
                    error_type=error_type,
                    error_message=error_message,
                    stack_trace=stack_trace,
                    severity=severity,
                    component=final_component,
                    user_id=ctx.user_id,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    request_id=ctx.request_id,
                    context=ctx.additional,
                )
                logger.info(f"Stored error {error_id} in database")

                # Mark as stored if it's a GlockError
                if isinstance(error, GlockError):
                    error.stored = True

            except Exception as store_error:
                # If we can't store the error, log it locally
                logger.error(
                    f"Failed to store error in database: {store_error}\n"
                    f"Original error [{error_id}]: {error_type}: {error_message}"
                )
        else:
            logger.warning(
                f"No database connection - error not stored: [{error_id}] {error_type}: {error_message}"
            )

        return error_id

    def store_error_sync(
        self,
        error: Exception,
        component: Optional[str] = None,
        context: Optional[ErrorContext] = None,
    ) -> str:
        """Synchronous wrapper for store_error.

        Queues the error for async storage or logs immediately in dev mode.
        """
        # In dev mode, just log synchronously
        if DEV_MODE:
            error_id = getattr(error, 'error_id', f"err_{uuid.uuid4().hex[:16]}")
            logger.error(
                f"[SYNC] {type(error).__name__}: {error}\n"
                f"Error ID: {error_id}\n"
                f"Stack trace:\n{traceback.format_exc()}"
            )
            return error_id

        # Try to run async in existing event loop
        try:
            loop = asyncio.get_running_loop()
            # Schedule for later execution
            future = asyncio.ensure_future(
                self.store_error(error, component, context)
            )
            # Return a temporary ID - the real one will be in the future
            return getattr(error, 'error_id', f"err_{uuid.uuid4().hex[:16]}")
        except RuntimeError:
            # No event loop running - queue for later or log
            error_id = getattr(error, 'error_id', f"err_{uuid.uuid4().hex[:16]}")
            self._pending_errors.append({
                'error': error,
                'component': component,
                'context': context,
                'error_id': error_id,
            })
            logger.warning(f"Error queued for async storage: {error_id}")
            return error_id

    async def flush_pending(self) -> int:
        """Flush any pending errors that were queued synchronously.

        Returns:
            Number of errors flushed
        """
        count = 0
        while self._pending_errors:
            pending = self._pending_errors.pop(0)
            await self.store_error(
                pending['error'],
                pending.get('component'),
                pending.get('context'),
            )
            count += 1
        return count


# Global error store instance
_error_store: Optional[ErrorStore] = None


def get_error_store() -> ErrorStore:
    """Get the global error store instance."""
    global _error_store
    if _error_store is None:
        _error_store = ErrorStore()
    return _error_store


def init_error_store(postgres: "PostgresClient") -> ErrorStore:
    """Initialize the error store with a Postgres client."""
    store = get_error_store()
    store.set_postgres(postgres)
    return store


async def handle_error(
    error: Exception,
    component: Optional[str] = None,
    context: Optional[ErrorContext] = None,
    reraise: bool = True,
) -> str:
    """Handle an error: store it and optionally re-raise.

    Args:
        error: The exception to handle
        component: Component where error occurred
        context: Additional context
        reraise: Whether to re-raise as GlockError

    Returns:
        Error ID

    Raises:
        GlockError: If reraise=True (wraps original error)
    """
    store = get_error_store()
    error_id = await store.store_error(error, component, context)

    if reraise:
        if isinstance(error, GlockError):
            raise error
        else:
            raise GlockError(
                str(error),
                original_error=error,
                context=context,
                error_id=error_id,
            ) from error

    return error_id


def handle_error_sync(
    error: Exception,
    component: Optional[str] = None,
    context: Optional[ErrorContext] = None,
    reraise: bool = True,
) -> str:
    """Synchronous version of handle_error.

    Args:
        error: The exception to handle
        component: Component where error occurred
        context: Additional context
        reraise: Whether to re-raise as GlockError

    Returns:
        Error ID

    Raises:
        GlockError: If reraise=True (wraps original error)
    """
    store = get_error_store()
    error_id = store.store_error_sync(error, component, context)

    if reraise:
        if isinstance(error, GlockError):
            raise error
        else:
            raise GlockError(
                str(error),
                original_error=error,
                context=context,
                error_id=error_id,
            ) from error

    return error_id
