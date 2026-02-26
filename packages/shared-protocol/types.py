"""Glock protocol types - shared between client, server, and runtime."""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


# =============================================================================
# ID Generation Utilities
# =============================================================================

def generate_id(length: int = 24) -> str:
    """Generate a random alphanumeric ID."""
    return secrets.token_hex(length // 2)


def generate_session_id() -> str:
    """Generate a session ID with sess_ prefix."""
    return f"sess_{generate_id(24)}"


def generate_task_id() -> str:
    """Generate a task ID with task_ prefix."""
    return f"task_{generate_id(24)}"


def generate_plan_id() -> str:
    """Generate a plan ID with plan_ prefix."""
    return f"plan_{generate_id(24)}"


def generate_message_id() -> str:
    """Generate a UUID message ID."""
    return str(uuid.uuid4())


def generate_client_id() -> str:
    """Generate a client ID with cli_ prefix."""
    return f"cli_{generate_id(16)}"


def generate_runtime_id() -> str:
    """Generate a runtime ID with rt_ prefix."""
    return f"rt_{generate_id(16)}"


def generate_tool_id() -> str:
    """Generate a tool request ID with tr_ prefix."""
    return f"tr_{generate_id(16)}"


def generate_checkpoint_id() -> str:
    """Generate a checkpoint ID with cp_ prefix."""
    return f"cp_{generate_id(24)}"


def generate_request_id() -> str:
    """Generate an LLM request ID with req_ prefix."""
    return f"req_{generate_id(16)}"


# =============================================================================
# Message Types
# =============================================================================

class MessageType(str, Enum):
    """All WebSocket message types."""
    # Session lifecycle
    SESSION_START = "session_start"
    SESSION_READY = "session_ready"
    SESSION_END = "session_end"
    SESSION_ERROR = "session_error"

    # Resume/reconnect
    RESUME_REQUEST = "resume_request"
    RESUME_FROM_SEQ = "resume_from_seq"

    # Heartbeat
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"
    WARNING = "warning"

    # Task lifecycle
    TASK_START = "task_start"
    TASK_DELTA = "task_delta"
    TASK_STATUS = "task_status"
    TASK_COMPLETE = "task_complete"
    TASK_FAILED = "task_failed"
    TASK_BUDGET_UPDATE = "task_budget_update"

    # Cancel
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_ACK = "cancel_ack"

    # Plan
    COMPILED_PLAN = "compiled_plan"

    # Tool execution
    TOOL_REQUEST = "tool_request"
    TOOL_RESULT = "tool_result"
    TOOL_ERROR = "tool_error"

    # Approvals
    TOOL_APPROVAL_REQUEST = "tool_approval_request"
    TOOL_APPROVAL_RESPONSE = "tool_approval_response"
    DEPLOY_APPROVAL_REQUEST = "deploy_approval_request"
    DEPLOY_APPROVAL_RESPONSE = "deploy_approval_response"

    # Validation
    VALIDATION_REQUEST = "validation_request"
    VALIDATION_RESULT = "validation_result"

    # Healer/retry
    RETRY_PLAN_READY = "retry_plan_ready"
    RETRY_EXHAUSTED = "retry_exhausted"

    # UI
    DIFF_PREVIEW = "diff_preview"
    CHECKPOINT_CREATED = "checkpoint_created"

    # Runtime binding (internal) - DEPRECATED in Model B
    RUNTIME_BIND = "runtime_bind"
    BIND_CONFIRMED = "bind_confirmed"
    BIND_ERROR = "bind_error"
    CLIENT_DISCONNECTED = "client_disconnected"
    RUNTIME_RECOVERED = "runtime_recovered"

    # ==========================================================================
    # Model B: LLM Proxy Messages
    # ==========================================================================
    LLM_REQUEST = "llm_request"              # Client → Server: Request LLM completion
    LLM_DELTA = "llm_delta"                  # Server → Client: Streaming response chunk
    LLM_RESPONSE_END = "llm_response_end"    # Server → Client: Response complete
    LLM_ERROR = "llm_error"                  # Server → Client: LLM error
    LLM_CANCEL = "llm_cancel"                # Client → Server: Cancel request

    # Model B: Context Checkpoint Messages
    CONTEXT_CHECKPOINT = "context_checkpoint"        # Client → Server: Store checkpoint
    CONTEXT_CHECKPOINT_ACK = "context_checkpoint_ack"  # Server → Client: Checkpoint stored

    # Model B: Enhanced Session Messages
    SESSION_SYNC = "session_sync"            # Server → Client: Session state on resume
    SESSION_RESUME = "session_resume"        # Client → Server: Resume session


# =============================================================================
# Message Envelope
# =============================================================================

@dataclass
class MessageEnvelope:
    """WebSocket message envelope with seq/ack for reliable delivery."""
    v: int  # Protocol version (always 1)
    type: MessageType
    session_id: str
    message_id: str
    seq: int  # Sender's sequence number
    ack: int  # Last received seq from peer
    timestamp_ms: int
    payload: dict[str, Any]
    client_id: Optional[str] = None
    task_id: Optional[str] = None
    idempotency_key: Optional[str] = None

    @classmethod
    def create(
        cls,
        msg_type: MessageType,
        session_id: str,
        payload: dict[str, Any],
        seq: int = 0,
        ack: int = 0,
        client_id: Optional[str] = None,
        task_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> MessageEnvelope:
        """Create a new message envelope."""
        return cls(
            v=1,
            type=msg_type,
            session_id=session_id,
            message_id=generate_message_id(),
            seq=seq,
            ack=ack,
            timestamp_ms=int(time.time() * 1000),
            payload=payload,
            client_id=client_id,
            task_id=task_id,
            idempotency_key=idempotency_key or generate_id(16),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "v": self.v,
            "type": self.type.value if isinstance(self.type, MessageType) else self.type,
            "session_id": self.session_id,
            "client_id": self.client_id,
            "task_id": self.task_id,
            "message_id": self.message_id,
            "seq": self.seq,
            "ack": self.ack,
            "timestamp_ms": self.timestamp_ms,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageEnvelope:
        """Create from dictionary."""
        return cls(
            v=data["v"],
            type=MessageType(data["type"]) if isinstance(data["type"], str) else data["type"],
            session_id=data["session_id"],
            message_id=data["message_id"],
            seq=data["seq"],
            ack=data["ack"],
            timestamp_ms=data["timestamp_ms"],
            payload=data["payload"],
            client_id=data.get("client_id"),
            task_id=data.get("task_id"),
            idempotency_key=data.get("idempotency_key"),
        )


# =============================================================================
# Session Payloads
# =============================================================================

@dataclass
class SessionStartPayload:
    """Client → Server: Start a new session."""
    workspace_label: str
    client_version: str
    repo_fingerprint: Optional[str] = None
    branch_name: Optional[str] = None
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_label": self.workspace_label,
            "client_version": self.client_version,
            "repo_fingerprint": self.repo_fingerprint,
            "branch_name": self.branch_name,
            "capabilities": self.capabilities,
        }


@dataclass
class SessionCaps:
    """Session capability limits."""
    max_concurrent_tasks: int = 3
    max_retries: int = 3


@dataclass
class SessionReadyPayload:
    """Server → Client: Session is ready."""
    session_id: str
    user_id: str
    plan_tier: str  # free, pro, team, enterprise
    session_caps: SessionCaps
    server_time_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "plan_tier": self.plan_tier,
            "session_caps": {
                "max_concurrent_tasks": self.session_caps.max_concurrent_tasks,
                "max_retries": self.session_caps.max_retries,
            },
            "server_time_ms": self.server_time_ms,
        }


@dataclass
class ResumeRequestPayload:
    """Client → Server: Resume a disconnected session."""
    session_id: str
    last_server_seq_seen: int
    last_client_seq_sent: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "last_server_seq_seen": self.last_server_seq_seen,
            "last_client_seq_sent": self.last_client_seq_sent,
        }


