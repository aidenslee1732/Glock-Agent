"""Error handling system for Glock server.

Provides centralized error handling that:
- Stores errors in Supabase for debugging/analysis
- Returns user-friendly messages to clients
- Supports both sync and async contexts
"""

from .handler import (
    GlockError,
    UserFacingError,
    handle_error,
    handle_error_sync,
    ErrorStore,
    get_error_store,
)

__all__ = [
    "GlockError",
    "UserFacingError",
    "handle_error",
    "handle_error_sync",
    "ErrorStore",
    "get_error_store",
]
