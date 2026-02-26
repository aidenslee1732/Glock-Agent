"""
Selective File Slicer for Model B.

Extracts relevant file slices based on:
1. Grep hits - 3 lines before, 7 after, extend to function boundary
2. Traceback lines - 2 before, 15 after (error context)
3. Changed hunks - 3 lines context each side
4. Function definitions - Full body when referenced
5. Call sites - 2 before, 5 after
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from packages.shared_protocol.types import FileSlice

logger = logging.getLogger(__name__)


@dataclass
class SliceConfig:
    """Configuration for file slicing."""
    # Context lines for different slice types
    grep_before: int = 3
    grep_after: int = 7
    traceback_before: int = 2
    traceback_after: int = 15
    change_context: int = 3
    call_site_before: int = 2
    call_site_after: int = 5

    # Maximum slice sizes
    max_slice_lines: int = 100
    max_total_slices: int = 20
    max_chars_per_slice: int = 4000


@dataclass
class SliceRequest:
    """A request for a file slice."""
    file_path: str
    line_number: int
    reason: str  # grep_hit, traceback, changed_hunk, function_def, call_site
    context: str = ""  # Additional context (e.g., function name)
    priority: int = 1  # Higher = more important


class SelectiveFileSlicer:
    """
    Extracts relevant file slices for context packing.

    Smart slicing strategies:
    - Extend to function boundaries when possible
    - Merge overlapping slices
    - Prioritize error-related slices
    - Preserve structural context (imports, class definitions)
    """

    def __init__(
        self,
        workspace_dir: str,
        config: Optional[SliceConfig] = None,
    ):
        self.workspace_dir = Path(workspace_dir).resolve()
        self.config = config or SliceConfig()

        # Cache file contents
        self._file_cache: dict[str, list[str]] = {}

    def clear_cache(self) -> None:
        """Clear the file content cache."""
        self._file_cache.clear()

    def slice(self, requests: list[SliceRequest]) -> list[FileSlice]:
        """
        Generate file slices for all requests.

        Args:
            requests: List of slice requests

        Returns:
            List of FileSlice objects
        """
        # Sort by priority
        sorted_requests = sorted(requests, key=lambda r: -r.priority)

        # Generate initial slices
        slices: list[FileSlice] = []

        for request in sorted_requests:
            if len(slices) >= self.config.max_total_slices:
                break

            try:
                slice_obj = self._create_slice(request)
                if slice_obj:
                    slices.append(slice_obj)
            except Exception as e:
                logger.warning(f"Failed to create slice for {request.file_path}: {e}")

        # Merge overlapping slices
        slices = self._merge_overlapping(slices)

        return slices

    def slice_for_grep_hit(
        self,
        file_path: str,
        line_number: int,
        pattern: str = "",
    ) -> Optional[FileSlice]:
        """Create slice for a grep hit."""
        return self._create_slice(SliceRequest(
            file_path=file_path,
            line_number=line_number,
            reason="grep_hit",
            context=pattern,
            priority=2,
        ))

    def slice_for_traceback(
        self,
        file_path: str,
        line_number: int,
        error_message: str = "",
    ) -> Optional[FileSlice]:
        """Create slice for a traceback line."""
        return self._create_slice(SliceRequest(
            file_path=file_path,
            line_number=line_number,
            reason="traceback",
            context=error_message,
            priority=5,  # High priority
        ))

    def slice_for_function(
        self,
        file_path: str,
        function_name: str,
    ) -> Optional[FileSlice]:
        """Create slice for a function definition."""
        lines = self._get_file_lines(file_path)
        if not lines:
            return None

        # Find function definition
        for i, line in enumerate(lines):
            if self._is_function_definition(line, function_name):
                return self._create_slice(SliceRequest(
                    file_path=file_path,
                    line_number=i + 1,  # 1-indexed
                    reason="function_def",
                    context=function_name,
                    priority=3,
                ))

        return None

    def slice_for_change(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
    ) -> Optional[FileSlice]:
        """Create slice for a changed hunk."""
        return self._create_slice(SliceRequest(
            file_path=file_path,
            line_number=(start_line + end_line) // 2,
            reason="changed_hunk",
            context=f"lines {start_line}-{end_line}",
            priority=4,
        ))

    def _create_slice(self, request: SliceRequest) -> Optional[FileSlice]:
        """Create a single file slice."""
        lines = self._get_file_lines(request.file_path)
        if not lines:
            return None

        line_idx = request.line_number - 1  # Convert to 0-indexed

        if line_idx < 0 or line_idx >= len(lines):
            logger.warning(f"Line {request.line_number} out of range for {request.file_path}")
            return None

        # Calculate context based on reason
        before, after = self._get_context_size(request.reason)

        # Calculate initial bounds
        start_idx = max(0, line_idx - before)
        end_idx = min(len(lines), line_idx + after + 1)

        # Try to extend to function boundaries
        if request.reason in ("grep_hit", "call_site"):
            start_idx, end_idx = self._extend_to_function_boundary(
                lines, start_idx, end_idx
            )

        # Enforce maximum slice size
        if end_idx - start_idx > self.config.max_slice_lines:
            # Keep centered on the target line
            half = self.config.max_slice_lines // 2
            start_idx = max(0, line_idx - half)
            end_idx = min(len(lines), start_idx + self.config.max_slice_lines)

        # Extract content
        slice_lines = lines[start_idx:end_idx]
        content = "\n".join(slice_lines)

        # Enforce character limit
        if len(content) > self.config.max_chars_per_slice:
            content = content[:self.config.max_chars_per_slice] + "\n... (truncated)"

        return FileSlice(
            file_path=request.file_path,
            start_line=start_idx + 1,  # Convert back to 1-indexed
            end_line=end_idx,
            content=content,
            reason=request.reason,
        )

    def _get_context_size(self, reason: str) -> tuple[int, int]:
        """Get (before, after) context lines for a reason."""
        context_map = {
            "grep_hit": (self.config.grep_before, self.config.grep_after),
            "traceback": (self.config.traceback_before, self.config.traceback_after),
            "changed_hunk": (self.config.change_context, self.config.change_context),
            "function_def": (0, self.config.max_slice_lines),  # Full function
            "call_site": (self.config.call_site_before, self.config.call_site_after),
        }
        return context_map.get(reason, (3, 5))

    def _get_file_lines(self, file_path: str) -> list[str]:
        """Get file lines (cached)."""
        if file_path in self._file_cache:
            return self._file_cache[file_path]

        try:
            full_path = self._resolve_path(file_path)
            if not full_path.exists():
                return []

            content = full_path.read_text()
            lines = content.split("\n")
            self._file_cache[file_path] = lines
            return lines

        except Exception as e:
            logger.warning(f"Failed to read {file_path}: {e}")
            return []

    def _resolve_path(self, file_path: str) -> Path:
        """Resolve path relative to workspace."""
        p = Path(file_path)
        if not p.is_absolute():
            p = self.workspace_dir / p
        return p.resolve()

    def _extend_to_function_boundary(
        self,
        lines: list[str],
        start_idx: int,
        end_idx: int,
    ) -> tuple[int, int]:
        """Extend slice to include complete function."""
        # Look backwards for function start
        new_start = start_idx
        indent_level = self._get_indent_level(lines[start_idx]) if start_idx < len(lines) else 0

        for i in range(start_idx - 1, max(0, start_idx - 20), -1):
            line = lines[i]
            if self._is_function_start(line):
                new_start = i
                break
            # Stop if we hit a lower indent level (different block)
            if line.strip() and self._get_indent_level(line) < indent_level:
                break

        # Look forwards for function end
        new_end = end_idx

        for i in range(end_idx, min(len(lines), end_idx + 30)):
            line = lines[i]
            # Stop at next function definition or class
            if i > end_idx and self._is_function_start(line):
                new_end = i
                break
            # Stop if we return to base indent level after content
            if (i > end_idx and line.strip() and
                self._get_indent_level(line) <= indent_level and
                not line.strip().startswith((")", "]", "}"))):
                new_end = i
                break

        return new_start, new_end

    def _is_function_start(self, line: str) -> bool:
        """Check if line starts a function definition."""
        stripped = line.strip()
        patterns = [
            r"^def\s+\w+\s*\(",
            r"^async\s+def\s+\w+\s*\(",
            r"^function\s+\w+\s*\(",
            r"^const\s+\w+\s*=\s*(async\s+)?(\(|function)",
            r"^(public|private|protected)?\s*(static\s+)?(async\s+)?\w+\s*\(",
        ]
        for pattern in patterns:
            if re.match(pattern, stripped):
                return True
        return False

    def _is_function_definition(self, line: str, name: str) -> bool:
        """Check if line defines a specific function."""
        stripped = line.strip()
        patterns = [
            rf"^def\s+{re.escape(name)}\s*\(",
            rf"^async\s+def\s+{re.escape(name)}\s*\(",
            rf"^function\s+{re.escape(name)}\s*\(",
            rf"^const\s+{re.escape(name)}\s*=",
        ]
        for pattern in patterns:
            if re.match(pattern, stripped):
                return True
        return False

    def _get_indent_level(self, line: str) -> int:
        """Get indentation level of a line."""
        if not line:
            return 0
        stripped = line.lstrip()
        if not stripped:
            return 0
        return len(line) - len(stripped)

    def _merge_overlapping(self, slices: list[FileSlice]) -> list[FileSlice]:
        """Merge overlapping slices for the same file."""
        if not slices:
            return slices

        # Group by file
        by_file: dict[str, list[FileSlice]] = {}
        for s in slices:
            if s.file_path not in by_file:
                by_file[s.file_path] = []
            by_file[s.file_path].append(s)

        merged: list[FileSlice] = []

        for file_path, file_slices in by_file.items():
            # Sort by start line
            sorted_slices = sorted(file_slices, key=lambda s: s.start_line)

            current: Optional[FileSlice] = None

            for s in sorted_slices:
                if current is None:
                    current = s
                elif s.start_line <= current.end_line + 1:
                    # Overlapping or adjacent - merge
                    lines = self._get_file_lines(file_path)
                    new_end = max(current.end_line, s.end_line)
                    new_content = "\n".join(
                        lines[current.start_line - 1:new_end]
                    )

                    current = FileSlice(
                        file_path=file_path,
                        start_line=current.start_line,
                        end_line=new_end,
                        content=new_content,
                        reason=f"{current.reason},{s.reason}",
                    )
                else:
                    # No overlap - save current and start new
                    merged.append(current)
                    current = s

            if current:
                merged.append(current)

        return merged
