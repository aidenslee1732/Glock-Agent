"""
Tool Output Compressor for Model B.

Compresses tool outputs to stay within token budgets while
preserving the most important information.

Per-tool limits:
- read_file: 4000 chars (need structure)
- grep: 2000 chars (just matches)
- bash: 2500 chars (errors important)
- glob: 1500 chars (just paths)
- default: 3000 chars
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class CompressionConfig:
    """Configuration for tool output compression."""
    # Per-tool character limits
    read_file: int = 4000
    grep: int = 2000
    bash: int = 2500
    glob: int = 1500
    list_directory: int = 2000
    edit_file: int = 1000
    write_file: int = 500
    default: int = 3000

    # Preserve error outputs (often critical)
    preserve_errors: bool = True
    error_limit: int = 2000

    # Keep first/last portions when truncating
    keep_head_ratio: float = 0.7
    keep_tail_ratio: float = 0.3


class ToolOutputCompressor:
    """
    Compresses tool outputs for context efficiency.

    Strategies:
    1. Truncation with head/tail preservation
    2. Path consolidation (group similar paths)
    3. Output summarization (for large outputs)
    4. Error preservation (always keep full errors)
    5. Whitespace normalization
    """

    def __init__(self, config: Optional[CompressionConfig] = None):
        self.config = config or CompressionConfig()

    def compress(
        self,
        tool_name: str,
        result: dict[str, Any],
        max_chars: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Compress a tool result.

        Args:
            tool_name: Name of the tool
            result: Tool execution result
            max_chars: Override max characters (uses tool default if None)

        Returns:
            Compressed result dictionary
        """
        if max_chars is None:
            max_chars = self._get_limit(tool_name)

        # Don't compress errors
        if result.get("status") == "error":
            return self._compress_error(result)

        # Dispatch to tool-specific compressor
        compressors = {
            "read_file": self._compress_read_file,
            "grep": self._compress_grep,
            "bash": self._compress_bash,
            "glob": self._compress_glob,
            "list_directory": self._compress_list_directory,
            "edit_file": self._compress_edit_file,
            "write_file": self._compress_write_file,
        }

        compressor = compressors.get(tool_name, self._compress_default)
        return compressor(result, max_chars)

    def _get_limit(self, tool_name: str) -> int:
        """Get character limit for tool."""
        return getattr(self.config, tool_name, self.config.default)

    def _compress_error(self, result: dict[str, Any]) -> dict[str, Any]:
        """Compress error result (preserve most of error message)."""
        error = result.get("error", "")
        if len(error) > self.config.error_limit:
            # Keep head and tail of error
            head_size = int(self.config.error_limit * 0.7)
            tail_size = self.config.error_limit - head_size - 20
            error = (
                error[:head_size] +
                "\n... (truncated) ...\n" +
                error[-tail_size:]
            )

        return {
            "status": "error",
            "error": error,
            "_compressed": True,
        }

    def _compress_read_file(
        self,
        result: dict[str, Any],
        max_chars: int,
    ) -> dict[str, Any]:
        """Compress read_file result."""
        content = result.get("result", {}).get("content", "")

        if len(content) <= max_chars:
            return result

        # Smart truncation: keep structure visible
        lines = content.split("\n")

        # Identify important lines (function defs, class defs, imports)
        important_patterns = [
            r"^(def |class |import |from |async def )",
            r"^(function |const |let |var |export |import )",
            r"^(public |private |protected |class |interface |def )",
        ]

        important_lines = set()
        for i, line in enumerate(lines):
            for pattern in important_patterns:
                if re.match(pattern, line.strip()):
                    important_lines.add(i)
                    # Include context around important lines
                    important_lines.add(max(0, i - 1))
                    important_lines.add(min(len(lines) - 1, i + 1))

        # Build compressed content
        if important_lines:
            # Show important lines with context
            compressed_lines = []
            last_shown = -2

            for i in sorted(important_lines):
                if i > last_shown + 1:
                    # Add ellipsis for gap
                    compressed_lines.append(f"... (lines {last_shown + 2}-{i} omitted) ...")
                compressed_lines.append(f"{i + 1}: {lines[i]}")
                last_shown = i

            if last_shown < len(lines) - 1:
                compressed_lines.append(f"... (lines {last_shown + 2}-{len(lines)} omitted) ...")

            compressed = "\n".join(compressed_lines)
        else:
            # Simple head/tail truncation
            compressed = self._truncate_head_tail(content, max_chars)

        return {
            "status": "success",
            "result": {
                "content": compressed,
                "path": result.get("result", {}).get("path"),
                "size": result.get("result", {}).get("size"),
                "_truncated": True,
                "_original_size": len(content),
            },
            "_compressed": True,
        }

    def _compress_grep(
        self,
        result: dict[str, Any],
        max_chars: int,
    ) -> dict[str, Any]:
        """Compress grep result."""
        matches = result.get("result", {}).get("matches", [])
        total = result.get("result", {}).get("total", len(matches))

        # Serialize and check length
        matches_str = "\n".join(matches)

        if len(matches_str) <= max_chars:
            return result

        # Truncate to fit
        kept_matches = []
        current_size = 0

        for match in matches:
            if current_size + len(match) + 1 > max_chars:
                break
            kept_matches.append(match)
            current_size += len(match) + 1

        return {
            "status": "success",
            "result": {
                "matches": kept_matches,
                "total": total,
                "_truncated": True,
                "_shown": len(kept_matches),
            },
            "_compressed": True,
        }

    def _compress_bash(
        self,
        result: dict[str, Any],
        max_chars: int,
    ) -> dict[str, Any]:
        """Compress bash result."""
        output = result.get("result", {}).get("output", "")
        exit_code = result.get("result", {}).get("exit_code", 0)

        if len(output) <= max_chars:
            return result

        # For errors, keep more of the tail (error messages usually at end)
        if exit_code != 0:
            compressed = self._truncate_head_tail(
                output,
                max_chars,
                head_ratio=0.3,
                tail_ratio=0.7,
            )
        else:
            compressed = self._truncate_head_tail(output, max_chars)

        return {
            "status": "success",
            "result": {
                "output": compressed,
                "exit_code": exit_code,
                "_truncated": True,
                "_original_size": len(output),
            },
            "_compressed": True,
        }

    def _compress_glob(
        self,
        result: dict[str, Any],
        max_chars: int,
    ) -> dict[str, Any]:
        """Compress glob result."""
        matches = result.get("result", {}).get("matches", [])
        total = result.get("result", {}).get("total", len(matches))

        # Group by directory for better compression
        by_dir: dict[str, list[str]] = {}
        for match in matches:
            parts = match.rsplit("/", 1)
            if len(parts) == 2:
                dir_part, file_part = parts
            else:
                dir_part, file_part = ".", parts[0]

            if dir_part not in by_dir:
                by_dir[dir_part] = []
            by_dir[dir_part].append(file_part)

        # Build compressed representation
        compressed_matches = []
        current_size = 0

        for dir_path, files in sorted(by_dir.items()):
            if len(files) == 1:
                entry = f"{dir_path}/{files[0]}"
            else:
                # Group files in same directory
                entry = f"{dir_path}/[{', '.join(files[:5])}{'...' if len(files) > 5 else ''}]"

            if current_size + len(entry) > max_chars:
                break

            compressed_matches.append(entry)
            current_size += len(entry) + 1

        return {
            "status": "success",
            "result": {
                "matches": compressed_matches,
                "total": total,
                "_truncated": len(compressed_matches) < len(matches),
                "_grouped": True,
            },
            "_compressed": True,
        }

    def _compress_list_directory(
        self,
        result: dict[str, Any],
        max_chars: int,
    ) -> dict[str, Any]:
        """Compress list_directory result."""
        entries = result.get("result", {}).get("entries", [])
        total = result.get("result", {}).get("total", len(entries))

        # Keep entries until we hit limit
        kept_entries = []
        current_size = 0

        for entry in entries:
            entry_str = f"{entry['name']} ({entry['type']})"
            if current_size + len(entry_str) > max_chars:
                break
            kept_entries.append(entry)
            current_size += len(entry_str) + 2

        return {
            "status": "success",
            "result": {
                "entries": kept_entries,
                "total": total,
                "_truncated": len(kept_entries) < len(entries),
            },
            "_compressed": True,
        }

    def _compress_edit_file(
        self,
        result: dict[str, Any],
        max_chars: int,
    ) -> dict[str, Any]:
        """Compress edit_file result."""
        # Edit results are usually small, just return as-is
        return result

    def _compress_write_file(
        self,
        result: dict[str, Any],
        max_chars: int,
    ) -> dict[str, Any]:
        """Compress write_file result."""
        # Write results are usually small, just return as-is
        return result

    def _compress_default(
        self,
        result: dict[str, Any],
        max_chars: int,
    ) -> dict[str, Any]:
        """Default compression for unknown tools."""
        result_str = json.dumps(result)

        if len(result_str) <= max_chars:
            return result

        # Truncate the JSON representation
        truncated = result_str[:max_chars - 20] + "... (truncated)"

        return {
            "status": result.get("status", "success"),
            "result": {"_raw_truncated": truncated},
            "_compressed": True,
        }

    def _truncate_head_tail(
        self,
        text: str,
        max_chars: int,
        head_ratio: float = 0.7,
        tail_ratio: float = 0.3,
    ) -> str:
        """Truncate text keeping head and tail portions."""
        if len(text) <= max_chars:
            return text

        # Calculate sizes
        ellipsis = "\n... (truncated) ...\n"
        available = max_chars - len(ellipsis)
        head_size = int(available * head_ratio)
        tail_size = available - head_size

        return text[:head_size] + ellipsis + text[-tail_size:]
