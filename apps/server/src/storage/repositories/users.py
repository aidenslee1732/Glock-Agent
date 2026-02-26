"""User repository for database access."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from ..postgres import PostgresClient


@dataclass
class User:
    """User model."""
    id: str
    email: str
    name: Optional[str]
    status: str
    plan_tier: str
    created_at: datetime
    updated_at: datetime
    last_seen_at: Optional[datetime]


class UserRepository:
    """Repository for user operations."""

    def __init__(self, db: PostgresClient):
        self.db = db

    async def get(self, user_id: str) -> Optional[User]:
        """Get user by ID."""
        data = await self.db.get_user(user_id)
        return self._to_user(data) if data else None

    async def get_by_email(self, email: str) -> Optional[User]:
        """Get user by email."""
        data = await self.db.get_user_by_email(email)
        return self._to_user(data) if data else None

    async def create(
        self,
        email: str,
        name: Optional[str] = None,
        plan_tier: str = "free",
    ) -> User:
        """Create a new user."""
        data = await self.db.create_user(email, name, plan_tier)
        return self._to_user(data)

    async def update_last_seen(self, user_id: str) -> None:
        """Update user's last seen timestamp."""
        await self.db.update_user_last_seen(user_id)

    def _to_user(self, data: dict) -> User:
        """Convert dict to User model."""
        return User(
            id=str(data['id']),
            email=data['email'],
            name=data.get('name'),
            status=data.get('status', 'active'),
            plan_tier=data.get('plan_tier', 'free'),
            created_at=data.get('created_at', datetime.utcnow()),
            updated_at=data.get('updated_at', datetime.utcnow()),
            last_seen_at=data.get('last_seen_at'),
        )