@dataclass
class TaskState:
    """Current task state for resume."""
    task_id: str
    status: str


@dataclass
class ResumeFromSeqPayload:
    """Server → Client: Resume from sequence."""
    resume_seq: int
    replay_messages: list[dict[str, Any]]
    task_state: Optional[TaskState] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "resume_seq": self.resume_seq,
            "replay_messages": self.replay_messages,
            "task_state": {
                "task_id": self.task_state.task_id,
                "status": self.task_state.status,
            } if self.task_state else None,
        }


# =============================================================================
# Task Payloads
# =============================================================================

@dataclass
class GitStatus:
    """Git repository status."""
    branch: str
    dirty: bool
    ahead: int = 0
    behind: int = 0


@dataclass
class TaskContext:
    """Context for task execution."""
    cwd: str
    active_files: list[str] = field(default_factory=list)
    git_status: Optional[GitStatus] = None
    available_validations: list[str] = field(default_factory=list)


@dataclass
class TaskStartPayload:
    """Client → Server: Start a new task."""
    prompt: str
    context: Optional[TaskContext] = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"prompt": self.prompt}
        if self.context:
            result["context"] = {
                "cwd": self.context.cwd,
                "active_files": self.context.active_files,
                "git_status": {
                    "branch": self.context.git_status.branch,
                    "dirty": self.context.git_status.dirty,
                } if self.context.git_status else None,
                "available_validations": self.context.available_validations,
            }
        return result


