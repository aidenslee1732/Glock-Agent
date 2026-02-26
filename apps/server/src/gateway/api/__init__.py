"""REST API routes for Glock gateway."""

from .auth import router as auth_router
from .sessions import router as sessions_router
from .tasks import router as tasks_router
from .profile import router as profile_router
from .health import router as health_router

__all__ = [
    "auth_router",
    "sessions_router",
    "tasks_router",
    "profile_router",
    "health_router",
]
