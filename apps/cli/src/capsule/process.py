"""Process-based capsule for local tool execution."""

from __future__ import annotations

import asyncio
import logging
import os
import resource
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ProcessCapsuleConfig:
    """Configuration for process capsule."""

    # Resource limits
    max_memory_mb: int = 512
    max_cpu_time_seconds: int = 300
    max_open_files: int = 1024
    max_processes: int = 64

    # Execution
    default_timeout_seconds: float = 120.0

    # Environment filtering
    blocked_env_patterns: list[str] = field(default_factory=lambda: [
        "AWS_",
        "GOOGLE_",
        "AZURE_",
        "ANTHROPIC_",
        "OPENAI_",
        "*_API_KEY",
        "*_SECRET*",
        "*_TOKEN*",
        "*_PASSWORD*",
    ])


class ProcessCapsule:
    """Process-based capsule for executing tools.

    Provides basic isolation via:
    - Restricted working directory
    - Filtered environment variables
    - Resource limits (soft)
    - Temporary file isolation
    """

    def __init__(
        self,
        session_id: str,
        workspace_path: str,
        config: Optional[ProcessCapsuleConfig] = None
    ):
        self.session_id = session_id
        self.workspace_path = workspace_path
        self.config = config or ProcessCapsuleConfig()

        self._temp_dir: Optional[Path] = None
        self._started = False

    async def start(self):
        """Initialize the capsule."""
        if self._started:
            return

        # Create temp directory for this session
        self._temp_dir = Path(tempfile.mkdtemp(prefix=f"glock_{self.session_id}_"))

        # Create subdirectories
        (self._temp_dir / "cache").mkdir()
        (self._temp_dir / "tmp").mkdir()

        self._started = True
        logger.info(f"Process capsule started: {self.session_id}")

    async def stop(self):
        """Clean up the capsule."""
        if not self._started:
            return

        # Clean up temp directory
        if self._temp_dir and self._temp_dir.exists():
            import shutil
            try:
                shutil.rmtree(self._temp_dir)
            except Exception as e:
                logger.warning(f"Failed to clean up temp dir: {e}")

        self._started = False
        logger.info(f"Process capsule stopped: {self.session_id}")

    async def execute(
        self,
        command: str,
        timeout: Optional[float] = None,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None
    ) -> tuple[int, str, str]:
        """Execute a command in the capsule.

        Args:
            command: Shell command to execute
            timeout: Timeout in seconds (default from config)
            env: Additional environment variables
            cwd: Working directory (default workspace_path)

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        if not self._started:
            await self.start()

        timeout = timeout or self.config.default_timeout_seconds
        working_dir = cwd or self.workspace_path

        # Build environment
        capsule_env = self._build_environment(env)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=working_dir,
                env=capsule_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=self._set_resource_limits
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                return process.returncode or 0, stdout, stderr

            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return -1, "", f"Command timed out after {timeout}s"

        except Exception as e:
            logger.error(f"Command execution failed: {e}")
            return -1, "", str(e)

    def _build_environment(self, extra_env: Optional[dict[str, str]] = None) -> dict[str, str]:
        """Build filtered environment for the subprocess."""
        env = {}

        # Start with safe base environment
        safe_vars = [
            "PATH", "HOME", "USER", "SHELL", "TERM",
            "LANG", "LC_ALL", "TZ",
            "EDITOR", "VISUAL", "PAGER",
            "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
        ]

        for var in safe_vars:
            if var in os.environ:
                env[var] = os.environ[var]

        # Add capsule-specific variables
        env["GLOCK_SESSION_ID"] = self.session_id
        env["GLOCK_CAPSULE_MODE"] = "process"

        if self._temp_dir:
            env["TMPDIR"] = str(self._temp_dir / "tmp")
            env["GLOCK_CACHE_DIR"] = str(self._temp_dir / "cache")

        # Add extra env (already filtered by caller)
        if extra_env:
            env.update(extra_env)

        return env

    def _set_resource_limits(self):
        """Set resource limits for subprocess (called in child before exec)."""
        try:
            # Memory limit (soft)
            memory_bytes = self.config.max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))

            # CPU time limit
            cpu_time = self.config.max_cpu_time_seconds
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_time, cpu_time))

            # Open files limit
            resource.setrlimit(
                resource.RLIMIT_NOFILE,
                (self.config.max_open_files, self.config.max_open_files)
            )

            # Process limit
            resource.setrlimit(
                resource.RLIMIT_NPROC,
                (self.config.max_processes, self.config.max_processes)
            )

        except Exception as e:
            # Log but don't fail - limits may not be available
            logger.debug(f"Could not set resource limits: {e}")

    def get_temp_path(self, subpath: str = "") -> Path:
        """Get a path within the capsule's temp directory."""
        if not self._temp_dir:
            raise RuntimeError("Capsule not started")
        return self._temp_dir / subpath
