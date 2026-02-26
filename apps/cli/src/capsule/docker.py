"""Docker-based capsule for isolated tool execution."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DockerCapsuleConfig:
    """Configuration for Docker capsule."""

    # Image
    image: str = "python:3.12-slim"
    pull_policy: str = "if-not-present"

    # Resources
    memory_limit: str = "2g"
    cpu_limit: float = 1.0
    pids_limit: int = 256

    # Security
    privileged: bool = False
    cap_drop: list[str] = field(default_factory=lambda: ["ALL"])
    cap_add: list[str] = field(default_factory=lambda: ["CHOWN", "SETUID", "SETGID"])
    security_opt: list[str] = field(default_factory=lambda: ["no-new-privileges:true"])
    read_only: bool = True

    # Network
    network_mode: str = "none"  # No network by default

    # Timeouts
    default_timeout_seconds: float = 120.0
    container_start_timeout: float = 30.0

    # Paths that must never be mounted
    forbidden_mounts: list[str] = field(default_factory=lambda: [
        "/var/run/docker.sock",
        "/dev",
        "~/.aws",
        "~/.gcloud",
        "~/.kube",
        "~/.docker",
        "/etc/passwd",
        "/etc/shadow",
        "/etc/sudoers",
    ])


class DockerCapsule:
    """Docker-based capsule for executing tools in isolation.

    Provides strong isolation via:
    - Container isolation
    - Limited capabilities
    - No network access (by default)
    - Read-only root filesystem
    - Resource limits
    - No privileged mode
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: str,
        config: Optional[DockerCapsuleConfig] = None
    ):
        self.session_id = session_id
        self.workspace_path = workspace_path
        self.config = config or DockerCapsuleConfig()

        self._container_id: Optional[str] = None
        self._container_name = f"glock_{session_id}_{secrets.token_hex(4)}"
        self._started = False

    async def start(self):
        """Start the Docker container."""
        if self._started:
            return

        # Verify Docker is available
        result = await self._run_docker_command(["docker", "info"])
        if result[0] != 0:
            raise RuntimeError("Docker is not available")

        # Pull image if needed
        if self.config.pull_policy == "always":
            await self._run_docker_command(["docker", "pull", self.config.image])

        # Build docker run command
        cmd = self._build_run_command()

        # Start container
        logger.info(f"Starting Docker capsule: {self._container_name}")
        exit_code, stdout, stderr = await self._run_docker_command(cmd)

        if exit_code != 0:
            raise RuntimeError(f"Failed to start container: {stderr}")

        self._container_id = stdout.strip()
        self._started = True

        logger.info(f"Docker capsule started: {self._container_id[:12]}")

    async def stop(self):
        """Stop and remove the Docker container."""
        if not self._started or not self._container_id:
            return

        logger.info(f"Stopping Docker capsule: {self._container_id[:12]}")

        # Stop container
        await self._run_docker_command(
            ["docker", "stop", "-t", "5", self._container_id]
        )

        # Remove container
        await self._run_docker_command(
            ["docker", "rm", "-f", self._container_id]
        )

        self._container_id = None
        self._started = False

    async def execute(
        self,
        command: str,
        timeout: Optional[float] = None,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None
    ) -> tuple[int, str, str]:
        """Execute a command in the container.

        Args:
            command: Shell command to execute
            timeout: Timeout in seconds
            env: Additional environment variables
            cwd: Working directory within container

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        if not self._started:
            await self.start()

        timeout = timeout or self.config.default_timeout_seconds
        working_dir = cwd or "/workspace"

        # Build docker exec command
        exec_cmd = ["docker", "exec"]

        # Add environment variables
        if env:
            for key, value in env.items():
                exec_cmd.extend(["-e", f"{key}={value}"])

        # Add working directory
        exec_cmd.extend(["-w", working_dir])

        # Add container and command
        exec_cmd.append(self._container_id)
        exec_cmd.extend(["sh", "-c", command])

        try:
            exit_code, stdout, stderr = await asyncio.wait_for(
                self._run_docker_command(exec_cmd),
                timeout=timeout
            )
            return exit_code, stdout, stderr

        except asyncio.TimeoutError:
            logger.warning(f"Command timed out in container: {command[:50]}")
            return -1, "", f"Command timed out after {timeout}s"

    def _build_run_command(self) -> list[str]:
        """Build the docker run command."""
        cmd = [
            "docker", "run",
            "--detach",
            "--name", self._container_name,
            "--hostname", "glock",
        ]

        # Resource limits
        cmd.extend(["--memory", self.config.memory_limit])
        cmd.extend(["--cpus", str(self.config.cpu_limit)])
        cmd.extend(["--pids-limit", str(self.config.pids_limit)])

        # Security options
        if not self.config.privileged:
            for cap in self.config.cap_drop:
                cmd.extend(["--cap-drop", cap])
            for cap in self.config.cap_add:
                cmd.extend(["--cap-add", cap])

        for opt in self.config.security_opt:
            cmd.extend(["--security-opt", opt])

        if self.config.read_only:
            cmd.append("--read-only")
            # Add tmpfs for writable areas
            cmd.extend(["--tmpfs", "/tmp:size=256M,mode=1777"])
            cmd.extend(["--tmpfs", "/home/glock:size=512M,mode=755"])

        # Network
        cmd.extend(["--network", self.config.network_mode])

        # Mount workspace
        workspace_abs = os.path.abspath(self.workspace_path)
        cmd.extend(["-v", f"{workspace_abs}:/workspace:rw"])

        # Environment variables
        cmd.extend(["-e", f"GLOCK_SESSION_ID={self.session_id}"])
        cmd.extend(["-e", "GLOCK_CAPSULE_MODE=docker"])

        # Image and command (keep container running)
        cmd.append(self.config.image)
        cmd.extend(["tail", "-f", "/dev/null"])

        return cmd

    async def _run_docker_command(
        self,
        cmd: list[str]
    ) -> tuple[int, str, str]:
        """Run a docker command."""
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            return process.returncode or 0, stdout, stderr

        except Exception as e:
            return -1, "", str(e)

    @property
    def container_id(self) -> Optional[str]:
        """Get the container ID if running."""
        return self._container_id

    async def get_logs(self, tail: int = 100) -> str:
        """Get container logs."""
        if not self._container_id:
            return ""

        exit_code, stdout, _ = await self._run_docker_command([
            "docker", "logs", "--tail", str(tail), self._container_id
        ])

        return stdout if exit_code == 0 else ""
