"""Capsule factory with auto-detection."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


class CapsuleMode(str, Enum):
    """Capsule isolation modes."""

    PROCESS = "process"  # Process-based isolation (default)
    DOCKER = "docker"    # Docker container isolation


@dataclass
class SessionContext:
    """Context for capsule mode detection."""

    session_id: str
    user_id: str
    workspace_path: str

    # Plan info
    risk_flags: list[str] = None
    user_plan: str = "free"

    # Environment info
    active_session_count: int = 1
    docker_available: bool = False
    has_host_deps: bool = False

    # User preferences
    prefer_docker: Optional[bool] = None


class Capsule(Protocol):
    """Protocol for capsule implementations."""

    @property
    def session_id(self) -> str: ...

    @property
    def workspace_path(self) -> str: ...

    async def start(self): ...

    async def stop(self): ...

    async def execute(self, command: str, timeout: float = 120.0) -> tuple[int, str, str]: ...


class CapsuleFactory:
    """Factory for creating capsules with auto-detection.

    Auto-detects when to use Docker isolation based on:
    1. More than 2 concurrent sessions
    2. Enterprise policy requires it
    3. Security-sensitive task (from risk_flags)
    4. Explicit user preference
    """

    def __init__(self):
        self._docker_available: Optional[bool] = None

    def create(self, context: SessionContext) -> Capsule:
        """Create a capsule for the given context."""
        mode = self._detect_mode(context)

        logger.info(f"Creating {mode.value} capsule for session {context.session_id}")

        if mode == CapsuleMode.DOCKER:
            from .docker import DockerCapsule
            return DockerCapsule(
                session_id=context.session_id,
                workspace_path=context.workspace_path
            )
        else:
            from .process import ProcessCapsule
            return ProcessCapsule(
                session_id=context.session_id,
                workspace_path=context.workspace_path
            )

    def _detect_mode(self, context: SessionContext) -> CapsuleMode:
        """Auto-detect the appropriate capsule mode."""
        # Check explicit user preference first
        if context.prefer_docker is True:
            if self._check_docker_available():
                return CapsuleMode.DOCKER
            logger.warning("Docker requested but not available, falling back to process")

        if context.prefer_docker is False:
            return CapsuleMode.PROCESS

        # Check if Docker is available
        if not self._check_docker_available():
            return CapsuleMode.PROCESS

        # Rule 1: More than 2 concurrent sessions
        if context.active_session_count > 2:
            logger.info("Using Docker due to multiple concurrent sessions")
            return CapsuleMode.DOCKER

        # Rule 2: Enterprise policy
        if context.user_plan == "enterprise":
            logger.info("Using Docker due to enterprise policy")
            return CapsuleMode.DOCKER

        # Rule 3: Security-sensitive task
        risk_flags = context.risk_flags or []
        if "security" in risk_flags or "secrets" in risk_flags:
            logger.info("Using Docker due to security-sensitive task")
            return CapsuleMode.DOCKER

        # Rule 4: Has host dependencies that conflict with Docker
        if context.has_host_deps:
            logger.info("Using process mode due to host dependencies")
            return CapsuleMode.PROCESS

        # Default: process mode for simplicity
        return CapsuleMode.PROCESS

    def _check_docker_available(self) -> bool:
        """Check if Docker is available and running."""
        if self._docker_available is not None:
            return self._docker_available

        # Check if docker command exists
        docker_path = shutil.which("docker")
        if not docker_path:
            self._docker_available = False
            return False

        # Check if Docker daemon is running
        try:
            import subprocess
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5
            )
            self._docker_available = result.returncode == 0
        except Exception:
            self._docker_available = False

        return self._docker_available

    def get_available_modes(self) -> list[CapsuleMode]:
        """Get list of available capsule modes."""
        modes = [CapsuleMode.PROCESS]

        if self._check_docker_available():
            modes.append(CapsuleMode.DOCKER)

        return modes
