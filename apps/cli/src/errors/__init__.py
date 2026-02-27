"""Client-side error handling for Glock CLI.

Provides:
- GlockClientError: Base exception with user-friendly messages
- Error reporting to server (in production)
- Local error logging
"""

from .handler import (
    GlockClientError,
    UserFacingError,
    ErrorContext,
    report_error,
    report_error_async,
    ErrorReporter,
    get_error_reporter,
    init_error_reporter,
)

__all__ = [
    "GlockClientError",
    "UserFacingError",
    "ErrorContext",
    "report_error",
    "report_error_async",
    "ErrorReporter",
    "get_error_reporter",
    "init_error_reporter",
]
