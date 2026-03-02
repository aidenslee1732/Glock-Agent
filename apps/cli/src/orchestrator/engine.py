"""
Orchestration Engine for Model B.

The full client-side orchestration loop:
- Manages LLM requests and responses
- Executes tools locally
- Handles context packing and checkpoints
- Enforces budgets and plan constraints
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, Callable, Optional

from packages.shared_protocol.types import (
    MessageEnvelope,
    MessageType,
    LLMRequestPayload,
    LLMDeltaPayload,
    LLMResponseEndPayload,
    ContextCheckpointPayload,
    ToolCallResult,
    ContextPack,
    ContextDelta,
    Message,
    RollingSummary,
    PinnedFact,
    FileSlice,
    TokenBudget,
    CompiledPlan,
    generate_request_id,
    generate_checkpoint_id,
)
from apps.cli.src.tools.broker import ToolBroker
from apps.cli.src.config import ModeManager, ModeConfig, get_mode_config, OperationalMode

# Import new v4 components
from .parallel_executor import ParallelToolExecutor, ToolCall, ParallelExecutionResult
from .retry import RetryableOperation, RetryConfig, RetryResult
from .council_integration import CouncilIntegration, CouncilResult
from .preflight import PreflightChecker, PreflightResult
from .tracing import ExecutionTracer, TaskOutcome

logger = logging.getLogger(__name__)


# Configuration defaults
MAX_TURNS = 100
MAX_TOOL_CALLS_PER_TURN = 30
DEFAULT_MODEL_TIER = "standard"
LLM_TIMEOUT_MS = 300000
MAX_TOTAL_TOKENS = 300000  # Force final response before hitting this
TOKEN_WARNING_THRESHOLD = 0.85  # Warn at 85% of limit


@dataclass
class OrchestrationConfig:
    """Configuration for the orchestration engine."""
    model_tier: str = DEFAULT_MODEL_TIER
    max_turns: int = MAX_TURNS
    max_tool_calls_per_turn: int = MAX_TOOL_CALLS_PER_TURN
    llm_timeout_ms: int = LLM_TIMEOUT_MS
    checkpoint_interval: int = 3  # Save checkpoint every N turns
    max_total_tokens: int = MAX_TOTAL_TOKENS  # Token budget for task
    token_warning_threshold: float = TOKEN_WARNING_THRESHOLD  # Force response at this %
    # Operational mode settings
    mode: str = "smart"  # smart, rush, or deep
    council_enabled: bool = True
    council_timeout: float = 120.0
    council_perspectives: list[str] = None  # Set from mode config
    quality_gate_enabled: bool = True
    quality_gate_min_score: float = 60.0
    # v4 enhancements
    parallel_tools_enabled: bool = True  # Enable parallel tool execution
    parallel_max_concurrency: int = 10  # Max concurrent tool calls
    retry_enabled: bool = True  # Enable retry for transient failures
    retry_max_attempts: int = 3  # Max retry attempts
    preflight_enabled: bool = True  # Enable pre-flight checks
    preflight_lint: bool = True  # Run lint checks
    preflight_types: bool = False  # Run type checks (slower)
    tracing_enabled: bool = True  # Enable execution tracing
    security_scan_enabled: bool = True  # Bug fix 3.5: Enable security scanning

    def __post_init__(self):
        """Apply mode configuration settings."""
        mode_config = get_mode_config(self.mode)
        # Apply mode defaults if not explicitly set
        if self.council_perspectives is None:
            self.council_perspectives = mode_config.council_perspectives
        self.model_tier = mode_config.model_tier
        self.council_enabled = mode_config.council_enabled
        self.council_timeout = mode_config.council_timeout
        self.quality_gate_enabled = mode_config.quality_gate_enabled
        self.quality_gate_min_score = mode_config.quality_gate_min_score
        if mode_config.max_iterations < self.max_turns:
            self.max_turns = mode_config.max_iterations


class EventType(str, Enum):
    """Types of orchestration events."""
    THINKING = "thinking"
    TEXT_DELTA = "text_delta"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    EDIT_PROPOSAL = "edit_proposal"
    TASK_COMPLETE = "task_complete"
    ERROR = "error"
    CHECKPOINT_SAVED = "checkpoint_saved"
    # New event types for v4
    COUNCIL_REVIEW = "council_review"
    COUNCIL_REJECTION = "council_rejection"
    PREFLIGHT_CHECK = "preflight_check"
    PREFLIGHT_FAILURE = "preflight_failure"
    RETRY_ATTEMPT = "retry_attempt"
    SECURITY_SCAN = "security_scan"  # Bug fix 3.5: Security scanner event
    SECURITY_ISSUE = "security_issue"  # Bug fix 3.5: Security issue found


@dataclass
class OrchestrationEvent:
    """An event from the orchestration loop."""
    type: EventType
    content: str = ""
    tool_name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    edit_id: str = ""
    file_path: str = ""
    diff: str = ""
    new_content: str = ""
    summary: str = ""
    message: str = ""
    checkpoint_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def data(self) -> dict[str, Any]:
        """Get event data as a dictionary for easy access."""
        return {
            "type": self.type.value if isinstance(self.type, EventType) else self.type,
            "content": self.content,
            "tool_name": self.tool_name,
            "args": self.args,
            "result": self.result,
            "edit_id": self.edit_id,
            "file_path": self.file_path,
            "diff": self.diff,
            "new_content": self.new_content,
            "summary": self.summary,
            "message": self.message,
            "checkpoint_id": self.checkpoint_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass
class TaskResult:
    """Result of a completed task."""
    success: bool
    summary: str
    files_modified: list[str]
    total_turns: int
    total_tokens: int
    tool_calls: int
    duration_ms: int
    error: Optional[str] = None


@dataclass
class ConversationTurn:
    """A turn in the conversation."""
    role: str
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)


class OrchestrationEngine:
    """
    Full client-side orchestration for Model B.

    This is the heart of Model B - the client does ALL orchestration:
    - Sends LLM requests to server (server is just a proxy)
    - Executes tools locally
    - Manages context packing for token efficiency
    - Creates checkpoints for resume capability
    - Enforces budget and plan constraints
    """

    def __init__(
        self,
        ws_client: Any,  # WebSocketClient
        tool_broker: ToolBroker,
        context_packer: Optional[Any] = None,  # ContextPacker
        key_manager: Optional[Any] = None,  # SessionKeyManager
        config: Optional[OrchestrationConfig] = None,
        workspace_path: Optional[str] = None,
        mode: str = "smart",  # Operational mode: smart, rush, or deep
    ):
        self.tools = tool_broker
        self._ws = ws_client
        self._context_packer = context_packer
        self._key_manager = key_manager
        # Apply mode to config if provided
        if config is None:
            config = OrchestrationConfig(mode=mode)
        self.config = config
        self.model_tier = self.config.model_tier
        self._workspace_path = workspace_path

        # Mode management
        self._mode_manager = ModeManager(
            default_mode=OperationalMode(self.config.mode)
        )

        # Load project config (GLOCK.md)
        self._project_config: Optional[str] = None
        if workspace_path:
            self._load_project_config(workspace_path)

        # Conversation state
        self._turns: list[ConversationTurn] = []
        self._context_ref: Optional[str] = None
        self._turn_count: int = 0
        self._total_tokens: int = 0
        self._tool_call_count: int = 0
        self._files_modified: set[str] = set()

        # Rolling summary and facts
        self._rolling_summary = RollingSummary(
            task_description="",
            files_modified=[],
            files_read=[],
            key_decisions=[],
            errors_encountered=[],
            current_state="",
            turn_count=0,
        )
        self._pinned_facts: list[PinnedFact] = []
        self._file_slices: list[FileSlice] = []

        # Active request tracking
        self._active_request_id: Optional[str] = None
        self._response_event: asyncio.Event = asyncio.Event()
        self._response_content: str = ""
        self._response_tool_calls: list[ToolCallResult] = []
        self._response_error: Optional[str] = None

        # Budget
        self._budget = TokenBudget()

        # v4 enhancements: Initialize new components
        # Parallel tool executor
        self._parallel_executor: Optional[ParallelToolExecutor] = None
        if self.config.parallel_tools_enabled:
            self._parallel_executor = ParallelToolExecutor(
                tool_broker=tool_broker,
                max_concurrency=self.config.parallel_max_concurrency,
                enable_parallel=True,
            )

        # Retry logic
        self._retry_config: Optional[RetryConfig] = None
        if self.config.retry_enabled:
            self._retry_config = RetryConfig(
                max_retries=self.config.retry_max_attempts,
                initial_delay=1.0,
                max_delay=30.0,
            )

        # Council integration
        self._council: Optional[CouncilIntegration] = None
        if self.config.council_enabled:
            self._council = CouncilIntegration(
                mode_manager=self._mode_manager,
                default_timeout=self.config.council_timeout,
            )

        # Pre-flight checker
        self._preflight: Optional[PreflightChecker] = None
        if self.config.preflight_enabled and workspace_path:
            self._preflight = PreflightChecker(
                workspace_path=workspace_path,
                enable_lint=self.config.preflight_lint,
                enable_type=self.config.preflight_types,
            )

        # Execution tracer
        self._tracer: Optional[ExecutionTracer] = None
        if self.config.tracing_enabled:
            self._tracer = ExecutionTracer()

        # Bug fix 3.5: Security scanner
        self._security_scanner = None
        if self.config.security_scan_enabled and workspace_path:
            try:
                from pathlib import Path
                from ..security.scanner import SecurityScanner

                self._security_scanner = SecurityScanner(
                    workspace_path=Path(workspace_path),
                )
                logger.info("Security scanner initialized")
            except ImportError:
                logger.debug("Security scanner module not available")
            except Exception as e:
                logger.warning(f"Failed to initialize security scanner: {e}")

        # Bug fix 3.2: Initialize memory store and pinned facts manager
        self._memory_store = None
        self._facts_manager = None
        if workspace_path:
            try:
                from pathlib import Path
                from ..memory.store import MemoryStore
                from ..context.facts import PinnedFactsManager

                # Initialize memory store
                memory_db_path = Path(workspace_path) / ".glock" / "memory.db"
                self._memory_store = MemoryStore(db_path=memory_db_path)

                # Initialize facts manager with memory store
                self._facts_manager = PinnedFactsManager(
                    memory_store=self._memory_store,
                    workspace=workspace_path,
                )

                # Load persisted facts from memory store
                loaded = self._facts_manager.load_from_memory_store()
                if loaded > 0:
                    logger.info(f"Loaded {loaded} facts from memory store")

            except ImportError as e:
                logger.debug(f"Memory store module not available: {e}")
            except Exception as e:
                logger.warning(f"Failed to initialize memory store: {e}")

        # Set up message handlers
        self._setup_handlers()

    def _load_project_config(self, workspace_path: str) -> None:
        """Load project configuration from GLOCK.md.

        Args:
            workspace_path: Path to workspace directory
        """
        try:
            from pathlib import Path
            from ..config import load_project_config

            path = Path(workspace_path)
            config = load_project_config(path)

            if not config.is_empty():
                self._project_config = config.to_system_prompt_section()
                logger.info(f"Loaded project config from {config.source_path}")
            else:
                logger.debug("No GLOCK.md configuration found")
        except ImportError:
            logger.debug("Project config module not available")
        except Exception as e:
            logger.warning(f"Failed to load project config: {e}")

    def _setup_handlers(self) -> None:
        """Set up handlers for LLM response messages."""

        # LLM Delta handler
        def handle_llm_delta(payload: LLMDeltaPayload):
            if payload.request_id != self._active_request_id:
                return

            if payload.delta_type == "text":
                self._response_content += payload.content
            elif payload.delta_type == "tool_call" and payload.tool_call:
                # Accumulate tool call
                pass  # Tool calls come in LLM_RESPONSE_END

        self._ws.on_llm_delta(handle_llm_delta)

        # LLM Response End handler
        def handle_llm_response_end(payload: LLMResponseEndPayload):
            if payload.request_id != self._active_request_id:
                return

            # Update context ref
            self._context_ref = payload.new_context_ref

            # Parse tool calls
            if payload.tool_calls:
                self._response_tool_calls = payload.tool_calls

            # Update token count
            self._total_tokens += payload.input_tokens + payload.output_tokens

            # Signal response complete
            self._response_event.set()

        self._ws.on_llm_response(handle_llm_response_end)

        # LLM Error handler
        def handle_llm_error(payload: LLMErrorPayload):
            if payload.request_id != self._active_request_id:
                return

            self._response_error = payload.error_message
            self._response_event.set()

        self._ws.on_llm_error(handle_llm_error)

        # Checkpoint ACK handler
        def handle_checkpoint_ack(payload):
            logger.debug(f"Checkpoint acknowledged: {payload.checkpoint_id}")

        self._ws.on_checkpoint_ack(handle_checkpoint_ack)

    async def run_task(
        self,
        prompt: str,
        initial_context_ref: Optional[str] = None,
        plan: Optional[CompiledPlan] = None,
    ) -> AsyncIterator[OrchestrationEvent]:
        """
        Run a task from start to completion.

        This is the main orchestration loop. It:
        1. Sends the initial prompt to the LLM
        2. Processes LLM responses (text, tool calls)
        3. Executes tools locally
        4. Continues until task is complete or budget exhausted

        Args:
            prompt: The user's task description
            initial_context_ref: Optional checkpoint reference to resume from
            plan: Optional compiled plan with constraints

        Yields:
            OrchestrationEvent for each significant event
        """
        start_time = time.time()
        self._reset_state()

        # Set initial context ref if resuming
        if initial_context_ref:
            self._context_ref = initial_context_ref

        # Initialize rolling summary with task
        self._rolling_summary.task_description = prompt
        self._rolling_summary.current_state = "Starting task"

        # v4: Start execution tracing
        task_id = generate_request_id()  # Use request ID format for task ID
        self._start_task_tracing(task_id, prompt)

        # If using context packer, set the task and process user message
        if self._context_packer:
            self._context_packer.set_task(prompt)
            self._context_packer.process_user_message(prompt)

        # Add initial user message
        self._turns.append(ConversationTurn(role="user", content=prompt))

        # Extract budgets from plan or config
        max_turns = self.config.max_turns
        max_tool_calls = self.config.max_tool_calls_per_turn * max_turns
        if plan and plan.budgets:
            max_turns = plan.budgets.max_iterations
            max_tool_calls = plan.budgets.max_tool_calls

        task_complete = False
        error: Optional[str] = None
        force_final_response = False
        token_limit = self.config.max_total_tokens
        token_threshold = self.config.token_warning_threshold

        try:
            while self._turn_count < max_turns and not task_complete and self._total_tokens < token_limit:
                self._turn_count += 1

                # v4: Start turn tracing
                self._trace_turn_start(self._turn_count)

                # Check if we should force final response
                approaching_turn_limit = self._turn_count >= max_turns - 1
                approaching_token_limit = self._total_tokens >= (token_limit * token_threshold)

                if approaching_turn_limit or approaching_token_limit:
                    force_final_response = True
                    if approaching_token_limit:
                        logger.info(f"Token limit approaching: {self._total_tokens}/{token_limit} ({self._total_tokens/token_limit*100:.1f}%)")

                # Build context for LLM request
                context_pack = self._build_context_pack()
                delta = self._build_delta()

                # If forcing final response, add instruction to summarize
                if force_final_response:
                    reason = "Token limit" if approaching_token_limit else "Turn limit"
                    self._turns.append(ConversationTurn(
                        role="user",
                        content=f"[SYSTEM: {reason} approaching. Please provide your final answer now without using any more tools. Summarize your findings and answer the original question directly.]",
                    ))
                    delta = self._build_delta()

                # Send LLM request
                yield OrchestrationEvent(type=EventType.THINKING)

                response_content, tool_calls = await self._request_llm(
                    context_pack=context_pack,
                    delta=delta,
                    plan=plan,
                    force_no_tools=force_final_response,  # Disable tools on final turn
                )

                if self._response_error:
                    error = self._response_error
                    yield OrchestrationEvent(
                        type=EventType.ERROR,
                        message=error,
                    )
                    break

                # Process response content
                if response_content:
                    # Stream text to UI
                    yield OrchestrationEvent(
                        type=EventType.TEXT_DELTA,
                        content=response_content,
                    )

                # Process through context packer (always, even without content)
                if self._context_packer and (response_content or tool_calls):
                    self._context_packer.process_assistant_response(
                        content=response_content or "",
                        tool_calls=[tc.to_dict() for tc in tool_calls] if tool_calls else None,
                    )

                # Add assistant turn if there's content OR tool_calls
                # CRITICAL: Must add assistant turn for tool_calls even without text content,
                # otherwise tool results won't be attached to any message
                if response_content or tool_calls:
                    self._turns.append(ConversationTurn(
                        role="assistant",
                        content=response_content or "",
                        tool_calls=[tc.to_dict() for tc in tool_calls] if tool_calls else [],
                    ))

                # Process tool calls - v4: Use parallel execution when safe
                if tool_calls:
                    # v4: Use parallel executor for multiple independent tool calls
                    if self._parallel_executor and len(tool_calls) > 1:
                        async for tool_call, result, status, event in self._execute_tools_parallel(
                            tool_calls, prompt
                        ):
                            if self._tool_call_count >= max_tool_calls:
                                error = "Tool call budget exceeded"
                                yield OrchestrationEvent(
                                    type=EventType.ERROR,
                                    message=error,
                                )
                                break

                            self._tool_call_count += 1

                            # Emit tool start event
                            yield OrchestrationEvent(
                                type=EventType.TOOL_START,
                                tool_name=tool_call.tool_name,
                                args=tool_call.arguments,
                            )

                            # Check for edit proposals
                            if tool_call.tool_name in ("edit_file", "write_file"):
                                file_path = tool_call.arguments.get("file_path", "")
                                new_content = tool_call.arguments.get("content", "")
                                old_string = tool_call.arguments.get("old_string", "")
                                new_string = tool_call.arguments.get("new_string", "")

                                # Build diff for edit
                                diff = ""
                                if old_string and new_string:
                                    diff = f"- {old_string}\n+ {new_string}"

                                yield OrchestrationEvent(
                                    type=EventType.EDIT_PROPOSAL,
                                    edit_id=tool_call.tool_call_id,
                                    file_path=file_path,
                                    diff=diff,
                                    new_content=new_content or new_string,
                                )

                            # Yield any v4 events (council rejection, preflight failure)
                            if event:
                                yield event

                            # Track modified files
                            if tool_call.tool_name in ("edit_file", "write_file"):
                                file_path = tool_call.arguments.get("file_path", "")
                                if file_path:
                                    self._files_modified.add(file_path)
                                    if file_path not in self._rolling_summary.files_modified:
                                        self._rolling_summary.files_modified.append(file_path)
                                    # v4: Record file modification in tracer
                                    if self._tracer:
                                        self._tracer.record_file_modification(file_path)

                            # Track read files
                            if tool_call.tool_name == "read_file":
                                file_path = tool_call.arguments.get("file_path", "")
                                if file_path and file_path not in self._rolling_summary.files_read:
                                    self._rolling_summary.files_read.append(file_path)

                            # v4: Trace tool call
                            self._trace_tool_call(
                                tool_call_id=tool_call.tool_call_id,
                                tool_name=tool_call.tool_name,
                                arguments=tool_call.arguments,
                                success=status == "success",
                                result_summary=str(result)[:200],
                                duration_ms=0,  # Parallel executor tracks this
                                error=result.get("error") if isinstance(result, dict) else None,
                            )

                            # Emit tool end event
                            yield OrchestrationEvent(
                                type=EventType.TOOL_END,
                                tool_name=tool_call.tool_name,
                                result=result,
                            )

                            # Process tool result through context packer
                            if self._context_packer:
                                wrapped_result = {
                                    "status": status,
                                    "result": result if isinstance(result, dict) else {"output": result},
                                }
                                self._context_packer.process_tool_result(
                                    tool_call_id=tool_call.tool_call_id,
                                    tool_name=tool_call.tool_name,
                                    args=tool_call.arguments,
                                    result=wrapped_result,
                                )

                            # Add tool result to conversation
                            if self._turns and self._turns[-1].role == "assistant":
                                self._turns[-1].tool_results.append({
                                    "tool_call_id": tool_call.tool_call_id,
                                    "tool_name": tool_call.tool_name,
                                    "status": status,
                                    "result": result,
                                })

                            # Bug fix 3.3: If council rejected or preflight failed, add feedback
                            if event and event.type in (EventType.COUNCIL_REJECTION, EventType.PREFLIGHT_FAILURE):
                                feedback_msg = (
                                    f"[FEEDBACK] The previous {tool_call.tool_name} operation was rejected. "
                                    f"Please review the feedback and revise your approach:\n\n{event.message}"
                                )
                                self._turns.append(ConversationTurn(
                                    role="user",
                                    content=feedback_msg,
                                ))
                    else:
                        # Sequential execution with v4 enhancements
                        for tool_call in tool_calls:
                            if self._tool_call_count >= max_tool_calls:
                                error = "Tool call budget exceeded"
                                yield OrchestrationEvent(
                                    type=EventType.ERROR,
                                    message=error,
                                )
                                break

                            self._tool_call_count += 1
                            tool_start_time = time.time()

                            # Emit tool start event
                            yield OrchestrationEvent(
                                type=EventType.TOOL_START,
                                tool_name=tool_call.tool_name,
                                args=tool_call.arguments,
                            )

                            # Check for edit proposals
                            if tool_call.tool_name in ("edit_file", "write_file"):
                                file_path = tool_call.arguments.get("file_path", "")
                                new_content = tool_call.arguments.get("content", "")
                                old_string = tool_call.arguments.get("old_string", "")
                                new_string = tool_call.arguments.get("new_string", "")

                                # Build diff for edit
                                diff = ""
                                if old_string and new_string:
                                    diff = f"- {old_string}\n+ {new_string}"

                                yield OrchestrationEvent(
                                    type=EventType.EDIT_PROPOSAL,
                                    edit_id=tool_call.tool_call_id,
                                    file_path=file_path,
                                    diff=diff,
                                    new_content=new_content or new_string,
                                )

                            # v4: Execute with council review, retry, and preflight
                            result, status, event = await self._execute_single_tool(tool_call, prompt)

                            tool_duration_ms = int((time.time() - tool_start_time) * 1000)

                            # Yield any v4 events (council rejection, preflight failure)
                            if event:
                                yield event

                            # Track modified files
                            if tool_call.tool_name in ("edit_file", "write_file"):
                                file_path = tool_call.arguments.get("file_path", "")
                                if file_path:
                                    self._files_modified.add(file_path)
                                    if file_path not in self._rolling_summary.files_modified:
                                        self._rolling_summary.files_modified.append(file_path)
                                    # v4: Record file modification in tracer
                                    if self._tracer:
                                        self._tracer.record_file_modification(file_path)

                            # Track read files
                            if tool_call.tool_name == "read_file":
                                file_path = tool_call.arguments.get("file_path", "")
                                if file_path and file_path not in self._rolling_summary.files_read:
                                    self._rolling_summary.files_read.append(file_path)

                            # v4: Trace tool call
                            self._trace_tool_call(
                                tool_call_id=tool_call.tool_call_id,
                                tool_name=tool_call.tool_name,
                                arguments=tool_call.arguments,
                                success=status == "success",
                                result_summary=str(result)[:200],
                                duration_ms=tool_duration_ms,
                                error=result.get("error") if isinstance(result, dict) else None,
                            )

                            # Emit tool end event
                            yield OrchestrationEvent(
                                type=EventType.TOOL_END,
                                tool_name=tool_call.tool_name,
                                result=result,
                            )

                            # Process tool result through context packer
                            if self._context_packer:
                                wrapped_result = {
                                    "status": status,
                                    "result": result if isinstance(result, dict) else {"output": result},
                                }
                                self._context_packer.process_tool_result(
                                    tool_call_id=tool_call.tool_call_id,
                                    tool_name=tool_call.tool_name,
                                    args=tool_call.arguments,
                                    result=wrapped_result,
                                )

                            # Add tool result to conversation
                            if self._turns and self._turns[-1].role == "assistant":
                                self._turns[-1].tool_results.append({
                                    "tool_call_id": tool_call.tool_call_id,
                                    "tool_name": tool_call.tool_name,
                                    "status": status,
                                    "result": result,
                                })

                            # Bug fix 3.3: If council rejected or preflight failed, add feedback message
                            # to guide the LLM to revise its approach
                            if event and event.type in (EventType.COUNCIL_REJECTION, EventType.PREFLIGHT_FAILURE):
                                feedback_msg = (
                                    f"[FEEDBACK] The previous {tool_call.tool_name} operation was rejected. "
                                    f"Please review the feedback and revise your approach:\n\n{event.message}"
                                )
                                self._turns.append(ConversationTurn(
                                    role="user",
                                    content=feedback_msg,
                                ))

                    if error:
                        break

                else:
                    # No tool calls - task is complete
                    task_complete = True

                # v4: End turn tracing
                self._trace_turn_end(
                    input_tokens=0,  # Will be updated when we track this
                    output_tokens=0,
                    response=response_content,
                    error=error,
                )

                # Update rolling summary
                self._rolling_summary.turn_count = self._turn_count
                self._rolling_summary.current_state = (
                    "Task completed" if task_complete else f"Turn {self._turn_count}"
                )
                self._rolling_summary.last_updated_at = datetime.utcnow()

                # Save checkpoint periodically
                if self._turn_count % 3 == 0 or task_complete:
                    checkpoint_id = await self._save_checkpoint()
                    yield OrchestrationEvent(
                        type=EventType.CHECKPOINT_SAVED,
                        checkpoint_id=checkpoint_id,
                    )

        except asyncio.CancelledError:
            error = "Task cancelled"
            yield OrchestrationEvent(type=EventType.ERROR, message=error)

        except Exception as e:
            error = str(e)
            logger.exception("Orchestration error")
            yield OrchestrationEvent(type=EventType.ERROR, message=error)

        # v4: End task tracing
        self._end_task_tracing(success=task_complete and not error, error=error)

        # Calculate duration
        duration_ms = int((time.time() - start_time) * 1000)

        # Build summary
        summary = self._build_summary(task_complete, error)

        # Emit completion event
        yield OrchestrationEvent(
            type=EventType.TASK_COMPLETE,
            summary=summary,
        )

    async def cancel(self) -> None:
        """Cancel the current task."""
        if self._active_request_id:
            await self._ws.send_llm_cancel(
                request_id=self._active_request_id,
                reason="user_cancelled",
            )

    @property
    def mode_manager(self) -> ModeManager:
        """Get the mode manager for this engine."""
        return self._mode_manager

    @property
    def current_mode(self) -> str:
        """Get the current operational mode."""
        return self._mode_manager.current_mode.value

    def set_mode(self, mode: str) -> ModeConfig:
        """Set the operational mode.

        Args:
            mode: Mode name (smart, rush, or deep)

        Returns:
            New mode configuration
        """
        mode_config = self._mode_manager.set_mode(mode)
        # Update engine config to reflect mode settings
        self.config.mode = mode
        self.config.model_tier = mode_config.model_tier
        self.config.council_enabled = mode_config.council_enabled
        self.config.council_timeout = mode_config.council_timeout
        self.config.council_perspectives = mode_config.council_perspectives
        self.config.quality_gate_enabled = mode_config.quality_gate_enabled
        self.config.quality_gate_min_score = mode_config.quality_gate_min_score
        self.model_tier = mode_config.model_tier
        logger.info(f"Switched to {mode} mode")
        return mode_config

    def should_run_council(self, task_complexity: str = "normal") -> bool:
        """Determine if council should run based on mode and task.

        Args:
            task_complexity: "trivial", "simple", "normal", "complex"

        Returns:
            Whether to run council
        """
        return self._mode_manager.should_run_council(task_complexity)

    async def _request_llm(
        self,
        context_pack: ContextPack,
        delta: ContextDelta,
        plan: Optional[CompiledPlan] = None,
        force_no_tools: bool = False,
    ) -> tuple[str, list[ToolCallResult]]:
        """
        Send LLM request and wait for response.

        Args:
            context_pack: The context pack with summary, facts, file slices
            delta: Recent messages and tool results
            plan: Optional compiled plan with constraints
            force_no_tools: If True, don't send tools (forces text response)

        Returns:
            Tuple of (response_content, tool_calls)
        """
        # Reset response state
        self._response_event.clear()
        self._response_content = ""
        self._response_tool_calls = []
        self._response_error = None

        # Generate request ID
        request_id = generate_request_id()
        self._active_request_id = request_id

        # Build tool definitions (empty if forcing no tools)
        if force_no_tools:
            tools = []
        else:
            tools = self._build_tool_definitions(plan)

        # Send request via WebSocket client
        await self._ws.send_llm_request(
            request_id=request_id,
            context_ref=self._context_ref,
            delta=delta.to_dict() if hasattr(delta, 'to_dict') else self._delta_to_dict(delta),
            context_pack=context_pack.to_dict() if hasattr(context_pack, 'to_dict') else self._pack_to_dict(context_pack),
            tools=[t.to_dict() if hasattr(t, 'to_dict') else t for t in tools],
            model_tier=self.model_tier,
            max_tokens=8192,
            temperature=0.7,
        )

        # Wait for response
        try:
            await asyncio.wait_for(
                self._response_event.wait(),
                timeout=self.config.llm_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            self._response_error = "LLM request timed out"

        self._active_request_id = None

        return self._response_content, self._response_tool_calls

    def _delta_to_dict(self, delta: ContextDelta) -> dict[str, Any]:
        """Convert ContextDelta to dict."""
        messages = []
        for m in delta.messages:
            msg = {"role": m.role, "content": m.content}
            # CRITICAL: Include tool_calls for assistant messages
            if m.tool_calls:
                msg["tool_calls"] = m.tool_calls
            messages.append(msg)

        return {
            "messages": messages,
            "tool_results_compressed": delta.tool_results_compressed,
            "token_count": delta.token_count,
        }

    def _pack_to_dict(self, pack: ContextPack) -> dict[str, Any]:
        """Convert ContextPack to dict."""
        result = {
            "rolling_summary": pack.rolling_summary.to_dict() if hasattr(pack.rolling_summary, 'to_dict') else {},
            "pinned_facts": [
                f.to_dict() if hasattr(f, 'to_dict') else {"key": f.key, "value": f.value}
                for f in pack.pinned_facts
            ],
            "file_slices": [
                s.to_dict() if hasattr(s, 'to_dict') else {"file_path": s.file_path, "content": s.content}
                for s in pack.file_slices
            ],
            "token_count": pack.token_count,
        }

        # Include project configuration if loaded
        if self._project_config:
            result["project_config"] = self._project_config

        return result

    def _build_context_pack(self) -> ContextPack:
        """Build the stable context pack."""
        # Use context packer if available
        if self._context_packer:
            pack, _ = self._context_packer.build()
            return pack

        # Fallback to manual building
        return ContextPack(
            rolling_summary=self._rolling_summary,
            pinned_facts=self._pinned_facts,
            file_slices=self._file_slices,
            token_count=self._estimate_pack_tokens(),
        )

    def _build_delta(self) -> ContextDelta:
        """Build the delta since last checkpoint."""
        # Use context packer if available
        if self._context_packer:
            _, delta = self._context_packer.build()
            return delta

        # Fallback to manual building
        # Get recent messages (after checkpoint)
        messages: list[Message] = []
        tool_results: list[dict[str, Any]] = []

        # For now, include last few turns as delta
        recent_turns = self._turns[-5:] if len(self._turns) > 5 else self._turns

        for turn in recent_turns:
            # Build message dict with tool_calls if present (for assistant messages)
            msg_dict = {
                "role": turn.role,
                "content": turn.content,
            }

            # CRITICAL: Include tool_calls for assistant messages
            # This is required for the Anthropic API to match tool_results
            if turn.role == "assistant" and turn.tool_calls:
                msg_dict["tool_calls"] = turn.tool_calls

            messages.append(Message(**msg_dict))

            # Include compressed tool results
            for result in turn.tool_results:
                compressed = self._compress_tool_result(result)
                tool_results.append(compressed)

        return ContextDelta(
            messages=messages,
            tool_results_compressed=tool_results,
            token_count=self._estimate_delta_tokens(messages, tool_results),
        )

    def _build_tool_definitions(self, plan: Optional[CompiledPlan]) -> list:
        """Build tool definitions for LLM."""
        from packages.shared_protocol.types import ToolDefinition

        # Base tools
        tools = [
            ToolDefinition(
                name="read_file",
                description="Read the contents of a file",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to file"},
                        "offset": {"type": "integer", "description": "Line offset"},
                        "limit": {"type": "integer", "description": "Max lines to read"},
                    },
                    "required": ["file_path"],
                },
            ),
            ToolDefinition(
                name="edit_file",
                description="Edit a file by replacing text",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["file_path", "old_string", "new_string"],
                },
            ),
            ToolDefinition(
                name="write_file",
                description="Write content to a new file",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["file_path", "content"],
                },
            ),
            ToolDefinition(
                name="glob",
                description="Find files matching a pattern",
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["pattern"],
                },
            ),
            ToolDefinition(
                name="grep",
                description="Search file contents",
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                        "output_mode": {"type": "string"},
                    },
                    "required": ["pattern"],
                },
            ),
            ToolDefinition(
                name="bash",
                description="Execute a shell command",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout": {"type": "integer"},
                    },
                    "required": ["command"],
                },
            ),
            ToolDefinition(
                name="list_directory",
                description="List directory contents",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": [],
                },
            ),
            ToolDefinition(
                name="web_fetch",
                description="Fetch content from a URL and extract readable text. Use this to read web pages, documentation, articles, or any publicly accessible URL. Returns the text content of the page with HTML stripped.",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to fetch (must be http or https)",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Optional: guidance for what information to focus on",
                        },
                        "extract_links": {
                            "type": "boolean",
                            "description": "Whether to also extract links from the page (default: false)",
                        },
                        "max_length": {
                            "type": "integer",
                            "description": "Maximum content length to return (default: 50000)",
                        },
                    },
                    "required": ["url"],
                },
            ),
            ToolDefinition(
                name="web_search",
                description="Search the web for information. Use this to find relevant URLs, research topics, look up documentation, or discover resources. Returns a list of search results with titles, URLs, and snippets.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results to return (default: 10)",
                        },
                    },
                    "required": ["query"],
                },
            ),
        ]

        # Filter by plan if provided
        if plan and plan.allowed_tools:
            tools = [t for t in tools if t.name in plan.allowed_tools]

        return tools

    def _compress_tool_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Compress a tool result for context efficiency."""
        from packages.shared_protocol.types import ToolOutputLimits
        limits = ToolOutputLimits()

        tool_name = result.get("tool_name", "")
        limit = limits.get_limit(tool_name)

        compressed = {
            "tool_call_id": result.get("tool_call_id"),
            "tool_name": tool_name,
            "status": result.get("status"),
        }

        # Compress the result content
        if result.get("status") == "success":
            inner_result = result.get("result", {})
            if isinstance(inner_result, dict):
                # Compress content fields
                for key in ("content", "output", "matches"):
                    if key in inner_result:
                        value = inner_result[key]
                        if isinstance(value, str) and len(value) > limit:
                            inner_result[key] = value[:limit] + f"\n... (truncated, {len(value)} chars total)"
                        elif isinstance(value, list) and len(value) > 50:
                            inner_result[key] = value[:50]
                            inner_result["_truncated"] = True
                compressed["result"] = inner_result
            else:
                compressed["result"] = inner_result
        else:
            compressed["error"] = result.get("error", "")[:500]

        return compressed

    async def _save_checkpoint(self) -> str:
        """Save a context checkpoint to the server."""
        checkpoint_id = generate_checkpoint_id()

        # Build checkpoint payload
        # Use context packer state if available
        if self._context_packer:
            packer_state = self._context_packer.serialize_state()
            payload_data = {
                "messages": packer_state.get("conversation", []),
                "tool_results": [],
                "rolling_summary": packer_state.get("summary", {}),
                "pinned_facts": packer_state.get("facts", []),
                "file_slices": [],
                "slice_requests": packer_state.get("slice_requests", []),
                "token_count": self._total_tokens,
                "turn_count": self._turn_count,
            }
        else:
            payload_data = {
                "messages": [
                    {
                        "role": turn.role,
                        "content": turn.content,
                        "tool_calls": turn.tool_calls,
                    }
                    for turn in self._turns
                ],
                "tool_results": [
                    result
                    for turn in self._turns
                    for result in turn.tool_results
                ],
                "rolling_summary": self._rolling_summary.to_dict(),
                "pinned_facts": [f.to_dict() for f in self._pinned_facts],
                "file_slices": [s.to_dict() for s in self._file_slices],
                "token_count": self._total_tokens,
                "turn_count": self._turn_count,
            }

        # Serialize payload
        payload_bytes = json.dumps(payload_data).encode()

        # Compute hash
        import hashlib
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()

        # Encrypt with session key if available
        if self._key_manager:
            try:
                encrypted_payload = self._key_manager.encrypt_checkpoint(payload_bytes)
            except Exception as e:
                logger.error(f"Failed to encrypt checkpoint: {e}")
                raise RuntimeError(
                    f"Checkpoint encryption failed - cannot save session state securely: {e}"
                ) from e
        else:
            # No encryption in dev mode - this is fine for local development
            logger.debug("Checkpoint saved without encryption (dev mode)")
            encrypted_payload = payload_bytes

        # Send checkpoint via WebSocket client
        await self._ws.send_context_checkpoint(
            checkpoint_id=checkpoint_id,
            parent_id=self._context_ref,
            encrypted_payload=encrypted_payload,
            payload_hash=payload_hash,
            token_count=self._total_tokens,
            is_full=self._context_ref is None,  # Full if no previous checkpoint
        )

        self._context_ref = checkpoint_id

        # Mark checkpoint in context packer
        if self._context_packer:
            self._context_packer.mark_checkpoint()

        return checkpoint_id

    def _estimate_pack_tokens(self) -> int:
        """Estimate token count for context pack."""
        tokens = 0

        # Summary
        summary_text = json.dumps(self._rolling_summary.to_dict())
        tokens += len(summary_text) // 4

        # Facts
        for fact in self._pinned_facts:
            tokens += len(fact.value) // 4 + 10

        # File slices
        for slice_ in self._file_slices:
            tokens += len(slice_.content) // 4

        return tokens

    def _estimate_delta_tokens(
        self,
        messages: list[Message],
        tool_results: list[dict],
    ) -> int:
        """Estimate token count for delta."""
        tokens = 0

        for msg in messages:
            tokens += len(msg.content) // 4

        for result in tool_results:
            tokens += len(json.dumps(result)) // 4

        return tokens

    def _build_summary(self, success: bool, error: Optional[str]) -> str:
        """Build task completion summary."""
        parts = []

        if success:
            parts.append("Task completed successfully.")
        elif error:
            parts.append(f"Task failed: {error}")
        else:
            # Check what limit was hit
            if self._turn_count >= self.config.max_turns:
                parts.append("Task ended: turn limit reached.")
            elif self._total_tokens >= self.config.max_total_tokens:
                parts.append("Task ended: token limit reached.")
            else:
                parts.append("Task completed.")

        if self._files_modified:
            parts.append(f"Files modified: {', '.join(sorted(self._files_modified))}")

        parts.append(f"Turns: {self._turn_count}, Tokens: {self._total_tokens:,}")

        return " ".join(parts)

    def _reset_state(self) -> None:
        """Reset state for new task."""
        self._turns = []
        self._context_ref = None
        self._turn_count = 0
        self._total_tokens = 0
        self._tool_call_count = 0
        self._files_modified = set()
        self._rolling_summary = RollingSummary(
            task_description="",
            files_modified=[],
            files_read=[],
            key_decisions=[],
            errors_encountered=[],
            current_state="",
            turn_count=0,
        )
        self._pinned_facts = []
        self._file_slices = []

    # =========================================================================
    # v4 Enhancement Methods
    # =========================================================================

    async def _execute_tools_parallel(
        self,
        tool_calls: list[ToolCallResult],
        task: str,
    ) -> AsyncIterator[tuple[ToolCallResult, dict, str, Optional[OrchestrationEvent]]]:
        """Execute tool calls with parallel execution when safe.

        Args:
            tool_calls: Tool calls to execute
            task: Current task description for council context

        Yields:
            Tuples of (tool_call, result, status, optional_event)
        """
        if not tool_calls:
            return

        # Convert to ToolCall format for parallel executor
        calls = [
            ToolCall(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                arguments=tc.arguments,
            )
            for tc in tool_calls
        ]

        # Use parallel executor if available
        if self._parallel_executor and len(calls) > 1:
            results = await self._parallel_executor.execute_batch(calls)

            for i, exec_result in enumerate(results):
                tool_call = tool_calls[i]

                if exec_result.error:
                    result = {"error": exec_result.error}
                    status = "error"
                elif exec_result.result:
                    result = exec_result.result.output if exec_result.result.output else {}
                    status = "success" if exec_result.result.success else "error"
                    if exec_result.result.error:
                        result = {"error": exec_result.result.error, "output": result}
                else:
                    result = {}
                    status = "error"

                yield tool_call, result, status, None
        else:
            # Sequential execution
            for tool_call in tool_calls:
                result, status, event = await self._execute_single_tool(tool_call, task)
                yield tool_call, result, status, event

    async def _execute_single_tool(
        self,
        tool_call: ToolCallResult,
        task: str,
    ) -> tuple[dict, str, Optional[OrchestrationEvent]]:
        """Execute a single tool with v4 enhancements.

        Includes:
        - Council review for write operations
        - Pre-flight checks after writes
        - Retry logic for transient failures

        Returns:
            Tuple of (result, status, optional_event)
        """
        event = None

        # Council review for write operations
        if self._council and tool_call.tool_name in ("edit_file", "write_file"):
            council_result = await self._run_council_review(tool_call, task)
            if council_result and not council_result.approved:
                # Council rejected - return feedback instead of executing
                event = OrchestrationEvent(
                    type=EventType.COUNCIL_REJECTION,
                    tool_name=tool_call.tool_name,
                    message=council_result.to_feedback(),
                )
                return {
                    "error": "Council rejected change",
                    "feedback": council_result.to_feedback(),
                }, "error", event

        # Execute with optional retry
        try:
            if self._retry_config:
                retry_op = RetryableOperation(self._retry_config)
                retry_result = await retry_op.execute(
                    self.tools.execute,
                    tool_call.tool_name,
                    tool_call.arguments,
                )
                if retry_result.success:
                    tool_result = retry_result.result
                else:
                    return {"error": retry_result.final_error}, "error", None
            else:
                tool_result = await self.tools.execute(
                    tool_call.tool_name,
                    tool_call.arguments,
                )

            # Extract result
            result = tool_result.output if tool_result.output else {}
            status = "success" if tool_result.success else "error"
            if tool_result.error:
                result = {"error": tool_result.error, "output": result}

            # Pre-flight check for write operations
            if self._preflight and tool_call.tool_name in ("edit_file", "write_file") and status == "success":
                preflight_result = await self._run_preflight_check(tool_call)
                if preflight_result and not preflight_result.passed:
                    event = OrchestrationEvent(
                        type=EventType.PREFLIGHT_FAILURE,
                        tool_name=tool_call.tool_name,
                        message=preflight_result.to_feedback(),
                    )
                    # Add preflight feedback to result
                    result["preflight_feedback"] = preflight_result.to_feedback()

            # Bug fix 3.5: Security scan for write operations
            if self._security_scanner and tool_call.tool_name in ("edit_file", "write_file") and status == "success":
                security_event = await self._run_security_scan(tool_call)
                if security_event:
                    # Prioritize security issues over preflight failures
                    event = security_event
                    result["security_feedback"] = security_event.message

            return result, status, event

        except Exception as e:
            self._rolling_summary.errors_encountered.append(
                f"{tool_call.tool_name}: {str(e)}"
            )
            return {"error": str(e)}, "error", None

    async def _run_council_review(
        self,
        tool_call: ToolCallResult,
        task: str,
    ) -> Optional[CouncilResult]:
        """Run council review for a write operation."""
        if not self._council:
            return None

        try:
            file_path = tool_call.arguments.get("file_path", "")

            if tool_call.tool_name == "edit_file":
                return await self._council.evaluate_edit(
                    task=task,
                    file_path=file_path,
                    old_string=tool_call.arguments.get("old_string", ""),
                    new_string=tool_call.arguments.get("new_string", ""),
                    llm_callback=self._council_llm_callback,
                )
            else:  # write_file
                return await self._council.evaluate_proposed_change(
                    task=task,
                    proposed_code=tool_call.arguments.get("content", ""),
                    file_path=file_path,
                    llm_callback=self._council_llm_callback,
                )
        except Exception as e:
            logger.warning(f"Council review failed: {e}")
            return None

    async def _council_llm_callback(
        self,
        system_prompt: str,
        prompt: str,
        model_tier: str,
    ) -> str:
        """LLM callback for council perspectives."""
        # Use agent_llm_callback with appropriate formatting
        result = await self.agent_llm_callback(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            model_tier=model_tier,
        )
        return result.get("content", "")

    async def _run_preflight_check(
        self,
        tool_call: ToolCallResult,
    ) -> Optional[PreflightResult]:
        """Run pre-flight checks after a write operation."""
        if not self._preflight:
            return None

        try:
            from pathlib import Path
            file_path = Path(tool_call.arguments.get("file_path", ""))
            content = tool_call.arguments.get("content") or tool_call.arguments.get("new_string")

            return await self._preflight.check_file(file_path, content)
        except Exception as e:
            logger.warning(f"Preflight check failed: {e}")
            return None

    async def _run_security_scan(
        self,
        tool_call: ToolCallResult,
    ) -> Optional[OrchestrationEvent]:
        """Bug fix 3.5: Run security scan after a write operation.

        Args:
            tool_call: The tool call that was executed

        Returns:
            OrchestrationEvent if security issues found, None otherwise
        """
        if not self._security_scanner:
            return None

        try:
            from pathlib import Path

            file_path = tool_call.arguments.get("file_path", "")
            if not file_path:
                return None

            # Scan the file
            vulnerabilities = await self._security_scanner.scan_file(file_path)

            if vulnerabilities:
                # Build feedback message
                issues = []
                for vuln in vulnerabilities:
                    severity = vuln.severity.value.upper()
                    issues.append(f"[{severity}] {vuln.title}: {vuln.remediation}")

                feedback = (
                    f"**SECURITY ISSUES FOUND** in {file_path}:\n"
                    + "\n".join(f"- {issue}" for issue in issues[:5])  # Limit to top 5
                )

                logger.warning(f"Security issues found in {file_path}: {len(vulnerabilities)} vulnerabilities")

                return OrchestrationEvent(
                    type=EventType.SECURITY_ISSUE,
                    tool_name=tool_call.tool_name,
                    file_path=file_path,
                    message=feedback,
                    result={"vulnerabilities": [v.to_dict() for v in vulnerabilities[:5]]},
                )

            return None

        except Exception as e:
            logger.warning(f"Security scan failed: {e}")
            return None

    def _start_task_tracing(self, task_id: str, task_description: str) -> None:
        """Start tracing for a task."""
        if self._tracer:
            self._tracer.start_task(task_id, task_description)

    def _end_task_tracing(self, success: bool, error: Optional[str] = None) -> None:
        """End tracing for a task."""
        if self._tracer:
            outcome = TaskOutcome.SUCCESS if success else TaskOutcome.FAILURE
            if error and "cancelled" in error.lower():
                outcome = TaskOutcome.CANCELLED
            elif error and "timeout" in error.lower():
                outcome = TaskOutcome.TIMEOUT
            self._tracer.end_task(outcome, error)

    def _trace_turn_start(self, turn_number: int) -> None:
        """Trace turn start."""
        if self._tracer:
            self._tracer.start_turn(turn_number)

    def _trace_turn_end(
        self,
        input_tokens: int,
        output_tokens: int,
        response: str,
        error: Optional[str] = None,
    ) -> None:
        """Trace turn end."""
        if self._tracer:
            self._tracer.end_turn(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_preview=response[:200] if response else "",
                error=error,
            )

    def _trace_tool_call(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict,
        success: bool,
        result_summary: str,
        duration_ms: int,
        error: Optional[str] = None,
    ) -> None:
        """Trace a tool call."""
        if self._tracer:
            self._tracer.record_tool_call(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                success=success,
                result_summary=result_summary,
                duration_ms=duration_ms,
                error=error,
            )

    def get_execution_metrics(self) -> dict:
        """Get execution metrics from v4 components."""
        metrics = {}

        if self._parallel_executor:
            metrics["parallel_executor"] = self._parallel_executor.get_metrics()

        if self._tracer:
            metrics["tracer"] = self._tracer.get_summary()

        if self._council:
            metrics["council"] = self._council.get_evaluation_stats()

        return metrics

    async def agent_llm_callback(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model_tier: str,
    ) -> dict[str, Any]:
        """LLM callback for agent execution.

        This method provides a simple interface for agents to make LLM requests
        through the existing WebSocket connection.

        Args:
            system_prompt: The agent's system prompt
            messages: Conversation messages
            tools: Tool definitions for the agent
            model_tier: Model tier (fast, standard, advanced)

        Returns:
            Dict with 'content' and optional 'tool_calls'
        """
        # Reset response state
        self._response_event.clear()
        self._response_content = ""
        self._response_tool_calls = []
        self._response_error = None

        # Generate request ID
        request_id = generate_request_id()
        self._active_request_id = request_id

        # Build delta from messages (agent provides full message history)
        delta = {
            "messages": messages,
            "tool_results_compressed": [],
            "token_count": 0,
        }

        # Build minimal context pack with system prompt
        context_pack = {
            "rolling_summary": {},
            "pinned_facts": [],
            "file_slices": [],
            "system_prompt": system_prompt,
            "token_count": 0,
        }

        # Send request via WebSocket client
        await self._ws.send_llm_request(
            request_id=request_id,
            context_ref=None,  # Agents don't use checkpoints
            delta=delta,
            context_pack=context_pack,
            tools=tools,
            model_tier=model_tier,
            max_tokens=8192,
            temperature=0.7,
        )

        # Wait for response
        try:
            await asyncio.wait_for(
                self._response_event.wait(),
                timeout=self.config.llm_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            raise TimeoutError("Agent LLM request timed out")

        self._active_request_id = None

        if self._response_error:
            raise RuntimeError(f"Agent LLM error: {self._response_error}")

        # Return response in format expected by AgentRunner
        result: dict[str, Any] = {"content": self._response_content}

        if self._response_tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tc.tool_name,
                        "arguments": tc.args if isinstance(tc.args, str) else json.dumps(tc.args),
                    },
                }
                for tc in self._response_tool_calls
            ]

        return result
