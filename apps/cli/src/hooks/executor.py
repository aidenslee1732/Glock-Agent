"""Hook execution engine."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
from typing import Any, Optional

from .config import HookDefinition, HookResult, HookType

logger = logging.getLogger(__name__)


class CommandInjectionError(Exception):
    """Raised when a potential command injection is detected."""
    pass


class HookExecutor:
    """Executes hooks with timeout and error handling."""

    def __init__(self, workspace_dir: Optional[str] = None):
        """Initialize the executor.

        Args:
            workspace_dir: Default working directory for hooks
        """
        self.workspace_dir = workspace_dir or os.getcwd()

    def _sanitize_shell_value(self, value: str) -> str:
        """Sanitize a value for safe shell interpolation.

        Uses shlex.quote to properly escape special characters,
        preventing command injection attacks.

        Args:
            value: The raw value to sanitize

        Returns:
            Shell-safe quoted string

        Raises:
            CommandInjectionError: If value contains suspicious patterns
        """
        if not value:
            return "''"

        # Detect obvious injection attempts and raise error
        # These patterns should never appear in legitimate input
        dangerous_patterns = [
            r';\s*rm\s+',           # ; rm
            r'\|\s*rm\s+',          # | rm
            r'`[^`]*`',             # backtick command substitution
            r'\$\([^)]*\)',         # $() command substitution
            r'>\s*/etc/',           # redirect to /etc
            r'>\s*/dev/',           # redirect to /dev
            r'\.\./\.\.',           # excessive path traversal
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, value, re.IGNORECASE):
                raise CommandInjectionError(
                    f"Potential command injection detected in value: {value[:50]}..."
                )

        # Use shlex.quote for proper shell escaping
        # This wraps the value in single quotes and escapes any internal single quotes
        return shlex.quote(value)

    async def execute(
        self,
        hook: HookDefinition,
        hook_type: HookType,
        context: Optional[dict[str, Any]] = None,
    ) -> HookResult:
        """Execute a single hook.

        Args:
            hook: Hook definition to execute
            hook_type: Type of hook being executed
            context: Context variables to pass to the hook

        Returns:
            HookResult with execution details
        """
        context = context or {}

        # Build environment
        env = {**os.environ}

        # Add context variables as environment variables
        for key, value in context.items():
            env_key = f"GLOCK_{key.upper()}"
            env[env_key] = str(value)

        # Add hook-specific env vars
        env.update(hook.env)

        # Determine working directory
        cwd = hook.working_dir or self.workspace_dir

        # Build command with SAFE variable substitution
        command = hook.command

        # Securely substitute variables using shell-safe quoting
        # Instead of direct interpolation, we pass values via environment variables
        # and only substitute with properly escaped values for display/logging
        substitutions = {
            "$PROMPT": context.get("prompt", ""),
            "$TOOL_NAME": context.get("tool_name", ""),
            "$FILE_PATH": context.get("file_path", ""),
        }

        # Validate and sanitize each substitution value
        for var_name, value in substitutions.items():
            if var_name in command:
                # Sanitize the value to prevent command injection
                sanitized_value = self._sanitize_shell_value(value)
                command = command.replace(var_name, sanitized_value)

        start_time = time.time()

        try:
            # Execute command
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=env,
            )

            # Wait with timeout
            timeout_sec = hook.timeout / 1000
            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_sec,
                )
                output = stdout.decode(errors="replace")
                exit_code = process.returncode
                success = exit_code == 0

            except asyncio.TimeoutError:
                process.kill()
                output = f"Hook timed out after {hook.timeout}ms"
                exit_code = -1
                success = False

            duration_ms = int((time.time() - start_time) * 1000)

            return HookResult(
                hook_type=hook_type,
                command=command,
                success=success,
                output=output,
                exit_code=exit_code,
                blocked=not success and hook.block_on_failure,
                duration_ms=duration_ms,
            )

        except CommandInjectionError as e:
            # Security errors: store in database and show friendly message
            logger.error(f"Command injection attempt blocked: {e}")
            from ..errors import report_error, ErrorContext, GlockClientError
            report_error(
                e,
                component="hooks.executor",
                context=ErrorContext(
                    component="hooks.executor",
                    additional={"command": hook.command[:100]},  # Don't log full command for security
                ),
                reraise=False,
            )
            raise GlockClientError(
                f"Security error in hook execution: {e}",
                original_error=e,
                severity="critical",
                context=ErrorContext(component="hooks.executor"),
            ) from e

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.exception(f"Hook execution failed: {hook.command}")

            return HookResult(
                hook_type=hook_type,
                command=command,
                success=False,
                output="",
                exit_code=-1,
                error=str(e),
                blocked=hook.block_on_failure,
                duration_ms=duration_ms,
            )

    async def execute_all(
        self,
        hooks: list[HookDefinition],
        hook_type: HookType,
        context: Optional[dict[str, Any]] = None,
        stop_on_block: bool = True,
    ) -> list[HookResult]:
        """Execute multiple hooks in sequence.

        Args:
            hooks: List of hooks to execute
            hook_type: Type of hooks being executed
            context: Context variables
            stop_on_block: Stop execution if a hook blocks

        Returns:
            List of results for each hook
        """
        results = []

        for hook in hooks:
            result = await self.execute(hook, hook_type, context)
            results.append(result)

            if result.blocked and stop_on_block:
                logger.warning(f"Hook blocked action: {hook.command}")
                break

        return results

    def any_blocked(self, results: list[HookResult]) -> bool:
        """Check if any result blocked the action.

        Args:
            results: List of hook results

        Returns:
            True if any result has blocked=True
        """
        return any(r.blocked for r in results)

    def get_block_message(self, results: list[HookResult]) -> Optional[str]:
        """Get the blocking message if any hook blocked.

        Args:
            results: List of hook results

        Returns:
            Block message or None
        """
        for result in results:
            if result.blocked:
                return f"Blocked by hook: {result.command}\n{result.output}"
        return None
