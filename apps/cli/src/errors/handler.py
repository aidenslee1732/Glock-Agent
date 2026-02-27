"""Client-side error handling for Glock CLI.

This module provides:
1. GlockClientError - Base exception with user-friendly messages
2. ErrorReporter - Reports errors to the server (prod) or logs locally (dev)
3. report_error() - Async function to report errors
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..transport.ws_client import WebSocketClient

logger = logging.getLogger(__name__)

# User-friendly error message
USER_FRIENDLY_MESSAGE = "We are experiencing some issues; please bear with us"

# Environment check
DEV_MODE = os.environ.get("DEV_MODE", "").lower() in ("1", "true", "yes")

# Local error log path
ERROR_LOG_PATH = Path.home() / ".glock" / "errors.log"


@dataclass
class ErrorContext:
    """Context information for error tracking."""
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    request_id: Optional[str] = None
    component: Optional[str] = None
    tool_name: Optional[str] = None
    additional: Optional[dict[str, Any]] = None


class GlockClientError(Exception):
    """Base exception for Glock CLI that shows user-friendly messages.

    In production:
    - Reports the full error to the server (if connected)
    - Logs the error locally
    - Shows a user-friendly message

    In development:
    - Shows the actual error message
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
        self.error_id = error_id or f"cli_err_{uuid.uuid4().hex[:12]}"
        self.reported = False

        # In production, show friendly message; in dev, show actual error
        if DEV_MODE:
            super().__init__(message)
        else:
            super().__init__(USER_FRIENDLY_MESSAGE)

    @property
    def full_message(self) -> str:
        """Get the full error message (for logging/reporting)."""
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


