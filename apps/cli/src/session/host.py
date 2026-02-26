"""Session host - manages the client-side session.

Model B: Client-orchestrated architecture.
The session host now drives the entire task loop locally using the OrchestrationEngine.
Server is a pure LLM proxy - no runtime processes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from packages.shared_protocol.types import (
    MessageEnvelope,
    MessageType,
    CompiledPlan,
    ToolRequestPayload,
    LLMDeltaPayload,
    LLMResponseEndPayload,
    LLMErrorPayload,
    ContextCheckpointAckPayload,
    SessionSyncPayload,
)
from apps.cli.src.transport.ws_client import (
    WebSocketClient,
    ConnectionConfig,
    ConnectionState,
)
from apps.cli.src.tools.broker import ToolBroker
from apps.cli.src.orchestrator.engine import (
    OrchestrationEngine,
    OrchestrationConfig,
    OrchestrationEvent,
)
from apps.cli.src.context.packer import ContextPacker, PackerConfig
from apps.cli.src.crypto.session_keys import SessionKeyManager

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    """Session state."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    TASK_RUNNING = "task_running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"  # Model B: Session paused (can resume)


@dataclass
class TaskInfo:
    """Information about the current task."""
    task_id: str
    prompt: str
    status: str = "running"
    plan: Optional[CompiledPlan] = None
    tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    context_ref: Optional[str] = None  # Model B: Last checkpoint reference


@dataclass
class SessionConfig:
    """Session configuration."""
    server_url: str = "wss://gateway.glock.dev"
    workspace_dir: str = ""
    auth_token: Optional[str] = None
    model_tier: str = "standard"  # Model B: fast/standard/advanced
    max_turns: int = 50
    max_tool_calls_per_turn: int = 30


