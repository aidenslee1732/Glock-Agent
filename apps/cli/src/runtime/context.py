"""
Context management for Glock CLI runtime.

Manages conversation context, tool outputs, and workspace state
with automatic compaction to stay within token limits.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, Any, Tuple
from pathlib import Path


logger = logging.getLogger(__name__)


class MessageRole(Enum):
    """Roles in conversation."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass
class Message:
    """A single message in the conversation."""
    role: MessageRole
    content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Optional metadata
    message_id: Optional[str] = None
    tool_id: Optional[str] = None
    tool_name: Optional[str] = None

    # Token estimation
    estimated_tokens: int = 0

    def __post_init__(self):
        if self.estimated_tokens == 0:
            # Rough estimate: ~4 chars per token
            self.estimated_tokens = len(self.content) // 4 + 1


@dataclass
class ToolOutput:
    """Output from a tool execution."""
    tool_id: str
    tool_name: str
    args: Dict[str, Any]
    output: str
    success: bool
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Compression state
    compressed: bool = False
    original_size: int = 0

    def compress(self, max_lines: int = 50) -> None:
        """Compress tool output by truncating."""
        if self.compressed:
            return

        self.original_size = len(self.output)
        lines = self.output.split('\n')

        if len(lines) > max_lines:
            # Keep first and last portions
            head = lines[:max_lines // 2]
            tail = lines[-max_lines // 2:]
            truncated = len(lines) - max_lines

            self.output = '\n'.join(head) + f"\n\n... ({truncated} lines truncated) ...\n\n" + '\n'.join(tail)
            self.compressed = True


@dataclass
class WorkspaceState:
    """Current state of the workspace."""
    root_path: Path
    current_dir: Path
    git_branch: Optional[str] = None
    git_status: Optional[Dict[str, Any]] = None

    # Active files being worked on
    active_files: List[str] = field(default_factory=list)

    # File content cache (for context)
    file_cache: Dict[str, str] = field(default_factory=dict)
    file_cache_tokens: int = 0

    def add_active_file(self, path: str, content: Optional[str] = None) -> None:
        """Add file to active files list."""
        if path not in self.active_files:
            self.active_files.append(path)

        if content:
            self.file_cache[path] = content
            self._update_cache_tokens()

    def remove_active_file(self, path: str) -> None:
        """Remove file from active files list."""
        if path in self.active_files:
            self.active_files.remove(path)
        if path in self.file_cache:
            del self.file_cache[path]
            self._update_cache_tokens()

    def _update_cache_tokens(self) -> None:
        """Update token count for file cache."""
        total = sum(len(content) for content in self.file_cache.values())
        self.file_cache_tokens = total // 4 + 1


@dataclass
class ContextConfig:
    """Configuration for context management."""
    # Token limits
    max_total_tokens: int = 100000
    max_conversation_tokens: int = 50000
    max_tool_output_tokens: int = 30000
    max_file_cache_tokens: int = 20000

    # Compaction thresholds
    compaction_threshold: float = 0.8  # Compact at 80% capacity

    # Retention
    keep_recent_messages: int = 20
    keep_recent_tool_outputs: int = 10

    # Tool output limits
    max_tool_output_lines: int = 100
    truncate_large_outputs: bool = True


class ContextManager:
    """
    Manages runtime context for the agentic loop.

    Handles:
    - Conversation history with automatic compaction
    - Tool output storage and compression
    - Workspace state tracking
    - Token budget management
    """

    def __init__(self, workspace_root: Path, config: Optional[ContextConfig] = None):
        self.config = config or ContextConfig()

        # Initialize workspace state
        self.workspace = WorkspaceState(
            root_path=workspace_root,
            current_dir=workspace_root
        )

        # Conversation history
        self.messages: List[Message] = []
        self.conversation_tokens: int = 0

        # Tool outputs
        self.tool_outputs: List[ToolOutput] = []
        self.tool_output_tokens: int = 0

        # Compaction state
        self.compaction_count: int = 0
        self.summaries: List[str] = []

    def add_message(
        self,
        role: MessageRole,
        content: str,
        **kwargs
    ) -> Message:
        """Add a message to the conversation."""
        message = Message(
            role=role,
            content=content,
            **kwargs
        )

        self.messages.append(message)
        self.conversation_tokens += message.estimated_tokens

        # Check if compaction needed
        if self._needs_compaction():
            self._compact()

        return message

    def add_tool_output(
        self,
        tool_id: str,
        tool_name: str,
        args: Dict[str, Any],
        output: str,
        success: bool
    ) -> ToolOutput:
        """Add tool output to context."""
        tool_output = ToolOutput(
            tool_id=tool_id,
            tool_name=tool_name,
            args=args,
            output=output,
            success=success
        )

        # Truncate large outputs
        if self.config.truncate_large_outputs:
            tool_output.compress(self.config.max_tool_output_lines)

        self.tool_outputs.append(tool_output)
        self.tool_output_tokens += len(tool_output.output) // 4 + 1

        # Check if compaction needed
        if self._needs_compaction():
            self._compact()

        return tool_output

    def _needs_compaction(self) -> bool:
        """Check if context needs compaction."""
        total_tokens = self._estimate_total_tokens()
        threshold = int(self.config.max_total_tokens * self.config.compaction_threshold)
        return total_tokens > threshold

    def _estimate_total_tokens(self) -> int:
        """Estimate total tokens in context."""
        return (
            self.conversation_tokens +
            self.tool_output_tokens +
            self.workspace.file_cache_tokens
        )

    def _compact(self) -> None:
        """Compact context to stay within limits."""
        logger.info(f"Starting context compaction (pass {self.compaction_count + 1})")

        # Stage 1: Compress old tool outputs
        self._compact_tool_outputs()

        # Stage 2: Summarize old conversation
        if self._estimate_total_tokens() > self.config.max_total_tokens * 0.7:
            self._compact_conversation()

        # Stage 3: Prune file cache
        if self._estimate_total_tokens() > self.config.max_total_tokens * 0.7:
            self._compact_file_cache()

        # Stage 4: Aggressive pruning if still over
        if self._estimate_total_tokens() > self.config.max_total_tokens:
            self._aggressive_compact()

        self.compaction_count += 1
        logger.info(f"Compaction complete. Tokens: {self._estimate_total_tokens()}")

    def _compact_tool_outputs(self) -> None:
        """Compress and prune tool outputs."""
        # Keep only recent tool outputs
        if len(self.tool_outputs) > self.config.keep_recent_tool_outputs:
            # Summarize older outputs
            older_outputs = self.tool_outputs[:-self.config.keep_recent_tool_outputs]
            summary = self._summarize_tool_outputs(older_outputs)
            if summary:
                self.summaries.append(summary)

            # Keep only recent
            self.tool_outputs = self.tool_outputs[-self.config.keep_recent_tool_outputs:]

        # Compress remaining outputs
        for output in self.tool_outputs:
            if not output.compressed:
                output.compress(self.config.max_tool_output_lines // 2)

        # Recalculate tokens
        self.tool_output_tokens = sum(
            len(o.output) // 4 + 1 for o in self.tool_outputs
        )

    def _compact_conversation(self) -> None:
        """Summarize older conversation turns."""
        if len(self.messages) <= self.config.keep_recent_messages:
            return

        # Split into old and recent
        split_point = len(self.messages) - self.config.keep_recent_messages
        old_messages = self.messages[:split_point]
        recent_messages = self.messages[split_point:]

        # Create summary of old messages
        summary = self._summarize_messages(old_messages)
        if summary:
            self.summaries.append(summary)

        # Keep only recent
        self.messages = recent_messages
        self.conversation_tokens = sum(m.estimated_tokens for m in self.messages)

    def _compact_file_cache(self) -> None:
        """Prune file cache to stay within limits."""
        if self.workspace.file_cache_tokens <= self.config.max_file_cache_tokens:
            return

        # Remove least recently used files
        # (Simple approach: remove files not in active_files)
        files_to_remove = []
        for path in self.workspace.file_cache:
            if path not in self.workspace.active_files:
                files_to_remove.append(path)

        for path in files_to_remove:
            del self.workspace.file_cache[path]

        self.workspace._update_cache_tokens()

    def _aggressive_compact(self) -> None:
        """Aggressive compaction when other methods aren't enough."""
        logger.warning("Performing aggressive context compaction")

        # Keep only last 5 messages
        if len(self.messages) > 5:
            self.messages = self.messages[-5:]
            self.conversation_tokens = sum(m.estimated_tokens for m in self.messages)

        # Keep only last 3 tool outputs
        if len(self.tool_outputs) > 3:
            self.tool_outputs = self.tool_outputs[-3:]
            self.tool_output_tokens = sum(
                len(o.output) // 4 + 1 for o in self.tool_outputs
            )

        # Clear file cache except active files
        self.workspace.file_cache = {
            path: content
            for path, content in self.workspace.file_cache.items()
            if path in self.workspace.active_files[:3]  # Keep max 3
        }
        self.workspace._update_cache_tokens()

    def _summarize_tool_outputs(self, outputs: List[ToolOutput]) -> str:
        """Create summary of tool outputs."""
        if not outputs:
            return ""

        summary_parts = [f"[Summary of {len(outputs)} tool executions]"]

        # Group by tool name
        by_tool: Dict[str, List[ToolOutput]] = {}
        for output in outputs:
            if output.tool_name not in by_tool:
                by_tool[output.tool_name] = []
            by_tool[output.tool_name].append(output)

        for tool_name, tool_outputs in by_tool.items():
            success_count = sum(1 for o in tool_outputs if o.success)
            fail_count = len(tool_outputs) - success_count

            summary_parts.append(
                f"- {tool_name}: {success_count} succeeded, {fail_count} failed"
            )

        return '\n'.join(summary_parts)

    def _summarize_messages(self, messages: List[Message]) -> str:
        """Create summary of conversation messages."""
        if not messages:
            return ""

        summary_parts = [f"[Summary of {len(messages)} earlier messages]"]

        # Count by role
        by_role: Dict[str, int] = {}
        for msg in messages:
            role = msg.role.value
            by_role[role] = by_role.get(role, 0) + 1

        for role, count in by_role.items():
            summary_parts.append(f"- {count} {role} messages")

        # Extract key topics (simple: look for common words)
        all_content = ' '.join(m.content[:500] for m in messages)
        # This is a simplified summary - in production would use LLM
        summary_parts.append(f"- Topics discussed: (conversation history)")

        return '\n'.join(summary_parts)

    def get_context_for_request(self) -> Dict[str, Any]:
        """Get context formatted for sending to server."""
        return {
            'workspace': {
                'root': str(self.workspace.root_path),
                'cwd': str(self.workspace.current_dir),
                'git_branch': self.workspace.git_branch,
                'git_status': self.workspace.git_status,
                'active_files': self.workspace.active_files
            },
            'summaries': self.summaries,
            'recent_messages': [
                {
                    'role': m.role.value,
                    'content': m.content[:1000],  # Truncate for request
                    'timestamp': m.timestamp.isoformat()
                }
                for m in self.messages[-5:]  # Last 5 messages
            ],
            'token_usage': {
                'conversation': self.conversation_tokens,
                'tool_outputs': self.tool_output_tokens,
                'file_cache': self.workspace.file_cache_tokens,
                'total': self._estimate_total_tokens()
            }
        }

    def get_full_context(self) -> Dict[str, Any]:
        """Get full context for local processing."""
        return {
            'workspace': {
                'root': str(self.workspace.root_path),
                'cwd': str(self.workspace.current_dir),
                'git_branch': self.workspace.git_branch,
                'git_status': self.workspace.git_status,
                'active_files': self.workspace.active_files,
                'file_cache': self.workspace.file_cache
            },
            'summaries': self.summaries,
            'messages': [
                {
                    'role': m.role.value,
                    'content': m.content,
                    'timestamp': m.timestamp.isoformat(),
                    'message_id': m.message_id,
                    'tool_id': m.tool_id,
                    'tool_name': m.tool_name
                }
                for m in self.messages
            ],
            'tool_outputs': [
                {
                    'tool_id': o.tool_id,
                    'tool_name': o.tool_name,
                    'args': o.args,
                    'output': o.output,
                    'success': o.success,
                    'compressed': o.compressed,
                    'timestamp': o.timestamp.isoformat()
                }
                for o in self.tool_outputs
            ]
        }

    def update_workspace_state(
        self,
        git_branch: Optional[str] = None,
        git_status: Optional[Dict[str, Any]] = None,
        current_dir: Optional[Path] = None
    ) -> None:
        """Update workspace state."""
        if git_branch is not None:
            self.workspace.git_branch = git_branch
        if git_status is not None:
            self.workspace.git_status = git_status
        if current_dir is not None:
            self.workspace.current_dir = current_dir

    def clear(self) -> None:
        """Clear all context."""
        self.messages.clear()
        self.tool_outputs.clear()
        self.summaries.clear()
        self.workspace.active_files.clear()
        self.workspace.file_cache.clear()

        self.conversation_tokens = 0
        self.tool_output_tokens = 0
        self.workspace.file_cache_tokens = 0
        self.compaction_count = 0

    def get_stats(self) -> Dict[str, Any]:
        """Get context statistics."""
        return {
            'messages': len(self.messages),
            'tool_outputs': len(self.tool_outputs),
            'summaries': len(self.summaries),
            'active_files': len(self.workspace.active_files),
            'cached_files': len(self.workspace.file_cache),
            'tokens': {
                'conversation': self.conversation_tokens,
                'tool_outputs': self.tool_output_tokens,
                'file_cache': self.workspace.file_cache_tokens,
                'total': self._estimate_total_tokens(),
                'max': self.config.max_total_tokens,
                'usage_percent': round(
                    self._estimate_total_tokens() / self.config.max_total_tokens * 100, 1
                )
            },
            'compaction_count': self.compaction_count
        }