class DeltaType(str, Enum):
    """Types of task delta updates."""
    TEXT = "text"
    THINKING = "thinking"
    TOOL_USE = "tool_use"


@dataclass
class TaskDeltaPayload:
    """Server → Client: Streaming task update."""
    delta_type: DeltaType
    content: str
    tokens_used: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "delta_type": self.delta_type.value,
            "content": self.content,
            "tokens_used": self.tokens_used,
        }


@dataclass
class TaskCompletePayload:
    """Server → Client: Task completed."""
    summary: str
    files_modified: list[str]
    validations_passed: list[str]
    total_tokens: int
    retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "files_modified": self.files_modified,
            "validations_passed": self.validations_passed,
            "total_tokens": self.total_tokens,
            "retry_count": self.retry_count,
        }


# =============================================================================
# Tool Payloads
# =============================================================================

@dataclass
class ToolRequestPayload:
    """Server → Client: Request tool execution."""
    tool_id: str
    tool_name: str
    args: dict[str, Any]
    requires_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "tool_name": self.tool_name,
            "args": self.args,
            "requires_approval": self.requires_approval,
        }


class ToolStatus(str, Enum):
    """Tool execution status."""
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class ToolResultPayload:
    """Client → Server: Tool execution result."""
    tool_id: str
    status: ToolStatus
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    duration_ms: int = 0
    output_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "output_truncated": self.output_truncated,
        }


@dataclass
class ToolApprovalRequestPayload:
    """Server → Client: Request approval for tool."""
    approval_id: str
    tool_name: str
    args: dict[str, Any]
    risk_level: str  # low, medium, high
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool_name": self.tool_name,
            "args": self.args,
            "risk_level": self.risk_level,
            "reason": self.reason,
        }


@dataclass
class ToolApprovalResponsePayload:
    """Client → Server: Approval response."""
    approval_id: str
    approved: bool
    user_modified_args: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "approved": self.approved,
            "user_modified_args": self.user_modified_args,
        }


# =============================================================================
# Validation Payloads
# =============================================================================

@dataclass
class ValidationStep:
    """A single validation step."""
    name: str
    command: str
    timeout_ms: int = 120000


@dataclass
class ValidationRequestPayload:
    """Server → Client: Request validation."""
    task_id: str
    attempt_no: int
    steps: list[ValidationStep]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "attempt_no": self.attempt_no,
            "steps": [
                {"name": s.name, "command": s.command, "timeout_ms": s.timeout_ms}
                for s in self.steps
            ],
        }


