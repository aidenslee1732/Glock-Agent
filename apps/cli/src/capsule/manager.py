"""Capsule Manager - orchestrates sandboxed command execution.

Manages the lifecycle of sandbox capsules and routes commands through
appropriate isolation mechanisms based on policy.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from .policy import SandboxPolicy, SandboxMode
from .docker import DockerCapsule, DockerCapsuleConfig

logger = logging.getLogger(__name__)


class CapsuleManager:
    """Manages sandboxed command execution.

    Routes commands through appropriate sandbox (Docker, process, or none)
    based on policy configuration. Handles fallback when primary sandbox
    is unavailable.
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: str,
        policy: Optional[SandboxPolicy] = None,
    ):
        """Initialize capsule manager.

        Args:
            session_id: Session identifier
            workspace_path: Path to workspace directory
            policy: Sandbox policy (defaults to standard)
        """
        self.session_id = session_id
        self.workspace_path = Path(workspace_path).resolve()
        self.policy = policy or SandboxPolicy.standard()

        self._capsule: Optional[DockerCapsule] = None
        self._docker_available: Optional[bool] = None
        self._active_mode: SandboxMode = self.policy.mode

    async def ensure_started(self) -> None:
        """Ensure sandbox is started.

        Starts the appropriate sandbox based on policy, with fallback
        if primary mode is unavailable.
        """
        if self._capsule is not None:
            return

        if self.policy.mode == SandboxMode.DOCKER:
            if await self._check_docker_available():
                await self._start_docker_capsule()
                return

            # Fall back
            logger.warning("Docker not available, falling back to process mode")
            self._active_mode = self.policy.fallback_mode

        if self._active_mode == SandboxMode.PROCESS:
            # Process mode uses direct execution with restrictions
            logger.info("Using process isolation mode")

        elif self._active_mode == SandboxMode.NONE:
            logger.warning("Running without sandbox isolation (development mode)")

    async def execute(
        self,
        command: str,
        timeout: Optional[float] = None,
        env: Optional[dict[str, str]] = None,
    ) -> tuple[int, str, str]:
        """Execute a command in the sandbox.

        Args:
            command: Shell command to execute
            timeout: Timeout in seconds (uses policy default if None)
            env: Additional environment variables

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        # Check command against policy
        allowed, reason = self._is_command_allowed(command)
        if not allowed:
            return -1, "", f"Command blocked by sandbox policy: {reason}"

        # Check workspace boundary
        if self.policy.workspace_boundary:
            if not self._is_within_workspace(command):
                return -1, "", "Command attempts to access files outside workspace"

        timeout = timeout or self.policy.timeout_seconds

        # Route to appropriate execution method
        await self.ensure_started()

        if self._active_mode == SandboxMode.DOCKER and self._capsule:
            return await self._capsule.execute(
                command=command,
                timeout=timeout,
                env=env,
            )

        elif self._active_mode == SandboxMode.PROCESS:
            return await self._execute_process(
                command=command,
                timeout=timeout,
                env=env,
            )

        else:  # NONE mode
            return await self._execute_direct(
                command=command,
                timeout=timeout,
                env=env,
            )

    async def stop(self) -> None:
        """Stop the sandbox."""
        if self._capsule:
            await self._capsule.stop()
            self._capsule = None

    def _is_command_allowed(self, command: str) -> tuple[bool, str]:
        """Check if command is allowed by policy."""
        return self.policy.is_command_allowed(command)

    def _is_within_workspace(self, command: str) -> bool:
        """Check if command stays within workspace boundaries.

        This is a heuristic check - not foolproof for all cases.
        """
        # Look for obvious path escapes
        escape_patterns = [
            "../..",
            "/etc/",
            "/root/",
            "/home/",
            "~/.ssh",
            "~/.aws",
        ]

        command_lower = command.lower()
        for pattern in escape_patterns:
            if pattern in command_lower:
                # Check if it's within allowed patterns
                if pattern.startswith("~"):
                    # Home directory patterns - check forbidden paths
                    allowed, _ = self.policy.is_path_allowed(pattern)
                    if not allowed:
                        return False

        return True

    async def _check_docker_available(self) -> bool:
        """Check if Docker is available."""
        if self._docker_available is not None:
            return self._docker_available

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            self._docker_available = proc.returncode == 0
        except Exception:
            self._docker_available = False

        return self._docker_available

    async def _start_docker_capsule(self) -> None:
        """Start Docker-based capsule."""
        config = DockerCapsuleConfig(
            memory_limit=self.policy.memory_limit,
            cpu_limit=self.policy.cpu_limit,
            pids_limit=self.policy.pids_limit,
            network_mode="bridge" if self.policy.allow_network else "none",
            read_only=self.policy.read_only_root,
            default_timeout_seconds=float(self.policy.timeout_seconds),
        )

        self._capsule = DockerCapsule(
            session_id=self.session_id,
            workspace_path=str(self.workspace_path),
            config=config,
        )

        await self._capsule.start()
        self._active_mode = SandboxMode.DOCKER
        logger.info("Docker capsule started")

    async def _execute_process(
        self,
        command: str,
        timeout: float,
        env: Optional[dict[str, str]],
    ) -> tuple[int, str, str]:
        """Execute command with process-level isolation."""
        import os
        import resource

        # Set resource limits for child process
        def set_limits():
            # Memory limit (soft, hard) in bytes
            mem_bytes = self._parse_memory_limit(self.policy.memory_limit)
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            except (ValueError, resource.error):
                pass

            # CPU time limit
            try:
                cpu_seconds = int(self.policy.timeout_seconds)
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            except (ValueError, resource.error):
                pass

        env_dict = {**os.environ}
        if env:
            env_dict.update(env)

        # Remove potentially dangerous environment variables
        for key in ["LD_PRELOAD", "LD_LIBRARY_PATH"]:
            env_dict.pop(key, None)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.workspace_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env_dict,
                preexec_fn=set_limits if hasattr(resource, 'setrlimit') else None,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            return (
                proc.returncode or 0,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )

        except asyncio.TimeoutError:
            proc.kill()
            return -1, "", f"Command timed out after {timeout}s"

        except Exception as e:
            return -1, "", str(e)

    async def _execute_direct(
        self,
        command: str,
        timeout: float,
        env: Optional[dict[str, str]],
    ) -> tuple[int, str, str]:
        """Execute command directly (no sandbox)."""
        import os

        env_dict = {**os.environ}
        if env:
            env_dict.update(env)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.workspace_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env_dict,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            return (
                proc.returncode or 0,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )

        except asyncio.TimeoutError:
            proc.kill()
            return -1, "", f"Command timed out after {timeout}s"

        except Exception as e:
            return -1, "", str(e)

    def _parse_memory_limit(self, limit: str) -> int:
        """Parse memory limit string to bytes."""
        limit = limit.lower().strip()

        multipliers = {
            "k": 1024,
            "m": 1024 ** 2,
            "g": 1024 ** 3,
        }

        for suffix, mult in multipliers.items():
            if limit.endswith(suffix):
                try:
                    return int(float(limit[:-1]) * mult)
                except ValueError:
                    break

        # Default to 2GB
        return 2 * 1024 ** 3

    @property
    def active_mode(self) -> SandboxMode:
        """Get the currently active sandbox mode."""
        return self._active_mode

    @property
    def is_docker_mode(self) -> bool:
        """Check if running in Docker mode."""
        return self._active_mode == SandboxMode.DOCKER and self._capsule is not None

    async def get_status(self) -> dict:
        """Get sandbox status."""
        status = {
            "mode": self._active_mode.value,
            "policy_mode": self.policy.mode.value,
            "workspace": str(self.workspace_path),
            "docker_available": self._docker_available,
            "capsule_running": self._capsule is not None,
        }

        if self._capsule:
            status["container_id"] = self._capsule.container_id

        return status
