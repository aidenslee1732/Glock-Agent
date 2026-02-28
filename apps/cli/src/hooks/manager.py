"""Hook manager - coordinates hook execution."""

from __future__ import annotations

import logging
from typing import Any, Optional

from .config import HookConfig, HookDefinition, HookResult, HookType
from .executor import HookExecutor

logger = logging.getLogger(__name__)


class HookManager:
    """Manages and executes hooks throughout the application lifecycle.

    Hook types:
    - user-prompt-submit: Before processing user input
    - pre-tool-execute: Before any tool runs
    - post-tool-execute: After any tool runs
    - pre-commit: Before git commit
    - post-commit: After git commit
    - session-start: When session starts
    - session-end: When session ends
    - plan-approved: When a plan is approved
    - plan-rejected: When a plan is rejected
    """

    def __init__(
        self,
        config: Optional[HookConfig] = None,
        executor: Optional[HookExecutor] = None,
        workspace_dir: Optional[str] = None,
    ):
        """Initialize the hook manager.

        Args:
            config: HookConfig instance
            executor: HookExecutor instance
            workspace_dir: Workspace directory
        """
        self.config = config or HookConfig()
        self.executor = executor or HookExecutor(workspace_dir)
        self._enabled = True

    def enable(self) -> None:
        """Enable hook execution."""
        self._enabled = True

    def disable(self) -> None:
        """Disable hook execution."""
        self._enabled = False

    @property
    def is_enabled(self) -> bool:
        """Check if hooks are enabled."""
        return self._enabled

    async def run_hooks(
        self,
        hook_type: HookType,
        context: Optional[dict[str, Any]] = None,
    ) -> list[HookResult]:
        """Run all hooks of a given type.

        Args:
            hook_type: Type of hooks to run
            context: Context variables to pass to hooks

        Returns:
            List of hook results
        """
        if not self._enabled:
            return []

        hooks = self.config.get_hooks(hook_type)
        if not hooks:
            return []

        logger.debug(f"Running {len(hooks)} hooks for {hook_type.value}")

        results = await self.executor.execute_all(
            hooks, hook_type, context, stop_on_block=True
        )

        # Log results
        for result in results:
            if result.blocked:
                logger.warning(
                    f"Hook blocked: {result.command} (exit code: {result.exit_code})"
                )
            elif not result.success:
                logger.debug(
                    f"Hook failed: {result.command} (exit code: {result.exit_code})"
                )

        return results

    async def on_user_prompt(self, prompt: str) -> tuple[bool, Optional[str]]:
        """Run user-prompt-submit hooks.

        Args:
            prompt: User's input prompt

        Returns:
            Tuple of (allowed, block_message)
        """
        results = await self.run_hooks(
            HookType.USER_PROMPT_SUBMIT,
            context={"prompt": prompt},
        )

        if self.executor.any_blocked(results):
            return False, self.executor.get_block_message(results)

        return True, None

    async def on_pre_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> tuple[bool, Optional[str]]:
        """Run pre-tool-execute hooks.

        Args:
            tool_name: Name of tool being executed
            args: Tool arguments

        Returns:
            Tuple of (allowed, block_message)
        """
        results = await self.run_hooks(
            HookType.PRE_TOOL_EXECUTE,
            context={
                "tool_name": tool_name,
                "tool_args": str(args),
            },
        )

        if self.executor.any_blocked(results):
            return False, self.executor.get_block_message(results)

        return True, None

    async def on_post_tool(
        self,
        tool_name: str,
        result: dict[str, Any],
        args: Optional[dict[str, Any]] = None,
        success: bool = True,
    ) -> list[HookResult]:
        """Run post-tool-execute hooks.

        Args:
            tool_name: Name of tool that was executed
            result: Tool result
            args: Tool arguments (optional)
            success: Whether tool succeeded (optional)

        Returns:
            List of hook results
        """
        return await self.run_hooks(
            HookType.POST_TOOL_EXECUTE,
            context={
                "tool_name": tool_name,
                "tool_result": str(result),
                "tool_args": str(args) if args else "",
                "tool_success": str(success),
            },
        )

    async def on_pre_commit(
        self,
        message: str,
        files: list[str],
    ) -> tuple[bool, Optional[str]]:
        """Run pre-commit hooks.

        Args:
            message: Commit message
            files: Files being committed

        Returns:
            Tuple of (allowed, block_message)
        """
        results = await self.run_hooks(
            HookType.PRE_COMMIT,
            context={
                "commit_message": message,
                "commit_files": ",".join(files),
            },
        )

        if self.executor.any_blocked(results):
            return False, self.executor.get_block_message(results)

        return True, None

    async def on_post_commit(
        self,
        commit_hash: str,
        message: str,
    ) -> list[HookResult]:
        """Run post-commit hooks.

        Args:
            commit_hash: The commit hash
            message: Commit message

        Returns:
            List of hook results
        """
        return await self.run_hooks(
            HookType.POST_COMMIT,
            context={
                "commit_hash": commit_hash,
                "commit_message": message,
            },
        )

    async def on_session_start(self, session_id: str) -> list[HookResult]:
        """Run session-start hooks.

        Args:
            session_id: Session ID

        Returns:
            List of hook results
        """
        return await self.run_hooks(
            HookType.SESSION_START,
            context={"session_id": session_id},
        )

    async def on_session_end(self, session_id: str) -> list[HookResult]:
        """Run session-end hooks.

        Args:
            session_id: Session ID

        Returns:
            List of hook results
        """
        return await self.run_hooks(
            HookType.SESSION_END,
            context={"session_id": session_id},
        )

    async def on_plan_approved(self, plan_id: str) -> list[HookResult]:
        """Run plan-approved hooks.

        Args:
            plan_id: Plan ID

        Returns:
            List of hook results
        """
        return await self.run_hooks(
            HookType.PLAN_APPROVED,
            context={"plan_id": plan_id},
        )

    async def on_plan_rejected(
        self,
        plan_id: str,
        reason: str = "",
    ) -> list[HookResult]:
        """Run plan-rejected hooks.

        Args:
            plan_id: Plan ID
            reason: Rejection reason

        Returns:
            List of hook results
        """
        return await self.run_hooks(
            HookType.PLAN_REJECTED,
            context={
                "plan_id": plan_id,
                "rejection_reason": reason,
            },
        )

    def add_hook(
        self,
        hook_type: HookType,
        command: str,
        block_on_failure: bool = False,
        timeout: int = 5000,
    ) -> None:
        """Add a hook.

        Args:
            hook_type: Type of hook
            command: Command to execute
            block_on_failure: Whether to block on failure
            timeout: Timeout in milliseconds
        """
        hook = HookDefinition(
            command=command,
            timeout=timeout,
            block_on_failure=block_on_failure,
        )
        self.config.add_hook(hook_type, hook)

    def remove_hook(self, hook_type: HookType, index: int) -> bool:
        """Remove a hook.

        Args:
            hook_type: Type of hook
            index: Index of hook to remove

        Returns:
            True if removed
        """
        return self.config.remove_hook(hook_type, index)

    def list_hooks(self) -> dict[str, list[dict]]:
        """List all configured hooks.

        Returns:
            Dictionary of hook configurations
        """
        return self.config.list_all_hooks()