@dataclass
class TestFailure:
    """A single test failure."""
    test_name: str
    file: str
    line: int
    expected: str
    actual: str
    message: str


class ValidationStatus(str, Enum):
    """Validation step status."""
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class ValidationResultPayload:
    """Client → Server: Validation result."""
    task_id: str
    attempt_no: int
    step_name: str
    status: ValidationStatus
    output_summary: str = ""
    failures: list[TestFailure] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "attempt_no": self.attempt_no,
            "step_name": self.step_name,
            "status": self.status.value,
            "output_summary": self.output_summary,
            "failures": [
                {
                    "test_name": f.test_name,
                    "file": f.file,
                    "line": f.line,
                    "expected": f.expected,
                    "actual": f.actual,
                    "message": f.message,
                }
                for f in self.failures
            ],
        }


# =============================================================================
# Compiled Plan
# =============================================================================

@dataclass
class PlanBudgets:
    """Execution budget limits."""
    max_iterations: int = 50
    max_tool_calls: int = 100
    max_retries: int = 2
    timeout_ms: int = 600000


@dataclass
class ApprovalRule:
    """Per-tool approval rules."""
    patterns: list[str] = field(default_factory=list)
    require_approval: bool = False


@dataclass
class CompiledPlan:
    """Server-compiled and signed execution plan."""
    plan_id: str
    session_id: str
    task_id: str
    issued_at: datetime
    expires_at: datetime
    signature: str
    objective: str
    execution_mode: str  # direct, escalated, retry
    allowed_tools: list[str]
    signature_alg: str = "ed25519"
    kid: Optional[str] = None
    payload_hash: Optional[str] = None
    workspace_scope: Optional[str] = None
    edit_scope: list[str] = field(default_factory=list)
    validation_steps: list[str] = field(default_factory=list)
    approval_requirements: dict[str, ApprovalRule] = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)
    budgets: PlanBudgets = field(default_factory=PlanBudgets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "signature": self.signature,
            "signature_alg": self.signature_alg,
            "kid": self.kid,
            "payload_hash": self.payload_hash,
            "objective": self.objective,
            "execution_mode": self.execution_mode,
            "allowed_tools": self.allowed_tools,
            "workspace_scope": self.workspace_scope,
            "edit_scope": self.edit_scope,
            "validation_steps": self.validation_steps,
            "approval_requirements": {
                k: {"patterns": v.patterns, "require_approval": v.require_approval}
                for k, v in self.approval_requirements.items()
            },
            "risk_flags": self.risk_flags,
            "budgets": {
                "max_iterations": self.budgets.max_iterations,
                "max_tool_calls": self.budgets.max_tool_calls,
                "max_retries": self.budgets.max_retries,
                "timeout_ms": self.budgets.timeout_ms,
            },
        }


# =============================================================================
# Model B: LLM Proxy Payloads
# =============================================================================

@dataclass
class ToolDefinition:
    """Tool definition for LLM context."""
    name: str
    description: str
    parameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class PinnedFact:
    """A pinned fact for context persistence."""
    key: str
    value: str
    category: str  # file_path, function_name, error_solution, user_preference, constraint
    importance: float = 1.0
    use_count: int = 0
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "category": self.category,
            "importance": self.importance,
            "use_count": self.use_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }


@dataclass
class FileSlice:
    """A slice of a file for context."""
    file_path: str
    start_line: int
    end_line: int
    content: str
    reason: str  # grep_hit, traceback, changed_hunk, function_def, call_site

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content": self.content,
            "reason": self.reason,
        }


@dataclass
class RollingSummary:
    """Rolling summary of session progress."""
    task_description: str
    files_modified: list[str]
    files_read: list[str]
    key_decisions: list[str]
    errors_encountered: list[str]
    current_state: str
    turn_count: int
    last_updated_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_description": self.task_description,
            "files_modified": self.files_modified,
            "files_read": self.files_read,
            "key_decisions": self.key_decisions,
            "errors_encountered": self.errors_encountered,
            "current_state": self.current_state,
            "turn_count": self.turn_count,
            "last_updated_at": self.last_updated_at.isoformat() if self.last_updated_at else None,
        }