class SessionHost:
    """Manages the client-side session.

    Model B Architecture:
    - Client does ALL orchestration locally
    - Server is pure LLM proxy
    - Context checkpoints stored server-side (encrypted)
    - No runtime processes

    Responsibilities:
    - WebSocket connection to gateway
    - Full orchestration via OrchestrationEngine
    - Local tool execution
    - Context packing and checkpoint management
    - Session encryption
    """

    def __init__(
        self,
        server_url: str,
        workspace_dir: str,
        auth_token: Optional[str] = None,
        model_tier: str = "standard",
    ):
        self.config = SessionConfig(
            server_url=server_url,
            workspace_dir=workspace_dir or os.getcwd(),
            auth_token=auth_token,
            model_tier=model_tier,
        )

        self.state = SessionState.DISCONNECTED
        self._session_id: Optional[str] = None
        self._current_task: Optional[TaskInfo] = None
        self._context_ref: Optional[str] = None  # Model B: Last checkpoint

        # WebSocket client
        self._ws = WebSocketClient(ConnectionConfig(
            server_url=server_url,
            auth_token=auth_token,
        ))

        # Tool broker
        self._tools = ToolBroker(workspace_dir=self.config.workspace_dir)

        # Model B: Context packer for token minimization
        self._context_packer = ContextPacker(
            workspace_dir=self.config.workspace_dir,
            config=PackerConfig(),
        )

        # Model B: Session key manager for encryption
        self._key_manager: Optional[SessionKeyManager] = None

        # Model B: Orchestration engine (initialized after connect)
        self._orchestrator: Optional[OrchestrationEngine] = None

        # Event callbacks
        self._on_delta: Optional[Callable[[str, str], None]] = None
        self._on_tool_request: Optional[Callable[[dict], None]] = None
        self._on_task_complete: Optional[Callable[[dict], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None
        self._on_tool_start: Optional[Callable[[str, dict], None]] = None
        self._on_tool_end: Optional[Callable[[str, dict], None]] = None

        # Register message handlers
        self._setup_handlers()
        self._setup_model_b_handlers()

    @property
    def session_id(self) -> Optional[str]:
        """Get session ID."""
        return self._session_id

    @property
    def current_task(self) -> Optional[TaskInfo]:
        """Get current task info."""
        return self._current_task

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self.state in (SessionState.CONNECTED, SessionState.TASK_RUNNING)

    def on_delta(self, callback: Callable[[str, str], None]) -> None:
        """Register callback for task deltas.

        Args:
            callback: Function(delta_type, content)
        """
        self._on_delta = callback

    def on_tool_request(self, callback: Callable[[dict], None]) -> None:
        """Register callback for tool requests.

        Args:
            callback: Function(tool_request)
        """
        self._on_tool_request = callback

    def on_task_complete(self, callback: Callable[[dict], None]) -> None:
        """Register callback for task completion.

        Args:
            callback: Function(result)
        """
        self._on_task_complete = callback

    def on_error(self, callback: Callable[[str], None]) -> None:
        """Register callback for errors.

        Args:
            callback: Function(error_message)
        """
        self._on_error = callback

    def on_tool_start(self, callback: Callable[[str, dict], None]) -> None:
        """Register callback for tool execution start.

        Args:
            callback: Function(tool_name, args)
        """
        self._on_tool_start = callback

    def on_tool_end(self, callback: Callable[[str, dict], None]) -> None:
        """Register callback for tool execution end.

        Args:
            callback: Function(tool_name, result)
        """
        self._on_tool_end = callback

    async def connect(self) -> str:
        """Connect to gateway and start session.

        Returns:
            Session ID
        """
        self.state = SessionState.CONNECTING

        workspace_label = Path(self.config.workspace_dir).name
        self._session_id = await self._ws.connect(workspace_label)

        # Model B: Initialize session key manager
        if self.config.auth_token:
            self._key_manager = SessionKeyManager(
                master_token=self.config.auth_token,
                session_id=self._session_id,
            )

        # Model B: Initialize orchestration engine
        self._orchestrator = OrchestrationEngine(
            ws_client=self._ws,
            tool_broker=self._tools,
            context_packer=self._context_packer,
            key_manager=self._key_manager,
            config=OrchestrationConfig(
                model_tier=self.config.model_tier,
                max_turns=self.config.max_turns,
                max_tool_calls_per_turn=self.config.max_tool_calls_per_turn,
            ),
        )

        self.state = SessionState.CONNECTED
        return self._session_id

    async def resume(self, session_id: str, context_ref: Optional[str] = None) -> None:
        """Resume a previous session.

        Model B: Uses SESSION_RESUME for checkpoint-based resume.

        Args:
            session_id: Session ID to resume
            context_ref: Last known context reference (checkpoint)
        """
        self.state = SessionState.CONNECTING
        self._session_id = session_id
        self._context_ref = context_ref

        # Model B: Send session resume with checkpoint info
        await self._ws.connect("")  # Connect first
        await self._ws.send_session_resume(
            session_id=session_id,
            client_state_hash=self._compute_state_hash(),
            expected_context_ref=context_ref,
        )

        # Initialize key manager
        if self.config.auth_token:
            self._key_manager = SessionKeyManager(
                master_token=self.config.auth_token,
                session_id=session_id,
            )

        # Initialize orchestration engine
        self._orchestrator = OrchestrationEngine(
            ws_client=self._ws,
            tool_broker=self._tools,
            context_packer=self._context_packer,
            key_manager=self._key_manager,
            config=OrchestrationConfig(
                model_tier=self.config.model_tier,
                max_turns=self.config.max_turns,
                max_tool_calls_per_turn=self.config.max_tool_calls_per_turn,
            ),
        )

        self.state = SessionState.CONNECTED

    def _compute_state_hash(self) -> str:
        """Compute hash of local state for resume verification."""
        import hashlib
        import json

        state_data = {
            "workspace_dir": self.config.workspace_dir,
            "context_ref": self._context_ref,
            "packer_state": self._context_packer.get_budget_summary(),
        }
        state_json = json.dumps(state_data, sort_keys=True)
        return hashlib.sha256(state_json.encode()).hexdigest()[:16]

    async def disconnect(self) -> None:
        """Disconnect from gateway."""
        await self._ws.disconnect()
        self.state = SessionState.DISCONNECTED

    async def submit_task(self, prompt: str) -> str:
        """Submit a new task.

        Model B: This now drives the full orchestration loop locally.

        Args:
            prompt: Task description

        Returns:
            Task ID
        """
        if self.state != SessionState.CONNECTED:
            raise RuntimeError("Not connected")

        if not self._orchestrator:
            raise RuntimeError("Orchestrator not initialized")

        # Build context
        context = self._build_context()

        # Create task info
        from packages.shared_protocol.types import generate_request_id
        task_id = f"task_{generate_request_id()}"

        self._current_task = TaskInfo(
            task_id=task_id,
            prompt=prompt,
            status="running",
            context_ref=self._context_ref,
        )

        # Send task_start to server for tracking/metering
        await self._ws.send(
            MessageType.TASK_START,
            {
                "task_id": task_id,
                "prompt": prompt,
                "context": context,
            },
        )

        self.state = SessionState.TASK_RUNNING

        return task_id

    async def run_task(self, prompt: str) -> AsyncIterator[OrchestrationEvent]:
        """Run a task with the orchestration engine.

        Model B: Full client-side orchestration.

        Args:
            prompt: Task description

        Yields:
            OrchestrationEvent for each step
        """
        if self.state not in (SessionState.CONNECTED, SessionState.TASK_RUNNING):
            raise RuntimeError("Not connected")

        if not self._orchestrator:
            raise RuntimeError("Orchestrator not initialized")

        # Submit the task first
        task_id = await self.submit_task(prompt)

        # Set task description in context packer
        self._context_packer.set_task(prompt)

        # Run orchestration loop
        try:
            async for event in self._orchestrator.run_task(prompt, self._context_ref):
                # Update task info based on events
                if event.type == "llm_response":
                    if self._current_task:
                        self._current_task.input_tokens += event.data.get("input_tokens", 0)
                        self._current_task.output_tokens += event.data.get("output_tokens", 0)
                        self._current_task.tokens_used = (
                            self._current_task.input_tokens + self._current_task.output_tokens
                        )

                elif event.type == "checkpoint":
                    self._context_ref = event.data.get("checkpoint_id")
                    if self._current_task:
                        self._current_task.context_ref = self._context_ref

                elif event.type == "tool_start":
                    if self._on_tool_start:
                        self._on_tool_start(event.data.get("tool_name"), event.data.get("args"))

                elif event.type == "tool_end":
                    if self._on_tool_end:
                        self._on_tool_end(event.data.get("tool_name"), event.data.get("result"))

                elif event.type == "text_delta":
                    if self._on_delta:
                        self._on_delta("text", event.data.get("content", ""))

                elif event.type == "thinking":
                    if self._on_delta:
                        self._on_delta("thinking", event.data.get("content", ""))

                elif event.type == "error":
                    if self._on_error:
                        self._on_error(event.data.get("message", "Unknown error"))

                elif event.type == "task_complete":
                    self.state = SessionState.CONNECTED
                    if self._current_task:
                        self._current_task.status = "completed"
                    if self._on_task_complete:
                        self._on_task_complete(event.data)

                yield event

        except Exception as e:
            self.state = SessionState.CONNECTED
            if self._current_task:
                self._current_task.status = "failed"
            if self._on_error:
                self._on_error(str(e))
            raise

    async def cancel_task(self) -> None:
        """Cancel the current task."""
        if not self._current_task:
            return

        # Model B: Cancel any pending LLM request
        if self._orchestrator:
            await self._orchestrator.cancel()

        await self._ws.send(
            MessageType.CANCEL_REQUESTED,
            {"task_id": self._current_task.task_id},
            task_id=self._current_task.task_id,
        )

        self.state = SessionState.CONNECTED
        self._current_task.status = "cancelled"

    async def request_plan(self, prompt: str) -> Optional[CompiledPlan]:
        """Request a compiled plan from the server.

        Model B: Server returns plan directly (no runtime dispatch).

        Args:
            prompt: Task description

        Returns:
            Compiled plan or None
        """
        context = self._build_context()

        # Send plan request
        await self._ws.send(
            MessageType.TASK_START,
            {
                "prompt": prompt,
                "context": context,
                "plan_only": True,  # Just get the plan, don't start execution
            },
        )

        # Wait for plan response (handled by COMPILED_PLAN handler)
        # The handler will set self._current_task.plan
        # For now, return None - TUI can check task.plan after receiving COMPILED_PLAN
        return None

    def get_token_usage(self) -> dict[str, int]:
        """Get current token usage for the task.

        Returns:
            Dict with input_tokens, output_tokens, total
        """
        if not self._current_task:
            return {"input_tokens": 0, "output_tokens": 0, "total": 0}

        return {
            "input_tokens": self._current_task.input_tokens,
            "output_tokens": self._current_task.output_tokens,
            "total": self._current_task.tokens_used,
        }

    def get_context_ref(self) -> Optional[str]:
        """Get the current context checkpoint reference."""
        return self._context_ref

    async def approve_tool(self, approval_id: str, approved: bool) -> None:
        """Respond to tool approval request.

        Args:
            approval_id: Approval request ID
            approved: Whether to approve
        """
        await self._ws.send(
            MessageType.TOOL_APPROVAL_RESPONSE,
            {
                "approval_id": approval_id,
                "approved": approved,
            },
        )

    def _setup_handlers(self) -> None:
        """Set up message handlers."""

        @self._ws.on_message(MessageType.TASK_STATUS)
        async def handle_task_status(msg: MessageEnvelope):
            task_id = msg.payload.get("task_id")
            status = msg.payload.get("status")

            if not self._current_task:
                self._current_task = TaskInfo(
                    task_id=task_id,
                    prompt="",
                    status=status,
                )
            else:
                self._current_task.status = status

        @self._ws.on_message(MessageType.TASK_DELTA)
        async def handle_task_delta(msg: MessageEnvelope):
            delta_type = msg.payload.get("delta_type", "text")
            content = msg.payload.get("content", "")

            if self._current_task:
                self._current_task.tokens_used += msg.payload.get("tokens_used", 0)

            if self._on_delta:
                self._on_delta(delta_type, content)

        @self._ws.on_message(MessageType.COMPILED_PLAN)
        async def handle_plan(msg: MessageEnvelope):
            # Store plan for verification
            if self._current_task:
                self._current_task.plan = msg.payload

        @self._ws.on_message(MessageType.TOOL_REQUEST)
        async def handle_tool_request(msg: MessageEnvelope):
            tool_id = msg.payload.get("tool_id")
            tool_name = msg.payload.get("tool_name")
            args = msg.payload.get("args", {})
            requires_approval = msg.payload.get("requires_approval", False)

            if requires_approval:
                # Notify UI for approval
                self.state = SessionState.WAITING_APPROVAL
                if self._on_tool_request:
                    self._on_tool_request(msg.payload)
            else:
                # Execute tool
                result = await self._execute_tool(tool_id, tool_name, args)

                # Send result
                await self._ws.send(
                    MessageType.TOOL_RESULT,
                    result,
                    task_id=msg.task_id,
                )

        @self._ws.on_message(MessageType.TOOL_APPROVAL_REQUEST)
        async def handle_approval_request(msg: MessageEnvelope):
            self.state = SessionState.WAITING_APPROVAL
            if self._on_tool_request:
                self._on_tool_request(msg.payload)

        @self._ws.on_message(MessageType.TASK_COMPLETE)
        async def handle_task_complete(msg: MessageEnvelope):
            self.state = SessionState.CONNECTED
            self._current_task = None

            if self._on_task_complete:
                self._on_task_complete(msg.payload)

        @self._ws.on_message(MessageType.TASK_FAILED)
        async def handle_task_failed(msg: MessageEnvelope):
            self.state = SessionState.CONNECTED
            self._current_task = None

            if self._on_error:
                self._on_error(msg.payload.get("reason", "Task failed"))

        @self._ws.on_message(MessageType.SESSION_ERROR)
        async def handle_error(msg: MessageEnvelope):
            if self._on_error:
                self._on_error(msg.payload.get("message", "Unknown error"))

        @self._ws.on_message(MessageType.VALIDATION_REQUEST)
        async def handle_validation_request(msg: MessageEnvelope):
            # Run validations locally
            await self._run_validations(msg.payload)

    def _setup_model_b_handlers(self) -> None:
        """Set up Model B specific handlers."""

        # LLM Delta handler - streaming text
        def handle_llm_delta(payload: LLMDeltaPayload):
            if payload.delta_type == "text" and self._on_delta:
                self._on_delta("text", payload.content)
            elif payload.delta_type == "thinking" and self._on_delta:
                self._on_delta("thinking", payload.content)

            # Update token count
            if self._current_task:
                self._current_task.output_tokens += payload.token_count

        self._ws.on_llm_delta(handle_llm_delta)

        # LLM Response End handler
        def handle_llm_response(payload: LLMResponseEndPayload):
            # Update context ref
            if payload.new_context_ref:
                self._context_ref = payload.new_context_ref
                if self._current_task:
                    self._current_task.context_ref = payload.new_context_ref

            # Update tokens
            if self._current_task:
                self._current_task.input_tokens += payload.input_tokens
                self._current_task.output_tokens += payload.output_tokens
                self._current_task.tokens_used = (
                    self._current_task.input_tokens + self._current_task.output_tokens
                )

        self._ws.on_llm_response(handle_llm_response)

        # LLM Error handler
        def handle_llm_error(payload: LLMErrorPayload):
            logger.error(f"LLM Error [{payload.error_code}]: {payload.error_message}")
            if self._on_error:
                self._on_error(f"LLM Error: {payload.error_message}")

        self._ws.on_llm_error(handle_llm_error)

        # Checkpoint ACK handler
        def handle_checkpoint_ack(payload: ContextCheckpointAckPayload):
            if payload.stored:
                logger.debug(f"Checkpoint stored: {payload.checkpoint_id}")
            else:
                logger.warning(f"Checkpoint failed: {payload.checkpoint_id}")

        self._ws.on_checkpoint_ack(handle_checkpoint_ack)

        # Session Sync handler (for resume)
        def handle_session_sync(payload: SessionSyncPayload):
            if payload.status == "resumed":
                self._context_ref = payload.last_context_ref
                logger.info(f"Session resumed: {payload.session_id}, context_ref={payload.last_context_ref}")

                if payload.needs_resync and self._on_error:
                    self._on_error(f"Session state changed. Resyncing from {payload.resync_from}")

            elif payload.status == "stale":
                logger.warning("Session is stale, context may be outdated")
                if self._on_error:
                    self._on_error("Session is stale. Some context may be lost.")

            elif payload.status == "ended":
                self.state = SessionState.DISCONNECTED
                if self._on_error:
                    self._on_error("Session has ended.")

        self._ws.on_session_sync(handle_session_sync)

    async def _execute_tool(
        self,
        tool_id: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a tool locally.

        Args:
            tool_id: Tool request ID
            tool_name: Name of tool
            args: Tool arguments

        Returns:
            Tool result payload
        """
        import time
        start = time.time()

        try:
            result = await self._tools.execute(tool_name, args)
            duration_ms = int((time.time() - start) * 1000)

            return {
                "tool_id": tool_id,
                "status": "success",
                "result": result,
                "duration_ms": duration_ms,
                "output_truncated": False,
            }

        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            return {
                "tool_id": tool_id,
                "status": "error",
                "error": str(e),
                "duration_ms": duration_ms,
            }

    async def _run_validations(self, payload: dict[str, Any]) -> None:
        """Run validation steps locally.

        Args:
            payload: Validation request payload
        """
        task_id = payload.get("task_id")
        attempt_no = payload.get("attempt_no")
        steps = payload.get("steps", [])

        for step in steps:
            step_name = step.get("name")
            command = step.get("command")
            timeout_ms = step.get("timeout_ms", 120000)

            try:
                result = await self._tools.execute(
                    "bash",
                    {"command": command},
                    timeout=timeout_ms / 1000,
                )

                # Send result
                await self._ws.send(
                    MessageType.VALIDATION_RESULT,
                    {
                        "task_id": task_id,
                        "attempt_no": attempt_no,
                        "step_name": step_name,
                        "status": "passed" if result.get("exit_code") == 0 else "failed",
                        "output_summary": result.get("output", "")[:1000],
                    },
                    task_id=task_id,
                )

            except asyncio.TimeoutError:
                await self._ws.send(
                    MessageType.VALIDATION_RESULT,
                    {
                        "task_id": task_id,
                        "attempt_no": attempt_no,
                        "step_name": step_name,
                        "status": "timeout",
                        "output_summary": f"Validation timed out after {timeout_ms}ms",
                    },
                    task_id=task_id,
                )

            except Exception as e:
                await self._ws.send(
                    MessageType.VALIDATION_RESULT,
                    {
                        "task_id": task_id,
                        "attempt_no": attempt_no,
                        "step_name": step_name,
                        "status": "error",
                        "output_summary": str(e),
                    },
                    task_id=task_id,
                )

    def _build_context(self) -> dict[str, Any]:
        """Build workspace context for task submission."""
        context = {
            "cwd": self.config.workspace_dir,
            "active_files": [],
            "git_status": None,
            "available_validations": [],
        }

        # Check for git
        git_dir = Path(self.config.workspace_dir) / ".git"
        if git_dir.exists():
            try:
                import subprocess
                result = subprocess.run(
                    ["git", "status", "--porcelain", "--branch"],
                    cwd=self.config.workspace_dir,
                    capture_output=True,
                    text=True,
                )
                lines = result.stdout.strip().split("\n")
                if lines:
                    branch_line = lines[0]
                    branch = branch_line.replace("## ", "").split("...")[0]
                    dirty = len(lines) > 1

                    context["git_status"] = {
                        "branch": branch,
                        "dirty": dirty,
                    }
            except Exception:
                pass

        # Check for test frameworks
        workspace = Path(self.config.workspace_dir)
        if (workspace / "pytest.ini").exists() or (workspace / "pyproject.toml").exists():
            context["available_validations"].append("pytest")
        if (workspace / "setup.cfg").exists():
            context["available_validations"].append("mypy")
            context["available_validations"].append("ruff")

        return context
