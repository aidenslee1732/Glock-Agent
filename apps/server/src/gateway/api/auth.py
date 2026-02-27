"""Authentication API endpoints."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

from ...storage.postgres import PostgresClient
from ...storage.redis import RedisClient

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


# Configuration - load from environment
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-mode-secret-key-at-least-32-chars")
JWT_ALGORITHM = "HS256"
JWT_ISSUER = os.environ.get("JWT_ISSUER", "glock.dev")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.environ.get("REFRESH_TOKEN_EXPIRE_DAYS", "30"))


@dataclass
class TokenPayload:
    """Decoded JWT token payload."""
    user_id: str
    email: str
    plan_tier: str
    token_type: str = "access"


class AuthError(Exception):
    """Authentication error with message."""
    def __init__(self, message: str, code: str = "auth_error"):
        self.message = message
        self.code = code
        super().__init__(message)


def verify_token(token: str, require_type: str = "access") -> TokenPayload:
    """
    Verify a JWT token and return the payload.

    This is the core verification function used by both HTTP and WebSocket.

    Args:
        token: The JWT token string
        require_type: Expected token type ("access" or "refresh")

    Returns:
        TokenPayload with user information

    Raises:
        AuthError: If token is invalid, expired, or wrong type
    """
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER
        )

        user_id = payload.get("sub")
        if not user_id:
            raise AuthError("Invalid token: missing user ID", "invalid_token")

        token_type = payload.get("type", "access")
        if token_type != require_type:
            raise AuthError(f"Invalid token type: expected {require_type}", "invalid_token_type")

        return TokenPayload(
            user_id=user_id,
            email=payload.get("email", ""),
            plan_tier=payload.get("plan_tier", "free"),
            token_type=token_type,
        )

    except jwt.ExpiredSignatureError:
        raise AuthError("Token has expired", "token_expired")
    except jwt.InvalidIssuerError:
        raise AuthError("Invalid token issuer", "invalid_issuer")
    except jwt.InvalidTokenError as e:
        raise AuthError(f"Invalid token: {e}", "invalid_token")


def verify_websocket_token(token: Optional[str]) -> TokenPayload:
    """
    Verify a token for WebSocket connections.

    Args:
        token: JWT token from query params or first message

    Returns:
        TokenPayload with user information

    Raises:
        AuthError: If token is missing or invalid
    """
    if not token:
        raise AuthError("Missing authentication token", "missing_token")

    # Remove "Bearer " prefix if present
    if token.startswith("Bearer "):
        token = token[7:]

    return verify_token(token, require_type="access")


# Request/Response Models
class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


# Dependency to get current user
async def get_current_user(
    authorization: str = None,
    db: PostgresClient = None
) -> dict:
    """Validate JWT and return current user."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header"
        )

    token = authorization[7:]  # Remove "Bearer "

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER
        )

        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload"
            )

        # Get user from database
        user = await db.get_user(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found"
            )

        if user.get("status") != "active":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is not active"
            )

        return user

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except jwt.InvalidTokenError as e:
        # In production, don't expose internal error details
        import os
        DEV_MODE = os.environ.get("DEV_MODE", "").lower() in ("1", "true", "yes")
        if DEV_MODE:
            detail = f"Invalid token: {e}"
        else:
            detail = "We are experiencing some issues; please bear with us"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail
        )


def create_access_token(user_id: str, email: str, plan_tier: str) -> tuple[str, int]:
    """Create a new access token."""
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    expires_in = ACCESS_TOKEN_EXPIRE_MINUTES * 60

    payload = {
        "sub": user_id,
        "email": email,
        "plan_tier": plan_tier,
        "iss": JWT_ISSUER,
        "iat": datetime.now(timezone.utc),
        "exp": expires_at,
        "type": "access"
    }

    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, expires_in


def create_refresh_token(user_id: str, client_id: str) -> tuple[str, str, datetime]:
    """Create a new refresh token.

    Returns:
        Tuple of (token, token_hash, expires_at)
    """
    token = secrets.token_urlsafe(64)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    return token, token_hash, expires_at


@router.post("/login", response_model=TokenResponse)
async def login(
    request: LoginRequest,
    db: PostgresClient = Depends(),
    redis: RedisClient = Depends()
):
    """Authenticate user and return tokens."""
    # In production, verify password hash
    # For now, simplified auth (replace with proper auth)
    user = await db.get_user_by_email(request.email)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # Create tokens
    access_token, expires_in = create_access_token(
        user_id=user["id"],
        email=user["email"],
        plan_tier=user.get("plan_tier", "free")
    )

    client_id = f"cli_{secrets.token_hex(8)}"
    refresh_token, token_hash, expires_at = create_refresh_token(
        user_id=user["id"],
        client_id=client_id
    )

    # Store refresh token in database
    await db.create_refresh_token(
        user_id=user["id"],
        token_hash=token_hash,
        client_id=client_id,
        expires_at=expires_at
    )

    # Update last seen
    await db.update_user_last_seen(user["id"])

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(
    request: RefreshRequest,
    db: PostgresClient = Depends()
):
    """Refresh access token using refresh token."""
    # Hash the provided token
    token_hash = hashlib.sha256(request.refresh_token.encode()).hexdigest()

    # Look up token in database
    token_record = await db.get_refresh_token(token_hash)

    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token"
        )

    if token_record.get("revoked_at"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked"
        )

    expires_at = token_record.get("expires_at")
    if expires_at and datetime.now(timezone.utc) > expires_at:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has expired"
        )

    # Get user
    user = await db.get_user(token_record["user_id"])
    if not user or user.get("status") != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive"
        )

    # Create new access token
    access_token, expires_in = create_access_token(
        user_id=user["id"],
        email=user["email"],
        plan_tier=user.get("plan_tier", "free")
    )

    # Optionally rotate refresh token (for enhanced security)
    new_refresh_token, new_token_hash, new_expires_at = create_refresh_token(
        user_id=user["id"],
        client_id=token_record["client_id"]
    )

    # Revoke old token and create new one
    await db.revoke_refresh_token(token_hash)
    await db.create_refresh_token(
        user_id=user["id"],
        token_hash=new_token_hash,
        client_id=token_record["client_id"],
        expires_at=new_expires_at
    )

    # Update last used
    await db.update_user_last_seen(user["id"])

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=expires_in
    )


@router.post("/logout")
async def logout(
    request: LogoutRequest,
    db: PostgresClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Logout user and revoke tokens."""
    if request.refresh_token:
        token_hash = hashlib.sha256(request.refresh_token.encode()).hexdigest()
        await db.revoke_refresh_token(token_hash)

    return {"status": "logged_out"}


@router.post("/revoke-all")
async def revoke_all_tokens(
    db: PostgresClient = Depends(),
    user: dict = Depends(get_current_user)
):
    """Revoke all refresh tokens for the current user."""
    await db.revoke_all_user_tokens(user["id"])
    return {"status": "all_tokens_revoked"}
