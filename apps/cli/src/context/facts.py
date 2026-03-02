"""
Pinned Facts Manager for Model B.

Manages up to 30 important facts that persist across context compaction:
- File paths from modifications
- Function/class names
- Error solutions
- User preferences
- Project constraints

Uses LRU eviction weighted by importance and use_count.
Integrates with MemoryStore for persistent storage.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING

from packages.shared_protocol.types import PinnedFact

if TYPE_CHECKING:
    from ..memory.store import MemoryStore

logger = logging.getLogger(__name__)


@dataclass
class FactsConfig:
    """Configuration for pinned facts."""
    max_facts: int = 30
    max_key_length: int = 100
    max_value_length: int = 500

    # Importance weights by category
    importance_weights: dict[str, float] = field(default_factory=lambda: {
        "error_solution": 1.5,
        "user_preference": 1.3,
        "constraint": 1.2,
        "file_path": 1.0,
        "function_name": 0.9,
        "variable": 0.7,
        "default": 0.5,
    })


class PinnedFactsManager:
    """
    Manages pinned facts for context packing.

    Facts are key pieces of information that should persist
    even when conversation history is compacted:
    - Discovered file locations
    - Important function names
    - Error solutions that worked
    - User's stated preferences

    Uses weighted LRU for eviction:
    - Higher importance = stays longer
    - More frequent use = stays longer
    - Recency also matters

    Optionally integrates with MemoryStore for persistent storage.
    """

    def __init__(
        self,
        config: Optional[FactsConfig] = None,
        memory_store: Optional["MemoryStore"] = None,
        workspace: Optional[str] = None,
    ):
        self.config = config or FactsConfig()
        self._facts: dict[str, PinnedFact] = {}
        self._memory_store = memory_store
        self._workspace = workspace

    @property
    def facts(self) -> list[PinnedFact]:
        """Get all facts sorted by importance."""
        return sorted(
            self._facts.values(),
            key=lambda f: self._compute_score(f),
            reverse=True,
        )

    def add_fact(
        self,
        key: str,
        value: str,
        category: str = "default",
        importance: Optional[float] = None,
        persist: bool = True,
    ) -> None:
        """
        Add or update a fact.

        Args:
            key: Fact identifier (e.g., "main_config_file")
            value: Fact value (e.g., "src/config.py")
            category: Category for importance weighting
            importance: Override importance (uses category default if None)
            persist: If True and memory_store is available, persist to disk
        """
        # Truncate key and value
        key = key[:self.config.max_key_length]
        value = value[:self.config.max_value_length]

        # Get importance from category if not specified
        if importance is None:
            importance = self.config.importance_weights.get(
                category,
                self.config.importance_weights["default"],
            )

        # Update existing or create new
        if key in self._facts:
            existing = self._facts[key]
            existing.value = value
            existing.use_count += 1
            existing.last_used_at = datetime.utcnow()
        else:
            self._facts[key] = PinnedFact(
                key=key,
                value=value,
                category=category,
                importance=importance,
                use_count=1,
                created_at=datetime.utcnow(),
                last_used_at=datetime.utcnow(),
            )

        # Persist to memory store if available
        if persist and self._memory_store is not None:
            try:
                self._memory_store.add(
                    key=key,
                    value=value,
                    category=category,
                    workspace=self._workspace,
                    importance=importance,
                )
            except Exception as e:
                logger.warning(f"Failed to persist fact to memory store: {e}")

        # Evict if over limit
        self._evict_if_needed()

    def get_fact(self, key: str) -> Optional[str]:
        """Get a fact value by key."""
        fact = self._facts.get(key)
        if fact:
            fact.use_count += 1
            fact.last_used_at = datetime.utcnow()
            return fact.value
        return None

    def remove_fact(self, key: str) -> bool:
        """Remove a fact."""
        if key in self._facts:
            del self._facts[key]
            return True
        return False

    def extract_from_content(self, content: str, role: str = "assistant") -> None:
        """
        Extract facts from conversation content.

        Looks for:
        - File paths mentioned
        - Function/class names
        - Error messages and their solutions
        - User preferences (from user messages)
        """
        # Extract file paths
        file_patterns = [
            r'(?:file|path):\s*["\']?([^"\'<>\s]+\.\w+)["\']?',
            r'(?:in|at)\s+["\']?([^"\'<>\s]+\.\w+)["\']?',
            r'["\']([^"\'<>\s]+(?:\.py|\.js|\.ts|\.tsx|\.json|\.yaml|\.yml))["\']',
        ]

        for pattern in file_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches[:5]:  # Limit per message
                # Clean up the path
                path = match.strip("\"'`")
                if "/" in path or "\\" in path:
                    key = f"file:{path.split('/')[-1]}"
                    self.add_fact(key, path, "file_path")

        # Extract function names from code blocks
        code_pattern = r'```[\w]*\n(.*?)```'
        code_blocks = re.findall(code_pattern, content, re.DOTALL)

        for block in code_blocks:
            # Python functions
            func_matches = re.findall(r'def\s+(\w+)\s*\(', block)
            for func in func_matches[:3]:
                self.add_fact(f"func:{func}", f"Function: {func}", "function_name")

            # Python classes
            class_matches = re.findall(r'class\s+(\w+)\s*[:\(]', block)
            for cls in class_matches[:3]:
                self.add_fact(f"class:{cls}", f"Class: {cls}", "function_name")

        # Extract error solutions (if assistant mentions fixing something)
        if role == "assistant":
            solution_patterns = [
                r'(?:fixed|resolved|the issue was|the problem was)\s+(.{20,100})',
                r'(?:solution|fix):\s*(.{20,100})',
            ]

            for pattern in solution_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches[:2]:
                    key = f"solution:{match[:30].strip()}"
                    self.add_fact(key, match.strip(), "error_solution")

        # Extract user preferences (from user messages)
        if role == "user":
            pref_patterns = [
                r'(?:I prefer|I want|please use|always|never)\s+(.{10,100})',
                r'(?:don\'t|do not)\s+(.{10,50})',
            ]

            for pattern in pref_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches[:2]:
                    key = f"pref:{match[:30].strip()}"
                    self.add_fact(key, match.strip(), "user_preference")

    def extract_from_tool_result(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Extract facts from tool execution."""
        # File operations
        if tool_name in ("read_file", "edit_file", "write_file"):
            file_path = args.get("file_path", "")
            if file_path:
                key = f"file:{file_path.split('/')[-1]}"
                self.add_fact(key, file_path, "file_path")

        # Grep results - remember important files
        elif tool_name == "grep":
            matches = result.get("result", {}).get("matches", [])
            for match in matches[:5]:
                if ":" in match:
                    file_path = match.split(":")[0]
                    key = f"grep_match:{file_path.split('/')[-1]}"
                    self.add_fact(key, file_path, "file_path")

        # Bash commands that reveal project info
        elif tool_name == "bash":
            command = args.get("command", "")
            output = result.get("result", {}).get("output", "")

            # Package manager detection
            if "npm" in command or "yarn" in command or "pnpm" in command:
                self.add_fact("pkg_manager", "npm/yarn", "constraint")
            elif "pip" in command or "poetry" in command:
                self.add_fact("pkg_manager", "pip/poetry", "constraint")

            # Test framework detection
            if "pytest" in command:
                self.add_fact("test_framework", "pytest", "constraint")
            elif "jest" in command:
                self.add_fact("test_framework", "jest", "constraint")

    def _compute_score(self, fact: PinnedFact) -> float:
        """Compute eviction score (higher = keep longer)."""
        # Base importance
        score = fact.importance

        # Use count bonus (logarithmic)
        import math
        score *= (1 + math.log(max(1, fact.use_count)))

        # Recency bonus
        if fact.last_used_at:
            age_hours = (datetime.utcnow() - fact.last_used_at).total_seconds() / 3600
            recency_factor = 1 / (1 + age_hours / 24)  # Decay over 24 hours
            score *= (0.5 + 0.5 * recency_factor)

        return score

    def _evict_if_needed(self) -> None:
        """Evict lowest-scored facts if over limit."""
        while len(self._facts) > self.config.max_facts:
            # Find fact with lowest score
            lowest_key = min(
                self._facts.keys(),
                key=lambda k: self._compute_score(self._facts[k]),
            )
            del self._facts[lowest_key]
            logger.debug(f"Evicted fact: {lowest_key}")

    def get_facts_text(self) -> str:
        """Get facts as formatted text for context."""
        if not self._facts:
            return ""

        lines = ["Important facts:"]
        for fact in self.facts:
            lines.append(f"  - {fact.key}: {fact.value}")

        return "\n".join(lines)

    def estimate_tokens(self) -> int:
        """Estimate token count."""
        text = self.get_facts_text()
        return len(text) // 4 + 1

    def reset(self) -> None:
        """Clear all facts."""
        self._facts.clear()

    def set_memory_store(self, memory_store: "MemoryStore", workspace: Optional[str] = None) -> None:
        """Set the memory store for persistence.

        Args:
            memory_store: MemoryStore instance
            workspace: Workspace path for scoping
        """
        self._memory_store = memory_store
        self._workspace = workspace

    def load_from_memory_store(self, limit: int = 30) -> int:
        """Load facts from memory store.

        Args:
            limit: Maximum facts to load

        Returns:
            Number of facts loaded
        """
        if self._memory_store is None:
            return 0

        try:
            # Get relevant memories from store
            memories = self._memory_store.get_all_for_context(
                workspace=self._workspace,
                max_tokens=self.config.max_facts * 50,  # Rough estimate
            )

            if not memories:
                return 0

            # Parse the formatted text and add facts
            loaded = 0
            for line in memories.split("\n"):
                if line.strip().startswith("- "):
                    # Parse "- key: value" format
                    content = line.strip()[2:]  # Remove "- "
                    if ": " in content:
                        key, value = content.split(": ", 1)
                        self.add_fact(key.strip(), value.strip(), persist=False)
                        loaded += 1
                        if loaded >= limit:
                            break

            logger.debug(f"Loaded {loaded} facts from memory store")
            return loaded

        except Exception as e:
            logger.warning(f"Failed to load facts from memory store: {e}")
            return 0

    def extract_and_persist(
        self,
        content: str,
        role: str = "assistant",
        source: Optional[str] = None,
    ) -> list[PinnedFact]:
        """Extract facts from content and persist to memory store.

        Args:
            content: Content to extract facts from
            role: Message role ("user" or "assistant")
            source: Optional source identifier

        Returns:
            List of extracted facts
        """
        # Extract facts using existing method
        self.extract_from_content(content, role)

        # Return the current facts list
        return self.facts

    def sync_to_memory_store(self) -> int:
        """Sync all current facts to memory store.

        Returns:
            Number of facts synced
        """
        if self._memory_store is None:
            return 0

        synced = 0
        for fact in self._facts.values():
            try:
                self._memory_store.add(
                    key=fact.key,
                    value=fact.value,
                    category=fact.category,
                    workspace=self._workspace,
                    importance=fact.importance,
                )
                synced += 1
            except Exception as e:
                logger.warning(f"Failed to sync fact {fact.key}: {e}")

        logger.debug(f"Synced {synced} facts to memory store")
        return synced