@dataclass
class ContextPack:
    """Stable context elements that persist across turns."""
    rolling_summary: RollingSummary
    pinned_facts: list[PinnedFact]
    file_slices: list[FileSlice]
    token_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rolling_summary": self.rolling_summary.to_dict(),
            "pinned_facts": [f.to_dict() for f in self.pinned_facts],
            "file_slices": [s.to_dict() for s in self.file_slices],
            "token_count": self.token_count,
        }


@dataclass
class Message:
    """A conversation message."""
    role: str  # user, assistant, system, tool
    content: str
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    name: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        if self.name:
            result["name"] = self.name
        return result


@dataclass
class ContextDelta:
    """Delta of new context since last checkpoint."""
    messages: list[Message]
    tool_results_compressed: list[dict[str, Any]]
    token_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [m.to_dict() for m in self.messages],
            "tool_results_compressed": self.tool_results_compressed,
            "token_count": self.token_count,
        }


@dataclass
class LLMRequestPayload:
    """Client → Server: Request LLM completion."""
    request_id: str
    context_ref: Optional[str]  # Reference to stored checkpoint
    delta: ContextDelta          # New info since checkpoint
    context_pack: ContextPack    # Stable summary + facts + slices
    tools: list[ToolDefinition]
    model_tier: str              # fast/standard/advanced
    max_tokens: int = 8192
    temperature: float = 0.7

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "context_ref": self.context_ref,
            "delta": self.delta.to_dict(),
            "context_pack": self.context_pack.to_dict(),
            "tools": [t.to_dict() for t in self.tools],
            "model_tier": self.model_tier,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }


@dataclass
class LLMDeltaPayload:
    """Server → Client: Streaming LLM response chunk."""
    request_id: str
    delta_type: str  # text, thinking, tool_use
    content: str
    index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "delta_type": self.delta_type,
            "content": self.content,
            "index": self.index,
        }


@dataclass
class ToolCallResult:
    """Tool call from LLM response."""
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
        }


@dataclass
class LLMResponseEndPayload:
    """Server → Client: LLM response complete."""
    request_id: str
    new_context_ref: str         # New checkpoint ID
    finish_reason: str           # stop, tool_use, length, error
    input_tokens: int
    output_tokens: int
    tool_calls: list[ToolCallResult] = field(default_factory=list)
    content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "new_context_ref": self.new_context_ref,
            "finish_reason": self.finish_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "content": self.content,
        }


@dataclass
class LLMErrorPayload:
    """Server → Client: LLM error."""
    request_id: str
    error_code: str  # rate_limit, context_length, provider_error, timeout
    error_message: str
    retryable: bool = False
    retry_after_ms: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "retry_after_ms": self.retry_after_ms,
        }


@dataclass
class LLMCancelPayload:
    """Client → Server: Cancel LLM request."""
    request_id: str
    reason: str = "user_cancelled"

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "reason": self.reason,
        }


# =============================================================================
# Model B: Context Checkpoint Payloads
# =============================================================================

@dataclass
class ContextCheckpointPayload:
    """Client → Server: Store context checkpoint."""
    checkpoint_id: str
    parent_id: Optional[str]
    encrypted_payload: str       # Base64-encoded encrypted data
    payload_hash: str            # SHA-256 hash for verification
    token_count: int
    turn_count: int
    is_full: bool = False        # Full snapshot vs delta

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "parent_id": self.parent_id,
            "encrypted_payload": self.encrypted_payload,
            "payload_hash": self.payload_hash,
            "token_count": self.token_count,
            "turn_count": self.turn_count,
            "is_full": self.is_full,
        }


