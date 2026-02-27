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
    ):
        self.tools = tool_broker
        self._ws = ws_client
        self._context_packer = context_packer
        self._key_manager = key_manager
        self.config = config or OrchestrationConfig()
        self.model_tier = self.config.model_tier

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

        # Set up message handlers
        self._setup_handlers()

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

                    # Process through context packer
                    if self._context_packer:
                        self._context_packer.process_assistant_response(
                            content=response_content,
                            tool_calls=[tc.to_dict() for tc in tool_calls] if tool_calls else None,
                        )

                    # Add assistant turn
                    self._turns.append(ConversationTurn(
                        role="assistant",
                        content=response_content,
                        tool_calls=[tc.to_dict() for tc in tool_calls],
                    ))

                # Process tool calls
                if tool_calls:
                    for tool_call in tool_calls:
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

                        # Execute tool
                        try:
                            tool_result = await self.tools.execute(
                                tool_call.tool_name,
                                tool_call.arguments,
                            )
                            # Extract output from ToolResult dataclass
                            result = tool_result.output if tool_result.output else {}
                            status = "success" if tool_result.success else "error"
                            if tool_result.error:
                                result = {"error": tool_result.error, "output": result}

                            # Track modified files
                            if tool_call.tool_name in ("edit_file", "write_file"):
                                file_path = tool_call.arguments.get("file_path", "")
                                if file_path:
                                    self._files_modified.add(file_path)
                                    if file_path not in self._rolling_summary.files_modified:
                                        self._rolling_summary.files_modified.append(file_path)

                            # Track read files
                            if tool_call.tool_name == "read_file":
                                file_path = tool_call.arguments.get("file_path", "")
                                if file_path and file_path not in self._rolling_summary.files_read:
                                    self._rolling_summary.files_read.append(file_path)

                        except Exception as e:
                            result = {"error": str(e)}
                            status = "error"
                            self._rolling_summary.errors_encountered.append(
                                f"{tool_call.tool_name}: {str(e)}"
                            )

                        # Emit tool end event
                        yield OrchestrationEvent(
                            type=EventType.TOOL_END,
                            tool_name=tool_call.tool_name,
                            result=result,
                        )

                        # Process tool result through context packer
                        if self._context_packer:
                            self._context_packer.process_tool_result(
                                tool_call_id=tool_call.tool_call_id,
                                tool_name=tool_call.tool_name,
                                args=tool_call.arguments,
                                result=result if isinstance(result, dict) else {"output": result},
                            )

                        # Add tool result to conversation
                        if self._turns and self._turns[-1].role == "assistant":
                            self._turns[-1].tool_results.append({
                                "tool_call_id": tool_call.tool_call_id,
                                "tool_name": tool_call.tool_name,
                                "status": status,
                                "result": result,
                            })

                    if error:
                        break

                else:
                    # No tool calls - task is complete
                    task_complete = True

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
        return {
            "messages": [
                {"role": m.role, "content": m.content}
                for m in delta.messages
            ],
            "tool_results_compressed": delta.tool_results_compressed,
            "token_count": delta.token_count,
        }

    def _pack_to_dict(self, pack: ContextPack) -> dict[str, Any]:
        """Convert ContextPack to dict."""
        return {
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
            # SECURITY WARNING: No encryption available
            # This should only happen in development/testing
            logger.warning(
                "SECURITY WARNING: Saving checkpoint WITHOUT encryption. "
                "This is not recommended for production use."
            )
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