class UserFacingError(GlockClientError):
    """Error that should be shown to the user as-is.

    Use this for user-caused errors where the message is safe to show.
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


class ErrorReporter:
    """Reports errors to the server and logs locally.

    In production:
    - Sends error details to server via WebSocket
    - Logs to local file as backup

    In development:
    - Logs to console and local file
    """

    def __init__(self, ws_client: Optional["WebSocketClient"] = None):
        self._ws_client = ws_client
        self._pending_errors: list[dict[str, Any]] = []

        # Ensure error log directory exists
        ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def set_ws_client(self, ws_client: "WebSocketClient") -> None:
        """Set the WebSocket client for error reporting."""
        self._ws_client = ws_client

    def _log_locally(
        self,
        error_id: str,
        error_type: str,
        message: str,
        stack_trace: str,
        severity: str,
        context: ErrorContext,
    ) -> None:
        """Log error to local file."""
        try:
            with open(ERROR_LOG_PATH, "a") as f:
                entry = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "error_id": error_id,
                    "error_type": error_type,
                    "message": message[:500],  # Truncate for local log
                    "severity": severity,
                    "component": context.component,
                    "session_id": context.session_id,
                    "task_id": context.task_id,
                }
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning(f"Failed to write to local error log: {e}")

    async def report_error(
        self,
        error: Exception,
        component: Optional[str] = None,
        context: Optional[ErrorContext] = None,
    ) -> str:
        """Report an error to the server.

        Args:
            error: The exception to report
            component: Component where error occurred
            context: Additional context

        Returns:
            Error ID for reference
        """
        # Extract error details
        if isinstance(error, GlockClientError):
            error_id = error.error_id
            error_type = type(error.original_error).__name__ if error.original_error else type(error).__name__
            error_message = error.full_message
            stack_trace = error.get_stack_trace()
            severity = error.severity
            ctx = error.context
        else:
            error_id = f"cli_err_{uuid.uuid4().hex[:12]}"
            error_type = type(error).__name__
            error_message = str(error)
            stack_trace = traceback.format_exc()
            severity = "error"
            ctx = context or ErrorContext()

        # Merge contexts
        if context:
            ctx.session_id = ctx.session_id or context.session_id
            ctx.task_id = ctx.task_id or context.task_id
            ctx.component = ctx.component or context.component

        final_component = ctx.component or component

        # Always log locally
        self._log_locally(
            error_id, error_type, error_message,
            stack_trace, severity, ctx
        )

        # In dev mode, also log to console
        if DEV_MODE:
            logger.error(
                f"[{severity.upper()}] {error_type} in {final_component}: {error_message}\n"
                f"Error ID: {error_id}"
            )
            return error_id

        # In production, try to report to server
        if self._ws_client:
            try:
                await self._ws_client.send_error_report(
                    error_id=error_id,
                    error_type=error_type,
                    error_message=error_message,
                    stack_trace=stack_trace,
                    severity=severity,
                    component=final_component,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    context=ctx.additional,
                )
                logger.debug(f"Reported error {error_id} to server")

                if isinstance(error, GlockClientError):
                    error.reported = True

            except Exception as report_error:
                logger.warning(f"Failed to report error to server: {report_error}")
        else:
            logger.debug(f"No WebSocket connection - error logged locally only: {error_id}")

        return error_id

    def report_error_sync(
        self,
        error: Exception,
        component: Optional[str] = None,
        context: Optional[ErrorContext] = None,
    ) -> str:
        """Synchronous wrapper for report_error."""
        # Extract error ID
        error_id = getattr(error, 'error_id', f"cli_err_{uuid.uuid4().hex[:12]}")

        # Always log locally (sync is always possible)
        if isinstance(error, GlockClientError):
            ctx = error.context
            self._log_locally(
                error_id,
                type(error.original_error).__name__ if error.original_error else type(error).__name__,
                error.full_message,
                error.get_stack_trace(),
                error.severity,
                ctx,
            )
        else:
            ctx = context or ErrorContext()
            self._log_locally(
                error_id,
                type(error).__name__,
                str(error),
                traceback.format_exc(),
                "error",
                ctx,
            )

        # Try to schedule async reporting
        try:
            loop = asyncio.get_running_loop()
            asyncio.ensure_future(self.report_error(error, component, context))
        except RuntimeError:
            # No event loop - queue for later
            self._pending_errors.append({
                'error': error,
                'component': component,
                'context': context,
            })

        return error_id

    async def flush_pending(self) -> int:
        """Flush any pending errors."""
        count = 0
        while self._pending_errors:
            pending = self._pending_errors.pop(0)
            await self.report_error(
                pending['error'],
                pending.get('component'),
                pending.get('context'),
            )
            count += 1
        return count


# Global reporter instance
_error_reporter: Optional[ErrorReporter] = None


def get_error_reporter() -> ErrorReporter:
    """Get the global error reporter instance."""
    global _error_reporter
    if _error_reporter is None:
        _error_reporter = ErrorReporter()
    return _error_reporter


def init_error_reporter(ws_client: "WebSocketClient") -> ErrorReporter:
    """Initialize the error reporter with a WebSocket client."""
    reporter = get_error_reporter()
    reporter.set_ws_client(ws_client)
    return reporter


async def report_error_async(
    error: Exception,
    component: Optional[str] = None,
    context: Optional[ErrorContext] = None,
    reraise: bool = True,
) -> str:
    """Report an error and optionally re-raise.

    Args:
        error: The exception to handle
        component: Component where error occurred
        context: Additional context
        reraise: Whether to re-raise as GlockClientError

    Returns:
        Error ID

    Raises:
        GlockClientError: If reraise=True
    """
    reporter = get_error_reporter()
    error_id = await reporter.report_error(error, component, context)

    if reraise:
        if isinstance(error, GlockClientError):
            raise error
        else:
            raise GlockClientError(
                str(error),
                original_error=error,
                context=context,
                error_id=error_id,
            ) from error

    return error_id


def report_error(
    error: Exception,
    component: Optional[str] = None,
    context: Optional[ErrorContext] = None,
    reraise: bool = True,
) -> str:
    """Synchronous version of report_error_async.

    Args:
        error: The exception to handle
        component: Component where error occurred
        context: Additional context
        reraise: Whether to re-raise as GlockClientError

    Returns:
        Error ID

    Raises:
        GlockClientError: If reraise=True
    """
    reporter = get_error_reporter()
    error_id = reporter.report_error_sync(error, component, context)

    if reraise:
        if isinstance(error, GlockClientError):
            raise error
        else:
            raise GlockClientError(
                str(error),
                original_error=error,
                context=context,
                error_id=error_id,
            ) from error

    return error_id