@dataclass
class ContextCheckpointAckPayload:
    """Server → Client: Checkpoint stored acknowledgment."""
    checkpoint_id: str
    stored_at: datetime
    expires_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "stored_at": self.stored_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


# =============================================================================
# Model B: Enhanced Session Payloads
# =============================================================================

@dataclass
class SessionResumePayload:
    """Client → Server: Resume a session."""
    session_id: str
    client_state_hash: str       # Hash of local state for verification
    expected_context_ref: str    # Last known checkpoint

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "client_state_hash": self.client_state_hash,
            "expected_context_ref": self.expected_context_ref,
        }


@dataclass
class SessionSyncPayload:
    """Server → Client: Session state on resume."""
    session_id: str
    status: str                  # resumed, stale, ended
    last_context_ref: str
    turn_count: int
    task_status: Optional[str] = None
    needs_resync: bool = False
    resync_from: Optional[str] = None  # Checkpoint to resync from if needed
    total_tokens: int = 0
    workspace_hash: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "last_context_ref": self.last_context_ref,
            "turn_count": self.turn_count,
            "task_status": self.task_status,
            "needs_resync": self.needs_resync,
            "resync_from": self.resync_from,
            "total_tokens": self.total_tokens,
            "workspace_hash": self.workspace_hash,
        }


# =============================================================================
# Model B: Token Budget Types
# =============================================================================

@dataclass
class TokenBudget:
    """Token budget allocation for context packing."""
    total_context: int = 100_000
    system_prompt: int = 2_000
    pinned_facts: int = 3_000        # ~30 items
    rolling_summary: int = 4_000
    file_context: int = 15_000
    tool_results: int = 8_000
    conversation: int = 10_000
    delta: int = 5_000
    completion_reserve: int = 8_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_context": self.total_context,
            "system_prompt": self.system_prompt,
            "pinned_facts": self.pinned_facts,
            "rolling_summary": self.rolling_summary,
            "file_context": self.file_context,
            "tool_results": self.tool_results,
            "conversation": self.conversation,
            "delta": self.delta,
            "completion_reserve": self.completion_reserve,
        }


@dataclass
class ToolOutputLimits:
    """Per-tool output character limits for compression."""
    read_file: int = 4000
    grep: int = 2000
    bash: int = 2500
    glob: int = 1500
    default: int = 3000

    def get_limit(self, tool_name: str) -> int:
        """Get the limit for a specific tool."""
        return getattr(self, tool_name, self.default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_file": self.read_file,
            "grep": self.grep,
            "bash": self.bash,
            "glob": self.glob,
            "default": self.default,
        }


# =============================================================================
# Relay Protocol
# =============================================================================

class RelayDirection(str, Enum):
    """Relay message direction."""
    CLIENT_TO_RUNTIME = "client_to_runtime"
    RUNTIME_TO_CLIENT = "runtime_to_client"


@dataclass
class RelayEnvelope:
    """Wraps messages for client↔gateway↔runtime relay."""
    relay_type: str  # Always "relay"
    session_id: str
    direction: RelayDirection
    inner: MessageEnvelope
    gateway_id: Optional[str] = None
    relay_seq: int = 0
    relay_ts: int = 0

    @classmethod
    def create(
        cls,
        session_id: str,
        direction: RelayDirection,
        inner: MessageEnvelope,
        gateway_id: str,
    ) -> RelayEnvelope:
        return cls(
            relay_type="relay",
            session_id=session_id,
            direction=direction,
            inner=inner,
            gateway_id=gateway_id,
            relay_seq=0,
            relay_ts=int(time.time() * 1000),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "relay_type": self.relay_type,
            "session_id": self.session_id,
            "direction": self.direction.value,
            "gateway_id": self.gateway_id,
            "relay_seq": self.relay_seq,
            "relay_ts": self.relay_ts,
            "inner": self.inner.to_dict(),
        }


@dataclass
class RuntimeBindPayload:
    """Runtime → Gateway: Request to bind to session."""
    session_id: str
    bind_token: str
    runtime_id: str
    nonce: Optional[str] = None
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "bind_token": self.bind_token,
            "runtime_id": self.runtime_id,
            "nonce": self.nonce,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass
class BindConfirmedPayload:
    """Gateway → Runtime: Bind confirmed."""
    session_id: str
    runtime_id: str
    bound_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "runtime_id": self.runtime_id,
            "bound_at": self.bound_at.isoformat(),
        }


@dataclass
class ClientRouting:
    """Client connection routing info."""
    gateway_id: str
    conn_id: str
    connected_at: float
    status: str  # connected, disconnected


@dataclass
class RuntimeRouting:
    """Runtime connection routing info."""
    runtime_id: str
    conn_id: str
    status: str  # warming, ready, busy, draining, disconnected
    connected_at: float


@dataclass
class AllocationInfo:
    """Runtime allocation info."""
    host_id: str
    process_id: str
    bind_token: str
    allocated_at: float


@dataclass
class RoutingInfo:
    """Complete session routing information."""
    session_id: str
    client: Optional[ClientRouting] = None
    runtime: Optional[RuntimeRouting] = None
    allocation: Optional[AllocationInfo] = None


# =============================================================================
# Usage Events
# =============================================================================

class UsageEventType(str, Enum):
    """Usage event types."""
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TOKENS_USED = "tokens_used"
    TOOL_EXECUTED = "tool_executed"
    VALIDATION_RUN = "validation_run"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    RETRY_ATTEMPTED = "retry_attempted"
    PLAN_COMPILED = "plan_compiled"


class UsageUnit(str, Enum):
    """Usage measurement units."""
    COUNT = "count"
    TOKENS = "tokens"
    SECONDS = "seconds"
    BYTES = "bytes"
    VALIDATIONS = "validations"


@dataclass
class UsageEvent:
    """Usage tracking event."""
    event_type: UsageEventType
    user_id: str
    quantity: float
    unit: UsageUnit
    id: Optional[str] = None
    org_id: Optional[str] = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id or str(uuid.uuid4()),
            "event_type": self.event_type.value,
            "org_id": self.org_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "quantity": self.quantity,
            "unit": self.unit.value,
            "metadata": self.metadata,
            "created_at": (self.created_at or datetime.utcnow()).isoformat(),
        }


# =============================================================================
# Audit Events
# =============================================================================

class AuditAction(str, Enum):
    """Audit log actions."""
    RUNTIME_BIND = "runtime_bind"
    RUNTIME_REBIND = "runtime_rebind"
    RUNTIME_RELEASE = "runtime_release"
    CLIENT_ATTACH = "client_attach"
    CLIENT_DETACH = "client_detach"
    SESSION_PARK = "session_park"
    SESSION_UNPARK = "session_unpark"
    SESSION_END = "session_end"
    SECRET_ACCESS_GRANTED = "secret_access_granted"
    SECRET_ACCESS_DENIED = "secret_access_denied"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    PLAN_COMPILED = "plan_compiled"
    PLAN_EXPIRED = "plan_expired"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"
    RETRY_STARTED = "retry_started"
    RETRY_EXHAUSTED = "retry_exhausted"


class ActorType(str, Enum):
    """Actor types for audit."""
    USER = "user"
    SYSTEM = "system"
    HEALER = "healer"
    RUNTIME = "runtime"


class Severity(str, Enum):
    """Audit event severity."""
    INFO = "info"
    WARN = "warn"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class AuditEvent:
    """Audit log event."""
    action: AuditAction
    actor_type: ActorType
    actor_id: str
    user_id: str
    id: Optional[str] = None
    org_id: Optional[str] = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    severity: Severity = Severity.INFO
    details: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id or str(uuid.uuid4()),
            "org_id": self.org_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "action": self.action.value,
            "actor_type": self.actor_type.value,
            "actor_id": self.actor_id,
            "severity": self.severity.value,
            "details": self.details,
            "created_at": (self.created_at or datetime.utcnow()).isoformat(),
        }
